"""Independent editable working-document workspace for ReqDoc Studio.

Original imported files remain immutable in studio_sources/_sources. This module stores
an independent editable Markdown working copy and keeps task records separate.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
from contextlib import ExitStack, nullcontext
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog


TASK_BLOCK_START = "<!-- reqdoc:tasks -->"
TASK_BLOCK_END = "<!-- /reqdoc:tasks -->"
TASK_BLOCK = re.compile(
    rf"^{re.escape(TASK_BLOCK_START)}\n.*?^{re.escape(TASK_BLOCK_END)}(?:\n|\Z)",
    re.MULTILINE | re.DOTALL,
)
IMAGE_LINK = re.compile(r"!\[([^\]\r\n]*)\]\(([^)\r\n]+)\)")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def ensure_working_schema(store):
    with store.conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS studio_working_docs(
                doc TEXT PRIMARY KEY,
                content TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'manual',
                updated TEXT NOT NULL
            )
        """)


def read_working_document(store, key, fallback=""):
    ensure_working_schema(store)
    with store.conn() as conn:
        row = conn.execute(
            "SELECT content FROM studio_working_docs WHERE doc=?", (key,)
        ).fetchone()
        if row:
            return row[0]
        conn.execute(
            "INSERT INTO studio_working_docs(doc,content,mode,updated) VALUES(?,?,?,?)",
            (key, fallback or "", "legacy-derived" if fallback else "manual", _now()),
        )
    return fallback or ""


def write_working_document(store, key, content, mode="manual", *, connection=None):
    if not isinstance(content, str):
        raise ValueError("工作文档内容必须是文本。")
    if len(content.encode("utf-8")) > 20 * 1024 * 1024:
        raise ValueError("工作文档不能超过 20 MB。")
    if connection is None:
        ensure_working_schema(store)
    with (store.conn() if connection is None else nullcontext(connection)) as conn:
        conn.execute("""
            INSERT INTO studio_working_docs(doc,content,mode,updated)
            VALUES(?,?,?,?)
            ON CONFLICT(doc) DO UPDATE SET
                content=excluded.content,
                mode=excluded.mode,
                updated=excluded.updated
        """, (key, content, mode, _now()))


def delete_working_documents(store, keys):
    ensure_working_schema(store)
    with store.conn() as conn:
        conn.executemany(
            "DELETE FROM studio_working_docs WHERE doc=?",
            [(key,) for key in keys],
        )


def document_to_task_items(content):
    """Convert prose and table rows in document order, excluding the live task block."""
    from reqdoc_studio import _section_parser, split_sections, import_task_title
    from reqdoc_source_table import tables_in, table_to_task_items, task_title_column

    content = TASK_BLOCK.sub("", content).strip()
    if not content:
        return []

    rows = []

    def append_text(section, text):
        text = text.strip()
        if not text:
            return
        items = split_sections(text if section == "导入内容" else f"# {section}\n\n{text}")
        for _, body in items:
            title = section.split(" / ")[-1].strip()
            if section == "导入内容" or len(items) > 1 or re.match(r"^[-*+]\s+|^\d+[.、)）]", body):
                title = import_task_title(section, body)
            rows.append({
                "title": title[:80] or "导入任务",
                "section": section,
                "content": body,
            })

    for section, body in _section_parser(content):
        lines = body.splitlines(keepends=True)
        start = 0
        for table in tables_in(body):
            append_text(section, "".join(lines[start:table.start_line]))
            title_col = task_title_column(table.headers)
            if title_col is None:
                title_col = next(
                    (i for i, header in enumerate(table.headers) if header.casefold() in {"名称", "标题", "title", "name"}),
                    0,
                )
            items = table_to_task_items(table, "", "", title_column=title_col)
            for item in items:
                if item["section"] == "表格导入" and section != "导入内容":
                    item["section"] = section
            rows.extend(items)
            start = table.end_line
        append_text(section, "".join(lines[start:]))
    return rows


def _copy_selection(widget):
    try:
        value = widget.get("sel.first", "sel.last")
    except tk.TclError:
        return
    widget.clipboard_clear()
    widget.clipboard_append(value)


def _text_menu(widget, event, delete_image=None):
    menu = tk.Menu(widget, tearoff=False)
    if delete_image is not None:
        menu.add_command(label="删除图片", command=delete_image)
        menu.add_separator()
    menu.add_command(label="复制", command=lambda: _copy_selection(widget))
    menu.add_command(label="全选", command=lambda: (
        widget.tag_add("sel", "1.0", "end-1c"), widget.focus_set()
    ))
    menu.add_separator()
    menu.add_command(label="粘贴", command=lambda: widget.event_generate("<<Paste>>"))
    menu.tk_popup(event.x_root, event.y_root)
    return "break"


def working_image_menu(self, event):
    text = self.working_text
    index = text.index(f"@{event.x},{event.y}")
    images = text.dump(index, f"{index}+1c", image=True)
    link = self._working_image_links.get(images[0][1]) if images else None

    def delete_image():
        text.delete(index)
        text.delete(*link)

    return _text_menu(text, event, delete_image if link is not None else None)


def _install_text_selection(widget):
    widget.bind("<Button-3>", lambda event, w=widget: _text_menu(w, event))
    widget.bind("<Control-c>", lambda event, w=widget: (_copy_selection(w), "break")[1])
    widget.bind("<Control-C>", lambda event, w=widget: (_copy_selection(w), "break")[1])



def _readonly_text_menu(widget, event):
    menu = tk.Menu(widget, tearoff=False)
    try:
        has_selection = bool(widget.tag_ranges("sel"))
    except tk.TclError:
        has_selection = False
    menu.add_command(
        label="复制选中内容",
        state="normal" if has_selection else "disabled",
        command=lambda: _copy_selection(widget),
    )
    menu.add_command(
        label="全选",
        command=lambda: (widget.tag_add("sel", "1.0", "end-1c"), widget.focus_set()),
    )
    menu.tk_popup(event.x_root, event.y_root)
    return "break"


def task_list_right_click(self, event):
    item = self.list.identify_row(event.y)
    if item:
        if item not in self.list.selection():
            self.list.selection_set(item)
        self.list.focus(item)
        self.on_task_selected()
    return "break"


def refresh_task_detail(self):
    if not hasattr(self, "task_detail"):
        return
    selected = list(self.list.selection()) if hasattr(self, "list") else []
    lines = []
    for item in selected:
        if not str(item).isdigit():
            continue
        try:
            row = self.s.get_requirement(int(item))
        except Exception:
            continue
        lines.extend([
            row.title or "（无标题）",
            f"所属分组：{row.section or '未分类'}    状态：{'已完成' if row.done else '未完成'}",
            "",
            row.content.strip() or "（无任务详情）",
            "─" * 48,
            "",
        ])
    self.task_detail.configure(state="normal")
    self.task_detail.delete("1.0", "end")
    self.task_detail.insert("1.0", "\n".join(lines) if lines else "选择任务后，这里显示可自由框选和复制的任务明细。")
    self.task_detail.configure(state="disabled")


def build_import_page(self):
    page = self.pages["import"]
    self.page_title(
        page, "导入文件",
        "导入后生成独立可编辑工作文档；原始文件只读归档，不提供原件预览或编辑。",
    )
    panel = ttk.LabelFrame(page, text="导入资料", padding=18)
    panel.pack(fill="x", pady=(0, 12))
    ttk.Label(
        panel,
        text="支持 Word（.docx）、Markdown（.md / .markdown）和文本（.txt），可一次导入多份。",
        font=("Microsoft YaHei", 10),
    ).pack(anchor="w", pady=(0, 8))
    ttk.Label(
        panel,
        text="导入后选择：形成可编辑工作文档/表格，或直接从内容生成任务。两种方式都会保留独立工作文档。",
        foreground="#64748B", wraplength=900, justify="left",
    ).pack(anchor="w", pady=(0, 12))
    ttk.Button(
        panel, text="选择文件并导入",
        command=lambda: self.guard(self.import_dialog),
    ).pack(anchor="w")
    self.import_hint = ttk.Label(
        page, text="原始文件会自动只读归档；编辑请在“任务处理”的工作文档区完成。",
        foreground="#64748B",
    )
    self.import_hint.pack(anchor="w", pady=(8, 0))


def refresh_import_page(self):
    return None


