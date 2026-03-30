#!/usr/bin/env python3
"""
plugin_installer.py
-------------------
One-shot container init: Installs the requirements for all plugins
in CCAT_PLUGINS_PATH using uv, before workers start.

No external dependencies. Serialization between containers is guaranteed
by Docker Compose (service_completed_successfully). A filesystem flock
protects the extreme case of multiple manual launches on the same host.

Exit codes:
- 0 everything ok (or nothing to install)
- 1 at least one plugin failed to install
"""

import fcntl
import os
import subprocess
import sys
from pathlib import Path

PLUGINS_DIR = Path(os.getenv("CCAT_PLUGINS_PATH", "/app/cat/plugins"))
VENV_PATH = Path(os.getenv("CCAT_VENV_PATH", "/app/.venv"))
LOCK_FILE = Path(os.getenv("CCAT_INSTALL_LOCK", "/tmp/grinning_cat_install.lock"))


def collect_requirements() -> dict[str, Path]:
    if not PLUGINS_DIR.exists():
        print(f"[installer] '{PLUGINS_DIR}' does not exist. Skipping.")
        return {}
    return {
        entry.name: entry / "requirements.txt"
        for entry in sorted(PLUGINS_DIR.iterdir())
        if entry.is_dir()
        and (entry / "requirements.txt").exists()
        and (entry / "requirements.txt").stat().st_size > 0
    }


def install(req_file: Path) -> bool:
    cmd = [
        "uv", "pip", "install",
        "--no-cache", "--no-upgrade",
        "--python", str(VENV_PATH / "bin" / "python"),
        "-r", str(req_file),
    ]
    print(f"[installer]   {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[installer] ERROR:\n{result.stderr.strip()}")
        return False
    if result.stdout.strip():
        print(f"[installer] {result.stdout.strip()}")
    return True


def main() -> None:
    print("[installer] Starting installation plugin requirements...")

    with open(LOCK_FILE, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX)
        except OSError as e:
            print(f"[installer] Lock acquisition failed: {e}")
            sys.exit(1)

        plugins = collect_requirements()
        if not plugins:
            print("[installer] No requirements.txt found. Exiting.")
            sys.exit(0)

        print(f"[installer] Plugin to install: {list(plugins.keys())}")
        failures = []
        for name, req_file in plugins.items():
            print(f"[installer] → '{name}'...")
            if install(req_file):
                print(f"[installer] OK")
            else:
                failures.append(name)

    if failures:
        print(f"[installer] FAILED: {failures}")
        sys.exit(1)

    print("[installer] All plugins installed successfully.")
    sys.exit(0)


if __name__ == "__main__":
    main()