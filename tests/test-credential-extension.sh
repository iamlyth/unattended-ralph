#!/usr/bin/env bash
# Adversarial validation of the Pi tool-call extension credential guardrail
# (scripts/pi-ralph-emit-extension.mjs) and its tracked sibling guard
# (scripts/credential-guard.py). Only fake secrets are used: every value is a
# clearly-labelled FAKE_* placeholder, so nothing here touches real credential
# material. The node fixture imports the exported extension helpers and
# simulates the registered tool_call/tool_result hooks to prove fail-closed
# tool-input classification, guard failure behavior, tool-result redaction,
# overflow-log sanitization, and guard-before-rewrite registration order.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
EXTENSION="$PROJECT_ROOT/scripts/pi-ralph-emit-extension.mjs"
GUARD="$PROJECT_ROOT/scripts/credential-guard.py"

if ! command -v node >/dev/null 2>&1; then
    echo "test-credential-extension: node unavailable — skipping extension checks" >&2
    exit 0
fi

[[ -f "$EXTENSION" && -f "$GUARD" ]] || {
    echo "test-credential-extension: missing extension or guard file" >&2
    exit 1
}

# Extension node syntax must stay green.
node --check "$EXTENSION"

# The guard must stay importable/compilable (py_compile writes to __pycache__
# under the tracked tree; remove the cache artifact we just produced).
python3 -m py_compile "$GUARD"
rm -f "$PROJECT_ROOT"/scripts/__pycache__/credential-guard.cpython-*.pyc

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/tmpdir"

# The fixture runs against a hermetic TMPDIR so the fake overflow log stays
# inside the test. Guard executables are swapped only through the explicit
# options.guardPath helper (never through the environment), and the registered
# hooks are proven to ignore hostile CREDENTIAL_GUARD and
# RALPH_CREDENTIAL_GUARD_TESTING variables because the runtime resolver is
# immutable.
TMPDIR="$tmp/tmpdir" \
    node --input-type=module - "$EXTENSION" "$tmp" <<'EOF'
import assert from 'node:assert/strict';
import {
  chmodSync, linkSync, mkdirSync, readFileSync, statSync, symlinkSync, writeFileSync,
} from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';

const extensionUrl = process.argv[2];
const work = process.argv[3];
const extension = await import(pathToFileURL(extensionUrl));

// Every exported helper must exist so the harness can exercise it directly.
for (const name of [
  'rewriteRalphEmitCommand', 'rewriteGitCommitCommand', 'resolveGuardPath',
  'isOwnedRegularFile', 'parseBashOverflowPath', 'parseStrictGuardLine',
  'runGuardCheck', 'redactText', 'redactDeep', 'redactToolResultPatch',
  'failRedactedResult', 'guardToolCallInput', 'sanitizeBashOverflowPath',
  'truncateOwnedOverflowFile',
]) {
  assert.equal(typeof extension[name], 'function', `missing exported helper ${name}`);
}
assert.equal(typeof extension.default, 'function', 'missing default registration');

// TEST_ENV never carries guard overrides: the immutable resolver ignores them
// and every helper swap below is expressed as an explicit guardPath option.
const TEST_ENV = { ...process.env };
delete TEST_ENV.CREDENTIAL_GUARD;
delete TEST_ENV.RALPH_CREDENTIAL_GUARD_TESTING;
const TMP = tmpdir();
const trackedGuard = extension.resolveGuardPath();

// ---------------------------------------------------------------------------
// 1. Tool-input guardrail (pure calls, real tracked guard).
// ---------------------------------------------------------------------------
const block = (event, options) => {
  const guarded = extension.guardToolCallInput(event, options);
  assert(guarded && guarded.block === true, `expected block for ${JSON.stringify(event)}`);
  return guarded;
};
const allow = (event, options) => {
  const guarded = extension.guardToolCallInput(event, options);
  assert.equal(guarded, null, `expected allow for ${JSON.stringify(event)}`);
};

