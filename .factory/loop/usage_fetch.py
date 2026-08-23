#!/usr/bin/env python3
"""Fetch child of the hidden Ollama usage guard (Task 7; QUOTA-02, §10).

This module is the *only* process that ever holds the Ollama cookie beyond
the guard parent, and it receives it exclusively on its private stdin (fd
0) — never through argv or environment.  Its argv is fully structural
(interpreter, the absolute ``.factory/loop/usage_fetch.py`` path, non-secret
settings URL / fixture path) and its environment is built from the guard's
documented allowlist, so ``/proc/<pid>/cmdline`` and
``/proc/<pid>/environ`` never carry credential material (§22 test 24).

Behavior:

* read a bounded cookie string from stdin (zeroized after use);
* either parse a saved settings HTML fixture (``--html-file``, no network)
  or fetch the settings page over HTTP(S) with :mod:`http.client` (stdlib);
* classify the page with the retained parse path, redacting every
  credential token from every printed field;
* print exactly one bounded tab-separated classification line and exit 0;
  exit 3 on transient network/server failure; exit 2 on any internal or
  fatal error.  Raw page bytes never leave the child.
"""

from __future__ import annotations

import argparse
import http.client
import ssl
import sys
import urllib.parse
from typing import List, Optional, Tuple

# Bounded same-origin redirect policy (Task 7 review, obligation 6): the
# fetch child follows only a finite number of redirect hops, and each hop
# must stay on the exact same origin (scheme, host, port) as the original
# URL.  Any unresolvable redirect (missing Location, redirect loop, hop
# exhaustion, cross-origin or cross-scheme target) and any 3xx final
# status is a fatal authentication failure (exit 2), never a transient
# retry.  401/403 responses are fatal authentication failures; other
# 4xx/5xx responses remain transient (network/server failure).
MAX_REDIRECT_HOPS = 5

# HTTPS-only with an explicit loopback test seam (Task 7 review, obligation
# 9): the *requested* URL must be ``https://`` or the explicit
# ``127.0.0.1``/``localhost`` loopback seam used exclusively by the hermetic
# hidden suite.  Redirect targets must additionally stay same-origin, so a
# redirect can never upgrade or downgrade the scheme.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

try:  # package-import mode (the hidden control-plane package)
    from .usage import (
        EXIT_FATAL,
        EXIT_TRANSIENT,
        MAX_COOKIE_BYTES,
        MAX_HTML_BYTES,
        classify_html,
        read_secure_file,
        redact,
        redaction_tokens,
    )
except ImportError:  # flat-import mode used by the hidden harness test suite
    from usage import (  # type: ignore[no-redef]
        EXIT_FATAL,
        EXIT_TRANSIENT,
        MAX_COOKIE_BYTES,
        MAX_HTML_BYTES,
        classify_html,
        read_secure_file,
        redact,
        redaction_tokens,
    )


def _read_bounded_cookie(stream: object, maximum: int) -> bytearray:
    """Read the bounded cookie from stdin; the caller zeroizes the buffer."""
    buffer = bytearray()
    while True:
        chunk = stream.read(min(65536, maximum - len(buffer) + 1))
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > maximum:
            raise ValueError(f"cookie exceeds the {maximum}-byte bound")
    return buffer


def _validate_cookie_for_header(cookie: str) -> None:
    """Reject a legacy CRLF / embedded-control cookie (header-injection).

    Defense-in-depth for the parent's validation (Task 7 review, obligation
    11): the cookie never reaches the HTTP request when it carries CR, LF,
    or any other control byte.
    """
    for index, char in enumerate(cookie):
        if ord(char) < 0x20 or ord(char) == 0x7F:
            raise ValueError(
                f"cookie contains a control character at offset {index}"
            )


def _origin_of(url: str) -> Optional[Tuple[str, str, str]]:
    """The normalized ``(scheme, host, port)`` origin of a URL."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    scheme = (parsed.scheme or "").lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else (80 if scheme == "http" else 0)
    return scheme, host, port


def _validate_request_url(url: str) -> None:
    """HTTPS-only plus the explicit loopback seam (defense-in-depth)."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        raise ValueError(f"invalid settings URL {url!r}")
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https":
        return
    if scheme == "http" and host in LOOPBACK_HOSTS:
        return
    raise ValueError(
        f"settings URL {url!r} is not HTTPS; http:// is permitted only for "
        "the explicit loopback seam"
    )


