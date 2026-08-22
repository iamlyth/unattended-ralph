// Sealed image-byte delivery driver for read-only visual-audit review.
//
// The auditor-mandated strategy: never pass an image by CLI @path (TOCTOU).
// This driver reads the exact image bytes from the provenance-bound capture
// directory, computes their SHA-256, verifies it matches the expected hash
// from the provenance manifest, base64-encodes the SAME bytes, and delivers
// them in-memory to the configured vision model through the pi SDK. The
// response is parsed as a strict structured finding and written alongside an
// invocation receipt.
//
// Request/response sealing (per-task, orchestrator-driven):
//   - The orchestrator generates a cryptographically random per-task request
//     nonce (>= 128 bits, 32 lowercase hex chars) and passes it via
//     --request-nonce. The frozen prompt demands the exact echo.
//   - The model-returned sealed fields (schema, state_id, image_sha256, role,
//     model, prompt_sha256, schema_sha256, request_nonce) are NEVER repaired
//     or overwritten locally: any drift from the sealed task values is fatal.
//   - A strict invocation receipt records schema, nonce, task bindings, the
//     input image hash, the raw response SHA-256, the normalized finding
//     SHA-256, timestamps/duration, and the model identity. Both files are
//     written as regular 0600 files.
//
// A receipt proves only that an invocation happened with those exact bytes
// under that fresh nonce; it never certifies visual truth and never elevates
// an evidence tier. The nonce proves invocation freshness only; supplemental
// authority (prompt/schema/calibration/gate policy) stays in the docs.
//
// The vision model is never a writer/Ralph model. Model selection is
// configurable and credential-free. Production requires PI_CODING_AGENT_DIR
// to name the same trusted Pi authority used by pi2; auth.json and models.json
// are passed explicitly to ModelRuntime so SDK defaults cannot silently fall
// back to a different agent directory. No visual-audit-specific credential or
// config path override exists.
//
// Usage:
//   node visual-audit-review-sdk.mjs \
//     --image PATH --expected-sha256 HEX --state-id ID --role ROLE \
//     --prompt-file PATH --expected-description-base64 BASE64 \
//     --calibration-expectation none|pass|finding \
//     --prompt-sha256 HEX --schema-sha256 HEX \
//     --request-nonce NONCE --model <consumer-configured-vision-model> \
//     --out-dir DIR
//
// The vision model is consumer-configured (`.factory/visual-audit.toml`
// `vision_model` / `VISUAL_AUDIT_VISION_MODEL`); the generic scaffold
// provides no default model and enables nothing until the consumer sets one.

import { createHash } from "node:crypto";
import {
  readFileSync, writeFileSync, chmodSync, mkdirSync, lstatSync, realpathSync,
} from "node:fs";
import { resolve, dirname, join, relative, isAbsolute, sep } from "node:path";
import { createRequire } from "node:module";
import { userInfo } from "node:os";

const FINDING_SCHEMA = "ralph-visual-audit-review/v1";
const RECEIPT_SCHEMA = "ralph-visual-audit-invocation/v1";
const NONCE_RE = /^[0-9a-f]{32}$/;
const SHA256_RE = /^[0-9a-f]{64}$/;
const BASE64_RE = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const TASK_PROMPT_BINDING_SCHEMA = "ralph-visual-audit-task-prompt/v1";
const MAX_EXPECTED_DESCRIPTION_BYTES = 16384;

// Resolve the pi-coding-agent package without hardcoding a store path. The
// pi2 wrapper exports PI_PACKAGE_DIR; failing that, resolve relative to the
// pi binary on PATH (its package is a sibling), failing that, via require
// resolution from this script.
async function resolvePackage() {
  const fromEnv = process.env.PI_PACKAGE_DIR;
  if (fromEnv) return fromEnv;
  const require = createRequire(import.meta.url);
  try {
    return dirname(require.resolve("@earendil-works/pi-coding-agent/package.json"));
  } catch {
    /* fall through */
  }
  const pi = process.env.PI_BIN || "pi";
  const { execFileSync } = await import("node:child_process");
  try {
    const bin = execFileSync("readlink", ["-f", pi], { encoding: "utf8" }).trim();
    if (bin) {
      const dir = dirname(bin);
      const candidate = resolve(dir, "../lib/node_modules/@earendil-works/pi-coding-agent");
      const { existsSync } = await import("node:fs");
      if (existsSync(resolve(candidate, "dist/index.js"))) return candidate;
    }
  } catch {
    /* fall through */
  }
  throw new Error("cannot resolve @earendil-works/pi-coding-agent; set PI_PACKAGE_DIR");
}