// bash: env/proc/auth/interpreter material is blocked, never echoed.
for (const command of [
  'cat /proc/self/environ',
  'head /proc/1234/cmdline',
  'sudo cat /proc/self/environ',
  'true && tail -c 64 /proc/self/environ',
  'cat ~/.ssh/id_rsa',
  'cat ~/.netrc',
  'cat .env',
  'cat .env.production',
  'cat ~/.ollama-usage-env',
  'cat auth.json',
  'cat token.json',
  'cat ~/.aws/credentials',
  'cat ~/.config/gcloud/application_default_credentials.json',
  'cat server.pem',
  'cat server.key',
  'env',
  'printenv',
  'set',
  'export',
  'env | grep -i secret',
  'set | grep SECRET',
  'ps eww',
  'ps -eo pid,env,cmd',
  'echo "$TOKEN"',
  'FOO="$GITHUB_TOKEN"',
  "python3 -c \"import os; print(os.environ['TOKEN'])\"",
  "python3 -c \"import os; print(os.environ)\"",
  "python3 -c \"import os; print(os.environ.get('GITHUB_TOKEN'))\"",
  "node -e \"console.log(process.env.TOKEN)\"",
  "perl -e 'print %ENV'",
  "sh -c 'env && ls'",
  'echo "$AWS_ACCESS_KEY_ID"',
  "sh -c 'cat /proc/self/environ'",
  'curl https://fakeuser:FAKE_USER_PASS_777@host.invalid/',
]) {
  const guarded = block({ toolName: 'bash', input: { command } }, { env: TEST_ENV });
  assert(!guarded.reason.includes('FAKE_USER_PASS_777'), 'block reason echoed input');
}

// read/edit/write sensitive paths are blocked.
for (const tool of ['read', 'edit', 'write']) {
  for (const path of [
    '/proc/self/environ',
    '/proc/1234/cmdline',
    '~/.ssh/id_ed25519',
    '~/.netrc',
    '.env',
    'auth.json',
    'token.json',
    '~/.ollama-usage-env',
    'server.key',
    'credentials.json',
  ]) {
    block({ toolName: tool, input: { path } }, { env: TEST_ENV });
  }
}

// Allowed build/env-assignment/project commands and paths pass through.
for (const command of [
  'cmake --build build-check --parallel',
  'ctest --test-dir build-check --output-on-failure',
  'env HOME=/tmp FOO=bar make test',
  'export CI=true && make test',
  'set -euo pipefail',
  'grep -rn "token" src/',
  'git status --short',
  "nix-shell --run 'cmake --build build-check'",
  'ralph emit factory.implement done',
]) {
  allow({ toolName: 'bash', input: { command } }, { env: TEST_ENV });
}
for (const tool of ['read', 'edit', 'write']) {
  for (const path of ['src/CMakeLists.txt', 'build-check/test.log', 'tests/CMakeLists.txt']) {
    allow({ toolName: tool, input: { path } }, { env: TEST_ENV });
  }
}

// ralph emit and git guard rewrites are preserved once the guardrail allows.
const ralph = extension.rewriteRalphEmitCommand('ralph emit factory.implement done');
assert.equal(ralph.matched, true);
assert.equal(ralph.command, './scripts/pi-cli-shims/ralph emit factory.implement done');
const git = extension.rewriteGitCommitCommand('git commit -m "done"');
assert.equal(git.matched, true);
assert.equal(git.command, './scripts/pi-cli-shims/git commit -m "done"');
assert.equal(extension.rewriteGitCommitCommand('git commit --no-verify -m x').blocked, true);
assert.equal(extension.rewriteGitCommitCommand('git cherry-pick abc').blocked, true);
// Repeated -C is still routed through the argv-level shim; --git-dir and
// --work-tree redirection of the commit boundary is blocked before Git runs.
const multiC = extension.rewriteGitCommitCommand('git -C /a -C /b commit -m "x"');
assert.equal(multiC.matched, true);
assert.equal(multiC.command, './scripts/pi-cli-shims/git -C /a -C /b commit -m "x"');
assert.equal(extension.rewriteGitCommitCommand('git --git-dir=/tmp/fake-repo commit -m "x"').blocked, true);
assert.equal(extension.rewriteGitCommitCommand('git --work-tree /tmp/fake commit -m "x"').blocked, true);
assert.equal(extension.rewriteGitCommitCommand('git -c core.hookspath=/tmp/fake commit -m "x"').blocked, true);
assert.equal(extension.rewriteGitCommitCommand('git --config core.HooksPath=/tmp/fake commit -m "x"').blocked, true);
assert.equal(extension.rewriteGitCommitCommand('git --config-env CORE.HOOKSPATH=FAKE_ENV commit -m "x"').blocked, true);

