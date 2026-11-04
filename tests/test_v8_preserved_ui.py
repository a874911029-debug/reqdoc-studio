"""V8 regression tests for the preserved original CompactStudio UI and new local service."""
from __future__ import annotations
import hashlib
import tempfile
import tkinter as tk
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from reqdoc.config import Settings
from reqdoc_studio import CompactStudio, Store


def _safe_destroy(widget):
    try:
        widget.destroy()
    except tk.TclError:
        pass


class PreservedUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "data"
        self.settings = Settings(home=root / "home", docs_dir=root / "docs")
        self.settings.ensure_directories()
        self.store = Store(self.settings)
        self.project = self.store.service.create_project_group("视觉项目")
        self.document = self.store.service.create_document(self.project.name, "场景整理")

    def test_original_four_module_navigation_is_retained(self):
        app = CompactStudio(self.store)
        app.withdraw()
        self.addCleanup(_safe_destroy, app)
        self.assertEqual(
            CompactStudio.NAV_ITEMS,
            [
                ("项目与文档", "projects"),
                ("任务处理", "tasks"),
                ("进度统计", "progress"),
                ("设置与数据", "settings"),
            ],
        )
        self.assertEqual(set(app.nav_buttons), {"projects", "tasks", "progress", "settings"})
        self.assertTrue(hasattr(app, "project_list"))
        self.assertTrue(hasattr(app, "task_list_panel"))
        self.assertTrue(hasattr(app, "task_source_panel"))
        self.assertTrue(hasattr(app, "task_editor_panel"))

    def test_original_ui_can_create_edit_and_reopen_task(self):
        app = CompactStudio(self.store)
        app.withdraw()
        self.addCleanup(_safe_destroy, app)
        app.load_doc(self.document.key)
        app.new_task()
        app.titlevar.set("第一条整理任务")
        app.chaptervar.set("空间结构")
        app.body.insert("1.0", "独立编辑的处理结果正文")
        app.save_task()

        rows = self.store.tasks(self.document.key)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, "第一条整理任务")
        self.assertEqual(rows[0].section, "空间结构")
        self.assertEqual(rows[0].content, "独立编辑的处理结果正文")
        self.assertTrue(self.store.service.document_path(self.document.key).is_file())

        app.destroy()
        reopened = CompactStudio(Store(self.settings))
        reopened.withdraw()
        self.addCleanup(_safe_destroy, reopened)
        reopened.load_doc(self.document.key)
        self.assertEqual(self.store.service.get_requirement(rows[0].id).content,
                         "独立编辑的处理结果正文")

    def test_original_archive_is_immutable_and_working_tasks_are_separate(self):
        original = Path(self.temp.name) / "source.md"
        original.write_text("# 原始标题\n\n原始资料正文。\n", encoding="utf-8")
        before = hashlib.sha256(original.read_bytes()).hexdigest()
        warnings, extracted = self.store.archive_source(self.document.key, original)
        self.assertIn("原始资料正文", extracted)
        record = self.store.sources(self.document.key)[0]
        archive = self.settings.docs_dir / record[2]
        archived_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(before, archived_hash)

        self.store.save_source_text(self.document.key, record[0], "仅修改工作副本")
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), archived_hash)
        self.store.save_task(self.document.key, "整理结果", "归纳", "这是独立的整理结果")
        self.assertEqual(self.store.tasks(self.document.key)[0].content, "这是独立的整理结果")
        self.assertEqual(original.read_text("utf-8"), "# 原始标题\n\n原始资料正文。\n")

    def test_backup_contains_database_and_original_archive(self):
        source = Path(self.temp.name) / "source.txt"
        source.write_text("保留原文", encoding="utf-8")
        self.store.archive_source(self.document.key, source)
        backup = Path(self.temp.name) / "backup.zip"
        self.store.backup(backup)
        with zipfile.ZipFile(backup) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("reqdoc-data/home/reqdoc.db", archive.namelist())
            self.assertTrue(any("/_sources/" in name for name in archive.namelist()))

    def test_destroyed_root_ignores_late_update_callback(self):
        app = CompactStudio(self.store)
        app.withdraw()
        app.destroy()
        self.assertTrue(app._closing)
        app._poll_github_update()
        app.report_callback_exception(RuntimeError, RuntimeError("late callback"), None)

    def test_dynamic_source_table_columns_can_be_cleared_and_rebuilt(self):
        app = CompactStudio(self.store)
        app.withdraw()
        self.addCleanup(_safe_destroy, app)
        app._refresh_source_table("""| 名称 | 状态 |
| --- | --- |
| 资料 A | 已整理 |""")
        self.assertEqual(tuple(app.task_table_grid["columns"]), ("col0", "col1"))
        app._refresh_source_table("没有表格的普通文本")
        self.assertEqual(tuple(app.task_table_grid["columns"]), ())
        app._refresh_source_table("""| 标题 | 章节 | 备注 |
| --- | --- | --- |
| 任务 A | 场景 | 说明 |""")
        self.assertEqual(tuple(app.task_table_grid["columns"]), ("col0", "col1", "col2"))

    def test_document_preview_and_task_editor_are_visible_together(self):
        app = CompactStudio(self.store)
        app.withdraw()
        self.addCleanup(_safe_destroy, app)
        self.assertTrue(app.source_panel_visible)
        self.assertTrue(app.editor_open)
        self.assertIn(str(app.task_source_panel), app.tasks_split.panes())
        self.assertIn(str(app.task_editor_panel), app.task_vertical_split.panes())

    def test_new_document_stays_on_project_page_until_double_click(self):
        app = CompactStudio(self.store)
        app.withdraw()
        self.addCleanup(_safe_destroy, app)
        app.show_page("projects")
        app.refresh_projects_page()
        app.project_list.selection_set("project-" + str(self.project.id))
        with patch("reqdoc_studio.simpledialog.askstring", return_value="第二份文档"):
            app.new_document()
        selected = app.project_list.selection()
        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0].startswith("d:"))
        new_key = selected[0][2:]
        self.assertIsNone(app.key)
        self.assertEqual(app.pages["projects"].winfo_manager(), "pack")
        app.open_project_from_list()
        self.assertEqual(app.key, new_key)
        self.assertEqual(app.pages["tasks"].winfo_manager(), "pack")

    def test_progress_filter_is_all_projects_or_one_project(self):
        app = CompactStudio(self.store)
        app.withdraw()
        self.addCleanup(_safe_destroy, app)
        app.refresh_progress(reset_group=True)
        self.assertEqual(tuple(app.progress_scope_box["values"]), ("全部项目", "视觉项目"))
        self.assertFalse(hasattr(app, "progress_project_box"))
        self.assertFalse(hasattr(app, "progress_document_box"))
        app.progress_scope_var.set("视觉项目")
        app.refresh_progress(reset_group=True)
        children = app.progress_chapters.get_children()
        self.assertEqual(len(children), 1)
        self.assertEqual(app.progress_chapters.item(children[0], "values")[0], "视觉项目 / 场景整理")

    def test_delete_document_does_not_create_automatic_backup(self):
        with patch.object(self.store, "backup", side_effect=AssertionError("backup called")):
            self.store.delete_document_permanently(self.document.key)
        self.assertEqual(self.store.service.documents_for(self.project.name), [])

    def test_archived_source_open_uses_checked_archive_path(self):
        app = CompactStudio(self.store)
        app.withdraw()
        self.addCleanup(_safe_destroy, app)
        original = Path(self.temp.name) / "original.md"
        original.write_text("# 原始资料\\n\\n来源正文", encoding="utf-8")
        self.store.archive_source(self.document.key, original)
        app.load_doc(self.document.key)
        with patch.object(app, "open_path") as open_path:
            app.open_task_source()
        row = self.store.sources(self.document.key)[0]
        open_path.assert_called_once_with((self.settings.docs_dir / row[2]).resolve())


if __name__ == "__main__":
    unittest.main()
