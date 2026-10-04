import fcntl
import os
import runpy
import shutil
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap  # noqa: E402

BOOTSTRAP_FILE = Path(bootstrap.__file__)
SITE = "lib/python3.13/site-packages"


def make_venv(path: Path, dists: set[str], python: str = "3.13.1") -> None:
    if path.exists():
        shutil.rmtree(path)
    (path / SITE).mkdir(parents=True)
    (path / "pyvenv.cfg").write_text(f"version_info = {python}\n")
    (path / "bin").mkdir()
    (path / "bin" / "python").symlink_to("/usr/local/bin/python3.13")
    for dist in dists:
        (path / SITE / f"{dist}.dist-info").mkdir()
        (path / SITE / f"{dist}.dist-info" / "METADATA").write_text(dist)


def dist_infos(venv: Path) -> set[str]:
    return {p.name.removesuffix(".dist-info") for p in (venv / SITE).glob("*.dist-info")} if (venv / SITE).is_dir() else set()


def tree(path: Path) -> dict[str, str]:
    out = {}
    for p in sorted(path.rglob("*")):
        rel = str(p.relative_to(path))
        if p.is_symlink():
            out[rel] = f"-> {os.readlink(p)}"
        elif p.is_file():
            out[rel] = p.read_text()
        else:
            out[rel] = "<dir>"
    return out


@pytest.fixture
def dirs(tmp_path):
    source, shared, plugins = tmp_path / "image-venv", tmp_path / "shared", tmp_path / "plugins"
    make_venv(source, {"fastapi-1.0", "redis-5.0"})
    shared.mkdir()
    plugins.mkdir()
    return source, shared, plugins


# ---------------------------------------------------------------- fingerprint

