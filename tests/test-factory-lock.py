#!/usr/bin/env python3
"""Adversarial checks for the repository-root factory lifecycle flock."""

from __future__ import annotations

import fcntl
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parent.parent
ENV_KEYS = ("FACTORY_LOCK_HELD", "FACTORY_LOCK_FD", "FACTORY_LOCK_ID", "FACTORY_LOCK_ROOT")


def run(command: list[str], root: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=root, text=True, capture_output=True)
    if check and result.returncode:
        raise AssertionError((command, result.returncode, result.stdout, result.stderr))
    return result


def make_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="factory-lock-test."))
    (root / "scripts").mkdir()
    for name in ("factory-lock-exec.py", "factory_lock.py", "factory_state_io.py", "ralph-campaign-state.py", "factory-lock.sh"):
        shutil.copy2(SOURCE / "scripts" / name, root / "scripts" / name)
    (root / ".gitignore").write_text(".factory-state/\n.factory-lock\n", encoding="utf-8")
    (root / "tracked").write_text("test\n", encoding="utf-8")
    run(["git", "init", "-q", "-b", "develop"], root)
    run(["git", "config", "user.name", "test"], root)
    run(["git", "config", "user.email", "test@example.invalid"], root)
    run(["git", "add", "."], root)
    run(["git", "commit", "-qm", "base"], root)
    return root


def test_root_flock_drop_and_untrusted_background_child() -> None:
    root = make_repo()
    try:
        legacy = root / ".factory-lock"
        legacy.write_text("legacy\n", encoding="utf-8")
        legacy.chmod(0o600)
        inspect = root / "inspect.py"
        inspect.write_text(
            """import fcntl, os, pathlib, subprocess, sys
root=pathlib.Path(sys.argv[1])
keys=('FACTORY_LOCK_HELD','FACTORY_LOCK_FD','FACTORY_LOCK_ID','FACTORY_LOCK_ROOT')
assert all(key not in os.environ for key in keys)
root_info=root.stat()
for item in pathlib.Path('/proc/self/fd').iterdir():
    try: info=os.stat(item)
    except OSError: continue
    assert (info.st_dev,info.st_ino)!=(root_info.st_dev,root_info.st_ino)
# LOCK_UN on an independently opened root FD cannot unlock the lifecycle
# parent's open-file description.
separate=os.open(root, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
fcntl.flock(separate, fcntl.LOCK_UN)
check=subprocess.run([str(root/'scripts/factory-lock-exec.py'),str(root),'--check'],capture_output=True)
assert check.returncode != 0
contender=subprocess.run([str(root/'scripts/factory-lock-exec.py'),str(root),'--','true'],capture_output=True)
assert contender.returncode != 0
# A background descendant receives only this independent FD. It cannot retain,
# unlock, or invoke a lock-authorized campaign-state helper.
pid=os.fork()
if pid==0:
    fcntl.flock(separate, fcntl.LOCK_UN)
    state=subprocess.run([str(root/'scripts/ralph-campaign-state.py'),'show'],capture_output=True)
    os._exit(0 if state.returncode != 0 else 91)
_,status=os.waitpid(pid,0)
assert os.waitstatus_to_exitcode(status)==0
os.close(separate)
print('untrusted-clean')
""",
            encoding="utf-8",
        )
        probe = root / "probe.sh"
        probe.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=${1:?repository root required}
helper=$SCRIPT_DIR/scripts/factory-lock-exec.py
# shellcheck source=scripts/factory-lock.sh
source "$SCRIPT_DIR/scripts/factory-lock.sh"
factory_lock_bootstrap "$root" bash "$0" "$@"
python3 "$helper" "$root" --check
fd=${FACTORY_LOCK_FD:?}
[[ $fd =~ ^[0-9]+$ ]] && (( fd >= 3 ))
[[ -e /proc/self/fd/$fd ]]
[[ ${FACTORY_LOCK_ROOT:?} == "$root" ]]
# The already-loaded shell function drops every lock descriptor and variable
# before any mutable workspace executable runs.
factory_lock_run_untrusted python3 "$root/inspect.py" "$root"
# The trusted parent retained its descriptor and authority.
[[ -e /proc/self/fd/$fd ]]
python3 "$helper" "$root" --check
# Rebind the repository pathname (the bwrap bind-mount analog): the retained
# descriptor pins the original root inode, the untrusted leaf still receives
# nothing, and the parent fails closed on pathname drift.
parent=$(dirname -- "$root")
name=$(basename -- "$root")
renamed="$parent/root.renamed.$$"
mv "$root" "$renamed"
mkdir "$parent/$name"
trap 'rmdir "$parent/$name" 2>/dev/null || true; mv "$renamed" "$root" 2>/dev/null || true' EXIT
helper="$renamed/scripts/factory-lock-exec.py"
factory_lock_run_untrusted python3 "$renamed/inspect.py" "$renamed"
# The parent's open-file description still owns the original inode after the
# pathname rebind; a fresh lock on the renamed root must contend.
python3 - "$renamed" <<'PY'
import fcntl, os, sys
root = sys.argv[1]
descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    os.close(descriptor)
    raise SystemExit(0)