// ---------------------------------------------------------------------------
// 2. Fail-closed guard failure: missing, crashing, timing-out, and invalid
//    guard executables never echo the input. Guard executables are swapped
//    only through the explicit options.guardPath helper; the environment can
//    never redirect the immutable runtime resolver.
// ---------------------------------------------------------------------------
const crashGuard = join(work, 'crash-guard.py');
const timeoutGuard = join(work, 'timeout-guard.py');
const invalidGuard = join(work, 'invalid-guard.py');
const badSchemaGuard = join(work, 'bad-schema-guard.py');
writeFileSync(crashGuard, 'import sys\nsys.exit(7)\n');
writeFileSync(timeoutGuard, 'import time\ntime.sleep(60)\n');
writeFileSync(invalidGuard, "import sys\nsys.stdout.write('definitely not json\\n')\n");
writeFileSync(badSchemaGuard, "import sys\nsys.stdout.write('{\"verdict\":\"allow\"}\\n')\n");

// resolveGuardPath is immutable: it takes no arguments, ignores any env
// override, and always resolves to the tracked sibling guard.
assert.equal(
  extension.resolveGuardPath({ RALPH_CREDENTIAL_GUARD_TESTING: '1', CREDENTIAL_GUARD: '/override.py' }),
  trackedGuard,
);
assert.notEqual(trackedGuard, '/override.py');
assert(trackedGuard.endsWith('scripts/credential-guard.py'));

const echoInput = 'cat /proc/self/environ FAKE_NEVER_ECHO_42';
for (const [label, guardPath, opts] of [
  ['missing', join(work, 'missing-guard.py'), {}],
  ['crashing', crashGuard, {}],
  ['timing-out', timeoutGuard, { timeout: 150 }],
  ['invalid-output', invalidGuard, {}],
  ['bad-schema-output', badSchemaGuard, {}],
]) {
  // The swap is expressed as an explicit guardPath option; env cannot redirect.
  const direct = extension.runGuardCheck('check-command-stdin', echoInput, { ...opts, env: TEST_ENV, guardPath });
  assert.equal(direct.allowed, false, `${label} guard allowed input`);
  assert.equal(direct.reason, label === 'timing-out' ? 'guard-unavailable' : 'invalid-guard-output', label);
  const event = { toolName: 'bash', input: { command: echoInput } };
  const guarded = block(event, { ...opts, env: TEST_ENV, guardPath });
  assert(!guarded.reason.includes('FAKE_NEVER_ECHO_42'), `${label} guard echoed input`);
  assert(!guarded.reason.includes('/proc/self/environ'), `${label} guard echoed input path`);
}

// Pure fail-closed runGuardCheck input validation and real allow/block verdicts.
assert.equal(extension.runGuardCheck('bogus-mode', 'x', { env: TEST_ENV }).reason, 'invalid-mode');
assert.equal(extension.runGuardCheck('check-command-stdin', 42, { env: TEST_ENV }).reason, 'invalid-input');
assert.equal(extension.runGuardCheck('check-command-stdin', 'x'.repeat((1 << 20) + 1), { env: TEST_ENV }).reason, 'oversized-input');
assert.equal(extension.runGuardCheck('check-command-stdin', 'cat /proc/self/environ', { env: TEST_ENV }).reason, 'procfs-environ-cmdline');
assert.equal(extension.runGuardCheck('check-command-stdin', 'cmake --build build-check', { env: TEST_ENV }).allowed, true);

// Strict verdict-line parsing fails closed on anything malformed.
const goodLine = '{"schema":"credential-guard/v1","tool":"credential-guard","version":"1","verdict":"block","reason":"procfs-environ-cmdline","reasons":["procfs-environ-cmdline"]}';
assert.deepEqual(extension.parseStrictGuardLine(goodLine + '\n'), { verdict: 'block', reason: 'procfs-environ-cmdline', reasons: ['procfs-environ-cmdline'] });
for (const bad of ['', 'not json', '{"verdict":"allow"}', '{"schema":"credential-guard/v1","tool":"credential-guard","version":"1","verdict":"partial","reason":"x","reasons":[]}', '{"schema":"credential-guard/v1","tool":"credential-guard","version":"1","verdict":"allow","reason":"ok","reasons":"nope"}', '42']) {
  assert.equal(extension.parseStrictGuardLine(bad), null, bad);
}

