#!/usr/bin/env python3
"""Hidden stdlib Ollama usage guard (Task 7; QUOTA-01, QUOTA-02, §10).

This module is the hardened, standard-library implementation of the retained
``scripts/ollama-usage-guard.sh`` ``--check``/``--wait`` contract
(FACTORY-LOOP-SPEC §10).  It is the guard the control plane runs before
every model invocation (wired into :mod:`factory.loop.launch` via
:func:`require_quota`), and it satisfies QUOTA-02:

* **Credentials never appear in child argv.**  The settings fetch runs in a
  dedicated fetch child (``factory.loop.usage_fetch``) whose argv is fully
  structural (interpreter, ``-m`` module name, non-secret settings URL /
  fixture path).  The cookie crosses only the child's private stdin pipe.
* **Credentials never appear in child environments.**  The fetch child's
  environment is *built* from a small documented allowlist plus
  ``PYTHONPATH`` — never inherited, never carrying ``OLLAMA_*``,
  ``*COOKIE*``, ``*TOKEN*``, or any credential-shaped key (defense-in-depth
  re-checked before spawn).
* **Credentials never appear in logs or results.**  The child prints only a
  bounded redacted classification line; the parent discards the child's
  stderr, validates the classification format, and emits only redacted
  status.  Raw responses never cross the pipe.
* **Bounded nofollow mode/owner checks.**  The cookie file and the
  operator ``.ollama-usage-env`` store (which defaults to an
  operator-owned path *outside* the model workspace, never
  ``<workspace>/.ollama-usage-env`` or ``<repository root>/.ollama-usage-env``)
  are read with ``O_NOFOLLOW`` and must be a regular single-link file with
  mode exactly ``0600`` (owner rw only) owned by the current user and
  bounded in size.  The HTML fixture is read no-follow and size-bounded
  (diagnostics path, no mode checks).
* **Zeroization and cleanup.**  Cookie buffers are ``bytearray`` buffers
  that are cleared after use; the guard creates no temporary files; every
  fetch child is reaped on success, transient timeout, signal, or any
  exception.
* **Signals received while waiting** terminate the wait and the in-flight
  fetch child and exit ``128 + signum``, so the campaign can terminate
  cleanly (§10: "signals received while waiting MUST terminate the wait and
  campaign cleanly").

Exit-table contract (retained from the shell guard; §10 decision table):

* ``--check`` exit 0 — quota allowed (invoke the model);
* ``--check`` exit 1 — quota threshold reached (run ``--wait``);
* ``--check`` exit 2 — fatal (missing/expired cookie, unparseable page,
  undocumented exit; terminate the campaign);
* ``--check`` exit 3 — transient (network/server failure; run ``--wait``);
* ``--wait`` exit 0 — quota available again (then one final ``--check`` that
  MUST exit 0 before invocation);
* any nonzero ``--wait`` exit — terminate the campaign without invoking the
  model.
"""

from __future__ import annotations

import argparse
import html
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import BinaryIO, Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# The retained exit table (§10).  The guard CLI and :func:`require_quota`
# share exactly these codes with the legacy shell guard.
# ---------------------------------------------------------------------------

EXIT_ALLOWED = 0
EXIT_BLOCKED = 1
EXIT_FATAL = 2
EXIT_TRANSIENT = 3
EXIT_INTERRUPT_BASE = 128  # + signal number for a clean signal abort

# Bounded channels (§10): every credential/response read is size-capped.
MAX_COOKIE_BYTES = 64 * 1024        # a cookie header is ~1 KiB; 64 KiB bound
MAX_ENV_FILE_BYTES = 64 * 1024      # the .ollama-usage-env store
MAX_HTML_BYTES = 1024 * 1024        # a settings page is small; 1 MiB bound
MAX_CHILD_OUTPUT_BYTES = 16 * 1024  # the classification line is tiny
MAX_HINT_BYTES = 512                # reset-hint / reason field bound
FETCH_TIMEOUT = 20.0                # finite network/timeout bound (legacy 20s)

DEFAULT_SETTINGS_URL = "https://ollama.com/settings"
DEFAULT_THRESHOLD = 80.0
DEFAULT_POLL_INTERVAL = 300
DEFAULT_MAX_WAIT = 0
DEFAULT_MAX_POLLS = 0

# The fetch child's environment is *built* from exactly these benign keys
# (when present in the parent) plus ``PYTHONPATH``.  Nothing else — no
# ``OLLAMA_*``, no ``*COOKIE*``/``*TOKEN*``/``*SECRET*``, no ``PI_*`` — can
# reach it.
FETCH_ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "LC_COLLATE", "LC_MESSAGES", "LC_MONETARY", "LC_NUMERIC", "LC_TIME",
    "TERM", "TZ", "SHELL", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE",
)

# Keys parsed from the operator's .ollama-usage-env store (never sourced —
# parsed with a bounded line parser so no value is exported to any child).
ENV_FILE_KEYS = (
    "OLLAMA_COOKIE",
    "OLLAMA_THRESHOLD",
    "OLLAMA_WAIT_INTERVAL_SECONDS",
    "OLLAMA_WAIT_MAX_SECONDS",
    "OLLAMA_WAIT_MAX_POLLS",
    "OLLAMA_SETTINGS_URL",
)

