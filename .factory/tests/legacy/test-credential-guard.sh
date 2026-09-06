#!/usr/bin/env bash
# Adversarial credential-guard validation. Only fake secrets are used: every
# "credential" below is a clearly-labelled FAKE_* placeholder that exists
# solely to prove that (a) fail-closed command classification blocks sensitive
# reads/dumps and allows ordinary project commands, and (b) streaming
# redaction removes the exact fake values from stdout/stderr/log output while
# preserving surrounding diagnostics. check-command output is machine-readable
# JSON and never echoes the command text or any secret value.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

GUARD="$PROJECT_ROOT/.factory/tools/credential-guard.py"

# Fake secrets used across the suite (never real credential material).
FAKE_SECRET_VAL="FAKE_SECRET_VALUE_998877665544332211"
FAKE_API_VAL="FAKE_APISECRET_00112233445566778899aa"
FAKE_BEARER_1="FAKE_BEARER_000111222333444555666"
FAKE_BEARER_2="FAKE_BEARER_AAA111222333444555666"
FAKE_USERPASS="FAKE_USER_PASS_777"
FAKE_QUERY_KEY="FAKE_QUERY_KEY_444555666"
FAKE_JSON_KEY="FAKE_JSON_KEY_12345"
FAKE_HEADER_KEY="FAKE_HEADER_KEY_111222333"
FAKE_PASSWD="FAKE_PASSWD_666"
FAKE_JWT_SIG="FAKE_JWT_SIGNATURE_999888777666"
FAKE_E_MATERIAL="MIIFakePrivateKeyMaterial000111222333444555666777888999aaabbbcccdddeeefff"
FAKE_MORE_MATERIAL="FAKE_MORE_KEY_MATERIAL_777"
FAKE_SINGLE_LINE_MATERIAL="MIIFakeSingleLinePemMaterial000111222333444555"
FAKE_NEVER_ECHO="FAKE_NEVER_ECHO_TOKEN_42"

expect_block() {
    local label=$1 expected=$2 command_text=$3 rc out
    set +e
    out=$("$GUARD" check-command --command "$command_text" 2>"$tmp/err")
    rc=$?
    set -e
    [[ $rc -eq 1 ]] || {
        echo "test: check-command '$label' expected block rc=1 got rc=$rc" >&2
        exit 1
    }
    grep -q "\"verdict\":\"block\"" <<<"$out" || {
        echo "test: check-command '$label' did not report verdict block" >&2
        exit 1
    }
    grep -q "\"reason\":\"$expected\"" <<<"$out" || {
        echo "test: check-command '$label' missing reason $expected: $out" >&2
        exit 1
    }
    [[ ! -s "$tmp/err" ]] || {
        echo "test: check-command '$label' wrote stderr: $(cat "$tmp/err")" >&2
        exit 1
    }
}

expect_allow() {
    local label=$1 command_text=$2 rc out
    set +e
    out=$("$GUARD" check-command --command "$command_text" 2>"$tmp/err")
    rc=$?
    set -e
    [[ $rc -eq 0 ]] || {
        echo "test: check-command '$label' expected allow rc=0 got rc=$rc out=$out" >&2
        exit 1
    }
    grep -q "\"verdict\":\"allow\"" <<<"$out" || {
        echo "test: check-command '$label' did not report verdict allow" >&2
        exit 1
    }
    [[ ! -s "$tmp/err" ]] || {
        echo "test: check-command '$label' wrote stderr: $(cat "$tmp/err")" >&2
        exit 1
    }
}

# ---------------------------------------------------------------------------
# check-command: adversarial block matrix (fake secrets / paths only)
# ---------------------------------------------------------------------------

