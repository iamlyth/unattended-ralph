import { spawn, spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import { chmod, lstat, rename, truncate, unlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SHIM = "./scripts/pi-cli-shims/ralph";
const GIT_SHIM = "./scripts/pi-cli-shims/git";
const DIRECT_PREFIX = /^[ \t]*ralph(?=[ \t]+emit(?:[ \t]|$))/;
const LIFECYCLE_TOKENS = [
  "PLAN_COMPLETE",
  "LOOP_COMPLETE",
  "AUDIT_COMPLETE",
  "MAINTENANCE_PLAN_COMPLETE",
  "MAINTENANCE_COMPLETE",
];

// Commit-creating Git verbs. Simple direct forms are routed through the
// argv-level shim (scripts/pi-cli-shims/git) so hook-bypass flags and missing
// guards fail before Git runs; anything more complex still lands in the
// fail-closed pre-commit boundary (scripts/git-commit-guard.sh).
const GIT_COMMIT_VERBS = "(?:commit|merge|cherry-pick|revert|am|rebase|pull)";
const GIT_DIRECT = new RegExp(`^[ \t]*git(?=[ \t]+${GIT_COMMIT_VERBS}(?:[ \t]|$))`);
const GIT_C_PREFIX = new RegExp("^[ \\t]*git(?=(?:[ \\t]+-C[ \\t]+[^;&|<>(){}`$#\\\\n\\\\r \\t]+)+[ \\t]+" + GIT_COMMIT_VERBS + "(?:[ \\t]|$))");
const GIT_CD_PREFIX = new RegExp("^[ \t]*cd[ \t]+[^;&|<>(){}`$#\\n\\r \t]+[ \t]*&&[ \t]+git(?=[ \t]+" + GIT_COMMIT_VERBS + "(?:[ \t]|$))");
// Fail-closed markers that disable or redirect the commit boundary. None of
// these have any legitimate use in the Ralph harness, so any git-invoking
// command that carries one is blocked before execution.
const GIT_BYPASS = /(?:--no-verify|core\.hookspath|GIT_CONFIG_(?:COUNT|PARAMETERS|KEY_|VALUE_)|--(?:git-dir|work-tree)(?:=|[ \t]))/i;
// Commit-creation verbs with no Git-hook coverage, or whose patch/replay
// semantics this boundary does not own, are blocked wherever they appear as
// the git verb (including chained command positions the rewrite regexes
// cannot reach). Only `git commit` may create commits from the model's
// command boundary. The verb must directly follow git, so words in commit
// messages or arguments never trigger this.
const GIT_UNGUARDED = /(?:^|(?:&&|[;&|])[ \t]*)[ \t]*git(?:[ \t]+-C[ \t]+[^;&|<>(){}`$#\n\r \t]+)*[ \t]+(?:cherry-pick|revert|rebase|pull|am)(?:[ \t]|$)/;

function shellWords(command) {
  const words = [];
  let word = "";
  let active = false;
  let quote = null;
  let escaped = false;
  let unsupported = false;
  const finish = () => {
    if (active) words.push(word);
    word = "";
    active = false;
  };
  for (let index = 0; index < command.length; index += 1) {
    const char = command[index];
    if (escaped) {
      word += char;
      active = true;
      escaped = false;
      continue;
    }
    if (quote === "'") {
      if (char === "'") quote = null;
      else word += char;
      active = true;
      continue;
    }
    if (quote === '"') {
      if (char === '"') quote = null;
      else if (char === "\\") escaped = true;
      else {
        // Any shell expansion makes the final emit argv unknowable to the
        // extension. The argv-level shim performs the second mandatory check.
        if (char === "`" || char === "$") unsupported = true;
        word += char;
      }
      active = true;
      continue;
    }
    if (char === "\\") {
      escaped = true;
      active = true;
    } else if (char === "'" || char === '"') {
      quote = char;
      active = true;
    } else if (char === " " || char === "\t") {
      finish();
    } else if (";|&<>(){}\n\r".includes(char) || char === "#" || char === "`") {
      finish();
      unsupported = true;
    } else {
      if (char === "$") unsupported = true;
      word += char;
      active = true;
    }
  }
  finish();
  if (quote !== null || escaped) unsupported = true;
  return { words, unsupported };
}

