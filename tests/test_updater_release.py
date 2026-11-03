import unittest

import reqdoc_updater as u


class ReleaseTests(unittest.TestCase):
    def make_release(self, version="7.5.1", repo="reqdoc-studio", digest=None):
        sha = digest or ("b" * 64)
        filename = f"reqdoc-studio-win64-v{version}.zip"
        return {
            "tag_name": "v" + version,
            "draft": False,
            "prerelease": False,
            "body": "Release notes",
            "assets": [{
                "name": filename,
                "size": 54000000,
                "digest": "sha256:" + sha,
                "browser_download_url": (
                    f"https://github.com/a874911029-debug/{repo}"
                    f"/releases/download/v{version}/{filename}"
                ),
            }],
        }

    def test_single_repo(self):
        self.assertEqual(u.UPDATE_REPO, "reqdoc-studio")
        self.assertIn("/reqdoc-studio/releases/latest", u.FEED_URL)

    def test_new_release(self):
        m = u.manifest_from_release(self.make_release())
        self.assertEqual(m["version"], "7.5.1")
        self.assertTrue(u.update_available(m))

    def test_untrusted_repo_rejected(self):
        with self.assertRaises(u.UpdateError):
            u.manifest_from_release(self.make_release(repo="reqdoc-studio-updates"))

    def test_missing_digest_rejected(self):
        release = self.make_release()
        release["assets"][0].pop("digest")
        with self.assertRaises(u.UpdateError):
            u.manifest_from_release(release)

    def test_draft_rejected(self):
        release = self.make_release()
        release["draft"] = True
        with self.assertRaises(u.UpdateError):
            u.manifest_from_release(release)


if __name__ == "__main__":
    unittest.main()
