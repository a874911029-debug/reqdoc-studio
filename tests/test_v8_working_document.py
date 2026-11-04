from __future__ import annotations

import sqlite3
import tempfile
from contextlib import contextmanager
import unittest
from pathlib import Path

from reqdoc_working_document import (
    delete_working_documents,
    document_to_task_items,
    ensure_working_schema,
    read_working_document,
    write_working_document,
)


class FakeStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)

    @contextmanager
    def conn(self):
        connection = sqlite3.connect(self.db_path)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class WorkingDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "reqdoc.db"
        self.store = FakeStore(self.db_path)
        with self.store.conn() as conn:
            conn.execute(
                "CREATE TABLE studio_sources("
                "id TEXT PRIMARY KEY, doc TEXT, name TEXT, stored TEXT, content TEXT, created TEXT)"
            )
            conn.execute(
                "INSERT INTO studio_sources VALUES(?,?,?,?,?,?)",
                ("source-1", "doc-1", "source.txt", "source.txt", "immutable source", "2026-11-04"),
            )
        ensure_working_schema(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_working_copy_is_separate_from_archived_source(self):
        write_working_document(self.store, "doc-1", "editable copy", "manual")
        self.assertEqual(read_working_document(self.store, "doc-1"), "editable copy")
        with self.store.conn() as conn:
            source = conn.execute(
                "SELECT content FROM studio_sources WHERE id='source-1'"
            ).fetchone()[0]
        self.assertEqual(source, "immutable source")

    def test_legacy_fallback_is_saved_as_derived_working_copy(self):
        self.assertEqual(
            read_working_document(self.store, "doc-legacy", "# Existing processed tasks"),
            "# Existing processed tasks",
        )
        with self.store.conn() as conn:
            row = conn.execute(
                "SELECT content,mode FROM studio_working_docs WHERE doc='doc-legacy'"
            ).fetchone()
        self.assertEqual(row, ("# Existing processed tasks", "legacy-derived"))

    def test_save_updates_only_working_document(self):
        write_working_document(self.store, "doc-1", "first")
        write_working_document(self.store, "doc-1", "second", "import-table")
        with self.store.conn() as conn:
            row = conn.execute(
                "SELECT content,mode FROM studio_working_docs WHERE doc='doc-1'"
            ).fetchone()
            source = conn.execute(
                "SELECT content FROM studio_sources WHERE id='source-1'"
            ).fetchone()[0]
        self.assertEqual(row, ("second", "import-table"))
        self.assertEqual(source, "immutable source")

    def test_document_cleanup_removes_only_working_copy(self):
        write_working_document(self.store, "doc-1", "editable")
        delete_working_documents(self.store, ["doc-1"])
        with self.store.conn() as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM studio_working_docs WHERE doc='doc-1'"
            ).fetchone())
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM studio_sources WHERE id='source-1'"
            ).fetchone())

    def test_manual_headings_convert_to_independent_tasks(self):
        rows = document_to_task_items(
            "# 第一章\n第一条任务正文\n\n## 第二章\n第二条任务正文"
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "第一章")
        self.assertIn("第一条任务正文", rows[0]["content"])
        self.assertEqual(rows[1]["title"], "第二章")

    def test_markdown_task_table_converts_rows(self):
        rows = document_to_task_items(
            "| Task | Details | Section |\n"
            "| --- | --- | --- |\n"
            "| 修复按钮 | 检查回调 | UI |\n"
            "| 修复图片 | 检查剪贴板 | Media |"
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "修复按钮")
        self.assertEqual(rows[0]["section"], "UI")
        self.assertIn("Details：检查回调", rows[0]["content"])


if __name__ == "__main__":
    unittest.main()