expect_block proc-environ-dump "procfs-environ-cmdline" 'cat /proc/self/environ'
expect_block proc-cmdline-dump "procfs-environ-cmdline" 'head /proc/1234/cmdline'
expect_block proc-chained "procfs-environ-cmdline" 'true && tail -c 64 /proc/self/environ'
expect_block proc-redirect "procfs-environ-cmdline" 'cat < /proc/self/environ'
expect_block proc-sudo "procfs-environ-cmdline" 'sudo cat /proc/self/environ'
expect_block ssh-id-rsa "ssh-key-material" 'cat ~/.ssh/id_rsa'
expect_block ssh-config "ssh-key-material" 'cat /home/alice/.ssh/config'
expect_block git-c-ssh-dir "ssh-key-material" 'git -C ~/.ssh log'
expect_block pem-file "private-key-material" 'cat server.pem'
expect_block key-file "private-key-material" 'cat server.key'
expect_block netrc-store "netrc-password-store" 'cat ~/.netrc'
expect_block dotenv-file "dotenv-store" 'cat .env'
expect_block dotenv-local "dotenv-store" 'cat .env.production'
expect_block git-show-dotenv "dotenv-store" 'git -C /tmp/fake-repo show HEAD:.env'
expect_block ollama-usage-env "ollama-usage-env" 'cat ~/.ollama-usage-env'
expect_block auth-json "credential-store-file" 'cat auth.json'
expect_block token-json "credential-store-file" 'cat token.json'
expect_block cookies-db "credential-store-file" 'cat cookies.db'
expect_block gcloud-creds "credential-store-file" 'cat ~/.config/gcloud/application_default_credentials.json'
expect_block aws-creds "credential-store-file" 'cat ~/.aws/credentials'
expect_block bare-env "environment-dump" 'env'
expect_block env-pipeline "environment-pipeline" 'env | grep -i secret'
expect_block env-no-command "environment-dump" 'env FAKE_A=1 FAKE_B=2'
expect_block printenv "environment-dump" 'printenv'
expect_block printenv-arg "environment-dump" 'printenv HOME'
expect_block bare-set "environment-dump" 'set'
expect_block set-pipeline "environment-pipeline" 'set | grep SECRET'
expect_block bare-export "environment-dump" 'export'
expect_block export-pipeline "environment-pipeline" 'export | grep FAKE'
expect_block declare-x "environment-dump" 'declare -x'
expect_block declare-xp "environment-dump" 'declare -xp'
expect_block wrapped-env "interpreter-bypass" "nix-shell --run 'env'"
expect_block wrapped-set "interpreter-bypass" "sh -c 'set'"
expect_block ps-eww "ps-environ-column" 'ps eww'
expect_block ps-o-env "ps-environ-column" 'ps -eo pid,env,cmd'
expect_block ps-E "ps-environ-column" 'ps -E'
expect_block ps-bsd-e "ps-environ-column" 'ps auxwwE'
expect_block echo-token "secret-variable-expansion" "echo \"\$TOKEN\""
expect_block printf-api-key "secret-variable-expansion" "printf '%s' \"\$OPENAI_API_KEY\""
expect_block assign-secret-var "secret-variable-expansion" "FOO=\"\$GITHUB_TOKEN\""
expect_block env-accessor "interpreter-bypass" "python3 -c \"import os; print(os.environ['TOKEN'])\""
expect_block perl-env-accessor "interpreter-bypass" "perl -e 'print \$ENV{TOKEN}'"
expect_block url-userinfo "url-userinfo-credential" 'curl https://fake-user:fake-pass@host.invalid/'
expect_block shell-bypass-proc "interpreter-bypass" "sh -c 'cat /proc/self/environ'"
expect_block python-bypass-proc "interpreter-bypass" "python3 -c \"import os; print(open('/proc/1/environ').read())\""
expect_block shell-bypass-netrc "interpreter-bypass" "sh -c 'cat \"\$HOME/.netrc\"'"

# Encoded and chained references, detectable when decoding is indicated.
enc_proc=$(printf '%s' '/proc/self/environ' | base64 -w0)
enc_echo_token=$(printf '%s' "echo \$TOKEN" | base64 -w0)
expect_block base64-proc "encoded-sensitive-reference" "echo $enc_proc | base64 -d"
expect_block base64-echo-token "encoded-sensitive-reference" "echo $enc_echo_token | base64 -d"
expect_block percent-proc "encoded-sensitive-reference" 'cat %2Fproc%2Fself%2Fenviron'

# ---------------------------------------------------------------------------
# check-command: adversarial allow matrix (ordinary project commands)
# ---------------------------------------------------------------------------