def build_home_page(self):
    page = self.pages["home"]
    self.heading = ttk.Label(page, text="选择文档，开始整理", font=("Microsoft YaHei", 18, "bold"))
    self.heading.pack(anchor="w", pady=(2, 2))
    self.breadcrumb = ttk.Label(page, text="先创建项目和文档，或选择已有文档。", foreground="#64748B")
    self.breadcrumb.pack(anchor="w", pady=(0, 18))
    cards = ttk.Frame(page)
    cards.pack(fill="x", pady=(0, 20))
    self.action_card(
        cards, "✎  手动输入",
        "先在工作文档中自由输入和编辑，确认内容后再转换成任务。",
        self.start_manual,
    )
    self.action_card(
        cards, "↓  导入文件",
        "导入 Word、Markdown 或 TXT，选择形成可编辑表格/文档，或形成任务。",
        self.import_dialog,
    )
    ttk.Label(page, text="当前文档概览", font=("Microsoft YaHei", 12, "bold")).pack(anchor="w", pady=(0, 8))
    stats = ttk.Frame(page)
    stats.pack(fill="x", pady=(0, 14))
    self.home_total = tk.StringVar(value="0")
    self.home_done = tk.StringVar(value="0")
    self.home_open = tk.StringVar(value="0")
    self.home_rate = tk.StringVar(value="0%")
    for label, var in [
        ("任务总数", self.home_total), ("已完成", self.home_done),
        ("未完成", self.home_open), ("完成率", self.home_rate),
    ]:
        box = ttk.LabelFrame(stats, text=label, padding=12)
        box.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ttk.Label(box, textvariable=var, font=("Microsoft YaHei", 18, "bold")).pack(anchor="center")
    ttk.Label(page, text="常用操作", font=("Microsoft YaHei", 12, "bold")).pack(anchor="w", pady=(2, 8))
    quick = ttk.Frame(page)
    quick.pack(fill="x")
    ttk.Button(quick, text="打开工作文档与任务", command=lambda: self.show_page("tasks")).pack(side="left", padx=(0, 8))
    ttk.Button(quick, text="查看完成进度", command=lambda: self.show_page("progress")).pack(side="left")
    self.home_hint = ttk.Label(page, text="尚未选择文档。请先新建项目和文档。", foreground="#64748B")
    self.home_hint.pack(anchor="w", pady=(22, 0))


def build_tasks_page(self):
    page = self.pages["tasks"]
    self.tasks_heading = ttk.Label(page, text="工作文档 / 任务处理", font=("Microsoft YaHei", 17, "bold"))
    self.tasks_heading.pack(anchor="w", pady=(0, 3))
    self.tasks_subtitle = ttk.Label(
        page,
        text="左侧是导入后生成的独立可编辑工作文档；右侧是从工作文档生成或手动新增的任务。",
        foreground="#64748B",
    )
    self.tasks_subtitle.pack(anchor="w", pady=(0, 8))

    selectors = ttk.LabelFrame(page, text="当前处理对象", padding=7)
    selectors.pack(fill="x", pady=(0, 7))
    ttk.Label(selectors, text="项目").pack(side="left", padx=(0, 6))
    self.task_project_var = tk.StringVar()
    self.task_project_box = ttk.Combobox(
        selectors, textvariable=self.task_project_var, state="readonly", width=24
    )
    self.task_project_box.pack(side="left", padx=(0, 12))
    self.task_project_box.bind("<<ComboboxSelected>>", self.on_task_project_selected)
    ttk.Label(selectors, text="文档").pack(side="left", padx=(0, 6))
    self.task_document_var = tk.StringVar()
    self.task_document_box = ttk.Combobox(
        selectors, textvariable=self.task_document_var, state="readonly"
    )
    self.task_document_box.pack(side="left", fill="x", expand=True)
    self.task_document_box.bind("<<ComboboxSelected>>", self.on_task_document_selected)
    self.task_doc_map = {}
    self._task_selector_loading = False

    self.tasks_split = ttk.Panedwindow(page, orient="horizontal")
    self.tasks_split.pack(fill="both", expand=True)

    left = ttk.LabelFrame(self.tasks_split, text="可编辑工作文档（独立副本）", padding=7)
    self.task_working_panel = left
    self.tasks_split.add(left, weight=3)
    workbar = ttk.Frame(left)
    workbar.pack(fill="x", pady=(0, 6))
    for label, command in [
        ("导入文档", self.import_dialog),
        ("保存文档", self.save_working_document),
        ("转成任务", self.convert_working_document),
        ("编辑表格", self.edit_working_table),
    ]:
        ttk.Button(workbar, text=label, command=lambda fn=command: self.guard(fn)).pack(
            side="left", padx=(0, 5), pady=2
        )
    tools = ttk.Frame(left)
    tools.pack(fill="x", pady=(0, 5))
    ttk.Button(tools, text="粘贴图片", command=lambda: self.guard(self.paste_working_image)).pack(side="left", padx=(0, 5))
    export = ttk.Menubutton(tools, text="导出文档 ▾")
    export_menu = tk.Menu(export, tearoff=False)
    for label, kind in [("导出 Word 文档", "docx"), ("导出 Markdown 与图片包", "zip")]:
        export_menu.add_command(label=label, command=lambda kind=kind: self.guard(lambda: export_working_document(self, kind)))
    export_menu.add_separator()
    export_menu.add_command(label="预览导出内容", command=lambda: self.guard(self.preview_working_document))
    export.configure(menu=export_menu)
    export.pack(side="left")
    self.working_hint = ttk.Label(
        left, text="这里编辑软件生成的工作副本；原始文件只读归档，不在此处预览。",
        foreground="#64748B",
    )
    self.working_hint.pack(anchor="w", pady=(0, 5))
    area = ttk.Frame(left)
    area.pack(fill="both", expand=True)
    self.working_text = tk.Text(
        area, wrap="word", undo=True, font=("Microsoft YaHei", 10),
        padx=12, pady=10, relief="flat", bg="white", fg="#243247",
        insertwidth=2, spacing3=4,
    )
    wy = ttk.Scrollbar(area, orient="vertical", command=self.working_text.yview)
    wx = ttk.Scrollbar(area, orient="horizontal", command=self.working_text.xview)
    self.working_text.configure(yscrollcommand=wy.set, xscrollcommand=wx.set)
    wy.pack(side="right", fill="y")
    wx.pack(side="bottom", fill="x")
    self.working_text.pack(fill="both", expand=True)
    self.working_text.bind("<<Modified>>", self.working_document_modified)
    self.working_text.bind("<<Paste>>", self.working_paste_event)
    _install_text_selection(self.working_text)
    self.working_text.bind("<Button-3>", lambda event: working_image_menu(self, event))
    self.working_text.tag_configure("task_marker", elide=True)
    self.working_text.tag_configure("image_markup", elide=True)
    self._working_images = {}
    self._working_image_links = {}
    self._working_preview_after_id = None
    self.body = self.working_text
    self._working_dirty = False
    self._working_loading = False
    self._working_mode = "manual"

    right = ttk.LabelFrame(self.tasks_split, text="任务列表", padding=7)
    self.task_right_panel = right
    self.tasks_split.add(right, weight=2)
    self.tasks_context = ttk.Label(right, text="尚未选择文档。", foreground="#64748B")
    self.tasks_context.pack(anchor="w", pady=(0, 5))
    self.task_right_split = ttk.Panedwindow(right, orient="vertical")
    self.task_right_split.pack(fill="both", expand=True)
    task_list_panel = ttk.LabelFrame(self.task_right_split, text="任务列表", padding=6)
    task_detail_panel = ttk.LabelFrame(
        self.task_right_split, text="任务明细（可自由框选、右键复制）", padding=6
    )
    self.task_right_split.add(task_list_panel, weight=3)
    self.task_right_split.add(task_detail_panel, weight=2)

    searchbar = ttk.Frame(task_list_panel)
    searchbar.pack(fill="x", pady=(0, 5))
    self.query = tk.StringVar()
    ttk.Entry(searchbar, textvariable=self.query).pack(side="left", fill="x", expand=True, padx=(0, 6))
    self.filter = tk.StringVar(value="全部")
    self.filter_box = ttk.Combobox(
        searchbar, textvariable=self.filter, values=["全部", "未完成", "已完成"],
        width=9, state="readonly",
    )
    self.filter_box.pack(side="left")
    self.filter_box.bind("<<ComboboxSelected>>", lambda event: (self.checked_ids.clear(), self.refresh_tasks()))
    self.query.trace_add("write", lambda *args: (self.checked_ids.clear(), self.refresh_tasks()))

    primary = ttk.Frame(task_list_panel)
    primary.pack(fill="x", pady=(0, 5))
    for label, command in [
        ("＋ 新增任务", self.new_task),
        ("选中后插入", self.insert_after_selected),
        ("编辑任务", self.edit_selected),
    ]:
        ttk.Button(primary, text=label, command=lambda fn=command: self.guard(fn)).pack(
            side="left", padx=(0, 5), pady=2
        )
    footer = ttk.Frame(task_list_panel)
    footer.pack(side="bottom", fill="x")
    list_frame = ttk.Frame(task_list_panel)
    list_frame.pack(fill="both", expand=True)
    self.list = ttk.Treeview(
        list_frame, columns=("check", "state", "title", "chapter"),
        show="headings", selectmode="extended",
    )
    for col, label, width in [
        ("check", "选", 38), ("state", "状态", 66),
        ("title", "任务标题", 190), ("chapter", "所属分组", 130),
    ]:
        self.list.heading(col, text=label)
        self.list.column(
            col, width=width, minwidth=42,
            stretch=col in ("title", "chapter"),
            anchor="center" if col in ("check", "state") else "w",
        )
    sy = ttk.Scrollbar(list_frame, orient="vertical", command=self.list.yview)
    sx = ttk.Scrollbar(list_frame, orient="horizontal", command=self.list.xview)
    self.list.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
    sy.pack(side="right", fill="y")
    sx.pack(side="bottom", fill="x")
    self.list.pack(fill="both", expand=True)
    self.list.bind("<Button-1>", self.task_cell_click)
    self.list.bind("<<TreeviewSelect>>", self.on_task_selected)
    self.list.bind("<Double-1>", lambda event: self.guard(self.edit_selected))
    self.list.bind("<Button-3>", self.task_list_right_click)

    actions = ttk.Frame(footer)
    actions.pack(fill="x", pady=(5, 0))
    for label, command in [
        ("全选/取消", self.select_all_visible),
        ("完成", lambda: self.mark(True)),
        ("未完成", lambda: self.mark(False)),
    ]:
        ttk.Button(actions, text=label, command=lambda fn=command: self.guard(fn)).pack(
            side="left", padx=(0, 4), pady=2
        )
    secondary = ttk.Frame(footer)
    secondary.pack(fill="x", pady=(3, 0))
    ttk.Button(secondary, text="删除任务", command=lambda: self.guard(self.delete_tasks)).pack(side="left", padx=(0, 4))
    more = ttk.Menubutton(secondary, text="更多任务操作 ▾")
    more.pack(side="left")
    more_menu = tk.Menu(more, tearoff=False)
    for label, command in [
        ("清除选择", self.clear_selection),
        ("批量改章节", self.change_chapter),
        ("上移", lambda: self.move(-1)),
        ("下移", lambda: self.move(1)),
        ("拆分任务", self.split_task),
        ("合并任务", self.merge_tasks),
    ]:
        more_menu.add_command(label=label, command=lambda fn=command: self.guard(fn))
    more.configure(menu=more_menu)
    self.count = ttk.Label(footer, text="0 条任务")
    self.count.pack(anchor="e", pady=(3, 0))

    detail_area = ttk.Frame(task_detail_panel)
    detail_area.pack(fill="both", expand=True)
    self.task_detail = tk.Text(
        detail_area, wrap="word", font=("Microsoft YaHei", 9),
        padx=10, pady=8, relief="flat", bg="white", fg="#243247",
        state="disabled", exportselection=False,
    )
    detail_scroll = ttk.Scrollbar(detail_area, orient="vertical", command=self.task_detail.yview)
    self.task_detail.configure(yscrollcommand=detail_scroll.set)
    detail_scroll.pack(side="right", fill="y")
    self.task_detail.pack(fill="both", expand=True)
    self.task_detail.bind("<Button-3>", lambda event: _readonly_text_menu(self.task_detail, event))
    self.task_detail.bind("<Control-c>", lambda event: (_copy_selection(self.task_detail), "break")[1])
    self.task_detail.bind("<Control-C>", lambda event: (_copy_selection(self.task_detail), "break")[1])

    self.editor_open = False
    self.source_panel_visible = False
    self.rid = None
    self._task_focus_id = None
    self._working_saved_key = None
    self._task_sashes_initialized = False
    self.task_right_split.bind("<Configure>", lambda event: self.position_task_sashes())


