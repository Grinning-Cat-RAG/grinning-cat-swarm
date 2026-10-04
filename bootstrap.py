#!/usr/bin/env python3
"""
bootstrap.py
------------
Two roles, with the same image as the replicas (standard or hardened/DHI):

- `bootstrap.py` — one-shot init of the replicated stack, run as root before
  the replicas start: seeds the virtual environment shared by all the
  grinning-cat-core replicas with the one shipped in the image, gives the
  shared volumes to the image's user, applies the Redis migrations;
- `bootstrap.py exec -- <command>` — entrypoint of every replica: holds a
  shared flock on the shared venv, then becomes <command> (the Cat).

The replicas share a single `.venv` (named volume) because the core records the
installation of a plugin's dependencies in `cat/plugins/<id>/.requirements_hash`,
on the plugins volume shared by all the replicas: once a replica has installed
them, the others take the fast path and install nothing. The dependencies must
therefore land in a venv every replica sees.

A named volume is populated by Docker only while it is empty, so a new image
would keep running on the venv of the old one. This script compares the
fingerprint of the image's venv (the installed distributions and the Python
version) with the one recorded in the shared venv and, when they differ:

1. removes the marker, so an interrupted run is redone at the next start;
2. empties the shared venv and copies the image's venv into it;
3. removes the `.requirements_hash` of every installed plugin: their
   dependencies were in the replaced venv, and the core reinstalls them
   (pyproject.toml + uv, constrained to the root uv.lock) when it activates
   the plugin;
4. gives the whole shared venv to the owner of the image's venv;
5. writes the marker: it certifies a complete copy, with its ownership.

Plugin dependencies are not installed here: the core does it on activation,
serialized across the replicas by a flock on the plugins volume.

The replicas run as the owner of the image's venv (root in the standard image,
the unprivileged `nonroot` in the hardened one) and must write in the shared
venv (plugin dependencies), plugins and data volumes: the bootstrap gives a
reseeded venv to that owner, and the plugins and data volumes too when their
root belongs to someone else (e.g. after switching from one image to the other).
Ownership changes go bottom-up, the root last, so an interrupted change is
detected and redone by the next run.

Then it applies the Redis migrations, once, before any replica starts: the
replicas do not run them.

Every replica holds a shared flock on `.grinning-cat-seed.lock` for its whole
life (`exec`: the lock is taken before the Cat starts and inherited by it; a
replica refuses to start on a venv without the marker, i.e. whose reseed was
interrupted). The bootstrap checks the marker under
a shared lock too, and reseeds only if it gets the exclusive one without
waiting: it never empties the venv under a running replica, nor while a replica
is installing a plugin's dependencies into it. With replicas running on an
outdated venv it fails, asking to stop them first.

Exit codes:
- 0 shared venv up to date (seeded now or already) and migrations applied
- 1 the shared venv could not be seeded, replicas are running on it, or the
  migrations failed; for `exec`, the shared venv is not seeded
- 2 usage error
"""

import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE_VENV = Path(os.getenv("CAT_BOOTSTRAP_SOURCE_VENV", "/app/.venv"))
SHARED_VENV = Path(os.getenv("CAT_BOOTSTRAP_SHARED_VENV", "/shared/venv"))
PLUGINS_DIR = Path(os.getenv("CAT_BOOTSTRAP_PLUGINS_PATH", "/app/cat/plugins"))
DATA_DIR = Path(os.getenv("CAT_BOOTSTRAP_DATA_PATH", "/app/data"))
APP_DIR = Path(os.getenv("CAT_BOOTSTRAP_APP_PATH", "/app"))

MARKER_NAME = ".grinning-cat-seed"
LOCK_NAME = ".grinning-cat-seed.lock"
REQUIREMENTS_HASH_NAME = ".requirements_hash"


def fingerprint(venv: Path) -> str:
    """Digest of the distributions installed in *venv* and of its Python version."""
    h = hashlib.sha256()
    cfg = venv / "pyvenv.cfg"
    if cfg.is_file():
        h.update(cfg.read_bytes())
    for dist_info in sorted(str(p.relative_to(venv)) for p in venv.glob("lib/python*/site-packages/*.dist-info")):
        h.update(dist_info.encode())
        h.update(b"\0")
    return h.hexdigest()


def read_marker(shared: Path) -> str | None:
    try:
        return (shared / MARKER_NAME).read_text().strip() or None
    except FileNotFoundError:
        return None


def write_marker(shared: Path, value: str) -> None:
    """Atomic: a crash leaves either no marker or the complete one."""
    tmp = shared / f"{MARKER_NAME}.tmp"
    tmp.write_text(value)
    os.replace(tmp, shared / MARKER_NAME)