const sdk = await import(resolve(await resolvePackage(), "dist/index.js"));
const {
  createAgentSession, SessionManager, ModelRuntime, getAgentDir,
  DefaultResourceLoader, SettingsManager,
} = sdk;

// Bind the SDK to Pi's standard, operator-provisioned authority. Requiring the
// standard Pi variable is intentional: a silent ~/.pi/agent fallback can use a
// different credential/model universe than pi2. The path is constrained to
// the invoking user's real home/.pi tree and every consumed object is checked
// without opening, parsing, or printing credential bytes.
function authorityError(message) {
  throw new Error(`unsafe Pi credential authority: ${message}`);
}

function requireSafeNode(path, kind, { exactOwner = false } = {}) {
  let st;
  try {
    st = lstatSync(path);
  } catch {
    authorityError(`${kind} is missing`);
  }
  if (st.isSymbolicLink()) authorityError(`${kind} must not be a symlink`);
  if (exactOwner ? st.uid !== process.getuid() : ![0, process.getuid()].includes(st.uid)) {
    authorityError(`${kind} has unsafe ownership`);
  }
  if ((st.mode & 0o022) !== 0) authorityError(`${kind} is group- or other-writable`);
  return st;
}

function resolvePiAuthority() {
  const configured = process.env.PI_CODING_AGENT_DIR;
  if (!configured) {
    authorityError("PI_CODING_AGENT_DIR is required and must match factory pi2");
  }
  if (!isAbsolute(configured) || configured !== resolve(configured)) {
    authorityError("PI_CODING_AGENT_DIR must be an absolute normalized path");
  }

  const home = realpathSync(userInfo().homedir);
  const piRoot = join(home, ".pi");
  const rel = relative(piRoot, configured);
  if (!rel || rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    authorityError("PI_CODING_AGENT_DIR must be beneath the invoking user's ~/.pi directory");
  }

  requireSafeNode(home, "home directory");
  requireSafeNode(piRoot, "~/.pi directory", { exactOwner: true });
  let current = piRoot;
  for (const component of rel.split(sep)) {
    current = join(current, component);
    const st = requireSafeNode(current, "agent directory", { exactOwner: true });
    if (!st.isDirectory()) authorityError("agent directory path contains a non-directory");
  }
  if (realpathSync(configured) !== configured) authorityError("agent directory must be canonical");

  const checkedFile = (name) => {
    const path = join(configured, name);
    const st = requireSafeNode(path, name, { exactOwner: true });
    if (!st.isFile()) authorityError(`${name} must be a regular file`);
    if (st.nlink !== 1) authorityError(`${name} must have exactly one link`);
    if ((st.mode & 0o077) !== 0 || (st.mode & 0o400) === 0) {
      authorityError(`${name} must be owner-readable and inaccessible to group/other`);
    }
    if (st.size < 1 || st.size > 4 * 1024 * 1024) authorityError(`${name} has an unsafe size`);
    return path;
  };
  const authPath = checkedFile("auth.json");
  const modelsPath = checkedFile("models.json");

  // Cross-check the loaded SDK's own standard resolver. agentDir alone does
  // not retarget an already-created ModelRuntime, hence the explicit paths.
  if (getAgentDir() !== configured) authorityError("Pi SDK agent directory resolution disagrees");
  return { agentDir: configured, authPath, modelsPath };
}

const authority = resolvePiAuthority();

const args = process.argv.slice(2);
function opt(name) {
  const i = args.indexOf(name);
  if (i < 0) throw new Error(`missing option ${name}`);
  return args[i + 1];
}

const imagePath = resolve(opt("--image"));
const expectedSha = opt("--expected-sha256");
const stateId = opt("--state-id");
const role = opt("--role");
const promptFile = resolve(opt("--prompt-file"));
const expectedDescriptionBase64 = opt("--expected-description-base64");
const calibrationExpectation = opt("--calibration-expectation");
const promptSha = opt("--prompt-sha256");
const schemaSha = opt("--schema-sha256");
const requestNonce = opt("--request-nonce");
const modelName = opt("--model");
const outDir = resolve(opt("--out-dir"));

