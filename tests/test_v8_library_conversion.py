import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import tkinter as tk
import zipfile
from tkinter import ttk
import unittest
from unittest.mock import patch

from reqdoc.config import Settings
from reqdoc_library import delete_history_document, migrate_portable_library, recover_history_deletions, delete_library_item
from reqdoc_organize import organize_materials
from reqdoc_task_conversion import conversion_items, strip_task_block
from reqdoc_working_document import (
    refresh_sync_conflict_button, resolve_sync_conflict, save_import_task_link,
    prepare_import_documents, restore_organization_rules_from_backup,
    sync_import_tasks, write_import_copy,
)
from test_v8_feedback import WorkingStore, WorkingStudio, widgets


class LibraryConversionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        data = self.root / "profile" / "ReqDocStudio" / "data"
        self.settings = Settings(data / "home", data / "docs", data.parent / "library.json")
        self.settings.ensure_directories()
        self.store = WorkingStore(self.settings)
        self.project = self.store.service.create_project_group("项目甲")
        self.doc = self.store.service.create_document(self.project.name, "工作甲")

    def app(self):
        app = WorkingStudio(self.store)
        self.addCleanup(app.close)
        app.load_doc(self.doc.key)
        errors = patch("reqdoc_studio.messagebox.showerror", side_effect=lambda title, message, **kw: self.fail(message))
        errors.start()
        self.addCleanup(errors.stop)
        return app

    def test_portable_migration_and_moving_the_application_folder(self):
        task = self.store.save_task(self.doc.key, "保留任务", "组", "详情")
        folder = self.store.service.document_folder(self.doc.key)
        (folder / "image.png").write_bytes(b"test-image")
        (self.settings.home / "organize-rules.json").write_text('{"rules_text":"灯光 = 照明"}', encoding="utf-8")
        application = self.root / "软件"
        application.mkdir()
        program = application / "ReqDocStudio.exe"
        program.write_bytes(b"test")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "profile"), "REQDOC_V8_DATA": ""}), \
             patch("reqdoc.config.sys.frozen", True, create=True), patch("reqdoc.config.sys.executable", str(program)):
            pending = Settings.from_env()
            self.assertEqual(pending.portable_target, (application / "总文档库").resolve())
            migrated = migrate_portable_library(self.store, pending.portable_target)
            self.assertTrue(Settings.from_env().portable)
        reopened = WorkingStore(migrated)
        self.assertEqual(reopened.service.get_requirement(task.id), task)
        self.assertEqual((reopened.service.document_folder(self.doc.key) / "image.png").read_bytes(), b"test-image")
        self.assertIn("照明", (migrated.home / "organize-rules.json").read_text("utf-8"))
        copied = self.root / "移动后的软件"
        shutil.copytree(application, copied)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "profile"), "REQDOC_V8_DATA": ""}), \
             patch("reqdoc.config.sys.frozen", True, create=True), patch("reqdoc.config.sys.executable", str(copied / program.name)):
            active = Settings.from_env()
        self.assertEqual(active.library_root, (copied / "总文档库").resolve())
        self.assertEqual(WorkingStore(active).service.get_requirement(task.id), task)

    def test_library_root_project_document_and_history_deletion_preserve_used_images(self):
        source = self.root / "来源.md"
        image = self.root / "参考.png"
        image.write_bytes(b"image")
        source.write_text("![参考](参考.png)", encoding="utf-8")
        _, text, sid = self.store.archive_source(self.doc.key, source, include_id=True)
        with self.store.conn() as conn:
            write_import_copy(conn, self.doc.key, sid, source.name, text)
            archive = self.settings.docs_dir / conn.execute("SELECT stored FROM studio_sources WHERE id=?", (sid,)).fetchone()[0]
        app = self.app()
        app.working_text.insert("1.0", "保留未保存的工作正文")
        app.show_page("library")
        self.assertEqual(app.library_tree.item("library-root", "text"), "总文档库")
        project_node = app.library_tree.get_children("library-root")[0]
        self.assertEqual(app.library_tree.item(project_node, "text"), self.project.name)
        history = next(iid for iid, item in app.library_items.items() if item[0] == "history")
        self.assertEqual(app.library_tree.parent(history), project_node)
        app.library_tree.selection_set(history)
        with patch("reqdoc_library.messagebox.askyesno", return_value=True):
            delete_library_item(app)
        self.assertIn("保留未保存的工作正文", app.working_text.get("1.0", "end-1c"))
        self.assertFalse(archive.exists())
        self.assertFalse(any(item[0] == "history" for item in app.library_items.values()))
        media = list((archive.parent / "media").iterdir())
        self.assertEqual(media[0].read_bytes(), b"image")

    def test_history_deletion_rolls_back_and_recovers_original_file(self):
        source = self.root / "历史.txt"
        source.write_text("历史记录", encoding="utf-8")
        _, _, sid = self.store.archive_source(self.doc.key, source, include_id=True)
        with self.store.conn() as conn:
            archived = self.settings.docs_dir / conn.execute("SELECT stored FROM studio_sources WHERE id=?", (sid,)).fetchone()[0]
            conn.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON studio_sources BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            delete_history_document(self.store, self.doc.key, sid)
        self.assertEqual(archived.read_text("utf-8"), "历史记录")
        journal = self.settings.home / "history-deletions" / "delete-test"
        journal.mkdir()
        (journal / "manifest.json").write_text(json.dumps({"doc": self.doc.key, "id": sid}), encoding="utf-8")
        os.replace(archived, journal / "original")
        recover_history_deletions(self.store)
        self.assertTrue(archived.is_file())
        self.assertFalse(journal.exists())

    def test_organized_task_conversion_preserves_topics_and_discards_provenance(self):
        content = organize_materials([("甲.md", "## 灯光\n\n入口使用暖光。\n\n## 材质\n\n墙面使用石材。")])
        rows = conversion_items(content, {"mode": "automatic", "organized": True})
        self.assertEqual([row["section"] for row in rows], ["灯光", "材质"])
        self.assertEqual([row["title"] for row in rows], ["入口使用暖光。", "墙面使用石材。"])
        self.assertNotIn("来源", str(rows))
        self.assertEqual(len(conversion_items(content, {"mode": "sections", "organized": True})), 2)

    def test_task_parser_preserves_literal_markers_inside_code(self):
        example = "```md\n<!-- reqdoc:tasks -->\n示例内容\n<!-- /reqdoc:tasks -->\n```"
        self.assertEqual(strip_task_block(example), example)
        actual = "正文\n<!-- reqdoc:tasks -->\n```\n任务代码\n```\n<!-- /reqdoc:tasks -->"
        self.assertEqual(strip_task_block(actual), "正文")

    def test_conversion_preview_edits_is_idempotent_and_syncs_source_changes(self):
        app = self.app()
        content = organize_materials([("甲", "## 灯光\n\n入口用暖光。\n\n侧面用补光。")])
        copy_id = "organized-test"
        with self.store.conn() as conn:
            write_import_copy(conn, self.doc.key, copy_id, "整理文档", content)
        app._working_copy_id = copy_id
        app.load_working_document()

        def preview():
            win = app.convert_working_document()
            button = next(widgets(win, ttk.Button, "确认生成任务"))
            deadline = time.monotonic() + 8
            while button.instate(["disabled"]) and time.monotonic() < deadline:
                app.update()
                time.sleep(0.01)
            self.assertFalse(button.instate(["disabled"]))
            return win, button

        win, confirm = preview()
        title = next(widget for widget in widgets(win, ttk.Entry) if type(widget) is ttk.Entry)
        title.delete(0, "end")
        title.insert(0, "编辑后的任务标题")
        confirm.invoke()
        tasks = self.store.tasks(self.doc.key)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].title, "编辑后的任务标题")
        win, _ = preview()
        tree = next(widgets(win, ttk.Treeview))
        self.assertTrue(all(tree.item(iid, "values")[0] == "已关联" for iid in tree.get_children()))
        next(widgets(win, ttk.Button, "取消")).invoke()
        self.assertEqual(len(self.store.tasks(self.doc.key)), 2)
        app.working_text.replace("1.0", "end-1c", content.replace("入口用暖光。", "入口改用冷光。"))
        app.save_working_document()
        self.assertEqual(self.store.service.get_requirement(tasks[0].id).content, "入口改用冷光。")

    def test_changing_conversion_mode_keeps_only_exact_existing_links(self):
        from reqdoc_task_conversion import match_conversion_links
        a = {"title": "甲", "section": "主题", "content": "正文甲"}
        b = {"title": "乙", "section": "主题", "content": "正文乙"}
        links = [(1, 0, a), (2, 1, b)]
        matched = match_conversion_links(links, [b, a], False)
        self.assertEqual([matched[position][0] for position in (0, 1)], [2, 1])
        self.assertFalse(match_conversion_links(links, [{"title": "新项", "section": "主题", "content": "新正文"}], False))

    def test_working_document_has_a_visible_delete_action(self):
        app = self.app()
        self.assertEqual(app.delete_copy_button.cget("text"), "删除当前文档")
        self.assertFalse(app.delete_copy_button.instate(["disabled"]))
        with patch("reqdoc_studio.messagebox.askyesno", return_value=True), patch("reqdoc_studio.messagebox.showinfo"):
            app.delete_copy_button.invoke()
        self.assertIsNone(self.store.service.get_document(self.doc.key))
        self.assertTrue(app.delete_copy_button.instate(["disabled"]))

    def test_migration_can_create_a_library_beside_existing_home_and_docs(self):
        target = self.settings.library_root / "总文档库"
        migrated = migrate_portable_library(self.store, target)
        self.assertTrue(migrated.db_path.is_file())
        self.assertEqual(WorkingStore(migrated).service.document(self.doc.key).name, self.doc.name)

    def test_interrupted_history_journal_write_does_not_block_startup(self):
        folder = self.settings.home / "history-deletions" / "delete-interrupted"
        folder.mkdir(parents=True)
        (folder / "manifest.json.temporary.tmp").write_text("partial", encoding="utf-8")
        recover_history_deletions(self.store)
        self.assertFalse(folder.exists())


    def test_backup_contains_organization_rules_and_restores_them(self):
        rules = {"rules_text": "灯光 = 照明"}
        rule_path = self.settings.home / "organize-rules.json"
        rule_path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
        archive = self.store.backup()
        with zipfile.ZipFile(archive) as package:
            self.assertIn("reqdoc-data/home/organize-rules.json", package.namelist())
            self.assertEqual(
                json.loads(package.read("reqdoc-data/home/organize-rules.json").decode("utf-8")),
                rules,
            )
        restored = self.store.restore_copy(archive, self.root / "restored")
        self.assertEqual(
            json.loads((restored / "reqdoc-data/home/organize-rules.json").read_text("utf-8")),
            rules,
        )

    def test_failed_working_document_save_keeps_edits_and_shows_error(self):
        app = self.app()
        text = "这段内容必须留在编辑窗口中"
        app.working_text.insert("1.0", text)
        with patch("reqdoc_working_document.write_working_document", side_effect=OSError("磁盘写入失败")):
            with patch("reqdoc_working_document.messagebox.showerror") as showerror:
                self.assertFalse(app.save_working_document())
        self.assertTrue(app._working_dirty)
        self.assertIn(text, app.working_text.get("1.0", "end-1c"))
        self.assertIn("保存失败", app.status.get())
        self.assertIn("仍保留", app.status.get())
        showerror.assert_called_once()

    def test_source_and_task_changes_create_resolvable_conflict(self):
        app = self.app()
        source_content = "## 灯光\n\n照明使用暖色调。"
        items = conversion_items(source_content)
        self.assertEqual(len(items), 1)
        item = items[0]
        task = self.store.save_task(
            self.doc.key, item["title"], item["section"], item["content"]
        )
        copy_id = "history-source-conflict"
        with self.store.conn() as conn:
            write_import_copy(conn, self.doc.key, copy_id, "灯光资料.md", source_content)
            save_import_task_link(conn, self.doc.key, copy_id, task.id, 0, item)

        task_edit = item["content"] + "\n任务侧补充了限制条件。"
        self.store.save_task(
            self.doc.key, item["title"], item["section"], task_edit, rid=task.id
        )
        source_updated = source_content.replace("暖色调", "冷色调")
        with self.store.conn() as conn:
            write_import_copy(conn, self.doc.key, copy_id, "灯光资料.md", source_updated)
            changed = sync_import_tasks(conn, self.doc.key, copy_id, source_updated)
        self.assertFalse(changed)
        self.assertEqual(self.store.service.get_requirement(task.id).content, task_edit)
        with self.store.conn() as conn:
            conflict = conn.execute("""
                SELECT id,field,previous_value,source_value,task_value,status
                FROM studio_sync_conflicts
                WHERE doc=? AND copy_id=? AND task_id=? AND status='unresolved'
            """, (self.doc.key, copy_id, task.id)).fetchone()
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict[1], "content")
        self.assertEqual(conflict[4], task_edit)
        self.assertIn("冷色调", conflict[3])
        refresh_sync_conflict_button(app)
        self.assertEqual(app.sync_conflict_button.cget("text"), "同步冲突 (1)")

        resolve_sync_conflict(app, conflict[0], "task")
        self.assertEqual(self.store.service.get_requirement(task.id).content, task_edit)
        with self.store.conn() as conn:
            status = conn.execute(
                "SELECT status,resolution FROM studio_sync_conflicts WHERE id=?",
                (conflict[0],),
            ).fetchone()
            link = json.loads(conn.execute(
                "SELECT source_item FROM studio_import_task_links WHERE doc=? AND copy_id=? AND task_id=?",
                (self.doc.key, copy_id, task.id),
            ).fetchone()[0])
        self.assertEqual(status, ("resolved", "task"))
        self.assertIn("冷色调", link["content"])
        self.assertEqual(app.sync_conflict_button.cget("text"), "同步冲突 (0)")


    def test_source_task_conflict_can_choose_source_content(self):
        app = self.app()
        source_content = "## 灯光\\n\\n照明使用暖色调。"
        item = conversion_items(source_content)[0]
        task = self.store.save_task(
            self.doc.key, item["title"], item["section"], item["content"]
        )
        copy_id = "history-source-choice"
        with self.store.conn() as conn:
            write_import_copy(conn, self.doc.key, copy_id, "灯光资料.md", source_content)
            save_import_task_link(conn, self.doc.key, copy_id, task.id, 0, item)
        task_edit = item["content"] + "\\n任务侧补充限制。"
        self.store.save_task(
            self.doc.key, item["title"], item["section"], task_edit, rid=task.id
        )
        source_updated = source_content.replace("暖色调", "冷色调")
        with self.store.conn() as conn:
            write_import_copy(conn, self.doc.key, copy_id, "灯光资料.md", source_updated)
            sync_import_tasks(conn, self.doc.key, copy_id, source_updated)
            conflict_id = conn.execute(
                "SELECT id FROM studio_sync_conflicts WHERE doc=? AND copy_id=? AND task_id=? AND status='unresolved'",
                (self.doc.key, copy_id, task.id),
            ).fetchone()[0]

        resolve_sync_conflict(app, conflict_id, "source")
        self.assertIn("冷色调", self.store.service.get_requirement(task.id).content)
        self.assertNotIn("任务侧补充限制", self.store.service.get_requirement(task.id).content)
        with self.store.conn() as conn:
            status = conn.execute(
                "SELECT status,resolution FROM studio_sync_conflicts WHERE id=?",
                (conflict_id,),
            ).fetchone()
            baseline = json.loads(conn.execute(
                "SELECT source_item FROM studio_import_task_links WHERE doc=? AND copy_id=? AND task_id=?",
                (self.doc.key, copy_id, task.id),
            ).fetchone()[0])
        self.assertEqual(status, ("resolved", "source"))
        self.assertIn("冷色调", baseline["content"])


    def test_import_preparation_keeps_good_unicode_files_when_one_file_fails(self):
        folder = self.root / "中文资料目录"
        folder.mkdir()
        good = folder / "场景说明.md"
        bad = folder / "损坏文件.txt"
        good.write_text("# 场景说明\\n\\n保留这份内容。", encoding="utf-8")
        bad.write_text("unreadable", encoding="utf-8")

        def reader(path):
            if path.name == bad.name:
                raise ValueError("模拟编码或格式错误")
            return path.read_text("utf-8"), []

        prepared, errors = prepare_import_documents([good, bad], reader)
        self.assertEqual(prepared, [(good.resolve(), good.read_text("utf-8"))])
        self.assertEqual(len(errors), 1)
        self.assertIn("损坏文件.txt", errors[0])
        self.assertIn("模拟编码或格式错误", errors[0])

    def test_restore_rules_validates_before_backup_and_replaces_atomically(self):
        archive = self.root / "rules-backup.zip"
        target = self.settings.home / "organize-rules.json"
        target.write_text(json.dumps({"rules_text": "旧规则"}), encoding="utf-8")
        expected = {"mode": "topic", "merge_duplicates": True, "suggest_similar": False,
                    "rules_text": "灯光 = 照明"}
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            package.writestr("reqdoc-data/home/organize-rules.json",
                             json.dumps(expected, ensure_ascii=False).encode("utf-8"))
        calls = []
        safety_backup = self.root / "before-restore.zip"
        result = restore_organization_rules_from_backup(
            archive, target, lambda: calls.append(True) or safety_backup
        )
        self.assertEqual(result, safety_backup)
        self.assertEqual(calls, [True])
        self.assertEqual(json.loads(target.read_text("utf-8")), expected)

    def test_invalid_rules_backup_is_rejected_before_creating_safety_backup(self):
        archive = self.root / "invalid-rules-backup.zip"
        target = self.settings.home / "organize-rules.json"
        original = {"mode": "topic", "merge_duplicates": True,
                    "suggest_similar": True, "rules_text": "旧规则"}
        target.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            package.writestr("reqdoc-data/home/organize-rules.json",
                             json.dumps({"mode": "invalid"}).encode("utf-8"))
        calls = []
        with self.assertRaises(ValueError):
            restore_organization_rules_from_backup(
                archive, target, lambda: calls.append(True)
            )
        self.assertEqual(calls, [])
        self.assertEqual(json.loads(target.read_text("utf-8")), original)