def empty_dir(path: Path, keep: set[str]) -> None:
    for entry in path.iterdir():
        if entry.name in keep:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def invalidate_plugin_requirements(plugins_dir: Path) -> list[str]:
    """Remove the installation records of the plugins; return the ids touched."""
    if not plugins_dir.is_dir():
        return []
    invalidated = []
    for hash_file in sorted(plugins_dir.glob(f"*/{REQUIREMENTS_HASH_NAME}")):
        hash_file.unlink()
        invalidated.append(hash_file.parent.name)
    return invalidated


def owner_of(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_uid, st.st_gid


chown = os.lchown


def align_ownership(path: Path, uid: int, gid: int, force: bool = False, chown=None) -> bool:
    """Give *path* and everything below it (symlinks themselves, never their targets) to uid:gid, when its root
    belongs to someone else or when *force*d; return True if anything was changed.

    Bottom-up, the root last: an interrupted run leaves the root to its old owner, so the next run walks it again."""
    chown = chown or globals()["chown"]
    if not path.exists():
        return False
    if not force and owner_of(path) == (uid, gid):
        return False
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files + dirs:
            chown(os.path.join(root, name), uid, gid)
    chown(path, uid, gid)
    return True


def run_migrations() -> None:
    result = subprocess.run([sys.executable, "migrations/manage_migrations.py", "upgrade", "head"], cwd=APP_DIR)
    if result.returncode != 0:
        raise RuntimeError(f"migrations failed (exit code {result.returncode})")


def seed(source: Path, shared: Path, plugins_dir: Path) -> bool:
    """Bring *shared* in line with *source*, owned by the owner of *source*; return True if it had to be reseeded.

    The marker is written last: it certifies a complete copy, with its ownership."""
    if not (source / "pyvenv.cfg").is_file():
        raise RuntimeError(f"'{source}' is not a virtual environment")

    expected = fingerprint(source)
    if read_marker(shared) == expected:
        return False

    (shared / MARKER_NAME).unlink(missing_ok=True)
    empty_dir(shared, keep={LOCK_NAME})
    shutil.copytree(source, shared, symlinks=True, dirs_exist_ok=True)
    invalidated = invalidate_plugin_requirements(plugins_dir)
    if invalidated:
        print(f"[bootstrap] Plugin dependencies to reinstall on activation: {invalidated}")
    align_ownership(shared, *owner_of(source), force=True)
    write_marker(shared, expected)
    return True


class ReplicasRunning(Exception):
    pass


def main() -> int:
    print(f"[bootstrap] Checking the shared venv '{SHARED_VENV}' against the image's '{SOURCE_VENV}'...")
    try:
        SHARED_VENV.mkdir(parents=True, exist_ok=True)
        with open(SHARED_VENV / LOCK_NAME, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_SH)
            reseeded = False
            if read_marker(SHARED_VENV) != fingerprint(SOURCE_VENV):
                try:
                    fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ReplicasRunning(
                        "the shared venv must be reseeded, but replicas (or another bootstrap) are using it: "
                        "stop the grinning-cat-core replicas first (docker compose stop grinning-cat-core)"
                    )
                reseeded = seed(SOURCE_VENV, SHARED_VENV, PLUGINS_DIR)
            uid, gid = owner_of(SOURCE_VENV)
            align_ownership(PLUGINS_DIR, uid, gid)
            align_ownership(DATA_DIR, uid, gid)
        print("[bootstrap] Shared venv seeded from the image." if reseeded else "[bootstrap] Shared venv already up to date.")
        print("[bootstrap] Applying the Redis migrations...")
        run_migrations()
    except Exception as e:
        print(f"[bootstrap] ERROR: {e}")
        return 1

    return 0


def replica_exec(argv: list[str]) -> int:
    """Hold a shared lock on the shared venv, then become *argv*: the lock lives as long as the replica."""
    try:
        fd = os.open(SHARED_VENV / LOCK_NAME, os.O_RDONLY)
    except FileNotFoundError:
        print(f"[bootstrap] ERROR: the shared venv '{SHARED_VENV}' is not seeded: run grinning-cat-bootstrap first")
        return 1
    fcntl.flock(fd, fcntl.LOCK_SH)  # waits for a reseed in progress
    if read_marker(SHARED_VENV) is None:  # an interrupted reseed: the lock survives, the marker does not
        print(f"[bootstrap] ERROR: the shared venv '{SHARED_VENV}' is not seeded: run grinning-cat-bootstrap first")
        return 1
    os.set_inheritable(fd, True)
    os.execvp(argv[0], argv)


USAGE = "usage: bootstrap.py | bootstrap.py exec [--] <command> [args...]"


def cli(argv: list[str]) -> int:
    if not argv:
        return main()
    if argv[0] == "exec":
        command = argv[2:] if argv[1:2] == ["--"] else argv[1:]
        if command:
            return replica_exec(command)
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