expect_allow env-with-assignment 'env HOME=/tmp FOO=bar cmake --build build-check --parallel'
expect_allow env-with-command 'env PATH=/usr/bin:/bin sh -c '"'"'echo hi'"'"''
expect_allow export-assignment 'export CI=true'
expect_allow export-path "export PATH=\"/usr/bin:\$PATH\""
expect_allow set-pipefail 'set -euo pipefail'
expect_allow set-eo 'set -eo pipefail'
expect_allow declare-assignment 'declare -x FOO=1'
expect_allow grep-quoted-id-rsa 'grep -rn "id_rsa" src/'
expect_allow grep-quoted-token 'grep -rn "token" src/'
expect_allow grep-quoted-secret 'grep -rn "secret" docs/'
expect_allow grep-password 'grep -rn "password" .'
expect_allow cmake-build 'cmake --build build-check --parallel'
expect_allow ctest-run 'ctest --test-dir build-check --output-on-failure'
expect_allow git-status 'git status --short'
expect_allow git-log 'git log --oneline -5'
expect_allow git-grep 'git grep -n TOKEN -- src/'
expect_allow python-getcwd "python3 -c \"import os; print(os.getcwd())\""
expect_allow bash-safe-set "bash -c 'set -euo pipefail; cmake --build build'"
expect_allow nix-shell-build "nix-shell --run 'cmake -S . -B build-check'"
expect_allow ps-aux 'ps aux'
expect_allow ps-ef 'ps -ef'
expect_allow curl-plain-url 'curl https://host.invalid/api?page=2'
expect_allow cat-cmake 'cat CMakeLists.txt'
expect_allow make-test 'make test'
expect_allow export-and-build 'export FOO=bar && make test'
expect_allow shell-echo 'sh -c '"'"'echo hi'"'"''

# The JSON contract is stable and machine-readable.
out=$("$GUARD" check-command --command 'cat /proc/self/environ' || true)
printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["schema"]=="credential-guard/v1"; assert d["tool"]=="credential-guard"; assert d["verdict"]=="block"; assert d["reason"] in d["reasons"]; assert isinstance(d["reasons"], list)'