export function rewriteRalphEmitCommand(command) {
  if (typeof command !== "string") {
    return { command, matched: false, unsafe: false, reserved: false };
  }
  const parsed = shellWords(command);
  const emitIndexes = parsed.words.flatMap(
    (word, index) => word === "ralph" && parsed.words[index + 1] === "emit" ? [index] : [],
  );
  const adjacentEmit = emitIndexes.length > 0;
  const containsEmit = adjacentEmit || (
    parsed.unsupported && /\bralph[ \t]+emit(?:[ \t]|$)/.test(command)
  );
  const reserved = emitIndexes.some((index) =>
    parsed.words.slice(index + 2).some((word) =>
      LIFECYCLE_TOKENS.some((token) => word.includes(token)),
    ),
  );
  if (reserved) {
    return { command, matched: false, unsafe: true, reserved: true };
  }
  if (
    !parsed.unsupported
    && parsed.words[0] === "ralph"
    && parsed.words[1] === "emit"
    && DIRECT_PREFIX.test(command)
  ) {
    return {
      command: command.replace(DIRECT_PREFIX, (prefix) => prefix.replace(/ralph$/, SHIM)),
      matched: true,
      unsafe: false,
      reserved: false,
    };
  }
  return { command, matched: false, unsafe: containsEmit, reserved: false };
}

export function rewriteGitCommitCommand(command) {
  if (typeof command !== "string") {
    return { command, matched: false, blocked: false, reason: "" };
  }
  if (GIT_BYPASS.test(command) && /\bgit\b/.test(command)) {
    return {
      command,
      matched: false,
      blocked: true,
      reason: "Git boundary redirection/bypass markers (--no-verify, --git-dir, --work-tree, core.hooksPath, GIT_CONFIG_*) are forbidden: they would bypass the fail-closed commit boundary.",
    };
  }
  if (GIT_UNGUARDED.test(command)) {
    return {
      command,
      matched: false,
      blocked: true,
      reason: "Only git commit can create commits from the command boundary: cherry-pick/revert run no Git hooks and am/rebase/pull bypass the fail-closed boundary.",
    };
  }
  if (GIT_DIRECT.test(command) || GIT_C_PREFIX.test(command)) {
    return {
      command: command.replace(/^([ \t]*)git(?=[ \t]+)/, `$1${GIT_SHIM}`),
      matched: true,
      blocked: false,
      reason: "",
    };
  }
  if (GIT_CD_PREFIX.test(command)) {
    return {
      command: command.replace(/([ \t]*&&[ \t]+)git(?=[ \t]+)/, `$1${GIT_SHIM}`),
      matched: true,
      blocked: false,
      reason: "",
    };
  }
  return { command, matched: false, blocked: false, reason: "" };
}

// ---------------------------------------------------------------------------
// Credential guardrail: fail-closed tool input classification and tool result
// redaction via the tracked sibling scripts/credential-guard.py. The guard
// never echoes its input: verdicts come back as one compact JSON line on
// stdout, and redaction masks secrets on stdin -> stdout.
// Production always uses the tracked sibling and cannot be redirected by
// environment variables. Pure test helpers accept an explicit guardPath
// option; the registered runtime hooks never pass one.
//
// Residual raw-overflow window: pi's bash tool begins streaming full command
// output into tmpdir()/pi-bash-<hex>.log as soon as accumulated output
// exceeds the truncation limits, so the raw (unredacted) file exists on disk
// while the command is still running and until this tool_result hook has
// sanitized it. If pi is killed or crashes inside that window the raw file
// remains. The hook therefore redacts the recognized owned file in place and
// fails the whole tool result closed when it cannot. The window is bounded
// to the lifetime of one bash tool result, but it is not zero.
// ---------------------------------------------------------------------------

