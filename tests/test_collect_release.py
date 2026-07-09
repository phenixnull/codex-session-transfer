from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import collect_release


class CollectReleaseTests(unittest.TestCase):
    def test_windows_release_collects_only_current_version_artifacts(self) -> None:
        original_root = collect_release.ROOT
        original_output_dir = collect_release.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output" / "electron"
            output_dir.mkdir(parents=True)
            (root / "package.json").write_text('{"version":"0.3.2"}', encoding="utf-8")
            (output_dir / "Codex Session Transfer-0.3.1-Setup-x64.exe").write_text("old", encoding="utf-8")
            (output_dir / "Codex Session Transfer-0.3.2-Setup-x64.exe").write_text("new", encoding="utf-8")
            (output_dir / "Codex Session Transfer-0.3.2-Portable-x64.exe").write_text("portable", encoding="utf-8")
            (output_dir / "latest.yml").write_text(
                "path: Codex Session Transfer-0.3.2-Setup-x64.exe\n",
                encoding="utf-8",
            )

            collect_release.ROOT = root
            collect_release.OUTPUT_DIR = output_dir
            try:
                collect_release.main(["--platform", "win"])
            finally:
                collect_release.ROOT = original_root
                collect_release.OUTPUT_DIR = original_output_dir

            release_dir = root / "release" / "v0.3.2" / "windows"
            self.assertFalse((release_dir / "Codex.Session.Transfer-0.3.1-Setup-x64.exe").exists())
            self.assertTrue((release_dir / "Codex.Session.Transfer-0.3.2-Setup-x64.exe").exists())
            self.assertTrue((release_dir / "Codex.Session.Transfer-0.3.2-Portable-x64.exe").exists())
            self.assertTrue((release_dir / "latest.yml").exists())


if __name__ == "__main__":
    unittest.main()