def position_task_sashes(self):
    if self._task_sashes_initialized:
        return
    width = self.tasks_split.winfo_width()
    height = self.task_right_split.winfo_height()
    if width > 1 and height > 1:
        self.tasks_split.sashpos(0, min(int(width * 0.60), width - 310))
        self.task_right_split.sashpos(0, max(230, int(height * 0.60)))
        self._task_sashes_initialized = True


def build_progress_page(self):
    page = self.pages["progress"]
    self.page_title(page, "进度统计", "左侧竖向选择项目/文档分组；右侧任务明细支持鼠标框选和右键复制。")
    filters = ttk.LabelFrame(page, text="统计范围", padding=8)
    filters.pack(fill="x", pady=(0, 8))
    ttk.Label(filters, text="项目").pack(side="left", padx=(0, 6))
    self.progress_scope_var = tk.StringVar(value="全部项目")
    self.progress_scope_box = ttk.Combobox(
        filters, textvariable=self.progress_scope_var,
        values=["全部项目"], state="readonly", width=30,
    )
    self.progress_scope_box.pack(side="left")
    self.progress_scope_box.bind("<<ComboboxSelected>>", lambda event: self.refresh_progress(reset_group=True))
    ttk.Button(filters, text="刷新统计", command=lambda: self.refresh_progress(reset_group=True)).pack(side="right")

    cards = ttk.Frame(page)
    cards.pack(fill="x", pady=(0, 8))
    self.progress_total = tk.StringVar(value="0")
    self.progress_done = tk.StringVar(value="0")
    self.progress_open = tk.StringVar(value="0")
    self.progress_rate = tk.StringVar(value="0%")
    for label, var in [
        ("任务总数", self.progress_total), ("已完成", self.progress_done),
        ("未完成", self.progress_open), ("完成率", self.progress_rate),
    ]:
        box = ttk.LabelFrame(cards, text=label, padding=7)
        box.pack(side="left", fill="both", expand=True, padx=(0, 7))
        ttk.Label(box, textvariable=var, font=("Microsoft YaHei", 15, "bold")).pack(anchor="center")
    self.progress_bar = ttk.Progressbar(page, orient="horizontal", mode="determinate", maximum=100)
    self.progress_bar.pack(fill="x", pady=(0, 8))

    split = ttk.Panedwindow(page, orient="horizontal")
    split.pack(fill="both", expand=True)
    self.progress_split = split
    group_box = ttk.LabelFrame(split, text="项目 / 文档分组", padding=6)
    split.add(group_box, weight=1)
    self.progress_chapters = ttk.Treeview(
        group_box, columns=("count",), show="tree headings", selectmode="browse", height=16
    )
    self.progress_chapters.heading("#0", text="分组")
    self.progress_chapters.column("#0", width=190, minwidth=125, stretch=True)
    self.progress_chapters.heading("count", text="任务")
    self.progress_chapters.column("count", width=55, minwidth=45, stretch=False, anchor="center")
    gy = ttk.Scrollbar(group_box, orient="vertical", command=self.progress_chapters.yview)
    self.progress_chapters.configure(yscrollcommand=gy.set)
    gy.pack(side="right", fill="y")
    self.progress_chapters.pack(fill="both", expand=True)
    self.progress_chapters.bind("<<TreeviewSelect>>", lambda event: self.refresh_progress_tasks())

    detail_box = ttk.LabelFrame(split, text="任务明细（可框选文本，右键复制）", padding=6)
    split.add(detail_box, weight=4)
    self.progress_detail = tk.Text(
        detail_box, wrap="word", font=("Microsoft YaHei", 10),
        padx=12, pady=10, relief="flat", bg="white", fg="#243247",
        state="disabled",
    )
    dy = ttk.Scrollbar(detail_box, orient="vertical", command=self.progress_detail.yview)
    self.progress_detail.configure(yscrollcommand=dy.set)
    dy.pack(side="right", fill="y")
    self.progress_detail.pack(fill="both", expand=True)
    self.progress_hint = ttk.Label(page, text="选择左侧分组查看任务明细。", foreground="#64748B")
    self.progress_hint.pack(anchor="w", pady=(5, 0))
    _install_text_selection(self.progress_detail)
    self.progress_detail.bind("<Button-3>", lambda event: _readonly_text_menu(self.progress_detail, event))
    self.progress_tasks = self.progress_detail
    self.progress_group_map = {}
    self.progress_task_doc_map = {}
    self.progress_chapter_map = {}
    self.progress_detail_context = None
    self._progress_sashes_initialized = False
    split.bind("<Configure>", lambda event: self.position_progress_sashes())