const GUARD_PYTHON = "python3";
const CREDENTIAL_GUARD_SCHEMA = "credential-guard/v1";
const CREDENTIAL_GUARD_TOOL = "credential-guard";
const CREDENTIAL_GUARD_MODES = new Set(["check-command-stdin", "check-path-stdin"]);
const GUARD_CHECK_TIMEOUT_MS = 3000; // verdicts must answer within 3 seconds
const GUARD_CHECK_MAX_BUFFER = 1 << 20; // the verdict JSON is tiny; bound stdout
const GUARD_STDIN_LIMIT = 1 << 20; // mirrors the guard's own 1 MiB stdin bound
const REDACT_TIMEOUT_MS = 3000;
const REDACT_MAX_BUFFER = 8 << 20; // redacted text mirrors input size
const REDACT_INPUT_LIMIT = 8 << 20;
const OVERFLOW_REDACT_TIMEOUT_MS = 30000; // bounded streaming redaction of the overflow file
const BASH_OVERFLOW_RE = /^pi-bash-([0-9a-f]{16})\.log$/;
const TOOLCALL_BLOCK_PREFIX = "credential guard blocked unsafe tool input: ";
const REDACTION_FAILED = "[REDACTION FAILED]";

/** Resolve the immutable tracked credential guard executable. */
export function resolveGuardPath() {
  return fileURLToPath(new URL("./credential-guard.py", import.meta.url));
}

function currentUid() {
  return typeof process.getuid === "function" ? process.getuid() : -1;
}

/** Identity predicate for the overflow log: not a symlink, a regular file,
 * owned by the current user, with exactly one hard link. */
export function isOwnedRegularFile(stats) {
  if (!stats || typeof stats !== "object") return false;
  if (typeof stats.isFile !== "function" || !stats.isFile()) return false;
  if (typeof stats.isSymbolicLink === "function" && stats.isSymbolicLink()) return false;
  if (stats.nlink !== 1) return false;
  if (stats.uid !== currentUid()) return false;
  return true;
}

/** Accept exactly the canonical tmpdir()/pi-bash-<16 hex>.log shape that pi's
 * bash tool writes; anything else is rejected so arbitrary paths are never
 * chmodded, truncated, renamed, or read. */
export function parseBashOverflowPath(fullOutputPath) {
  if (typeof fullOutputPath !== "string" || fullOutputPath.length === 0) return null;
  const base = basename(fullOutputPath);
  const match = BASH_OVERFLOW_RE.exec(base);
  if (!match) return null;
  const canonical = join(tmpdir(), base);
  if (resolve(fullOutputPath) !== resolve(canonical)) return null;
  return { id: match[1], canonical };
}

/** Strict compact-JSON verdict parse. Anything that is not exactly one
 * credential-guard/v1 result line is rejected (fail closed). */
