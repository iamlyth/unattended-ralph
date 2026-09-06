#!/usr/bin/env python3
"""Product-neutral credential guard: fail-closed command classification and
streaming secret redaction.

Two independent capabilities, exposed both as a CLI and as importable library
functions:

  1. ``check-command --command <text>``: classify a shell command line as
   allowed or blocked without ever echoing its contents. The classification is
   fail-closed: command/reads of ``/proc/*/environ,cmdline``, auth/token/
   cookie stores, ``.env`` and ``.ollama-usage-env`` files, SSH/private-key/
   .pem/.key/.netrc credential stores, bare ``env|printenv|set|export|declare
   -x`` dumps, ``env | ...`` pipelines, ``ps`` environment columns,
   secret-variable expansions and echoes, URL userinfo credentials, encoded
   sensitive references (base64/percent/hex), shell/interpreter bypasses, and
   runtime reconstruction of sensitive paths are blocked. Runtime
   reconstruction means the literal sensitive path is *not* present as text:
   it is spliced with ANSI-C ``$'...'`` quoting, command substitutions or
   backticks, glob characters (``?*[]``), or split stems (``/proc/self/env
   'iron'``, ``.en[v]``, ``auth.jso[n]``); those are reported with the stable
   reason ``dynamic-sensitive-path``.
   ``env NAME=value command``, ``export NAME=value``, ``set -euo pipefail``,
   and ordinary project grep/build commands are allowed.

   Output is a single machine-readable JSON line on stdout; the command text
   and any secret values are never echoed.

2. ``redact``: a streaming stdin/stdout filter that masks fake/real-shaped
   credential assignments (including comma-continued unquoted values), API
   keys, tokens, cookies, Bearer/Basic auth, JWTs, private-key blocks, URL
   userinfo/query secrets, and same-line trailing text after a private-key
   END marker, while preserving surrounding diagnostics. The filter is
   idempotent, linear/bounded even on oversized lines (URL userinfo and token
   query parameters are still masked there), and never writes the original
   secret material anywhere (stdout carries only the masked stream; stderr is
   reserved for usage errors).

Library API::

    # The tool ships as scripts/credential-guard.py (dash) for the CLI; load it
    # as a library by path or import it under its snake_case alias:
    import importlib.util
    spec = importlib.util.spec_from_file_location("credential_guard", "scripts/credential-guard.py")
    credential_guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(credential_guard)

    verdict = credential_guard.classify_command("cat /proc/self/environ")
    credential_guard.redact_stream(sys.stdin, sys.stdout)

The guard is product-neutral and dependency-free (Python standard library
only).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import shlex
import sys
import urllib.parse
from typing import BinaryIO, List, Optional, Tuple

TOOL = "credential-guard"
SCHEMA = "credential-guard/v1"
VERSION = "1"

# ---------------------------------------------------------------------------
# Sensitive-path patterns for fail-closed classification.
#
# Every pattern keeps word-ish boundaries so ordinary project words ("token",
# "secret", "key") used as grep patterns or identifiers do not trip the guard
# unless they are path-like or shell-expansion-like.
# ---------------------------------------------------------------------------

_PROC_RE = re.compile(r"/proc/[^\s\"'`;|&()<>]+/(environ|cmdline)")
# B1 security review: the openai-codex credential descriptor is referenced
# only through ``/proc/self/fd/N`` (or ``/proc/<pid>/fd/N``); a model tool
# that dereferences that path in-process could read the credential memfd, so
# every ``/proc/.../fd`` reference is blocked in commands and paths (defense
# in depth; Landlock already denies ``/proc`` to subprocess tools).
_PROC_FD_RE = re.compile(r"/proc/[^\s\"'`;|&()<>]+/fd(?:/\d+)?")
_OLLAMA_RE = re.compile(r"(?<![A-Za-z0-9_])\.ollama-usage-env(?![A-Za-z0-9_])")
_SSH_DIR_RE = re.compile(r"(?<![A-Za-z0-9_])\.ssh(?=[\s\"'`;/]|$)")
_SSH_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:id_rsa|id_ed25519|id_ecdsa|id_dsa|id_ed448)(?![A-Za-z0-9_])"
)
_PRIVKEY_EXT_RE = re.compile(r"\.(?:pem|key|p12|pfx|ppk|kdbx|jks|keystore)(?![A-Za-z0-9_])")
_NETRC_RE = re.compile(
    r"(?<![A-Za-z0-9_])\.(?:netrc|pgpass|npmrc|git-credentials)(?![A-Za-z0-9_])"
)
_DOTENV_RE = re.compile(r"\.env(?:\.[A-Za-z0-9_-]+)?(?![A-Za-z0-9_])")
_CRED_STORE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:auth|token|tokens|cookie|cookies|credential|credentials|"
    r"secret|secrets)\.(?:json|txt|toml|yml|yaml|ini|conf|cfg|db|sqlite|sqlite3|kdbx)"
    r"(?![A-Za-z0-9_])"
)
_CREDENTIALS_PATH_RE = re.compile(r"(?:^|/)(?:\.\./)*credentials(?![A-Za-z0-9_])")

_PATH_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("procfs-environ-cmdline", _PROC_RE),
    ("procfs-fd", _PROC_FD_RE),
    ("credential-store-file", _CRED_STORE_RE),
    ("credential-store-file", _CREDENTIALS_PATH_RE),
    ("dotenv-store", _DOTENV_RE),
    ("ollama-usage-env", _OLLAMA_RE),
    ("ssh-key-material", _SSH_DIR_RE),
    ("ssh-key-material", _SSH_KEY_RE),
    ("private-key-material", _PRIVKEY_EXT_RE),
    ("netrc-password-store", _NETRC_RE),
]

# ---------------------------------------------------------------------------
# Runtime-reconstruction detection (stable reason ``dynamic-sensitive-path``).
#
# The literal sensitive path is *absent* from the text; the shell would splice
# it at runtime from quotes, substitutions/backticks, globs, or split stems.
# Ordinary builds still use $CC, $(nproc), and source globs (src/*.c); only
# sensitive stems near /proc/.../env|cmd, .ssh/.ss, .netrc/.netr, .env,
# auth/credential/token/key/cookie store names, or a decode/printf context
# are blocked.
# ---------------------------------------------------------------------------

# ``/proc/<pid>/...`` component whose env/cmd stem is cut short and globbed
# (e.g. ``/proc/self/enviro?``, ``/proc/self/env*``, ``/proc/123/cmdli?e``).
_PROC_TAIL_RE = re.compile(
    r"/proc/[^\s\"'`;|&<>()]+/(?:env|cmd)[A-Za-z0-9_]*[?*\[][^\s\"'`;|&<>()]*"
)
_PROC_SUBSTITUTION_RE = re.compile(
    r"/proc/[^\s\"'`;|&()<>]+/(?:env|cmd)[^\s;|&<>]{0,64}(?:\$\(|`|\$')"
)
# ``/proc/.../fd`` cut short by a glob (``/proc/self/fd/3?``, ``/proc/self/fd*``)
# or spliced by a substitution (``/proc/self/fd/$(echo 3)``).
_PROC_FD_TAIL_RE = re.compile(
    r"/proc/[^\s\"'`;|&<>()]+/fd(?:/\d*)?[?*\[][^\s\"'`;|&<>()]*"
)
_PROC_FD_SUBSTITUTION_RE = re.compile(
    r"/proc/[^\s\"'`;|&<>()]+/fd[^\s;|&<>]{0,64}(?:\$\(|`|\$')"
)
# Credential-store file whose extension is globbed (``auth.jso[n]`` is caught
# by the de-glued scan; ``token.*``/``token.jso?``/``auth.[js]on`` land here).
_STORE_EXT_GLOB_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:auth|token|tokens|cookie|cookies|credential|credentials|secret|secrets)"
    r"\.[A-Za-z0-9_-]*[?*\[][^\s\"'`;|&<>()]*"
)
# Dotenv name cut short by a glob (``.env?``, ``.env*``, ``.env[0-9]``).
_DOTENV_TAIL_GLOB_RE = re.compile(
    r"\.env(?:\.[A-Za-z0-9_-]+)*[?*\[][^\s\"'`;|&<>()]*"
)
# ``.ssh``/``.netrc`` components continued by a glob (``.ss?`` -> .ssh,
# ``.netr?`` -> .netrc, ``.ssh*``).
_SSH_TAIL_GLOB_RE = re.compile(r"\.ss(?:h)?[?*\[][^\s\"'`;|&<>()]*")
_NETRC_TAIL_GLOB_RE = re.compile(r"\.netr(?:c)?[?*\[][^\s\"'`;|&<>()]*")
# Private-key extension cut short by a glob (``server.key*``, ``*.pem`` is
# already literal-sensitive; ``server.ke?`` lands here).
_PRIVKEY_TAIL_GLOB_RE = re.compile(
    r"\.(?:p(?:e(?:m)?)?|k(?:e(?:y)?)?|p1(?:2)?|pf(?:x)?|pp(?:k)?|"
    r"kd(?:b(?:x)?)?|jk(?:s)?|keyst(?:o(?:r(?:e)?)?)?)[?*\[]"
    r"[^\s\"'`;|&<>()]*"
)
_OLLAMA_TAIL_GLOB_RE = re.compile(r"\.ollama-usage-env[?*\[][^\s\"'`;|&<>()]*")
_CREDENTIALS_TAIL_GLOB_RE = re.compile(
    r"(?:^|/)(?:\.\./)*credentials[?*\[][^\s\"'`;|&<>()]*"
)

_DYNAMIC_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("procfs-environ-cmdline", _PROC_TAIL_RE),
    ("procfs-environ-cmdline", _PROC_SUBSTITUTION_RE),
    ("procfs-fd", _PROC_FD_TAIL_RE),
    ("procfs-fd", _PROC_FD_SUBSTITUTION_RE),
    ("credential-store-file", _STORE_EXT_GLOB_RE),
    ("credential-store-file", _CREDENTIALS_TAIL_GLOB_RE),
    ("dotenv-store", _DOTENV_TAIL_GLOB_RE),
    ("ollama-usage-env", _OLLAMA_TAIL_GLOB_RE),
    ("ssh-key-material", _SSH_TAIL_GLOB_RE),
    ("netrc-password-store", _NETRC_TAIL_GLOB_RE),
    ("private-key-material", _PRIVKEY_TAIL_GLOB_RE),
]

# Secret-shaped environment-variable suffixes. The variable name must end with
# the suffix or continue with an underscore, so plain project variables like
# $PATH, $HOME, $USER, $AUTHORS, $SESSION_TIME do not match.
_SECRET_SUFFIX = (
    r"(?:TOKEN|TOKENS|SECRET|SECRETS|PASSWORD|PASSWD|PASSW|API[_-]?KEY|APIKEY|"
    r"APITOKEN|AUTH[_-]?TOKEN|AUTH[_-]?KEY|AUTHORIZATION|AUTH|COOKIE|COOKIES|CREDENTIAL|"
    r"CREDENTIALS|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET|ACCESS[_-]?TOKEN|"
    r"ACCESS[_-]?KEY(?:[_-]?ID)?|AWSACCESSKEYID|"
    r"REFRESH[_-]?TOKEN|SESSION[_-]?ID|SESSION[_-]?TOKEN|SESSIONKEY|SIGNATURE|"
    r"SIGNING[_-]?KEY|MASTER[_-]?KEY|OAUTH|OLLAMA[_-]?AUTH|OPENAI[_-]?API[_-]?KEY|"
    r"ANTHROPIC[_-]?API[_-]?KEY|GITHUB[_-]?TOKEN|GH[_-]?TOKEN|PGPASSWORD|"
    r"DB[_-]?PASSWORD)"
)

_SECRET_SUFFIX_RE = re.compile(_SECRET_SUFFIX + r"(?:$|_)", re.IGNORECASE)
_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _secret_var_reasons(text: str) -> List[str]:
    """Secret-shaped shell variable references ($TOKEN, ${API_KEY}, ...)."""
    for match in _VAR_REF_RE.finditer(text):
        if _SECRET_SUFFIX_RE.search(match.group(1)):
            return ["secret-variable-expansion"]
    return []


# Interpreter accessor forms referencing a secret by name without a shell
# expansion: os.environ["TOKEN"], getenv("TOKEN"), $ENV{TOKEN}, environ[TOKEN].
_SECRET_ACCESSOR_RE = re.compile(
    r"(?:os\.)?environ\s*\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\]"
    r"|(?:os\.)?environ\.get\s*\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"
    r"|\bgetenv\s*\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"
    r"|\bENV\s*[\[{]\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s*[\]}]"
    r"|\bprocess\.env\.([A-Za-z_][A-Za-z0-9_]*)"
    r"|\bprocess\.env\s*\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\]"
)
_ENV_OBJECT_DUMP_RE = re.compile(r"(?:\bos\.environ|\bprocess\.env|%ENV\b)")
_INNER_ENV_DUMP_RE = re.compile(
    r"(?:os\.system|subprocess\.(?:run|call|check_output)|child_process\.[A-Za-z_]+)"
    r"\s*\([^\n]{0,512}?[\"'](?:env|printenv)[\"']"
)


def _secret_accessor_reasons(text: str) -> List[str]:
    reasons: List[str] = []
    for match in _SECRET_ACCESSOR_RE.finditer(text):
        for group in match.groups():
            if group and _SECRET_SUFFIX_RE.search(group):
                reasons.append("secret-variable-expansion")
                break
    return reasons

_URL_USERINFO_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]{0,31}://)"
    r"(?P<userinfo>[^/\s@]+)@"
)

# Interpreter/one-shot wrappers: code passed via -c/-e/-r or a --run wrapper.
# Only relevant when the wrapped content also trips another sensitive rule.
_INTERPRETER_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:sh|bash|zsh|dash|ksh|python[23]?|pypy[23]?|"
    r"perl|ruby|node|php|nix-shell|su|runuser)(?![A-Za-z0-9_])"
)
_INTERPRETER_FLAG_RE = re.compile(
    r"(?:^|\s)(?:-c(?=\s|[\"']|\S)|--\s*c(?=\s|[\"']|\S)|"
    r"-e(?=\s|[\"']|\S)|-r(?=\s|[\"']|\S)|--run\b|--command\b)"
)
# A wrapped bare dump: 'env', 'set', 'export', ... inside a -c/--run argument.
_DUMP_WRAPPER_RE = re.compile(
    r"(?:--run\b|--command\b|-c)\s*[\"']?\s*(?:"
    r"(?:env|printenv|set|export)(?=\s*(?:[\"']|&&|\|\||[;&|]|$))"
    r"|declare\s+-x(?=\s*(?:[\"']|&&|\|\||[;&|]|$)))"
)
_ENV_PIPE_RE = re.compile(r"(?<![A-Za-z0-9_])(?:env|printenv|set|export)\s*\|")

# Base64/percent-encoded references: blocked only when the decoded content is
# itself sensitive and the command line indicates decoding is taking place.
_DECODE_INDICATOR_RE = re.compile(
    r"\b(?:base64|b64|decode|unhexlify|fromhex|xxd|openssl)\b|"
    r"(?<![A-Za-z0-9_])-(?:d|D)(?![A-Za-z0-9_])"
)
_B64_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_-]{8,}={0,2}")
_PERCENT_RE = re.compile(r"%[0-9A-Fa-f]{2}")

# Hex-encoded references: \xNN/octal escapes (printf %b, echo -e, ANSI
# $'...') and raw hex dumps piped through xxd -r/-p or unhex. As with base64
# and percent, only decoded when a decode/printf indicator is present.
_HEX_ESC_RE = re.compile(r"\\x[0-9A-Fa-f]{2}|\\[0-7]{1,3}")
_XXD_UNHEX_RE = re.compile(r"\bxxd\b[^\n;|&]*\s-[^ ]*[rp]|\bunhex(?:lify)?\b")
_HEX_RUN_RE = re.compile(r"[0-9A-Fa-f]{32,}")

# ---------------------------------------------------------------------------
# Redaction patterns. Masks are fixed tokens, so redaction is idempotent and
# never re-introduces the original secret material.
# ---------------------------------------------------------------------------

REDACTED = "[REDACTED]"
REDACTED_JWT = "[REDACTED-JWT]"
KEY_BLOCK_START = "[REDACTED-PRIVATE-KEY-BLOCK]"
KEY_BLOCK_END = "[REDACTED-PRIVATE-KEY-BLOCK-END]"

_KEY_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_KEY_END_RE = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")

_ASSIGN_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(?P<name>[\"']?[A-Za-z_][A-Za-z0-9_.-]{0,127}[\"']?)(?P<op>\s*[:=]\s*)"
    r"(?P<val>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\s;\"']+)"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)(?P<prefix>\bAuthorization\s*:\s*)(?:(?:Bearer|Basic)\s+)?\S+"
)
_BEARER_RE = re.compile(r"\b(?P<sch>(?:Bearer|Basic))\s+(?P<tok>[A-Za-z0-9._~+/-]{6,}={0,2})")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b")
_URL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])[a-zA-Z][a-zA-Z0-9+.-]{0,31}://[^\s<>\"']+"
)
_QUERY_SECRET_NAME_RE = re.compile(
    r"(?i)^(?:key|api[_-]?key|apikey|access[_-]?token|access[_-]?key(?:[_-]?id)?|"
    r"awsaccesskeyid|refresh[_-]?token|"
    r"client[_-]?secret|token|secret|password|passwd|auth|authorization|"
    r"cookie|session|session[_-]?id|signature|sig|id[_-]?token|code|bearer|"
    r"x[_-]?api[_-]?key)$"
)

_MAX_LINE_BYTES_DEFAULT = 1 << 20  # 1 MiB per-line bound; URL/userinfo/query masking stays linear above this


# ---------------------------------------------------------------------------
# check-command classification
# ---------------------------------------------------------------------------

def _quoted_spans(text: str) -> List[Tuple[int, int, str]]:
    """Return (start, end, quote_char) spans of quoted regions."""
    spans: List[Tuple[int, int, str]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            quote = ch
            start = i
            i += 1
            while i < n:
                if text[i] == "\\" and quote == '"' and i + 1 < n:
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            spans.append((start, i, quote))
            continue
        i += 1
    return spans


def _split_segments(text: str) -> List[Tuple[int, int, str]]:
    """Split a shell command into pipeline/sequence segments with offsets."""
    parts = re.split(r"(\|\|?|&&|;|&|\n)", text)
    segments: List[Tuple[int, int, str]] = []
    offset = 0
    for part in parts:
        stripped = part.strip()
        if stripped:
            start = text.find(stripped, offset)
            segments.append((start, start + len(stripped), stripped))
        offset += len(part)
    return segments


def _first_word(segment: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_./+-]+)", segment)
    return match.group(1) if match else ""


_SEARCH_TOOLS = {"grep", "rg", "ag", "egrep", "fgrep", "ack"}


def _strip_privilege(segment: str) -> str:
    """Remove a leading sudo/doas wrapper so the inner verb is inspected."""
    match = re.match(r"\s*(?:sudo|doas)(?:\s+[^\s|;&#]+)*\s+(.*)$", segment, re.S)
    return match.group(1) if match else segment


def _env_dump_reason(segment: str) -> Optional[str]:
    """Bare env/printenv/set/export/declare -x dump detection for a segment."""
    rest = _strip_privilege(segment).strip()
    parts = rest.split(None, 1)
    if not parts:
        return None
    verb = parts[0].rsplit("/", 1)[-1]
    tail = parts[1].strip() if len(parts) > 1 else ""

    if verb == "printenv":
        return "environment-dump"
    if verb == "env":
        tokens = tail.split()
        has_command = any(
            not token.startswith("-") and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token)
            for token in tokens
        )
        if not has_command:
            return "environment-dump"
        for token in tokens:
            if not token.startswith("-") and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
                if token in ("env", "printenv", "set", "export", "declare", "typeset"):
                    return "environment-dump"
        return None
    if verb == "set":
        if tail == "":
            return "environment-dump"
        return None
    if verb == "export":
        if tail == "" or tail.startswith("-"):
            return "environment-dump"
        return None
    if verb in ("declare", "typeset"):
        if tail == "":
            return "environment-dump"
        tokens = tail.split()
        has_assignment = any("=" in token for token in tokens)
        has_print_flag = any(
            token.startswith("-") and ("p" in token or ("x" in token and not has_assignment))
            for token in tokens
        )
        if has_print_flag or not has_assignment:
            return "environment-dump"
        return None
    return None


def _ps_reason(segment: str) -> Optional[str]:
    """ps environment-column detection (BSD `e` flag, -E, -o/-O env)."""
    rest = _strip_privilege(segment)
    tokens = rest.split()
    if not tokens:
        return None
    verb = tokens[0].rsplit("/", 1)[-1]
    if verb != "ps":
        return None
    for token in tokens[1:]:
        if token.startswith("-"):
            body = token[1:]
            if "E" in body or "env" in body.lower():
                return "ps-environ-column"
            if re.search(r"o", body) and re.search(r"\benv\b", rest):
                return "ps-environ-column"
            continue
        if "e" in token or "E" in token:
            return "ps-environ-column"
    return None


def _path_reasons(text: str, exempt: set) -> List[str]:
    reasons: List[str] = []
    for reason, pattern in _PATH_PATTERNS:
        for match in pattern.finditer(text):
            if match.start() in exempt:
                continue
            if reason not in reasons:
                reasons.append(reason)
    return reasons


# Shell word-glue that the shell consumes while joining a word: quotes,
# substitution/backtick wrappers, escape backslashes, braces/parens, and glob
# metacharacters. Used only to *detect* reconstructed sensitive stems, never
# to echo command text.
_SUBSTITUTION_RE = re.compile(r"\$(?:\\([^()]*\\)|\{[^{}]*\})")


def _deglue(text: str, keep_globs: bool) -> Tuple[str, List[int]]:
    """Collapse shell word-glue so a spliced sensitive stem becomes visible.

    Returns (projected_text, original_index_of_each_output_char). Command
    substitutions/backticks are replaced by ``*`` so they read as a wildcard
    run inside the projected path. When ``keep_globs`` is true, ``?*[``
    characters are retained (the glob-tail scan needs them); otherwise they
    are removed so interior globs like ``enviro[n]`` collapse to ``environ``.
    The projection is linear in the input size.
    """
    out: List[str] = []
    orig: List[int] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "$":
            m = _SUBSTITUTION_RE.match(text, i)
            if m:
                out.append("*")
                orig.append(i)
                i = m.end()
                continue
            i += 1
            continue
        if ch == "`":
            j = text.find("`", i + 1)
            if j != -1:
                out.append("*")
                orig.append(i)
                i = j + 1
                continue
            i += 1
            continue
        if ch in "'\"\\(){}":
            i += 1
            continue
        if ch in "?*[":
            if keep_globs:
                out.append(ch)
                orig.append(i)
            i += 1
            continue
        out.append(ch)
        orig.append(i)
        i += 1
    return "".join(out), orig


def _dynamic_path_reasons(text: str, exempt: set) -> List[str]:
    """Stable ``dynamic-sensitive-path`` detection for runtime reconstruction:
    globs cut into sensitive stems, quote-split stems, and substitution /
    backtick splices that rebuild ``/proc/.../{env,cmd}``, ``.ssh``,
    ``.netrc``, ``.env``, or auth/credential/token/key/cookie stores.
    Literal matches (already covered by ``_path_reasons``) are left alone, so
    ordinary ``$CC``/``$(nproc)``/``src/*.c`` builds stay allowed.
    """
    reasons: List[str] = []

    def add() -> None:
        if "dynamic-sensitive-path" not in reasons:
            reasons.append("dynamic-sensitive-path")

    # Glob tails on the raw text: the glob can only expand onto the
    # sensitive filename (``/proc/self/enviro?``, ``token.*``, ``.ss?``).
    for _label, pattern in _DYNAMIC_PATTERNS:
        for match in pattern.finditer(text):
            if match.start() not in exempt:
                add()
                break

    # Quote/substitution/glob projections re-checked against every
    # sensitive-path shape (interior globs and split stems).
    for keep_globs in (False, True):
        projected, orig = _deglue(text, keep_globs)
        if projected == text:
            continue
        for _reason, pattern in _PATH_PATTERNS:
            for match in pattern.finditer(projected):
                if match.start() >= len(orig):
                    continue
                if orig[match.start()] not in exempt:
                    add()
                    break
    return reasons


def _secret_accessor_reasons(text: str) -> List[str]:
    reasons: List[str] = []
    for match in _SECRET_ACCESSOR_RE.finditer(text):
        if any(group and _SECRET_SUFFIX_RE.search(group) for group in match.groups()):
            reasons.append("secret-variable-expansion")
    return reasons


def _decode_b64(token: str) -> Optional[bytes]:
    cleaned = re.sub(r"\s+", "", token)
    try:
        return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4), validate=False)
    except (binascii.Error, ValueError):
        return None


def _looks_like_text(data: bytes) -> bool:
    if not data:
        return False
    printable = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13))
    return printable * 10 >= len(data) * 6


def _encoded_base64_reasons(text: str) -> List[str]:
    if not _DECODE_INDICATOR_RE.search(text):
        return []
    seen: set = set()
    for token in _B64_TOKEN_RE.findall(text):
        if len(token) > 4096 or token in seen:
            continue
        seen.add(token)
        decoded = _decode_b64(token)
        if not decoded or not _looks_like_text(decoded):
            continue
        decoded_text = decoded.decode("utf-8", errors="replace")
        if any(pattern.search(decoded_text) for _reason, pattern in _PATH_PATTERNS):
            return ["encoded-sensitive-reference"]
        if _secret_var_reasons(decoded_text) or _secret_accessor_reasons(decoded_text):
            return ["encoded-sensitive-reference"]
    return []


def _encoded_percent_reasons(text: str) -> List[str]:
    if not _PERCENT_RE.search(text):
        return []
    decoded = urllib.parse.unquote(text)
    if decoded == text:
        return []
    if any(pattern.search(decoded) for _reason, pattern in _PATH_PATTERNS):
        return ["encoded-sensitive-reference"]
    if _secret_var_reasons(decoded):
        return ["encoded-sensitive-reference"]
    return []


def _decoded_text_is_sensitive(text: str) -> bool:
    """True when a decoded/decrypted command line itself trips the sensitive
    path or secret-variable rules (shared by base64, percent, and hex)."""
    if any(pattern.search(text) for _reason, pattern in _PATH_PATTERNS):
        return True
    return bool(_secret_var_reasons(text) or _secret_accessor_reasons(text))


def _decode_hex_escape(match: re.Match) -> str:
    esc = match.group(0)
    if esc.startswith(r"\x"):
        return chr(int(esc[2:], 16))
    return chr(int(esc[1:], 8))


def _encoded_hex_reasons(text: str) -> List[str]:
    """Hex-encoded sensitive references: \\xNN/octal escapes (printf %b,
    echo -e, ANSI $'...') and raw hex dumps decoded by xxd -r/-p/unhex. The
    decoded command line is run through the same checks as base64/percent."""
    candidates: List[str] = []
    if _HEX_ESC_RE.search(text):
        candidates.append(_HEX_ESC_RE.sub(_decode_hex_escape, text))
    if _XXD_UNHEX_RE.search(text):
        seen: set = set()
        for token in _HEX_RUN_RE.findall(text):
            if len(token) % 2 or len(token) > 4096 or token in seen:
                continue
            seen.add(token)
            try:
                raw = binascii.unhexlify(token)
            except (binascii.Error, ValueError):
                continue
            if not _looks_like_text(raw):
                continue
            candidates.append(raw.decode("utf-8", errors="replace"))
    for candidate in candidates:
        if _decoded_text_is_sensitive(candidate):
            return ["encoded-sensitive-reference"]
    return []


_PRIORITY = [
    "interpreter-bypass", "encoded-sensitive-reference", "procfs-environ-cmdline",
    "credential-store-file", "dotenv-store", "ollama-usage-env", "ssh-key-material",
    "private-key-material", "netrc-password-store", "dynamic-sensitive-path",
    "url-userinfo-credential", "environment-pipeline", "environment-dump",
    "ps-environ-column", "secret-variable-expansion",
    "path-resolution-failed",
]


def _ordered_reasons(reasons: List[str]) -> List[str]:
    ordered: List[str] = []
    for key in _PRIORITY:
        if key in reasons and key not in ordered:
            ordered.append(key)
    for reason in reasons:
        if reason not in ordered:
            ordered.append(reason)
    return ordered


_STDIN_MAX_BYTES = 1 << 20  # 1 MiB bound for stdin subcommands.


def _read_bounded(stream: BinaryIO, limit: int = _STDIN_MAX_BYTES) -> Optional[bytes]:
    """Read at most limit+1 bytes; return None if the stream is oversized."""
    data = stream.read(limit + 1)
    if len(data) > limit:
        return None
    return data


def _block_result(reason: str) -> dict:
    return {
        "schema": SCHEMA,
        "tool": TOOL,
        "version": VERSION,
        "verdict": "block",
        "reason": reason,
        "reasons": [reason],
    }


def _emit_result(result: dict) -> int:
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    return 0 if result["verdict"] == "allow" else 1


def classify_command(command: str) -> dict:
    """Fail-closed classification. Returns a stable machine-readable dict."""
    text = command.strip()
    reasons: List[str] = []

    # Runner transport is a coordinator-only authority. Model tool calls pass
    # through this classifier, while the trusted campaign coordinator executes
    # its retained descriptor directly outside model confinement. Block every
    # shell spelling that names the entrypoint; no model may probe transport,
    # mint evidence, or race the one-writer aggregate.
    runner_spelling = re.sub(r"\\\r?\n", "", text)
    runner_spelling = runner_spelling.translate(
        str.maketrans("", "", "'\"\\")
    )
    if "run-factory" in runner_spelling:
        reasons.append("coordinator-only-runner")

    # Whole-text sensitive-path scan. Search-tool regex patterns inside quotes
    # are ordinary project grep patterns, never paths.
    quote_spans = _quoted_spans(text)
    segments = _split_segments(text)
    exempt: set[int] = set()
    for start, _end, segment in segments:
        verb = _first_word(segment)
        if verb not in _SEARCH_TOOLS:
            continue
        for _, pattern in _PATH_PATTERNS + _DYNAMIC_PATTERNS:
            for match in pattern.finditer(segment):
                pos = start + match.start()
                if any(span_start < pos < span_end for span_start, span_end, _q in quote_spans):
                    exempt.add(pos)
    for reason in _path_reasons(text, exempt):
        if reason not in reasons:
            reasons.append(reason)

    for reason in _dynamic_path_reasons(text, exempt):
        if reason not in reasons:
            reasons.append(reason)
    for reason in _command_path_reasons(text):
        if reason not in reasons:
            reasons.append(reason)

    for reason in _secret_var_reasons(text):
        if reason not in reasons:
            reasons.append(reason)
    for reason in _secret_accessor_reasons(text):
        if reason not in reasons:
            reasons.append(reason)
    env_object_hit = bool(_ENV_OBJECT_DUMP_RE.search(text) or _INNER_ENV_DUMP_RE.search(text))

    if _URL_USERINFO_RE.search(text):
        reason = "url-userinfo-credential"
        if reason not in reasons:
            reasons.append(reason)

    for reason in _encoded_percent_reasons(text):
        if reason not in reasons:
            reasons.append(reason)
    for reason in _encoded_base64_reasons(text):
        if reason not in reasons:
            reasons.append(reason)
    for reason in _encoded_hex_reasons(text):
        if reason not in reasons:
            reasons.append(reason)

    if _ENV_PIPE_RE.search(text):
        reason = "environment-pipeline"
        if reason not in reasons:
            reasons.append(reason)
    if _DUMP_WRAPPER_RE.search(text):
        reason = "environment-dump"
        if reason not in reasons:
            reasons.append(reason)

    interpreter_hit = False
    for _start, _end, segment in segments:
        if _INTERPRETER_NAME_RE.search(segment) and _INTERPRETER_FLAG_RE.search(segment):
            interpreter_hit = True
        reason = _ps_reason(segment)
        if reason and reason not in reasons:
            reasons.append(reason)
        reason = _env_dump_reason(segment)
        if reason and reason not in reasons:
            reasons.append(reason)

    if interpreter_hit and env_object_hit and "environment-dump" not in reasons:
        reasons.append("environment-dump")
    if interpreter_hit and reasons:
        reasons.insert(0, "interpreter-bypass")

    ordered = _ordered_reasons(reasons)

    verdict = "block" if ordered else "allow"
    return {
        "schema": SCHEMA,
        "tool": TOOL,
        "version": VERSION,
        "verdict": verdict,
        "reason": ordered[0] if ordered else "ok",
        "reasons": ordered,
    }


def _classify_path_str(text: str) -> List[str]:
    """Literal-path reasons: sensitive path shapes plus secret-variable and
    accessor rules (the same checks ``classify_path`` applies to the raw
    input). Never reads file contents."""
    reasons: List[str] = []
    for reason in _path_reasons(text, set()):
        if reason not in reasons:
            reasons.append(reason)
    for reason in _secret_var_reasons(text):
        if reason not in reasons:
            reasons.append(reason)
    for reason in _secret_accessor_reasons(text):
        if reason not in reasons:
            reasons.append(reason)
    return reasons


_PATH_RESOLVE_MAX_BYTES = 1 << 20  # mirrors the stdin bound; no unbounded fs work


def _resolve_path(path: str) -> Optional[str]:
    """Resolve symlink indirection for one literal path without ever reading
    file contents (only path components are inspected, synchronously).

    Existing entries (regular files, directories, symlinks, and dangling
    symlinks alike) are fully resolved with realpath, which also resolves
    symlinked ancestors. A nonexisting write target resolves its nearest
    existing ancestor and appends the missing remainder (realpath keeps
    nonexistent tail components verbatim). Any resolution error returns None
    so the caller fails closed.
    """
    if len(path) > _PATH_RESOLVE_MAX_BYTES:
        return None
    try:
        return os.path.realpath(path)
    except (OSError, ValueError, RuntimeError):
        return None


def _command_path_reasons(command: str) -> List[str]:
    """Resolve literal path-like shell argv so Bash cannot bypass the direct
    file-tool guard through a symlink. Parsing never executes or expands shell
    code, and file contents are never read. Unparseable/oversized argv fails
    closed; dynamic tokens remain covered by the reconstruction rules.
    """
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return ["path-resolution-failed"]
    if len(tokens) > 4096:
        return ["path-resolution-failed"]
    reasons: List[str] = []
    for token in tokens:
        candidate = token.lstrip("<>0123456789")
        if "=" in candidate and candidate.split("=", 1)[1].startswith(("/", ".", "~")):
            candidate = candidate.split("=", 1)[1]
        if not candidate.startswith(("/", ".", "~")) and "/" not in candidate:
            continue
        if any(mark in candidate for mark in ("$", "`", "*", "?", "[")):
            continue
        candidate = os.path.expanduser(candidate)
        resolved = _resolve_path(candidate)
        if resolved is None:
            if "path-resolution-failed" not in reasons:
                reasons.append("path-resolution-failed")
            continue
        for reason in _classify_path_str(resolved):
            if reason not in reasons:
                reasons.append(reason)
    return reasons


def classify_path(path: str) -> dict:
    """Fail-closed classification of a single direct file-tool path.

    Applies the same sensitive-path patterns as ``classify_command`` plus the
    secret-variable/accessor rules to BOTH the literal path and its
    symlink-resolved form (see ``_resolve_path``), so a symlink that
    indirection to a procfs/auth-store/dotenv/private-key path is blocked even
    when the link name itself is ordinary, while a normal symlink to an
    ordinary project file stays allowed. A resolution error fails closed with
    the stable reason ``path-resolution-failed``. File contents are never
    read.
    """
    reasons: List[str] = _classify_path_str(path)
    resolved = _resolve_path(path)
    if resolved is None:
        if "path-resolution-failed" not in reasons:
            reasons.append("path-resolution-failed")
    else:
        for reason in _classify_path_str(resolved):
            if reason not in reasons:
                reasons.append(reason)
    ordered = _ordered_reasons(reasons)
    verdict = "block" if ordered else "allow"
    return {
        "schema": SCHEMA,
        "tool": TOOL,
        "version": VERSION,
        "kind": "path",
        "verdict": verdict,
        "reason": ordered[0] if ordered else "ok",
        "reasons": ordered,
    }


def check_command_stdin(source: BinaryIO = sys.stdin.buffer) -> int:
    """Classify a shell command line read from stdin (bounded, fail-closed)."""
    data = _read_bounded(source)
    if data is None:
        return _emit_result(_block_result("oversized-input"))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _emit_result(_block_result("invalid-utf-8"))
    return _emit_result(classify_command(text))


def check_path_stdin(source: BinaryIO = sys.stdin.buffer) -> int:
    """Classify a single file path read from stdin (bounded, fail-closed).

    The input must be exactly one path string: non-empty, with no NUL byte and
    no embedded newline/CR. Any structural violation or a sensitive-path hit
    fails closed.
    """
    data = _read_bounded(source)
    if data is None:
        return _emit_result(_block_result("oversized-input"))
    try:
        path = data.decode("utf-8")
    except UnicodeDecodeError:
        return _emit_result(_block_result("invalid-utf-8"))
    if not path or "\x00" in path or "\n" in path or "\r" in path:
        return _emit_result(_block_result("invalid-path"))
    return _emit_result(classify_path(path))


# ---------------------------------------------------------------------------
# Streaming redaction
# ---------------------------------------------------------------------------

def _mask_value(match: re.Match) -> str:
    name = match.group("name")
    core_name = name.strip("\"'")
    if not _SECRET_SUFFIX_RE.search(core_name):
        return match.group(0)
    op = match.group("op")
    val = match.group("val")
    if val[:1] in ("'", '"'):
        return name + op + val[:1] + REDACTED + val[:1]
    return name + op + REDACTED


def _mask_query(url_token: str) -> str:
    """Mask sensitive URL query values in linear time, including long URLs."""
    frag = ""
    if "#" in url_token:
        url_token, frag = url_token.split("#", 1)
        frag = "#" + frag
    if "?" not in url_token:
        return url_token + frag
    base, query = url_token.split("?", 1)
    pairs = []
    for pair in query.split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            if _QUERY_SECRET_NAME_RE.match(key) and value:
                pair = key + "=" + REDACTED
        pairs.append(pair)
    return base + "?" + "&".join(pairs) + frag


def _mask_ordinary(line: str) -> str:
    """Apply all non-private-key masks to one line in bounded linear passes."""
    line = _URL_USERINFO_RE.sub(lambda m: m.group("scheme") + REDACTED + "@", line)
    line = _URL_TOKEN_RE.sub(lambda m: _mask_query(m.group(0)), line)
    line = _AUTH_HEADER_RE.sub(lambda m: m.group("prefix") + REDACTED, line)
    line = _ASSIGN_RE.sub(_mask_value, line)
    line = _BEARER_RE.sub(lambda m: m.group("sch") + " " + REDACTED, line)
    return _JWT_RE.sub(REDACTED_JWT, line)


def _redact_line(line: str, in_key_block: bool) -> Tuple[str, bool]:
    """Mask one line. Returns (masked_text, new_key_block_state)."""
    if in_key_block:
        end_match = _KEY_END_RE.search(line)
        if end_match:
            trailing = _mask_ordinary(line[end_match.end():])
            return KEY_BLOCK_END + trailing, False
        return "", True

    begin_match = _KEY_BEGIN_RE.search(line)
    if begin_match:
        prefix = _mask_ordinary(line[:begin_match.start()])
        rest = line[begin_match.end():]
        end_match = _KEY_END_RE.search(rest)
        if end_match:
            trailing = _mask_ordinary(rest[end_match.end():])
            return prefix + KEY_BLOCK_START + KEY_BLOCK_END + trailing, False
        return prefix + KEY_BLOCK_START, True

    return _mask_ordinary(line), False


def _fast_mask(line: str, in_key_block: bool) -> Tuple[str, bool]:
    """Linear-time masking for oversized lines, with full URL/key coverage."""
    return _redact_line(line, in_key_block)


def redact_text(text: str, max_line_bytes: int = _MAX_LINE_BYTES_DEFAULT) -> str:
    """Mask one chunk of already-decoded text. Idempotent and bounded."""
    out: List[str] = []
    in_key_block = False
    for line in text.splitlines(keepends=True):
        line_text = line.rstrip("\r\n")
        if len(line_text) > max_line_bytes:
            masked, in_key_block = _fast_mask(line_text, in_key_block)
        else:
            masked, in_key_block = _redact_line(line_text, in_key_block)
        out.append(masked + line[len(line_text):])
    return "".join(out)


def redact_stream(
    source: BinaryIO,
    sink: BinaryIO,
    max_line_bytes: int = _MAX_LINE_BYTES_DEFAULT,
) -> None:
    """Stream source -> sink, masking secrets in bounded memory."""
    in_key_block = False
    for raw in source:
        line = raw.decode("utf-8", errors="replace")
        line_text = line.rstrip("\r\n")
        if len(line_text) > max_line_bytes:
            masked, in_key_block = _fast_mask(line_text, in_key_block)
        else:
            masked, in_key_block = _redact_line(line_text, in_key_block)
        sink.write((masked + line[len(line_text):]).encode("utf-8"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_JSON_STDIN_MAX_BYTES = 8 << 20
_JSON_MAX_NODES = 100000
_JSON_MAX_DEPTH = 64


def redact_json_stdin(source: BinaryIO = sys.stdin.buffer) -> int:
    """Redact all string values in one bounded JSON document in one process."""
    data = _read_bounded(source, _JSON_STDIN_MAX_BYTES)
    if data is None:
        return 1
    try:
        value = json.loads(data.decode("utf-8"))
        nodes = 0

        def visit(item, depth: int):
            nonlocal nodes
            nodes += 1
            if nodes > _JSON_MAX_NODES or depth > _JSON_MAX_DEPTH:
                raise ValueError("JSON redaction bounds exceeded")
            if isinstance(item, str):
                return redact_text(item)
            if isinstance(item, list):
                return [visit(child, depth + 1) for child in item]
            if isinstance(item, dict):
                return {key: visit(child, depth + 1) for key, child in item.items()}
            if item is None or isinstance(item, (bool, int, float)):
                return item
            raise ValueError("unsupported JSON value")

        redacted = visit(value, 0)
        output = json.dumps(redacted, separators=(",", ":"), ensure_ascii=False)
        if len(output.encode("utf-8")) > _JSON_STDIN_MAX_BYTES:
            return 1
        sys.stdout.write(output + "\n")
        return 0
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError, json.JSONDecodeError):
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="credential-guard.py",
        description="Fail-closed credential classification and streaming redaction.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-command", help="classify a shell command line")
    check.add_argument(
        "--command", dest="command_text", required=True,
        help="shell command text to classify",
    )

    check_stdin = sub.add_parser(
        "check-command-stdin",
        help="classify a shell command line read from stdin (bounded, fail-closed)",
    )

    check_path = sub.add_parser(
        "check-path-stdin",
        help="classify a single file path read from stdin (bounded, fail-closed)",
    )

    sub.add_parser(
        "redact-json-stdin",
        help="mask every string value in one bounded JSON document",
    )

    redact = sub.add_parser("redact", help="mask secrets on stdin -> stdout")
    redact.add_argument("--file", metavar="PATH", help="read from PATH instead of stdin")
    redact.add_argument(
        "--max-line-bytes",
        type=int,
        default=_MAX_LINE_BYTES_DEFAULT,
        help=f"per-line bound for URL reconstruction (default {_MAX_LINE_BYTES_DEFAULT})",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "check-command":
        result = classify_command(args.command_text)
        sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
        return 0 if result["verdict"] == "allow" else 1

    if args.command == "check-command-stdin":
        return check_command_stdin()

    if args.command == "check-path-stdin":
        return check_path_stdin()

    if args.command == "redact-json-stdin":
        return redact_json_stdin()

    if args.command == "redact":
        source: BinaryIO = sys.stdin.buffer
        close_source = False
        if getattr(args, "file", None):
            source = open(args.file, "rb")
            close_source = True
        try:
            redact_stream(source, sys.stdout.buffer, args.max_line_bytes)
        finally:
            if close_source:
                source.close()
        return 0

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