# check-command never echoes the command text or any secret value, even for
# allowed commands whose text embeds a fake secret-shaped word.
never_out=$("$GUARD" check-command --command "echo $FAKE_NEVER_ECHO" || true)
[[ "$never_out" != *"$FAKE_NEVER_ECHO"* ]] || {
    echo "test: check-command echoed the fake secret" >&2
    exit 1
}
blocked_never_out=$("$GUARD" check-command --command "cat /proc/self/environ; echo $FAKE_NEVER_ECHO" || true)
[[ "$blocked_never_out" != *"$FAKE_NEVER_ECHO"* && "$blocked_never_out" != *"/proc/self/environ"* ]] || {
    echo "test: check-command echoed command text or fake secret on block" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# check-command-stdin / check-path-stdin: bounded stdin, fail-closed, and the
# same allow/block verdict contract. Only fake strings are used.
# ---------------------------------------------------------------------------

# One MiB + 1 bytes forces the oversize branch.
python3 - > "$tmp/oversized.bin" <<'PY'
import sys
sys.stdout.buffer.write(b"x" * ((1 << 20) + 1))
PY
python3 - > "$tmp/invalid-utf8.bin" <<'PY'
import sys
sys.stdout.buffer.write(b"\xff\xfe\x00\x01")
PY

# check-command-stdin: allow for an ordinary project command, no input echo.
set +e
cmd_out=$(printf 'cmake --build build-check --parallel' | "$GUARD" check-command-stdin 2>"$tmp/err")
rc=$?
set -e
[[ $rc -eq 0 ]] || { echo "test: check-command-stdin allowed rc=$rc" >&2; exit 1; }
grep -q '"verdict":"allow"' <<<"$cmd_out" || { echo "test: stdin-command allow verdict" >&2; exit 1; }

# check-command-stdin: block a sensitive command; reason is stable.
set +e
cmd_out=$(printf 'cat /proc/self/environ' | "$GUARD" check-command-stdin 2>"$tmp/err")
rc=$?
set -e
[[ $rc -eq 1 ]] || { echo "test: stdin-command block rc=$rc" >&2; exit 1; }
if ! grep -q '"verdict":"block"' <<<"$cmd_out" || ! grep -q '"reason":"procfs-environ-cmdline"' <<<"$cmd_out"; then
    echo "test: stdin-command block reason" >&2
    exit 1
fi

# check-command-stdin: oversized and invalid UTF-8 both fail closed.
set +e
"$GUARD" check-command-stdin < "$tmp/oversized.bin" >"$tmp/oc" 2>/dev/null
rc=$?
set -e
[[ $rc -eq 1 ]] || { echo "test: stdin-command oversize rc=$rc" >&2; exit 1; }
grep -q '"reason":"oversized-input"' "$tmp/oc" || { echo "test: stdin-command oversize reason" >&2; exit 1; }
set +e
"$GUARD" check-command-stdin < "$tmp/invalid-utf8.bin" >"$tmp/oc" 2>/dev/null
rc=$?
set -e
[[ $rc -eq 1 ]] || { echo "test: stdin-command invalid-utf8 rc=$rc" >&2; exit 1; }
grep -q '"reason":"invalid-utf-8"' "$tmp/oc" || { echo "test: stdin-command invalid-utf8 reason" >&2; exit 1; }

# check-command-stdin never echoes the input, even when it is allow verdict
# and the input text embeds a fake secret-shaped word.
stdin_never="echo $FAKE_NEVER_ECHO"
never_stdin=$("$GUARD" check-command-stdin <<<"$stdin_never" || true)
[[ "$never_stdin" != *"$FAKE_NEVER_ECHO"* ]] || { echo "test: stdin-command echoed fake input" >&2; exit 1; }

# check-path-stdin: ordinary project paths are allowed, including a filename
# that merely contains the word "credential".
for p in 'src/CMakeLists.txt' 'src/credential-guard.py' 'build-check/test.log'; do
    set +e
    p_out=$(printf '%s' "$p" | "$GUARD" check-path-stdin 2>"$tmp/err")
    rc=$?
    set -e
    [[ $rc -eq 0 ]] || { echo "test: path allow rc=$rc for $p" >&2; exit 1; }
    grep -q '"verdict":"allow"' <<<"$p_out" || { echo "test: path allow verdict for $p" >&2; exit 1; }
    [[ "$p_out" != *"$p"* ]] || { echo "test: path-stdin echoed path $p" >&2; exit 1; }
done

# check-path-stdin: every sensitive category fails closed with a stable reason.
path_block() {
    local label=$1 expected=$2 p=$3 rc out
    set +e
    out=$(printf '%s' "$p" | "$GUARD" check-path-stdin 2>"$tmp/err")
    rc=$?
    set -e
    [[ $rc -eq 1 ]] || { echo "test: path block '$label' rc=$rc" >&2; exit 1; }
    grep -q '"verdict":"block"' <<<"$out" || { echo "test: path block '$label' verdict" >&2; exit 1; }
    grep -q "\"reason\":\"$expected\"" <<<"$out" || { echo "test: path block '$label' reason $out" >&2; exit 1; }
    [[ "$out" != *"$p"* ]] || { echo "test: path block '$label' echoed input" >&2; exit 1; }
}
path_block proc-environ 'procfs-environ-cmdline' '/proc/self/environ'
path_block proc-cmdline 'procfs-environ-cmdline' '/proc/1234/cmdline'
path_block ssh-key 'ssh-key-material' '/home/fake/.ssh/id_ed25519'
path_block netrc 'netrc-password-store' "$HOME-FAKE/.netrc"
path_block dotenv 'dotenv-store' '/tmp/FAKE-REPO/.env'
path_block credential-store 'credential-store-file' 'auth.json'
path_block credential-json 'credential-store-file' 'token.json'
path_block ollama-usage 'ollama-usage-env' '/home/fake/.ollama-usage-env'
path_block private-key 'private-key-material' 'server.key'

# check-path-stdin: oversized, invalid UTF-8, empty, newline, and NUL are all
# fail-closed invalid inputs.
for label in oversized invalid utf8empty newline nul; do
    set +e
    case $label in
        oversized) python3 -c 'import sys;sys.stdout.buffer.write(b"y"*((1<<20)+1))' | "$GUARD" check-path-stdin >"$tmp/pc" 2>/dev/null;;
        invalid) "$GUARD" check-path-stdin < "$tmp/invalid-utf8.bin" >"$tmp/pc" 2>/dev/null;;
        utf8empty) printf '' | "$GUARD" check-path-stdin >"$tmp/pc" 2>/dev/null;;
        newline) printf 'src/a.c\n' | "$GUARD" check-path-stdin >"$tmp/pc" 2>/dev/null;;
        nul) printf 'src\x00a.c' | "$GUARD" check-path-stdin >"$tmp/pc" 2>/dev/null;;
    esac
    rc=$?
    set -e
    [[ $rc -eq 1 ]] || { echo "test: path invalid '$label' rc=$rc" >&2; exit 1; }
    grep -q '"verdict":"block"' "$tmp/pc" || { echo "test: path invalid '$label' verdict" >&2; exit 1; }