export function parseStrictGuardLine(stdout) {
  if (typeof stdout !== "string") return null;
  const line = stdout.trim();
  if (line.length === 0) return null;
  let parsed;
  try {
    parsed = JSON.parse(line);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  if (parsed.schema !== CREDENTIAL_GUARD_SCHEMA) return null;
  if (parsed.tool !== CREDENTIAL_GUARD_TOOL) return null;
  if (typeof parsed.version !== "string" || parsed.version.length === 0) return null;
  if (parsed.verdict !== "allow" && parsed.verdict !== "block") return null;
  if (typeof parsed.reason !== "string" || parsed.reason.length === 0) return null;
  if (!Array.isArray(parsed.reasons)) return null;
  return { verdict: parsed.verdict, reason: parsed.reason, reasons: parsed.reasons };
}

/** Run one stdin-based guard subcommand (check-command-stdin or
 * check-path-stdin) with the input piped through stdin. A missing guard,
 * crash, timeout, oversized/invalid input, and invalid output all fail
 * closed without ever echoing the input. */
export function runGuardCheck(mode, input, options = {}) {
  if (!CREDENTIAL_GUARD_MODES.has(mode)) {
    return { allowed: false, verdict: "block", reason: "invalid-mode" };
  }
  if (typeof input !== "string") {
    return { allowed: false, verdict: "block", reason: "invalid-input" };
  }
  if (Buffer.byteLength(input, "utf8") > GUARD_STDIN_LIMIT) {
    return { allowed: false, verdict: "block", reason: "oversized-input" };
  }
  const env = options.env ?? process.env;
  const guardPath = options.guardPath ?? resolveGuardPath();
  const result = spawnSync(GUARD_PYTHON, [guardPath, mode], {
    input,
    encoding: "utf8",
    timeout: options.timeout ?? GUARD_CHECK_TIMEOUT_MS,
    maxBuffer: options.maxBuffer ?? GUARD_CHECK_MAX_BUFFER,
    killSignal: "SIGKILL",
    env,
  });
  if (result.error || result.signal || result.status === null) {
    return { allowed: false, verdict: "block", reason: "guard-unavailable" };
  }
  const parsed = parseStrictGuardLine(result.stdout);
  if (!parsed) {
    return { allowed: false, verdict: "block", reason: "invalid-guard-output" };
  }
  const allowed = parsed.verdict === "allow";
  return { allowed, verdict: parsed.verdict, reason: parsed.reason, reasons: parsed.reasons };
}

/** Redact one text chunk through the guard's streaming filter. Returns
 * {ok:false} on any failure so callers fail closed with no original text. */
export function redactText(text, options = {}) {
  if (typeof text !== "string") return { ok: false, text: null };
  if (text.length === 0) return { ok: true, text: "" };
  if (Buffer.byteLength(text, "utf8") > REDACT_INPUT_LIMIT) {
    return { ok: false, text: null };
  }
  const env = options.env ?? process.env;
  const guardPath = options.guardPath ?? resolveGuardPath();
  const result = spawnSync(GUARD_PYTHON, [guardPath, "redact"], {
    input: text,
    encoding: "utf8",
    timeout: options.timeout ?? REDACT_TIMEOUT_MS,
    maxBuffer: options.maxBuffer ?? REDACT_MAX_BUFFER,
    killSignal: "SIGKILL",
    env,
  });
  if (result.error || result.signal || result.status === null || result.status !== 0) {
    return { ok: false, text: null };
  }
  return { ok: true, text: result.stdout };
}

/** Redact every string value in one JSON-shaped payload with one bounded
 * guard subprocess. This avoids one subprocess per nested string. */
export function redactDeep(value, options = {}) {
  let input;
  try {
    input = JSON.stringify(value);
  } catch {
    return { ok: false, value: null };
  }
  if (typeof input !== "string" || Buffer.byteLength(input, "utf8") > REDACT_INPUT_LIMIT) {
    return { ok: false, value: null };
  }
  const env = options.env ?? process.env;
  const guardPath = options.guardPath ?? resolveGuardPath();
  const result = spawnSync(GUARD_PYTHON, [guardPath, "redact-json-stdin"], {
    input,
    encoding: "utf8",
    timeout: options.timeout ?? REDACT_TIMEOUT_MS,
    maxBuffer: options.maxBuffer ?? REDACT_MAX_BUFFER,
    killSignal: "SIGKILL",
    env,
  });
  if (result.error || result.signal || result.status !== 0) {
    return { ok: false, value: null };
  }
  try {
    return { ok: true, value: JSON.parse(result.stdout) };
  } catch {
    return { ok: false, value: null };
  }
}

function guardTempPath(id) {
  return join(tmpdir(), `pi-bash-${id}.credential-guard-${randomBytes(6).toString("hex")}.tmp`);
}

/** Stream the recognized overflow file through the guard's redact filter into
 * an exclusive-creation, owner-only (0600) temp in the same directory. The
 * promise settles only after the child exits AND the temp stream has flushed
 * (or any error/timeout fires), so the caller can rename safely. */
function redactFileViaGuard(originalPath, tempPath, env, guardPath) {
  return new Promise((resolvePromise, rejectPromise) => {
    let settled = false;
    const child = spawn(GUARD_PYTHON, [guardPath ?? resolveGuardPath(), "redact"], {
      stdio: ["pipe", "pipe", "pipe"],
      env,
      windowsHide: true,
    });
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      outStream.destroy();
      childDone = true;
      outDone = true;
      childError = childError ?? new Error("credential guard redact timed out");
      maybeFinish();
    }, OVERFLOW_REDACT_TIMEOUT_MS);
    const inStream = createReadStream(originalPath);
    const outStream = createWriteStream(tempPath, { flags: "wx", mode: 0o600 });
    let childDone = false;
    let childCode = null;
    let childError = null;
    let outDone = false;
    let outError = null;
    const finish = (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      inStream.destroy();
      child.stdin?.destroy();
      child.stdout?.destroy();
      child.kill("SIGKILL");
      if (err) rejectPromise(err);
      else resolvePromise();
    };
    const maybeFinish = () => {
      if (settled || !childDone || !outDone) return;
      const err =
        childError ??
        outError ??
        (childCode !== 0 ? new Error(`credential guard redact exited ${childCode}`) : null);
      finish(err);
    };
    child.once("error", (err) => {
      // Spawn failure: no stdout will arrive; end the pipeline now.
      childError = err;
      childDone = true;
      outDone = true;
      outStream.destroy();
      maybeFinish();
    });
    child.once("exit", (code) => {
      childCode = code;
      childDone = true;
      maybeFinish();
    });
    child.stdin.once("error", () => {});
    inStream.once("error", () => child.stdin?.destroy());
    outStream.once("error", (err) => {
      child.kill("SIGKILL");
      outError = err;
      outDone = true;
      maybeFinish();
    });
    outStream.once("finish", () => {
      outDone = true;
      maybeFinish();
    });
    inStream.pipe(child.stdin);
    child.stdout.pipe(outStream);
  });
}

