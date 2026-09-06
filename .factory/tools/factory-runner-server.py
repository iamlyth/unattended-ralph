#!/usr/bin/env python3
"""Unprivileged ForcedCommand trampoline for the root runner broker.

This process deliberately has no protocol parser and no signing surface.  The
SSH account can invoke exactly one root-owned broker through the single
sudoers command installed by the deployment bundle.  Stdin/stdout remain the
byte-exact broker protocol.  Production never imports code from the candidate
archive here.
"""
from __future__ import annotations
import os
import sys

# Canonical support-module location for execution through compatibility links.
BUNDLE_PATH = "/usr/local/libexec/factory-runner-v2.bundle"
SUDO = "/usr/bin/sudo"
BROKER = "/usr/local/libexec/factory-runner-broker"
SAFE_ENV = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"}


def main() -> int:
    if os.getuid() == 0 or os.geteuid() == 0 or os.getuid() != os.geteuid():
        print("factory-runner-server: dedicated unprivileged SSH identity required", file=sys.stderr)
        return 1
    if os.environ.get("SSH_ORIGINAL_COMMAND") != "factory-runner-v2":
        print("factory-runner-server: obsolete or non-canonical ForcedCommand", file=sys.stderr)
        return 1
    # No caller argument, environment value, pathname, manifest, nonce, or
    # candidate byte can influence this argv.
    os.execve(SUDO, [SUDO, "-n", BROKER], SAFE_ENV)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