if (!NONCE_RE.test(requestNonce)) {
  console.error("visual-audit-sdk: request nonce must be 32 lowercase hex chars (>=128 bits)");
  process.exit(2);
}
if (!SHA256_RE.test(promptSha) || !SHA256_RE.test(schemaSha) || !SHA256_RE.test(expectedSha)) {
  console.error("visual-audit-sdk: sealed sha256 bindings must be 64 lowercase hex chars");
  process.exit(2);
}
if (!["none", "pass", "finding"].includes(calibrationExpectation)) {
  console.error("visual-audit-sdk: calibration expectation must be none, pass, or finding");
  process.exit(2);
}
if (!BASE64_RE.test(expectedDescriptionBase64)) {
  console.error("visual-audit-sdk: expected description must be canonical base64");
  process.exit(2);
}
const expectedDescriptionBytes = Buffer.from(expectedDescriptionBase64, "base64");
if (expectedDescriptionBytes.length > MAX_EXPECTED_DESCRIPTION_BYTES
    || expectedDescriptionBytes.toString("base64") !== expectedDescriptionBase64) {
  console.error("visual-audit-sdk: expected description base64 is non-canonical or too large");
  process.exit(2);
}
let expectedDescription;
try {
  expectedDescription = new TextDecoder("utf-8", { fatal: true }).decode(expectedDescriptionBytes);
} catch {
  console.error("visual-audit-sdk: expected description is not valid UTF-8");
  process.exit(2);
}
if (calibrationExpectation === "none" && expectedDescription.length === 0) {
  console.error("visual-audit-sdk: live expected description must not be empty");
  process.exit(2);
}

const bytes = readFileSync(imagePath);
const imageSha = createHash("sha256").update(bytes).digest("hex");
if (imageSha !== expectedSha) {
  console.error(`visual-audit-sdk: image hash mismatch (expected ${expectedSha}, got ${imageSha})`);
  process.exit(3);
}
const b64 = bytes.toString("base64");
const promptTemplate = readFileSync(promptFile, "utf8");
if (!promptTemplate.includes("{expected_json}")) {
  console.error("visual-audit-sdk: prompt template omits the required expected-description placeholder");
  process.exit(2);
}

// Domain-separated task binding: the existing sealed prompt_sha256 now binds
// the exact template bytes, exact expected-state description, and a separate
// calibration classification. The classification is committed but is not
// disclosed to the reviewer (avoids turning calibration into answer leakage).
const promptTemplateSha = createHash("sha256").update(promptTemplate).digest("hex");
const taskPromptBinding = JSON.stringify({
  schema: TASK_PROMPT_BINDING_SCHEMA,
  prompt_template_sha256: promptTemplateSha,
  expected_description: expectedDescription,
  calibration_expectation: calibrationExpectation,
});
const computedPromptSha = createHash("sha256").update(taskPromptBinding).digest("hex");
if (computedPromptSha !== promptSha) {
  console.error("visual-audit-sdk: task prompt/expected-description binding mismatch (tampered or stale)");
  process.exit(3);
}

// Expected criteria are inserted only as a JSON-escaped data string. Quotes,
// markdown delimiters, newlines, option-looking text, and prompt-like content
// cannot break out of the template's task-context data boundary.
const promptValues = new Map([
  ["{role}", role],
  ["{state_id}", stateId],
  ["{expected_json}", JSON.stringify(expectedDescription)],
  ["{image_sha256}", imageSha],
  ["{model}", modelName],
  ["{prompt_sha256}", promptSha],
  ["{schema_sha256}", schemaSha],
  ["{request_nonce}", requestNonce],
]);
// One-pass substitution is security-significant: placeholder-looking text
// inside the untrusted expected description must remain byte-for-byte data,
// rather than being rewritten by a later replacement pass.
const prompt = promptTemplate.replace(
  /\{(?:role|state_id|expected_json|image_sha256|model|prompt_sha256|schema_sha256|request_nonce)\}/g,
  (placeholder) => promptValues.get(placeholder),
);

// Keep remote catalog cache state in memory. This prevents an unvalidated
// models-store.json sibling from becoming a third filesystem config input.
const inMemoryModelsStore = {
  async read() { return undefined; },
  async write() {},
  async delete() {},
};
const modelRuntime = await ModelRuntime.create({
  authPath: authority.authPath,
  modelsPath: authority.modelsPath,
  modelsStore: inMemoryModelsStore,
  allowModelNetwork: false,
});
const model = modelRuntime.getModel(...modelName.split("/"));
if (!model) throw new Error("configured visual-audit model is not registered by the trusted Pi authority");