/** Truncate the recognized owned overflow file to zero. Never touches
 * arbitrary paths: only canonical tmpdir()/pi-bash-<hex>.log files that still
 * pass the identity predicate. Returns true when truncated. */
export async function truncateOwnedOverflowFile(fullOutputPath) {
  const parsed = parseBashOverflowPath(fullOutputPath);
  if (!parsed) return false;
  try {
    const stats = await lstat(parsed.canonical);
    if (!isOwnedRegularFile(stats)) return false;
    await truncate(parsed.canonical, 0);
    return true;
  } catch {
    return false;
  }
}

async function unlinkQuiet(pathToUnlink) {
  try {
    await unlink(pathToUnlink);
  } catch {
    /* best-effort temp cleanup */
  }
}

/** Sanitize a recognized bash overflow log: chmod 0600, stream it through the
 * guard into an owner-only same-directory temp, recheck identity, then
 * atomically rename. On any failure the recognized owned regular file is
 * truncated to zero and { ok:false } is returned so the tool result is
 * fail-redacted. Never touches arbitrary paths. */
export async function sanitizeBashOverflowPath(fullOutputPath, options = {}) {
  const env = options.env ?? process.env;
  const parsed = parseBashOverflowPath(fullOutputPath);
  if (!parsed) return { ok: false, reason: "non-canonical-overflow-path" };
  const original = parsed.canonical;
  let firstStats;
  try {
    firstStats = await lstat(original);
  } catch {
    return { ok: false, reason: "overflow-file-missing" };
  }
  if (!isOwnedRegularFile(firstStats)) {
    return { ok: false, reason: "overflow-file-identity" };
  }
  try {
    await chmod(original, 0o600);
  } catch {
    await truncateOwnedOverflowFile(original);
    return { ok: false, reason: "overflow-chmod-failed" };
  }
  const tempPath = guardTempPath(parsed.id);
  try {
    await redactFileViaGuard(original, tempPath, env, options.guardPath);
  } catch {
    await unlinkQuiet(tempPath);
    await truncateOwnedOverflowFile(original);
    return { ok: false, reason: "overflow-redact-failed" };
  }
  try {
    const recheck = await lstat(original);
    if (
      !isOwnedRegularFile(recheck)
      || recheck.dev !== firstStats.dev
      || recheck.ino !== firstStats.ino
    ) {
      await unlinkQuiet(tempPath);
      await truncateOwnedOverflowFile(original);
      return { ok: false, reason: "overflow-identity-recheck-failed" };
    }
    const tempStats = await lstat(tempPath);
    if (!isOwnedRegularFile(tempStats) || (tempStats.mode & 0o077) !== 0) {
      await unlinkQuiet(tempPath);
      await truncateOwnedOverflowFile(original);
      return { ok: false, reason: "overflow-temp-identity-failed" };
    }
    await rename(tempPath, original);
    return { ok: true, reason: "" };
  } catch {
    await unlinkQuiet(tempPath);
    await truncateOwnedOverflowFile(original);
    return { ok: false, reason: "overflow-sanitize-failed" };
  }
}