def _fetch_page(url: str, cookie: str, timeout: float) -> Tuple[int, str]:
    """Fetch the settings page with stdlib http.client (bounded response).

    Returns ``(status, body_text)`` where status is 0 on success.  The
    fetch is HTTPS-only (plus the explicit loopback seam), follows only
    bounded same-origin redirects, and classifies:

    * 0 — a final 2xx body;
    * :data:`EXIT_TRANSIENT` (3) — network/server error, timeout, or a
      non-401/403 4xx/5xx response;
    * :data:`EXIT_FATAL` (2) — any unresolvable redirect (missing Location,
      loop, hop exhaustion, cross-origin/cross-scheme target), any 3xx final
      status, and any 401/403 response.
    """
    current = url
    for _hop in range(MAX_REDIRECT_HOPS + 1):
        try:
            _validate_request_url(current)
            parsed = urllib.parse.urlsplit(current)
        except ValueError:
            return EXIT_FATAL, ""
        host = parsed.hostname or ""
        port = parsed.port
        if parsed.scheme == "https":
            connection_cls = http.client.HTTPSConnection
            kwargs: dict = {"context": ssl.create_default_context()}
            if port is None:
                port = 443
        elif parsed.scheme == "http":
            connection_cls = http.client.HTTPConnection
            kwargs = {}
            if port is None:
                port = 80
        else:
            return EXIT_FATAL, ""
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            connection = connection_cls(host, port, timeout=timeout, **kwargs)
            connection.request(
                "GET",
                path,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "Gecko/20100101 Firefox/140.0"
                    ),
                    "Cookie": cookie,
                    "Accept": "text/html,application/xhtml+xml",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            status_code = response.status
            location = response.getheader("Location")
            if status_code in (301, 302, 303, 307, 308):
                body = response.read(1)  # drain (bounded)
                connection.close()
                if not location:
                    # An unresolvable redirect (no Location) is fatal.
                    return EXIT_FATAL, ""
                target = urllib.parse.urljoin(current, location)
                if _origin_of(target) != _origin_of(current):
                    # Cross-origin or cross-scheme redirect: fatal.
                    return EXIT_FATAL, ""
                current = target
                continue
            if status_code in (401, 403):
                connection.close()
                # Authentication is broken: fatal, never a transient retry.
                return EXIT_FATAL, ""
            if status_code >= 400:
                connection.close()
                return EXIT_TRANSIENT, ""
            if status_code >= 300:
                # Any other 3xx final status (e.g. a redirect without a
                # followable target) is an unresolvable redirect: fatal.
                connection.close()
                return EXIT_FATAL, ""
            body = response.read(MAX_HTML_BYTES + 1)
            connection.close()
        except (OSError, http.client.HTTPException, ssl.SSLError):
            return EXIT_TRANSIENT, ""
        return 0, body.decode("utf-8", "replace")[:MAX_HTML_BYTES]
    # Hop exhaustion: fatal (unresolvable redirect).
    return EXIT_FATAL, ""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--settings-url")
    parser.add_argument("--html-file")
    parser.add_argument("--cookie-max", type=int, default=MAX_COOKIE_BYTES)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    if not args.settings_url and not args.html_file:
        return EXIT_FATAL

    try:
        cookie_buffer = _read_bounded_cookie(sys.stdin.buffer, args.cookie_max)
    except ValueError:
        return EXIT_FATAL
    cookie_text = cookie_buffer.decode("utf-8", "replace").strip()
    try:
        _validate_cookie_for_header(cookie_text)
    except ValueError:
        # Header-injection class cookie: never reaches an HTTP request.
        for index in range(len(cookie_buffer)):
            cookie_buffer[index] = 0
        del cookie_buffer
        return EXIT_FATAL
    # Defense-in-depth for the parent's fail-closed pre-fetch gate: a
    # *network* fetch with no cookie can never be attempted, even if the
    # parent were bypassed.
    if not args.html_file and not cookie_text:
        return EXIT_FATAL
    tokens = redaction_tokens(cookie_buffer)
    # The cookie buffer is no longer needed: zeroize the owned copy.
    for index in range(len(cookie_buffer)):
        cookie_buffer[index] = 0
    del cookie_buffer

    try:
        if args.html_file:
            raw = read_secure_file(
                args.html_file,
                max_bytes=MAX_HTML_BYTES,
                label="HTML fixture",
                check_owner_mode=False,
            )
            page_text = raw.decode("utf-8", "replace")
        else:
            status, page_text = _fetch_page(
                args.settings_url, cookie_text, args.timeout
            )
            if status != 0:
                # Preserve the exact documented class: ``_fetch_page`` returns
                # EXIT_FATAL for an unresolvable/cross-origin redirect, a 3xx
                # final status, and any 401/403, and EXIT_TRANSIENT only for
                # a network/server error — never collapse a fatal outcome into
                # a transient one (Task 7 review, obligation 6), or a broken
                # authentication would retry forever instead of failing the
                # campaign.
                return status
    except Exception:
        # Any internal or fatal failure (unsafe/unreadable fixture, ...):
        # nothing is printed, the parent maps this to the fatal class.
        return EXIT_FATAL

    kind, session, weekly, extra = classify_html(page_text)
    if kind == "usage":
        print(f"usage\t{session}\t{weekly}\t{redact(extra, tokens)}")
    elif kind == "auth":
        print(f"auth\t\t\t{redact(extra, tokens)}")
    else:
        print(f"parse\t{session}\t{weekly}\t{redact(extra, tokens)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