def test_fingerprint_changes_with_distributions_and_python(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    make_venv(a, {"x-1.0"})
    make_venv(b, {"x-1.1"})
    make_venv(c, {"x-1.0"}, python="3.13.2")
    assert len({bootstrap.fingerprint(a), bootstrap.fingerprint(b), bootstrap.fingerprint(c)}) == 3


def test_fingerprint_ignores_files_that_are_not_distributions(tmp_path):
    a = tmp_path / "a"
    make_venv(a, {"x-1.0"})
    before = bootstrap.fingerprint(a)
    (a / SITE / "x").mkdir()
    (a / SITE / "x" / "__pycache__").mkdir()
    assert bootstrap.fingerprint(a) == before


def test_fingerprint_without_pyvenv_cfg(tmp_path):
    v = tmp_path / "v"
    make_venv(v, {"x-1.0"})
    with_cfg = bootstrap.fingerprint(v)
    (v / "pyvenv.cfg").unlink()
    assert bootstrap.fingerprint(v) != with_cfg


# --------------------------------------------------------------------- marker

def test_read_marker_missing_empty_and_present(tmp_path):
    assert bootstrap.read_marker(tmp_path) is None
    (tmp_path / bootstrap.MARKER_NAME).write_text("  \n")
    assert bootstrap.read_marker(tmp_path) is None
    bootstrap.write_marker(tmp_path, "abc")
    assert bootstrap.read_marker(tmp_path) == "abc"
    assert not (tmp_path / f"{bootstrap.MARKER_NAME}.tmp").exists()


# ------------------------------------------------------------------ empty_dir

def test_empty_dir_keeps_only_the_given_names(tmp_path):
    (tmp_path / "keep").write_text("k")
    (tmp_path / "file").write_text("f")
    (tmp_path / "dir" / "sub").mkdir(parents=True)
    target = tmp_path.parent / f"{tmp_path.name}-outside"
    target.mkdir()
    (target / "precious").write_text("p")
    (tmp_path / "link").symlink_to(target)
    bootstrap.empty_dir(tmp_path, keep={"keep"})
    assert [p.name for p in tmp_path.iterdir()] == ["keep"]
    assert (target / "precious").read_text() == "p"  # a symlink is unlinked, never followed


# ----------------------------------------------------------------- invalidate

def test_invalidate_without_plugins_dir(tmp_path):
    assert bootstrap.invalidate_plugin_requirements(tmp_path / "missing") == []


def test_invalidate_removes_only_the_requirements_hashes(tmp_path):
    for plugin in ("b", "a", "c"):
        (tmp_path / plugin).mkdir()
        (tmp_path / plugin / "pyproject.toml").write_text("")
    (tmp_path / "a" / ".requirements_hash").write_text("h")
    (tmp_path / "b" / ".requirements_hash").write_text("h")
    (tmp_path / "b" / "uv.lock").write_text("l")
    assert bootstrap.invalidate_plugin_requirements(tmp_path) == ["a", "b"]
    assert not list(tmp_path.glob("*/.requirements_hash"))
    assert (tmp_path / "b" / "uv.lock").exists()
    assert (tmp_path / "c" / "pyproject.toml").exists()


# ----------------------------------------------------------------------- seed

def test_seed_rejects_a_source_that_is_not_a_venv(dirs, tmp_path):
    _, shared, plugins = dirs
    with pytest.raises(RuntimeError, match="not a virtual environment"):
        bootstrap.seed(tmp_path / "nothing", shared, plugins)


def test_seed_copies_the_image_venv_and_invalidates_the_plugins(dirs, capsys):
    source, shared, plugins = dirs
    (shared / "stale").write_text("old venv")
    (shared / bootstrap.LOCK_NAME).write_text("")
    (plugins / "p").mkdir()
    (plugins / "p" / ".requirements_hash").write_text("h")

    assert bootstrap.seed(source, shared, plugins) is True

    expected = tree(source)
    actual = tree(shared)
    assert actual.pop(bootstrap.MARKER_NAME) == bootstrap.fingerprint(source)
    assert actual.pop(bootstrap.LOCK_NAME) == ""
    assert actual == expected
    assert (shared / "bin" / "python").is_symlink()
    assert not (plugins / "p" / ".requirements_hash").exists()
    assert "['p']" in capsys.readouterr().out


def test_seed_without_plugins_to_invalidate_is_silent(dirs, capsys):
    source, shared, plugins = dirs
    assert bootstrap.seed(source, shared, plugins) is True
    assert capsys.readouterr().out == ""


def test_seed_is_a_noop_when_the_marker_matches(dirs):
    source, shared, plugins = dirs
    bootstrap.seed(source, shared, plugins)
    (shared / SITE / "plugin_dep-1.0.dist-info").mkdir()  # installed by a replica
    (plugins / "p").mkdir()
    (plugins / "p" / ".requirements_hash").write_text("h")
    before = tree(shared)

    assert bootstrap.seed(source, shared, plugins) is False
    assert tree(shared) == before
    assert (plugins / "p" / ".requirements_hash").exists()


def test_seed_replaces_the_venv_of_an_older_image(dirs):
    source, shared, plugins = dirs
    bootstrap.seed(source, shared, plugins)
    (shared / SITE / "plugin_dep-1.0.dist-info").mkdir()
    make_venv(source, {"fastapi-2.0", "redis-5.0"})

    assert bootstrap.seed(source, shared, plugins) is True
    assert dist_infos(shared) == {"fastapi-2.0", "redis-5.0"}


# ----------------------------------------------------------------------- main

ROOT = (0, 0)
IMAGE_OWNER = (65532, 65532)


class Ownership:
    """Owners of the files as the bootstrap sees them, since the tests cannot chown for real: `chown` applies to this
    model, `owner_of` reads it. A file the model has not seen, or that changed identity since (deleted and created
    again: new inode or ctime; for a directory the inode only, as adding entries changes its ctime), belongs to
    root — the bootstrap's user."""

    def __init__(self, fail_after: int | None = None):
        self.owners: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {}
        self.order: list[str] = []
        self.fail_after = fail_after  # crash after that many chowns
        self.fired = False

    @staticmethod
    def _identity(path) -> tuple[int, int]:
        st_ = os.lstat(path)
        return st_.st_ino, 0 if stat.S_ISDIR(st_.st_mode) else st_.st_ctime_ns

    def owner_of(self, path) -> tuple[int, int]:
        owner, identity = self.owners.get(str(path), (ROOT, None))
        return owner if identity == self._identity(path) else ROOT

    def chown(self, path, uid, gid) -> None:
        if self.fail_after is not None and len(self.order) >= self.fail_after:
            self.fired = True
            raise Crash("chown")
        self.owners[str(path)] = ((uid, gid), self._identity(path))
        self.order.append(str(path))

    def give(self, path: Path, owner=IMAGE_OWNER) -> None:
        """*path* and its tree were created by *owner* (e.g. by a replica)."""
        for q in [path, *path.rglob("*")]:
            self.owners[str(q)] = (owner, self._identity(q))

    def not_owned_by(self, root: Path, owner=IMAGE_OWNER, skip=()) -> set[str]:
        return {str(q) for q in [root, *root.rglob("*")] if q.name not in skip and self.owner_of(q) != owner}


class Crash(Exception):
    pass


def migrations_stub(app: Path, exit_code: int = 0) -> Path:
    """An app folder whose migrations script records its arguments and exits with *exit_code*."""
    (app / "migrations").mkdir(parents=True, exist_ok=True)
    (app / "migrations" / "manage_migrations.py").write_text(
        "import sys, pathlib\n"
        "pathlib.Path('migrated').write_text(' '.join(sys.argv[1:]))\n"
        f"sys.exit({exit_code})\n"
    )
    return app


@contextmanager
def configured(source: Path, shared: Path, plugins: Path, data: Path | None = None, app: Path | None = None,
               ownership: Ownership | None = None):
    names = ("SOURCE_VENV", "SHARED_VENV", "PLUGINS_DIR", "DATA_DIR", "APP_DIR", "chown", "owner_of")
    saved = {n: getattr(bootstrap, n) for n in names}
    app = app or migrations_stub(shared.parent / "app")
    if ownership is None:
        ownership = Ownership()
        if source.exists():
            ownership.give(source)
    bootstrap.SOURCE_VENV, bootstrap.SHARED_VENV, bootstrap.PLUGINS_DIR = source, shared, plugins
    bootstrap.DATA_DIR, bootstrap.APP_DIR = data or shared.parent / "data", app
    bootstrap.chown, bootstrap.owner_of = ownership.chown, ownership.owner_of
    try:
        yield
    finally:
        for n, v in saved.items():
            setattr(bootstrap, n, v)


def test_main_seeds_then_reports_up_to_date(dirs, capsys):
    source, shared, plugins = dirs
    with configured(source, shared / "nested", plugins):
        assert bootstrap.main() == 0
        assert "seeded from the image" in capsys.readouterr().out
        assert bootstrap.main() == 0
        assert "already up to date" in capsys.readouterr().out
    assert (shared / "nested" / bootstrap.LOCK_NAME).exists()


def test_main_fails_when_the_venv_cannot_be_seeded(dirs, tmp_path, capsys):
    _, shared, plugins = dirs
    with configured(tmp_path / "nothing", shared, plugins):
        assert bootstrap.main() == 1
    assert "ERROR" in capsys.readouterr().out
    assert bootstrap.read_marker(shared) is None


@contextmanager
def running_replica(shared: Path):
    """A replica: holds a shared lock on the seed lock for its whole life."""
    with open(shared / bootstrap.LOCK_NAME, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield


def test_main_refuses_to_reseed_under_running_replicas(dirs, capsys):
    source, shared, plugins = dirs
    with configured(source, shared, plugins):
        assert bootstrap.main() == 0
        make_venv(source, {"fastapi-2.0"})
        before = tree(shared)
        with running_replica(shared):
            assert bootstrap.main() == 1
        assert tree(shared) == before
        assert "stop the grinning-cat-core replicas" in capsys.readouterr().out
        assert bootstrap.main() == 0  # replicas stopped: reseeds
    assert dist_infos(shared) == {"fastapi-2.0"}


def test_main_with_running_replicas_and_an_up_to_date_venv(dirs, capsys):
    source, shared, plugins = dirs
    with configured(source, shared, plugins):
        assert bootstrap.main() == 0
        with running_replica(shared):
            assert bootstrap.main() == 0
    assert "already up to date" in capsys.readouterr().out


# ------------------------------------------------------------------ ownership

def test_align_ownership_skips_a_tree_already_owned(tmp_path):
    calls = []
    st_ = tmp_path.stat()
    assert bootstrap.align_ownership(tmp_path, st_.st_uid, st_.st_gid, chown=lambda *a: calls.append(a)) is False
    assert calls == []


def test_align_ownership_of_a_missing_path(tmp_path):
    calls = []
    assert bootstrap.align_ownership(tmp_path / "missing", 1, 1, chown=lambda *a: calls.append(a)) is False
    assert calls == []


def test_align_ownership_walks_the_tree_bottom_up_without_following_symlinks(tmp_path):
    (tmp_path / "d" / "e").mkdir(parents=True)
    (tmp_path / "d" / "f").write_text("")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "x").write_text("")
    (tmp_path / "link").symlink_to(outside)
    calls = []
    assert bootstrap.align_ownership(tmp_path, 65532, 65532, chown=lambda *a: calls.append(a)) is True
    paths = [str(c[0]) for c in calls]
    assert sorted(paths) == sorted(str(q) for q in [tmp_path, *tmp_path.rglob("*")])
    assert {c[1:] for c in calls} == {(65532, 65532)}
    assert str(outside / "x") not in paths
    # every directory after its content, the root last: an interrupted run leaves the root to its old owner,
    # so the next run sees the mismatch and walks the tree again
    assert paths[-1] == str(tmp_path)
    assert paths.index(str(tmp_path / "d")) > paths.index(str(tmp_path / "d" / "e"))


def test_align_ownership_forced_on_a_tree_already_owned(tmp_path):
    (tmp_path / "f").write_text("")
    st_ = tmp_path.stat()
    calls = []
    assert bootstrap.align_ownership(tmp_path, st_.st_uid, st_.st_gid, force=True, chown=lambda *a: calls.append(a)) is True
    assert sorted(str(c[0]) for c in calls) == sorted([str(tmp_path), str(tmp_path / "f")])


def test_align_ownership_uses_the_module_chown_by_default(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(bootstrap, "chown", lambda *a: calls.append(a))
    assert bootstrap.align_ownership(tmp_path, 65532, 65532) is True
    assert calls == [(tmp_path, 65532, 65532)]


def test_owner_of(tmp_path):
    st_ = tmp_path.stat()
    assert bootstrap.owner_of(tmp_path) == (st_.st_uid, st_.st_gid)


def test_main_gives_the_reseeded_venv_plugins_and_data_to_the_image_owner(dirs, tmp_path):
    source, shared, plugins = dirs
    data = tmp_path / "data"
    data.mkdir()
    (plugins / "p").mkdir()
    ownership = Ownership()
    ownership.give(source)
    with configured(source, shared, plugins, data=data, ownership=ownership):
        assert bootstrap.main() == 0
    assert ownership.not_owned_by(shared, skip={bootstrap.MARKER_NAME}) == set()
    assert ownership.not_owned_by(plugins) == set()
    assert ownership.not_owned_by(data) == set()


def test_main_gives_the_whole_reseeded_venv_even_if_its_root_already_belongs_to_the_image_owner(dirs):
    source, shared, plugins = dirs
    ownership = Ownership()
    ownership.give(source)
    ownership.give(shared)  # e.g. a venv seeded before, by an older image
    with configured(source, shared, plugins, ownership=ownership):
        assert bootstrap.main() == 0
    assert ownership.not_owned_by(shared, skip={bootstrap.MARKER_NAME}) == set()


def test_main_leaves_ownership_alone_when_it_already_matches(dirs):
    source, shared, plugins = dirs
    ownership = Ownership()
    ownership.give(source)
    with configured(source, shared, plugins, ownership=ownership):
        assert bootstrap.main() == 0
        done = len(ownership.order)
        assert done > 0  # the reseed gave the venv to the image owner
        assert bootstrap.main() == 0
    assert len(ownership.order) == done


def test_main_resumes_an_interrupted_ownership_change(dirs, tmp_path):
    source, shared, plugins = dirs
    data = tmp_path / "data"
    (data / "storage" / "docs").mkdir(parents=True)
    (data / "storage" / "docs" / "a.pdf").write_text("")
    ownership = Ownership()
    ownership.give(source)
    with configured(source, shared, plugins, data=data, ownership=ownership):
        assert bootstrap.main() == 0
        ownership.give(data, ROOT)  # e.g. written by the standard image before switching to the hardened one
        ownership.fail_after = len(ownership.order) + 2
        assert bootstrap.main() == 1
        ownership.fail_after = None
        assert bootstrap.main() == 0
    assert ownership.not_owned_by(data) == set()


# ----------------------------------------------------------------- migrations

def test_main_applies_the_migrations_after_seeding(dirs, tmp_path, capsys):
    source, shared, plugins = dirs
    app = migrations_stub(tmp_path / "app")
    with configured(source, shared, plugins, app=app):
        assert bootstrap.main() == 0
    assert (app / "migrated").read_text() == "upgrade head"
    assert bootstrap.read_marker(shared) == bootstrap.fingerprint(source)


def test_main_fails_when_the_migrations_fail(dirs, tmp_path, capsys):
    source, shared, plugins = dirs
    app = migrations_stub(tmp_path / "app", exit_code=3)
    with configured(source, shared, plugins, app=app):
        assert bootstrap.main() == 1
    assert "migrations failed" in capsys.readouterr().out


def test_main_does_not_migrate_when_it_refuses_to_reseed(dirs, tmp_path):
    source, shared, plugins = dirs
    app = migrations_stub(tmp_path / "app")
    with configured(source, shared, plugins, app=app):
        assert bootstrap.main() == 0
        (app / "migrated").unlink()
        make_venv(source, {"fastapi-2.0"})
        with running_replica(shared):
            assert bootstrap.main() == 1
    assert not (app / "migrated").exists()


# -------------------------------------------------------------- replica exec

class Exec(Exception):
    pass


def test_replica_exec_holds_a_shared_lock_through_the_exec(dirs, monkeypatch):
    source, shared, plugins = dirs
    with configured(source, shared, plugins):
        assert bootstrap.main() == 0
        seen = {}

        def fake_execvp(file, args):
            # at this point the process would become the Cat: the lock must be held, and survive the exec
            fds = [fd for fd in range(3, 256) if _is_lock(fd, shared)]
            seen.update(file=file, args=args, inheritable=[os.get_inheritable(fd) for fd in fds])
            with open(shared / bootstrap.LOCK_NAME) as other:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)  # other replicas still start
            raise Exec

        monkeypatch.setattr(bootstrap.os, "execvp", fake_execvp)
        with pytest.raises(Exec):
            bootstrap.replica_exec(["python", "-m", "cat.main"])
    assert seen == {"file": "python", "args": ["python", "-m", "cat.main"], "inheritable": [True]}


def _is_lock(fd: int, shared: Path) -> bool:
    try:
        return os.readlink(f"/proc/self/fd/{fd}") == str(shared / bootstrap.LOCK_NAME)
    except OSError:
        return False


def test_replica_exec_waits_for_a_reseed_in_progress(dirs, monkeypatch):
    source, shared, plugins = dirs
    with configured(source, shared, plugins):
        assert bootstrap.main() == 0
        monkeypatch.setattr(bootstrap.os, "execvp", lambda *_: (_ for _ in ()).throw(Exec))
        calls = []
        real_flock = fcntl.flock
        monkeypatch.setattr(bootstrap.fcntl, "flock", lambda fd, op: (calls.append(op), real_flock(fd, op)))
        with pytest.raises(Exec):
            bootstrap.replica_exec(["true"])
    assert calls == [fcntl.LOCK_SH]  # blocking: a replica waits for the bootstrap, never skips it


def test_replica_exec_on_an_interrupted_reseed(dirs, capsys, monkeypatch):
    source, shared, plugins = dirs
    monkeypatch.setattr(bootstrap.os, "execvp", lambda *_: pytest.fail("the Cat must not start"))
    with configured(source, shared, plugins):
        assert bootstrap.main() == 0
    (shared / bootstrap.MARKER_NAME).unlink()  # what an interrupted reseed leaves: the lock, no marker
    with configured(source, shared, plugins):
        assert bootstrap.replica_exec(["true"]) == 1
    assert "not seeded" in capsys.readouterr().out


def test_replica_exec_without_a_seeded_venv(dirs, capsys):
    source, shared, plugins = dirs
    with configured(source, shared, plugins):
        assert bootstrap.replica_exec(["true"]) == 1
    assert "not seeded" in capsys.readouterr().out


# ------------------------------------------------------------------------ cli

def test_cli_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(bootstrap, "main", lambda: calls.append("main") or 0)
    monkeypatch.setattr(bootstrap, "replica_exec", lambda argv: calls.append(argv) or 0)
    assert bootstrap.cli([]) == 0
    assert bootstrap.cli(["exec", "--", "python", "-m", "cat.main"]) == 0
    assert bootstrap.cli(["exec", "python"]) == 0
    assert calls == ["main", ["python", "-m", "cat.main"], ["python"]]


@pytest.mark.parametrize("argv", [["exec"], ["exec", "--"], ["other"]])
def test_cli_usage_errors(argv, capsys):
    assert bootstrap.cli(argv) == 2
    assert "usage" in capsys.readouterr().out


def test_script_entry_point_reads_the_environment_and_exits_with_the_result(dirs, tmp_path, monkeypatch):
    source, shared, plugins = dirs
    app = migrations_stub(tmp_path / "app")
    st_ = source.stat()
    monkeypatch.setenv("CAT_BOOTSTRAP_SOURCE_VENV", str(source))
    monkeypatch.setenv("CAT_BOOTSTRAP_SHARED_VENV", str(shared))
    monkeypatch.setenv("CAT_BOOTSTRAP_PLUGINS_PATH", str(plugins))
    monkeypatch.setenv("CAT_BOOTSTRAP_DATA_PATH", str(tmp_path / "data"))
    monkeypatch.setenv("CAT_BOOTSTRAP_APP_PATH", str(app))
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(BOOTSTRAP_FILE), run_name="__main__")
    assert exc.value.code == 0
    assert bootstrap.read_marker(shared) == bootstrap.fingerprint(source)
    assert (app / "migrated").exists()
    assert (shared.stat().st_uid, shared.stat().st_gid) == (st_.st_uid, st_.st_gid)


