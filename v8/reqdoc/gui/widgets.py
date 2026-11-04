from __future__ import annotations
from pathlib import Path
import re
import tkinter as tk
from tkinter import ttk

try:
    from PIL import Image, ImageTk
except Exception:
    Image = ImageTk = None


_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_HEADING = re.compile(r"^(#{1,6})[ \t]+\S")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_STATUS = re.compile(r"^>\s*#\d+\s*[·:：]\s*(?:已完成|未完成|完成)\s*$")
_HEADING_SIZES = (19, 16, 13, 12, 11, 11)
_HEADING_TAGS = tuple(f"document_heading_{level}" for level in range(1, 7))
_METADATA_TAG = "document_metadata"
DOCUMENT_FONT = "Microsoft YaHei"


def configure_document_styles(text):
    text.configure(font=(DOCUMENT_FONT, 11))
    for tag, size in zip(_HEADING_TAGS, _HEADING_SIZES):
        text.tag_configure(tag, font=(DOCUMENT_FONT, size, "bold"), spacing1=7, spacing3=4)
    text.tag_configure(_METADATA_TAG, font=(DOCUMENT_FONT, 9), foreground="#52647B")
    text.tag_raise("sel")


def style_document_lines(text, lines):
    """Apply visual hierarchy without editing the Markdown source."""
    for tag in (*_HEADING_TAGS, _METADATA_TAG):
        text.tag_remove(tag, "1.0", "end")
    fence = None
    for line, value in enumerate(lines, 1):
        delimiter = _FENCE.match(value)
        if delimiter:
            token = delimiter[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        heading = _HEADING.match(value)
        if heading:
            text.tag_add(_HEADING_TAGS[len(heading[1]) - 1], f"{line}.0", f"{line + 1}.0")
        elif _STATUS.fullmatch(value) or value.startswith("> 项目："):
            text.tag_add(_METADATA_TAG, f"{line}.0", f"{line + 1}.0")


class MarkdownView(ttk.Frame):
    """Read-only Markdown preview used by the original Studio layout."""
    def __init__(self, parent, theme=None):
        super().__init__(parent)
        self.base_dir = Path.cwd()
        self._images = []
        self.text = tk.Text(self, wrap="word", padx=12, pady=10, relief="flat",
                            background="white", foreground="#243247", state="disabled")
        configure_document_styles(self.text)
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

    def set_base_dir(self, path):
        self.base_dir = Path(path).resolve()

    def set_markdown(self, markdown):
        self._images = []
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        cursor = 0
        source = str(markdown or "")
        for match in _IMAGE.finditer(source):
            self.text.insert("end", source[cursor:match.start()])
            raw = match.group(2).strip().strip("<>")
            candidate = Path(raw)
            path = candidate if candidate.is_absolute() else self.base_dir / candidate
            try:
                path = path.resolve()
                if not path.is_relative_to(self.base_dir) or not path.is_file():
                    raise FileNotFoundError(raw)
                if Image is None:
                    raise RuntimeError("Pillow unavailable")
                with Image.open(path) as opened:
                    image = opened.copy()
                image.thumbnail((760, 520))
                photo = ImageTk.PhotoImage(image)
                self.text.image_create("end", image=photo)
                self._images.append(photo)
                if match.group(1):
                    self.text.insert("end", "\n" + match.group(1) + "\n")
            except Exception:
                self.text.insert("end", "[图片未能显示：" + match.group(1) + "]")
            cursor = match.end()
        self.text.insert("end", source[cursor:])
        style_document_lines(self.text, self.text.get("1.0", "end-1c").split("\n"))
        self.text.configure(state="disabled")