# The guard's own ambient credential keys are *scrubbed*, never a cookie
# source and never fatal: the ambient environment is not a valid cookie
# transport, so a legacy sourced-store context is handled by omitting every
# ambient credential-shaped key from the built child environment and
# continuing (failing closed only on a genuinely missing private cookie at
# the fetch boundary, see :func:`_missing_cookie_reason`).

_NUMBER_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_INT_RE = re.compile(r"^[0-9]+$")
# The settings page classification patterns (ported verbatim from the legacy
# shell guard so the parse path is the retained contract).
_AUTH_PAGE_RE = re.compile(r"\bSign in\b", re.I)
_USAGE_SETTINGS_RE = re.compile(r"Usage\s*[·|-]?\s*Settings", re.I)
# The session/weekly classification patterns use exactly ``re.I | re.S`` as
# the legacy shell guard does, so the retained fixtures drive the identical
# parse contract (Task 7 review, obligation 7).
_SESSION_PATTERNS = (
    re.compile(r'aria-label=["\x27]Session\s+usage\s+([0-9]+(?:\.[0-9]+)?)%\s*used', re.I | re.S),
    re.compile(r"Session\s+usage.*?([0-9]+(?:\.[0-9]+)?)\s*%\s*used", re.I | re.S),
)
_WEEKLY_PATTERNS = (
    re.compile(r'aria-label=["\x27]Weekly\s+usage\s+([0-9]+(?:\.[0-9]+)?)%\s*used', re.I | re.S),
    re.compile(r"Weekly\s+usage.*?([0-9]+(?:\.[0-9]+)?)\s*%\s*used", re.I | re.S),
)
_HINT_RE = re.compile(
    r"((?:session|weekly)?\s*(?:usage\s*)?resets?\s+(?:in|at|on)?\s*[^<]{1,80})",
    re.I,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class UsageGuardError(Exception):
    """Base class for the hidden usage guard's fail-closed outcomes."""


class UsageConfigError(UsageGuardError):
    """The credential/config store is missing, unsafe, or malformed."""


class UsageQuotaBlocked(UsageGuardError):
    """§10: the quota guard blocks the model invocation (campaign stops)."""


class WaitInterrupted(UsageGuardError):
    """A TERM/INT/HUP signal aborted the wait (already cleaned up)."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(
            f"usage guard interrupted by {signal.Signals(signum).name}"
        )


# ---------------------------------------------------------------------------
# Secure bounded reads (nofollow, mode/owner/link checks)
# ---------------------------------------------------------------------------

def read_secure_file(
    path_text: str,
    *,
    max_bytes: int,
    label: str,
    check_owner_mode: bool = True,
) -> bytes:
    """Bounded no-follow read of a regular owned single-link file.

    ``check_owner_mode`` applies the credential-store checks (owned by the
    current user, single link, not group/other-writable); the diagnostics
    HTML fixture path keeps the no-follow and size bounds only.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path_text, flags)
    except OSError as exc:
        raise UsageConfigError(f"cannot open {label} {path_text}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise UsageConfigError(f"{label} {path_text} is not a regular file")
        if check_owner_mode:
            if info.st_uid != os.geteuid():
                raise UsageConfigError(
                    f"{label} {path_text} is not owned by the current user"
                )
            if info.st_nlink != 1:
                raise UsageConfigError(
                    f"{label} {path_text} has an unsafe link count"
                )
            # Strict 0600 (owner rw only): any mode other than exactly 0600 —
            # group/other bits, execute bits, or a world-readable file — fails
            # closed (Task 7 review, obligation 1).
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise UsageConfigError(
                    f"{label} {path_text} must be mode 0600 (owner rw only); "
                    f"got {oct(stat.S_IMODE(info.st_mode))}"
                )
        if info.st_size > max_bytes:
            raise UsageConfigError(
                f"{label} {path_text} exceeds the {max_bytes}-byte bound"
            )
        data = bytearray()
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                raise UsageConfigError(
                    f"{label} {path_text} exceeds the {max_bytes}-byte bound"
                )
        return bytes(data)
    finally:
        os.close(descriptor)


def _shell_unquote(raw: str) -> Optional[str]:
    """Unquote a single shell word (single/double quotes) bounded by shlex."""
    try:
        parts = shlex.split(raw)
    except ValueError:
        return None
    if len(parts) != 1:
        return None
    return parts[0]


def parse_env_file(data: bytes) -> Dict[str, str]:
    """Bounded parser for the operator's ``.ollama-usage-env`` store.

    The store is *parsed*, never sourced: no value is exported to the
    guard's environment and therefore nothing can be inherited by a child.
    Each line is ``export KEY=value`` (or ``KEY=value``) with shell quoting;
    a malformed value for any recognized key fails closed.
    """
    if len(data) > MAX_ENV_FILE_BYTES:
        raise UsageConfigError(
            f"env store exceeds the {MAX_ENV_FILE_BYTES}-byte bound"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageConfigError(f"env store is not valid UTF-8: {exc}") from exc
    result: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key = key.strip()
        if not key or not key.isidentifier():
            continue
        value = _shell_unquote(raw_value.strip())
        if value is None:
            raise UsageConfigError(
                f"cannot parse the value for {key} in the env store"
            )
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# Credential state
# ---------------------------------------------------------------------------

@dataclass
class Credentials:
    """Bounded credential/config state of one guard run.

    ``cookie`` is a zeroizable ``bytearray``; the caller clears it with
    :meth:`zeroize` when the run ends (best-effort zeroization of every
    owned buffer; the kernel pipe copy and the operator's own store are
    outside the guard's ownership).
    """

    cookie: bytearray
    threshold: float
    poll_interval: int
    max_wait: int
    max_polls: int
    settings_url: str
    html_file: Optional[str] = None
    cookie_source: str = "unset"

    def zeroize(self) -> None:
        for index in range(len(self.cookie)):
            self.cookie[index] = 0
        self.cookie.clear()

    def redaction_tokens(self) -> List[str]:
        """Credential tokens the guard redacts from every printed field."""
        return redaction_tokens(self.cookie)


def redaction_tokens(cookie: bytes) -> List[str]:
    """The exact credential tokens (full cookie, pairs, names, values).

    Tokens shorter than four characters are skipped so ordinary words (for
    example ``aid``) are never mangled by over-eager redaction.
    """
    if not cookie:
        return []
    text = cookie.decode("utf-8", "replace")
    tokens: List[str] = []
    if text:
        tokens.append(text)
        for pair in text.split(";"):
            pair = pair.strip()
            if pair:
                tokens.append(pair)
            name, separator, value = pair.partition("=")
            if separator and name:
                tokens.append(name)
                tokens.append(value)
    return sorted(
        {token for token in tokens if len(token) >= 4}, key=len, reverse=True
    )


def redact(text: str, tokens: Sequence[str]) -> str:
    """Replace every credential token in ``text`` with ``[redacted]``."""
    output = text
    for token in tokens:
        if token:
            output = output.replace(token, "[redacted]")
    return output


def _validate_settings_url(url: str) -> None:
    """HTTPS-only, with an explicit loopback test seam (Task 7 review, 9).

    Network fetches accept ``https://`` only.  ``http://`` is rejected in
    production and permitted only for an explicit loopback
    (``127.0.0.1``/``localhost``) seam used exclusively by the hermetic
    hidden suite.  The fetch child re-checks the same rule defensively
    before connecting.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise UsageConfigError(f"invalid settings URL {url!r}") from exc
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https":
        return
    if scheme == "http" and host in ("127.0.0.1", "localhost"):
        return
    raise UsageConfigError(
        f"settings URL {url!r} is not HTTPS; http:// is permitted only for "
        "the explicit 127.0.0.1/localhost loopback seam"
    )


def _validate_cookie(cookie: bytes) -> None:
    """Reject a legacy CRLF / embedded-control cookie (header-injection class).

    Task 7 review obligation 11: a cookie value containing CR, LF, or any
    other control character is fatal and is never passed to the HTTP
    request.  Applied to every cookie source (store, cookie file, stdin)
    before the fetch child is spawned and re-checked inside the fetch child
    before the header is built.
    """
    for index, value in enumerate(cookie):
        if value < 0x20 or value == 0x7F:
            raise UsageConfigError(
                "the cookie contains a control character "
                f"(byte 0x{value:02x} at offset {index}); refusing the "
                "legacy CRLF/header-injection cookie"
            )


def _read_bounded_stdin(stream: BinaryIO) -> bytearray:
    """Read a bounded cookie from a private stdin channel (fd 0)."""
    buffer = bytearray()
    while True:
        chunk = stream.read(min(65536, MAX_COOKIE_BYTES - len(buffer) + 1))
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_COOKIE_BYTES:
            raise UsageConfigError(
                f"cookie stdin exceeds the {MAX_COOKIE_BYTES}-byte bound"
            )
    return buffer


def _default_env_file() -> str:
    """The canonical *operator* credential store, outside the model workspace.

    Task 7 review obligation 1: the guard's default store is never
    ``<workspace>/.ollama-usage-env`` or ``<repository root>/.ollama-usage-env``.
    It defaults to an operator-owned path outside the model-visible workspace
    (``$OLLAMA_USAGE_ENV_FILE`` when set by the operator, otherwise
    ``$XDG_CONFIG_HOME/controller-box/ollama-usage-env`` or
    ``~/.config/controller-box/ollama-usage-env``).  The launch authority
    therefore never scopes the store into the model workspace; the store is
    read strictly nofollow with mode exactly ``0600``, owned by the operator,
    single-link, and size-bounded (see :func:`read_secure_file`).
    """
    override = os.environ.get("OLLAMA_USAGE_ENV_FILE")
    if override:
        return override
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        base = Path(config_home)
    else:
        base = Path.home() / ".config"
    return str(base / "controller-box" / "ollama-usage-env")


def assert_store_outside_workspace(path_text: str, workspace: object) -> None:
    """Fail closed when a credential store is scoped inside the model workspace.

    Task 7 review obligation 1 (launch-authority side): the operator store
    must live outside the model-visible workspace, so an Ollama launch can
    never read a cookie store from an unrelated directory or hand the model
    workspace a store path.  ``workspace`` is the canonical workspace root of
    the invocation.
    """
    store = Path(path_text)
    root = Path(str(workspace))
    try:
        store_resolved = store.resolve()
        root_resolved = root.resolve()
    except OSError as exc:
        raise UsageConfigError(
            f"cannot resolve the credential store {path_text!r} against "
            f"workspace {root}: {exc}"
        ) from exc
    if store_resolved == root_resolved or store_resolved.is_relative_to(root_resolved):
        raise UsageConfigError(
            f"the operator credential store {path_text} is scoped inside the "
            f"model workspace {root}; the store must live outside the "
            "model-visible workspace"
        )


def acquire_credentials(
    *,
    cookie_file: Optional[str] = None,
    cookie_stdin: bool = False,
    env_file: Optional[str] = None,
    settings_url: Optional[str] = None,
    threshold: Optional[float] = None,
    poll_interval: Optional[int] = None,
    max_wait: Optional[int] = None,
    max_polls: Optional[int] = None,
    html_file: Optional[str] = None,
    stdin: BinaryIO = sys.stdin.buffer,
    environ: Optional[Mapping[str, str]] = None,
) -> Credentials:
    """Acquire the credential/config for one guard run (never from a child).

    Cookie source priority (all read, none re-exported to any child):

    1. ``--cookie-file`` — the operator's mode-0600 no-follow owned store;
    2. ``--cookie-stdin`` — a bounded read of the private stdin channel;
    3. the canonical ``.ollama-usage-env`` store, parsed without sourcing.

    The ambient ``OLLAMA_COOKIE`` environment variable is deliberately
    **not** a cookie source (QUOTA-02): the hardened transport is a bounded
    private stdin or a mode-0600 descriptor/store, never a shared
    environment variable that every child would inherit.  A guard run from
    a shell that sourced the legacy store fails closed as a missing
    private cookie instead of silently exporting the credential to child
    processes.

    Non-credential settings come from CLI flags, then the ambient
    environment, then the store, then the documented defaults.  A present
    but unsafe store fails closed (never read through a symlink, wrong
    owner, multi-link, or group/other-writable path).
    """
    environ = os.environ if environ is None else environ
    cookie = bytearray()
    source = "unset"
    store: Dict[str, str] = {}
    if cookie_file:
        cookie.extend(
            read_secure_file(
                cookie_file, max_bytes=MAX_COOKIE_BYTES, label="cookie file"
            )
        )
        source = "file"
    elif cookie_stdin:
        cookie = _read_bounded_stdin(stdin)
        source = "stdin"
    else:
        env_path = env_file or _default_env_file()
        if os.path.lexists(env_path):
            raw = read_secure_file(
                env_path, max_bytes=MAX_ENV_FILE_BYTES, label="env store"
            )
            store = parse_env_file(raw)
        if store.get("OLLAMA_COOKIE"):
            cookie.extend(store["OLLAMA_COOKIE"].encode("utf-8"))
            source = "env-store"
    if cookie:
        # Header-injection class: CR/LF/control bytes are fatal before the
        # cookie can reach any HTTP request (Task 7 review obligation 11).
        _validate_cookie(cookie)

    def setting_value(name: str, flag_value: object, default: object) -> str:
        if flag_value is not None:
            return str(flag_value)
        if name in environ and environ[name]:
            return environ[name]
        if store.get(name):
            return store[name]
        return str(default)

    threshold_raw = setting_value("OLLAMA_THRESHOLD", threshold, DEFAULT_THRESHOLD)
    if not _NUMBER_RE.fullmatch(threshold_raw):
        raise UsageConfigError(f"invalid threshold {threshold_raw!r}")
    threshold_value = float(threshold_raw)

    def integer_setting(name: str, flag_value: Optional[int], default: int) -> int:
        if flag_value is not None:
            raw = str(flag_value)
        elif name in environ and environ[name]:
            raw = environ[name]
        elif store.get(name):
            raw = store[name]
        else:
            return default
        if not _INT_RE.fullmatch(raw):
            raise UsageConfigError(f"invalid {name} {raw!r}")
        return int(raw)

    poll_interval = integer_setting(
        "OLLAMA_WAIT_INTERVAL_SECONDS", poll_interval, DEFAULT_POLL_INTERVAL
    )
    max_wait = integer_setting("OLLAMA_WAIT_MAX_SECONDS", max_wait, DEFAULT_MAX_WAIT)
    max_polls = integer_setting("OLLAMA_WAIT_MAX_POLLS", max_polls, DEFAULT_MAX_POLLS)

    settings_value = setting_value("OLLAMA_SETTINGS_URL", settings_url, DEFAULT_SETTINGS_URL)
    _validate_settings_url(settings_value)

    if poll_interval <= 0 and max_wait <= 0 and max_polls <= 0:
        raise UsageConfigError(
            "a zero poll interval with no --wait bound would busy-loop "
            "unboundedly; every --wait bound must be finite (Task 7 review "
            "obligation 10)"
        )

    return Credentials(
        cookie=cookie,
        threshold=threshold_value,
        poll_interval=poll_interval,
        max_wait=max_wait,
        max_polls=max_polls,
        settings_url=settings_value,
        html_file=html_file,
        cookie_source=source,
    )


# ---------------------------------------------------------------------------
# Classification (the retained parse path)
# ---------------------------------------------------------------------------

def classify_html(raw_text: str) -> Tuple[str, str, str, str]:
    """Classify a settings page: ``(kind, session, weekly, extra)``.

    Ported verbatim from the legacy shell guard so ``tests/fixtures/
    usage-ok.html``, ``usage-blocked.html``, and ``login.html`` drive exactly
    the same parse path (QUOTA-01).  ``kind`` is ``usage``, ``auth``, or
    ``parse``; for ``usage`` the extra field is the reset hint, for
    ``auth``/``parse`` the reason text.
    """
    plain = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw_text)))
    if _AUTH_PAGE_RE.search(plain) and not _USAGE_SETTINGS_RE.search(plain):
        return "auth", "", "", "login page returned"
    values: Dict[str, str] = {}
    for name, patterns in (("session", _SESSION_PATTERNS), ("weekly", _WEEKLY_PATTERNS)):
        match = None
        for pattern in patterns:
            match = pattern.search(raw_text)
            if match:
                break
        values[name] = match.group(1) if match else ""
    if not values["session"] or not values["weekly"]:
        return "parse", values["session"], values["weekly"], "usage fields not found"
    hint_match = _HINT_RE.search(plain)
    hint = hint_match.group(1).strip() if hint_match else ""
    return "usage", values["session"], values["weekly"], hint


# ---------------------------------------------------------------------------
# The fetch child boundary (private stdin channel)
# ---------------------------------------------------------------------------

def fetch_child_argv(
    settings_url: str, html_file: Optional[str] = None
) -> List[str]:
    """The fetch child argv: fully structural, no credential material.

    The child is the sibling ``usage_fetch.py`` executed by the exact
    control-plane interpreter (``.factory/`` is dot-hidden so it can never
    be resolved by name as a ``factory`` package; a direct absolute script
    path is the deterministic entry).
    """
    child = Path(__file__).resolve().with_name("usage_fetch.py")
    argv = [
        sys.executable,
        str(child),
        "--settings-url",
        settings_url,
        "--cookie-max",
        str(MAX_COOKIE_BYTES),
        "--timeout",
        str(FETCH_TIMEOUT),
    ]
    if html_file:
        argv += ["--html-file", str(html_file)]
    return argv


def fetch_child_env(
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """The fetch child's built environment (allowlist + PYTHONPATH only).

    The parent environment is never inherited wholesale; every key is drawn
    from :data:`FETCH_ENV_ALLOWLIST` and the module resolution path, and a
    defensive check refuses any surviving credential-shaped key before
    spawn.

    **Ambient credentials are scrubbed, never fatal** (Task 7 review,
    obligation 8): a parent environment that carries ``OLLAMA_COOKIE`` or
    any other credential-shaped key is *not* a cookie source and never
    spawns a child with that key — every ambient credential-shaped key is
    simply omitted from the built child environment and the guard continues
    (failing closed only on a genuinely missing private cookie at the fetch
    boundary, see :func:`_missing_cookie_reason`).  A caller-supplied
    environment cannot smuggle a credential into the built child
    environment through the allowlist; any key that would survive is a
    hard error because it would leak into ``/proc/<pid>/environ``.
    """
    parent = os.environ if environ is None else environ
    environment: Dict[str, str] = {}
    for key in FETCH_ENV_ALLOWLIST:
        if key in parent:
            environment[key] = parent[key]
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    environment["PYTHONHASHSEED"] = "0"
    for key in environment:
        upper = key.upper()
        if key.startswith("OLLAMA_"):
            raise UsageConfigError(
                f"refusing to spawn the fetch child with key {key!r}"
            )
        if any(
            token in upper
            for token in ("COOKIE", "TOKEN", "PASSWORD", "PASSWD", "API_KEY",
                          "SECRET", "CREDENTIAL", "PRIVATE_KEY", "AUTH")
        ):
            raise UsageConfigError(
                f"refusing to spawn the fetch child with credential-shaped "
                f"key {key!r}"
            )
    return environment


def _terminate_child(proc: subprocess.Popen[bytes]) -> None:
    """Bounded terminate → kill → reap of the fetch child (never leaves it)."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _run_fetch(credentials: Credentials) -> Tuple[int, str]:
    """Run the fetch child with the cookie on its private stdin.

    Returns ``(child_exit, classification_line)``: 0 (classification
    printed), 3 (transient network failure), or 2 (internal child failure).
    Every path reaps the child; any signal or exception during the fetch
    also reaps it before the exception propagates, so no child is ever left
    running.  The child's stderr is never surfaced.
    """
    argv = fetch_child_argv(credentials.settings_url, html_file=credentials.html_file)
    environment = fetch_child_env()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            close_fds=True,
        )
    except OSError as exc:
        raise UsageGuardError(f"cannot spawn the usage fetch child: {exc}") from exc
    try:
        out, _err = proc.communicate(
            bytes(credentials.cookie), timeout=FETCH_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        _terminate_child(proc)
        return EXIT_TRANSIENT, ""
    except BaseException:
        # A signal (WaitInterrupted / KeyboardInterrupt) or any other
        # exception while the fetch is in flight: bounded-terminate and reap
        # the child before propagating, so nothing is left running.
        _terminate_child(proc)
        raise
    if proc.returncode == 0:
        if len(out) > MAX_CHILD_OUTPUT_BYTES:
            return EXIT_FATAL, ""
        return EXIT_ALLOWED, out.decode("utf-8", "replace")
    if proc.returncode == EXIT_TRANSIENT:
        return EXIT_TRANSIENT, ""
    return EXIT_FATAL, ""


def _validate_classification(line: str) -> Optional[Tuple[str, str, str, str]]:
    """Validate the single bounded classification line from the child.

    ``kind<TAB>session<TAB>weekly<TAB>extra`` — any other shape is an
    undocumented (fatal) result.
    """
    fields = line.strip().split("\t")
    if len(fields) != 4:
        return None
    kind, session, weekly, extra = fields
    if kind not in ("usage", "auth", "parse"):
        return None
    if kind == "usage":
        if not _NUMBER_RE.fullmatch(session) or not _NUMBER_RE.fullmatch(weekly):
            return None
    else:
        if session and not _NUMBER_RE.fullmatch(session):
            return None
        if weekly and not _NUMBER_RE.fullmatch(weekly):
            return None
    if len(extra) > MAX_HINT_BYTES:
        return None
    return kind, session, weekly, extra


def _format_threshold(threshold: float) -> str:
    if threshold == int(threshold):
        return str(int(threshold))
    return str(threshold)


def print_json_status(status: Mapping[str, object]) -> None:
    """Emit the machine-readable redacted single-check status (--json)."""
    payload = {
        "session_percent": float(status["session"]),
        "weekly_percent": float(status["weekly"]),
        "threshold_percent": float(status["threshold"]),
        "blocked": bool(status["blocked"]),
        "reset_hint": status.get("reset_hint"),
    }
    print(json.dumps(payload, separators=(",", ":")))


def _missing_cookie_reason(credentials: Credentials) -> Optional[str]:
    """Fail-closed pre-fetch gate (QUOTA-02).

    A *network* guard (no diagnostics ``--html-file``) with no valid
    private cookie is fatal **before any fetch**.  Without this gate a
    missing cookie would fall through to the network fetch, classify the
    resulting failure as transient (exit 3), and then retry for an
    unbounded ``--wait`` - the operator credential error would never
    surface and a missing cookie would be indistinguishable from a
    network outage.  The diagnostics fixture path needs no cookie and
    skips the gate.
    """
    if credentials.html_file:
        return None
    if not credentials.cookie:
        return (
            "OLLAMA_COOKIE is missing; run source "
            "scripts/update-ollama-cookies.sh"
        )
    return None


def check_once(
    credentials: Credentials,
    *,
    json_output: bool = False,
) -> Tuple[int, Dict[str, object]]:
    """One ``--check`` (the retained exit table).

    Returns ``(exit_code, status)`` where ``status`` is the redacted
    machine status (also printed by the CLI according to ``json_output``).
    Exit codes: 0 allowed, 1 quota blocked, 2 fatal, 3 transient.  A
    network guard with no valid private cookie fails closed (fatal, exit
    2) *before* the fetch child is spawned (see
    :func:`_missing_cookie_reason`).
    """
    tokens = credentials.redaction_tokens()
    missing_reason = _missing_cookie_reason(credentials)
    if missing_reason is not None:
        print(f"ollama-guard: {missing_reason}", file=sys.stderr)
        return EXIT_FATAL, {"kind": "no-cookie"}

    exit_code, line = _run_fetch(credentials)
    if exit_code == EXIT_TRANSIENT:
        print(
            "ollama-guard: transient failure fetching "
            f"{redact(credentials.settings_url, tokens)}",
            file=sys.stderr,
        )
        return EXIT_TRANSIENT, {"kind": "transient"}
    if exit_code != EXIT_ALLOWED:
        if credentials.html_file:
            print(
                "ollama-guard: cannot read HTML fixture "
                f"{redact(credentials.html_file, tokens)}",
                file=sys.stderr,
            )
        else:
            print(
                "ollama-guard: usage fetch failed internally "
                "(undocumented child result)",
                file=sys.stderr,
            )
        return EXIT_FATAL, {"kind": "fatal"}

    parsed = _validate_classification(line)
    if parsed is None:
        print("ollama-guard: invalid parser result", file=sys.stderr)
        return EXIT_FATAL, {"kind": "fatal"}
    kind, session, weekly, extra = parsed
    extra_redacted = redact(extra, tokens)
    if kind == "auth":
        print(
            "ollama-guard: authentication expired; refresh cookies with "
            "source scripts/update-ollama-cookies.sh",
            file=sys.stderr,
        )
        return EXIT_FATAL, {"kind": "auth", "reason": extra_redacted}
    if kind == "parse":
        print(
            "ollama-guard: settings page format changed; "
            f"session='{session}' weekly='{weekly}'",
            file=sys.stderr,
        )
        return EXIT_FATAL, {"kind": "parse", "reason": extra_redacted}

    session_value = float(session)
    weekly_value = float(weekly)
    blocked = (
        session_value >= credentials.threshold
        or weekly_value >= credentials.threshold
    )
    status: Dict[str, object] = {
        "kind": "usage",
        "session": session_value,
        "weekly": weekly_value,
        "threshold": credentials.threshold,
        "blocked": blocked,
        "reset_hint": extra_redacted or None,
    }
    if json_output:
        print_json_status(status)
    else:
        print(
            f"ollama-guard: session={session}% weekly={weekly}% "
            f"threshold={_format_threshold(credentials.threshold)}%"
        )
        if extra_redacted:
            print(f"ollama-guard: {extra_redacted}")
    return (EXIT_BLOCKED if blocked else EXIT_ALLOWED), status


# ---------------------------------------------------------------------------
# --wait (bounded poll loop, signal-clean)
# ---------------------------------------------------------------------------

def _install_wait_handlers() -> Dict[int, object]:
    saved: Dict[int, object] = {}
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        saved[signum] = signal.getsignal(signum)

        def handler(_signum: int, _frame: object) -> None:
            raise WaitInterrupted(_signum)

        signal.signal(signum, handler)
    return saved


def _restore_wait_handlers(saved: Mapping[int, object]) -> None:
    for signum, previous in saved.items():
        signal.signal(signum, previous)


def wait(credentials: Credentials) -> int:
    """``--wait``: poll until allowed, bounded by poll count / wait seconds.

    Returns 0 (allowed), 1 (bounds exhausted while quota/transient), 2
    (fatal), or ``128 + signum`` (a signal terminated the wait cleanly; the
    in-flight fetch child was already terminated and reaped).
    """
    saved = _install_wait_handlers()
    try:
        polls = 0
        started = time.monotonic()
        while True:
            polls += 1
            try:
                rc, _status = check_once(credentials)
            except WaitInterrupted as exc:
                return EXIT_INTERRUPT_BASE + exc.signum
            if rc == EXIT_ALLOWED:
                if polls > 1:
                    print("ollama-guard: quota available again; continuing")
                return EXIT_ALLOWED
            if rc == EXIT_BLOCKED:
                print(
                    "ollama-guard: quota threshold reached; waiting "
                    f"{credentials.poll_interval}s before recheck",
                    file=sys.stderr,
                )
            elif rc == EXIT_TRANSIENT:
                print(
                    "ollama-guard: network failure while waiting; retrying "
                    f"in {credentials.poll_interval}s",
                    file=sys.stderr,
                )
            else:
                return rc
            elapsed = time.monotonic() - started
            if credentials.max_polls > 0 and polls >= credentials.max_polls:
                print("ollama-guard: maximum poll count reached", file=sys.stderr)
                return EXIT_BLOCKED
            if (
                credentials.max_wait > 0
                and elapsed + credentials.poll_interval > credentials.max_wait
            ):
                print("ollama-guard: maximum wait time reached", file=sys.stderr)
                return EXIT_BLOCKED
            try:
                time.sleep(credentials.poll_interval)
            except WaitInterrupted as exc:
                return EXIT_INTERRUPT_BASE + exc.signum
    finally:
        _restore_wait_handlers(saved)


# ---------------------------------------------------------------------------
# The §10 decision table (wired into the launch authority)
# ---------------------------------------------------------------------------

def require_quota(
    *,
    cookie_file: Optional[str] = None,
    cookie_stdin: bool = False,
    html_file: Optional[str] = None,
    settings_url: Optional[str] = None,
    env_file: Optional[str] = None,
    threshold: Optional[float] = None,
    poll_interval: Optional[int] = None,
    max_wait: Optional[int] = None,
    max_polls: Optional[int] = None,
) -> None:
    """Run the §10 decision table before a model invocation.

    ``--check`` exit 0 → return (allowed).  ``--check`` exit 1 or 3 → run
    ``--wait``; ``--wait`` exit 0 → one final ``--check`` which MUST exit 0.
    Any fatal ``--check`` exit, undocumented exit, or nonzero ``--wait``
    exit raises :class:`UsageQuotaBlocked` so the caller terminates the
    campaign without invoking the model.  TERM/INT/HUP during the initial
    ``--check``, the ``--wait``, or the final ``--check`` all terminate and
    reap the credential-holding fetch child and propagate
    :class:`WaitInterrupted` so the caller exits ``128 + signum`` — no
    ``require_quota`` path is signal-unsafe (Task 7 review, obligation 12).
    The cookie buffers are zeroized on every path.
    """
    credentials = acquire_credentials(
        cookie_file=cookie_file,
        cookie_stdin=cookie_stdin,
        env_file=env_file,
        settings_url=settings_url,
        threshold=threshold,
        poll_interval=poll_interval,
        max_wait=max_wait,
        max_polls=max_polls,
        html_file=html_file,
    )
    saved = _install_wait_handlers()
    try:
        rc, _status = check_once(credentials)
        if rc == EXIT_ALLOWED:
            return
        if rc in (EXIT_BLOCKED, EXIT_TRANSIENT):
            wait_rc = wait(credentials)
            if wait_rc >= EXIT_INTERRUPT_BASE:
                raise WaitInterrupted(wait_rc - EXIT_INTERRUPT_BASE)
            if wait_rc != EXIT_ALLOWED:
                raise UsageQuotaBlocked(
                    f"ollama usage guard --wait ended with exit {wait_rc}"
                )
            final_rc, _final_status = check_once(credentials)
            if final_rc != EXIT_ALLOWED:
                raise UsageQuotaBlocked(
                    "the final ollama usage --check after --wait must exit 0 "
                    f"before invocation (got {final_rc})"
                )
            return
        raise UsageQuotaBlocked(
            f"ollama usage guard --check ended with fatal exit {rc}"
        )
    finally:
        _restore_wait_handlers(saved)
        credentials.zeroize()


# ---------------------------------------------------------------------------
# CLI (python -m factory.loop.usage)
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="factory-usage",
        description=(
            "Hidden Ollama usage guard (FACTORY-LOOP-SPEC §10; Task 7). "
            "Retains the scripts/ollama-usage-guard.sh --check/--wait "
            "contract with hardened credential transport: the cookie never "
            "appears in a child argv, a child environment, a log, or a "
            "result."
        ),
    )
    parser.add_argument("--check", action="store_true", help="check once (default)")
    parser.add_argument("--wait", action="store_true", help="poll until allowed")
    parser.add_argument("--json", action="store_true", help="machine status for one check")
    parser.add_argument("--html-file", metavar="FILE", help="parse a saved settings page")
    parser.add_argument("--cookie-file", metavar="FILE", help="mode-0600 owned cookie store")
    parser.add_argument("--cookie-stdin", action="store_true", help="read the cookie from stdin")
    parser.add_argument("--env-file", metavar="FILE", help="canonical .ollama-usage-env store")
    parser.add_argument("--settings-url", metavar="URL", help="settings endpoint")
    parser.add_argument("--threshold", type=float, metavar="PCT")
    parser.add_argument("--poll-interval", type=int, metavar="SECONDS")
    parser.add_argument("--max-wait", type=int, metavar="SECONDS")
    parser.add_argument("--max-polls", type=int, metavar="N")
    args = parser.parse_args(argv)

    mode = "wait" if args.wait else "check"
    if args.json and mode == "wait":
        print("ollama-guard: --json cannot be combined with --wait", file=sys.stderr)
        return EXIT_FATAL
    try:
        credentials = acquire_credentials(
            cookie_file=args.cookie_file,
            cookie_stdin=args.cookie_stdin,
            env_file=args.env_file,
            settings_url=args.settings_url,
            threshold=args.threshold,
            poll_interval=args.poll_interval,
            max_wait=args.max_wait,
            max_polls=args.max_polls,
            html_file=args.html_file,
        )
    except UsageGuardError as exc:
        print(f"ollama-guard: {exc}", file=sys.stderr)
        return EXIT_FATAL
    if mode == "wait":
        try:
            return wait(credentials)
        except WaitInterrupted as exc:
            return EXIT_INTERRUPT_BASE + exc.signum
        except UsageGuardError as exc:
            print(f"ollama-guard: {exc}", file=sys.stderr)
            return EXIT_FATAL
        except Exception as exc:
            # Catch-all: every exception maps to a documented exit with no
            # traceback and no undocumented exit code (Task 7 review, 8).
            print(f"ollama-guard: internal failure: {exc}", file=sys.stderr)
            return EXIT_FATAL
        finally:
            credentials.zeroize()
    # A single ``--check`` is also signal-clean (§10): a TERM/INT/HUP while
    # the fetch child is in flight terminates and reaps the credential-holding
    # child and returns 128+signum instead of leaking it.  ``wait`` installs
    # its own handlers internally.
    saved = _install_wait_handlers()
    try:
        try:
            rc, _status = check_once(credentials, json_output=args.json)
            return rc
        except WaitInterrupted as exc:
            return EXIT_INTERRUPT_BASE + exc.signum
        except UsageGuardError as exc:
            print(f"ollama-guard: {exc}", file=sys.stderr)
            return EXIT_FATAL
        except Exception as exc:
            print(f"ollama-guard: internal failure: {exc}", file=sys.stderr)
            return EXIT_FATAL
    finally:
        _restore_wait_handlers(saved)
        credentials.zeroize()


if __name__ == "__main__":
    sys.exit(main())
