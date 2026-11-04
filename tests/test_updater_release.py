import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import reqdoc_updater as updater


def make_manifest(version="7.5.3", url=None, sha=None, size=54000):
    return {
        "schema": 2,
        "version": version,
        "asset": {
            "url": url or updater.PACKAGE_BASE_URL + f"reqdoc-update-v{version}.zip",
            "sha256": sha or ("b" * 64),
            "size": size,
        },
        "notes": "Updater bootstrap",
        "minimum_supported_version": "7.5.2",
        "source_commit": "a" * 40,
    }


def make_patch(path, version="7.5.3", extra=None):
    files = {
        "_internal/reqdoc_updater.py": b"APP_VERSION = '7.5.3'\n",
        "updater.ps1": b"# updater\n",
        "version.json": json.dumps({
            "name": "ReqDoc",
            "version": version,
            "channel": "stable",
            "feed": updater.FEED_URL,
            "package_type": "updater-bootstrap",
        }).encode("utf-8"),
    }
    if extra:
        files.update(extra)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


class UpdaterFeedTests(unittest.TestCase):
    def test_feed_points_to_persistent_updates_branch(self):
        self.assertIn("/reqdoc-studio/updates/latest.json", updater.FEED_URL)
        self.assertIn("/reqdoc-studio/updates/", updater.PACKAGE_BASE_URL)

    def test_valid_manifest(self):
        result = updater.validate_manifest(make_manifest())
        self.assertEqual(result["version"], "7.5.3")
        self.assertEqual(result["minimum_supported_version"], "7.5.2")

    def test_release_url_is_rejected(self):
        payload = make_manifest(
            url="https://github.com/a874911029-debug/reqdoc-studio/releases/download/v7.5.3/package.zip"
        )
        with self.assertRaises(updater.UpdateError):
            updater.validate_manifest(payload)

    def test_untrusted_host_is_rejected(self):
        payload = make_manifest(url="https://example.com/reqdoc-update-v7.5.3.zip")
        with self.assertRaises(updater.UpdateError):
            updater.validate_manifest(payload)

    def test_missing_digest_is_rejected(self):
        payload = make_manifest(sha="not-a-digest")
        with self.assertRaises(updater.UpdateError):
            updater.validate_manifest(payload)

    def test_invalid_version_is_rejected(self):
        with self.assertRaises(updater.UpdateError):
            updater.validate_manifest(make_manifest(version="../7.5.3"))

    def test_newer_version_is_available(self):
        self.assertTrue(updater.update_available(make_manifest(version="7.5.4")))

    def test_same_version_is_not_available(self):
        self.assertFalse(updater.update_available(make_manifest(), "7.5.3"))

    def test_valid_updater_patch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "patch.zip"
            make_patch(path)
            updater.validate_zip(path, expected_version="7.5.3")

    def test_executable_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "patch.zip"
            make_patch(path, extra={"reqdoc-gui.exe": b"not an executable"})
            with self.assertRaises(updater.UpdateError):
                updater.validate_zip(path)

    def test_user_data_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "patch.zip"
            make_patch(path, extra={"reqdoc-data/projects.db": b"private"})
            with self.assertRaises(updater.UpdateError):
                updater.validate_zip(path)

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "patch.zip"
            make_patch(path, extra={"../evil.py": b"bad"})
            with self.assertRaises(updater.UpdateError):
                updater.validate_zip(path)

    def test_internal_version_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "patch.zip"
            make_patch(path, version="7.5.4")
            with self.assertRaises(updater.UpdateError):
                updater.validate_zip(path, expected_version="7.5.3")


if __name__ == "__main__":
    unittest.main()
