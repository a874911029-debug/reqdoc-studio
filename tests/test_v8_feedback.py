"""Regression tests for the installed working-document UI on Windows."""
import tempfile
import tkinter as tk
import unittest
import zipfile
from pathlib import Path
from tkinter import ttk
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
        settings = Settings(home=self.root / "home", docs_dir=self.root / "docs")
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
