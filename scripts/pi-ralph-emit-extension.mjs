const SHIM = "./scripts/pi-cli-shims/ralph";
const DIRECT_PREFIX = /^[ \t]*ralph(?=[ \t]+emit(?:[ \t]|$))/;

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
        if (char === "`" || (char === "$" && command[index + 1] === "(")) unsupported = true;
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
      if (char === "$" && command[index + 1] === "(") unsupported = true;
      word += char;
      active = true;
    }
  }
  finish();
  if (quote !== null || escaped) unsupported = true;
  return { words, unsupported };
}

export function rewriteRalphEmitCommand(command) {
  if (typeof command !== "string") return { command, matched: false, unsafe: false };
  const parsed = shellWords(command);
  const adjacentEmit = parsed.words.some(
    (word, index) => word === "ralph" && parsed.words[index + 1] === "emit",
  );
  const containsEmit = adjacentEmit || (
    parsed.unsupported && /\bralph[ \t]+emit(?:[ \t]|$)/.test(command)
  );
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
    };
  }
  return { command, matched: false, unsafe: containsEmit };
}

export default function registerRalphEmitShim(pi) {
  pi.on("tool_call", (event) => {
    if (event.toolName !== "bash") return;
    const result = rewriteRalphEmitCommand(event.input?.command);
    if (result.matched) {
      event.input.command = result.command;
      return;
    }
    if (result.unsafe) {
      return {
        block: true,
        reason: "Run `ralph emit` as the direct final command so the factory can publish it safely.",
      };
    }
  });
}