def position_progress_sashes(self):
    width = self.progress_split.winfo_width()
    if not self._progress_sashes_initialized and width > 1:
        self.progress_split.sashpos(0, max(230, int(width * 0.25)))
        self._progress_sashes_initialized = True


def build_export_page(self):
    page = self.pages["export"]
    self.page_title(page, "任务导出", "导出已整理的任务内容；独立工作文档可在任务处理页继续编辑。")
    box = ttk.LabelFrame(page, text="导出当前文档", padding=18)
    box.pack(fill="x")
    self.export_doc_label = ttk.Label(box, text="请先选择一份文档。", foreground="#64748B")
    self.export_doc_label.pack(anchor="w", pady=(0, 10))
    ttk.Button(box, text="导出 Word 文档", command=lambda: self.guard(lambda: self.export("docx"))).pack(anchor="w", pady=5)
    ttk.Button(box, text="导出 Markdown 与图片包", command=lambda: self.guard(lambda: self.export("zip"))).pack(anchor="w", pady=5)
    ttk.Label(
        page, text="工作文档保存在本地数据库中；原始导入文件独立归档，不会被编辑覆盖。",
        foreground="#64748B",
    ).pack(anchor="w", pady=(12, 0))


def working_document_modified(self, event=None):
    if self.working_text.edit_modified():
        if not self._working_loading:
            self._working_dirty = True
        self.working_text.edit_modified(False)
        if self._working_preview_after_id is None:
            self._working_preview_after_id = self.after_idle(lambda: refresh_working_images(self))


def refresh_working_images(self):
    from PIL import Image, ImageTk, ImageOps

    self._working_preview_after_id = None
    text = self.working_text
    content = text.get("1.0", "end-1c")
    previews = {}
    links = {}
    base = self.s.document_folder(self.key).resolve() if self.key else None
    existing = {name: index for kind, name, index in text.dump("1.0", "end", image=True)}
    for index in reversed(list(existing.values())):
        text.delete(index)
    text.tag_remove("task_marker", "1.0", "end")
    text.tag_remove("image_markup", "1.0", "end")
    for line, value in enumerate(content.split("\n"), 1):
        if value in (TASK_BLOCK_START, TASK_BLOCK_END):
            text.tag_add("task_marker", f"{line}.0", f"{line + 1}.0")
        if base is None:
            continue
        for match in IMAGE_LINK.finditer(value):
            try:
                path = (base / match[2].strip().strip("<>")).resolve()
                if not path.is_relative_to(base) or not path.is_file():
                    continue
                if path not in previews:
                    photo = self._working_images.get(path)
                    if photo is None:
                        with Image.open(path) as opened:
                            opened.thumbnail((300, 210))
                            photo = ImageTk.PhotoImage(ImageOps.exif_transpose(opened), master=text)
                    previews[path] = photo
                positions = [match.start(), match.start(1), match.end(1), match.end()]
                first, alt_start, alt_end, last = (
                    f"{line}.{text.tk.call('string', 'length', value[:offset])}"
                    for offset in positions
                )
                text.tag_add("image_markup", first, alt_start)
                text.tag_add("image_markup", alt_end, last)
                name = text.image_create(f"{line}.end", image=previews[path], padx=5, pady=5)
                links[name] = (first, last)
            except (OSError, ValueError, Image.DecompressionBombError):
                logging.warning("Could not preview working-document image", exc_info=True)
    self._working_images = previews
    self._working_image_links = links


def set_working_text(self, content):
    self._working_loading = True
    self.working_text.replace("1.0", "end-1c", content)
    self.working_text.edit_modified(False)
    self.working_text.edit_reset()
    self._working_loading = False
    self._working_dirty = False
    if self._working_preview_after_id is not None:
        self.after_cancel(self._working_preview_after_id)
    refresh_working_images(self)


def sync_working_tasks(self, rows):
    if not self.key or self._working_saved_key != self.key:
        return
    widget = self.working_text
    content = widget.get("1.0", "end-1c")
    match = TASK_BLOCK.search(content)
    if not rows and not match:
        return
    task_text = self.store.compose(self.key, rows=rows).partition("\n")[2].rstrip()
    block = f"{TASK_BLOCK_START}\n## 任务清单（自动同步）\n\n{task_text}\n{TASK_BLOCK_END}\n"
    if match:
        if match[0] == block:
            return
        first_line = content.count("\n", 0, match.start()) + 1
        after_line = first_line + match[0].count("\n")
        end = f"{after_line}.0" if match[0].endswith("\n") else "end-1c"
        widget.replace(f"{first_line}.0", end, block)
    else:
        separator = "\n\n" if content and not content.endswith("\n\n") else ""
        widget.insert("end-1c", separator + block)
    self._working_dirty = True


def load_working_document(self):
    if not self.key:
        self._working_saved_key = None
        set_working_text(self, "")
        return
    tasks = self.store.tasks(self.key)
    fallback = self.store.compose(self.key, rows=tasks) if tasks else ""
    content = read_working_document(self.store, self.key, fallback)
    # Earlier versions stored a whole task summary as the editable fallback.
    # Replace it only when its full text matches a generated summary.
    legacy_summary = False
    if content.startswith("# ") and re.match(r"# [^\n]+\n> 项目：.* · 任务 \d+ · 已完成 \d+", content):
        with self.store.conn() as conn:
            legacy_summary = content == fallback or conn.execute(
                "SELECT 1 FROM studio_versions WHERE doc=? AND content=? LIMIT 1", (self.key, content)
            ).fetchone() is not None
    set_working_text(self, "" if legacy_summary else content)
    self._working_saved_key = self.key
    with self.store.conn() as conn:
        self._working_mode = conn.execute(
            "SELECT mode FROM studio_working_docs WHERE doc=?", (self.key,)
        ).fetchone()[0]
    sync_working_tasks(self, tasks)
    self._working_dirty |= legacy_summary
    self.working_hint.configure(text="正文可直接编辑，任务清单请在右侧修改。")
    self.working_text.focus_set()


def save_working_document(self, silent=False):
    if not self.key:
        if silent:
            return False
        raise ValueError("请先选择一份文档。")
    content = TASK_BLOCK.sub("", self.working_text.get("1.0", "end-1c"))
    write_working_document(self.store, self.key, content, getattr(self, "_working_mode", "manual"))
    self._working_dirty = False
    self._working_saved_key = self.key
    if not silent and hasattr(self, "status"):
        self.status.set("工作文档已保存；原始资料未被修改。")
    return True


def keep_draft(self):
    if self.key and self._working_saved_key == self.key and (self._working_dirty or self.working_text.edit_modified()):
        return self.save_working_document(silent=True)
    return False


def autosave(self):
    if getattr(self, "_closing", False):
        return
    try:
        self.keep_draft()
        if hasattr(self, "persist_notes"):
            self.persist_notes()
    except Exception:
        logging.exception("Working document autosave failed")
    try:
        self._autosave_after_id = self.after(2000, self.autosave)
    except tk.TclError:
        return


def load_doc(self, key):
    self.keep_draft()
    if key == self.key:
        self.refresh_task_selectors(key)
        self.load_working_document()
        self.refresh_tasks()
        return
    self.key = key
    self.rid = None
    self._task_focus_id = None
    self.checked_ids.clear()
    self.insert_after_id = None
    self.filter.set("全部")
    self.query.set("")
    doc = self.s.document(key)
    self.current_project_name = doc.project_name
    self.heading.configure(text=doc.name)
    self.breadcrumb.configure(text=f"项目：{doc.project_name} / 文档：{doc.name}")
    self.tasks_heading.configure(text=f"工作文档 / 任务处理  /  {doc.name}")
    self.refresh_task_selectors(key)
    self.load_working_document()
    self.refresh_tasks()
    self.refresh_home()
    self.refresh_progress(reset_group=True)
    self.refresh_import_page()
    self.refresh_export_page()
    self.context_label.configure(text=f"当前文档：{doc.project_name} / {doc.name}")
    self.tasks_context.configure(text=f"正在处理：{doc.project_name} / {doc.name}")