os.close(descriptor)
raise SystemExit('lock was lost during rebind')
PY
set +e
python3 "$helper" "$root" --check
rebound_rc=$?
set -e
[[ $rebound_rc -ne 0 ]] || { echo "rebound root pathname still authorized the lock" >&2; exit 44; }
rmdir "$parent/$name"
mv "$renamed" "$root"
trap - EXIT
python3 "$root/scripts/factory-lock-exec.py" "$root" --check
""",
            encoding="utf-8",
        )
        probe.chmod(0o755)
        result = run(
            [str(root / "scripts/factory-lock-exec.py"), str(root), "--", str(probe), str(root)],
            root,
        )
        assert "untrusted-clean" in result.stdout
        assert not legacy.exists(), "legacy pathname was not removed after dual-lock migration"
        # No replaceable lock pathname is created. A later owner locks the same
        # canonical root inode and leaves the repository namespace unchanged.
        before = (root.stat().st_dev, root.stat().st_ino)
        run([str(root / "scripts/factory-lock-exec.py"), str(root), "--", "true"], root)
        assert (root.stat().st_dev, root.stat().st_ino) == before
    finally:
        shutil.rmtree(root)


def test_legacy_holder_and_unsafe_legacy_fail_closed() -> None:
    root = make_repo()
    try:
        helper = root / "scripts/factory-lock-exec.py"
        legacy = root / ".factory-lock"
        legacy.write_text("held\n", encoding="utf-8")
        legacy.chmod(0o600)
        with legacy.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            rejected = run([str(helper), str(root), "--", "true"], root, check=False)
            assert rejected.returncode != 0 and "legacy factory lock is still held" in rejected.stderr
            assert legacy.is_file()

        legacy.unlink()
        external = root / "external"
        external.write_text("unchanged\n", encoding="utf-8")
        legacy.symlink_to(external)
        rejected = run([str(helper), str(root), "--", "true"], root, check=False)
        assert rejected.returncode != 0
        assert external.read_text(encoding="utf-8") == "unchanged\n"
        legacy.unlink()
        os.link(external, legacy)
        rejected = run([str(helper), str(root), "--", "true"], root, check=False)
        assert rejected.returncode != 0
        assert external.read_text(encoding="utf-8") == "unchanged\n"
        legacy.unlink()
    finally:
        shutil.rmtree(root)


def test_legacy_substitution_after_final_check_is_not_unlinked() -> None:
    root = make_repo()
    try:
        spec = importlib.util.spec_from_file_location("factory_lock_race", root / "scripts/factory_lock.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        legacy = root / ".factory-lock"
        legacy.write_text("original\n", encoding="utf-8")
        legacy.chmod(0o600)
        replacement = root / "replacement"
        replacement.write_text("replacement\n", encoding="utf-8")
        replacement.chmod(0o600)
        def replace_after_final_check() -> None:
            legacy.unlink()
            replacement.rename(legacy)

        root_fd = module._open_root(root)
        try:
            fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                module._migrate_root_legacy(
                    root_fd, after_final_check=replace_after_final_check,
                )
            except module.FactoryLockError as exc:
                assert "replaced at quarantine" in str(exc)
            else:
                raise AssertionError("post-check legacy substitution was accepted")
        finally:
            os.close(root_fd)
        quarantines = list(root.glob("..factory-lock.quarantine-*"))
        assert len(quarantines) == 1
        assert quarantines[0].read_text(encoding="utf-8") == "replacement\n"
    finally:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        shutil.rmtree(root)


def main() -> None:
    test_root_flock_drop_and_untrusted_background_child()
    test_legacy_holder_and_unsafe_legacy_fail_closed()
    test_legacy_substitution_after_final_check_is_not_unlinked()
    print("test: repository-root factory lock checks passed")


if __name__ == "__main__":
    main()
