from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from reqdoc_win_drop import select_drop_files


class WindowsDropPathTests(unittest.TestCase):
    def test_unicode_windows_path_is_preserved_and_duplicates_are_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "中文资料目录"
            folder.mkdir()
            source = folder / "场景设计说明.MD"
            source.write_text("# 场景设计", encoding="utf-8")

            accepted, rejected = select_drop_files([str(source), str(source)])

            self.assertEqual(accepted, [str(source.resolve(strict=True))])
            self.assertEqual(rejected, [])

    def test_unsupported_missing_and_directory_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unsupported = root / "资料.pdf"
            missing = root / "不存在.md"
            directory = root / "目录.md"
            directory.mkdir()

            accepted, rejected = select_drop_files(
                [str(unsupported), str(missing), str(directory)]
            )

            self.assertEqual(accepted, [])
            self.assertCountEqual(
                rejected, ["资料.pdf", "不存在.md", "目录.md"]
            )

    def test_batch_limit_is_checked_before_path_resolution(self):
        with self.assertRaisesRegex(ValueError, "最多拖入 20 个文件"):
            select_drop_files([f"not-existing-{index}.md" for index in range(21)])

    def test_malformed_path_does_not_abort_other_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "有效.md"
            source.write_text("valid", encoding="utf-8")

            accepted, rejected = select_drop_files(["\x00", str(source)])

            self.assertEqual(accepted, [str(source.resolve(strict=True))])
            self.assertEqual(rejected, ["\x00"])


if __name__ == "__main__":
    unittest.main()
