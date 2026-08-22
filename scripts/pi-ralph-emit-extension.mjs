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
const GIT_C_PREFIX = new RegExp("^[ \t]*git(?=[ \t]+-C[ \t]+[^;&|<>(){}`$#\\n\\r \t]+[ \t]+" + GIT_COMMIT_VERBS + "(?:[ \t]|$))");
const GIT_CD_PREFIX = new RegExp("^[ \t]*cd[ \t]+[^;&|<>(){}`$#\\n\\r \t]+[ \t]*&&[ \t]+git(?=[ \t]+" + GIT_COMMIT_VERBS + "(?:[ \t]|$))");
// Fail-closed markers that disable or redirect the commit boundary. None of
// these have any legitimate use in the Ralph harness, so any git-invoking
// command that carries one is blocked before execution.
const GIT_BYPASS = /(?:--no-verify|core\.hooksPath|GIT_CONFIG_(?:COUNT|PARAMETERS|KEY_|VALUE_))/;
// Commit-creation verbs with no Git-hook coverage, or whose patch/replay
// semantics this boundary does not own, are blocked wherever they appear as
// the git verb (including chained command positions the rewrite regexes
// cannot reach). Only `git commit` may create commits from the model's
// command boundary. The verb must directly follow git, so words in commit
// messages or arguments never trigger this.
const GIT_UNGUARDED = /(?:^|(?:&&|[;&|])[ \t]*)[ \t]*git[ \t]+(?:cherry-pick|revert|rebase|pull|am)(?:[ \t]|$)/;

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
      reason: "Git hook-bypass markers (--no-verify, core.hooksPath, GIT_CONFIG_*) are forbidden: they would bypass the fail-closed commit boundary.",
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

export default function registerRalphEmitShim(pi) {
  pi.on("tool_call", (event) => {
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
}