def refresh_task_source(self):
    # Deliberately no source preview in the task workspace.
    return None


def on_task_project_selected(self, event=None):
    if getattr(self, "_task_selector_loading", False):
        return
    project = self.task_project_var.get()
    if not project:
        return
    self.keep_draft()
    self.current_project_name = project
    self.checked_ids.clear()
    labels = self.refresh_task_selectors("")
    if labels:
        self.on_task_document_selected()
    else:
        self.clear_task_document("该项目下还没有文档。请先创建文档。")


def on_task_document_selected(self, event=None):
    if getattr(self, "_task_selector_loading", False):
        return
    key = self.task_doc_map.get(self.task_document_var.get())
    if key:
        self.load_doc(key)


def clear_task_document(self, message="尚未选择文档。"):
    self.keep_draft()
    self.key = None
    self.rid = None
    self.checked_ids.clear()
    self.visible_ids = []
    self.list.delete(*self.list.get_children())
    self._working_saved_key = None
    set_working_text(self, "")
    self.tasks_context.configure(text=message)
    self.working_hint.configure(text=message)
    self.refresh_task_detail()
    self.update_count()


def refresh_tasks(self):
    if not hasattr(self, "list"):
        return
    keep = list(self.list.selection())
    self._refreshing_tasks = True
    self.list.delete(*self.list.get_children())
    self.visible_ids = []
    if self.key:
        active_rows = self.store.tasks(self.key)
        query = self.query.get().casefold()
        for row in active_rows:
            haystack = (row.title + " " + row.content + " " + row.section).casefold()
            if query and query not in haystack:
                continue
            if self.filter.get() == "未完成" and row.done:
                continue
            if self.filter.get() == "已完成" and not row.done:
                continue
            self.visible_ids.append(row.id)
            self.list.insert(
                "", "end", iid=str(row.id),
                values=(
                    "☑" if row.id in self.checked_ids else "☐",
                    "已完成" if row.done else "未完成",
                    row.title, row.section or "未分类",
                ),
            )
    keep = [item for item in keep if self.list.exists(item)]
    if keep:
        self.list.selection_set(keep)
    self._refreshing_tasks = False
    if self.key:
        sync_working_tasks(self, active_rows)
        self.tasks_context.configure(
            text=f"正在处理：{self.s.document(self.key).project_name} / {self.s.document(self.key).name}"
        )
    else:
        self.tasks_context.configure(text="请选择项目和文档。")
    self.update_count()
    self.refresh_task_detail()
    self.refresh_home()
    if hasattr(self, "progress_chapters") and self.pages["progress"].winfo_manager():
        self.refresh_progress()


def update_count(self):
    if not hasattr(self, "count"):
        return
    checked = len(set(self.checked_ids) & set(self.visible_ids))
    selected = len(self.list.selection())
    self.count.configure(
        text=f"{len(self.list.get_children())} 条显示 · 已勾选 {checked} 条 · 当前选择 {selected} 条"
    )


def selected_ids(self):
    if not self.key:
        return []
    allowed = set(self.visible_ids)
    checked = set(self.checked_ids) & allowed
    if checked:
        return sorted(checked)
    return sorted({int(item) for item in self.list.selection() if str(item).isdigit()} & allowed)


def task_cell_click(self, event):
    if not self.key:
        return "break"
    item = self.list.identify_row(event.y)
    column = self.list.identify_column(event.x)
    if not item:
        return
    rid = int(item)
    if column == "#1":
        if rid in self.checked_ids:
            self.checked_ids.remove(rid)
        else:
            self.checked_ids.add(rid)
        values = list(self.list.item(item, "values"))
        values[0] = "☑" if rid in self.checked_ids else "☐"
        self.list.item(item, values=values)
        self.update_count()
        return "break"
    if column == "#2":
        row = self.s.get_requirement(rid)
        self.s.set_done(rid, not bool(row.done))
        self.store.snapshot(self.key)
        self.refresh_tasks()
        self.refresh_progress()
        return "break"
    self._task_focus_id = rid


def on_task_selected(self, event=None):
    if getattr(self, "_refreshing_tasks", False):
        return
    selected = self.list.selection()
    if len(selected) == 1:
        self.rid = int(selected[0])
        self._task_focus_id = self.rid
    self.update_count()
    self.refresh_task_detail()


def load_editor(self, rid):
    # Task editing uses a dedicated dialog; the main editor remains the working document.
    self.rid = rid


def show_editor(self):
    return None


def hide_editor(self):
    return None


def save_task(self):
    return self.save_working_document()


def save_current(self):
    self.guard(self.save_working_document)
    return "break"


def start_manual(self):
    self.require_doc()
    self.show_page("tasks")
    self.working_text.focus_set()
    self.status.set("手动输入模式：直接编辑左侧工作文档，完成后点击“转成任务”。")


def _task_dialog(self, rid=None, after_id=None):
    self.require_doc()
    existing = self.s.get_requirement(rid) if rid is not None else None
    win = tk.Toplevel(self)
    win.title("编辑任务" if existing else "插入任务" if after_id is not None else "新增任务")
    self.sized_dialog(win, 760, 620)
    win.transient(self)
    win.grab_set()
    root = ttk.Frame(win, padding=12)
    root.pack(fill="both", expand=True)
    footer = ttk.Frame(root)
    footer.pack(side="bottom", fill="x", pady=(9, 0))
    ttk.Label(root, text="任务标题").pack(anchor="w")
    title_var = tk.StringVar(value=existing.title if existing else "")
    ttk.Entry(root, textvariable=title_var).pack(fill="x", pady=(2, 7))
    ttk.Label(root, text="所属章节 / 分组").pack(anchor="w")
    chapter_var = tk.StringVar(value=(existing.section or "未分类") if existing else "未分类")
    chapter = ttk.Combobox(root, textvariable=chapter_var)
    chapter.pack(fill="x", pady=(2, 7))
    chapter.configure(values=sorted({r.section or "未分类" for r in self.store.tasks(self.key)}))
    done_var = tk.BooleanVar(value=bool(existing.done) if existing else False)
    ttk.Checkbutton(root, text="此任务已完成", variable=done_var).pack(anchor="w", pady=(0, 6))
    ttk.Label(root, text="任务详情").pack(anchor="w")
    area = ttk.Frame(root)
    area.pack(fill="both", expand=True)
    body = tk.Text(area, height=6, wrap="word", undo=True, font=("Microsoft YaHei", 10), padx=10, pady=8)
    sy = ttk.Scrollbar(area, orient="vertical", command=body.yview)
    body.configure(yscrollcommand=sy.set)
    sy.pack(side="right", fill="y")
    body.pack(fill="both", expand=True)
    if existing:
        body.insert("1.0", existing.content)
    _install_text_selection(body)

    def save():
        title = title_var.get().strip()
        section = chapter_var.get().strip() or "未分类"
        content = body.get("1.0", "end-1c")
        if not title and not content.strip():
            raise ValueError("请至少填写任务标题或详情。")
        title = title or content.strip().splitlines()[0][:80]
        if existing:
            row = self.store.save_task(self.key, title, section, content, rid)
        else:
            row = self.store.insert_task(
                self.key, title, section, content, after_id=after_id, chapter_end=True
            )
        self.s.set_done(row.id, bool(done_var.get()))
        self.store.snapshot(self.key)
        self.checked_ids.clear()
        self.refresh_tasks()
        if self.list.exists(str(row.id)):
            self.list.selection_set(str(row.id))
            self.list.see(str(row.id))
        self.rid = row.id
        self._task_focus_id = row.id
        self.refresh_progress(reset_group=False)
        self.refresh_home()
        self.refresh_export_page()
        self.status.set("任务已保存。")
        win.destroy()

    ttk.Button(footer, text="确定", command=lambda: self.guard(save)).pack(side="right", padx=(6, 0))
    ttk.Button(footer, text="取消", command=win.destroy).pack(side="right")
    body.focus_set()
    return win


def new_task(self):
    self.require_doc()
    self.filter.set("全部")
    self.query.set("")
    self.checked_ids.clear()
    self.refresh_tasks()
    return _task_dialog(self)


