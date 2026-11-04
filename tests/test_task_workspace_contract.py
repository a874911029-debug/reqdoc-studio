from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src" / "reqdoc_studio.py"
TEXT = SOURCE.read_text(encoding="utf-8")


def last_method(name, next_name):
    start = TEXT.rfind(f"    def {name}(")
    end = TEXT.find(f"    def {next_name}(", start + 1)
    if start < 0 or end < 0:
        raise AssertionError(f"Could not locate {name} -> {next_name}")
    return TEXT[start:end]


class TaskWorkspaceContractTests(unittest.TestCase):
    def test_processed_task_workspace_is_the_default_view(self):
        block = last_method("build_tasks_page", "build_progress_page")
        self.assertIn("self.source_panel_visible=False", block)
        self.assertNotIn("self.tasks_split.add(source_panel", block)
        self.assertIn("self.tasks_split.add(right", block)
        self.assertIn("整理结果 / 任务列表", block)

    def test_source_material_is_read_only_in_task_workspace(self):
        block = last_method("build_tasks_page", "build_progress_page")
        self.assertIn("原始资料（只读参考）", block)
        self.assertNotIn("self.task_source_edit.pack(", block)
        source_editor = last_method("edit_task_source", "table_rows_to_tasks")
        self.assertIn("原始资料为只读归档", source_editor)

    def test_image_insertion_uses_text_caret_and_has_preview(self):
        prepare = last_method("_prepare_image_edit", "add_image")
        add = last_method("add_image", "_insert_clipboard_images")
        paste = last_method("paste_image", "paste_event")
        preview = last_method("preview_task_body", "save_task")
        self.assertIn("if self.rid != rid:", prepare)
        self.assertIn("A newly loaded task starts at the end", prepare)
        self.assertEqual(prepare.count("mark_set('insert','end-1c')"), 1)
        self.assertIn("Studio.insert_image(self,Path(path))", add)
        self.assertIn("at_end=False", paste)
        self.assertIn("MarkdownView(win,self.theme)", preview)
        self.assertIn("view.set_base_dir(self.s.document_folder(self.key))", preview)

    def test_import_archives_source_and_creates_independent_tasks(self):
        start = TEXT.rfind("def commit_import_to_store(")
        end = TEXT.find("\n\nclass ImportPreviewDialog", start)
        self.assertGreaterEqual(start, 0)
        block = TEXT[start:end]
        self.assertIn("store.archive_source(target_key,path)", block)
        self.assertIn("store.insert_task(target_key", block)
        self.assertIn("store.snapshot(target_key)", block)


if __name__ == "__main__":
    unittest.main()