done

# ---------------------------------------------------------------------------
# redact: fake values must vanish; diagnostics must survive; idempotent.
# ---------------------------------------------------------------------------

cat > "$tmp/fixture.txt" <<EOF
# diagnostic header: build dir /tmp/fake-build-123
export FAKE_TOKEN_AB12CD34=$FAKE_SECRET_VAL
FAKE_API_KEY_001122=$FAKE_API_VAL
token=$FAKE_SECRET_VAL
Bearer $FAKE_BEARER_1
Authorization: Bearer $FAKE_BEARER_2
https://fakeuser:$FAKE_USERPASS@host.invalid/api?api_key=$FAKE_QUERY_KEY&page=2&limit=10
{"user": "alice", "api_key": "$FAKE_JSON_KEY", "note": "keep me"}
X-Api-Key: $FAKE_HEADER_KEY
password: $FAKE_PASSWD
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJmYWtlIn0.$FAKE_JWT_SIG
-----BEGIN PRIVATE KEY-----
$FAKE_E_MATERIAL
$FAKE_MORE_MATERIAL
-----END PRIVATE KEY-----
-----BEGIN PRIVATE KEY-----$FAKE_SINGLE_LINE_MATERIAL-----END PRIVATE KEY-----
# diagnostic tail: exit code 0
EOF

"$GUARD" redact < "$tmp/fixture.txt" > "$tmp/redacted.log" 2>"$tmp/redact-err.txt"
[[ ! -s "$tmp/redact-err.txt" ]] || {
    echo "test: redact wrote to stderr: $(cat "$tmp/redact-err.txt")" >&2
    exit 1
}
[[ -s "$tmp/redacted.log" ]] || {
    echo "test: redact produced empty output" >&2
    exit 1
}

# Every fake value must be absent from the masked log file.
for fake in \
    "$FAKE_SECRET_VAL" "$FAKE_API_VAL" "$FAKE_BEARER_1" "$FAKE_BEARER_2" \
    "$FAKE_USERPASS" "$FAKE_QUERY_KEY" "$FAKE_JSON_KEY" "$FAKE_HEADER_KEY" \
    "$FAKE_PASSWD" "$FAKE_JWT_SIG" "$FAKE_E_MATERIAL" "$FAKE_MORE_MATERIAL" \
    "$FAKE_SINGLE_LINE_MATERIAL"
do
    if grep -Fq "$fake" "$tmp/redacted.log"; then
        echo "test: redact leaked fake value: $fake" >&2
        exit 1
    fi
done

