"""Regression tests for the installed working-document UI on Windows."""
import os
import sys
import tempfile
import time
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
from reqdoc.gui.widgets import MarkdownView
from reqdoc_studio import CompactStudio, Store
from reqdoc_working_document import (
    TASK_BLOCK_START,
    MAIN_DOCUMENT_LABEL,
    document_to_task_items,
    install,
    on_working_view_selected,
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

    def test_document_first_workspace_has_topbar_tree_and_inspector(self):
        self.app.update_idletasks()
        self.assertTrue(self.app.doc_sidebar.winfo_ismapped())
        self.assertTrue(self.app.nav_buttons["tasks"].winfo_ismapped())
        self.assertIs(self.app.nav_buttons["tasks"].master, self.app.nav)

        # Search is a view-only filter: projects and documents remain unchanged.
        self.app.show_page("settings")
        self.app.sidebar_query.set("不存在的搜索条目")
        self.assertEqual(self.app.tree.get_children(), ())
        self.app.sidebar_query.set("文档 001")
        self.assertTrue(self.app.tree.exists("d:" + self.doc.key))
        self.assertTrue(self.app.pages["settings"].winfo_ismapped())

        self.app.tree.selection_set("d:" + self.doc.key)
        self.app.sidebar_tree_select()
        self.app.update_idletasks()
        self.assertTrue(self.app.pages["tasks"].winfo_ismapped())
        self.assertTrue(self.app.task_working_panel.winfo_ismapped())
        self.assertTrue(self.app.task_right_panel.winfo_ismapped())
        self.assertEqual(self.store.service.get_document(self.doc.key).name, "文档 001")

    def wait_for_organization(self, dialog):
        save = self.button(dialog, "另存整理文档")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and save.instate(["disabled"]):
            self.app.update()
            time.sleep(0.01)
        self.assertFalse(save.instate(["disabled"]), "organization did not finish")

    def convert_preview(self, commit=True):
        dialog = self.app.convert_working_document()
        button = self.button(dialog, "确认生成任务")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and button.instate(["disabled"]):
            self.app.update()
            time.sleep(0.01)
        self.assertFalse(button.instate(["disabled"]))
        if commit:
            button.invoke()
        return dialog

    def test_organize_saves_separate_document_without_task_changes(self):
        first = self.root / "甲.md"
        second = self.root / "乙.txt"
        first.write_text("## 背景\n\n共有段落\n\n## 参考\n\n甲的内容", encoding="utf-8")
        second.write_text("## 背景\n\n共有段落\n\n## 说明\n\n乙的内容", encoding="utf-8")
        dialog = self.app.import_paths([first, second])
        self.button(dialog, "确认导入").invoke()
        self.store.save_task(self.doc.key, "原有任务", "", "原有内容")
        self.app.working_view_var.set(MAIN_DOCUMENT_LABEL)
        on_working_view_selected(self.app)
        self.app.working_text.insert("1.0", "未保存的散记\n\n")
        before = self.store.tasks(self.doc.key)
        original = first.read_bytes()

        self.button(self.app.task_working_panel, "一键整理").invoke()
        dialog = next(win for win in widgets(self.app, tk.Toplevel) if win.title() == "一键整理资料")
        self.wait_for_organization(dialog)
        preview = next(widgets(dialog, tk.Text))
        result = preview.get("1.0", "end-1c")
        self.assertEqual(result.count("共有段落"), 1)
        for expected in ("甲.md", "乙.txt", "甲的内容", "乙的内容", "未保存的散记"):
            self.assertIn(expected, result)
        self.assertNotIn("原有任务", result)
        preview.insert("end", "整理补充")
        self.button(dialog, "另存整理文档").invoke()

        self.assertEqual(self.store.tasks(self.doc.key), before)
        self.assertEqual(first.read_bytes(), original)
        self.assertTrue(self.app.working_text.get("1.0", "end-1c").startswith("未保存的散记"))
        result_label = next(label for label in self.app._working_view_ids if label.startswith("整理 · "))
        self.app.working_view_var.set(result_label)
        on_working_view_selected(self.app)
        self.assertIn("整理补充", self.app.working_text.get("1.0", "end-1c"))
        self.app.working_text.insert("end", "\n再编辑")
        self.app.save_working_document()
        self.app.load_working_document()
        self.assertIn("再编辑", self.app.working_text.get("1.0", "end-1c"))
        self.assertEqual(self.store.tasks(self.doc.key), before)

    def test_organize_rules_persist_and_cancel_does_not_save_document(self):
        import json

        self.app.working_text.insert("1.0", "照明使用暖色调。\n\n纹理使用石材。")
        self.button(self.app.task_working_panel, "一键整理").invoke()
        dialog = next(win for win in widgets(self.app, tk.Toplevel) if win.title() == "一键整理资料")
        self.wait_for_organization(dialog)
        self.button(dialog, "整理规则").invoke()
        editor = next(win for win in widgets(dialog, tk.Toplevel) if win.title() == "整理规则")
        rule_text = next(widgets(editor, tk.Text))
        rule_text.insert("1.0", "材质 = 纹理\n灯光 = 照明")
        self.button(editor, "应用规则").invoke()
        self.assertTrue(self.button(dialog, "另存整理文档").instate(["disabled"]))
        self.button(dialog, "重新整理所选资料").invoke()
        self.wait_for_organization(dialog)
        preview = next(widgets(dialog, tk.Text)).get("1.0", "end-1c")
        self.assertLess(preview.index("## 材质"), preview.index("## 灯光"))
        preferences = json.loads((self.store.settings.home / "organize-rules.json").read_text("utf-8"))
        self.assertEqual(preferences["rules_text"], "材质 = 纹理\n灯光 = 照明")
        self.button(dialog, "取消").invoke()
        with self.store.conn() as conn:
            self.assertFalse(conn.execute("SELECT id FROM studio_import_copies WHERE doc=?", (self.doc.key,)).fetchall())
        self.assertFalse(self.store.tasks(self.doc.key))

    def test_organize_keeps_task_marker_examples_inside_imported_code(self):
        source = self.root / "代码范例.md"
        content = "```md\n<!-- reqdoc:tasks -->\n保留的范例\n<!-- /reqdoc:tasks -->\n```"
        source.write_text(content, encoding="utf-8")
        imported = self.app.import_paths([source])
        self.button(imported, "确认导入").invoke()
        self.button(self.app.task_working_panel, "一键整理").invoke()
        dialog = next(win for win in widgets(self.app, tk.Toplevel) if win.title() == "一键整理资料")
        self.wait_for_organization(dialog)
        self.assertIn(content, next(widgets(dialog, tk.Text)).get("1.0", "end-1c"))
        self.button(dialog, "取消").invoke()

    def test_organize_can_close_while_background_work_is_running(self):
        import threading
        from reqdoc_organize import OrganizationCancelled

        self.app.working_text.insert("1.0", "后台整理测试正文")
        started = threading.Event()
        stopped = threading.Event()

        def slow_work(materials, options, *, cancel):
            started.set()
            deadline = time.monotonic() + 3
            while not cancel() and time.monotonic() < deadline:
                time.sleep(0.01)
            stopped.set()
            raise OrganizationCancelled()

        with patch("reqdoc_organize_dialog.organize_with_report", side_effect=slow_work):
            self.button(self.app.task_working_panel, "一键整理").invoke()
            self.assertTrue(started.wait(2))
            dialog = next(win for win in widgets(self.app, tk.Toplevel) if win.title() == "一键整理资料")
            self.button(dialog, "取消").invoke()
            self.assertTrue(stopped.wait(2))
            self.app.update()
        with self.store.conn() as conn:
            self.assertFalse(conn.execute("SELECT id FROM studio_import_copies WHERE doc=?", (self.doc.key,)).fetchall())

    def test_delete_imported_copy_persists_and_keeps_existing_tasks(self):
        from reqdoc_working_document import read_import_copy, write_import_copy

        first = self.root / "待删除.md"
        second = self.root / "保留.md"
        first.write_text("## 标题\n\n已导入内容", encoding="utf-8")
        second.write_text("另一份副本", encoding="utf-8")
        imported = self.app.import_paths([first, second])
        self.button(imported, "确认导入").invoke()
        copy_id = self.app._working_copy_id
        copy_label = self.app.working_view_var.get()
        self.convert_preview()
        tasks = self.store.tasks(self.doc.key)
        self.assertTrue(tasks)
        self.app.working_text.insert("end", "未保存的修改")
        button = self.app.delete_copy_button
        self.assertFalse(button.instate(["disabled"]))
        with patch("reqdoc_working_document.messagebox.askyesno", return_value=False):
            button.invoke()
        self.assertEqual(self.app._working_copy_id, copy_id)
        self.assertIn("未保存的修改", self.app.working_text.get("1.0", "end-1c"))
        with patch("reqdoc_working_document.messagebox.askyesno", return_value=True):
            button.invoke()
        self.assertNotIn(copy_label, self.app._working_view_ids)
        self.assertIsNone(self.app._working_copy_id)
        self.assertFalse(button.instate(["disabled"]))
        self.assertEqual(button.cget("text"), "删除当前文档")
        self.assertEqual(self.store.tasks(self.doc.key), tasks)
        self.assertTrue(any("保留.md" in label for label in self.app._working_view_ids))
        reopened = WorkingStore(self.store.settings)
        with self.assertRaises(ValueError):
            read_import_copy(reopened, self.doc.key, copy_id)
        with reopened.conn() as conn:
            self.assertIsNone(conn.execute("SELECT stored FROM studio_sources WHERE doc=? AND id=?", (self.doc.key, copy_id)).fetchone())
            self.assertFalse(conn.execute("SELECT 1 FROM studio_import_task_links WHERE doc=? AND copy_id=?", (self.doc.key, copy_id)).fetchone())
        with self.assertRaises(ValueError), reopened.conn() as conn:
            write_import_copy(conn, self.doc.key, copy_id, first.name, "旧回调写入")
        self.app.load_working_document()
        self.assertNotIn(copy_label, self.app._working_view_ids)
        imported = self.app.import_paths([first])
        self.button(imported, "确认导入").invoke()
        self.assertNotEqual(self.app._working_copy_id, copy_id)
        self.assertIn("已导入内容", self.app.working_text.get("1.0", "end-1c"))

    def test_delete_organized_result_removes_it_from_the_document_list(self):
        self.app.working_text.insert("1.0", "整理测试内容")
        self.button(self.app.task_working_panel, "一键整理").invoke()
        dialog = next(win for win in widgets(self.app, tk.Toplevel) if win.title() == "一键整理资料")
        self.wait_for_organization(dialog)
        self.button(dialog, "另存整理文档").invoke()
        label = next(label for label in self.app._working_view_ids if label.startswith("整理 · "))
        self.app.working_view_var.set(label)
        on_working_view_selected(self.app)
        copy_id = self.app._working_copy_id
        with patch("reqdoc_working_document.messagebox.askyesno", return_value=True):
            self.app.delete_copy_button.invoke()
        self.app.load_working_document()
        self.assertNotIn(label, self.app._working_view_ids)
        with self.store.conn() as conn:
            self.assertFalse(conn.execute("SELECT id FROM studio_import_copies WHERE doc=? AND id=?", (self.doc.key, copy_id)).fetchone())

    @unittest.skipUnless(sys.platform == "win32", "requires Windows native messages")
    def test_native_file_drop_on_document_editor(self):
        import ctypes
        from ctypes import wintypes
        import struct

        path = self.root / "拖进编辑区.docx"
        document = Document()
        document.add_paragraph("拖入的 Word 正文")
        document.save(path)
        self.app.show_page("tasks")
        self.app.update()
        if self.app._drop_after_id:
            self.app.after_cancel(self.app._drop_after_id)
        self.assertTrue(self.app.install_file_drop())

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel.GlobalLock.restype = ctypes.c_void_p
        kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel.GlobalFree.restype = wintypes.HGLOBAL
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.PostMessageW.restype = wintypes.BOOL
        data = struct.pack("<IiiII", 20, 0, 0, 0, 1) + (str(path) + "\0\0").encode("utf-16-le")
        handle = kernel.GlobalAlloc(2, len(data))
        self.assertTrue(handle)
        try:
            ptr = kernel.GlobalLock(handle)
            self.assertTrue(ptr)
            ctypes.memmove(ptr, data, len(data))
            kernel.GlobalUnlock(handle)
            self.assertTrue(user.PostMessageW(self.app.working_text.winfo_id(), 0x0233, handle, 0))
        except BaseException:
            kernel.GlobalFree(handle)
            raise

        deadline = time.monotonic() + 3
        dialog = None
        while time.monotonic() < deadline and dialog is None:
            self.app.update()
            dialog = next((win for win in widgets(self.app, tk.Toplevel) if win.title().startswith("导入文件：")), None)
            time.sleep(0.01)
        self.assertIsNotNone(dialog, "native drop did not reach the import dialog")
        with patch("reqdoc_working_document.messagebox.showwarning") as warning:
            self.button(dialog, "确认导入").invoke()
        self.assertEqual(warning.call_args.args[0], "导入提示")
        self.assertIn("拖入的 Word 正文", self.app.working_text.get("1.0", "end-1c"))
        self.assertFalse(self.store.tasks(self.doc.key))

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

    def test_document_titles_and_task_details_have_distinct_visual_styles(self):
        self.app.show_page("tasks")
        source = "# 文档标题\n## 章节\n### 任务标题\n正文\n> #42 · 未完成\n```\n# 代码\n```"
        text = self.app.working_text
        text.insert("1.0", source)
        self.app.update()
        for line, tag in [(1, "document_heading_1"), (2, "document_heading_2"),
                          (3, "document_heading_3"), (5, "document_metadata")]:
            self.assertIn(tag, text.tag_names(f"{line}.0"))
        self.assertNotIn("document_heading_1", text.tag_names("7.0"))
        self.assertEqual(text.get("1.0", "end-1c"), source)
        heading_font = text.tk.splitlist(text.tag_cget("document_heading_1", "font"))
        task_font = text.tk.splitlist(text.tag_cget("document_heading_3", "font"))
        self.assertGreater(int(heading_font[1]), int(task_font[1]))
        self.assertEqual(task_font[2], "bold")
        preview = self.app.preview_working_document()
        self.addCleanup(preview.destroy)
        self.assertIn("document_heading_2", next(widgets(preview, MarkdownView)).text.tag_names("2.0"))
        task = self.store.save_task(self.doc.key, "已选任务", "分组", "任务详情")
        self.app.refresh_tasks()
        self.app.list.selection_set(str(task.id))
        self.app.on_task_selected()
        detail = self.app.task_detail
        self.assertIn("detail_title", detail.tag_names("1.0"))
        self.assertIn("detail_metadata", detail.tag_names("2.0"))
        self.assertNotIn("detail_metadata", detail.tag_names("4.0"))

    def select_working_view(self, label):
        self.app.working_view_var.set(label)
        self.app.working_view_box.event_generate("<<ComboboxSelected>>")
        self.app.update()

    def test_imported_copies_switch_separately_and_sync_associated_tasks(self):
        first_file, second_file = self.root / "资料甲.md", self.root / "资料乙.md"
        first_file.write_text("# 甲任务\n\n原始详情", encoding="utf-8")
        second_file.write_text("# 乙任务\n\n第二份内容", encoding="utf-8")
        text = self.app.working_text
        text.insert("1.0", "工作文档正文")
        dialog = self.app.import_paths([first_file, second_file])
        self.button(dialog, "确认导入").invoke()
        self.assertEqual(text.get("1.0", "end-1c"), first_file.read_text("utf-8"))
        self.convert_preview()
        repeated = self.convert_preview(commit=False)
        self.button(repeated, "取消").invoke()
        task, = self.store.tasks(self.doc.key)
        self.store.service.set_done(task.id, True)
        text.replace("3.0", "end-1c", "更新后的详情")
        self.app.save_working_document()
        updated = self.store.service.get_requirement(task.id)
        self.assertEqual(updated.content, "更新后的详情")
        self.assertTrue(updated.done)
        self.assertNotIn(TASK_BLOCK_START, text.get("1.0", "end-1c"))
        text.event_generate("<<Undo>>")
        self.app.update()
        self.app.save_working_document()
        self.assertEqual(self.store.service.get_requirement(task.id).content, "原始详情")
        self.select_working_view("副本 · 资料乙.md")
        self.assertEqual(text.get("1.0", "end-1c"), second_file.read_text("utf-8"))
        self.select_working_view(MAIN_DOCUMENT_LABEL)
        working = text.get("1.0", "end-1c")
        self.assertTrue(working.startswith("工作文档正文"))
        self.assertIn(f"> #{task.id} · 已完成", working)
        self.assertNotIn("第二份内容", working)
        self.assertEqual(first_file.read_text("utf-8"), "# 甲任务\n\n原始详情")

    def test_existing_edited_import_is_preserved_as_a_copy(self):
        source = self.root / "旧资料.md"
        source.write_text("# 任务甲\n\n原文", encoding="utf-8")
        self.store.archive_source(self.doc.key, source)
        task_id, = self.store.service.commit_task_rows(
            self.doc.key, [{"title": "任务甲", "section": "任务甲", "content": "原文"}]
        )
        write_working_document(self.store, self.doc.key, "# 任务甲\n\n旧版编辑", "import-table")
        self.app.load_working_document()
        self.app.show_page("tasks")
        self.assertNotIn("旧版编辑", self.app.working_text.get("1.0", "end-1c"))
        self.select_working_view("副本 · 旧资料.md")
        self.assertIn("旧版编辑", self.app.working_text.get("1.0", "end-1c"))
        self.app.save_working_document()
        self.assertEqual(self.store.service.get_requirement(task_id).content, "旧版编辑")

    def test_undo_restores_table_edit_and_pasted_image(self):
        self.app.show_page("tasks")
        text = self.app.working_text
        table = "| 名称 | 内容 |\n| --- | --- |\n| 原值 | 文本 |"
        text.insert("1.0", table)
        dialog = self.app.edit_working_table()
        grid = next(widgets(dialog, ttk.Treeview))
        grid.item("0", values=("修改值", "文本"))
        self.button(dialog, "保存表格修改").invoke()
        self.assertIn("修改值", text.get("1.0", "end-1c"))
        text.event_generate("<<Undo>>")
        self.app.update()
        self.assertEqual(text.get("1.0", "end-1c"), table)
        text.mark_set("insert", "end-1c")
        with patch("PIL.ImageGrab.grabclipboard", return_value=Image.new("RGB", (20, 20), "blue")), \
                patch("reqdoc_working_document.simpledialog.askstring", return_value=""):
            self.app.paste_working_image()
        self.app.update()
        self.assertEqual(len(text.image_names()), 1)
        text.event_generate("<<Undo>>")
        self.app.update()
        self.assertEqual(text.get("1.0", "end-1c"), table)
        self.assertFalse(text.image_names())

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
        self.assertEqual(labels, {"更改路径", "从备份恢复整理规则", "检查 GitHub 更新", "使用说明"})
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
            win = next(w for w in widgets(self.app, tk.Toplevel) if w.wm_title() == "导入文件：创建独立副本")
            self.button(win, "确认导入").invoke()
        self.convert_preview()
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
