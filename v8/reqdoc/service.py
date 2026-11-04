from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

from .documents import DocumentRecord, DocumentService


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _file_stem(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")
    return (value[:80] or "document")


@dataclass(frozen=True)
class ProjectGroup:
    id: int
    name: str


@dataclass(frozen=True)
class Requirement:
    id: int
    project: str
    title: str
    section: str
    content: str
    done: bool


@dataclass(frozen=True)
class ServiceResult:
    requirement: Requirement


class Database:
    @staticmethod
    def delete_requirements(connection, ids):
        ids = list(dict.fromkeys(int(x) for x in ids))
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        connection.execute(
            f"DELETE FROM reqdoc_requirements WHERE id IN ({placeholders})", ids
        )


class ReqdocService:
    """V8 normalized local data service consumed by the original CompactStudio UI."""
    def __init__(self, settings):
        self.settings = settings
        self.db = Database()
        self.documents = DocumentService(settings, self)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(str(self.settings.db_path), timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        self.settings.ensure_directories()
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS reqdoc_projects(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reqdoc_documents(
                    key TEXT PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES reqdoc_projects(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    file_stem TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, name)
                );
                CREATE TABLE IF NOT EXISTS reqdoc_requirements(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL REFERENCES reqdoc_documents(key) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    section TEXT NOT NULL DEFAULT '未分类',
                    content TEXT NOT NULL DEFAULT '',
                    done INTEGER NOT NULL DEFAULT 0,
                    position INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reqdoc_documents_project
                    ON reqdoc_documents(project_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_reqdoc_requirements_project
                    ON reqdoc_requirements(project, position, id);
            """)
        return True

    def _document_row(self, key):
        with self.connect() as c:
            row = c.execute("""
                SELECT d.key,d.name,d.file_stem,p.name AS project_name
                FROM reqdoc_documents d JOIN reqdoc_projects p ON p.id=d.project_id
                WHERE d.key=?
            """, (str(key),)).fetchone()
        return row

    def _to_document(self, row):
        if row is None:
            return None
        path = self.document_path(row["key"])
        content = path.read_text("utf-8") if path.is_file() else ""
        return DocumentRecord(
            key=row["key"], project=row["key"], project_name=row["project_name"],
            name=row["name"], file_stem=row["file_stem"], content=content,
        )

    def project_groups(self):
        with self.connect() as c:
            rows = c.execute(
                "SELECT id,name FROM reqdoc_projects ORDER BY id"
            ).fetchall()
        return [ProjectGroup(int(row["id"]), row["name"]) for row in rows]

    def documents_for(self, project_name):
        with self.connect() as c:
            rows = c.execute("""
                SELECT d.key,d.name,d.file_stem,p.name AS project_name
                FROM reqdoc_documents d JOIN reqdoc_projects p ON p.id=d.project_id
                WHERE p.name=? ORDER BY d.created_at,d.rowid
            """, (str(project_name),)).fetchall()
        return [self._to_document(row) for row in rows]

    def document(self, key):
        row = self._document_row(key)
        if row is None:
            raise ValueError("找不到这份文档。")
        return self._to_document(row)

    def get_document(self, key):
        row = self._document_row(key)
        return self._to_document(row)

    def create_project_group(self, name):
        name = str(name).strip()
        if not name or len(name) > 180:
            raise ValueError("项目名称不能为空，且不能超过180个字符。")
        with self.connect() as c:
            cursor = c.execute(
                "INSERT INTO reqdoc_projects(name,created_at) VALUES(?,?)",
                (name, _now()),
            )
            ident = cursor.lastrowid
        return ProjectGroup(int(ident), name)

    def create_document(self, project_name, name):
        name = str(name).strip()
        if not name or len(name) > 180:
            raise ValueError("文档名称不能为空，且不能超过180个字符。")
        with self.connect() as c:
            project = c.execute(
                "SELECT id FROM reqdoc_projects WHERE name=?", (str(project_name),)
            ).fetchone()
            if project is None:
                raise ValueError("目标项目不存在。")
            key = uuid.uuid4().hex
            stamp = _now()
            c.execute("""
                INSERT INTO reqdoc_documents(key,project_id,name,file_stem,created_at,updated_at)
                VALUES(?,?,?,?,?,?)
            """, (key, int(project["id"]), name, _file_stem(name), stamp, stamp))
        self.document_folder(key).mkdir(parents=True, exist_ok=True)
        return self.document(key)

    def rename_project_group(self, old_name, new_name):
        new_name = str(new_name).strip()
        if not new_name or len(new_name) > 180:
            raise ValueError("项目名称不能为空，且不能超过180个字符。")
        with self.connect() as c:
            cursor = c.execute(
                "UPDATE reqdoc_projects SET name=? WHERE name=?",
                (new_name, str(old_name)),
            )
            if cursor.rowcount != 1:
                raise ValueError("项目不存在或名称已变化。")
        return True

    def rename_document(self, key, new_name):
        new_name = str(new_name).strip()
        if not new_name or len(new_name) > 180:
            raise ValueError("文档名称不能为空，且不能超过180个字符。")
        with self.connect() as c:
            cursor = c.execute("""
                UPDATE reqdoc_documents SET name=?,file_stem=?,updated_at=? WHERE key=?
            """, (new_name, _file_stem(new_name), _now(), str(key)))
            if cursor.rowcount != 1:
                raise ValueError("文档不存在或名称已变化。")
        return self.document(key)

    def document_folder(self, key):
        if self._document_row(key) is None:
            raise ValueError("文档不存在。")
        return self.settings.docs_dir / str(key)

    def document_path(self, key):
        return self.document_folder(key) / "document.md"

    def list_requirements(self, project=None):
        if project is None:
            query = "SELECT * FROM reqdoc_requirements ORDER BY project,position,id"
            params = ()
        else:
            query = "SELECT * FROM reqdoc_requirements WHERE project=? ORDER BY position,id"
            params = (str(project),)
        with self.connect() as c:
            rows = c.execute(query, params).fetchall()
        return [self._to_requirement(row) for row in rows]

    @staticmethod
    def _to_requirement(row):
        return Requirement(
            id=int(row["id"]), project=row["project"], title=row["title"],
            section=row["section"] or "未分类", content=row["content"] or "",
            done=bool(row["done"]),
        )

    def get_requirement(self, requirement_id):
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM reqdoc_requirements WHERE id=?", (int(requirement_id),)
            ).fetchone()
        if row is None:
            raise ValueError("任务不存在或已删除。")
        return self._to_requirement(row)

    def prepare_add(self, content, project, title, section, use_llm=False):
        if self._document_row(project) is None:
            raise ValueError("目标文档不存在。")
        title = str(title).strip()
        content = str(content or "")
        if not title and not content.strip():
            raise ValueError("请至少填写标题或正文。")
        if not title:
            title = content.strip().splitlines()[0][:80]
        return {
            "project": str(project), "title": title,
            "section": str(section or "未分类"), "content": content,
        }

    def commit_add(self, prepared):
        if not isinstance(prepared, dict):
            raise ValueError("任务数据无效。")
        project = prepared["project"]
        self._document_row(project) or (_ for _ in ()).throw(ValueError("目标文档不存在。"))
        with self.connect() as c:
            position = c.execute(
                "SELECT COALESCE(MAX(position),-1000)+1000 FROM reqdoc_requirements WHERE project=?",
                (project,),
            ).fetchone()[0]
            cursor = c.execute("""
                INSERT INTO reqdoc_requirements(project,title,section,content,done,position,created_at,updated_at)
                VALUES(?,?,?,?,0,?,?,?)
            """, (project, prepared["title"], prepared["section"],
                  prepared["content"], int(position), _now(), _now()))
            ident = cursor.lastrowid
        return ServiceResult(self.get_requirement(ident))

    def update(self, requirement_id, **fields):
        allowed = {"title", "section", "content", "done"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "title" in updates:
            updates["title"] = str(updates["title"]).strip()
            if not updates["title"]:
                raise ValueError("任务标题不能为空。")
        if "section" in updates:
            updates["section"] = str(updates["section"]).strip() or "未分类"
        if "content" in updates:
            updates["content"] = str(updates["content"])
        if "done" in updates:
            updates["done"] = int(bool(updates["done"]))
        if updates:
            assignments = ",".join(f"{key}=?" for key in updates)
            values = list(updates.values()) + [_now(), int(requirement_id)]
            with self.connect() as c:
                cursor = c.execute(
                    f"UPDATE reqdoc_requirements SET {assignments},updated_at=? WHERE id=?",
                    values,
                )
                if cursor.rowcount != 1:
                    raise ValueError("任务不存在或已删除。")
        return ServiceResult(self.get_requirement(requirement_id))

    def set_done(self, requirement_id, done):
        return self.update(requirement_id, done=bool(done)).requirement

    def delete_document(self, key):
        folder = self.settings.docs_dir / str(key)
        with self.connect() as c:
            cursor = c.execute("DELETE FROM reqdoc_documents WHERE key=?", (str(key),))
            if cursor.rowcount != 1:
                raise ValueError("文档不存在。")
        if folder.exists():
            shutil.rmtree(folder)
        return True

    def delete_project(self, name):
        with self.connect() as c:
            row = c.execute(
                "SELECT id FROM reqdoc_projects WHERE name=?", (str(name),)
            ).fetchone()
            if row is None:
                raise ValueError("项目不存在。")
            keys = [r[0] for r in c.execute(
                "SELECT key FROM reqdoc_documents WHERE project_id=?", (row["id"],)
            )]
            c.execute("DELETE FROM reqdoc_projects WHERE id=?", (row["id"],))
        for key in keys:
            folder = self.settings.docs_dir / key
            if folder.exists():
                shutil.rmtree(folder)
        return True
