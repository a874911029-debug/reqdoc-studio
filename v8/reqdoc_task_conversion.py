"""Task preview that shares organization topics and preserves source links."""
from __future__ import annotations
from collections import defaultdict
import json
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from reqdoc_organize import FENCE, OrganizeOptions, organize_with_report, split_material
from reqdoc_organize_dialog import load_options

MAIN_COPY_ID = "working-main"
MODES = {"自动识别（含表格）": "automatic", "按整理主题逐条": "topics", "按章节汇总": "sections"}
SOURCE_LINE = re.compile(r"^> 条目 \d+ · 来源：")


def strip_task_block(content):
    output = []
    fence = None
    inside = False
    for line in content.splitlines():
        marker = FENCE.match(line)
        if fence:
            if not inside:
                output.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
        if line == "<!-- reqdoc:tasks -->" and not fence:
            inside = True
        elif line == "<!-- /reqdoc:tasks -->" and not fence:
            inside = False
        elif not inside:
            output.append(line)
    return "\n".join(output).strip()


def organized_items(content):
    topic = "未分类"
    body = []
    rows = []
    active = False
    fence = None

    def append():
        text = "\n".join(body).strip()
        if text:
            rows.extend(_block_items(topic, text))

    for line in content.splitlines():
        marker = FENCE.match(line)
        if fence:
            body.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
        if line.startswith("## "):
            append()
            body = []
            topic = line[3:].strip()
            active = True
        elif SOURCE_LINE.match(line) and not fence:
            append()
            body = []
            active = True
        elif active:
            body.append(line)
    append()
    return rows


def _block_items(topic, text):
    from reqdoc_working_document import document_to_task_items
    if re.search(r"^\s*\|.*\|", text, re.MULTILINE):
        rows = document_to_task_items(text)
        for row in rows:
            row["section"] = topic
        if rows:
            return rows
    first = next((line.strip() for line in text.splitlines() if line.strip()), "任务")
    first = re.sub(r"^(?:[-*+]\s+(?:\[[ xX]\]\s*)?|\d+[.、)]\s*)", "", first)
    if first.startswith(("```", "~~~")):
        first = topic + " · 代码说明"
    return [{"title": first[:80] or topic, "section": topic, "content": text}]


def conversion_items(content, settings=None, *, cancel=None):
    settings = settings or {"mode": "automatic", "rules_text": ""}
    mode = settings.get("mode", "automatic")
    if mode not in MODES.values():
        raise ValueError("任务识别方式无效。")
    content = strip_task_block(content)
    if not content:
        return []
    is_organized = settings.get("organized", False) and content.startswith("# 整理结果") and any(SOURCE_LINE.match(line) for line in content.splitlines())
    if is_organized:
        rows = organized_items(content)
    elif mode == "topics":
        result = organize_with_report([("当前文档", content)], OrganizeOptions(rules_text=settings.get("rules_text", "")), cancel=cancel)
        rows = [item for block in result.blocks if block.text for item in _block_items(block.topic, block.text)]
    elif mode == "sections":
        rows = [{"title": " / ".join(block.headings) or "未分类", "section": " / ".join(block.headings) or "未分类", "content": block.text}
                for block in split_material("当前文档", content) if block.text]
    else:
        from reqdoc_working_document import document_to_task_items
        return document_to_task_items(content)
    if mode == "sections":
        groups = defaultdict(list)
        for row in rows:
            groups[row["section"]].append(row["content"])
        rows = [{"title": section.split(" / ")[-1][:80], "section": section, "content": "\n\n".join(parts)} for section, parts in groups.items()]
    return rows


def conversion_settings(conn, key, copy_id):
    row = conn.execute("SELECT settings FROM studio_conversion_modes WHERE doc=? AND copy_id=?", (key, copy_id)).fetchone()
    return json.loads(row[0]) if row else {"mode": "automatic", "rules_text": "", "organized": copy_id.startswith("organized-")}