const TOOL_CALL_GUARDED_INPUTS = {
  bash: (input) => (input && typeof input === "object" ? input.command : undefined),
  read: (input) => (input && typeof input === "object" ? input.path : undefined),
  write: (input) => (input && typeof input === "object" ? input.path : undefined),
  edit: (input) => (input && typeof input === "object" ? input.path : undefined),
};

/** Guard one tool call before any rewrite: bash commands through
 * check-command-stdin, read/write/edit paths through check-path-stdin. A
 * block returns the fail-closed pi block payload with a stable reason that
 * never echoes the input; allowed calls return null so the existing
 * ralph/git rewrites still run. */
export function guardToolCallInput(event, options = {}) {
  if (!event || typeof event !== "object") return null;
  const pick = TOOL_CALL_GUARDED_INPUTS[event.toolName];
  if (!pick) return null;
  const inputText = pick(event.input);
  if (typeof inputText !== "string") return null;
  const mode = event.toolName === "bash" ? "check-command-stdin" : "check-path-stdin";
  const verdict = runGuardCheck(mode, inputText, options);
  if (!verdict.allowed) {
    return {
      block: true,
      reason: `${TOOLCALL_BLOCK_PREFIX}${verdict.reason}`,
    };
  }
  return null;
}

/** Redact every text content item and every string in details. Returns
 * { failed:false, patch } with a partial tool_result patch that preserves
 * arrays/object shape, isError, and usage (omitted fields stay untouched), or
 * { failed:true, patch:null } so the caller can fail the whole result. */
export function redactToolResultPatch(event, options = {}) {
  const payload = {};
  if (event.content !== undefined) payload.content = event.content;
  if (event.details !== undefined) payload.details = event.details;
  const redacted = redactDeep(payload, options);
  if (!redacted.ok || !redacted.value || typeof redacted.value !== "object") {
    return { failed: true, patch: null };
  }
  const patch = {};
  if (Object.prototype.hasOwnProperty.call(payload, "content")) {
    patch.content = redacted.value.content;
  }
  if (Object.prototype.hasOwnProperty.call(payload, "details")) {
    patch.details = redacted.value.details;
  }
  return { failed: false, patch };
}

/** Fail-closed tool_result outcome: content is replaced by a single
 * [REDACTION FAILED] text item, details are cleared by direct mutation (the
 * runner merges only non-undefined patch fields, so the mutation is what
 * drops them), and isError/usage are untouched. No original text or error
 * detail is included. */
export function failRedactedResult(event) {
  event.details = undefined;
  return { content: [{ type: "text", text: REDACTION_FAILED }] };
}

export default function registerRalphEmitShim(pi) {
  pi.on("tool_call", (event) => {
    const guarded = guardToolCallInput(event);
    if (guarded) {
      return guarded;
    }
    if (event.toolName !== "bash") return;
    const ralph = rewriteRalphEmitCommand(event.input?.command);
    if (ralph.reserved) {
      return {
        block: true,
        reason: "Lifecycle completion tokens are model-output protocol lines, not Ralph event topics or payloads.",
      };
    }
    if (ralph.matched) {
      event.input.command = ralph.command;
    } else if (ralph.unsafe) {
      return {
        block: true,
        reason: "Run `ralph emit` as the direct final command with no chaining or redirects so the factory can publish it safely.",
      };
    }
    const git = rewriteGitCommitCommand(event.input.command);
    if (git.blocked) {
      return { block: true, reason: git.reason };
    }
    if (git.matched) {
      event.input.command = git.command;
    }
  });

  pi.on("tool_result", async (event) => {
    if (
      event.toolName === "bash"
      && event.details
      && typeof event.details === "object"
      && typeof event.details.fullOutputPath === "string"
    ) {
      const sanitized = await sanitizeBashOverflowPath(event.details.fullOutputPath);
      if (!sanitized.ok) {
        return failRedactedResult(event);
      }
    }
    const redacted = redactToolResultPatch(event);
    if (redacted.failed) {
      return failRedactedResult(event);
    }
    return redacted.patch;
  });
}
