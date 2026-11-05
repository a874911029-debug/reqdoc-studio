"""Document organization preview and locally persisted rules."""
from __future__ import annotations

from dataclasses import asdict
import json
import logging
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from reqdoc_organize import OrganizeOptions, OrganizationCancelled, organize_with_report, parse_rules


MODES = {"按内容主题归类": "topic", "按原文章节归并": "structure", "按资料来源保序": "source"}


def load_options(path):
    try:
        data = json.loads(Path(path).read_text("utf-8"))
        options = OrganizeOptions(**data)
        options.validate()
        return options
    except FileNotFoundError:
        return OrganizeOptions()
    except (OSError, ValueError, TypeError):
        logging.exception("Invalid organization preferences")
        return OrganizeOptions()


def open_organize_dialog(app, sources, on_save):
    preferences = Path(app.store.settings.home) / "organize-rules.json"
    initial = load_options(preferences)
    win = tk.Toplevel(app)
    win.title("一键整理资料")
    app.sized_dialog(win, 1000, 760)
    win.transient(app)
    win.grab_set()
    panel = ttk.Frame(win, padding=12)
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text="选择资料后自动分块和归类。预览可以直接修改，整理说明中可查看依据和待核对内容。",
              wraplength=900).pack(anchor="w")
    picker = ttk.Frame(panel)
    picker.pack(fill="x", pady=(6, 6))
    names = tk.Listbox(picker, selectmode="extended", exportselection=False, height=min(5, len(sources)))
    source_scroll = ttk.Scrollbar(picker, orient="vertical", command=names.yview)
    names.configure(yscrollcommand=source_scroll.set)
    source_scroll.pack(side="right", fill="y")
    names.pack(side="left", fill="x", expand=True)
    for label, _ in sources:
        names.insert("end", label)
    names.selection_set(0, "end")
    select_tools = ttk.Frame(panel)
    select_tools.pack(fill="x", pady=(0, 6))
    mode = tk.StringVar(value=next(label for label, value in MODES.items() if value == initial.mode))
    mode_box = ttk.Combobox(select_tools, state="readonly", values=tuple(MODES), textvariable=mode, width=19)
    mode_box.pack(side="left", padx=(0, 8))
    duplicates = tk.BooleanVar(value=initial.merge_duplicates)
    similar = tk.BooleanVar(value=initial.suggest_similar)
    ttk.Checkbutton(select_tools, text="合并重复内容", variable=duplicates).pack(side="left", padx=(0, 8))
    ttk.Checkbutton(select_tools, text="提示相近内容和数值差异", variable=similar).pack(side="left")
    rule_text = tk.StringVar(value=initial.rules_text)
    tabs = ttk.Notebook(panel)
    footer = ttk.Frame(panel)
    footer.pack(side="bottom", fill="x", pady=(8, 0))
    progress_text = tk.StringVar(value="准备整理…")
    ttk.Label(footer, textvariable=progress_text).pack(side="top", anchor="w", pady=(0, 5))
    progress = ttk.Progressbar(footer, mode="indeterminate")
    progress.pack(side="top", fill="x", pady=(0, 5))
    tabs.pack(fill="both", expand=True)
    document_page = ttk.Frame(tabs)
    report_page = ttk.Frame(tabs)
    tabs.add(document_page, text="整理文档")
    tabs.add(report_page, text="整理说明 / 待核对")

    def text_area(parent, editable):
        text = tk.Text(parent, wrap="word", padx=10, pady=8, undo=editable)
        scroll = ttk.Scrollbar(parent, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        if not editable:
            text.configure(state="disabled")
        return text

    preview = text_area(document_page, True)
    report = text_area(report_page, False)
    work_queue = queue.Queue()
    cancelled = threading.Event()
    state = {"busy": False, "closed": False, "poll": None, "stale": False}

    def invalidate(*_):
        state["stale"] = True
        save_button.state(["disabled"])
        if not state["busy"]:
            progress_text.set("资料或规则已更改，请重新整理。")

    def dispose():
        if state["closed"]:
            return
        state["closed"] = True
        cancelled.set()
        try:
            if state["poll"] is not None:
                win.after_cancel(state["poll"])
            progress.stop()
        except tk.TclError:
            pass

    def close():
        dispose()
        win.destroy()

    def edit_rules():
        editor = tk.Toplevel(win)
        editor.title("整理规则")
        app.sized_dialog(editor, 760, 560)
        editor.transient(win)
        editor.grab_set()
        content = ttk.Frame(editor, padding=12)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="每行一个主题，逗号分隔关键词或同义表达。主题的排列顺序决定结果顺序。\n"
                  "留空时根据原文章节和正文词组相似度整理。并列命中的内容会保留待核对。",
                  wraplength=690).pack(anchor="w")
        ttk.Label(content, text="填写示例\n灯光 = 灯光, 照明, 光照\n材质 = 材质, 贴图, 纹理",
                  foreground="#64748B").pack(anchor="w", pady=8)
        actions = ttk.Frame(content)
        actions.pack(side="bottom", fill="x", pady=(8, 0))
        text = text_area(content, True)
        text.insert("1.0", rule_text.get())

        def dismiss():
            editor.destroy()
            win.grab_set()

        def apply_rules():
            value = text.get("1.0", "end-1c")
            parse_rules(value)
            rule_text.set(value)
            dismiss()

        ttk.Button(actions, text="取消", command=dismiss).pack(side="right", padx=(6, 0))
        ttk.Button(actions, text="应用规则", command=lambda: app.guard(apply_rules)).pack(side="right")
        editor.protocol("WM_DELETE_WINDOW", dismiss)

    ttk.Button(select_tools, text="整理规则", command=edit_rules).pack(side="right")

    def options():
        return OrganizeOptions(MODES[mode.get()], duplicates.get(), similar.get(), rule_text.get())

    def poll():
        state["poll"] = None
        if state["closed"]:
            return
        try:
            result, error, used_options = work_queue.get_nowait()
        except queue.Empty:
            if not state["closed"]:
                state["poll"] = win.after(60, poll)
            return
        state["busy"] = False
        run_button.state(["!disabled"])
        progress.stop()
        if error is not None:
            if not isinstance(error, OrganizationCancelled):
                logging.error("Organization failed", exc_info=(type(error), error, error.__traceback__))
                messagebox.showerror("整理未完成", str(error), parent=win)
            progress_text.set("整理未完成，可调整所选资料或规则后重试。")
            return
        preview.delete("1.0", "end")
        preview.insert("1.0", result.content)
        preview.edit_reset()
        preview.edit_modified(False)
        report.configure(state="normal")
        report.delete("1.0", "end")
        report.insert("1.0", result.report)
        report.configure(state="disabled")
        counts = result.stats
        progress_text.set(f"保留 {counts['output_blocks']} 块，归为 {counts['topics']} 组，"
                          f"合并重复 {counts['duplicates']} 块，待归类 {counts['unclassified']} 块。")
        if state["stale"]:
            progress_text.set("资料或规则已更改，请重新整理后保存。")
        else:
            save_button.state(["!disabled"])
            from reqdoc_studio import write_atomic
            try:
                write_atomic(preferences, json.dumps(asdict(used_options), ensure_ascii=False, indent=2).encode("utf-8"))
            except OSError:
                logging.exception("Could not persist organization preferences")
                progress_text.set("整理已完成，本次规则未能保存到本机。")

    def refresh():
        if state["busy"]:
            return
        if preview.edit_modified() and not messagebox.askyesno("重新整理", "重新整理将替换当前预览中的手动修改，是否继续？", parent=win):
            return
        selected = [sources[index] for index in names.curselection()]
        if not selected:
            raise ValueError("请至少选择一份资料。")
        selected_options = options()
        selected_options.validate()
        state["busy"] = True
        state["stale"] = False
        cancelled.clear()
        run_button.state(["disabled"])
        save_button.state(["disabled"])
        progress_text.set("正在分块、归类和检查重复内容…")
        progress.start(15)

        def work():
            try:
                result = organize_with_report(selected, selected_options, cancel=cancelled.is_set)
                work_queue.put((result, None, selected_options))
            except Exception as error:
                work_queue.put((None, error, selected_options))

        threading.Thread(target=work, name="reqdoc-organize", daemon=True).start()
        state["poll"] = win.after(60, poll)

    def save():
        if state["busy"] or state["stale"]:
            raise ValueError("请等待本次整理完成，或重新整理更改后的资料。")
        if not on_save(preview.get("1.0", "end-1c")):
            return
        close()

    run_button = ttk.Button(footer, text="重新整理所选资料", command=lambda: app.guard(refresh))
    run_button.pack(side="left")
    ttk.Button(footer, text="取消", command=close).pack(side="right", padx=(6, 0))
    save_button = ttk.Button(footer, text="另存整理文档", style="Accent.TButton", command=lambda: app.guard(save))
    save_button.pack(side="right")
    save_button.state(["disabled"])
    names.bind("<<ListboxSelect>>", invalidate)
    mode.trace_add("write", invalidate)
    duplicates.trace_add("write", invalidate)
    similar.trace_add("write", invalidate)
    rule_text.trace_add("write", invalidate)
    win.protocol("WM_DELETE_WINDOW", close)
    win.bind("<Destroy>", lambda event: dispose() if event.widget == win else None, add="+")
    app.guard(refresh)
    return win
