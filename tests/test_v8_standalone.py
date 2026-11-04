"""V8 contract tests: source immutability, working-document edits, ordering and updater."""
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from core import Store, parse_tasks, render_task, replace_task, VERSION
from update_client import validate_feed, validate_package, UpdateError, BASE


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "userdata")
        self.p = self.store.add_project("项目甲")
        self.d = self.store.add_document(self.p, "文档一")

    def test_new_project_document_and_single_source_of_truth(self):
        self.assertEqual(self.store.projects()[0]["name"], "项目甲")
        self.assertEqual(self.store.documents(self.p)[0]["name"], "文档一")
        text, tid = replace_task("", None, "第一项", "包含正文", False)
        self.store.update_content(self.d, text)
        data = self.store.document(self.d)["content"]
        self.assertEqual(len(parse_tasks(data)), 1)
        self.assertIn("包含正文", data)
        changed, same = replace_task(data, tid, "第一项修订", "改动内容", True)
        self.assertEqual(same, tid)
        self.store.update_content(self.d, changed)
        row = parse_tasks(self.store.document(self.d)["content"])[0]
        self.assertEqual(row["title"], "第一项修订")
        self.assertEqual(row["body"], "改动内容")
        self.assertTrue(row["done"])

    def test_original_archive_never_changes_when_editing(self):
        original = self.root / "source.md"
        original.write_text("### 原任务\n\n- [ ]\n保留内容\n", encoding="utf-8")
        original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        source_id = self.store.archive_source(self.d, original)
        archived = self.store.sources(self.d)[0]
        archive = self.store.root / archived["path"]
        archived_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(original_hash, archived_hash)
        self.assertEqual(len(parse_tasks(self.store.document(self.d)["content"])), 1)
        self.store.update_content(self.d, render_task("新标题", "新正文", True))
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), archived_hash)
        self.assertIn("原任务", self.store.original_text(source_id))
        self.assertIn("新标题", self.store.document(self.d)["content"])

    def test_import_without_heading_becomes_task(self):
        src = self.root / "note.txt"
        src.write_text("这是尚未分类的说明。", encoding="utf-8")
        self.store.archive_source(self.d, src)
        rows = parse_tasks(self.store.document(self.d)["content"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "note")
        self.assertIn("说明", rows[0]["body"])

    def test_relative_markdown_image_is_copied_into_working_assets(self):
        src = self.root / "withimage.md"
        pic = self.root / "picture.png"
        pic.write_bytes(b"fake-image-data-for-archive-test")
        src.write_text("### 图例\n![例子](picture.png)", encoding="utf-8")
        self.store.archive_source(self.d, src)
        working = self.store.document(self.d)["content"]
        self.assertIn("](assets/", working)
        self.assertEqual(len(list(self.store.assets_dir(self.d).iterdir())), 1)

    def test_reordering_projects_and_cross_project_document_move(self):
        p2 = self.store.add_project("项目乙")
        d2 = self.store.add_document(self.p, "文档二")
        self.store.reorder_project(p2, self.p)
        self.assertEqual([p["name"] for p in self.store.projects()], ["项目乙", "项目甲"])
        self.store.move_document(self.d, p2)
        self.assertEqual([d["name"] for d in self.store.documents(p2)], ["文档一"])
        self.assertEqual([d["name"] for d in self.store.documents(self.p)], ["文档二"])
        self.store.move_document(d2, p2, target=self.d, after=False)
        self.assertEqual([d["name"] for d in self.store.documents(p2)], ["文档二", "文档一"])

    def test_backup_contains_db_and_assets_but_never_edits_source(self):
        source = self.root / "untouched.txt"
        source.write_text("原文不可修改", encoding="utf-8")
        self.store.archive_source(self.d, source)
        destination = self.root / "backup.zip"
        self.store.backup(destination)
        with zipfile.ZipFile(destination) as z:
            self.assertIn("workspace.db", z.namelist())
            self.assertTrue(any(n.startswith("documents/") and "/sources/" in n for n in z.namelist()))
            self.assertIsNone(z.testzip())
        self.assertEqual(source.read_text("utf-8"), "原文不可修改")

    def test_backup_rejects_writing_inside_user_storage(self):
        with self.assertRaises(ValueError):
            self.store.backup(self.store.root / "danger.zip")


class UpdateTests(unittest.TestCase):
    def make_feed(self):
        return {"schema": 8, "version": VERSION, "asset": {
            "url": BASE + f"reqdoc-v{VERSION}-win64.zip",
            "size": 2048, "sha256": "a" * 64,
        }}

    def test_v8_manifest_is_strict(self):
        self.assertEqual(validate_feed(self.make_feed())["version"], VERSION)
        bad = self.make_feed()
        bad["asset"]["url"] = "https://example.com/fake.zip"
        with self.assertRaises(UpdateError):
            validate_feed(bad)
        bad = self.make_feed()
        bad["version"] = "7.5.4"
        with self.assertRaises(UpdateError):
            validate_feed(bad)

    def test_only_exact_three_program_files_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "release.zip"
            files = {
                "ReqDocStudio.exe": b"MZ-representative-test-payload",
                "updater.ps1": b"# test",
                "version.json": json.dumps({"name": "ReqDocStudio", "version": VERSION}).encode(),
            }
            with zipfile.ZipFile(path, "w") as z:
                for name, value in files.items():
                    z.writestr(name, value)
            self.assertTrue(validate_package(path, VERSION))
            with zipfile.ZipFile(path, "a") as z:
                z.writestr("reqdoc-data/workspace.db", b"private")
            with self.assertRaises(UpdateError):
                validate_package(path, VERSION)


if __name__ == "__main__":
    unittest.main()
