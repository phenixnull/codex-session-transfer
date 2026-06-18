from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output" / "electron"


def package_version() -> str:
    return json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_patterns(platform: str) -> list[str]:
    if platform == "win":
        return ["*.exe", "latest.yml"]
    return ["*.dmg", "*.zip", "latest-mac.yml"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect packaged artifacts into the repo release folder.")
    parser.add_argument("--platform", choices=("win", "mac"), required=True)
    args = parser.parse_args(argv)

    version = package_version()
    platform_dir = "windows" if args.platform == "win" else "macos"
    dest = ROOT / "release" / f"v{version}" / platform_dir
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    copied: list[Path] = []
    for pattern in artifact_patterns(args.platform):
        for source in sorted(OUTPUT_DIR.glob(pattern)):
            target = dest / source.name
            shutil.copy2(source, target)
            copied.append(target)

    if not copied:
        raise SystemExit(f"No {args.platform} artifacts found in {OUTPUT_DIR}")

    checksums = []
    for path in copied:
        checksums.append({"file": path.name, "sha256": sha256(path), "bytes": path.stat().st_size})
    (dest / "SHA256SUMS.txt").write_text(
        "".join(f"{item['sha256']}  {item['file']}\n" for item in checksums),
        encoding="utf-8",
    )
    (dest / "manifest.json").write_text(
        json.dumps(
            {
                "version": version,
                "platform": args.platform,
                "built_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "artifacts": checksums,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Collected {len(copied)} artifacts in {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