# ------------------------------------------------- stateful, with fault injection
#
# Invariants:
# I1 the shared venv is declared seeded (marker == fingerprint of the image's venv) only if it holds every
#    distribution of the image's venv and the image's pyvenv.cfg;
# I2 while the shared venv is declared seeded, every plugin recorded as installed (.requirements_hash) has its
#    dependencies in the shared venv: the core trusts that record and would otherwise skip the installation;
# I3 a run that succeeds leaves the shared venv declared seeded for the current image;
# I4 a run on an up-to-date shared venv changes nothing (no wipe of what the replicas installed);
# I5 the lock file outlives every run, interrupted or not;
# I6 while a replica runs (shared lock held), the bootstrap never touches the shared venv nor the plugin records;
# I7 a run that succeeds has applied the migrations, and a run that fails before the end has not reported success;
#    the migrations never run when the venv could not be brought up to date;
# I8 while the shared venv is declared seeded, all of it (but the marker) belongs to the owner of the image's venv,
#    the replicas' user — interrupted ownership changes included;
# I9 after a run that succeeds, the plugins and data volumes belong to that owner too.

FAULTS = [None, "empty_dir", "empty_dir_partial", "copytree", "copytree_partial", "invalidate", "write_marker",
          "chown", "migrations"]


class SharedVenvMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.root = Path(tempfile.mkdtemp(prefix="bootstrap-sm-"))
        self.source, self.shared, self.plugins = self.root / "image", self.root / "shared", self.root / "plugins"
        self.data = self.root / "data"
        self.shared.mkdir()
        self.plugins.mkdir()
        (self.data / "storage").mkdir(parents=True)
        self.ownership = Ownership()  # everything starts as root's
        self.image = 0
        self.released: list[set[str]] = []
        self.ran = False
        self.replicas: list = []

    def teardown(self):
        for lf in self.replicas:
            lf.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def seeded(self) -> bool:
        return bootstrap.read_marker(self.shared) == bootstrap.fingerprint(self.source)

    @initialize(dists=st.sets(st.sampled_from(["fastapi", "redis", "qdrant", "uvicorn"]), min_size=1))
    def build_first_image(self, dists):
        self.released.append({f"{d}-0" for d in dists})
        self.build(self.released[-1])

    def build(self, dists):
        make_venv(self.source, dists)
        self.ownership.give(self.source)

    @rule(dists=st.sets(st.sampled_from(["fastapi", "redis", "qdrant", "uvicorn"]), min_size=1), same=st.booleans())
    def release_image(self, dists, same):
        """A new image: different distributions, or the same ones rebuilt."""
        if not same:
            self.image += 1
        self.released.append({f"{d}-{self.image}" for d in dists})
        self.build(self.released[-1])

    @rule(data=st.data())
    def roll_back_image(self, data):
        """Back to an image released before: its fingerprint may be the one a stale marker still holds."""
        self.build(data.draw(st.sampled_from(self.released)))

    @rule(plugin=st.sampled_from(["alpha", "beta", "gamma"]))
    def add_plugin(self, plugin):
        """Extracted by a replica, as the image's user."""
        if not (self.plugins / plugin).exists():
            (self.plugins / plugin).mkdir()
            self.ownership.give(self.plugins / plugin)

    @rule()
    def write_data(self):
        """A file in the data volume, written by a replica (the image's user). A switch of image, with the volume
        still owned by the previous user, is covered by test_main_resumes_an_interrupted_ownership_change."""
        f = self.data / "storage" / f"doc-{len(list((self.data / 'storage').iterdir()))}"
        f.write_text("")
        self.ownership.give(f)

    @precondition(lambda self: self.ran and self.seeded())
    @rule()
    def start_replica(self):
        """compose starts a replica only after a successful bootstrap; it holds the shared lock while it lives."""
        lf = open(self.shared / bootstrap.LOCK_NAME, "a")
        fcntl.flock(lf, fcntl.LOCK_SH | fcntl.LOCK_NB)
        self.replicas.append(lf)

    @precondition(lambda self: self.replicas)
    @rule()
    def stop_replica(self):
        self.replicas.pop().close()

    @precondition(lambda self: self.replicas)
    @rule(data=st.data())
    def replica_activates_plugin(self, data):
        """The core: installs the plugin's dependencies in the shared venv unless recorded as installed."""
        plugins = sorted(p.name for p in self.plugins.iterdir())
        if not plugins:
            return
        plugin = data.draw(st.sampled_from(plugins))
        if (self.plugins / plugin / ".requirements_hash").exists():
            return
        (self.shared / SITE).mkdir(parents=True, exist_ok=True)
        (self.shared / SITE / f"{plugin}_dep-1.dist-info").mkdir(exist_ok=True)
        (self.plugins / plugin / ".requirements_hash").write_text("h")
        # created by the replica, as the image's user
        self.ownership.give(self.shared / SITE / f"{plugin}_dep-1.dist-info")
        self.ownership.give(self.plugins / plugin / ".requirements_hash")

    @rule(fault=st.sampled_from(FAULTS))
    def run_bootstrap(self, fault):
        was_seeded = self.ran and self.seeded()
        before = tree(self.shared), tree(self.plugins)
        migrated = []
        self.fired = False
        self.ownership.fired = False
        self.ownership.fail_after = len(self.ownership.order) + 3 if fault == "chown" else None

        def run_migrations():
            if fault == "migrations":
                self.fired = True
                raise RuntimeError("migrations failed")
            migrated.append(True)

        with self.injected(fault), configured(self.source, self.shared, self.plugins, data=self.data,
                                              ownership=self.ownership):
            bootstrap.run_migrations, saved = run_migrations, bootstrap.run_migrations
            try:
                code = bootstrap.main()
            finally:
                bootstrap.run_migrations = saved
        self.ownership.fail_after = None
        self.ran = True

        refused = bool(self.replicas) and not was_seeded
        crashed = self.fired or self.ownership.fired
        assert code == (1 if refused or crashed else 0)
        assert bool(migrated) == (code == 0)  # I7: migrated only by a run that succeeds
        if code == 0:
            assert self.seeded()  # I3
            assert self.ownership.not_owned_by(self.plugins) == set()  # I9
            assert self.ownership.not_owned_by(self.data) == set()  # I9
        if was_seeded or refused:
            assert (tree(self.shared), tree(self.plugins)) == before  # I4, I6

    @invariant()
    def seeded_venv_holds_the_image(self):  # I1
        if self.seeded():
            assert dist_infos(self.source) <= dist_infos(self.shared)
            assert (self.shared / "pyvenv.cfg").read_text() == (self.source / "pyvenv.cfg").read_text()

    @invariant()
    def installed_records_are_true(self):  # I2
        if self.seeded():
            for hash_file in self.plugins.glob("*/.requirements_hash"):
                assert f"{hash_file.parent.name}_dep-1" in dist_infos(self.shared)

    @invariant()
    def seeded_venv_belongs_to_the_image_owner(self):  # I8
        if self.seeded():
            assert self.ownership.not_owned_by(self.shared, skip={bootstrap.MARKER_NAME}) == set()

    @invariant()
    def lock_survives(self):  # I5
        if self.ran:
            assert (self.shared / bootstrap.LOCK_NAME).exists()

    @contextmanager
    def injected(self, fault):
        originals = {
            "empty_dir": bootstrap.empty_dir,
            "invalidate": bootstrap.invalidate_plugin_requirements,
            "write_marker": bootstrap.write_marker,
            "copytree": shutil.copytree,
        }

        def crash(*_args, **_kwargs):
            self.fired = True
            raise Crash(fault)

        def empty_half(path, keep):
            # an interrupted wipe: some distributions are gone, then the wipe dies
            entries = sorted(path.rglob("*.dist-info"))
            for entry in entries[: len(entries) // 2 + 1]:
                shutil.rmtree(entry)
            self.fired = True
            raise Crash(fault)

        def copy_half(src, dst, **kwargs):
            # an interrupted copy: some distributions land, then the copy dies
            entries = sorted(Path(src).rglob("*.dist-info"))
            for entry in entries[: len(entries) // 2 + 1]:
                target = Path(dst) / entry.relative_to(src)
                target.mkdir(parents=True, exist_ok=True)
            self.fired = True
            raise Crash(fault)

        if fault in ("empty_dir", "invalidate", "write_marker"):
            setattr(bootstrap, {"invalidate": "invalidate_plugin_requirements"}.get(fault, fault), crash)
        elif fault == "empty_dir_partial":
            bootstrap.empty_dir = empty_half
        elif fault == "copytree":
            shutil.copytree = crash
        elif fault == "copytree_partial":
            shutil.copytree = copy_half
        try:
            yield
        finally:
            bootstrap.empty_dir = originals["empty_dir"]
            bootstrap.invalidate_plugin_requirements = originals["invalidate"]
            bootstrap.write_marker = originals["write_marker"]
            shutil.copytree = originals["copytree"]


SharedVenvMachine.TestCase.settings = settings(max_examples=300, stateful_step_count=30, deadline=None)
TestSharedVenv = SharedVenvMachine.TestCase
