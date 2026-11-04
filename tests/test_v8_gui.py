"""Windows Tk smoke test: new task, attached image and editable working document."""
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from PIL import Image
from core import Store, parse_tasks
from app import App


class WindowsGuiSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "data")
        self.project = self.store.add_project("视觉项目")
        self.doc = self.store.add_document(self.project, "整理文档")
        self.app = App(self.store)
        self.app.withdraw()
        self.app.load_document(self.doc)
        self.app.update_idletasks()

    def tearDown(self):
        if self.app.winfo_exists():
            self.app.destroy()

    def test_add_task_attach_image_and_reopen(self):
        self.app.new_task()
        self.app.task_title.set("第一个任务")
        self.app.task_body.insert("1.0", "需要添加场景图片")
        self.app.save_task()
        self.assertEqual(len(parse_tasks(self.store.document(self.doc)["content"])), 1)

        pic = Path(self.temp.name) / "image.png"
        Image.new("RGB", (32, 20), "white").save(pic)
        self.app.tabs.select(self.app.task_tab)
        self.app._insert_image(pic)
        self.app.save_task()
        saved = self.store.document(self.doc)["content"]
        self.assertIn("assets/", saved)
        self.assertIn("需要添加场景图片", saved)
        self.assertEqual(len(list(self.store.assets_dir(self.doc).glob("*.png"))), 1)

        self.app.load_document(None)
        self.app.load_document(self.doc)
        self.assertIn("第一个任务", self.app.heading.get() + self.store.document(self.doc)["content"])

    def test_editing_working_document_updates_task_list(self):
        self.app.tabs.select(self.app.doc_tab)
        self.app.doc_body.insert("1.0", "### 全文创建的任务\n- [ ]\n正文\n")
        self.app.save_document()
        self.assertEqual(len(self.app.task_rows), 1)
        self.assertIn("全文创建的任务", [x["title"] for x in self.app.task_rows.values()])


if __name__ == "__main__":
    unittest.main()
