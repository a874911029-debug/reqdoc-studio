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


class MarkdownView(ttk.Frame):
    """Read-only Markdown preview used by the original Studio layout."""
    def __init__(self, parent, theme=None):
        super().__init__(parent)
        self.base_dir = Path.cwd()
        self._images = []
        self.text = tk.Text(self, wrap="word", padx=12, pady=10, relief="flat",
                            background="white", foreground="#243247",
                            font=("Microsoft YaHei UI", 10), state="disabled")
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
        self.text.configure(state="disabled")
