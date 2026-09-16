import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from netlab import __version__
from netlab.updater import UpdateClient, UpdateError
from tools import make_update_manifest as release


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.installer = self.directory / f"MotuNet-{__version__}-Setup-x64.exe"
        self.payload = b"MZ fixture installer bytes"
        self.installer.write_bytes(self.payload)
        self.repository = "example/MotuNet"
        self.changelog = f"# 更新日志\n\n## {__version__}（2026-09-16）\n\n- 更新通知。\n- 校验下载。\n\n## 0.4.1\n\n- 旧版本。\n"
        self.notes = "- 更新通知。\n- 校验下载。"

    def test_exact_current_version_notes_preserve_body(self):
        self.assertEqual(release.release_notes(self.changelog, __version__), self.notes)
        self.assertEqual(release.release_notes("## 0.5.10\nwrong\n## 0.5.1\ncorrect", "0.5.1"), "correct")
        with self.assertRaisesRegex(ValueError, "缺少"):
            release.release_notes(self.changelog, "9.0.0")
        with self.assertRaisesRegex(ValueError, "不能为空"):
            release.release_notes("## 0.5.0\n\n## 0.4.1\nold", "0.5.0")

    def test_tag_must_exactly_match_source_version(self):
        self.assertEqual(release.validate_release(self.repository, __version__, self.changelog,
                         f"v{__version__}"), self.notes)
        for tag in (__version__, "v9.0.0", f"v{__version__}-beta"):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "不一致"):
                release.validate_release(self.repository, __version__, self.changelog, tag)

    def test_manifest_matches_consumer_schema_version_asset_and_sha(self):
        manifest = release.make_manifest(self.installer, self.repository, __version__, self.notes)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["platform"], "windows-x64")
        self.assertEqual(manifest["version"], __version__)
        self.assertEqual(manifest["size"], len(self.payload))
        self.assertEqual(manifest["sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(manifest["notes"], self.notes)
        self.assertEqual(manifest["url"], f"https://github.com/{self.repository}/releases/download/v{__version__}/{self.installer.name}")
        info = UpdateClient(self.repository, "0.0.0")._parse_manifest(manifest)
        self.assertEqual(info.size, len(self.payload))
        self.assertEqual(info.version, __version__)

    def test_writes_manifest_checksum_and_only_current_release_notes(self):
        manifest = release.make_manifest(self.installer, self.repository, __version__, self.notes)
        output = self.directory / "output"
        release.write_manifest(manifest, output)
        self.assertEqual(json.loads((output / "latest.json").read_text(encoding="utf-8")), manifest)
        self.assertEqual((output / f"{self.installer.name}.sha256").read_text(), f"{manifest['sha256']}  {self.installer.name}\n")
        self.assertEqual((output / "release-notes.md").read_text(encoding="utf-8"), self.notes + "\n")
        self.assertNotIn(b"\r\n", (output / "latest.json").read_bytes())

    def test_invalid_repository_version_and_installer_name_rejected(self):
        for repository in ("", "../repo", "owner/..", "owner/repo?x"):
            with self.subTest(repository=repository), self.assertRaises(UpdateError):
                release.make_manifest(self.installer, repository, __version__, self.notes)
        with self.assertRaises(UpdateError):
            release.make_manifest(self.installer, self.repository, "0.5.0-rc1", self.notes)
        wrong = self.directory / "setup.exe"
        wrong.write_bytes(self.payload)
        with self.assertRaisesRegex(ValueError, "文件名"):
            release.make_manifest(wrong, self.repository, __version__, self.notes)

    def test_empty_missing_oversized_installer_or_manifest_rejected(self):
        self.installer.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "大小"):
            release.make_manifest(self.installer, self.repository, __version__, self.notes)
        self.installer.write_bytes(self.payload)
        with patch.object(release, "DOWNLOAD_LIMIT", 2), self.assertRaisesRegex(ValueError, "大小"):
            release.make_manifest(self.installer, self.repository, __version__, self.notes)
        with patch.object(release, "MANIFEST_LIMIT", 20), self.assertRaisesRegex(ValueError, "128 KiB"):
            release.make_manifest(self.installer, self.repository, __version__, self.notes)
        self.installer.unlink()
        with self.assertRaises(OSError):
            release.make_manifest(self.installer, self.repository, __version__, self.notes)

    def test_cli_uses_source_version_and_baked_repository(self):
        changelog = self.directory / "CHANGELOG.md"
        changelog.write_text(self.changelog, encoding="utf-8")
        output = self.directory / "generated"
        with patch.object(release, "UPDATE_REPOSITORY", self.repository), redirect_stdout(io.StringIO()):
            result = release.main(["--installer", str(self.installer), "--output-dir", str(output),
                                   "--changelog", str(changelog), "--check-tag", f"v{__version__}"])
        self.assertEqual(result, 0)
        manifest = json.loads((output / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], __version__)
        self.assertIn(f"github.com/{self.repository}/", manifest["url"])

    def test_cli_validation_writes_no_assets(self):
        changelog = self.directory / "CHANGELOG.md"
        changelog.write_text(self.changelog, encoding="utf-8")
        output = self.directory / "not-created"
        with patch.object(release, "UPDATE_REPOSITORY", self.repository), redirect_stdout(io.StringIO()):
            result = release.main(["--validate-only", "--output-dir", str(output), "--changelog", str(changelog)])
        self.assertEqual(result, 0)
        self.assertFalse(output.exists())

    def test_cli_rejects_empty_or_different_baked_source(self):
        for baked, requested in (("", ""), (self.repository, "other/repo")):
            with self.subTest(baked=baked, requested=requested), patch.object(release, "UPDATE_REPOSITORY", baked):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    release.main(["--repository", requested, "--validate-only"])
                self.assertEqual(error.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