// The visual reviewer needs no tools, extensions, skills, project context, or
// mutable settings. Suppressing those surfaces prevents unrelated Pi config
// from becoming executable review input while retaining the authority's model
// and credentials only.
const settingsManager = SettingsManager.inMemory({
  compaction: { enabled: false },
  retry: { enabled: false },
});
const resourceLoader = new DefaultResourceLoader({
  cwd: process.cwd(),
  agentDir: authority.agentDir,
  settingsManager,
  noExtensions: true,
  noSkills: true,
  noPromptTemplates: true,
  noThemes: true,
  noContextFiles: true,
});
await resourceLoader.reload();
const { session } = await createAgentSession({
  sessionManager: SessionManager.inMemory(),
  settingsManager,
  resourceLoader,
  noTools: "all",
  modelRuntime,
  model,
  agentDir: authority.agentDir,
});

let out = "";
session.subscribe((event) => {
  if (event.type === "message_update" && event.assistantMessageEvent?.type === "text_delta") {
    out += event.assistantMessageEvent.delta;
  }
});

const startedAt = Date.now();
try {
  await session.prompt(prompt, {
    images: [{ type: "image", data: b64, mimeType: "image/png" }],
  });
} finally {
  session.dispose?.();
  modelRuntime.dispose?.();
}
const finishedAt = Date.now();
const elapsedMs = finishedAt - startedAt;

// Raw response commitment: SHA-256 over the exact streamed text before any
// parsing or normalization (never the parsed object re-serialized).
const rawResponseSha = createHash("sha256").update(out).digest("hex");

// Extract the strict JSON finding (no markdown fences).
const start = out.indexOf("{");
const end = out.lastIndexOf("}");
if (start < 0 || end <= start) {
  console.error("visual-audit-sdk: model returned no JSON object");
  process.exit(4);
}
let finding;
try {
  finding = JSON.parse(out.slice(start, end + 1));
} catch (err) {
  console.error(`visual-audit-sdk: malformed JSON from model: ${err.message}`);
  process.exit(5);
}
if (typeof finding !== "object" || finding === null || Array.isArray(finding)) {
  console.error("visual-audit-sdk: model returned a non-object JSON value");
  process.exit(5);
}

// Sealed-field verification. The driver never repairs or overwrites what the
// model returned: any mismatch is fatal so a finding can never borrow another
// task's state/image/nonce or drift from the frozen protocol.
function requireEcho(actual, expected, label) {
  if (actual !== expected) {
    throw new Error(
      `model-returned ${label} ${JSON.stringify(actual)} != sealed ${JSON.stringify(expected)}`,
    );
  }
}
try {
  requireEcho(finding.schema, FINDING_SCHEMA, "schema");
  requireEcho(finding.state_id, stateId, "state_id");
  requireEcho(finding.image_sha256, imageSha, "image_sha256");
  requireEcho(finding.role, role, "role");
  requireEcho(finding.model, modelName, "model");
  requireEcho(finding.prompt_sha256, promptSha, "prompt_sha256");
  requireEcho(finding.schema_sha256, schemaSha, "schema_sha256");
  requireEcho(finding.request_nonce, requestNonce, "request_nonce");
  if (!Array.isArray(finding.observations)) {
    throw new Error("model-returned observations must be an array");
  }
} catch (err) {
  console.error(`visual-audit-sdk: ${err.message}`);
  process.exit(6);
}

// Normalized finding digest over the exact bytes written to the finding file.
const findingBytes = Buffer.from(JSON.stringify(finding, null, 2) + "\n", "utf8");
const findingSha = createHash("sha256").update(findingBytes).digest("hex");

const receipt = {
  schema: RECEIPT_SCHEMA,
  request_nonce: requestNonce,
  state_id: stateId,
  role,
  image_sha256: imageSha,
  prompt_sha256: promptSha,
  schema_sha256: schemaSha,
  model: modelName,
  raw_response_sha256: rawResponseSha,
  finding_sha256: findingSha,
  started_at_ms: startedAt,
  finished_at_ms: finishedAt,
  elapsed_ms: elapsedMs,
};

mkdirSync(outDir, { recursive: true });
function writeSealed(rel, data) {
  const path = resolve(outDir, rel);
  writeFileSync(path, data, { mode: 0o600 });
  chmodSync(path, 0o600); // an existing file must be re-sealed to 0600 too
}
writeSealed(`finding-${stateId}-${role}.json`, findingBytes);
writeSealed(`receipt-${stateId}-${role}.json`, Buffer.from(JSON.stringify(receipt, null, 2) + "\n", "utf8"));
console.log(`visual-audit-sdk: ${stateId}/${role} verdict=${finding.verdict} (${imageSha.slice(0, 12)})`);