def insert_after_selected(self):
    selected = self.list.selection()
    if len(selected) != 1:
        raise ValueError("请先在右侧任务列表中选择一条任务。")
    rid = int(selected[0])
    return _task_dialog(self, after_id=rid)


def edit_selected(self):
    selected = self.list.selection()
    if len(selected) != 1:
        raise ValueError("请先选择一条任务进行编辑。")
    return _task_dialog(self, rid=int(selected[0]))


def copy_selected_tasks(self, mode="all"):
    ids = self.selected_ids()
    if not ids:
        selected = self.list.selection()
        ids = [int(x) for x in selected]
    if not ids:
        raise ValueError("请先选择任务。")
    rows = [self.s.get_requirement(rid) for rid in ids]
    if mode == "title":
        value = "\n".join(row.title for row in rows)
    elif mode == "detail":
        value = "\n\n".join(row.content for row in rows)
    else:
        value = "\n\n".join(
            f"{row.title}\n分组：{row.section}\n状态：{'已完成' if row.done else '未完成'}\n{row.content}"
            for row in rows
        )
    self.clipboard_clear()
    self.clipboard_append(value)
    self.status.set(f"已复制 {len(rows)} 条任务内容。")


def task_list_context_menu(self, event):
    item = self.list.identify_row(event.y)
    if item and item not in self.list.selection():
        self.list.selection_set(item)
        self.list.focus(item)
    self._task_context.tk_popup(event.x_root, event.y_root)
    return "break"


def mark(self, done):
    ids = self.selected_ids()
    if not ids:
        raise ValueError("请先勾选或选择任务。")
    for rid in ids:
        self.s.set_done(rid, done)
    self.store.snapshot(self.key)
    self.refresh_tasks()
    self.refresh_progress()
    self.status.set(f"已将 {len(ids)} 条任务标记为" + ("已完成。" if done else "未完成。"))


def delete_tasks(self):
    ids = self.selected_ids()
    if not ids:
        raise ValueError("请先勾选或选择要删除的任务。")
    if not messagebox.askyesno(
        "删除任务",
        f"确定删除 {len(ids)} 条任务吗？此操作不可撤销，并会清除当前文档的旧任务快照。",
        icon="warning", parent=self,
    ):
        return
    self.keep_draft()
    self.store.delete_tasks_permanently(self.key, ids)
    self.checked_ids.clear()
    self.load_editor(None)
    self.refresh_tasks()
    self.refresh_progress()
    self.store.snapshot(self.key)
    self.status.set(f"已删除 {len(ids)} 条任务。")


def select_all_visible(self):
    ids = set(self.visible_ids)
    if ids and ids.issubset(self.checked_ids):
        self.checked_ids.difference_update(ids)
    else:
        self.checked_ids.update(ids)
    self.refresh_tasks()


def clear_selection(self):
    self.checked_ids.clear()
    if self.list.selection():
        self.list.selection_remove(*self.list.selection())
    self.update_count()


def edit_working_table(self):
    self.require_doc()
    from reqdoc_source_table import tables_in, replace_table

    original = TASK_BLOCK.sub("", self.working_text.get("1.0", "end-1c"))
    tables = tables_in(original)
    if not tables:
        raise ValueError("当前工作文档中没有识别到 Markdown 表格。")
    if len(tables) > 1:
        messagebox.showinfo(
            "编辑表格",
            f"识别到 {len(tables)} 张表格。本次先编辑第一张；保存后可再次打开继续编辑其他表格。",
            parent=self,
        )
    table = tables[0]
    win = tk.Toplevel(self)
    win.title("编辑工作文档表格")
    self.sized_dialog(win, 900, 560)
    win.transient(self)
    win.grab_set()
    root = ttk.Frame(win, padding=10)
    root.pack(fill="both", expand=True)
    ttk.Label(
        root,
        text="双击单元格即可修改；保存后会写回左侧工作文档，不会修改原始文件。",
        foreground="#52647B",
    ).pack(anchor="w", pady=(0, 7))
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    columns = [f"c{i}" for i in range(len(table.headers))]
    grid = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
    for col, header in zip(columns, table.headers):
        grid.heading(col, text=header)
        grid.column(col, width=max(120, min(280, len(header) * 18)), minwidth=80, stretch=True)
    sy = ttk.Scrollbar(frame, orient="vertical", command=grid.yview)
    sx = ttk.Scrollbar(frame, orient="horizontal", command=grid.xview)
    grid.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
    sy.pack(side="right", fill="y")
    sx.pack(side="bottom", fill="x")
    grid.pack(fill="both", expand=True)
    for index, row in enumerate(table.rows):
        grid.insert("", "end", iid=str(index), values=row)

    def edit_cell(event):
        iid = grid.identify_row(event.y)
        column_id = grid.identify_column(event.x)
        if not iid or not column_id:
            return
        column_index = int(column_id[1:]) - 1
        values = list(grid.item(iid, "values"))
        current = values[column_index]
        header = table.headers[column_index]
        value = simpledialog.askstring(
            "编辑单元格", f"{header}：", initialvalue=current, parent=win
        )
        if value is not None:
            values[column_index] = value
            grid.item(iid, values=values)

    grid.bind("<Double-1>", edit_cell)

    def save():
        rows = [list(grid.item(iid, "values")) for iid in grid.get_children()]
        updated = replace_table(original, table, list(table.headers), rows)
        set_working_text(self, updated)
        sync_working_tasks(self, self.store.tasks(self.key))
        self._working_dirty = True
        self.save_working_document(silent=True)
        win.destroy()
        self.status.set("表格已写回独立工作文档；原始文件未修改。")

    footer = ttk.Frame(root)
    footer.pack(fill="x", pady=(8, 0))
    ttk.Button(footer, text="取消", command=win.destroy).pack(side="right", padx=(6, 0))
    ttk.Button(footer, text="保存表格修改", command=lambda: self.guard(save)).pack(side="right")
    return win


def paste_working_image(self, silent=False):
    from PIL import Image, ImageGrab, ImageOps
    from reqdoc_studio import write_atomic

    data = ImageGrab.grabclipboard()
    image_exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
    paths = []
    if isinstance(data, list):
        paths = [Path(item) for item in data if Path(item).suffix.lower() in image_exts]
    if not isinstance(data, Image.Image) and not paths:
        if not silent:
            messagebox.showinfo("粘贴图片", "剪贴板中没有可用图片。", parent=self)
        return False
    self.require_doc()
    index = self.working_text.index("insert")
    description = simpledialog.askstring("粘贴图片", "图片描述（可留空）", parent=self)
    if description is None:
        return True
    alt = description.strip().replace("[", "［").replace("]", "］")
    base = self.s.document_folder(self.key)
    raw_images = []

    def png_bytes(image):
        buffer = io.BytesIO()
        ImageOps.exif_transpose(image).convert("RGBA").save(buffer, format="PNG")
        return buffer.getvalue()

    if isinstance(data, Image.Image):
        raw_images.append(png_bytes(data))
    for path in paths:
        with Image.open(path) as image:
            raw_images.append(png_bytes(image))
    links = []
    for raw in raw_images:
        relative = f"assets/working/{hashlib.sha256(raw).hexdigest()[:24]}.png"
        if not (base / relative).is_file():
            write_atomic(base / relative, raw)
        links.append(f"![{alt}]({relative})")
    self.working_text.insert(index, "\n" + "\n".join(links) + "\n")
    self._working_dirty = True
    self.working_text.focus_set()
    self.status.set("已将剪贴板图片插入工作文档。")
    return True


def working_paste_event(self, event=None):
    if not self.key:
        return None
    return "break" if self.guard(lambda: self.paste_working_image(silent=True)) else None


def working_export_content(self):
    sync_working_tasks(self, self.store.tasks(self.key))
    content = self.working_text.get("1.0", "end-1c")
    return re.sub(rf"^(?:{re.escape(TASK_BLOCK_START)}|{re.escape(TASK_BLOCK_END)})(?:\n|$)", "", content, flags=re.MULTILINE)


def export_working_document(self, kind):
    self.require_doc()
    return self.export(kind, content=working_export_content(self))


def preview_working_document(self):
    self.require_doc()
    from reqdoc_studio import MarkdownView
    content = working_export_content(self)
    win = tk.Toplevel(self)
    win.title("工作文档预览")
    self.sized_dialog(win, 900, 720)
    view = MarkdownView(win, self.theme)
    view.pack(fill="both", expand=True, padx=10, pady=10)
    view.set_base_dir(self.s.document_folder(self.key))
    view.set_markdown(content or "（工作文档为空）")
    return win


