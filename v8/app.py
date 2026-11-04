"""ReqDoc Studio V8 Windows interface. Local-only workspace; originals are read-only."""
from __future__ import annotations
import logging
import os
from pathlib import Path
import queue
import re
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from PIL import Image, ImageTk, ImageGrab
from core import Store, VERSION, default_home, parse_tasks, replace_task, safe_title, IMAGE_LINK
import update_client


class App(tk.Tk):
    def __init__(self, store=None):
        super().__init__()
        self.store = store or Store()
        self.doc = None
        self.task_id = None
        self.task_rows = {}
        self._loading = False
        self.dirty_task = False
        self.dirty_doc = False
        self._drag = None
        self._update_queue = queue.Queue()
        self.title(f"文档工作台 · ReqDoc Studio V{VERSION}")
        self.geometry("1370x860")
        self.minsize(1050, 640)
        self._build()
        self.refresh_tree()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.report_callback_exception = self.report_error
        self.after(200, self.poll_updates)

    def report_error(self, exc, value, tb):
        logging.exception("Tk callback failed", exc_info=(exc, value, tb))
        messagebox.showerror("操作未完成", str(value), parent=self)
        self.status.set("操作失败：" + str(value)[:110])

    def action(self, fn):
        def wrapped(*args):
            try:
                return fn(*args)
            except Exception as exc:
                self.report_error(type(exc), exc, exc.__traceback__)
        return wrapped

    def _button(self, parent, title, fn):
        return ttk.Button(parent, text=title, command=self.action(fn))

    def _build(self):
        self.status = tk.StringVar(value="选择项目和文档，或从左上角新建。")
        top = ttk.Frame(self, padding=(12, 10))
        top.pack(fill="x")
        ttk.Label(top, text="文档工作台", font=("Microsoft YaHei UI", 17, "bold")).pack(side="left", padx=(0, 15))
        for title, fn in [
            ("新建项目", self.new_project), ("新建文档", self.new_document),
            ("导入资料", self.import_sources), ("新增任务", self.new_task),
            ("添加图片", self.add_image), ("粘贴图片", self.paste_image),
            ("查看原件", self.view_originals), ("预览汇总", self.preview),
            ("导出 Word", self.export_word), ("导出资料包", self.export_zip),
            ("备份", self.backup), ("检查更新", self.check_update),
        ]:
            self._button(top, title, fn).pack(side="left", padx=2)

        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=12, pady=(0, 5))

        left = ttk.Frame(pane, padding=(5, 0))
        pane.add(left, weight=1)
        ttk.Label(left, text="项目 / 文档", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=7)
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.action(self.on_tree_select))
        self.tree.bind("<ButtonPress-1>", self._tree_drag_begin)
        self.tree.bind("<B1-Motion>", self._tree_drag_move)
        self.tree.bind("<ButtonRelease-1>", self.action(self._tree_drag_end))
        ttk.Label(left, text="拖动项目可排序；文档可拖动到其他项目或同级调整顺序。",
                  foreground="#64748b", wraplength=220).pack(fill="x", pady=8)

        mid = ttk.Frame(pane, padding=(5, 0))
        pane.add(mid, weight=2)
        self.heading = tk.StringVar(value="请先选择文档")
        ttk.Label(mid, textvariable=self.heading, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=7)
        self.task_list = ttk.Treeview(mid, show="headings", columns=("done", "title"), selectmode="browse")
        self.task_list.heading("done", text="状态")
        self.task_list.heading("title", text="整理后的任务")
        self.task_list.column("done", width=70, stretch=False, anchor="center")
        self.task_list.column("title", width=260, stretch=True)
        self.task_list.pack(fill="both", expand=True)
        self.task_list.bind("<<TreeviewSelect>>", self.action(self.on_task_select))
        ttk.Label(mid, text="单击一条任务进行编辑；任务来源于右侧工作文档中的 ### 标题。",
                  foreground="#64748b", wraplength=350).pack(fill="x", pady=8)

        right = ttk.Frame(pane, padding=(5, 0))
        pane.add(right, weight=3)
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        self.task_tab = ttk.Frame(self.tabs, padding=9)
        self.doc_tab = ttk.Frame(self.tabs, padding=9)
        self.tabs.add(self.task_tab, text="任务编辑")
        self.tabs.add(self.doc_tab, text="整理后的工作文档")
        self.tabs.bind("<<NotebookTabChanged>>", self.action(self.on_tab_change))

        header = ttk.Frame(self.task_tab)
        header.pack(fill="x", pady=(2, 7))
        ttk.Label(header, text="任务标题").pack(side="left")
        self.task_title = tk.StringVar()
        self.task_title.trace_add("write", self._mark_task_var)
        ttk.Entry(header, textvariable=self.task_title).pack(side="left", fill="x", expand=True, padx=8)
        self.done = tk.BooleanVar(value=False)
        self.done.trace_add("write", self._mark_task_var)
        ttk.Checkbutton(header, text="已完成", variable=self.done).pack(side="left")
        self.task_body = tk.Text(self.task_tab, wrap="word", undo=True, font=("Microsoft YaHei UI", 11),
                                 padx=10, pady=8)
        self.task_body.pack(fill="both", expand=True)
        self.task_body.bind("<<Modified>>", self._task_modified)
        self.task_body.bind("<Control-v>", self._maybe_paste_clipboard)
        task_buttons = ttk.Frame(self.task_tab)
        task_buttons.pack(fill="x", pady=(7, 0))
        self._button(task_buttons, "保存任务", self.save_task).pack(side="left")
        self._button(task_buttons, "新建任务", self.new_task).pack(side="left", padx=6)
        ttk.Label(task_buttons, text="图片插入在正文光标位置；原始资料不会被修改。",
                  foreground="#64748b").pack(side="left", padx=5)

        ttk.Label(self.doc_tab, text="这是软件整理后的可编辑文档。以 ### 标题划分任务；可直接编辑整篇。",
                  foreground="#64748b").pack(anchor="w")
        self.doc_body = tk.Text(self.doc_tab, wrap="word", undo=True, font=("Microsoft YaHei UI", 11),
                                padx=10, pady=8)
        self.doc_body.pack(fill="both", expand=True, pady=7)
        self.doc_body.bind("<<Modified>>", self._doc_modified)
        self.doc_body.bind("<Control-v>", self._maybe_paste_clipboard)
        self._button(self.doc_tab, "保存工作文档并同步任务", self.save_document).pack(anchor="w")

        bottom = ttk.Frame(self, padding=(12, 5))
        bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.status, foreground="#475569").pack(side="left")

    def _mark_task_var(self, *_):
        if not self._loading and self.doc is not None:
            self.dirty_task = True

    def _task_modified(self, _=None):
        if self.task_body.edit_modified():
            if not self._loading and self.doc is not None:
                self.dirty_task = True
            self.task_body.edit_modified(False)

    def _doc_modified(self, _=None):
        if self.doc_body.edit_modified():
            if not self._loading and self.doc is not None:
                self.dirty_doc = True
            self.doc_body.edit_modified(False)

    def _commit_pending(self):
        if self.dirty_task:
            ans = messagebox.askyesnocancel("未保存任务", "任务编辑尚未保存，是否先保存？", parent=self)
            if ans is None:
                return False
            if ans:
                self.save_task()
            self.dirty_task = False
        if self.dirty_doc:
            ans = messagebox.askyesnocancel("未保存工作文档", "工作文档尚未保存，是否先保存？", parent=self)
            if ans is None:
                return False
            if ans:
                self.save_document()
            self.dirty_doc = False
        return True

    def refresh_tree(self, select=None):
        current = select or (self.tree.selection()[0] if self.tree.selection() else "")
        self._loading = True
        self.tree.delete(*self.tree.get_children())
        for p in self.store.projects():
            pid = "p:" + p["id"]
            self.tree.insert("", "end", iid=pid, text="▣ " + p["name"], open=True)
            for d in self.store.documents(p["id"]):
                self.tree.insert(pid, "end", iid="d:" + d["id"], text=d["name"])
        if current and self.tree.exists(current):
            self.tree.selection_set(current)
            self.tree.focus(current)
            self.tree.see(current)
        self._loading = False

    def project_id(self):
        sel = self.tree.selection()
        if not sel:
            return None
        item = sel[0]
        if item.startswith("p:"):
            return item[2:]
        if item.startswith("d:"):
            return self.store.document(item[2:])["project_id"]
        return None

    def on_tree_select(self, _=None):
        if self._loading:
            return
        selected = self.tree.selection()
        new_doc = selected[0][2:] if selected and selected[0].startswith("d:") else None
        if new_doc == self.doc:
            return
        if not self._commit_pending():
            self.refresh_tree(select="d:" + self.doc if self.doc else None)
            return
        self.load_document(new_doc)

    def load_document(self, doc):
        self.doc = doc
        self.task_id = None
        self.dirty_task = False
        self.dirty_doc = False
        self._loading = True
        self.task_title.set("")
        self.done.set(False)
        self.task_body.delete("1.0", "end")
        self.task_body.edit_modified(False)
        self.doc_body.delete("1.0", "end")
        if doc:
            data = self.store.document(doc)
            self.heading.set(data["name"])
            self.doc_body.insert("1.0", data["content"])
        else:
            self.heading.set("请先选择一个文档")
        self.doc_body.edit_modified(False)
        self._loading = False
        self.refresh_tasks()
        self.status.set("工作区已加载；原件只读，任务和整理后文档均可修改。" if doc else "请选择项目下的文档。")

    def refresh_tasks(self, select=None):
        self.task_list.delete(*self.task_list.get_children())
        self.task_rows = {}
        if not self.doc:
            return
        rows = parse_tasks(self.store.document(self.doc)["content"])
        for row in rows:
            self.task_rows[row["id"]] = row
            self.task_list.insert("", "end", iid=row["id"],
                                  values=("完成" if row["done"] else "待办", row["title"]))
        if select and select in self.task_rows:
            self.task_list.selection_set(select)
            self.task_list.focus(select)
            self.task_list.see(select)
            self.load_task(select)
        self.heading.set(self.store.document(self.doc)["name"] + f" · {len(rows)} 条任务")

    def on_task_select(self, _=None):
        if self._loading or not self.doc:
            return
        selection = self.task_list.selection()
        if not selection:
            return
        ident = selection[0]
        if ident == self.task_id:
            return
        if self.dirty_task:
            answer = messagebox.askyesnocancel("未保存任务", "保存当前任务的修改吗？", parent=self)
            if answer is None:
                return
            if answer:
                self.save_task()
        if self.dirty_doc:
            if not self._commit_pending():
                return
        self.load_task(ident)
        self.tabs.select(self.task_tab)

    def load_task(self, identity):
        row = self.task_rows.get(identity)
        self._loading = True
        self.task_id = identity if row else None
        self.task_title.set(row["title"] if row else "")
        self.done.set(bool(row["done"]) if row else False)
        self.task_body.delete("1.0", "end")
        if row:
            self.task_body.insert("1.0", row["body"])
        self.task_body.edit_modified(False)
        self.dirty_task = False
        self._loading = False

    def _require_document(self):
        if not self.doc:
            raise ValueError("请先在左侧选择一个文档。")

    def new_project(self):
        name = simpledialog.askstring("新建项目", "项目名称：", parent=self)
        if name is None:
            return
        ident = self.store.add_project(name)
        self.refresh_tree("p:" + ident)
        self.status.set("项目已创建。可以继续新建文档。")

    def new_document(self):
        project = self.project_id()
        if not project:
            raise ValueError("请先选择左侧项目。")
        name = simpledialog.askstring("新建文档", "文档名称：", parent=self)
        if name is None:
            return
        ident = self.store.add_document(project, name)
        self.refresh_tree("d:" + ident)
        self.load_document(ident)

    def import_sources(self):
        self._require_document()
        if not self._commit_pending():
            return
        paths = filedialog.askopenfilenames(parent=self, title="导入原始资料（原件只读归档）",
                                            filetypes=[("资料", "*.docx *.md *.markdown *.txt")])
        if not paths:
            return
        imported = 0
        for file in paths:
            self.store.archive_source(self.doc, Path(file))
            imported += 1
        self.load_document(self.doc)
        self.status.set(f"导入 {imported} 份原始资料，已生成可编辑工作文档。")

    def new_task(self):
        self._require_document()
        if not self._commit_pending():
            return
        self.task_list.selection_remove(self.task_list.selection())
        self.load_task(None)
        self.tabs.select(self.task_tab)
        self.task_title.set("")
        self.task_body.focus_set()
        self.status.set("新增任务编辑器已打开，填写标题和正文后点击「保存任务」。")

    def save_task(self):
        self._require_document()
        title = safe_title(self.task_title.get())
        if self.dirty_doc:
            raise ValueError("请先保存「整理后的工作文档」，再保存任务，避免覆盖全文修改。")
        content = self.task_body.get("1.0", "end-1c")
        old = self.store.document(self.doc)["content"]
        updated, new_id = replace_task(old, self.task_id, title, content, self.done.get())
        self.store.update_content(self.doc, updated)
        self.dirty_task = False
        self.refresh_tasks(select=new_id)
        self._loading = True
        self.doc_body.delete("1.0", "end")
        self.doc_body.insert("1.0", updated)
        self.doc_body.edit_modified(False)
        self._loading = False
        self.dirty_doc = False
        self.status.set("任务已保存，并同步到整理后的工作文档。")

    def save_document(self):
        self._require_document()
        if self.dirty_task:
            raise ValueError("任务编辑尚未保存。请先保存任务，以免被全文改动覆盖。")
        content = self.doc_body.get("1.0", "end-1c")
        self.store.update_content(self.doc, content)
        self.dirty_doc = False
        self.refresh_tasks()
        self.load_task(None)
        self.status.set("工作文档已保存，任务列表从 ### 标题重新提取。")

    def on_tab_change(self, _=None):
        if self._loading:
            return
        if self.tabs.select() == str(self.task_tab) and self.dirty_doc:
            self.status.set("工作文档有未保存修改；请先保存全文再操作任务。")

    def _tree_drag_begin(self, event):
        selected = self.tree.identify_row(event.y)
        self._drag = (selected, event.y) if selected else None

    def _tree_drag_move(self, event):
        if self._drag and abs(event.y - self._drag[1]) > 8:
            self.status.set("松开鼠标调整项目或文档位置。")

    def _tree_drag_end(self, event):
        if not self._drag:
            return
        source, start = self._drag
        self._drag = None
        if abs(event.y - start) < 8 or not self.tree.exists(source):
            return
        target = self.tree.identify_row(event.y)
        if not target or target == source:
            return
        if not self._commit_pending():
            return
        box = self.tree.bbox(target)
        after = bool(box and event.y > box[1] + box[3] / 2)
        if source.startswith("p:") and target.startswith("p:"):
            self.store.reorder_project(source[2:], target[2:], after)
        elif source.startswith("d:"):
            if target.startswith("p:"):
                self.store.move_document(source[2:], target[2:])
            elif target.startswith("d:"):
                self.store.move_document(source[2:],
                                         self.store.document(target[2:])["project_id"],
                                         target[2:], after)
            else:
                return
        else:
            return
        self.refresh_tree(source)
        self.status.set("移动完成并已保存。")

    def _insert_image(self, file):
        self._require_document()
        marker = self.store.add_image(self.doc, Path(file))
        target = self.doc_body if self.tabs.select() == str(self.doc_tab) else self.task_body
        if target is self.task_body and not self.task_id and not self.task_title.get().strip():
            raise ValueError("请先选择任务，或点击「新增任务」填写任务标题。")
        target.insert("insert", "\n" + marker + "\n")
        if target is self.doc_body:
            self.dirty_doc = True
        else:
            self.dirty_task = True
        self.status.set("图片已插入光标位置。保存任务或工作文档后生效。")

    def add_image(self):
        self._require_document()
        path = filedialog.askopenfilename(parent=self, title="选择图片",
                                           filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.gif *.bmp")])
        if path:
            self._insert_image(path)

    def paste_image(self):
        self._require_document()
        data = ImageGrab.grabclipboard()
        if isinstance(data, Image.Image):
            with tempfile.TemporaryDirectory() as folder:
                filename = Path(folder) / "clipboard.png"
                data.save(filename)
                self._insert_image(str(filename))
            return True
        if isinstance(data, list):
            pics = [Path(p) for p in data if isinstance(p, str) and Path(p).suffix.lower() in
                    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}]
            if pics:
                self._insert_image(pics[0])
                return True
        messagebox.showinfo("粘贴图片", "剪贴板中没有图片。", parent=self)
        return False

    def _maybe_paste_clipboard(self, _):
        try:
            data = ImageGrab.grabclipboard()
            if isinstance(data, Image.Image):
                self.paste_image()
                return "break"
        except Exception:
            logging.exception("Clipboard image handling failed")
        return None

    def view_originals(self):
        self._require_document()
        rows = self.store.sources(self.doc)
        if not rows:
            messagebox.showinfo("原始资料", "当前文档没有归档原件。", parent=self)
            return
        win = tk.Toplevel(self)
        win.title("原始资料（只读）")
        win.geometry("900x640")
        left = ttk.Frame(win, padding=8)
        left.pack(side="left", fill="y")
        viewer = tk.Text(win, wrap="word", state="disabled", font=("Microsoft YaHei UI", 10))
        viewer.pack(side="right", fill="both", expand=True, padx=8, pady=8)
        source_list = tk.Listbox(left, width=32)
        source_list.pack(fill="y", expand=True)
        for row in rows:
            source_list.insert("end", row["filename"])
        def show(_=None):
            index = source_list.curselection()
            if not index:
                return
            text = self.store.original_text(rows[index[0]]["id"])
            viewer.configure(state="normal")
            viewer.delete("1.0", "end")
            viewer.insert("1.0", text)
            viewer.configure(state="disabled")
        source_list.bind("<<ListboxSelect>>", self.action(show))
        source_list.selection_set(0)
        show()

    def preview(self):
        self._require_document()
        if self.dirty_task or self.dirty_doc:
            if not self._commit_pending():
                return
        win = tk.Toplevel(self)
        win.title("整理后文档预览 · " + self.store.document(self.doc)["name"])
        win.geometry("880x720")
        viewer = tk.Text(win, wrap="word", state="normal", padx=16, pady=12,
                         font=("Microsoft YaHei UI", 11))
        viewer.pack(fill="both", expand=True)
        text = self.store.document(self.doc)["content"]
        images = []
        base = self.store.workspace_dir(self.doc)
        cursor = 0
        for m in IMAGE_LINK.finditer(text):
            viewer.insert("end", text[cursor:m.start()])
            path = (base / m.group(2)).resolve()
            if path.is_relative_to(base) and path.is_file():
                try:
                    with Image.open(path) as original:
                        copy = original.copy()
                    copy.thumbnail((660, 420))
                    picture = ImageTk.PhotoImage(copy)
                    viewer.image_create("end", image=picture)
                    images.append(picture)
                except Exception:
                    viewer.insert("end", "[图片无法读取]")
            else:
                viewer.insert("end", "[图片路径未找到]")
            cursor = m.end()
        viewer.insert("end", text[cursor:])
        win._images = images
        viewer.configure(state="disabled")

    def export_zip(self):
        self._require_document()
        if not self._commit_pending():
            return
        import zipfile
        name = self.store.document(self.doc)["name"]
        destination = filedialog.asksaveasfilename(parent=self, defaultextension=".zip",
             initialfile=name + "-整理后.zip", filetypes=[("ZIP 资料包", "*.zip")])
        if not destination:
            return
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("工作文档.md", self.store.document(self.doc)["content"].encode("utf-8"))
            asset_root = self.store.assets_dir(self.doc)
            if asset_root.exists():
                for file in asset_root.iterdir():
                    if file.is_file():
                        archive.write(file, "assets/" + file.name)
        self.status.set("整理后的 Markdown 及图片已导出，不包含原始归档文件。")

    def export_word(self):
        self._require_document()
        if not self._commit_pending():
            return
        from docx import Document
        from docx.shared import Inches
        target = filedialog.asksaveasfilename(parent=self, defaultextension=".docx",
                     initialfile=self.store.document(self.doc)["name"] + ".docx",
                     filetypes=[("Word", "*.docx")])
        if not target:
            return
        word = Document()
        base = self.store.workspace_dir(self.doc)
        for line in self.store.document(self.doc)["content"].splitlines():
            if line.startswith("<!-- reqdoc-task:"):
                continue
            if line.startswith("### "):
                word.add_heading(line[4:], level=2)
                continue
            match = IMAGE_LINK.fullmatch(line.strip())
            if match:
                img = (base / match.group(2)).resolve()
                if img.is_relative_to(base) and img.is_file():
                    word.add_picture(str(img), width=Inches(5.5))
                else:
                    word.add_paragraph("[图片未找到]")
                continue
            word.add_paragraph(line)
        word.save(target)
        self.status.set("整理后的工作文档已导出 Word。")

    def backup(self):
        destination = filedialog.asksaveasfilename(parent=self, defaultextension=".zip",
                     initialfile="ReqDocStudio-V8-备份.zip", filetypes=[("ZIP", "*.zip")])
        if not destination:
            return
        if not self._commit_pending():
            return
        self.store.backup(Path(destination))
        self.status.set("数据备份已保存，不覆盖程序文件。")

    def check_update(self):
        self.status.set("正在检查 GitHub 新版本……")
        def worker():
            try:
                result = update_client.fetch_manifest()
                self._update_queue.put(("found", result))
            except Exception as e:
                self._update_queue.put(("error", str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def poll_updates(self):
        try:
            while True:
                kind, value = self._update_queue.get_nowait()
                if kind == "error":
                    self.status.set("更新检查失败：" + value)
                    messagebox.showerror("检查更新", value, parent=self)
                elif kind == "found":
                    if update_client.version_key(value["version"]) <= update_client.version_key(VERSION):
                        self.status.set("当前 V" + VERSION + " 已是最新版本。")
                    elif messagebox.askyesno("发现更新",
                          f"发现 V{value['version']}，是否下载并安装？\n{value['notes']}",
                          parent=self):
                        self.status.set("正在下载并验证更新……")
                        def download():
                            try:
                                self._update_queue.put(("ready", update_client.download_package(value)))
                            except Exception as e:
                                self._update_queue.put(("error", str(e)))
                        threading.Thread(target=download, daemon=True).start()
                elif kind == "ready":
                    if not self._commit_pending():
                        self.status.set("已取消安装，更新包暂存于临时目录。")
                        continue
                    update_client.install_and_exit(value, self.store.root)
                    self.destroy()
        except queue.Empty:
            pass
        finally:
            if self.winfo_exists():
                self.after(200, self.poll_updates)

    def close(self):
        if not self._commit_pending():
            return
        self.destroy()


def main():
    home = default_home()
    (home / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(home / "logs" / "app.log"),
                        level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        app = App(Store(home))
        app.mainloop()
    except Exception:
        logging.exception("ReqDoc V8 fatal error")
        raise


if __name__ == "__main__":
    main()
