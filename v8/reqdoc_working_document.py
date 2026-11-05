"""Independent editable working-document workspace for ReqDoc Studio.

Original imported files remain immutable in studio_sources/_sources. This module stores
an independent editable Markdown working copy and keeps task records separate.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import uuid
from collections import defaultdict
from contextlib import ExitStack, nullcontext
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from reqdoc.gui.widgets import DOCUMENT_FONT, configure_document_styles, style_document_lines
from reqdoc_organize import ORGANIZED_PREFIX


TASK_BLOCK_START = "<!-- reqdoc:tasks -->"
TASK_BLOCK_END = "<!-- /reqdoc:tasks -->"
TASK_START_MARK = "reqdoc_tasks_start"
TASK_END_MARK = "reqdoc_tasks_end"
MAIN_DOCUMENT_LABEL = "工作文档 / 任务清单"
LEGACY_IMPORT_COPY = "legacy-import"
TASK_BLOCK = re.compile(
    rf"^{re.escape(TASK_BLOCK_START)}\n.*?^{re.escape(TASK_BLOCK_END)}(?:\n|\Z)",
    re.MULTILINE | re.DOTALL,
)
IMAGE_LINK = re.compile(r"!\[([^\]\r\n]*)\]\(([^)\r\n]+)\)")
TASK_FIELDS = ("title", "section", "content")


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS studio_import_task_links(
                doc TEXT NOT NULL,
                copy_id TEXT NOT NULL,
                task_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                source_item TEXT NOT NULL,
                PRIMARY KEY(doc,copy_id,task_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS studio_import_copies(
                doc TEXT NOT NULL,
                id TEXT NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                PRIMARY KEY(doc,id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS studio_deleted_copies(
                doc TEXT NOT NULL,
                id TEXT NOT NULL,
                PRIMARY KEY(doc,id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS studio_conversion_modes(
                doc TEXT NOT NULL,
                copy_id TEXT NOT NULL,
                settings TEXT NOT NULL,
                PRIMARY KEY(doc,copy_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS studio_sync_conflicts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc TEXT NOT NULL,
                copy_id TEXT NOT NULL,
                task_id INTEGER NOT NULL,
                field TEXT NOT NULL,
                previous_value TEXT NOT NULL,
                source_value TEXT NOT NULL,
                task_value TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unresolved',
                resolution TEXT NOT NULL DEFAULT '',
                resolved_at TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_studio_sync_conflicts_doc_status
            ON studio_sync_conflicts(doc,status,detected_at)
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


def validate_working_content(content):
    if not isinstance(content, str):
        raise ValueError("工作文档内容必须是文本。")
    if len(content.encode("utf-8")) > 20 * 1024 * 1024:
        raise ValueError("工作文档不能超过 20 MB。")


def write_working_document(store, key, content, mode="manual", *, connection=None):
    validate_working_content(content)
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


def write_import_copy(conn, key, copy_id, name, content):
    validate_working_content(content)
    if conn.execute("SELECT 1 FROM studio_deleted_copies WHERE doc=? AND id=?", (key, copy_id)).fetchone():
        raise ValueError("该副本已删除，请重新导入文件。")
    conn.execute("""
        INSERT INTO studio_import_copies(doc,id,name,content) VALUES(?,?,?,?)
        ON CONFLICT(doc,id) DO UPDATE SET content=excluded.content
    """, (key, copy_id, name, content))


def read_import_copy(store, key, copy_id):
    with store.conn() as conn:
        if conn.execute("SELECT 1 FROM studio_deleted_copies WHERE doc=? AND id=?", (key, copy_id)).fetchone():
            raise ValueError("该副本已删除。")
        row = conn.execute(
            "SELECT name,content FROM studio_import_copies WHERE doc=? AND id=?",
            (key, copy_id),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT name,content FROM studio_sources WHERE doc=? AND id=?",
                (key, copy_id),
            ).fetchone()
        if row is None:
            raise ValueError("导入副本不存在。")
        return row


def delete_editable_copy(store, key, copy_id, *, connection=None):
    if not copy_id:
        raise ValueError("请先在‘显示文档’中选择要删除的副本。")
    if connection is None:
        ensure_working_schema(store)
    with (store.conn() if connection is None else nullcontext(connection)) as conn:
        if conn.execute("SELECT 1 FROM studio_deleted_copies WHERE doc=? AND id=?", (key, copy_id)).fetchone():
            return False
        existing = conn.execute("""
            SELECT 1 FROM studio_import_copies WHERE doc=? AND id=?
            UNION ALL SELECT 1 FROM studio_sources WHERE doc=? AND id=? LIMIT 1
        """, (key, copy_id, key, copy_id)).fetchone()
        if not existing:
            raise ValueError("副本不存在，请重新选择文档。")
        conn.execute("INSERT INTO studio_deleted_copies(doc,id) VALUES(?,?)", (key, copy_id))
        conn.execute("DELETE FROM studio_import_copies WHERE doc=? AND id=?", (key, copy_id))
        conn.execute("DELETE FROM studio_import_task_links WHERE doc=? AND copy_id=?", (key, copy_id))
        conn.execute("DELETE FROM studio_conversion_modes WHERE doc=? AND copy_id=?", (key, copy_id))
    return True


def separate_legacy_import(conn, key, content):
    """Keep edits from older versions that put imported text in the main editor."""
    if content.strip():
        sources = conn.execute(
            "SELECT id,name,content FROM studio_sources WHERE doc=? ORDER BY created", (key,)
        ).fetchall()
        if len(sources) == 1:
            copy_id, name, original = sources[0]
            write_import_copy(conn, key, copy_id, name, content)
            link_existing_copy_tasks(conn, key, copy_id, document_to_task_items(original))
        else:
            matches = [source for source in sources if content.rstrip() == source[2].rstrip()]
            if len(matches) == 1:
                copy_id, _, original = matches[0]
                link_existing_copy_tasks(conn, key, copy_id, document_to_task_items(original))
            elif not matches:
                write_import_copy(conn, key, LEGACY_IMPORT_COPY, "旧版工作副本", content)
    conn.execute(
        "UPDATE studio_working_docs SET content='',mode='manual',updated=? WHERE doc=?",
        (_now(), key),
    )


def delete_working_documents(store, keys):
    ensure_working_schema(store)
    with store.conn() as conn:
        keys = [(key,) for key in keys]
        conn.executemany("DELETE FROM studio_working_docs WHERE doc=?", keys)
        conn.executemany("DELETE FROM studio_import_copies WHERE doc=?", keys)
        conn.executemany("DELETE FROM studio_import_task_links WHERE doc=?", keys)
        conn.executemany("DELETE FROM studio_deleted_copies WHERE doc=?", keys)
        conn.executemany("DELETE FROM studio_conversion_modes WHERE doc=?", keys)


def import_item_values(item):
    return {field: item[field] for field in TASK_FIELDS}


def import_task_links(conn, key, copy_id):
    return [
        (task_id, position, json.loads(source))
        for task_id, position, source in conn.execute(
            """SELECT task_id,l.position,source_item FROM studio_import_task_links l
               JOIN reqdoc_requirements r ON r.id=l.task_id AND r.project=l.doc
               WHERE l.doc=? AND copy_id=? ORDER BY l.position,task_id""", (key, copy_id)
        )
    ]


def save_import_task_link(conn, key, copy_id, task_id, position, item):
    conn.execute("""
        INSERT INTO studio_import_task_links(doc,copy_id,task_id,position,source_item)
        VALUES(?,?,?,?,?) ON CONFLICT(doc,copy_id,task_id) DO UPDATE SET
            position=excluded.position, source_item=excluded.source_item
    """, (key, copy_id, task_id, position, json.dumps(import_item_values(item), ensure_ascii=False)))


def link_existing_copy_tasks(conn, key, copy_id, items):
    """Recover old associations only when every source item has one exact match."""
    if not items or import_task_links(conn, key, copy_id):
        return
    candidates = defaultdict(list)
    for task_id, title, section, content in conn.execute("""
        SELECT r.id,r.title,r.section,r.content FROM reqdoc_requirements r
        WHERE r.project=? AND NOT EXISTS(SELECT 1 FROM studio_trash t WHERE t.id=r.id)
            AND NOT EXISTS(SELECT 1 FROM studio_import_task_links l WHERE l.doc=r.project AND l.task_id=r.id)
    """, (key,)):
        candidates[(title, section, content)].append(task_id)
    matches = [candidates[tuple(import_item_values(item).values())] for item in items]
    if any(len(task_ids) != 1 for task_ids in matches):
        return
    ids = [task_ids[0] for task_ids in matches]
    if len(set(ids)) != len(ids):
        return
    for position, (task_id, item) in enumerate(zip(ids, items)):
        save_import_task_link(conn, key, copy_id, task_id, position, item)


def match_import_task_links(links, items):
    """Match exact and unique source fields before using stable positions."""
    remaining_links = {task_id: (position, item) for task_id, position, item in links}
    remaining_items = dict(enumerate(items))
    matches = {}
    for fields in (TASK_FIELDS, ("title", "section"), ("section", "content"), ("title", "content")):
        left, right = defaultdict(list), defaultdict(list)
        for task_id, (_, item) in remaining_links.items():
            left[tuple(item[field] for field in fields)].append(task_id)
        for position, item in remaining_items.items():
            right[tuple(item[field] for field in fields)].append(position)
        for fingerprint in left.keys() & right.keys():
            ids, positions = left[fingerprint], right[fingerprint]
            if len(ids) == len(positions) == 1 or (fields == TASK_FIELDS and len(ids) == len(positions)):
                for task_id, position in zip(ids, positions):
                    matches[position] = (task_id, remaining_links.pop(task_id)[1])
                    remaining_items.pop(position)
    if len(links) == len(items):
        for task_id, (position, item) in remaining_links.items():
            if position in remaining_items:
                matches[position] = (task_id, item)
                remaining_items.pop(position)
    return matches


def _record_sync_conflict(conn, key, copy_id, task_id, field, previous, source, task):
    row = conn.execute("""
        SELECT id FROM studio_sync_conflicts
        WHERE doc=? AND copy_id=? AND task_id=? AND field=? AND status='unresolved'
        ORDER BY id DESC LIMIT 1
    """, (key, copy_id, task_id, field)).fetchone()
    values = (str(previous), str(source), str(task), _now())
    if row:
        conn.execute("""
            UPDATE studio_sync_conflicts
            SET previous_value=?,source_value=?,task_value=?,detected_at=?
            WHERE id=?
        """, (*values, row[0]))
    else:
        conn.execute("""
            INSERT INTO studio_sync_conflicts(
                doc,copy_id,task_id,field,previous_value,source_value,task_value,detected_at
            ) VALUES(?,?,?,?,?,?,?,?)
        """, (key, copy_id, task_id, field, *values))


def _resolve_converged_sync_conflict(conn, key, copy_id, task_id, field):
    conn.execute("""
        UPDATE studio_sync_conflicts
        SET status='resolved',resolution='converged',resolved_at=?
        WHERE doc=? AND copy_id=? AND task_id=? AND field=? AND status='unresolved'
    """, (_now(), key, copy_id, task_id, field))


def sync_import_tasks(conn, key, copy_id, content):
    """Sync source edits without silently overwriting independent task edits."""
    links = import_task_links(conn, key, copy_id)
    if not links:
        return False
    from reqdoc_task_conversion import conversion_items, conversion_settings
    items = conversion_items(content, conversion_settings(conn, key, copy_id))
    changed = False
    for position, (task_id, previous) in match_import_task_links(links, items).items():
        current = import_item_values(items[position])
        task_row = conn.execute("""
            SELECT title,section,content FROM reqdoc_requirements
            WHERE project=? AND id=?
              AND NOT EXISTS(SELECT 1 FROM studio_trash WHERE id=?)
        """, (key, task_id, task_id)).fetchone()
        if task_row is None:
            continue
        task_values = dict(zip(TASK_FIELDS, task_row))
        baseline = dict(previous)
        updates = {}
        for field in TASK_FIELDS:
            source_changed = current[field] != previous[field]
            task_changed = task_values[field] != previous[field]
            if source_changed and task_changed and current[field] != task_values[field]:
                _record_sync_conflict(
                    conn, key, copy_id, task_id, field,
                    previous[field], current[field], task_values[field],
                )
                # Keep the previous common ancestor until the user resolves the conflict.
                continue
            if source_changed and current[field] != task_values[field]:
                updates[field] = current[field]
            baseline[field] = current[field]
            if current[field] == task_values[field]:
                _resolve_converged_sync_conflict(conn, key, copy_id, task_id, field)
        if updates:
            updated = conn.execute("""
                UPDATE reqdoc_requirements SET title=coalesce(?,title), section=coalesce(?,section),
                    content=coalesce(?,content), updated_at=?
                WHERE project=? AND id=? AND NOT EXISTS(SELECT 1 FROM studio_trash WHERE id=?)
            """, (
                updates.get("title"), updates.get("section"), updates.get("content"),
                _now(), key, task_id, task_id,
            ))
            changed |= bool(updated.rowcount)
        if baseline != previous:
            save_import_task_link(conn, key, copy_id, task_id, position, baseline)
    return changed


def refresh_sync_conflict_button(self):
    button = getattr(self, "sync_conflict_button", None)
    if button is None:
        return
    count = 0
    if self.key:
        with self.store.conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM studio_sync_conflicts WHERE doc=? AND status='unresolved'",
                (self.key,),
            ).fetchone()[0]
    button.configure(text=f"同步冲突 ({count})")
    button.state(["!disabled"] if count else ["disabled"])


def resolve_sync_conflict(self, conflict_id, choice):
    if choice not in ("source", "task"):
        raise ValueError("未知的冲突处理方式。")
    with self.store.conn() as conn:
        row = conn.execute("""
            SELECT doc,copy_id,task_id,field,source_value,status
            FROM studio_sync_conflicts WHERE id=?
        """, (int(conflict_id),)).fetchone()
        if row is None or row[5] != "unresolved":
            raise ValueError("这条同步冲突已处理或不存在。")
        doc, copy_id, task_id, field, source_value, _ = row
        if doc != self.key:
            raise ValueError("冲突不属于当前文档，请重新打开对应文档。")
        if field not in TASK_FIELDS:
            raise ValueError("冲突字段无效，已拒绝修改。")
        link = conn.execute("""
            SELECT position,source_item FROM studio_import_task_links
            WHERE doc=? AND copy_id=? AND task_id=?
        """, (doc, copy_id, task_id)).fetchone()
        if link is None:
            raise ValueError("任务来源关联已不存在，不能自动解决该冲突。")
        if choice == "source":
            updated = conn.execute(
                f"""UPDATE reqdoc_requirements SET {field}=?,updated_at=?
                    WHERE project=? AND id=?
                      AND NOT EXISTS(SELECT 1 FROM studio_trash WHERE id=?)""",
                (source_value, _now(), doc, task_id, task_id),
            )
            if not updated.rowcount:
                raise ValueError("关联任务已不存在，不能采用来源内容。")
        baseline = json.loads(link[1])
        baseline[field] = source_value
        save_import_task_link(conn, doc, copy_id, task_id, link[0], baseline)
        conn.execute("""
            UPDATE studio_sync_conflicts
            SET status='resolved',resolution=?,resolved_at=?
            WHERE id=? AND status='unresolved'
        """, (choice, _now(), int(conflict_id)))
    self.refresh_tasks()
    self.refresh_progress()
    if self.key:
        sync_working_tasks(self, self.store.tasks(self.key), force=True)
    refresh_sync_conflict_button(self)
    self.status.set("已采用来源内容解决冲突。" if choice == "source" else "已保留任务内容并记录当前来源版本。")


def show_sync_conflicts(self):
    if not self.key:
        raise ValueError("请先选择一份文档。")
    from reqdoc_task_conversion import MAIN_COPY_ID
    with self.store.conn() as conn:
        rows = conn.execute("""
            SELECT c.id,c.copy_id,c.task_id,c.field,c.previous_value,c.source_value,
                   c.task_value,c.detected_at,
                   CASE WHEN c.copy_id=? THEN '工作文档'
                        ELSE COALESCE(i.name,s.name,'导入资料') END
            FROM studio_sync_conflicts c
            LEFT JOIN studio_import_copies i ON i.doc=c.doc AND i.id=c.copy_id
            LEFT JOIN studio_sources s ON s.doc=c.doc AND s.id=c.copy_id
            WHERE c.doc=? AND c.status='unresolved'
            ORDER BY c.detected_at DESC,c.id DESC
        """, (MAIN_COPY_ID, self.key)).fetchall()
    if not rows:
        messagebox.showinfo("同步冲突", "当前文档没有待处理的同步冲突。", parent=self)
        refresh_sync_conflict_button(self)
        return
    win = tk.Toplevel(self)
    win.title("来源与任务同步冲突")
    self.sized_dialog(win, 980, 650)
    win.transient(self)
    panel = ttk.Frame(win, padding=12)
    panel.pack(fill="both", expand=True)
    ttk.Label(
        panel,
        text="来源和任务都修改了同一字段。系统不会静默覆盖；请选择采用来源内容，或保留任务内容。",
        wraplength=900,
    ).pack(anchor="w", pady=(0, 8))
    tree = ttk.Treeview(panel, columns=("task", "source", "field", "time"), show="headings", height=8)
    for column, title, width in (
        ("task", "任务 ID", 90), ("source", "来源文档", 300),
        ("field", "冲突字段", 120), ("time", "发现时间", 180),
    ):
        tree.heading(column, text=title)
        tree.column(column, width=width, minwidth=70)
    tree.pack(fill="x", expand=False)
    by_id = {str(row[0]): row for row in rows}
    for row in rows:
        field_label = {"title": "标题", "section": "主题/章节", "content": "正文"}.get(row[3], row[3])
        tree.insert("", "end", iid=str(row[0]), values=(row[2], row[8], field_label, row[7]))
    detail = tk.Text(panel, height=13, wrap="word", padx=10, pady=8)
    detail.pack(fill="both", expand=True, pady=(8, 8))
    detail.configure(state="disabled")

    def render_detail(event=None):
        selected = tree.selection()
        if not selected:
            return
        row = by_id[selected[0]]
        field_label = {"title": "标题", "section": "主题/章节", "content": "正文"}.get(row[3], row[3])
        body = (
            f"任务 ID：{row[2]}\n来源：{row[8]}\n字段：{field_label}\n发现时间：{row[7]}\n\n"
            f"共同基线：\n{row[4]}\n\n来源新内容：\n{row[5]}\n\n任务新内容：\n{row[6]}"
        )
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", body)
        detail.configure(state="disabled")

    tree.bind("<<TreeviewSelect>>", render_detail)
    if rows:
        tree.selection_set(str(rows[0][0]))
        render_detail()

    actions = ttk.Frame(panel)
    actions.pack(fill="x")

    def apply(choice):
        selected = tree.selection()
        if not selected:
            raise ValueError("请先选择一条冲突。")
        resolve_sync_conflict(self, int(selected[0]), choice)
        win.destroy()
        refresh_sync_conflict_button(self)
        with self.store.conn() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM studio_sync_conflicts WHERE doc=? AND status='unresolved'",
                (self.key,),
            ).fetchone()[0]
        if remaining:
            show_sync_conflicts(self)

    ttk.Button(actions, text="采用来源内容", command=lambda: self.guard(lambda: apply("source"))).pack(side="left")
    ttk.Button(actions, text="保留任务内容", command=lambda: self.guard(lambda: apply("task"))).pack(side="left", padx=(8, 0))
    ttk.Button(actions, text="稍后处理", command=win.destroy).pack(side="right")
    return win


def add_import_tasks(service, conn, key, copy_id, items):
    if not items:
        return 0
    positions, rows = zip(*items)
    ids = service.commit_task_rows(key, rows, connection=conn)
    for task_id, position, row in zip(ids, positions, rows):
        save_import_task_link(conn, key, copy_id, task_id, position, row)
    return len(ids)


def document_to_task_items(content):
    """Convert prose and table rows in document order, excluding the live task block."""
    from reqdoc_studio import _section_parser, split_sections, import_task_title
    from reqdoc_source_table import tables_in, table_to_task_items, task_title_column

    from reqdoc_task_conversion import strip_task_block
    content = strip_task_block(content)
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


def _text_menu(widget, event, delete_image=None, add_task=None):
    menu = tk.Menu(widget, tearoff=False)
    if add_task is not None:
        menu.add_command(label="新增任务", command=add_task)
        menu.add_separator()
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


def working_document_menu(self, event):
    text = self.working_text
    index = text.index(f"@{event.x},{event.y}")
    images = text.dump(index, f"{index}+1c", image=True)
    link = self._working_image_links.get(images[0][1]) if images else None
    after_id = None
    if self._working_synced_block and text.compare(TASK_START_MARK, "<=", index) and text.compare(index, "<", TASK_END_MARK):
        for rid, (start, _, _) in self._working_task_marks.items():
            if text.compare(f"{start} -1 lines linestart", "<=", index):
                after_id = rid
    selection = ""
    if text.tag_ranges("sel") and text.compare("sel.first", "<=", index) and text.compare(index, "<", "sel.last"):
        selection = text.get("sel.first", "sel.last").strip()

    def delete_image():
        text.delete(index)
        text.delete(*link)

    def add_task():
        self.require_doc()
        flush_working_task_edits(self)
        active = {row.id for row in self.store.tasks(self.key)}
        return _task_dialog(self, after_id=after_id if after_id in active else None, initial_content=selection)

    return _text_menu(
        text, event,
        delete_image if link is not None else None,
        (lambda: self.guard(add_task)) if self.key else None,
    )


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


def configure_task_detail_styles(widget):
    widget.configure(font=(DOCUMENT_FONT, 11))
    widget.tag_configure("detail_title", font=(DOCUMENT_FONT, 13, "bold"), spacing1=4, spacing3=3)
    widget.tag_configure("detail_metadata", font=(DOCUMENT_FONT, 9), foreground="#52647B")
    widget.tag_raise("sel")


def render_task_details(widget, entries, empty_message, separator_width):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    if not entries:
        widget.insert("end", empty_message)
    for title, metadata, body in entries:
        widget.insert("end", title + "\n", "detail_title")
        widget.insert("end", metadata + "\n", "detail_metadata")
        widget.insert("end", body + "\n", ())
        widget.insert("end", "─" * separator_width + "\n", "detail_metadata")
    widget.configure(state="disabled")


def refresh_task_detail(self):
    if not hasattr(self, "task_detail"):
        return
    selected = list(self.list.selection()) if hasattr(self, "list") else []
    entries = []
    for item in selected:
        if not str(item).isdigit():
            continue
        try:
            row = self.s.get_requirement(int(item))
        except Exception:
            continue
        entries.append((
            row.title or "（无标题）",
            f"所属分组：{row.section or '未分类'}    状态：{'已完成' if row.done else '未完成'}\n",
            row.content.strip() or "（无任务详情）",
        ))
    render_task_details(self.task_detail, entries, "选择任务后，这里显示可自由框选和复制的任务明细。", 48)


def build_import_page(self):
    page = self.pages["import"]
    self.page_title(
        page, "导入文件",
        "每个文件生成独立的可编辑副本，可在任务处理页按文件名切换。",
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
        text="可只创建导入副本，也可同时生成任务。副本和工作文档分别显示。",
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
        text="左侧可切换工作文档和各导入副本，右侧显示当前文档的任务。",
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

    left = ttk.LabelFrame(self.tasks_split, text="文档编辑", padding=7)
    self.task_working_panel = left
    self.tasks_split.add(left, weight=3)
    viewbar = ttk.Frame(left)
    viewbar.pack(fill="x", pady=(0, 5))
    ttk.Label(viewbar, text="显示文档").pack(side="left", padx=(0, 6))
    self.working_view_var = tk.StringVar(value=MAIN_DOCUMENT_LABEL)
    self.working_view_box = ttk.Combobox(viewbar, textvariable=self.working_view_var, state="readonly")
    self.working_view_box.pack(side="left", fill="x", expand=True)
    self.working_view_box.bind("<<ComboboxSelected>>", lambda event: self.guard(lambda: on_working_view_selected(self)))
    self._working_view_ids = {MAIN_DOCUMENT_LABEL: None}
    self._working_copy_id = None
    self._working_copy_name = ""
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
    self.sync_conflict_button = ttk.Button(
        workbar, text="同步冲突 (0)", command=lambda: self.guard(lambda: show_sync_conflicts(self))
    )
    self.sync_conflict_button.pack(side="left", padx=(0, 5), pady=2)
    self.sync_conflict_button.state(["disabled"])
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
    ttk.Button(tools, text="一键整理", command=lambda: self.guard(lambda: organize_working_materials(self))).pack(side="left", padx=(5, 0))
    self.delete_copy_button = ttk.Button(tools, text="删除当前文档", command=lambda: self.guard(self.delete_current_copy))
    self.delete_copy_button.pack(side="left", padx=(5, 0))
    self.delete_copy_button.state(["disabled"])
    self.working_hint = ttk.Label(
        left, text="这里编辑软件生成的工作副本；原始文件只读归档，不在此处预览。",
        foreground="#64748B",
    )
    self.working_hint.pack(anchor="w", pady=(0, 5))
    area = ttk.Frame(left)
    area.pack(fill="both", expand=True)
    self.working_text = tk.Text(
        area, wrap="word", undo=True,
        padx=12, pady=10, relief="flat", bg="white", fg="#243247",
        insertwidth=2, spacing3=4,
    )
    configure_document_styles(self.working_text)
    wy = ttk.Scrollbar(area, orient="vertical", command=self.working_text.yview)
    wx = ttk.Scrollbar(area, orient="horizontal", command=self.working_text.xview)
    self.working_text.configure(yscrollcommand=wy.set, xscrollcommand=wx.set)
    wy.pack(side="right", fill="y")
    wx.pack(side="bottom", fill="x")
    self.working_text.pack(fill="both", expand=True)
    self.working_text.bind("<<Modified>>", self.working_document_modified)
    self.working_text.bind("<<Paste>>", self.working_paste_event)
    self.working_text.bind("<<Undo>>", lambda event: undo_working_text(self))
    _install_text_selection(self.working_text)
    self.working_text.bind("<Button-3>", lambda event: working_document_menu(self, event))
    self.working_text.tag_configure("task_marker", elide=True)
    self.working_text.tag_configure("image_markup", elide=True)
    self._working_images = {}
    self._working_image_links = {}
    self._working_preview_after_id = None
    self._working_task_edit_after_id = None
    self._working_task_refresh_pending = False
    self._working_synced_block = ""
    self._working_task_marks = {}
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
        detail_area, wrap="word",
        padx=10, pady=8, relief="flat", bg="white", fg="#243247",
        state="disabled", exportselection=False,
    )
    configure_task_detail_styles(self.task_detail)
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
        detail_box, wrap="word",
        padx=12, pady=10, relief="flat", bg="white", fg="#243247",
        state="disabled",
    )
    configure_task_detail_styles(self.progress_detail)
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
            if self._working_task_marks and self.working_text.get(TASK_START_MARK, TASK_END_MARK) != self._working_synced_block:
                if self._working_task_edit_after_id is not None:
                    self.after_cancel(self._working_task_edit_after_id)
                self._working_task_edit_after_id = self.after(350, lambda: self.guard(lambda: apply_working_task_edits(self)))
        self.working_text.edit_modified(False)
        if self._working_preview_after_id is None:
            self._working_preview_after_id = self.after_idle(lambda: refresh_working_view(self))


def undo_working_text(self):
    try:
        self.working_text.edit_undo()
    except tk.TclError:
        self.status.set("当前文档没有可撤销的编辑。")
    else:
        self.status.set("已撤销当前文档的编辑。")
    return "break"


def refresh_working_view(self):
    from PIL import Image, ImageTk, ImageOps

    self._working_preview_after_id = None
    text = self.working_text
    lines = text.get("1.0", "end-1c").split("\n")
    previews = {}
    links = {}
    base = self.s.document_folder(self.key).resolve() if self.key else None
    existing = {name: index for kind, name, index in text.dump("1.0", "end", image=True)}
    for index in reversed(list(existing.values())):
        text.delete(index)
    text.tag_remove("task_marker", "1.0", "end")
    text.tag_remove("image_markup", "1.0", "end")
    style_document_lines(text, lines)
    for line, value in enumerate(lines, 1):
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
    clear_working_task_marks(self)
    self._working_loading = True
    self.working_text.replace("1.0", "end-1c", content)
    self.working_text.edit_modified(False)
    self.working_text.edit_reset()
    self._working_loading = False
    self._working_dirty = False
    if self._working_preview_after_id is not None:
        self.after_cancel(self._working_preview_after_id)
    refresh_working_view(self)


def clear_working_task_marks(self):
    if self._working_task_edit_after_id is not None:
        self.after_cancel(self._working_task_edit_after_id)
        self._working_task_edit_after_id = None
    marks = [mark for start, end, _ in self._working_task_marks.values() for mark in (start, end)]
    self.working_text.mark_unset(TASK_START_MARK, TASK_END_MARK, *marks)
    self._working_synced_block = ""
    self._working_task_marks = {}
    self._working_task_refresh_pending = False


def sync_working_tasks(self, rows, force=False):
    if not self.key or self._working_saved_key != self.key or self._working_copy_id is not None:
        return
    if self._working_task_edit_after_id is not None:
        self._working_task_refresh_pending = True
        return
    widget = self.working_text
    content = widget.get("1.0", "end-1c")
    match = TASK_BLOCK.search(content)
    if not rows and not match and not self._working_synced_block:
        return
    status_lines = {}
    block = ""
    if rows:
        task_text = self.store.compose(self.key, rows=rows, status_lines=status_lines).partition("\n")[2].rstrip()
        block = f"{TASK_BLOCK_START}\n## 任务清单（自动同步）\n\n{task_text}\n{TASK_BLOCK_END}\n"
    separator = ""
    if match:
        if not force and match[0] == block == self._working_synced_block:
            return
        first_line = content.count("\n", 0, match.start()) + 1
        after_line = first_line + match[0].count("\n")
        start = f"{first_line}.0"
        end = f"{after_line}.0" if match[0].endswith("\n") else "end-1c"
    elif self._working_synced_block:
        start, end = widget.index(TASK_START_MARK), widget.index(TASK_END_MARK)
        if widget.compare(end, "<", start):
            end = start
        if block and not start.endswith(".0"):
            separator = "\n\n"
    else:
        start = end = widget.index("end-1c")
        separator = "\n\n" if content and not content.endswith("\n\n") else ""
    clear_working_task_marks(self)
    if widget.get(start, end) != separator + block:
        # Generated refreshes must never be undone as if the user removed task lines.
        undo_enabled = widget["undo"]
        widget.configure(undo=False)
        try:
            widget.replace(start, end, separator + block)
        finally:
            widget.configure(undo=undo_enabled)
    if not block:
        return
    first_line = int(start.split(".")[0]) + separator.count("\n")
    widget.mark_set(TASK_START_MARK, f"{first_line}.0")
    widget.mark_gravity(TASK_START_MARK, "right")
    after_line = first_line + block.count("\n")
    widget.mark_set(TASK_END_MARK, f"{after_line}.0")
    widget.mark_gravity(TASK_END_MARK, "left")
    for row in rows:
        line = first_line + status_lines[row.id] + 2
        left, right = f"reqdoc_status_start_{row.id}", f"reqdoc_status_end_{row.id}"
        widget.mark_set(left, f"{line}.0")
        widget.mark_gravity(left, "left")
        widget.mark_set(right, f"{line}.end")
        widget.mark_gravity(right, "right")
        self._working_task_marks[row.id] = (left, right, row.done)
    self._working_synced_block = block


def apply_working_task_edits(self):
    self._working_task_edit_after_id = None
    if not self.key or self._working_saved_key != self.key or not self._working_task_marks:
        return
    text = self.working_text
    if text.get(TASK_START_MARK, TASK_END_MARK) == self._working_synced_block:
        sync_working_tasks(self, self.store.tasks(self.key), force=True)
        return
    completion = {}
    removed = set()
    for rid, (start, end, was_done) in self._working_task_marks.items():
        value = text.get(start, end).strip()
        if not value:
            removed.add(rid)
            continue
        status = re.fullmatch(rf">\s*#{rid}\s*[·:：]\s*(完成|已完成|未完成)", value)
        label = status[1] if status else value
        if label in ("完成", "已完成", "未完成") and (label != "未完成") != was_done:
            completion[rid] = label != "未完成"
    if completion or removed:
        self.store.apply_document_task_edits(self.key, completion, removed)
        self.checked_ids.difference_update(removed)
        if self.rid in removed:
            self.load_editor(None)
        self.refresh_tasks()
        self.refresh_progress()
        self.store.snapshot(self.key)
        self.status.set("已同步工作文档中的任务变更。")
    elif self._working_task_refresh_pending:
        sync_working_tasks(self, self.store.tasks(self.key), force=True)


def flush_working_task_edits(self):
    if self._working_task_edit_after_id is not None:
        self.after_cancel(self._working_task_edit_after_id)
        apply_working_task_edits(self)
    elif self._working_task_marks and self.working_text.get(TASK_START_MARK, TASK_END_MARK) != self._working_synced_block:
        apply_working_task_edits(self)


def refresh_working_views(self):
    self._working_view_ids = {MAIN_DOCUMENT_LABEL: None}
    if self.key:
        with self.store.conn() as conn:
            sources = conn.execute(
                """SELECT id,name FROM studio_sources AS source WHERE doc=?
                   AND NOT EXISTS(SELECT 1 FROM studio_deleted_copies AS removed
                                  WHERE removed.doc=source.doc AND removed.id=source.id)
                   ORDER BY created DESC,id""", (self.key,)
            ).fetchall()
            preserved = conn.execute(
                """SELECT id,name FROM studio_import_copies AS copy WHERE doc=? AND id=?
                   AND NOT EXISTS(SELECT 1 FROM studio_deleted_copies AS removed
                                  WHERE removed.doc=copy.doc AND removed.id=copy.id)""",
                (self.key, LEGACY_IMPORT_COPY),
            ).fetchall()
            organized = conn.execute(
                "SELECT id,name FROM studio_import_copies WHERE doc=? AND id GLOB ? ORDER BY name DESC,id",
                (self.key, ORGANIZED_PREFIX + "*"),
            ).fetchall()
        for copy_id, name in sources + preserved + organized:
            label = f"整理 · {name}" if copy_id.startswith(ORGANIZED_PREFIX) else f"副本 · {name}"
            suffix = 2
            while label in self._working_view_ids:
                label = f"副本 · {name}（{suffix}）"
                suffix += 1
            self._working_view_ids[label] = copy_id
    self.working_view_box.configure(values=tuple(self._working_view_ids), state="readonly" if self.key else "disabled")
    label = next((label for label, copy_id in self._working_view_ids.items() if copy_id == self._working_copy_id),
                 MAIN_DOCUMENT_LABEL)
    self.working_view_var.set(label)


def on_working_view_selected(self):
    copy_id = self._working_view_ids[self.working_view_var.get()]
    if copy_id == self._working_copy_id:
        return
    previous = self._working_copy_id
    try:
        self.keep_draft()
        self._working_copy_id = copy_id
        self.load_working_document()
    except Exception:
        self._working_copy_id = previous
        refresh_working_views(self)
        raise


def load_working_document(self):
    label = "删除当前文档"
    if self._working_copy_id:
        label = "删除整理文档" if self._working_copy_id.startswith(ORGANIZED_PREFIX) else "删除历史记录"
    self.delete_copy_button.configure(text=label)
    self.delete_copy_button.state(["!disabled"] if self.key else ["disabled"])
    if not self.key:
        self._working_saved_key = None
        self._working_copy_id = None
        set_working_text(self, "")
        refresh_working_views(self)
        refresh_sync_conflict_button(self)
        return
    if self._working_copy_id is not None:
        self._working_copy_name, content = read_import_copy(self.store, self.key, self._working_copy_id)
        set_working_text(self, content)
        self._working_saved_key = self.key
        refresh_working_views(self)
        refresh_sync_conflict_button(self)
        hint = "整理结果，可直接编辑、保存和导出。" if self._working_copy_id.startswith(ORGANIZED_PREFIX) else "导入副本独立编辑，可从此副本转成任务。"
        self.working_hint.configure(text=hint)
        self.working_text.focus_set()
        return
    self._working_copy_name = ""
    tasks = self.store.tasks(self.key)
    fallback = self.store.compose(self.key, rows=tasks) if tasks else ""
    content = read_working_document(self.store, self.key, fallback)
    with self.store.conn() as conn:
        self._working_mode = conn.execute("SELECT mode FROM studio_working_docs WHERE doc=?", (self.key,)).fetchone()[0]
        if self._working_mode in ("import-table", "import-tasks"):
            separate_legacy_import(conn, self.key, content)
            self._working_mode = "manual"
            content = ""
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
    refresh_working_views(self)
    sync_working_tasks(self, tasks)
    self._working_dirty |= legacy_summary
    refresh_sync_conflict_button(self)
    self.working_hint.configure(text="改状态或删除编号行可同步任务，右键可新增。")
    self.working_text.focus_set()


def save_working_document(self, silent=False):
    if not self.key:
        if silent:
            return False
        raise ValueError("请先选择一份文档。")
    try:
        flush_working_task_edits(self)
        if self._working_synced_block and self.working_text.compare(TASK_START_MARK, "<=", TASK_END_MARK):
            content = self.working_text.get("1.0", TASK_START_MARK) + self.working_text.get(TASK_END_MARK, "end-1c")
        else:
            content = self.working_text.get("1.0", "end-1c")
        tasks_changed = False
        if self._working_copy_id is None:
            from reqdoc_task_conversion import MAIN_COPY_ID
            with self.s.connect() as conn:
                write_working_document(self.store, self.key, content, self._working_mode, connection=conn)
                tasks_changed = sync_import_tasks(conn, self.key, MAIN_COPY_ID, content)
        else:
            with self.s.connect() as conn:
                write_import_copy(conn, self.key, self._working_copy_id, self._working_copy_name, content)
                tasks_changed = sync_import_tasks(conn, self.key, self._working_copy_id, content)
    except Exception as error:
        self._working_dirty = True
        self._working_save_error = str(error)
        message = (
            f"保存失败：{error}\n\n"
            "当前编辑内容仍保留在窗口中。请先不要关闭或切换文档，修复问题后再次点击“保存文档”。"
        )
        self.status.set(message.replace("\n", " "))
        logging.exception("Working document save failed")
        if silent:
            raise RuntimeError(message) from error
        try:
            messagebox.showerror("保存失败", message, parent=self)
        except tk.TclError:
            pass
        return False
    previous_save_error = getattr(self, "_working_save_error", None)
    self._working_dirty = False
    self._working_save_error = None
    self._working_saved_key = self.key
    if tasks_changed:
        self.store.snapshot(self.key)
        self.refresh_tasks()
        self.refresh_progress()
    refresh_sync_conflict_button(self)
    if not silent:
        self.status.set("导入副本已保存。" if self._working_copy_id is not None else "工作文档已保存。")
    elif previous_save_error:
        self.status.set("自动保存已恢复，当前编辑内容已保存。")
    return True


def keep_draft(self):
    if self.key and self._working_saved_key == self.key and (self._working_dirty or self.working_text.edit_modified()):
        return self.save_working_document(silent=True)
    return False


def delete_current_copy(self):
    self.require_doc()
    copy_id = self._working_copy_id
    if copy_id is None:
        self.project_list.selection_set("d:" + self.key)
        self.delete_project_or_document()
        return
    name = self._working_copy_name
    history = not copy_id.startswith(ORGANIZED_PREFIX)
    if not messagebox.askyesno(
        "删除历史记录" if history else "删除整理文档",
        f"删除「{name}」及其编辑内容？\n该文档与任务的同步关联也会移除。\n\n删除后无法撤销。任务请在任务列表中单独管理。",
        parent=self, icon="warning",
    ):
        return
    if history:
        from reqdoc_library import delete_history_document
        delete_history_document(self.store, self.key, copy_id)
    else:
        delete_editable_copy(self.store, self.key, copy_id)
    self._working_copy_id = None
    self._working_copy_name = ""
    self._working_dirty = False
    self.load_working_document()
    self.refresh_tree()
    self.status.set(f"已删除文档「{name}」。")


def autosave(self):
    if getattr(self, "_closing", False):
        return
    try:
        self.keep_draft()
        if hasattr(self, "persist_notes"):
            self.persist_notes()
    except Exception as error:
        logging.exception("Working document autosave failed")
        self._autosave_error = str(error)
        if getattr(self, "_working_dirty", False) or getattr(self, "notes_dirty", False):
            self.status.set(
                f"自动保存失败：{error}。当前编辑仍保留在窗口中，程序会稍后重试；请勿在保存成功前关闭软件。"
            )
    else:
        if getattr(self, "_autosave_error", None):
            self._autosave_error = None
            self.status.set("自动保存已恢复。")
    try:
        self._autosave_after_id = self.after(2000, self.autosave)
    except tk.TclError:
        return


def load_doc(self, key):
    self.keep_draft()
    if key == self.key:
        self.refresh_task_selectors(key)
        refresh_working_views(self)
        self.refresh_tasks()
        return
    self.key = key
    self._working_copy_id = None
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
    self.load_working_document()
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


def _task_dialog(self, rid=None, after_id=None, initial_content=""):
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
    body.insert("1.0", existing.content if existing else initial_content)
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
    flush_working_task_edits(self)
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
    flush_working_task_edits(self)
    from reqdoc_source_table import tables_in, replace_table

    text = self.working_text
    original = text.get("1.0", "end-1c")
    tables = tables_in(original)
    if self._working_synced_block:
        tables = [
            table for table in tables
            if text.compare(f"{table.end_line + 1}.0", "<=", TASK_START_MARK)
            or text.compare(f"{table.start_line + 1}.0", ">=", TASK_END_MARK)
        ]
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
        lines = original.splitlines(keepends=True)
        prefix_length = sum(map(len, lines[:table.start_line]))
        suffix_length = sum(map(len, lines[table.end_line:]))
        end = f"{table.end_line + 1}.0" if suffix_length else "end-1c"
        text.edit_separator()
        text.replace(f"{table.start_line + 1}.0", end, updated[prefix_length:len(updated)-suffix_length])
        text.edit_separator()
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
    self.working_text.edit_separator()
    self.working_text.insert(index, "\n" + "\n".join(links) + "\n")
    self.working_text.edit_separator()
    self._working_dirty = True
    self.working_text.focus_set()
    self.status.set("已将剪贴板图片插入工作文档。")
    return True


def working_paste_event(self, event=None):
    if not self.key:
        return None
    return "break" if self.guard(lambda: self.paste_working_image(silent=True)) else None


def working_export_content(self):
    flush_working_task_edits(self)
    sync_working_tasks(self, self.store.tasks(self.key))
    content = self.working_text.get("1.0", "end-1c")
    return re.sub(rf"^(?:{re.escape(TASK_BLOCK_START)}|{re.escape(TASK_BLOCK_END)})(?:\n|$)", "", content, flags=re.MULTILINE)


def export_working_document(self, kind):
    self.require_doc()
    name = Path(self._working_copy_name).stem if self._working_copy_id is not None else None
    return self.export(kind, content=working_export_content(self), name=name)


def organize_working_materials(self):
    from reqdoc_organize_dialog import open_organize_dialog

    self.require_doc()
    key = self.key
    with self.store.conn() as conn:
        main_row = conn.execute("SELECT content FROM studio_working_docs WHERE doc=?", (key,)).fetchone()
    sources = []
    for label, copy_id in self._working_view_ids.items():
        if copy_id and copy_id.startswith(ORGANIZED_PREFIX):
            continue
        if copy_id == self._working_copy_id:
            if self._working_synced_block:
                text = self.working_text.get("1.0", TASK_START_MARK) + self.working_text.get(TASK_END_MARK, "end-1c")
            else:
                text = self.working_text.get("1.0", "end-1c")
        elif copy_id is None:
            text = main_row[0] if main_row else ""
        else:
            _, text = read_import_copy(self.store, key, copy_id)
        if text.strip():
            sources.append((label if copy_id else "工作文档", text))
    if not sources:
        raise ValueError("请先输入正文，或导入要整理的文件。")

    def save_result(content):
        if self.key != key:
            raise ValueError("当前文档已切换，请重新打开整理窗口。")
        if not content.strip():
            raise ValueError("整理结果为空。")
        name = datetime.now().strftime("整理结果 %Y-%m-%d %H-%M-%S")
        copy_id = ORGANIZED_PREFIX + uuid.uuid4().hex
        with self.store.conn() as conn:
            write_import_copy(conn, key, copy_id, name, content)
        if not self._working_dirty and not self.working_text.edit_modified():
            self._working_copy_id = copy_id
            self.load_working_document()
        else:
            refresh_working_views(self)
        self.status.set(f"已另存为「{name}」，可在‘显示文档’下拉框打开。")
        return True

    return open_organize_dialog(self, sources, save_result)


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
    if self._working_synced_block:
        content = self.working_text.get("1.0", TASK_START_MARK) + self.working_text.get(TASK_END_MARK, "end-1c")
    else:
        content = self.working_text.get("1.0", "end-1c")
    if not content.strip():
        raise ValueError("工作文档为空，请先输入或导入内容。")
    from reqdoc_task_conversion import open_conversion_dialog
    return open_conversion_dialog(self, content)


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
    win.title("导入文件：创建独立副本")
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
        root, text="为每个文件创建可编辑副本",
        variable=mode_var, value="table",
    ).pack(anchor="w")
    ttk.Radiobutton(
        root, text="创建独立副本并生成任务",
        variable=mode_var, value="tasks",
    ).pack(anchor="w")
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
                raise ValueError("没有识别到可转换的内容。可选择只创建导入副本。")
            if estimated > 500:
                raise ValueError("一次导入最多生成 500 条任务，请先缩小文件范围。")
        all_warnings = []
        self.keep_draft()
        with ExitStack() as rollback:
            with self.s.connect() as connection:
                archived_docs = []
                for path, _ in prepared:
                    notes, archived_text, copy_id = self.store.archive_source(
                        self.key, path, connection=connection, rollback=rollback, include_id=True
                    )
                    all_warnings.extend(f"{path.name}：{note}" for note in notes)
                    write_import_copy(connection, self.key, copy_id, path.name, archived_text)
                    archived_docs.append((copy_id, archived_text))
                task_batches = [(copy_id, document_to_task_items(text)) for copy_id, text in archived_docs] if as_tasks else []
                task_count = sum(len(items) for _, items in task_batches)
                if as_tasks and not task_count:
                    raise ValueError("没有识别到可转换的内容。可选择只创建导入副本。")
                if task_count > 500:
                    raise ValueError("一次导入最多生成 500 条任务，请分批导入。")
                for copy_id, items in task_batches:
                    add_import_tasks(self.s, connection, self.key, copy_id, list(enumerate(items)))
            rollback.pop_all()
        self._working_copy_id = archived_docs[0][0]
        self.load_working_document()
        win.destroy()
        if task_count:
            self.store.snapshot(self.key)
        self.refresh_tasks()
        self.refresh_progress()
        self.refresh_home()
        self.refresh_import_page()
        self.refresh_export_page()
        self.show_page("tasks")
        self.status.set(
            f"已创建 {len(archived_docs)} 份独立导入副本。"
            + (f"已生成 {task_count} 条任务。" if as_tasks else "")
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
    entries = []
    if group:
        for project, name, key, row in group["items"]:
            entries.append((
                row.title,
                f"所属项目：{project}    所属文档：{name}\n"
                f"章节：{row.section or '未分类'}    状态：{'已完成' if row.done else '未完成'}    任务编号：{row.id}",
                row.content.strip(),
            ))
            self.progress_task_doc_map[row.id] = key
    render_task_details(self.progress_detail, entries, "当前分组没有任务。", 56)


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
    self.page_title(page, "设置与数据", "管理文件库、整理规则备份和软件更新。")
    box = ttk.LabelFrame(page, text="文件库保存路径", padding=12)
    box.pack(fill="x")
    ttk.Label(box, text=str(self.store.settings.library_root), wraplength=600).pack(anchor="w", pady=(0, 8))
    if self.store.settings.portable:
        ttk.Label(box, text="资料库随软件文件夹一起移动。", foreground="#64748B").pack(anchor="w")
        ttk.Button(box, text="打开资料库文件夹", command=lambda: self.open_path(self.store.settings.library_root)).pack(anchor="w")
    else:
        ttk.Button(box, text="更改路径", command=lambda: self.guard(lambda: change_library_folder(self))).pack(anchor="w")

    rules_box = ttk.LabelFrame(page, text="整理规则备份", padding=12)
    rules_box.pack(fill="x", pady=(10, 0))
    ttk.Label(
        rules_box,
        text="完整备份包含数据库、文档、图片和本地整理规则。可以从备份包单独恢复整理规则；恢复前会自动备份当前数据。",
        foreground="#52647B", wraplength=900, justify="left",
    ).pack(anchor="w", pady=(0, 8))
    ttk.Button(
        rules_box, text="从备份恢复整理规则",
        command=lambda: self.guard(lambda: restore_organization_rules(self)),
    ).pack(anchor="w")

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




def restore_organization_rules(self):
    archive = filedialog.askopenfilename(
        parent=self, title="选择包含整理规则的 ReqDoc 备份包",
        filetypes=[("ReqDoc 备份 ZIP", "*.zip")],
    )
    if not archive:
        return False
    import zipfile
    from reqdoc_organize import OrganizeOptions
    member = "reqdoc-data/home/organize-rules.json"
    with zipfile.ZipFile(archive) as package:
        try:
            info = package.getinfo(member)
        except KeyError as error:
            raise ValueError("该备份包没有整理规则。请使用包含整理规则的新版完整备份。") from error
        if info.file_size > 1024 * 1024:
            raise ValueError("整理规则文件超过 1 MB，已拒绝恢复。")
        payload = package.read(member)
    data = json.loads(payload.decode("utf-8"))
    options = OrganizeOptions(**data)
    options.validate()
    # Save current edits and keep a full pre-restore recovery point before changing the active rules.
    self.keep_draft()
    if hasattr(self, "persist_notes"):
        self.persist_notes()
    safety_backup = self.store.backup()
    from reqdoc_studio import write_atomic
    target = Path(self.store.settings.home) / "organize-rules.json"
    write_atomic(target, payload)
    self.status.set(f"整理规则已恢复；恢复前完整备份：{safety_backup}")
    messagebox.showinfo(
        "整理规则已恢复",
        f"已恢复整理规则。恢复前自动备份了当前数据库、文档和规则：\n{safety_backup}",
        parent=self,
    )
    return True


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
2. 导入文件：选择 Word、Markdown 或 TXT。每个文件都会生成独立副本，可同时生成任务。在“显示文档”下拉框按文件名切换。

【工作文档与源文件】
工作文档和各导入副本分别编辑和保存。Markdown 表格可通过“编辑表格”双击单元格修改。复制图片后，将光标放在正文中，按 Ctrl+V 或点击“粘贴图片”。图片描述可留空，插入后会显示缩略图。“导出文档”预览或导出当前显示的内容。

【任务处理】
左右两侧的任务状态会同步。在工作文档中把编号行的状态改成“完成”或“未完成”，右侧会更新。删除编号行会直接删除任务。在文档中右键可新增任务；在某条任务上右键时，新任务插在它后面。右侧下方的任务明细可框选和复制。

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
        from reqdoc_library import recover_history_deletions
        recover_history_deletions(self)

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
        "delete_current_copy": delete_current_copy,
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
    from reqdoc_library import install_library
    install_library(StudioClass)