def convert_working_document(self):
    self.require_doc()
    self.save_working_document(silent=True)
    content = self.working_text.get("1.0", "end-1c")
    if not content.strip():
        raise ValueError("工作文档为空，请先输入或导入内容。")
    rows = document_to_task_items(content)
    rows = [row for row in rows if (row.get("title") or "").strip() or (row.get("content") or "").strip()]
    if not rows:
        raise ValueError("没有识别到可转换的任务内容。")
    if len(rows) > 500:
        raise ValueError("一次最多转换 500 条任务，请先缩小内容范围。")
    if not messagebox.askyesno(
        "工作文档转换为任务",
        f"将从当前工作文档生成 {len(rows)} 条任务。\n工作文档和原始资料都会保留；重复转换会产生重复任务。\n\n是否继续？",
        parent=self,
    ):
        return
    self.s.commit_task_rows(self.key, rows)
    self.store.snapshot(self.key)
    self.refresh_tasks()
    self.refresh_progress()
    self.refresh_home()
    self.status.set(f"已从工作文档生成 {len(rows)} 条任务；工作文档仍可继续编辑。")


def import_dialog(self):
    self.require_doc()
    paths = filedialog.askopenfilenames(
        parent=self, title="选择要导入的资料（可多选）",
        filetypes=[
            ("Word / Markdown / TXT", ("*.docx", "*.md", "*.markdown", "*.txt")),
            ("Word 文档", "*.docx"),
            ("Markdown 文档", ("*.md", "*.markdown")),
            ("文本文件", "*.txt"),
        ],
    )
    if paths:
        self.import_paths(paths)


def import_paths(self, paths):
    self.require_doc()
    from reqdoc_studio import read_source
    from reqdoc_win_drop import select_drop_files

    paths, skipped = select_drop_files(tuple(str(path) for path in paths))
    if skipped:
        messagebox.showwarning(
            "部分文件未导入",
            "不支持的文件：" + "、".join(skipped[:12]) + "\n支持 docx、txt、md、markdown。",
            parent=self,
        )
    if not paths:
        return
    prepared = []
    for raw in paths:
        path = Path(raw).resolve()
        text, _ = read_source(path)
        prepared.append((path, text))
    if not prepared:
        raise ValueError("没有可导入的资料。")
    win = tk.Toplevel(self)
    win.title("导入文件：形成工作文档或任务")
    self.sized_dialog(win, 900, 680)
    win.transient(self)
    win.grab_set()
    root = ttk.Frame(win, padding=12)
    root.pack(fill="both", expand=True)
    footer = ttk.Frame(root)
    footer.pack(side="bottom", fill="x", pady=(8, 0))
    ttk.Label(root, text=f"已读取 {len(prepared)} 份文件。原文件只归档，不作为编辑对象。", font=("Microsoft YaHei", 12, "bold")).pack(anchor="w")
    ttk.Label(root, text="选择导入后的处理方式：", foreground="#64748B").pack(anchor="w", pady=(8, 3))
    mode_var = tk.StringVar(value="table")
    ttk.Radiobutton(
        root, text="形成可编辑表格 / 文档（不自动生成任务）",
        variable=mode_var, value="table",
    ).pack(anchor="w")
    ttk.Radiobutton(
        root, text="形成任务（同时生成并保留可编辑工作文档）",
        variable=mode_var, value="tasks",
    ).pack(anchor="w")
    append_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(root, text="追加到当前工作文档末尾（不勾选则替换工作文档内容）", variable=append_var).pack(anchor="w", pady=(5, 7))
    ttk.Label(
        root,
        text="导入文件：" + "、".join(path.name for path, _ in prepared[:8]) + (" 等" if len(prepared) > 8 else ""),
        foreground="#52647B", wraplength=840, justify="left",
    ).pack(anchor="w", pady=(8, 12))
    def commit():
        as_tasks = mode_var.get() == "tasks"
        if as_tasks:
            estimated = sum(len(document_to_task_items(text)) for _, text in prepared)
            if not estimated:
                raise ValueError("没有识别到可转换的内容。可选择先导入为工作文档。")
            if estimated > 500:
                raise ValueError("一次导入最多生成 500 条任务，请先缩小文件范围。")
        all_warnings = []
        mode = "import-tasks" if as_tasks else "import-table"
        with ExitStack() as rollback:
            with self.s.connect() as connection:
                archived_docs = []
                for path, _ in prepared:
                    notes, archived_text = self.store.archive_source(
                        self.key, path, connection=connection, rollback=rollback
                    )
                    all_warnings.extend(f"{path.name}：{note}" for note in notes)
                    archived_docs.append((path, archived_text))
                imported = "\n\n".join(
                    f"# {path.stem}\n\n{text}" if len(archived_docs) > 1 else text
                    for path, text in archived_docs
                )
                old = TASK_BLOCK.sub("", self.working_text.get("1.0", "end-1c"))
                content = (old.rstrip() + "\n\n---\n\n" + imported) if append_var.get() and old.strip() else imported
                rows = [row for _, text in archived_docs for row in document_to_task_items(text)] if as_tasks else []
                if as_tasks and not rows:
                    raise ValueError("没有识别到可转换的内容。可选择先导入为工作文档。")
                if len(rows) > 500:
                    raise ValueError("一次导入最多生成 500 条任务，请分批导入。")
                write_working_document(self.store, self.key, content, mode, connection=connection)
                if rows:
                    self.s.commit_task_rows(self.key, rows, connection=connection)
            rollback.pop_all()
        self._working_mode = mode
        set_working_text(self, content)
        win.destroy()
        if rows:
            self.store.snapshot(self.key)
        self.refresh_tasks()
        self.refresh_progress()
        self.refresh_home()
        self.refresh_import_page()
        self.refresh_export_page()
        self.show_page("tasks")
        self.status.set(
            f"已归档 {len(archived_docs)} 份原始文件；工作文档已独立生成。"
            + (f" 并生成 {len(rows)} 条任务。" if as_tasks else " 当前为可编辑表格/文档模式。")
        )
        if all_warnings:
            messagebox.showwarning("导入提示", "\n".join(all_warnings[:30]), parent=self)

    ttk.Button(footer, text="取消", command=win.destroy).pack(side="right", padx=(6, 0))
    ttk.Button(footer, text="确认导入", command=lambda: self.guard(commit)).pack(side="right")
    return win


def refresh_progress(self, reset_group=False):
    if not hasattr(self, "progress_chapters"):
        return
    projects = self.store.sort_tree_items("project", "root", self.s.project_groups(), lambda project: str(project.id))
    options = ["全部项目"] + [project.name for project in projects]
    self.progress_scope_box.configure(values=options)
    scope = self.progress_scope_var.get()
    if scope not in options:
        scope = "全部项目"
        self.progress_scope_var.set(scope)
    tree = self.progress_chapters
    previous = tree.selection()[0] if tree.selection() and not reset_group else None
    open_projects = {item: tree.item(item, "open") for item in tree.get_children()}
    tree.delete(*tree.get_children())
    self.progress_group_map = {}
    all_projects = scope == "全部项目"
    entries = []
    doc_count = 0
    for project in projects:
        if not all_projects and project.name != scope:
            continue
        parent = f"project:{project.id}" if all_projects else ""
        project_rows = []
        if parent:
            tree.insert("", "end", iid=parent, text=project.name, open=open_projects.get(parent, True))
            self.progress_group_map[parent] = {"items": project_rows}
        docs = self.store.sort_tree_items("document", str(project.id), self.s.documents_for(project.name), lambda doc: doc.key)
        for doc in docs:
            items = [(project.name, doc.name, doc.key, row) for row in self.store.tasks(doc.key)]
            iid = f"document:{doc.key}"
            tree.insert(parent, "end", iid=iid, text=doc.name, values=(len(items),))
            self.progress_group_map[iid] = {"items": items}
            project_rows.extend(items)
            doc_count += 1
        if parent:
            tree.item(parent, values=(len(project_rows),))
        entries.extend(project_rows)
    total = len(entries)
    done = sum(bool(item[3].done) for item in entries)
    pending = total - done
    self.progress_total.set(str(total))
    self.progress_done.set(str(done))
    self.progress_open.set(str(pending))
    self.progress_rate.set(f"{done / total:.0%}" if total else "0%")
    self.progress_bar.configure(value=(done / total * 100) if total else 0)
    self.progress_hint.configure(
        text=f"统计范围：{scope} · {doc_count} 份文档 · {total} 条任务"
    )
    children = tree.get_children()
    if children:
        tree.selection_set(previous if previous in self.progress_group_map else children[0])
    self.refresh_progress_tasks()


