"""Local library navigation, portable migration and history deletion."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
import uuid


def migrate_portable_library(store, target):
    from reqdoc.config import Settings
    from reqdoc_studio import write_atomic
    target = Path(target).resolve()
    source = store.settings.library_root.resolve()
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError("软件资料库位置与旧资料库重叠，请移动软件文件夹后重试。")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("软件目录的总文档库不是空目录，请先核对已有内容。")
    with tempfile.TemporaryDirectory(prefix=".reqdoc-portable-", dir=target.parent) as temporary:
        stage = Path(temporary) / "library"
        stage.mkdir()
        store.clone_library_data(stage)
        if target.exists():
            target.rmdir()
        os.replace(stage, target)
    if store.settings.library_config is not None:
        try:
            write_atomic(store.settings.library_config, json.dumps({"path": str(target)}, ensure_ascii=False).encode("utf-8"))
        except OSError:
            logging.exception("Portable data installed; old location pointer update deferred")
    return Settings(home=target / "home", docs_dir=target / "docs", portable=True)


def _archive_path(store, key, source_id, stored):
    target = (store.settings.docs_dir / stored).resolve()
    expected = (store.service.document_folder(key) / "_sources" / source_id).resolve()
    if target.parent != expected or not target.is_relative_to(store.settings.docs_dir.resolve()):
        raise ValueError("历史记录的归档路径无效。")
    return target


def recover_history_deletions(store):
    root = store.settings.home / "history-deletions"
    if not root.exists():
        return
    if root.is_symlink() or not root.resolve().is_relative_to(store.settings.home.resolve()):
        raise ValueError("历史记录清理目录无效。")
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir() or not folder.name.startswith("delete-"):
            continue
        journal = folder / "manifest.json"
        staged = folder / "original"
        if not journal.exists():
            folder.rmdir()
            continue
        data = json.loads(journal.read_text("utf-8"))
        with store.conn() as conn:
            row = conn.execute("SELECT stored FROM studio_sources WHERE doc=? AND id=?", (data["doc"], data["id"])).fetchone()
        if row and staged.exists():
            target = _archive_path(store, data["doc"], data["id"], row[0])
            if target.exists():
                raise ValueError("历史记录恢复遇到同名文件，请保留清理目录以便核对。")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
        elif staged.exists():
            staged.unlink()
        journal.unlink()
        folder.rmdir()


def delete_history_document(store, key, source_id):
    from reqdoc_studio import write_atomic
    from reqdoc_working_document import delete_editable_copy
    root = store.settings.home / "history-deletions"
    root.mkdir(exist_ok=True)
    if root.is_symlink() or not root.resolve().is_relative_to(store.settings.home.resolve()):
        raise ValueError("历史记录清理目录无效。")
    folder = root / ("delete-" + uuid.uuid4().hex)
    try:
        with store.conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT stored FROM studio_sources WHERE doc=? AND id=?", (key, source_id)).fetchone()
            if row is None:
                return False
            target = _archive_path(store, key, source_id, row[0])
            folder.mkdir()
            write_atomic(folder / "manifest.json", json.dumps({"doc": key, "id": source_id}).encode("utf-8"))
            if target.exists():
                os.replace(target, folder / "original")
            delete_editable_copy(store, key, source_id, connection=conn)
            conn.execute("DELETE FROM studio_source_revisions WHERE doc=? AND source_id=?", (key, source_id))
            conn.execute("DELETE FROM studio_sources WHERE doc=? AND id=?", (key, source_id))
    except BaseException:
        try:
            recover_history_deletions(store)
        except Exception:
            logging.exception("History deletion recovery deferred")
        raise
    try:
        recover_history_deletions(store)
    except OSError:
        logging.exception("History file cleanup will retry at startup")
    return True


def refresh_library(self, selected=None):
    if not hasattr(self, "library_tree"):
        return
    tree = self.library_tree
    previous = tree.selection()
    selected = selected or (previous[0] if previous else "library-root")
    opened = {iid for iid in self.library_items if tree.exists(iid) and tree.item(iid, "open")}
    tree.delete(*tree.get_children())
    tree.insert("", "end", iid="library-root", text="总文档库", open=True, values=("本地资料库", ""))
    self.library_items = {"library-root": ("root", "", "", "")}
    for project in self.s.project_groups():
        pid = "library-project-" + str(project.id)
        tree.insert("library-root", "end", iid=pid, text=project.name, open=not opened or pid in opened, values=("项目", ""))
        self.library_items[pid] = ("project", project.name, "", "")
        for doc in self.s.documents_for(project.name):
            iid = "library-doc-" + doc.key
            tree.insert(pid, "end", iid=iid, text=doc.name, values=("工作文档", ""))
            self.library_items[iid] = ("document", project.name, doc.key, "")
            with self.store.conn() as conn:
                histories = conn.execute("SELECT id,name FROM studio_sources WHERE doc=? ORDER BY created,id", (doc.key,)).fetchall()
                results = conn.execute("SELECT id,name FROM studio_import_copies WHERE doc=? AND id GLOB 'organized-*' ORDER BY name,id", (doc.key,)).fetchall()
            for kind, rows in (("history", histories), ("organized", results)):
                for copy_id, name in rows:
                    iid = "library-copy-" + doc.key + "-" + copy_id
                    tree.insert(pid, "end", iid=iid, text=name, values=("历史记录文档" if kind == "history" else "整理文档", doc.name))
                    self.library_items[iid] = (kind, project.name, doc.key, copy_id)
    if tree.exists(selected):
        tree.selection_set(selected)
        tree.focus(selected)
        tree.see(selected)


def _selected(self):
    selection = self.library_tree.selection()
    if not selection:
        raise ValueError("请在总文档库中选择项目或文档。")
    return self.library_items[selection[0]]


def open_library_item(self):
    kind, project, key, copy_id = _selected(self)
    if kind in ("root", "project"):
        self.library_tree.item(self.library_tree.selection()[0], open=True)
        return
    self.load_doc(key)
    self.keep_draft()
    if copy_id:
        with self.store.conn() as conn:
            removed = conn.execute("SELECT 1 FROM studio_deleted_copies WHERE doc=? AND id=?", (key, copy_id)).fetchone()
            row = conn.execute("SELECT name,content FROM studio_sources WHERE doc=? AND id=?", (key, copy_id)).fetchone()
        if removed and row:
            from reqdoc.gui.widgets import MarkdownView
            win = tk.Toplevel(self)
            win.title("历史记录 · " + row[0])
            self.sized_dialog(win, 900, 680)
            view = MarkdownView(win, self.theme)
            view.pack(fill="both", expand=True)
            view.set_base_dir(self.s.document_folder(key))
            view.set_markdown(row[1])
            return
    self._working_copy_id = copy_id or None
    self.load_working_document()
    self.show_page("tasks")


def delete_library_item(self):
    kind, project, key, copy_id = _selected(self)
    if kind == "root":
        raise ValueError("请选择要删除的项目或文档。")
    if kind in ("project", "document"):
        if kind == "project":
            record = next(item for item in self.s.project_groups() if item.name == project)
            iid = "project-" + str(record.id)
        else:
            iid = "d:" + key
        self.project_list.selection_set(iid)
        self.delete_project_or_document()
    else:
        label = self.library_tree.item(self.library_tree.selection()[0], "text")
        if not messagebox.askyesno("删除文档", f"删除「{label}」及其编辑内容？\n删除后无法撤销。", parent=self, icon="warning"):
            return
        if kind == "history":
            delete_history_document(self.store, key, copy_id)
        else:
            from reqdoc_working_document import delete_editable_copy
            delete_editable_copy(self.store, key, copy_id)
        if self.key == key:
            if self._working_copy_id == copy_id:
                self._working_copy_id = None
                self._working_dirty = False
            self.load_working_document()
        self.status.set("已删除文档「" + label + "」。")
    refresh_library(self)


def build_library_page(self):
    page = self.pages["library"]
    self.page_title(page, "本地资料库", "总文档库 → 项目 → 文档。导入资料归为历史记录文档，可在使用完成后删除。")
    ttk.Label(page, text=str(self.store.settings.library_root), foreground="#64748B", wraplength=900).pack(anchor="w", pady=(0, 8))
    actions = ttk.Frame(page)
    actions.pack(fill="x", pady=(0, 8))

    def new_project():
        name = simpledialog.askstring("新建项目", "项目名称", parent=self)
        if name and name.strip():
            project = self.s.create_project_group(name.strip())
            self.current_project_name = project.name
            self.refresh_tree()
            refresh_library(self, "library-project-" + str(project.id))

    def new_document():
        _, project, _, _ = _selected(self)
        if not project:
            raise ValueError("请先选择项目，再新建文档。")
        name = simpledialog.askstring("新建文档", "文档名称", parent=self)
        if name and name.strip():
            doc = self.s.create_document(project, name.strip())
            self.refresh_tree()
            refresh_library(self, "library-doc-" + doc.key)

    def rename():
        kind, project, key, _ = _selected(self)
        if kind not in ("project", "document"):
            raise ValueError("请选择项目或工作文档进行重命名。")
        iid = "d:" + key if kind == "document" else "project-" + str(next(p.id for p in self.s.project_groups() if p.name == project))
        self.project_list.selection_set(iid)
        self.rename_project_or_document()

    for label, action in [("新建项目", new_project), ("新建文档", new_document), ("打开文档", lambda: open_library_item(self)),
                          ("重命名", rename), ("删除选中文档", lambda: delete_library_item(self)),
                          ("打开资料库文件夹", lambda: self.open_path(self.store.settings.library_root))]:
        ttk.Button(actions, text=label, command=lambda action=action: self.guard(action)).pack(side="left", padx=(0, 6))
    area = ttk.Frame(page)
    area.pack(fill="both", expand=True)
    self.library_tree = ttk.Treeview(area, columns=("type", "owner"), show="tree headings", selectmode="browse")
    for col, label, width in [("#0", "总文档库 / 项目 / 文档", 500), ("type", "文档类型", 150), ("owner", "所属工作文档", 190)]:
        self.library_tree.heading(col, text=label)
        self.library_tree.column(col, width=width)
    scroll = ttk.Scrollbar(area, command=self.library_tree.yview)
    self.library_tree.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    self.library_tree.pack(fill="both", expand=True)
    self.library_tree.bind("<Double-1>", lambda event: self.guard(lambda: open_library_item(self)))
    self.library_items = {}

    def menu(event):
        row = self.library_tree.identify_row(event.y)
        if row:
            self.library_tree.selection_set(row)
            popup = tk.Menu(self, tearoff=False)
            popup.add_command(label="打开文档", command=lambda: self.guard(lambda: open_library_item(self)))
            popup.add_command(label="删除选中文档", command=lambda: self.guard(lambda: delete_library_item(self)))
            popup.tk_popup(event.x_root, event.y_root)
    self.library_tree.bind("<Button-3>", menu)
    refresh_library(self)


def install_library(StudioClass):
    original_build = StudioClass.build
    original_refresh = StudioClass.refresh_tree
    original_show = StudioClass.show_page
    StudioClass.NAV_ITEMS = [("本地资料库", "library")] + [item for item in StudioClass.NAV_ITEMS if item[1] != "library"]

    def build(self):
        original_build(self)
        self.pages["library"] = ttk.Frame(self.pages_host)
        build_library_page(self)
        self.show_page("library")

    def refresh(self, *args, **kwargs):
        result = original_refresh(self, *args, **kwargs)
        refresh_library(self)
        return result

    def show(self, name):
        original_show(self, name)
        if name == "library":
            refresh_library(self)

    StudioClass.build = build
    StudioClass.refresh_tree = refresh
    StudioClass.show_page = show