# Surrounding diagnostics must remain intact.
grep -Fq '# diagnostic header: build dir /tmp/fake-build-123' "$tmp/redacted.log" || {
    echo "test: redact dropped the header diagnostic" >&2
    exit 1
}
grep -Fq '# diagnostic tail: exit code 0' "$tmp/redacted.log" || {
    echo "test: redact dropped the tail diagnostic" >&2
    exit 1
}
grep -Fq 'page=2&limit=10' "$tmp/redacted.log" || {
    echo "test: redact dropped non-secret URL query diagnostics" >&2
    exit 1
}
grep -Fq '"user": "alice"' "$tmp/redacted.log" || {
    echo "test: redact dropped JSON user diagnostic" >&2
    exit 1
}
grep -Fq '"note": "keep me"' "$tmp/redacted.log" || {
    echo "test: redact dropped JSON note diagnostic" >&2
    exit 1
}
grep -Fq 'FAKE_TOKEN_AB12CD34=[REDACTED]' "$tmp/redacted.log" || {
    echo "test: redact did not mask the token assignment value" >&2
    exit 1
}
grep -Fq 'Authorization: [REDACTED]' "$tmp/redacted.log" || {
    echo "test: redact did not mask the Authorization header" >&2
    exit 1
}
grep -Fq 'X-Api-Key: [REDACTED]' "$tmp/redacted.log" || {
    echo "test: redact did not mask the X-Api-Key header" >&2
    exit 1
}
grep -Fq 'password: [REDACTED]' "$tmp/redacted.log" || {
    echo "test: redact did not mask the password value" >&2
    exit 1
}
grep -Fq '[REDACTED-JWT]' "$tmp/redacted.log" || {
    echo "test: redact did not mask the JWT" >&2
    exit 1
}
grep -Fq '[REDACTED-PRIVATE-KEY-BLOCK]' "$tmp/redacted.log" || {
    echo "test: redact did not mask the private-key block" >&2
    exit 1
}
grep -Fq '[REDACTED-PRIVATE-KEY-BLOCK-END]' "$tmp/redacted.log" || {
    echo "test: redact did not emit the private-key block end marker" >&2
    exit 1
}

# Idempotency: redacting the redacted stream changes nothing.
"$GUARD" redact < "$tmp/redacted.log" > "$tmp/redacted-twice.log" 2>/dev/null
cmp -s "$tmp/redacted.log" "$tmp/redacted-twice.log" || {
    echo "test: redact is not idempotent" >&2
    exit 1
}
for fake in "$FAKE_SECRET_VAL" "$FAKE_JWT_SIG" "$FAKE_E_MATERIAL"; do
    if grep -Fq "$fake" "$tmp/redacted-twice.log"; then
        echo "test: redact leaked a fake value on stdout" >&2
        exit 1
    fi
done

# Bounded large-line handling: an oversized line is still masked quickly.
python3 - "$tmp/bigline.txt" <<'PY'
import sys
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    stream.write("a" * 200000)
PY
printf ' TOKEN=FAKE_BIG_SECRET_987654321\n' >> "$tmp/bigline.txt"
"$GUARD" redact --max-line-bytes 64 < "$tmp/bigline.txt" > "$tmp/bigout.txt"
grep -Fq 'TOKEN=[REDACTED]' "$tmp/bigout.txt" || {
    echo "test: redact did not mask the oversized-line assignment" >&2
    exit 1
}
if grep -Fq 'FAKE_BIG_SECRET_987654321' "$tmp/bigout.txt"; then
    echo "test: redact leaked the oversized-line fake value" >&2
    exit 1
fi

# Runtime path reconstruction and encoded references fail closed without
# globally rejecting ordinary build substitutions or source globs.
check_stdin_verdict() {
    local expected=$1 reason=$2 command=$3 out rc
    set +e
    out=$(printf '%s' "$command" | "$GUARD" check-command-stdin 2>/dev/null)
    rc=$?
    set -e
    if [[ $expected == block ]]; then
        [[ $rc -eq 1 ]] || { echo "test: reconstructed command allowed" >&2; exit 1; }
        grep -Fq "\"reason\":\"$reason\"" <<<"$out" || { echo "test: reconstructed command wrong reason" >&2; exit 1; }
    else
        [[ $rc -eq 0 ]] || { echo "test: ordinary build command blocked" >&2; exit 1; }
        grep -Fq '"verdict":"allow"' <<<"$out" || { echo "test: ordinary command verdict" >&2; exit 1; }
    fi
}
check_stdin_verdict block dynamic-sensitive-path "cat /proc/self/env\$(printf 'ir')on"
check_stdin_verdict block dynamic-sensitive-path "cat /proc/self/env\$'iron'"
check_stdin_verdict block dynamic-sensitive-path 'cat /proc/self/enviro?'
check_stdin_verdict block ssh-key-material "cat /home/fake/.ss\$'h'/id_rsa"
check_stdin_verdict block dynamic-sensitive-path 'cat server.ke?'
check_stdin_verdict block encoded-sensitive-reference 'echo 2f70726f632f73656c662f656e7669726f6e | xxd -r -p | xargs cat'
check_stdin_verdict block encoded-sensitive-reference 'printf %b '\''\x2fproc\x2fself\x2fenviron'\'''
check_stdin_verdict allow ok "\$CC -c src/main.c"
check_stdin_verdict allow ok "make -j\$(nproc)"
check_stdin_verdict allow ok 'printf "%s\n" src/*.c'
check_stdin_verdict allow ok 'rg "token.*" src/'

