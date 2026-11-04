"""Regression tests for the installed working-document UI on Windows."""
import os
import tempfile
import tkinter as tk
import unittest
import zipfile
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import patch

from docx import Document
from PIL import Image

from reqdoc.config import Settings
from reqdoc_studio import CompactStudio, Store
from reqdoc_working_document import (
    TASK_BLOCK_START,
    document_to_task_items,
    install,
    read_working_document,
    working_document_menu,
    write_working_document,
)


class WorkingStudio(CompactStudio):
    pass


class WorkingStore(Store):
    pass


install(WorkingStudio, WorkingStore)


def widgets(parent, kind, label=None):
    for child in parent.winfo_children():
        if isinstance(child, kind) and (label is None or child.cget("text") == label):
            yield child
        yield from widgets(child, kind, label)


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.appdata = self.root / "appdata"
        config = self.appdata / "ReqDocStudio" / "library.json"
        library = config.parent / "data"
        settings = Settings(home=library / "home", docs_dir=library / "docs", library_config=config)
        settings.ensure_directories()
        self.store = WorkingStore(settings)
        self.project = self.store.service.create_project_group("项目 A")
        self.doc = self.store.service.create_document(self.project.name, "文档 001")
        self.app = WorkingStudio(self.store)
        self.addCleanup(self.app.close)
        errors = patch("reqdoc_studio.messagebox.showerror", side_effect=lambda title, message, **kwargs: self.fail(message))
        errors.start()
        self.addCleanup(errors.stop)
        self.app.geometry("900x600")
        self.app.report_callback_exception = lambda exc, value, tb: self.fail(f"Tk callback failed: {value}")
        self.app.load_doc(self.doc.key)

    def button(self, parent, label):
        return next(widgets(parent, ttk.Button, label))

    def test_task_navigation_dialogs_and_live_document(self):
        self.app.nav_buttons["tasks"].invoke()
        self.app.update()
        self.assertTrue(self.app._task_sashes_initialized)
        self.app.working_text.insert("1.0", "未保存的手写正文")

        def save_dialog(win, title):
            win.geometry("560x400")
            self.app.update()
            confirm = self.button(win, "确定")
            self.assertTrue(confirm.winfo_ismapped())
            self.assertGreater(confirm.winfo_height(), 10)
            next(widgets(win, ttk.Entry)).insert(0, title)
            confirm.invoke()

        save_dialog(self.app.new_task(), "原任务")
        save_dialog(self.app.insert_after_selected(), "插入任务")
        save_dialog(self.app.edit_selected(), "修改")
        first, second = self.store.tasks(self.doc.key)
        self.assertEqual((first.title, second.title), ("原任务", "修改插入任务"))
        self.button(self.app.pages["tasks"], "完成").invoke()
        content = self.app.working_text.get("1.0", "end-1c")
        self.assertIn("未保存的手写正文", content)
        self.assertIn(f"> #{second.id} · 已完成", content)
        self.app.mark(False)
        self.assertIn(f"> #{second.id} · 未完成", self.app.working_text.get("1.0", "end-1c"))
        self.app.save_working_document()
        self.assertNotIn(TASK_BLOCK_START, read_working_document(self.store, self.doc.key))
        self.app.load_working_document()
        self.assertEqual(self.app.working_text.get("1.0", "end-1c").count(TASK_BLOCK_START), 1)

    def test_delete_project_from_tree_does_not_create_backup(self):
        task = self.store.save_task(self.doc.key, "待删除任务", "章节", "项目内容")
        source = self.root / "source.txt"
        source.write_text("原始资料", encoding="utf-8")
        self.store.archive_source(self.doc.key, source)
        folder = self.store.service.document_folder(self.doc.key)
        other = self.store.service.create_project_group("另一个项目")
        other_doc = self.store.service.create_document(other.name, "保留的文档")
        self.app.show_page("projects")
        self.app.refresh_projects_page()
        self.app.project_list.selection_set(f"project-{self.project.id}")
        with patch.object(self.store, "backup", side_effect=AssertionError("backup called")), \
                patch("reqdoc_studio.messagebox.askyesno", return_value=True) as confirm, \
                patch("reqdoc_studio.messagebox.showinfo") as notice:
            self.button(self.app.pages["projects"], "删除选中项").invoke()
        self.assertIn("不可撤销", confirm.call_args.args[1])
        notice.assert_called_once_with("删除完成", "已删除所选项目。", parent=self.app)
        self.assertIsNone(self.app.key)
        self.assertEqual(self.store.service.project_groups(), [other])
        self.assertEqual(self.store.service.documents_for(other.name), [other_doc])
        with self.assertRaises(ValueError):
            self.store.service.get_requirement(task.id)
        self.assertFalse(folder.exists())
        self.assertFalse((self.store.settings.library_root / "studio-backups").exists())

    def test_progress_tree_shows_empty_documents_and_keeps_selection(self):
        empty = self.store.service.create_document(self.project.name, "空文档")
        self.app.nav_buttons["progress"].invoke()
        self.app.update()
        tree = self.app.progress_chapters
        project_id, = tree.get_children()
        self.assertEqual(set(tree.get_children(project_id)), {f"document:{self.doc.key}", f"document:{empty.key}"})
        tree.selection_set(f"document:{empty.key}")
        self.app.refresh_progress()
        self.assertEqual(tree.selection(), (f"document:{empty.key}",))
        self.app.progress_scope_var.set(self.project.name)
        self.app.refresh_progress(reset_group=True)
        self.assertEqual(len(tree.get_children()), 2)
        self.assertEqual(tree.get_children(tree.get_children()[0]), ())

    def test_clipboard_image_and_exports(self):
        self.app.show_page("tasks")
        text = self.app.working_text
        text.insert("1.0", "前文\n后文")
        text.mark_set("insert", "2.0")
        with patch("PIL.ImageGrab.grabclipboard", return_value=Image.new("RGB", (20, 20), "blue")), \
                patch("reqdoc_working_document.simpledialog.askstring", return_value=""):
            text.event_generate("<<Paste>>")
            self.app.update()
        content = text.get("1.0", "end-1c")
        self.assertRegex(content, r"^前文\n\n!\[\]\(assets/working/[^)]+\)\n后文$")
        self.assertEqual(len(text.image_names()), 1)
        visible = text.tk.call(text._w, "get", "-displaychars", "1.0", "end-1c")
        self.assertEqual(visible, "前文\n\n\n后文")
        self.app.save_working_document()
        self.app.load_working_document()
        self.assertEqual(len(text.image_names()), 1)
        menu_button = next(widgets(self.app.pages["tasks"], ttk.Menubutton, "导出文档 ▾"))
        menu = self.app.nametowidget(menu_button["menu"])
        word, archive = self.root / "out.docx", self.root / "out.zip"
        with patch("reqdoc_studio.filedialog.asksaveasfilename", side_effect=[str(word), str(archive)]), \
                patch("reqdoc_studio.messagebox.showinfo"):
            menu.invoke(0)
            menu.invoke(1)
        self.assertEqual(len(Document(word).inline_shapes), 1)
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            markdown = bundle.read(next(name for name in names if name.endswith(".md"))).decode()
            self.assertIn("前文", markdown)
            image = next(name for name in names if name.startswith("images/"))
            self.assertIn(f"![]({image})", markdown)

    def test_pasted_image_shows_only_user_description(self):
        self.app.show_page("tasks")
        text = self.app.working_text
        with patch("PIL.ImageGrab.grabclipboard", return_value=Image.new("RGB", (20, 20), "blue")), \
                patch("reqdoc_working_document.simpledialog.askstring", return_value="图像说明"):
            self.app.paste_working_image()
        self.app.update()
        visible = text.tk.call(text._w, "get", "-displaychars", "1.0", "end-1c")
        self.assertEqual(visible.strip(), "图像说明")
        self.assertEqual(len(text.image_names()), 1)
        _, _, position = text.dump("1.0", "end", image=True)[0]
        x, y, _, height = text.bbox(position)
        with patch("reqdoc_working_document._text_menu") as menu:
            working_document_menu(self.app, SimpleNamespace(x=x + 1, y=y + height // 2))
            delete_image = menu.call_args.args[2]
        self.assertIsNotNone(delete_image)
        delete_image()
        self.app.update()
        self.assertFalse(text.image_names())
        self.assertNotIn("assets/working/", text.get("1.0", "end-1c"))

    def test_delete_tasks_removes_selected_records_directly(self):
        first = self.store.save_task(self.doc.key, "待删除任务", "章节", "删除的内容")
        kept = self.store.save_task(self.doc.key, "保留任务", "章节", "保留的内容")
        old_trash = self.store.save_task(self.doc.key, "旧版回收任务", "章节", "旧数据")
        self.store.trash([old_trash.id])
        self.app.show_page("tasks")
        self.app.update()
        self.app.list.selection_set(str(first.id))
        button = self.button(self.app.pages["tasks"], "删除任务")
        self.assertTrue(button.winfo_ismapped())
        self.assertNotIn("回收站", self.app.filter_box["values"])
        with patch("reqdoc_working_document.messagebox.askyesno", return_value=False):
            button.invoke()
        self.assertEqual(self.store.service.get_requirement(first.id), first)
        with patch("reqdoc_working_document.messagebox.askyesno", return_value=True), \
                patch.object(self.store, "backup", side_effect=AssertionError("backup called")):
            button.invoke()
        with self.assertRaises(ValueError):
            self.store.service.get_requirement(first.id)
        self.assertEqual(self.store.tasks(self.doc.key), [kept])
        self.assertEqual(self.store.tasks(self.doc.key, trash=True), [old_trash])
        self.assertNotIn(first.title, self.app.working_text.get("1.0", "end-1c"))

    def test_document_status_edits_and_line_removal_sync_automatically(self):
        first = self.store.save_task(self.doc.key, "应标记完成", "章节", "详情\n另一行")
        second = self.store.save_task(self.doc.key, "应删除", "章节", "原任务详情")
        self.app.show_page("tasks")
        self.app.update()
        text = self.app.working_text

        def wait_for_sync():
            finished = tk.BooleanVar(master=self.app, value=False)
            self.app.after(475, lambda: finished.set(True))
            self.app.wait_variable(finished)

        start, end, _ = self.app._working_task_marks[first.id]
        text.replace(start, end, f"> #{first.id} · 完成")
        wait_for_sync()
        self.assertTrue(self.store.service.get_requirement(first.id).done)
        self.assertEqual(self.app.list.item(str(first.id), "values")[1], "已完成")
        self.assertIn(f"> #{first.id} · 已完成", text.get("1.0", "end-1c"))
        start, _, _ = self.app._working_task_marks[second.id]
        text.delete(start, f"{start} +1 lines linestart")
        wait_for_sync()
        self.assertFalse(self.app.list.exists(str(second.id)))
        with self.assertRaises(ValueError):
            self.store.service.get_requirement(second.id)
        self.assertNotIn(second.title, text.get("1.0", "end-1c"))
        self.assertTrue(self.store.service.get_requirement(first.id).done)

    def test_document_context_menu_adds_task_after_clicked_task(self):
        first = self.store.save_task(self.doc.key, "第一项", "章节", "保留正文")
        second = self.store.save_task(self.doc.key, "后续任务", "章节", "详情")
        self.app.show_page("tasks")
        self.app.update()
        text = self.app.working_text
        start = self.app._working_task_marks[first.id][0]
        body = f"{start} +2 lines linestart"
        text.tag_add("sel", body, f"{body} lineend")
        text.see(body)
        self.app.update()
        x, y, _, height = text.bbox(body)
        with patch("reqdoc_working_document._text_menu") as menu:
            working_document_menu(self.app, SimpleNamespace(x=x + 1, y=y + height // 2))
            add_task = menu.call_args.args[3]
        dialog = add_task()
        details = next(widgets(dialog, tk.Text))
        self.assertEqual(details.get("1.0", "end-1c"), first.content)
        next(widgets(dialog, ttk.Entry)).insert(0, "右键新增")
        self.button(dialog, "确定").invoke()
        self.assertEqual([row.title for row in self.store.tasks(self.doc.key)],
                         [first.title, "右键新增", second.title])

    def test_library_settings_switch_copies_current_data_and_updates_startup(self):
        self.app.show_page("settings")
        page = self.app.pages["settings"]
        labels = {button.cget("text") for button in widgets(page, ttk.Button)}
        self.assertEqual(labels, {"更改路径", "检查 GitHub 更新", "使用说明"})
        self.app.working_text.insert("1.0", "尚未保存的正文")
        task = self.store.save_task(self.doc.key, "保留任务", "章节", "保留的内容")
        doc_folder = self.store.service.document_folder(self.doc.key)
        image = doc_folder / "assets" / "working" / "image.png"
        image.parent.mkdir(parents=True)
        Image.new("RGB", (4, 4), "blue").save(image)
        old_db = self.store.settings.db_path
        target = self.root / "新文件库"
        target.mkdir()
        with patch("reqdoc_working_document.filedialog.askdirectory", return_value=str(target)), \
                patch("reqdoc_working_document.messagebox.askyesno", return_value=True):
            self.button(page, "更改路径").invoke()
        self.assertTrue(self.app._library_changed)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.appdata), "REQDOC_V8_DATA": ""}):
            active = Settings.from_env()
        self.assertTrue(active.library_root.samefile(target))
        reopened = WorkingStore(active)
        self.assertEqual(reopened.service.get_requirement(task.id), task)
        self.assertIn("尚未保存的正文", read_working_document(reopened, self.doc.key))
        self.assertEqual((reopened.service.document_folder(self.doc.key) / image.relative_to(doc_folder)).read_bytes(),
                         image.read_bytes())
        self.assertTrue(old_db.is_file())

    def test_library_switch_keeps_current_path_when_config_write_fails(self):
        target = self.root / "新文件库"
        target.mkdir()
        with patch("reqdoc_studio.write_atomic", side_effect=OSError("模拟配置写入失败")), \
                self.assertRaises(OSError):
            self.store.relocate_library(target)
        self.assertFalse(self.store.settings.library_config.exists())
        self.assertFalse(list(target.iterdir()))
        self.assertEqual(self.store.service.document(self.doc.key), self.doc)

    def test_undoing_prose_does_not_remove_generated_task_lines(self):
        text = self.app.working_text
        text.insert("1.0", "手写正文")
        task = self.store.save_task(self.doc.key, "保持任务", "章节", "详情")
        self.app.show_page("tasks")
        self.app.update()
        text.edit_undo()
        self.app.update()
        self.assertNotIn("手写正文", text.get("1.0", "end-1c"))
        self.assertIn(f"> #{task.id} · 未完成", text.get("1.0", "end-1c"))
        self.assertEqual(self.store.tasks(self.doc.key), [task])

    def test_split_merge_replacement_rolls_back_and_keeps_order(self):
        first = self.store.save_task(self.doc.key, "原任务", "章节", "详情")
        kept = self.store.save_task(self.doc.key, "后续任务", "章节", "详情")
        replacement = {"title": "拆分后任务", "section": "章节", "content": "拆分后的详情"}
        with patch.object(self.store.service.db, "delete_requirements", side_effect=RuntimeError("失败")), \
                self.assertRaises(RuntimeError):
            self.store.replace_tasks(self.doc.key, [first.id], [replacement])
        self.assertEqual(self.store.tasks(self.doc.key), [first, kept])
        self.store.replace_tasks(self.doc.key, [first.id], [replacement])
        self.assertEqual([row.title for row in self.store.tasks(self.doc.key)], [replacement["title"], kept.title])
        self.assertEqual(self.store.tasks(self.doc.key, trash=True), [])

    def test_word_import_and_convert_preserve_table_and_prose(self):
        source = self.root / "input.docx"
        word = Document()
        word.add_heading("背景", 1)
        word.add_paragraph("保留这段正文")
        word.add_heading("界面", 1)
        table = word.add_table(rows=2, cols=3)
        for row, values in zip(table.rows, [("编号", "名称", "分组"), ("001", "检查按钮", "交互")]):
            for cell, value in zip(row.cells, values):
                cell.text = value
        word.save(source)
        before = source.read_bytes()
        with patch("reqdoc_working_document.filedialog.askopenfilenames", return_value=(str(source),)), \
                patch("reqdoc_working_document.messagebox.showwarning"):
            self.app.show_page("tasks")
            self.button(self.app.pages["tasks"], "导入文档").invoke()
            win = next(w for w in widgets(self.app, tk.Toplevel) if w.wm_title() == "导入文件：形成工作文档或任务")
            self.button(win, "确认导入").invoke()
        with patch("reqdoc_working_document.messagebox.askyesno", return_value=True):
            self.button(self.app.pages["tasks"], "转成任务").invoke()
        self.assertEqual([(row.title, row.section) for row in self.store.tasks(self.doc.key)],
                         [("背景", "背景"), ("检查按钮", "交互")])
        self.assertEqual(source.read_bytes(), before)
        editable = self.app.working_text.get("1.0", "end-1c")
        self.assertEqual(len(document_to_task_items(editable)), 2)

    def test_failed_direct_import_rolls_back_source_and_working_copy(self):
        source = self.root / "input.txt"
        source.write_text("待导入任务", encoding="utf-8")
        self.app.working_text.insert("1.0", "原正文")
        self.app.save_working_document()
        win = self.app.import_paths([source])
        next(w for w in widgets(win, ttk.Radiobutton) if w.cget("value") == "tasks").invoke()
        confirm = self.button(win, "确认导入")
        with patch.object(self.store.service, "commit_task_rows", side_effect=RuntimeError("模拟批量写入失败")), \
                patch("reqdoc_studio.messagebox.showerror") as error, \
                patch("reqdoc_working_document.logging.exception"):
            confirm.invoke()
        error.assert_called_once()
        self.assertEqual(self.store.sources(self.doc.key), [])
        self.assertEqual(read_working_document(self.store, self.doc.key), "原正文")
        self.assertFalse(list(self.store.service.document_folder(self.doc.key).glob("_sources/*/*")))
        confirm.invoke()
        self.assertEqual([row.title for row in self.store.tasks(self.doc.key)], ["待导入任务"])
        self.assertEqual(len(self.store.sources(self.doc.key)), 1)

    def test_legacy_generated_summary_updates_without_duplication(self):
        row = self.store.save_task(self.doc.key, "原任务", "章节", "任务详情")
        self.store.snapshot(self.doc.key)
        write_working_document(self.store, self.doc.key, self.store.compose(self.doc.key), "legacy-derived")
        self.store.service.set_done(row.id, True)
        self.app.load_working_document()
        editable = self.app.working_text.get("1.0", "end-1c")
        self.assertEqual(editable.count("任务详情"), 1)
        self.assertIn(f"> #{row.id} · 已完成", editable)

    def test_existing_word_working_copy_with_spaced_rows_converts(self):
        rows = document_to_task_items(
            "| 任务 | 分组 |\n\n| --- | --- |\n\n| 甲 | UI |\n\n| 乙 | UI |"
        )
        self.assertEqual([row["title"] for row in rows], ["甲", "乙"])
