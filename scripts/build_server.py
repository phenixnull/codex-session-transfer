from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER_NAME = "codex-session-transfer-server"


def current_platform() -> str:
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def add_data_value(target: str) -> str:
    separator = ";" if target == "win" else ":"
    return f"{ROOT / 'static'}{separator}static"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the local Python server for Electron packaging.")
    parser.add_argument("--platform", choices=("win", "mac", "linux"), default=current_platform())
    args = parser.parse_args(argv)

    host_platform = current_platform()
    if args.platform != host_platform:
        raise SystemExit(
            f"PyInstaller must build on the target OS. Requested {args.platform}, running on {host_platform}."
        )

    binary_name = f"{SERVER_NAME}.exe" if args.platform == "win" else SERVER_NAME
    dist_dir = ROOT / "build" / "server" / args.platform
    work_dir = ROOT / "build" / "pyinstaller" / args.platform
    spec_dir = ROOT / "build" / "pyinstaller" / "specs" / args.platform
    shutil.rmtree(dist_dir, ignore_errors=True)
    dist_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        SERVER_NAME,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(spec_dir),
        "--add-data",
        add_data_value(args.platform),
        str(ROOT / "server.py"),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    output = dist_dir / binary_name
    if args.platform != "win":
        output.chmod(output.stat().st_mode | 0o111)
    print(f"Built {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