# Comma-separated assignment tails and text after a private-key END marker
# remain covered by ordinary masking.
cat > "$tmp/redact-edge.log" <<'EOF'
FAKE_TOKEN_AB12CD34=alpha,FAKE_COMMA_TAIL_87654321
-----BEGIN PRIVATE KEY-----FAKE_KEY_MATERIAL-----END PRIVATE KEY----- FAKE_TOKEN_AB12CD34=FAKE_AFTER_END_87654321
-----BEGIN PRIVATE KEY-----
FAKE_MULTILINE_KEY
-----END PRIVATE KEY----- Authorization: Bearer FAKE_AFTER_MULTI_87654321
EOF
"$GUARD" redact < "$tmp/redact-edge.log" > "$tmp/redact-edge.out"
for fake in FAKE_COMMA_TAIL_87654321 FAKE_AFTER_END_87654321 FAKE_MULTILINE_KEY FAKE_AFTER_MULTI_87654321; do
    ! grep -Fq "$fake" "$tmp/redact-edge.out" || { echo "test: edge redaction leaked $fake" >&2; exit 1; }
done
grep -Fq 'FAKE_TOKEN_AB12CD34=[REDACTED]' "$tmp/redact-edge.out" || { echo "test: comma assignment not masked" >&2; exit 1; }
grep -Fq 'Authorization: [REDACTED]' "$tmp/redact-edge.out" || { echo "test: trailing key text not ordinarily masked" >&2; exit 1; }

# Oversized URL lines receive the same userinfo and query masking.
python3 - "$tmp/big-url.log" <<'PY'
import sys
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    stream.write("x" * ((1 << 20) + 32))
    stream.write(" https://fake-user:FAKE_URL_PASSWORD_87654321@host.invalid/path?api_key=FAKE_QUERY_SECRET_87654321&limit=10\n")
PY
"$GUARD" redact < "$tmp/big-url.log" > "$tmp/big-url.out"
for fake in FAKE_URL_PASSWORD_87654321 FAKE_QUERY_SECRET_87654321; do
    ! grep -Fq "$fake" "$tmp/big-url.out" || { echo "test: oversized URL leaked $fake" >&2; exit 1; }
done
grep -Fq 'https://[REDACTED]@host.invalid/path?api_key=[REDACTED]&limit=10' "$tmp/big-url.out" || { echo "test: oversized URL masking incomplete" >&2; exit 1; }
"$GUARD" redact < "$tmp/big-url.out" > "$tmp/big-url-twice.out"
cmp -s "$tmp/big-url.out" "$tmp/big-url-twice.out" || { echo "test: oversized URL redaction not idempotent" >&2; exit 1; }

# Interpreter environment accessors/dumps and AWS access-key forms fail
# closed, while a long non-wrapper interpreter argv remains linear/allowed.
check_stdin_verdict block interpreter-bypass "python3 -c 'import os; print(os.environ)'"
check_stdin_verdict block interpreter-bypass "python3 -c 'import os; print(dict(os.environ))'"
check_stdin_verdict block interpreter-bypass "python3 -c 'import os; print(os.environ.items())'"
check_stdin_verdict block interpreter-bypass "node -e 'console.log(JSON.stringify(process.env))'"
check_stdin_verdict block interpreter-bypass "node -e 'console.log({...process.env})'"
check_stdin_verdict block interpreter-bypass "python3 -c \"import os; os.system('printenv')\""
check_stdin_verdict block interpreter-bypass "sh -c'env'"
check_stdin_verdict block interpreter-bypass "python3 -c 'import os; print(os.environ.get(\"GITHUB_TOKEN\"))'"
check_stdin_verdict block interpreter-bypass "node -e 'console.log(process.env.TOKEN)'"
check_stdin_verdict block interpreter-bypass "perl -e 'print %ENV'"
check_stdin_verdict block interpreter-bypass "sh -c 'env && ls'"
check_stdin_verdict block secret-variable-expansion "echo \$AWS_ACCESS_KEY_ID"
long_interpreter=$(python3 - <<'PY'
print("sh " + "x " * 3000)
PY
)
check_stdin_verdict allow ok "$long_interpreter"