def refresh_progress_tasks(self):
    if not hasattr(self, "progress_detail"):
        return
    selected = self.progress_chapters.selection()
    group = self.progress_group_map.get(selected[0]) if selected else None
    self.progress_task_doc_map = {}
    lines = []
    if group:
        for project, name, key, row in group["items"]:
            lines.extend([
                f"{row.title}",
                f"所属项目：{project}    所属文档：{name}",
                f"章节：{row.section or '未分类'}    状态：{'已完成' if row.done else '未完成'}    任务编号：{row.id}",
                row.content.strip(),
                "─" * 56,
                "",
            ])
            self.progress_task_doc_map[row.id] = key
    self.progress_detail.configure(state="normal")
    self.progress_detail.delete("1.0", "end")
    self.progress_detail.insert("1.0", "\n".join(lines) if lines else "当前分组没有任务。")
    self.progress_detail.configure(state="disabled")


def edit_progress_task(self, event=None):
    selected = self.progress_chapters.selection()
    group = self.progress_group_map.get(selected[0]) if selected else None
    if not group or not group["items"]:
        raise ValueError("当前分组没有可编辑任务。")
    row = group["items"][0][3]
    key = group["items"][0][2]
    if key != self.key:
        self.load_doc(key)
    return _task_dialog(self, rid=row.id)


def build_settings_page(self):
    from version import VERSION

    page = self.pages["settings"]
    self.page_title(page, "设置与数据", "管理文件库保存位置和软件更新。")
    box = ttk.LabelFrame(page, text="文件库保存路径", padding=12)
    box.pack(fill="x")
    ttk.Label(box, text=str(self.store.settings.library_root), wraplength=600).pack(anchor="w", pady=(0, 8))
    ttk.Button(box, text="更改路径", command=lambda: self.guard(lambda: change_library_folder(self))).pack(anchor="w")

    update_box = ttk.LabelFrame(page, text="软件更新 · GitHub", padding=12)
    update_box.pack(fill="x", pady=(10, 0))
    self.update_status_var = tk.StringVar(value=f"当前版本：V{VERSION} · GitHub 单仓库更新")
    ttk.Label(update_box, textvariable=self.update_status_var, foreground="#52647B").pack(
        side="left", fill="x", expand=True
    )
    self.update_button = ttk.Button(
        update_box, text="检查 GitHub 更新",
        command=lambda: self.guard(self.check_github_update),
    )
    self.update_button.pack(side="right")
    self._update_queue = None
    self._update_in_progress = False
    self._update_poll_after_id = None
    ttk.Button(page, text="使用说明", command=lambda: self.guard(self.help)).pack(anchor="w", pady=(10, 0))


def change_library_folder(self):
    if self._update_in_progress:
        raise ValueError("请先完成当前的软件更新。")
    current = self.store.settings.library_root.resolve()
    selected = filedialog.askdirectory(
        parent=self, title="选择空文件夹作为新文件库", initialdir=str(current.parent), mustexist=True,
    )
    if not selected or Path(selected).resolve() == current:
        return
    if not messagebox.askyesno(
        "更改文件库路径",
        f"将当前项目、文档和图片复制到下方目录，再重新打开工作台。原目录留存。\n\n{selected}",
        parent=self,
    ):
        return
    self.keep_draft()
    self.persist_notes()
    self.store.relocate_library(selected)
    self._library_changed = True
    self.close()


def help_text(self):
    from version import VERSION

    self.show_text(
        f"ReqDoc Studio V{VERSION} 使用说明",
        """【两种文档工作流程】
1. 手动输入：打开任务处理，在左侧工作文档直接输入、编辑文字、Markdown 表格和图片；保存后点击“转成任务”。
2. 导入文件：选择 Word、Markdown 或 TXT；导入时选择“形成可编辑表格/文档”或“形成任务”。两种方式都会把原始文件只读归档，并生成独立工作文档。

【工作文档与源文件】
工作文档是独立编辑副本，保存在本机数据库中。Markdown 表格可通过“编辑表格”双击单元格修改。复制图片后，将光标放在正文中，按 Ctrl+V 或点击“粘贴图片”。图片描述可留空，插入后会显示缩略图。“导出文档”可预览内容，或导出 Word 和 Markdown 图片包。

【任务处理】
右侧任务列表用于新增、插入、编辑、标记完成和删除任务。确认后直接从当前任务库删除。左侧任务清单会随右侧更新，正文可独立编辑。选择任务后，可在下方明细中框选文字并右键复制。

【进度统计】
顶部只选择全部项目或单个项目。下方左侧竖向显示项目/文档分组，右侧任务明细可以鼠标框选文本并右键复制。

【文件库】
在“设置与数据”中查看或更改文件库保存路径。切换路径时会将当前文件库内容复制到新位置。原始文件归档保持只读。""",
    )


def install(StudioClass, StoreClass):
    """Install the active V8 workspace and independent working-document persistence."""
    original_store_init = StoreClass.__init__

    def store_init(self, settings):
        original_store_init(self, settings)
        ensure_working_schema(self)

    StoreClass.__init__ = store_init

    original_cleanup = StoreClass._cleanup_studio_refs

    def cleanup(self, doc_keys, task_ids):
        original_cleanup(self, doc_keys, task_ids)
        delete_working_documents(self, doc_keys)

    StoreClass._cleanup_studio_refs = cleanup

    original_migrate = getattr(StoreClass, "import_legacy_data", None)
    if callable(original_migrate):
        def migrate(self, *args, **kwargs):
            result = original_migrate(self, *args, **kwargs)
            ensure_working_schema(self)
            return result
        StoreClass.import_legacy_data = migrate

    def removed_summary_preview(self):
        raise ValueError("旧的文档汇总预览已移除。请直接编辑左侧工作文档。")

    replacements = {
        "build_home_page": build_home_page,
        "build_import_page": build_import_page,
        "refresh_import_page": refresh_import_page,
        "build_tasks_page": build_tasks_page,
        "build_progress_page": build_progress_page,
        "build_export_page": build_export_page,
        "build_settings_page": build_settings_page,
        "position_task_sashes": position_task_sashes,
        "position_progress_sashes": position_progress_sashes,
        "working_document_modified": working_document_modified,
        "load_working_document": load_working_document,
        "save_working_document": save_working_document,
        "keep_draft": keep_draft,
        "autosave": autosave,
        "load_doc": load_doc,
        "refresh_task_source": refresh_task_source,
        "on_task_project_selected": on_task_project_selected,
        "on_task_document_selected": on_task_document_selected,
        "clear_task_document": clear_task_document,
        "refresh_tasks": refresh_tasks,
        "update_count": update_count,
        "selected_ids": selected_ids,
        "task_cell_click": task_cell_click,
        "on_task_selected": on_task_selected,
        "task_list_right_click": task_list_right_click,
        "refresh_task_detail": refresh_task_detail,
        "load_editor": load_editor,
        "show_editor": show_editor,
        "hide_editor": hide_editor,
        "save_task": save_task,
        "save_current": save_current,
        "start_manual": start_manual,
        "new_task": new_task,
        "insert_after_selected": insert_after_selected,
        "edit_selected": edit_selected,
        "copy_selected_tasks": copy_selected_tasks,
        "task_list_context_menu": task_list_context_menu,
        "mark": mark,
        "delete_tasks": delete_tasks,
        "select_all_visible": select_all_visible,
        "clear_selection": clear_selection,
        "edit_working_table": edit_working_table,
        "paste_working_image": paste_working_image,
        "working_paste_event": working_paste_event,
        "preview_working_document": preview_working_document,
        "convert_working_document": convert_working_document,
        "import_dialog": import_dialog,
        "import_paths": import_paths,
        "refresh_progress": refresh_progress,
        "refresh_progress_tasks": refresh_progress_tasks,
        "edit_progress_task": edit_progress_task,
        "help": help_text,
        "show_preview": removed_summary_preview,
    }
    for name, method in replacements.items():
        setattr(StudioClass, name, method)