def open_conversion_dialog(app, content):
    from reqdoc_working_document import import_task_links, match_import_task_links, save_import_task_link, link_existing_copy_tasks
    key = app.key
    copy_id = app._working_copy_id or MAIN_COPY_ID
    with app.store.conn() as conn:
        initial = conversion_settings(conn, key, copy_id)
    rules = load_options(app.store.settings.home / "organize-rules.json").rules_text
    win = tk.Toplevel(app)
    win.title("转换为任务 · 预览确认")
    app.sized_dialog(win, 1100, 760)
    win.transient(app)
    win.grab_set()
    root = ttk.Frame(win, padding=12)
    root.pack(fill="both", expand=True)
    ttk.Label(root, text="整理文档沿用主题和条目；表格按行识别。选择条目后可修改标题、分组和正文。", wraplength=1000).pack(anchor="w")
    ttk.Label(root, text="‘按整理主题逐条’沿用一键整理中保存的关键词规则。", foreground="#64748B").pack(anchor="w", pady=(3, 0))
    mode = tk.StringVar(value=next(label for label, value in MODES.items() if value == initial["mode"]))
    box = ttk.Combobox(root, state="readonly", textvariable=mode, values=tuple(MODES), width=25)
    box.pack(anchor="w", pady=6)
    footer = ttk.Frame(root)
    footer.pack(side="bottom", fill="x", pady=(8, 0))
    status = tk.StringVar(value="正在识别任务…")
    ttk.Label(footer, textvariable=status).pack(side="top", anchor="w", pady=(0, 5))
    panes = ttk.Panedwindow(root, orient="horizontal")
    panes.pack(fill="both", expand=True)
    left = ttk.Frame(panes)
    detail = ttk.Frame(panes, padding=(10, 0))
    panes.add(left, weight=3)
    panes.add(detail, weight=2)
    tree = ttk.Treeview(left, columns=("chosen", "title", "section"), show="headings", selectmode="browse")
    for column, label, width in [("chosen", "生成", 65), ("title", "任务标题", 260), ("section", "主题 / 分组", 180)]:
        tree.heading(column, text=label)
        tree.column(column, width=width, stretch=column != "chosen")
    tree.pack(fill="both", expand=True)
    ttk.Label(detail, text="任务标题").pack(anchor="w")
    title = ttk.Entry(detail)
    title.pack(fill="x", pady=(0, 6))
    ttk.Label(detail, text="主题 / 分组").pack(anchor="w")
    section = ttk.Entry(detail)
    section.pack(fill="x", pady=(0, 6))
    ttk.Label(detail, text="任务正文").pack(anchor="w")
    body = tk.Text(detail, wrap="word", undo=True)
    body.pack(fill="both", expand=True)
    state = {"rows": [], "originals": [], "chosen": set(), "matched": {}, "current": None,
             "busy": False, "closed": False, "poll": None, "settings": initial, "edited": False}
    results = queue.Queue()
    cancelled = threading.Event()

    def render():
        tree.delete(*tree.get_children())
        for index, row in enumerate(state["rows"]):
            mark = "已关联" if index in state["matched"] else "☑" if index in state["chosen"] else "☐"
            tree.insert("", "end", iid=str(index), values=(mark, row["title"], row["section"]))
        status.set(f"识别 {len(state['rows'])} 条，已关联 {len(state['matched'])} 条，选中生成 {len(state['chosen'])} 条。")

    def apply():
        index = state["current"]
        if index is None or index in state["matched"]:
            return
        if not title.get().strip():
            raise ValueError("任务标题不能为空。")
        row = {"title": title.get().strip()[:80], "section": section.get().strip() or "未分类", "content": body.get("1.0", "end-1c")}
        state["edited"] |= row != state["rows"][index]
        state["rows"][index] = row
        tree.item(str(index), values=("☑" if index in state["chosen"] else "☐", row["title"], row["section"]))

    def select(event=None):
        chosen = tree.selection()
        if not chosen:
            return
        index = int(chosen[0])
        if state["current"] is not None and state["current"] != index:
            try:
                apply()
            except ValueError as error:
                tree.selection_set(str(state["current"]))
                messagebox.showerror("任务内容无效", str(error), parent=win)
                return
        state["current"] = index
        row = state["rows"][index]
        for widget in (title, section, body):
            widget.configure(state="normal")
        for widget, value in ((title, row["title"]), (section, row["section"])):
            widget.delete(0, "end")
            widget.insert(0, value)
        body.delete("1.0", "end")
        body.insert("1.0", row["content"])
        editable = index not in state["matched"]
        for widget in (title, section, body):
            widget.configure(state="normal" if editable else "disabled")
        apply_button.state(["!disabled"] if editable else ["disabled"])

    def toggle(event):
        iid = tree.identify_row(event.y)
        if iid and tree.identify_column(event.x) == "#1":
            index = int(iid)
            if index not in state["matched"]:
                state["chosen"].symmetric_difference_update({index})
                row = state["rows"][index]
                tree.item(iid, values=("☑" if index in state["chosen"] else "☐", row["title"], row["section"]))
                status.set(f"选中生成 {len(state['chosen'])} 条。")

    def dispose():
        if state["closed"]:
            return
        state["closed"] = True
        cancelled.set()
        if state["poll"] is not None:
            try:
                win.after_cancel(state["poll"])
            except tk.TclError:
                pass

    def close():
        dispose()
        win.destroy()

    def poll():
        state["poll"] = None
        if state["closed"]:
            return
        try:
            rows, error, settings = results.get_nowait()
        except queue.Empty:
            state["poll"] = win.after(60, poll)
            return
        state["busy"] = False
        refresh_button.state(["!disabled"])
        if error:
            messagebox.showerror("识别未完成", str(error), parent=win)
            return
        with app.store.conn() as conn:
            previous = conversion_settings(conn, key, copy_id)
            if previous == settings and settings["mode"] == "automatic" and not settings["organized"]:
                link_existing_copy_tasks(conn, key, copy_id, rows)
            links = import_task_links(conn, key, copy_id) if previous == settings else []
        state["rows"] = [dict(row) for row in rows]
        state["originals"] = rows
        state["settings"] = settings
        state["matched"] = match_import_task_links(links, rows)
        state["chosen"] = set(range(len(rows))) - state["matched"].keys()
        state["current"] = None
        render()
        commit_button.state(["!disabled"])
        if rows:
            tree.selection_set("0")
            select()

    def refresh():
        if state["busy"]:
            return
        if state["rows"]:
            apply()
        if state["edited"] and not messagebox.askyesno("重新识别", "重新识别会替换本次预览的编辑内容，是否继续？", parent=win):
            return
        settings = {"mode": MODES[mode.get()], "rules_text": rules if MODES[mode.get()] == "topics" else "",
                    "organized": copy_id.startswith("organized-")}
        state["edited"] = False
        state["busy"] = True
        refresh_button.state(["disabled"])
        commit_button.state(["disabled"])
        status.set("正在识别任务…")

        def work():
            try:
                rows = conversion_items(content, settings, cancel=cancelled.is_set)
                if len(rows) > 500:
                    raise ValueError("一次最多识别 500 条任务，请缩小当前文档范围。")
                results.put((rows, None, settings))
            except Exception as error:
                results.put(([], error, settings))
        threading.Thread(target=work, name="reqdoc-task-preview", daemon=True).start()
        state["poll"] = win.after(60, poll)

    def commit():
        if state["busy"] or app.key != key or (app._working_copy_id or MAIN_COPY_ID) != copy_id:
            raise ValueError("当前文档已变化，请重新打开任务预览。")
        if MODES[mode.get()] != state["settings"]["mode"]:
            raise ValueError("识别方式已更改，请先重新识别。")
        apply()
        positions = sorted(state["chosen"])
        if not positions:
            raise ValueError("请勾选要生成的新任务。")
        with app.s.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM studio_deleted_copies WHERE doc=? AND id=?", (key, copy_id)).fetchone():
                raise ValueError("当前文档已删除，请关闭预览后重新选择。")
            current = conversion_settings(conn, key, copy_id)
            if current != state["settings"]:
                conn.execute("DELETE FROM studio_import_task_links WHERE doc=? AND copy_id=?", (key, copy_id))
            matched = match_import_task_links(import_task_links(conn, key, copy_id), state["originals"])
            positions = [position for position in positions if position not in matched]
            rows = [state["rows"][position] for position in positions]
            ids = app.s.commit_task_rows(key, rows, connection=conn)
            for ident, position in zip(ids, positions):
                save_import_task_link(conn, key, copy_id, ident, position, state["originals"][position])
            conn.execute("""INSERT INTO studio_conversion_modes(doc,copy_id,settings) VALUES(?,?,?)
                            ON CONFLICT(doc,copy_id) DO UPDATE SET settings=excluded.settings""",
                         (key, copy_id, json.dumps(state["settings"], ensure_ascii=False)))
        close()
        app.store.snapshot(key)
        app.refresh_tasks()
        app.refresh_progress()
        app.refresh_home()
        app.status.set(f"已按预览生成 {len(ids)} 条任务。")

    def choose_all(value):
        apply()
        state["chosen"] = set(range(len(state["rows"]))) - state["matched"].keys() if value else set()
        render()

    tree.bind("<<TreeviewSelect>>", select)
    tree.bind("<Button-1>", toggle, add="+")
    apply_button = ttk.Button(detail, text="应用修改", command=lambda: app.guard(apply))
    apply_button.pack(anchor="e", pady=(6, 0))
    refresh_button = ttk.Button(footer, text="重新识别", command=lambda: app.guard(refresh))
    refresh_button.pack(side="left")
    ttk.Button(footer, text="全选新条目", command=lambda: app.guard(lambda: choose_all(True))).pack(side="left", padx=6)
    ttk.Button(footer, text="清空选择", command=lambda: app.guard(lambda: choose_all(False))).pack(side="left")
    ttk.Button(footer, text="取消", command=close).pack(side="right", padx=(6, 0))
    commit_button = ttk.Button(footer, text="确认生成任务", command=lambda: app.guard(commit))
    commit_button.pack(side="right")
    commit_button.state(["disabled"])
    box.bind("<<ComboboxSelected>>", lambda event: commit_button.state(["disabled"]))
    win.protocol("WM_DELETE_WINDOW", close)
    win.bind("<Destroy>", lambda event: dispose() if event.widget == win else None, add="+")
    refresh()
    return win