printf '%s\n' 'AWS_ACCESS_KEY_ID=FAKE_AWS_ACCESS_87654321' \
    'https://host.invalid/?AWSAccessKeyId=FAKE_AWS_QUERY_87654321' > "$tmp/aws-redact.log"
"$GUARD" redact < "$tmp/aws-redact.log" > "$tmp/aws-redact.out"
! grep -Fq 'FAKE_AWS_' "$tmp/aws-redact.out" || { echo "test: AWS access key leaked" >&2; exit 1; }

# Direct path classification resolves symlinks without reading their targets.
mkdir -p "$tmp/path-fixture"
: > "$tmp/path-fixture/auth.json"
: > "$tmp/path-fixture/ordinary.txt"
ln -s "$tmp/path-fixture/auth.json" "$tmp/sensitive-link"
ln -s "$tmp/path-fixture/ordinary.txt" "$tmp/ordinary-link"
set +e
printf '%s' "$tmp/sensitive-link" | "$GUARD" check-path-stdin > "$tmp/path-link.out" 2>/dev/null
rc=$?
set -e
[[ $rc -eq 1 ]] || { echo "test: sensitive symlink path allowed" >&2; exit 1; }
grep -Fq '"reason":"credential-store-file"' "$tmp/path-link.out" || { echo "test: sensitive symlink wrong reason" >&2; exit 1; }
printf '%s' "$tmp/ordinary-link" | "$GUARD" check-path-stdin > "$tmp/path-link.out"
grep -Fq '"verdict":"allow"' "$tmp/path-link.out" || { echo "test: ordinary symlink path blocked" >&2; exit 1; }
check_stdin_verdict block credential-store-file "cat $tmp/sensitive-link"
check_stdin_verdict allow ok "cat $tmp/ordinary-link"

# One-process JSON redaction preserves shape/non-string leaves and fails
# closed on malformed or bounded-resource violations.
python3 - "$tmp/json-input.json" <<'PY'
import json, sys
json.dump({"content": [{"text": "TOKEN=FAKE_JSON_SECRET_87654321"}],
           "details": {"nested": [7, True, None, "Bearer FAKE_JSON_BEARER_87654321"]}},
          open(sys.argv[1], "w", encoding="utf-8"))
PY
"$GUARD" redact-json-stdin < "$tmp/json-input.json" > "$tmp/json-output.json"
python3 - "$tmp/json-output.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value["content"][0]["text"] == "TOKEN=[REDACTED]"
assert value["details"]["nested"][:3] == [7, True, None]
assert value["details"]["nested"][3] == "Bearer [REDACTED]"
PY
! grep -Fq 'FAKE_JSON_' "$tmp/json-output.json" || { echo "test: JSON redaction leaked fake value" >&2; exit 1; }

printf '{bad json' > "$tmp/json-bad"
if "$GUARD" redact-json-stdin < "$tmp/json-bad" > /dev/null 2>&1; then
    echo "test: malformed JSON did not fail closed" >&2
    exit 1
fi
python3 - "$tmp/json-deep" "$tmp/json-nodes" "$tmp/json-large" <<'PY'
import json, sys
value = "leaf"
for _ in range(66):
    value = [value]
json.dump(value, open(sys.argv[1], "w", encoding="utf-8"))
json.dump([0] * 100001, open(sys.argv[2], "w", encoding="utf-8"))
with open(sys.argv[3], "w", encoding="utf-8") as stream:
    stream.write('"' + ('x' * ((8 << 20) + 1)) + '"')
PY
for bounded in "$tmp/json-deep" "$tmp/json-nodes" "$tmp/json-large"; do
    if "$GUARD" redact-json-stdin < "$bounded" > /dev/null 2>&1; then
        echo "test: bounded JSON violation did not fail closed" >&2
        exit 1
    fi
done

echo "test: credential-guard adversarial checks passed"
