"""ReqDoc Studio V8: standalone local workspace and lossless source archive."""
from __future__ import annotations
import hashlib
import os
import re
import shutil
import sqlite3
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

VERSION = "8.0.0"
APP_NAME = "ReqDocStudio"
SUPPORTED = {".md", ".markdown", ".txt", ".docx"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
TASK_HEADING = re.compile(r"(?m)^### ([^\r\n]+)\s*$")
TASK_MARKER = re.compile(r"^\s*- \[([xX ])\]\s*\n?")
TASK_ID = re.compile(r"^\s*<!-- reqdoc-task:([0-9a-f]{32}) -->\s*\n?")
IMAGE_LINK = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def default_home() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / APP_NAME


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_title(title: str) -> str:
    title = " ".join(title.split())
    if not title or "\n" in title or len(title) > 180:
        raise ValueError("名称不能为空，且不能超过180个字符。")
    return title


def render_task(title: str, body: str, done: bool, identity: str | None = None) -> str:
    ident = identity if identity and re.fullmatch("[0-9a-f]{32}", identity) else uuid.uuid4().hex
    return f"### {safe_title(title)}\n- [{'x' if done else ' '}]\n<!-- reqdoc-task:{ident} -->\n\n{body.strip()}\n\n"


def parse_tasks(markdown: str) -> list[dict]:
    matches = list(TASK_HEADING.finditer(markdown))
    results = []
    for i, found in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        content = markdown[found.end():end].lstrip("\r\n")
        m = TASK_MARKER.match(content)
        done = False
        if m:
            done = m.group(1).lower() == "x"
            content = content[m.end():]
        identity = None
        im = TASK_ID.match(content)
        if im:
            identity = im.group(1)
            content = content[im.end():]
        results.append({
            "id": identity or f"legacy-{found.start()}",
            "title": found.group(1).strip(),
            "body": content.strip(),
            "done": done,
            "start": found.start(),
            "end": end,
        })
    return results


def replace_task(markdown: str, identity: str | None, title: str, body: str, done: bool) -> tuple[str, str]:
    tasks = parse_tasks(markdown)
    if identity is None:
        new_id = uuid.uuid4().hex
        join = "" if not markdown.strip() else "\n\n"
        return markdown.rstrip() + join + render_task(title, body, done, new_id), new_id
    row = next((r for r in tasks if r["id"] == identity), None)
    if row is None:
        raise ValueError("任务已变化，请刷新后再编辑。")
    new_id = identity if re.fullmatch("[0-9a-f]{32}", identity) else uuid.uuid4().hex
    next_text = markdown[:row["start"]] + render_task(title, body, done, new_id) + markdown[row["end"]:]
    return next_text, new_id


def decode_text(path: Path) -> str:
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("单份资料不能超过20 MB。")
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            result = path.read_text(encoding)
            if "\x00" not in result:
                return result
        except UnicodeError:
            continue
    raise ValueError("无法识别文本编码，请将文件另存为 UTF-8。")


def convert_source(path: Path, asset_dir: Path) -> str:
    if path.suffix.lower() not in SUPPORTED:
        raise ValueError("仅支持 TXT、Markdown 和 DOCX。")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("单份资料不能超过20 MB。")
    if path.suffix.lower() != ".docx":
        result = decode_text(path)
        if path.suffix.lower() in (".md", ".markdown"):
            def keep_image(match):
                ref = match.group(2)
                if "://" in ref or ref.startswith("data:") or Path(ref).is_absolute() or ".." in Path(ref).parts:
                    return match.group(0)
                source = path.parent / ref
                if not source.is_file() or source.suffix.lower() not in IMAGES:
                    return match.group(0)
                dest = archive_asset(source, asset_dir)
                return f"![{match.group(1)}](assets/{dest.name})"
            result = IMAGE_LINK.sub(keep_image, result)
        return result
    from docx import Document
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph
    from docx.table import Table
    doc = Document(str(path))
    parts = []
    for node in doc.element.body:
        if node.tag == qn("w:p"):
            para = Paragraph(node, doc)
            style = para.style.name.lower() if para.style else ""
            text = para.text.strip()
            if text:
                parts.append(("### " if "heading" in style or "标题" in style else "") + text)
            for blip in node.xpath(".//a:blip"):
                rid = blip.get(qn("r:embed"))
                if rid and rid in doc.part.related_parts:
                    asset = doc.part.related_parts[rid]
                    ext = Path(str(asset.partname)).suffix.lower() or ".png"
                    if ext in IMAGES:
                        dest = archive_asset_bytes(asset.blob, ext, asset_dir)
                        parts.append(f"![Word图片](assets/{dest.name})")
        elif node.tag == qn("w:tbl"):
            rows = [[cell.text.replace("|", "／").replace("\n", " / ") for cell in row.cells] for row in Table(node, doc).rows]
            if rows:
                n = max(len(r) for r in rows)
                parts += ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * n) + " |"]
                parts += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n\n".join(parts)


def archive_asset_bytes(blob: bytes, extension: str, directory: Path) -> Path:
    if len(blob) > 20 * 1024 * 1024:
        raise ValueError("图片超过20 MB。")
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / (hashlib.sha256(blob).hexdigest()[:24] + extension.lower())
    if not out.exists():
        out.write_bytes(blob)
    return out