// ---------------------------------------------------------------------------
// 3. Tool-result redaction: content items and nested details mask
//    assignments, JWTs, Bearer headers, and private-key blocks while
//    preserving non-strings, non-text items, isError, and usage. Cyclic or
//    guard-failure results fail closed to exactly [REDACTION FAILED] and
//    clear details.
// ---------------------------------------------------------------------------
const fakeSecretValue = 'FAKE_SECRET_VALUE_998877665544332211';
const fakeApiSecret = 'FAKE_APISECRET_00112233445566778899aa';
const fakeBearer = 'FAKE_BEARER_000111222333444555666';
const fakeJwtSig = 'FAKE_JWT_SIGNATURE_999888777666';
const fakeKeyMaterial = 'MIIFakePrivateKeyMaterial000111222333444555666777888999aaabbbcccdddeeefff';
const resultEvent = {
  toolName: 'bash',
  content: [
    { type: 'text', text: `export FAKE_TOKEN_AB12CD34=${fakeSecretValue}` },
    { type: 'image', data: 'AAAA', caption: 'keep me' },
    { type: 'text', text: 'Authorization: Bearer ' + fakeBearer },
    { type: 'text', text: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJmYWtlIn0.' + fakeJwtSig },
    { type: 'text', text: '-----BEGIN PRIVATE KEY-----\n' + fakeKeyMaterial + '\n-----END PRIVATE KEY-----' },
  ],
  details: {
    env: { github: 'GITHUB_TOKEN=' + fakeApiSecret },
    query: 'https://fakeuser:FAKE_USER_PASS_777@host.invalid/api?api_key=FAKE_QUERY_KEY_444555666&page=2&limit=10',
    note: 'build dir /tmp/fake-build-123',
    count: 7,
    flag: false,
    nul: null,
    arr: [1, 'keep me', { keep: true }],
  },
  isError: false,
  usage: { input: 1, output: 2 },
};
const redacted = extension.redactToolResultPatch(resultEvent, { env: TEST_ENV });
assert.equal(redacted.failed, false);
// Content text items are masked; non-text items survive untouched (redactDeep
// round-trips through JSON, so identity is replaced by value equality).
assert.deepEqual(redacted.patch.content[1], resultEvent.content[1]);
assert.equal(redacted.patch.content[0].text, `export FAKE_TOKEN_AB12CD34=[REDACTED]`);
assert.equal(redacted.patch.content[2].text, 'Authorization: [REDACTED]');
// Nested details: assignment masked, non-secret leaves preserved.
assert.equal(redacted.patch.details.env.github, 'GITHUB_TOKEN=[REDACTED]');
assert.equal(redacted.patch.details.note, 'build dir /tmp/fake-build-123');
assert.equal(redacted.patch.details.count, 7);
assert.equal(redacted.patch.details.flag, false);
assert.equal(redacted.patch.details.nul, null);
assert.deepEqual(redacted.patch.details.arr, [1, 'keep me', { keep: true }]);
// No fake secret material anywhere in the patch.
const patchText = JSON.stringify(redacted.patch);
for (const fake of [fakeSecretValue, fakeApiSecret, fakeBearer, fakeJwtSig, fakeKeyMaterial, 'FAKE_USER_PASS_777', 'FAKE_QUERY_KEY_444555666']) {
  assert(!patchText.includes(fake), `leaked ${fake}`);
}
assert(patchText.includes('[REDACTED-PRIVATE-KEY-BLOCK]'), 'private-key block not masked');
assert(patchText.includes('[REDACTED-JWT]'), 'JWT not masked');
// isError/usage are preserved by omission; the original event fields stay untouched.
assert(!('isError' in redacted.patch));
assert(!('usage' in redacted.patch));
assert.equal(resultEvent.isError, false);
assert.deepEqual(resultEvent.usage, { input: 1, output: 2 });

// redactText helpers: masking, idempotency, non-string/oversize rejection.
const plain = `export FAKE_TOKEN_AB12CD34=${fakeSecretValue}\n`;
const one = extension.redactText(plain, { env: TEST_ENV });
assert.equal(one.ok, true);
assert(!one.text.includes(fakeSecretValue));
assert(one.text.includes('[REDACTED]'));
const two = extension.redactText(one.text, { env: TEST_ENV });
assert.equal(two.ok, true);
assert.equal(two.text, one.text);
assert.equal(extension.redactText(42, { env: TEST_ENV }).ok, false);
assert.equal(extension.redactText('', { env: TEST_ENV }).ok, true);
assert.equal(extension.redactText('a'.repeat((8 << 20) + 1), { env: TEST_ENV }).ok, false);

// Guard failure during redaction fails the whole patch closed (guard swapped
// explicitly via guardPath, never via env).
const failGuard = join(work, 'missing-guard.py');
assert.equal(extension.redactText('TOKEN=FAKE_X\n', { env: TEST_ENV, guardPath: failGuard }).ok, false);
const failedPatch = extension.redactToolResultPatch(
  { toolName: 'bash', content: [{ type: 'text', text: `TOKEN=${fakeSecretValue}` }], details: {} },
  { env: TEST_ENV, guardPath: failGuard },
);
assert.equal(failedPatch.failed, true);
assert.equal(failedPatch.patch, null);

// Cyclic details: redactDeep fails closed to {ok:false} without ever throwing
// (JSON.stringify of the cyclic payload is caught); the patch caller then
// fails the whole result closed.
const cyc = { a: 1 };
cyc.self = cyc;
assert.deepEqual(extension.redactDeep(cyc, { env: TEST_ENV }), { ok: false, value: null });
const cyclicPatch = extension.redactToolResultPatch({ content: [{ type: 'text', text: 'x' }], details: cyc }, { env: TEST_ENV });
assert.equal(cyclicPatch.failed, true);

// failRedactedResult: only [REDACTION FAILED], details cleared, nothing else.
const doomed = { toolName: 'bash', content: [{ type: 'text', text: 'anything' }], details: { secret: fakeSecretValue }, isError: false };
const failedResult = extension.failRedactedResult(doomed);
assert.deepEqual(failedResult, { content: [{ type: 'text', text: '[REDACTION FAILED]' }] });
assert.equal(doomed.details, undefined);
assert.equal(doomed.isError, false);

// Counting fake guard (redact-json-stdin): proves redactToolResultPatch
// streams ONE whole JSON payload through a single guard subprocess, redacts
// every nested string value (2000 of them) plus content auxiliary string
// fields, and leaves isError/usage untouched.
const countLog = join(work, 'guard-count.log');
writeFileSync(countLog, '0');
const countingGuard = join(work, 'counting-guard.py');
writeFileSync(countingGuard, String.raw`import json, sys
with open(${JSON.stringify(countLog)}, 'r') as stream:
    count = int(stream.read().strip())
with open(${JSON.stringify(countLog)}, 'w') as stream:
    stream.write(str(count + 1))
value = json.load(sys.stdin)
def visit(item):
    if isinstance(item, str):
        return '[REDACTED]'
    if isinstance(item, list):
        return [visit(child) for child in item]
    if isinstance(item, dict):
        return {key: visit(child) for key, child in item.items()}
    return item
json.dump(visit(value), sys.stdout)
`);
const nestedSecret = 'FAKE_NESTED_SECRET_9988776655';
// 2000 string values nested inside the payload (kept shallow so the fake guard
// walks them without exhausting Python's recursion stack).
const nestedDeep = [];
for (let i = 0; i < 2000; i += 1) {
  nestedDeep.push({ value: `secret_${nestedSecret}_${i}`, meta: { ordinal: i } });
}
const nestedEvent = {
  toolName: 'bash',
  content: [
    { type: 'text', text: 'nested content' },
    { type: 'image', data: 'AAAA', caption: `cap_${nestedSecret}` },
  ],
  details: { deep: nestedDeep },
  isError: false,
  usage: { input: 1, output: 2 },
};
const nestedPatch = extension.redactToolResultPatch(nestedEvent, { env: TEST_ENV, guardPath: countingGuard });
assert.equal(nestedPatch.failed, false);
assert.equal(parseInt(readFileSync(countLog, 'utf8'), 10), 1, 'redactToolResultPatch did not invoke the guard exactly once');
// Every one of the 2000 nested string values was redacted to the guard marker.
assert.equal(nestedPatch.patch.details.deep.length, 2000);
nestedPatch.patch.details.deep.forEach((item, index) => {
  assert.equal(item.value, '[REDACTED]');
  assert.equal(item.meta.ordinal, index); // numbers survive redaction
});
// Content auxiliary string fields (type, text, caption) are redacted too.
assert.equal(nestedPatch.patch.content[0].type, '[REDACTED]');
assert.equal(nestedPatch.patch.content[0].text, '[REDACTED]');
assert.equal(nestedPatch.patch.content[1].caption, '[REDACTED]');
// Non-string values survive; isError/usage are untouched by omission.
assert.deepEqual(nestedPatch.patch.content[1], { type: '[REDACTED]', data: '[REDACTED]', caption: '[REDACTED]' });
assert(!('isError' in nestedPatch.patch));
assert(!('usage' in nestedPatch.patch));
assert.equal(nestedEvent.isError, false);
assert.deepEqual(nestedEvent.usage, { input: 1, output: 2 });
assert(!JSON.stringify(nestedPatch.patch).includes(nestedSecret), 'nested fake secret leaked');

// ---------------------------------------------------------------------------
// 4. Overflow-log sanitization: only a canonical, owned, single-link regular
//    tmpdir()/pi-bash-<16 hex>.log is atomically sanitized. Symlink,
//    non-canonical, hard-linked, and directory cases never touch the target
//    and fail safe; a failing guard truncates the recognized owned file.
// ---------------------------------------------------------------------------
const hex = '0123456789abcdef';
const canonical = join(TMP, `pi-bash-${hex}.log`);
const rawFake = 'RAW_FAKE_TOKEN_VALUE_ABCDEF123456';
writeFileSync(canonical, `export FAKE_TOKEN_AB12CD34=${rawFake}\nkeep this line\n`);
chmodSync(canonical, 0o644);
const sanitized = await extension.sanitizeBashOverflowPath(canonical, { env: TEST_ENV, guardPath: trackedGuard });
assert.equal(sanitized.ok, true, sanitized.reason);
const sanitizedText = readFileSync(canonical, 'utf8');
assert(!sanitizedText.includes(rawFake), 'raw fake value survived sanitize');
assert(sanitizedText.includes('[REDACTED]'), 'assignment not masked');
assert(sanitizedText.includes('keep this line'), 'diagnostic line dropped');
assert((statSync(canonical).mode & 0o077) === 0, 'sanitized file not 0600');
assert.equal(extension.parseBashOverflowPath(canonical).id, hex);

// parseBashOverflowPath shape rejection.
assert.equal(extension.parseBashOverflowPath(''), null);
assert.equal(extension.parseBashOverflowPath(null), null);
assert.equal(extension.parseBashOverflowPath(42), null);
assert.equal(extension.parseBashOverflowPath(join(TMP, 'pi-bash-nope.log')), null);
assert.equal(extension.parseBashOverflowPath(join(TMP, `pi-bash-${hex}.txt`)), null);
assert.equal(extension.parseBashOverflowPath(join(work, `pi-bash-${hex}.log`)), null);

// Symlink at the canonical path: identity predicate rejects it; target intact.
const symlinkTarget = join(work, 'symlink-target.txt');
writeFileSync(symlinkTarget, 'KEEP_SYMLINK_TARGET');
const symlinkPath = join(TMP, 'pi-bash-0123456789abcde0.log');
symlinkSync(symlinkTarget, symlinkPath);
assert.equal(extension.parseBashOverflowPath(symlinkPath).canonical, symlinkPath);
const symlinkResult = await extension.sanitizeBashOverflowPath(symlinkPath, { env: TEST_ENV, guardPath: trackedGuard });
assert.equal(symlinkResult.ok, false);
assert.equal(symlinkResult.reason, 'overflow-file-identity');
assert.equal(readFileSync(symlinkTarget, 'utf8'), 'KEEP_SYMLINK_TARGET');
assert.equal(await extension.truncateOwnedOverflowFile(symlinkPath), false);
assert.equal(readFileSync(symlinkTarget, 'utf8'), 'KEEP_SYMLINK_TARGET');

// Non-canonical path: parsed name matches but the absolute path does not.
const nonCanonical = join(work, 'pi-bash-0123456789abcde2.log');
writeFileSync(nonCanonical, 'untouchable');
const nonCanonicalResult = await extension.sanitizeBashOverflowPath(nonCanonical, { env: TEST_ENV, guardPath: trackedGuard });
assert.equal(nonCanonicalResult.ok, false);
assert.equal(nonCanonicalResult.reason, 'non-canonical-overflow-path');
assert.equal(readFileSync(nonCanonical, 'utf8'), 'untouchable');

// A directory at the canonical path (unsafe non-regular) is never touched.
const dirPath = join(TMP, 'pi-bash-0123456789abcde3.log');
mkdirSync(dirPath);
writeFileSync(join(dirPath, 'marker'), 'x');
const dirResult = await extension.sanitizeBashOverflowPath(dirPath, { env: TEST_ENV, guardPath: trackedGuard });
assert.equal(dirResult.ok, false);
assert.equal(readFileSync(join(dirPath, 'marker'), 'utf8'), 'x');

// Hard-linked file (nlink 2) is not the single-link owned shape.
const hardLinkPath = join(TMP, 'pi-bash-0123456789abcde4.log');
writeFileSync(hardLinkPath, 'HARDLINKED_CONTENT');
linkSync(hardLinkPath, join(work, 'hardlink-sibling'));
assert.equal(statSync(hardLinkPath).nlink, 2);
const hardLinkResult = await extension.sanitizeBashOverflowPath(hardLinkPath, { env: TEST_ENV, guardPath: trackedGuard });
assert.equal(hardLinkResult.ok, false);
assert.equal(readFileSync(hardLinkPath, 'utf8'), 'HARDLINKED_CONTENT');

// Guard failure on a recognized owned file truncates it (fail closed); the
// failing guard is provided explicitly via guardPath.
const crashTruncatePath = join(TMP, 'pi-bash-0123456789abcde5.log');
writeFileSync(crashTruncatePath, `export FAKE_TOKEN_AB12CD34=${rawFake}\n`);
chmodSync(crashTruncatePath, 0o644);
const crashResult = await extension.sanitizeBashOverflowPath(crashTruncatePath, { env: TEST_ENV, guardPath: crashGuard });
assert.equal(crashResult.ok, false);
assert.equal(crashResult.reason, 'overflow-redact-failed');
assert.equal(statSync(crashTruncatePath).size, 0, 'recognized owned file not truncated');
assert((statSync(crashTruncatePath).mode & 0o077) === 0, 'truncated file not 0600');

// truncateOwnedOverflowFile: recognized owned file truncated, other paths untouched.
const truncatePath = join(TMP, 'pi-bash-0123456789abcde6.log');
writeFileSync(truncatePath, 'erase me');
assert.equal(await extension.truncateOwnedOverflowFile(truncatePath), true);
assert.equal(statSync(truncatePath).size, 0);
assert.equal(await extension.truncateOwnedOverflowFile(join(work, 'pi-bash-0123456789abcde6.log')), false);

// isOwnedRegularFile identity predicate.
const myStats = { isFile: () => true, isSymbolicLink: () => false, nlink: 1, uid: process.getuid() };
assert.equal(extension.isOwnedRegularFile(myStats), true);
assert.equal(extension.isOwnedRegularFile({ ...myStats, nlink: 2 }), false);
assert.equal(extension.isOwnedRegularFile({ ...myStats, uid: myStats.uid + 1 }), false);
assert.equal(extension.isOwnedRegularFile({ ...myStats, isSymbolicLink: () => true }), false);
assert.equal(extension.isOwnedRegularFile(null), false);
assert.equal(extension.isOwnedRegularFile({ isFile: () => false }), false);

// ---------------------------------------------------------------------------
// 5. Registration order and immutable resolver semantics: the registered
//    hooks resolve the tracked sibling guard via an immutable runtime
//    resolver and ignore hostile CREDENTIAL_GUARD / RALPH_CREDENTIAL_GUARD_
//    TESTING environment variables. The guard runs before any ralph/git
//    rewrite (proven by a recording guard observing the exact pre-rewrite
//    stdin), and the tool_result hook is async and fails results closed.
// ---------------------------------------------------------------------------
let toolCallHook = null;
let toolResultHook = null;
extension.default({
  on(name, handler) {
    if (name === 'tool_call') toolCallHook = handler;
    if (name === 'tool_result') toolResultHook = handler;
  },
});
assert.equal(typeof toolCallHook, 'function');
assert.equal(toolResultHook.constructor.name, 'AsyncFunction');

// Recording guard (swapped explicitly via guardPath): logs argv/stdin, allows
// everything, so the fixture can observe exactly what the guard saw. The
// guard classifies the pre-rewrite command on stdin, and only after it allows
// does the ralph rewrite complete.
const stdinLog = join(work, 'guard-stdin.log');
writeFileSync(stdinLog, '');
const recordGuard = join(work, 'record-guard.py');
writeFileSync(recordGuard, String.raw`import sys
mode = sys.argv[1]
data = sys.stdin.buffer.read().decode('utf-8', 'replace')
with open(${JSON.stringify(stdinLog)}, 'ab') as stream:
    stream.write((mode + "\n" + data + "\n---\n").encode())
if mode == 'redact':
    sys.stdout.write(data)
sys.stdout.write('{"schema":"credential-guard/v1","tool":"credential-guard","version":"1","verdict":"allow","reason":"ok","reasons":["ok"]}\n')
`);
const orderEvent = { toolName: 'bash', input: { command: 'ralph emit factory.implement done' } };
assert.equal(extension.guardToolCallInput(orderEvent, { env: TEST_ENV, guardPath: recordGuard }), null);
const seen = readFileSync(stdinLog, 'utf8');
assert(seen.includes('check-command-stdin\nralph emit factory.implement done\n---\n'), `guard did not see the pre-rewrite command: ${JSON.stringify(seen)}`);
assert.equal(extension.rewriteRalphEmitCommand(orderEvent.input.command).command, './scripts/pi-cli-shims/ralph emit factory.implement done');

// Registered hooks ignore hostile env: point CREDENTIAL_GUARD and
// RALPH_CREDENTIAL_GUARD_TESTING at a guard that would block every redirect
// if it were honored. The hooks must still use the immutable tracked resolver
// so the allowed ralph/git rewrites proceed.
const blockAllGuard = join(work, 'block-all-guard.py');
writeFileSync(blockAllGuard, String.raw`import sys
sys.stdout.write('{"schema":"credential-guard/v1","tool":"credential-guard","version":"1","verdict":"block","reason":"hostile","reasons":["hostile"]}')
sys.exit(1)
`);
const savedCred = process.env.CREDENTIAL_GUARD;
const savedTesting = process.env.RALPH_CREDENTIAL_GUARD_TESTING;
process.env.CREDENTIAL_GUARD = blockAllGuard;
process.env.RALPH_CREDENTIAL_GUARD_TESTING = '1';
try {
  const ralphEvent = { toolName: 'bash', input: { command: 'ralph emit factory.implement done' } };
  assert.equal(toolCallHook(ralphEvent), undefined);
  assert.equal(ralphEvent.input.command, './scripts/pi-cli-shims/ralph emit factory.implement done');
  const gitEvent = { toolName: 'bash', input: { command: 'git commit -m "done"' } };
  toolCallHook(gitEvent);
  assert.equal(gitEvent.input.command, './scripts/pi-cli-shims/git commit -m "done"');
} finally {
  if (savedCred !== undefined) process.env.CREDENTIAL_GUARD = savedCred; else delete process.env.CREDENTIAL_GUARD;
  if (savedTesting !== undefined) process.env.RALPH_CREDENTIAL_GUARD_TESTING = savedTesting; else delete process.env.RALPH_CREDENTIAL_GUARD_TESTING;
}

// The registered tool_call hook still blocks a genuinely sensitive input via
// the tracked guard and never rewrites or echoes it.
const blockedEvent = { toolName: 'bash', input: { command: 'ralph emit factory.implement done && cat /proc/self/environ' } };
const blockedResult = toolCallHook(blockedEvent);
assert.equal(blockedResult.block, true);
assert.equal(blockedEvent.input.command, 'ralph emit factory.implement done && cat /proc/self/environ');
assert(!blockedResult.reason.includes('factory.implement'));

// tool_result hook end-to-end: a canonical owned overflow file is sanitized
// in place and content is redacted.
const hookHex = '0123456789abcdf0';
const hookPath = join(TMP, `pi-bash-${hookHex}.log`);
writeFileSync(hookPath, `export FAKE_TOKEN_AB12CD34=${rawFake}\n`);
chmodSync(hookPath, 0o644);
const hookEvent = {
  toolName: 'bash',
  content: [{ type: 'text', text: `export FAKE_TOKEN_AB12CD34=${rawFake}` }],
  details: { fullOutputPath: hookPath },
};
const hookPatch = await toolResultHook(hookEvent);
assert(!JSON.stringify(hookPatch).includes(rawFake));
assert(!readFileSync(hookPath, 'utf8').includes(rawFake));
assert.equal(hookPatch.details.fullOutputPath, hookPath);

// Cyclic details through the real registered hook: only [REDACTION FAILED]
// and details are cleared.
const cycEvent = { toolName: 'bash', content: [{ type: 'text', text: 'x' }], details: cyc, isError: false };
const cycPatch = await toolResultHook(cycEvent);
assert.deepEqual(cycPatch, { content: [{ type: 'text', text: '[REDACTION FAILED]' }] });
assert.equal(cycEvent.details, undefined);
assert.equal(cycEvent.isError, false);

// Unsafe overflow file through the registered hook: fail-redacted, target
// never touched.
const unsafeTarget = join(work, 'unsafe-target.txt');
writeFileSync(unsafeTarget, 'UNSAFE_TARGET_KEEP');
const unsafeHookPath = join(TMP, 'pi-bash-0123456789abcdf1.log');
symlinkSync(unsafeTarget, unsafeHookPath);
const unsafeEvent = { toolName: 'bash', content: [{ type: 'text', text: 'x' }], details: { fullOutputPath: unsafeHookPath } };
const unsafePatch = await toolResultHook(unsafeEvent);
assert.deepEqual(unsafePatch, { content: [{ type: 'text', text: '[REDACTION FAILED]' }] });
assert.equal(unsafeEvent.details, undefined);
assert.equal(readFileSync(unsafeTarget, 'utf8'), 'UNSAFE_TARGET_KEEP');

process.stdout.write('fixture: all credential extension assertions passed\n');
EOF

echo "test: credential extension guardrail checks passed"
