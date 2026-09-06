import { spawn, spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { createHash } from "node:crypto";
import { once } from "node:events";
import {
  closeSync,
  constants as fsConstants,
  fsyncSync,
  fstatSync,
  lstatSync,
  openSync,
  readdirSync,
  readFileSync,
  realpathSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import { lstat, open, unlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// Production stages this extension beside exact-commit credential-guard.py
// and git-shim files. Relative resolution binds reads to those staged bytes.
// Staged paths are never executable: a recognized direct Git command runs the
// read-only shim as data through a separately resolved immutable Bash. Any
// obfuscated/unqualified path that escapes text reduction cannot execute the
// staged shim (no execute bit/Landlock right) or real Git (not broker-approved).
const GIT_SHIM = fileURLToPath(new URL("./git", import.meta.url));
const GIT_READ_ONLY_VERBS = new Set([
  "status", "diff", "diff-files", "diff-index", "diff-tree", "show", "log",
  "rev-parse", "ls-files", "ls-tree", "cat-file", "merge-base", "name-rev",
  "describe", "shortlog", "blame",
]);
const GIT_BYPASS = /(?:--no-verify|core\.hookspath|GIT_(?:DIR|WORK_TREE|COMMON_DIR|OBJECT_DIRECTORY|ALTERNATE_OBJECT_DIRECTORIES|NAMESPACE|INDEX_FILE|SHALLOW_FILE|GRAFT_FILE|REPLACE_REF_BASE|CONFIG(?:_|\b)|EXEC_PATH|TEMPLATE_DIR)|--(?:git-dir|work-tree|namespace|exec-path|config|config-env)(?:=|[ \t])|(?:^|[ \t])(?:-C|-c)(?:=|[ \t]))/i;
const GIT_READ_SIDE_EFFECT = /^(?:--output(?:=.*)?|--ext-diff|--textconv|--filters|--open-files-in-pager(?:=.*)?)$/i;
const DIRECT_ABSOLUTE_GIT = /(?:^|[\s;&|('"`])\/(?:[^\s;&|()'"`]+\/)*git(?=$|[\s;&|)'"`])/i;

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
        // Any shell expansion makes the final argv unknowable to the
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

function shellQuote(word) {
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(word)) return word;
  return `'${word.replaceAll("'", `'"'"'`)}'`;
}

function gitLikeText(command) {
  return /(?:^|[\s/'"])(?:git|g[*?]t|[?*]it|g[*?]it|\[g\]it|g\[[^\]]+\]t)(?=$|[\s;&|'"])/i.test(command)
    || /\bcommand[ \t]+(?:[^ \t]+\/)?git\b/i.test(command)
    || /(?:\$\([^)]*\)|\$\{[^}]+\})it[ \t]+(?:commit|merge|cherry-pick|revert|am|rebase|pull)\b/i.test(command);
}

function parseDirectGit(command) {
  let cwd = null;
  let invocation = command;
  const pieces = command.split("&&");
  if (pieces.length === 2) {
    const left = shellWords(pieces[0]);
    if (left.unsupported || left.words.length !== 2 || left.words[0] !== "cd") return null;
    cwd = left.words[1];
    invocation = pieces[1];
  } else if (pieces.length !== 1) {
    return null;
  }
  const parsed = shellWords(invocation);
  if (parsed.unsupported) return null;
  const words = parsed.words;
  let index = 0;
  const assignments = [];
  while (index < words.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(words[index])) {
    const split = words[index].indexOf("=");
    assignments.push([words[index].slice(0, split), words[index].slice(split + 1)]);
    index += 1;
  }
  if (words[index] === "command") index += 1;
  if (words[index] !== "git") return null;
  const gitIndex = index;
  index += 1;
  const safeGlobalFlags = new Set([
    "--no-pager", "--literal-pathspecs", "--no-literal-pathspecs",
    "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs",
    "--no-optional-locks",
  ]);
  while (index < words.length && words[index].startsWith("-")) {
    if (!safeGlobalFlags.has(words[index])) {
      return { words, assignments, gitIndex, cwd, unsafeGlobal: true, verb: null };
    }
    index += 1;
  }
  return {
    words, assignments, gitIndex, cwd, unsafeGlobal: false,
    verb: index < words.length ? words[index].toLowerCase() : null,
  };
}

export function resolveGitShimPath() {
  return GIT_SHIM;
}

export function rewriteGitCommitCommand(command) {
  if (typeof command !== "string") {
    return { command, matched: false, blocked: false, reason: "" };
  }
  if (DIRECT_ABSOLUTE_GIT.test(command)) {
    return {
      command, matched: false, blocked: true,
      reason: "Direct absolute Git binaries are forbidden; all Git subprocesses must resolve through the sealed staged PATH shim.",
    };
  }
  const gitLike = gitLikeText(command);
  if (!gitLike) return { command, matched: false, blocked: false, reason: "" };
  if (GIT_BYPASS.test(command)) {
    return {
      command, matched: false, blocked: true,
      reason: "Git repository/configuration redirection and hook-bypass markers are forbidden at the model command boundary.",
    };
  }
  const parsed = parseDirectGit(command);
  if (!parsed || parsed.unsafeGlobal) {
    return {
      command, matched: false, blocked: true,
      reason: "A Git-like shell/global-option form cannot be reduced to one exact argv; the command boundary fails closed.",
    };
  }
  if (parsed.assignments.some(([name]) => name.toUpperCase().startsWith("GIT_") || name === "PATH")) {
    return {
      command, matched: false, blocked: true,
      reason: "Git/PATH prefix assignments are forbidden at the commit boundary.",
    };
  }
  if (!parsed.verb) {
    return {
      command, matched: false, blocked: true,
      reason: "A direct Git call requires an explicitly allowlisted read-only verb or git commit.",
    };
  }
  if (parsed.verb !== "commit" && !GIT_READ_ONLY_VERBS.has(parsed.verb)) {
    return {
      command, matched: false, blocked: true,
      reason: "The Git verb is not on the fail-closed read-only allowlist; only shim-mediated git commit may mutate the repository.",
    };
  }
  const verbIndex = parsed.words.findIndex(
    (word, index) => index > parsed.gitIndex && word.toLowerCase() === parsed.verb,
  );
  if (
    parsed.verb !== "commit"
    && parsed.words.slice(verbIndex + 1).some((word) => GIT_READ_SIDE_EFFECT.test(word))
  ) {
    return {
      command, matched: false, blocked: true,
      reason: "Git output/external-filter options are forbidden on the read-only boundary.",
    };
  }
  const bash = resolveTrustedBash();
  if (bash === null) {
    return {
      command, matched: false, blocked: true,
      reason: "The immutable shell interpreter for the staged Git shim is unavailable.",
    };
  }
  const assignmentText = parsed.assignments
    .map(([name, value]) => `${name}=${shellQuote(value)}`);
  const args = parsed.words.slice(parsed.gitIndex + 1).map(shellQuote);
  const rewritten = [
    ...assignmentText, shellQuote(bash), shellQuote(GIT_SHIM), ...args,
  ].join(" ");
  return {
    command: parsed.cwd === null ? rewritten : `cd ${shellQuote(parsed.cwd)} && ${rewritten}`,
    matched: true,
    blocked: false,
    reason: "",
  };
}

// ---------------------------------------------------------------------------
// Credential guardrail: fail-closed tool input classification and tool result
// redaction via the tracked sibling .factory/tools/credential-guard.py. The guard
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

// The guard interpreter is *never* an unqualified PATH-resolved ``python3``:
// every guard subprocess runs under ``resolveTrustedPython()`` — a pinned
// absolute interpreter whose immutable chain is validated (Task 11 review).
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

// ---------------------------------------------------------------------------
// Exact-commit guard binding and the pinned trusted interpreter (Task 11).
//
// The extension runs inside the model process and has **no Git access**
// (Landlock denies it), so it cannot re-derive the committed guard blob
// itself.  The trusted pre-spawn authority (the launch control plane) reads
// the exact committed ``.factory/tools/credential-guard.py`` blob through its own
// descriptor-anchored Git and forwards the SHA-256 through the sanitized
// launch environment (``PI_FACTORY_GUARD_DIGEST``).  Every production guard
// invocation hashes the *worktree* guard it is about to run and fails
// closed on a missing or mismatched digest — a swapped, tampered, or
// uncommitted guard never executes inside the model.
// ---------------------------------------------------------------------------

const GUARD_DIGEST_ENV = "PI_FACTORY_GUARD_DIGEST";
const GUARD_PYTHON_ENV = "PI_FACTORY_GUARD_PYTHON";
export { GUARD_DIGEST_ENV, GUARD_PYTHON_ENV };
const GUARD_DIGEST_RE = /^[0-9a-f]{64}$/;

// Fixed absolute trusted interpreter candidates (mirrors the control plane's
// ``gitutil._immutable_chain`` authority): an unqualified ``python3`` from a
// caller-controlled PATH could substitute a different interpreter behind the
// credential boundary, so only validated immutable absolute candidates run
// the guard.  The store scan is ``/nix/store/<32-hex>-<name>/bin/python3``
// only (deterministic lexicographic first valid candidate).
const TRUSTED_PYTHON_CANDIDATES = [
  "/usr/bin/python3",
  "/bin/python3",
  "/run/current-system/sw/bin/python3",
];
const NIX_STORE_PYTHON_GLOB = "/nix/store/*/bin/python3";
const NIX_STORE_PATH_RE = /^\/nix\/store\/[0-9a-z]{32}-[^/]+\/bin\/python3$/;
const TRUSTED_BASH_CANDIDATES = [
  "/usr/bin/bash",
  "/bin/bash",
  "/run/current-system/sw/bin/bash",
];
const NIX_STORE_BASH_RE = /^\/nix\/store\/[0-9a-z]{32}-[^/]+\/bin\/bash$/;

let _trustedPython = undefined;
let _trustedBash = undefined;

function isStickyDirectory(stat) {
  return typeof stat.isDirectory === "function" && stat.isDirectory()
    && (stat.mode & 0o1000) !== 0;
}

/** Fail-closed immutable chain: the caller (the model user, the same uid that
 * runs the extension) must not be able to replace the candidate or any
 * directory that names it. Mirrors ``gitutil._immutable_chain``: every
 * component up to the containment boundary (the store root for
 * ``/nix/store/...`` paths, the filesystem root otherwise) must be
 * non-group/other-writable and owned by a uid that differs from the caller,
 * with the single sticky-foreign-owned-directory exception (the Nix store
 * is mode 1775 owned by root). A candidate resolved through a symlink is
 * validated at its real target. */
export function immutableChainValid(path) {
  let resolved;
  try {
    resolved = realpathSync(path);
  } catch {
    return false;
  }
  const uid = currentUid();
  const boundary = resolved.startsWith("/nix/store/") ? "/nix/store" : "/";
  let current = resolved;
  for (;;) {
    let stat;
    try {
      stat = lstatSync(current);
    } catch {
      return false;
    }
    if (stat.uid === uid && !isStickyDirectory(stat)) {
      return false; // caller-owned non-sticky component is replaceable
    }
    if ((stat.mode & 0o022) !== 0 && !isStickyDirectory(stat)) {
      return false; // group/other-writable component is replaceable
    }
    if (current === boundary) break;
    const parent = dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return true;
}

function isTrustedRegularExecutable(path) {
  let stat;
  try {
    stat = lstatSync(path);
  } catch {
    return false;
  }
  return typeof stat.isFile === "function" && stat.isFile()
    && (stat.mode & 0o111) !== 0
    && stat.uid !== currentUid()
    && (stat.mode & 0o022) === 0;
}

function storePythonCandidates() {
  let entries = [];
  try {
    entries = readdirSync("/nix/store").sort();
  } catch {
    return [];
  }
  const candidates = [];
  for (const entry of entries) {
    const candidate = `/nix/store/${entry}/bin/python3`;
    if (NIX_STORE_PATH_RE.test(candidate)) candidates.push(candidate);
  }
  return candidates;
}

/** Resolve the absolute trusted interpreter the guard runs under. Fixed
 *  absolute candidates only (never ``PATH``), each validated as a regular
 *  executable with an immutable chain; when no candidate validates the
 *  resolver returns null and the guard fails closed (``guard-unavailable``).
 *  The boundary refuses to resolve as root: under uid 0 every component is
 *  caller-owned and no candidate can be proven immutable. */
export function resolveTrustedPython() {
  if (currentUid() === 0) return null;
  if (_trustedPython !== undefined) return _trustedPython;
  const pinned = process.env[GUARD_PYTHON_ENV];
  if (typeof pinned === "string" && pinned.startsWith("/")) {
    if (isTrustedRegularExecutable(pinned) && immutableChainValid(pinned)) {
      _trustedPython = realpathSync(pinned);
      return _trustedPython;
    }
    _trustedPython = null;
    return null;
  }
  let chosen = null;
  for (const candidate of TRUSTED_PYTHON_CANDIDATES) {
    if (isTrustedRegularExecutable(candidate) && immutableChainValid(candidate)) {
      chosen = candidate;
      break;
    }
  }
  if (chosen === null) {
    for (const candidate of storePythonCandidates()) {
      if (isTrustedRegularExecutable(candidate) && immutableChainValid(candidate)) {
        chosen = candidate;
        break;
      }
    }
  }
  _trustedPython = chosen;
  return chosen;
}

/** Resolve the immutable Bash that consumes the non-executable staged Git
 * shim. The shim pathname is an argument, never an execve target. */
export function resolveTrustedBash() {
  if (currentUid() === 0) return null;
  if (_trustedBash !== undefined) return _trustedBash;
  const candidates = [...TRUSTED_BASH_CANDIDATES];
  // The launch authority rebuilds PATH from immutable directories and grants
  // the exact real executable files found there. Prefer those same canonical
  // Bash candidates so Landlock and the broker bind the interpreter selected
  // here; every candidate still passes the full immutable-chain check.
  for (const directory of (process.env.PATH ?? "").split(":")) {
    if (directory.startsWith("/")) candidates.push(`${directory}/bash`);
  }
  try {
    for (const entry of readdirSync("/nix/store").sort()) {
      const candidate = `/nix/store/${entry}/bin/bash`;
      if (NIX_STORE_BASH_RE.test(candidate)) candidates.push(candidate);
    }
  } catch {
    // Fixed candidates remain; absence of a valid one fails closed below.
  }
  let chosen = null;
  for (const candidate of candidates) {
    if (isTrustedRegularExecutable(candidate) && immutableChainValid(candidate)) {
      chosen = realpathSync(candidate);
      break;
    }
  }
  _trustedBash = chosen;
  return chosen;
}

/** Verify the staged guard sibling the extension is about to run against the
 *  expected exact-commit digest the trusted pre-spawn authority forwarded
 *  through the sanitized launch environment. Missing or malformed digest,
 *  unreadable guard, and byte mismatch all fail closed. */
export function verifyGuardDigest(env, guardPath) {
  const expected =
    env && typeof env[GUARD_DIGEST_ENV] === "string" ? env[GUARD_DIGEST_ENV] : "";
  if (!GUARD_DIGEST_RE.test(expected)) {
    return { ok: false, reason: "guard-digest-missing" };
  }
  let bytes;
  try {
    bytes = readFileSync(guardPath);
  } catch {
    return { ok: false, reason: "guard-digest-unreadable" };
  }
  const actual = createHash("sha256").update(bytes).digest("hex");
  if (actual !== expected) {
    return { ok: false, reason: "guard-digest-mismatch" };
  }
  return { ok: true, reason: "" };
}

/** The production guard-binding gate: whenever the guard path about to run
 *  IS the tracked sibling (the runtime immutable resolver), the exact
 *  committed digest must be present in the environment and match the
 *  staged sibling bytes. A fixture that explicitly swapped ``guardPath`` owns its
 *  own authority and bypasses the digest requirement. */
export function guardBindingStatus(env, guardPath) {
  if (guardPath !== resolveGuardPath()) {
    return { ok: true, reason: "" };
  }
  return verifyGuardDigest(env, guardPath);
}

/** Resolve the immutable tracked credential guard executable. */
export function resolveGuardPath() {
  return fileURLToPath(new URL("./credential-guard.py", import.meta.url));
}

function currentUid() {
  return typeof process.getuid === "function" ? process.getuid() : -1;
}

// Pi2 starts with an anonymous auth descriptor. The exact-commit extension
// captures that credential into extension-owned memory and detaches the private
// auth file before every tool. Tool subprocesses do not inherit the descriptor,
// /proc is denied, and the allowlisted in-process tools have no numeric-
// descriptor API. After each tool result is fully redacted, the extension
// recreates the private mode-0600 file for Pi's next authenticated model turn;
// the next tool call synchronously captures any legitimate OAuth rotation and
// detaches it again before dispatch.
const TOOL_FD_ENV = "PI_FACTORY_TOOL_FD";
const TOOL_FD_DEV_ENV = "PI_FACTORY_TOOL_FD_DEV";
const TOOL_FD_INO_ENV = "PI_FACTORY_TOOL_FD_INO";
const TOOL_FD_LIMIT_ENV = "PI_FACTORY_TOOL_FD_LIMIT";
const TOOL_FILE_ENV = "PI_FACTORY_TOOL_FILE";
const TOOL_FILE_DEV_ENV = "PI_FACTORY_TOOL_FILE_DEV";
const TOOL_FILE_INO_ENV = "PI_FACTORY_TOOL_FILE_INO";
const DECIMAL_IDENTITY_RE = /^(?:0|[1-9][0-9]{0,30})$/;

/** Credential bytes and exact file identity held only by this extension. */
let toolCredentialState = null;

function validCredentialIdentity(identity, expectedDev = null, expectedIno = null) {
  return identity.isFile() && !identity.isSymbolicLink()
    && (expectedDev === null || identity.dev === expectedDev)
    && (expectedIno === null || identity.ino === expectedIno)
    && identity.nlink === 1n && identity.uid === BigInt(currentUid())
    && (identity.mode & 0o77n) === 0n
    && identity.size > 0n && identity.size <= 1_048_576n;
}

function validCredentialFile(path, expectedDev, expectedIno) {
  return validCredentialIdentity(
    lstatSync(path, { bigint: true }), expectedDev, expectedIno,
  );
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map(
      (key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`,
    ).join(",")}}`;
  }
  return JSON.stringify(value);
}

/** Validate a provider-produced OAuth state transition without exposing any
 * credential value. Account and all unrelated provider records are immutable.
 * A refresh-token rotation is accepted only together with a new access token
 * and a strictly later finite expiry; an unchanged refresh remains valid. */
function validCredentialTransition(previousBytes, candidateBytes) {
  let previous;
  let candidate;
  try {
    previous = JSON.parse(previousBytes.toString("utf8"));
    candidate = JSON.parse(candidateBytes.toString("utf8"));
  } catch {
    return false;
  }
  if (!previous || typeof previous !== "object" || Array.isArray(previous)
      || !candidate || typeof candidate !== "object" || Array.isArray(candidate)
      || canonicalJson(Object.keys(previous).sort())
        !== canonicalJson(Object.keys(candidate).sort())) return false;
  const oldAuth = previous["openai-codex"];
  const newAuth = candidate["openai-codex"];
  if (!oldAuth || typeof oldAuth !== "object" || Array.isArray(oldAuth)
      || !newAuth || typeof newAuth !== "object" || Array.isArray(newAuth)
      || oldAuth.type !== "oauth" || newAuth.type !== "oauth"
      || typeof oldAuth.accountId !== "string" || oldAuth.accountId.length === 0
      || newAuth.accountId !== oldAuth.accountId
      || typeof newAuth.access !== "string" || newAuth.access.length < 16
      || newAuth.access.length > 16_384
      || typeof newAuth.refresh !== "string" || newAuth.refresh.length < 16
      || newAuth.refresh.length > 16_384
      || typeof newAuth.expires !== "number" || !Number.isFinite(newAuth.expires)
      || newAuth.expires <= Date.now() + 300_000) return false;
  for (const key of Object.keys(previous)) {
    if (key !== "openai-codex"
        && canonicalJson(previous[key]) !== canonicalJson(candidate[key])) return false;
  }
  if (newAuth.refresh !== oldAuth.refresh
      && (newAuth.access === oldAuth.access
          || typeof oldAuth.expires !== "number"
          || !Number.isFinite(oldAuth.expires)
          || newAuth.expires <= oldAuth.expires)) return false;
  return true;
}

/** Open, identity-check, read, and unlink one attached auth file synchronously.
 * A provider may atomically replace the restored inode while refreshing OAuth;
 * the replacement is rebound only after the transition validator accepts it.
 * The single-link check rejects every hardlink alias. */
function captureAttachedCredential(saved) {
  let descriptor = -1;
  try {
    const named = lstatSync(saved.filePath, { bigint: true });
    if (!validCredentialIdentity(named)) {
      return { ok: false, reason: "tool-file-binding-mismatch" };
    }
    descriptor = openSync(
      saved.filePath,
      fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW | fsConstants.O_CLOEXEC,
    );
    const opened = fstatSync(descriptor, { bigint: true });
    if (!validCredentialIdentity(opened)
        || opened.dev !== named.dev || opened.ino !== named.ino) {
      return { ok: false, reason: "tool-file-binding-mismatch" };
    }
    const refreshed = readFileSync(descriptor);
    if (!validCredentialTransition(saved.bytes, refreshed)) {
      return { ok: false, reason: "tool-credential-transition-invalid" };
    }
    const beforeUnlink = lstatSync(saved.filePath, { bigint: true });
    if (beforeUnlink.dev !== opened.dev || beforeUnlink.ino !== opened.ino
        || !validCredentialIdentity(beforeUnlink)) {
      return { ok: false, reason: "tool-file-binding-mismatch" };
    }
    unlinkSync(saved.filePath);
    saved.bytes.fill(0);
    saved.bytes = refreshed;
    saved.expectedFileDev = opened.dev;
    saved.expectedFileIno = opened.ino;
    saved.status = "detached";
    return { ok: true, reason: "detached" };
  } catch {
    return { ok: false, reason: "tool-credential-removal-failed" };
  } finally {
    if (descriptor >= 0) {
      try { closeSync(descriptor); } catch { /* fail-closed result already chosen */ }
    }
  }
}

function removeSafeEmptyCredentialPlaceholder(path) {
  let descriptor = -1;
  try {
    const named = lstatSync(path, { bigint: true });
    if (!validCredentialIdentity(named)) return false;
    descriptor = openSync(
      path, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW | fsConstants.O_CLOEXEC,
    );
    const opened = fstatSync(descriptor, { bigint: true });
    if (!validCredentialIdentity(opened)
        || opened.dev !== named.dev || opened.ino !== named.ino) return false;
    const document = JSON.parse(readFileSync(descriptor, "utf8"));
    if (!document || typeof document !== "object" || Array.isArray(document)
        || Object.keys(document).length !== 0) return false;
    const beforeUnlink = lstatSync(path, { bigint: true });
    if (!validCredentialIdentity(beforeUnlink)
        || beforeUnlink.dev !== opened.dev || beforeUnlink.ino !== opened.ino) return false;
    unlinkSync(path);
    return true;
  } catch {
    return false;
  } finally {
    if (descriptor >= 0) {
      try { closeSync(descriptor); } catch { /* caller rechecks before dispatch */ }
    }
  }
}

/** Detach auth.json and close every inherited descriptor before a tool runs. */
export function closeToolCredentialBoundary(env = process.env) {
  if (toolCredentialState?.status === "detached") {
    // This synchronous check is mandatory on every tool_call. Pi may
    // legitimately recreate auth.json while preparing an authenticated turn;
    // in that case captureAttachedCredential performs the complete no-follow,
    // single-link, owner/mode, account/provider, OAuth-transition, and
    // pre-unlink identity checks before detaching the refreshed file again.
    // Any hardlink, symlink, malformed/substituted credential, or other inode
    // fails closed before the tool-specific implementation can run.
    try {
      lstatSync(toolCredentialState.filePath, { bigint: true });
    } catch (error) {
      if (error?.code === "ENOENT") {
        return { ok: true, reason: "already-detached" };
      }
      return { ok: false, reason: "tool-file-absence-unverifiable" };
    }
    // Pi's credential store may publish an exact empty-object placeholder
    // after observing the deliberately detached path. It carries no secret;
    // remove only a no-follow, single-link, private, identity-stable `{}`.
    if (removeSafeEmptyCredentialPlaceholder(toolCredentialState.filePath)) {
      return { ok: true, reason: "empty-placeholder-detached" };
    }
    return captureAttachedCredential(toolCredentialState);
  }
  if (toolCredentialState?.status === "attached") {
    return captureAttachedCredential(toolCredentialState);
  }

  const numericValues = [
    env?.[TOOL_FD_ENV], env?.[TOOL_FD_DEV_ENV], env?.[TOOL_FD_INO_ENV],
    env?.[TOOL_FD_LIMIT_ENV], env?.[TOOL_FILE_DEV_ENV], env?.[TOOL_FILE_INO_ENV],
  ];
  const filePath = env?.[TOOL_FILE_ENV];
  if (numericValues.every((value) => value === undefined) && filePath === undefined) {
    return { ok: true, reason: "not-provisioned" };
  }
  if (numericValues.some(
    (value) => typeof value !== "string" || !DECIMAL_IDENTITY_RE.test(value)
  ) || typeof filePath !== "string") {
    return { ok: false, reason: "tool-credential-binding-malformed" };
  }
  let fd;
  let expectedDev;
  let expectedIno;
  let fdLimit;
  let expectedFileDev;
  let expectedFileIno;
  try {
    fd = Number(numericValues[0]);
    expectedDev = BigInt(numericValues[1]);
    expectedIno = BigInt(numericValues[2]);
    fdLimit = Number(numericValues[3]);
    expectedFileDev = BigInt(numericValues[4]);
    expectedFileIno = BigInt(numericValues[5]);
  } catch {
    return { ok: false, reason: "tool-credential-binding-malformed" };
  }
  const agentDir = env?.PI_CODING_AGENT_DIR;
  const expectedFilePath = typeof agentDir === "string" ? join(agentDir, "auth.json") : "";
  if (!Number.isSafeInteger(fd) || fd < 3
      || !Number.isSafeInteger(fdLimit) || fdLimit < 64 || fdLimit > 65_536
      || fd >= fdLimit || filePath !== expectedFilePath) {
    return { ok: false, reason: "tool-credential-binding-malformed" };
  }
  let fileBytes;
  try {
    if (!validCredentialFile(filePath, expectedFileDev, expectedFileIno)) {
      return { ok: false, reason: "tool-file-binding-mismatch" };
    }
    fileBytes = readFileSync(filePath);
  } catch {
    return { ok: false, reason: "tool-file-binding-mismatch" };
  }
  const aliases = [];
  let originalMatched = false;
  for (let candidate = 3; candidate < fdLimit; candidate += 1) {
    try {
      const identity = fstatSync(candidate, { bigint: true });
      if (identity.dev === expectedDev && identity.ino === expectedIno) {
        aliases.push(candidate);
        if (candidate === fd) originalMatched = true;
      }
    } catch { /* EBADF is ordinary; the original check fails closed. */ }
  }
  if (!originalMatched || aliases.length === 0) {
    return { ok: false, reason: "tool-fd-binding-mismatch" };
  }
  try {
    for (const alias of aliases) closeSync(alias);
    unlinkSync(filePath);
  } catch {
    return { ok: false, reason: "tool-credential-removal-failed" };
  }
  toolCredentialState = {
    status: "detached", filePath, bytes: fileBytes,
    expectedFileDev, expectedFileIno,
  };
  for (const name of [
    TOOL_FD_ENV, TOOL_FD_DEV_ENV, TOOL_FD_INO_ENV, TOOL_FD_LIMIT_ENV,
    TOOL_FILE_ENV, TOOL_FILE_DEV_ENV, TOOL_FILE_INO_ENV,
  ]) delete env[name];
  return { ok: true, reason: "detached" };
}

/** Restore auth.json after the redacted tool result and before Pi's next
 * authenticated model turn. An already-attached file is first captured and
 * transition-validated so shutdown also preserves a legitimate late refresh. */
export function restoreToolCredentialBoundary() {
  const saved = toolCredentialState;
  if (saved === null) return { ok: true, reason: "not-detached" };
  if (saved.status === "attached") {
    const captured = captureAttachedCredential(saved);
    if (!captured.ok) return captured;
  }
  if (saved.status !== "detached") return { ok: false, reason: "tool-file-still-attached" };
  let targetFd = -1;
  try {
    // Pi's credential store may publish an empty-object placeholder before
    // extension shutdown after observing the deliberately detached path.
    // Replace only that exact safe placeholder; any other pre-existing inode
    // is a fail-closed collision.
    try {
      const placeholder = lstatSync(saved.filePath, { bigint: true });
      const document = JSON.parse(readFileSync(saved.filePath, "utf8"));
      if (!placeholder.isFile() || placeholder.isSymbolicLink()
          || placeholder.nlink !== 1n || placeholder.uid !== BigInt(currentUid())
          || (placeholder.mode & 0o77n) !== 0n
          || !document || typeof document !== "object" || Array.isArray(document)
          || Object.keys(document).length !== 0) {
        return { ok: false, reason: "tool-credential-restore-collision" };
      }
      unlinkSync(saved.filePath);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    targetFd = openSync(
      saved.filePath,
      fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL
        | fsConstants.O_NOFOLLOW | fsConstants.O_CLOEXEC,
      0o600,
    );
    let offset = 0;
    while (offset < saved.bytes.length) {
      offset += writeSync(
        targetFd, saved.bytes, offset, saved.bytes.length - offset, offset
      );
    }
    fsyncSync(targetFd);
    const fileIdentity = fstatSync(targetFd, { bigint: true });
    if (!fileIdentity.isFile() || fileIdentity.nlink !== 1n
        || fileIdentity.uid !== BigInt(currentUid())
        || (fileIdentity.mode & 0o77n) !== 0n
        || fileIdentity.size !== BigInt(saved.bytes.length)) {
      throw new Error("restored credential identity mismatch");
    }
    closeSync(targetFd);
    targetFd = -1;
    saved.expectedFileDev = fileIdentity.dev;
    saved.expectedFileIno = fileIdentity.ino;
    saved.status = "attached";
    return { ok: true, reason: "restored" };
  } catch {
    if (targetFd >= 0) {
      try { closeSync(targetFd); } catch { /* fail closed below */ }
    }
    try { unlinkSync(saved.filePath); } catch { /* absent is safe */ }
    return { ok: false, reason: "tool-credential-restore-failed" };
  }
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

/** One guard invocation's authority: the pinned trusted interpreter plus
 * (for the tracked guard) the exact-commit digest binding.  Any failure
 * carries ``ok:false`` so the caller fails closed before a byte reaches the
 * guard.  A fixture that explicitly swapped ``guardPath`` owns its own
 * authority and bypasses the digest requirement. */
function guardAuthority(options) {
  const env = options.env ?? process.env;
  const guardPath = options.guardPath ?? resolveGuardPath();
  const python = resolveTrustedPython();
  if (python === null) {
    return { ok: false, reason: "guard-unavailable" };
  }
  const binding = guardBindingStatus(env, guardPath);
  if (!binding.ok) {
    return { ok: false, reason: binding.reason };
  }
  return { ok: true, reason: "", python, env, guardPath };
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
  const authority = guardAuthority(options);
  if (!authority.ok) {
    return { allowed: false, verdict: "block", reason: authority.reason };
  }
  const result = spawnSync(authority.python, [authority.guardPath, mode], {
    input,
    encoding: "utf8",
    timeout: options.timeout ?? GUARD_CHECK_TIMEOUT_MS,
    maxBuffer: options.maxBuffer ?? GUARD_CHECK_MAX_BUFFER,
    killSignal: "SIGKILL",
    env: authority.env,
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
  const authority = guardAuthority(options);
  if (!authority.ok) {
    return { ok: false, text: null };
  }
  const result = spawnSync(authority.python, [authority.guardPath, "redact"], {
    input: text,
    encoding: "utf8",
    timeout: options.timeout ?? REDACT_TIMEOUT_MS,
    maxBuffer: options.maxBuffer ?? REDACT_MAX_BUFFER,
    killSignal: "SIGKILL",
    env: authority.env,
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
  const authority = guardAuthority(options);
  if (!authority.ok) {
    return { ok: false, value: null };
  }
  const result = spawnSync(authority.python, [authority.guardPath, "redact-json-stdin"], {
    input,
    encoding: "utf8",
    timeout: options.timeout ?? REDACT_TIMEOUT_MS,
    maxBuffer: options.maxBuffer ?? REDACT_MAX_BUFFER,
    killSignal: "SIGKILL",
    env: authority.env,
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
 * promise settles only after the child exits AND the temp bytes have flushed
 * (or any error/timeout fires), so descriptor publication can proceed. ``authority``
 * is the pinned-interpreter + exact-commit binding gate of
 * :func:`guardAuthority`; the guard runs only under that trusted interpreter. */
async function redactFileViaGuard(originalHandle, tempHandle, authority) {
  const child = spawn(authority.python, [authority.guardPath, "redact"], {
    stdio: ["pipe", "pipe", "pipe"],
    env: authority.env,
    windowsHide: true,
  });
  child.stderr.resume(); // bounded lifetime; never let an error pipe deadlock
  const inputPump = (async () => {
    const buffer = Buffer.allocUnsafe(65536);
    let position = 0;
    for (;;) {
      const { bytesRead } = await originalHandle.read(
        buffer, 0, buffer.length, position,
      );
      if (bytesRead === 0) break;
      position += bytesRead;
      if (!child.stdin.write(buffer.subarray(0, bytesRead))) {
        await once(child.stdin, "drain");
      }
    }
    child.stdin.end();
  })();
  const outputPump = (async () => {
    let position = 0;
    for await (const chunk of child.stdout) {
      let offset = 0;
      while (offset < chunk.length) {
        const { bytesWritten } = await tempHandle.write(
          chunk, offset, chunk.length - offset, position,
        );
        if (bytesWritten <= 0) throw new Error("short overflow temp write");
        offset += bytesWritten;
        position += bytesWritten;
      }
    }
  })();
  const exit = new Promise((resolvePromise, rejectPromise) => {
    child.once("error", rejectPromise);
    child.once("exit", (code, signal) => {
      if (code === 0 && signal === null) resolvePromise();
      else rejectPromise(new Error(`credential guard redact exited ${code ?? signal}`));
    });
  });
  let timer;
  const timeout = new Promise((_resolvePromise, rejectPromise) => {
    timer = setTimeout(() => {
      child.kill("SIGKILL");
      rejectPromise(new Error("credential guard redact timed out"));
    }, OVERFLOW_REDACT_TIMEOUT_MS);
  });
  try {
    await Promise.race([Promise.all([inputPump, outputPump, exit]), timeout]);
  } finally {
    clearTimeout(timer);
    child.kill("SIGKILL");
  }
}

/** Open one recognized overflow inode once, with no symlink following. */
async function openOwnedOverflow(canonical) {
  const flags = fsConstants.O_RDWR
    | (fsConstants.O_CLOEXEC ?? 0)
    | (fsConstants.O_NOFOLLOW ?? 0)
    | (fsConstants.O_NONBLOCK ?? 0);
  const handle = await open(canonical, flags);
  try {
    const stats = await handle.stat();
    if (!isOwnedRegularFile(stats)) throw new Error("unsafe overflow inode");
    return { handle, stats };
  } catch (error) {
    await handle.close();
    throw error;
  }
}

/** Truncate only the exact O_NOFOLLOW-opened owned inode. */
export async function truncateOwnedOverflowFile(fullOutputPath) {
  const parsed = parseBashOverflowPath(fullOutputPath);
  if (!parsed) return false;
  let opened;
  try {
    opened = await openOwnedOverflow(parsed.canonical);
    await opened.handle.truncate(0);
    return true;
  } catch {
    return false;
  } finally {
    try { await opened?.handle.close(); } catch { /* best effort */ }
  }
}

async function unlinkQuiet(pathToUnlink) {
  try {
    await unlink(pathToUnlink);
  } catch {
    /* best-effort temp cleanup */
  }
}

/** Sanitize a recognized bash overflow log: one O_NOFOLLOW open, descriptor
 * chmod/read/fstat, redact into an owner-only temp, then descriptor-truncate
 * and descriptor-write the redacted bytes back to that same inode. On any
 * failure the opened owned inode is truncated and the result fails redacted;
 * no later pathname operation can be redirected onto a replacement target. */
export async function sanitizeBashOverflowPath(fullOutputPath, options = {}) {
  const parsed = parseBashOverflowPath(fullOutputPath);
  if (!parsed) return { ok: false, reason: "non-canonical-overflow-path" };
  const original = parsed.canonical;
  let source;
  let tempHandle;
  const tempPath = guardTempPath(parsed.id);
  const truncateSource = async () => {
    try { await source?.handle.truncate(0); } catch { /* fail-redacted result */ }
  };
  try {
    try {
      source = await openOwnedOverflow(original);
    } catch (error) {
      return {
        ok: false,
        reason: error?.code === "ENOENT"
          ? "overflow-file-missing" : "overflow-file-identity",
      };
    }
    const authority = guardAuthority(options);
    if (!authority.ok) {
      await truncateSource();
      return { ok: false, reason: authority.reason };
    }
    try {
      await source.handle.chmod(0o600);
    } catch {
      await truncateSource();
      return { ok: false, reason: "overflow-chmod-failed" };
    }
    try {
      const tempFlags = fsConstants.O_RDWR | fsConstants.O_CREAT | fsConstants.O_EXCL
        | (fsConstants.O_CLOEXEC ?? 0) | (fsConstants.O_NOFOLLOW ?? 0);
      tempHandle = await open(tempPath, tempFlags, 0o600);
      await redactFileViaGuard(source.handle, tempHandle, authority);
      await tempHandle.sync();
    } catch {
      await truncateSource();
      return { ok: false, reason: "overflow-redact-failed" };
    }
    const after = await source.handle.stat();
    const named = await lstat(original);
    const tempStats = await tempHandle.stat();
    if (
      !isOwnedRegularFile(after)
      || after.dev !== source.stats.dev || after.ino !== source.stats.ino
      || after.size !== source.stats.size || after.mtimeMs !== source.stats.mtimeMs
      || !isOwnedRegularFile(named)
      || named.dev !== source.stats.dev || named.ino !== source.stats.ino
    ) {
      await truncateSource();
      return { ok: false, reason: "overflow-identity-recheck-failed" };
    }
    if (!isOwnedRegularFile(tempStats) || (tempStats.mode & 0o077) !== 0) {
      await truncateSource();
      return { ok: false, reason: "overflow-temp-identity-failed" };
    }
    // Publish back through the *same verified original descriptor*. This
    // removes the final lstat->pathname-rename race entirely: a concurrent
    // rename/symlink swap can change only the name, never the inode receiving
    // truncate/write. The final named-identity check decides success.
    await source.handle.truncate(0);
    const publishBuffer = Buffer.allocUnsafe(65536);
    let readPosition = 0;
    let writePosition = 0;
    for (;;) {
      const { bytesRead } = await tempHandle.read(
        publishBuffer, 0, publishBuffer.length, readPosition,
      );
      if (bytesRead === 0) break;
      readPosition += bytesRead;
      let offset = 0;
      while (offset < bytesRead) {
        const { bytesWritten } = await source.handle.write(
          publishBuffer, offset, bytesRead - offset, writePosition,
        );
        if (bytesWritten <= 0) throw new Error("short overflow publication write");
        offset += bytesWritten;
        writePosition += bytesWritten;
      }
    }
    await source.handle.sync();
    const published = await source.handle.stat();
    const publishedName = await lstat(original);
    if (
      !isOwnedRegularFile(published)
      || published.dev !== source.stats.dev || published.ino !== source.stats.ino
      || !isOwnedRegularFile(publishedName)
      || publishedName.dev !== source.stats.dev
      || publishedName.ino !== source.stats.ino
    ) {
      return { ok: false, reason: "overflow-publication-identity-failed" };
    }
    return { ok: true, reason: "" };
  } catch {
    await truncateSource();
    return { ok: false, reason: "overflow-sanitize-failed" };
  } finally {
    try { await tempHandle?.close(); } catch { /* best effort */ }
    await unlinkQuiet(tempPath);
    try { await source?.handle.close(); } catch { /* best effort */ }
  }
}

const TOOL_CALL_GUARDED_INPUTS = {
  bash: (input) => (input && typeof input === "object" ? input.command : undefined),
  read: (input) => (input && typeof input === "object" ? input.path : undefined),
  write: (input) => (input && typeof input === "object" ? input.path : undefined),
  edit: (input) => (input && typeof input === "object" ? input.path : undefined),
  // B1 security review: grep reads file contents in-process, so its path is
  // guarded exactly like read/write/edit (a /proc/.../fd reference could
  // otherwise dereference the inherited credential descriptor).
  grep: (input) => (input && typeof input === "object" ? input.path : undefined),
};

/** Guard one tool call before any rewrite: bash commands through
 * check-command-stdin, read/write/edit paths through check-path-stdin. A
 * block returns the fail-closed pi block payload with a stable reason that
 * never echoes the input; allowed calls return null so the git rewrite
 * still runs. */
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

export default function registerFactoryGuard(pi) {
  let providerAuthReady = false;

  pi.on("session_start", async () => {
    if (process.env[TOOL_FD_ENV] === undefined) return;
    const filePath = process.env[TOOL_FILE_ENV];
    const expectedDev = process.env[TOOL_FILE_DEV_ENV];
    const expectedIno = process.env[TOOL_FILE_INO_ENV];
    if (!filePath || !DECIMAL_IDENTITY_RE.test(expectedDev ?? "")
        || !DECIMAL_IDENTITY_RE.test(expectedIno ?? "")
        || !validCredentialFile(filePath, BigInt(expectedDev), BigInt(expectedIno))) {
      throw new Error("factory guard could not validate openai-codex runtime auth");
    }
    const document = JSON.parse(readFileSync(filePath, "utf8"));
    const credential = document?.["openai-codex"];
    if (credential?.type !== "oauth" || typeof credential.access !== "string"
        || credential.access.length < 16 || credential.access.length > 16_384
        || typeof credential.refresh !== "string" || credential.refresh.length < 16
        || credential.refresh.length > 16_384
        || typeof credential.accountId !== "string" || credential.accountId.length === 0
        || credential.accountId.length > 1024
        || typeof credential.expires !== "number" || !Number.isFinite(credential.expires)
        || credential.expires <= Date.now() + 300_000) {
      throw new Error("factory guard could not pin openai-codex runtime auth");
    }
    // Override only request authentication; Pi keeps the exact built-in
    // provider, model catalogue, API implementation, and base URL.
    pi.registerProvider("openai-codex", { apiKey: credential.access });
    providerAuthReady = true;
  });

  pi.on("before_agent_start", () => {
    if (process.env[TOOL_FD_ENV] === undefined) return;
    if (!providerAuthReady) {
      throw new Error("factory guard refuses to detach unpinned provider auth");
    }
    const detached = closeToolCredentialBoundary();
    if (!detached.ok) {
      throw new Error(`${TOOLCALL_BLOCK_PREFIX}${detached.reason}`);
    }
  });

  pi.on("tool_call", (event) => {
    // This is deliberately first and applies to every tool name. The private
    // credential pathname is detached before any in-process/tool subprocess
    // implementation and restored only from extension-owned bytes after the
    // corresponding tool result has been redacted.
    const detached = closeToolCredentialBoundary();
    if (!detached.ok) {
      return {
        block: true,
        reason: `${TOOLCALL_BLOCK_PREFIX}${detached.reason}`,
      };
    }
    const guarded = guardToolCallInput(event);
    if (guarded) {
      return guarded;
    }
    if (event.toolName !== "bash") return;
    const git = rewriteGitCommitCommand(event.input?.command);
    if (git.blocked) {
      return { block: true, reason: git.reason };
    }
    if (git.matched) {
      event.input.command = git.command;
    }
  });

  pi.on("tool_result", async (event) => {
    let result;
    if (
      event.toolName === "bash"
      && event.details
      && typeof event.details === "object"
      && typeof event.details.fullOutputPath === "string"
    ) {
      const sanitized = await sanitizeBashOverflowPath(event.details.fullOutputPath);
      if (!sanitized.ok) {
        result = failRedactedResult(event);
      }
    }
    if (result === undefined) {
      const redacted = redactToolResultPatch(event);
      result = redacted.failed ? failRedactedResult(event) : redacted.patch;
    }
    // Restoration happens only after every raw result/overflow byte has been
    // sanitized. The private file is then available to Pi's provider code for
    // the next model turn, never to the just-completed tool implementation.
    const restored = restoreToolCredentialBoundary();
    if (!restored.ok) {
      throw new Error(`${TOOLCALL_BLOCK_PREFIX}${restored.reason}`);
    }
    return result;
  });

  pi.on("session_shutdown", () => {
    const restored = restoreToolCredentialBoundary();
    if (!restored.ok) {
      throw new Error(`${TOOLCALL_BLOCK_PREFIX}${restored.reason}`);
    }
  });
}