def archive_asset(path: Path, directory: Path) -> Path:
    if path.suffix.lower() not in IMAGES or not path.is_file():
        raise ValueError("只能添加有效的图片文件。")
    return archive_asset_bytes(path.read_bytes(), path.suffix, directory)


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or default_home()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "workspace.db"
        with self.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS projects(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, position INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL, position INTEGER NOT NULL, content TEXT NOT NULL DEFAULT '',
                    updated TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources(
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                    filename TEXT NOT NULL, path TEXT NOT NULL, created TEXT NOT NULL
                );
            """)

    def connect(self):
        con = sqlite3.connect(str(self.db), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def projects(self):
        with self.connect() as c:
            return [dict(x) for x in c.execute("SELECT * FROM projects ORDER BY position,id")]

    def documents(self, project_id=None):
        with self.connect() as c:
            query = "SELECT * FROM documents"
            params = ()
            if project_id:
                query += " WHERE project_id=?"
                params = (project_id,)
            return [dict(x) for x in c.execute(query + " ORDER BY position,id", params)]

    def document(self, document_id: str) -> dict:
        with self.connect() as c:
            row = c.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            raise ValueError("文档不存在或已经移动。")
        return dict(row)

    def add_project(self, name: str):
        identity = uuid.uuid4().hex
        with self.connect() as c:
            position = c.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            c.execute("INSERT INTO projects VALUES(?,?,?)", (identity, safe_title(name), position))
            c.commit()
        return identity

    def add_document(self, project: str, name: str):
        identity = uuid.uuid4().hex
        with self.connect() as c:
            pos = c.execute("SELECT COUNT(*) FROM documents WHERE project_id=?", (project,)).fetchone()[0]
            c.execute("INSERT INTO documents VALUES(?,?,?,?,?,?)",
                      (identity, project, safe_title(name), pos, "", now()))
            c.commit()
        return identity

    def update_content(self, doc: str, content: str):
        self.document(doc)
        with self.connect() as c:
            c.execute("UPDATE documents SET content=?, updated=? WHERE id=?", (content, now(), doc))
            c.commit()

    def workspace_dir(self, doc: str) -> Path:
        self.document(doc)
        return self.root / "documents" / doc

    def assets_dir(self, doc: str) -> Path:
        return self.workspace_dir(doc) / "assets"

    def add_image(self, doc: str, path: Path) -> str:
        output = archive_asset(Path(path), self.assets_dir(doc))
        return f"![{output.stem}](assets/{output.name})"

    def sources(self, doc: str):
        with self.connect() as c:
            return [dict(x) for x in c.execute(
                "SELECT * FROM sources WHERE document_id=? ORDER BY created,id", (doc,))]

    def archive_source(self, doc: str, path: Path) -> str:
        self.document(doc)
        path = Path(path).resolve()
        if path.suffix.lower() not in SUPPORTED or not path.is_file():
            raise ValueError("资料格式不支持。")
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("资料超过20 MB。")
        wid = uuid.uuid4().hex
        folder = self.workspace_dir(doc)
        archive = folder / "sources" / (wid + path.suffix.lower())
        archive.parent.mkdir(parents=True, exist_ok=True)
        # Convert BEFORE committing the database; failures leave no false source entry.
        converted = convert_source(path, self.assets_dir(doc))
        shutil.copyfile(path, archive)
        if not archive.is_file() or archive.stat().st_size != path.stat().st_size:
            raise OSError("原件归档失败。")
        old = self.document(doc)["content"]
        if "### " not in converted and converted.strip():
            converted = render_task(path.stem, converted, False)
        updated = old.rstrip() + ("\n\n" if old.strip() else "") + converted
        with self.connect() as c:
            c.execute("INSERT INTO sources VALUES(?,?,?,?,?)",
                      (wid, doc, path.name, str(archive.relative_to(self.root)), now()))
            c.execute("UPDATE documents SET content=?,updated=? WHERE id=?", (updated, now(), doc))
            c.commit()
        return wid

    def original_text(self, source_id: str) -> str:
        with self.connect() as c:
            row = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise ValueError("找不到归档原件。")
        path = self.root / row["path"]
        # Return text for viewing only; archived original is never the editing target.
        return convert_source(path, self.assets_dir(row["document_id"]))

    def reorder_project(self, source: str, target: str, after=False):
        ids = [r["id"] for r in self.projects()]
        if source == target or source not in ids or target not in ids:
            return
        ids.remove(source)
        ids.insert(ids.index(target) + int(after), source)
        with self.connect() as c:
            for i, ident in enumerate(ids):
                c.execute("UPDATE projects SET position=? WHERE id=?", (i, ident))
            c.commit()

    def move_document(self, source: str, project: str, target=None, after=False):
        row = self.document(source)
        if project not in {p["id"] for p in self.projects()}:
            raise ValueError("目标项目不存在。")
        src_project = row["project_id"]
        ids = [d["id"] for d in self.documents(project) if d["id"] != source]
        if target is not None and target not in ids:
            raise ValueError("目标文档不属于该项目。")
        index = len(ids) if target is None else ids.index(target) + int(after)
        ids.insert(index, source)
        with self.connect() as c:
            c.execute("UPDATE documents SET project_id=? WHERE id=?", (project, source))
            for i, ident in enumerate(ids):
                c.execute("UPDATE documents SET position=? WHERE id=?", (i, ident))
            if project != src_project:
                for i, doc in enumerate(self.documents(src_project)):
                    c.execute("UPDATE documents SET position=? WHERE id=?", (i, doc["id"]))
            c.commit()

    def backup(self, destination: Path):
        destination = Path(destination).resolve()
        if destination.is_relative_to(self.root):
            raise ValueError("备份文件必须保存在用户数据目录之外。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_db = self.root.parent / ("reqdoc-backup-" + uuid.uuid4().hex + ".db")
        try:
            with self.connect() as original, sqlite3.connect(temp_db) as snapshot:
                original.backup(snapshot)
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(temp_db, "workspace.db")
                docs = self.root / "documents"
                if docs.exists():
                    for file in docs.rglob("*"):
                        if file.is_file():
                            archive.write(file, file.relative_to(self.root).as_posix())
        finally:
            temp_db.unlink(missing_ok=True)
