"""Reqdoc Studio test 1. Original task engine with a document-focused shell.
GitHub update check is opt-in. Python 3.12, Tk, python-docx.
"""
from __future__ import annotations
import os, sys, re, json, uuid, hashlib, shutil, sqlite3, zipfile, tempfile, logging
from pathlib import Path
from datetime import datetime
from contextlib import ExitStack, contextmanager, nullcontext
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from reqdoc.config import Settings
from reqdoc.service import ReqdocService
from reqdoc.documents import DocumentService, render_requirement_body
from reqdoc.gui.theme import configure_theme
from reqdoc.gui.widgets import MarkdownView
from reqdoc_source_table import tables_in, is_task_table, table_to_task_items
from version import VERSION as APP_VERSION

VERSION = f'ReqDoc Studio V{APP_VERSION}'
def stamp(): return datetime.now().strftime('%Y%m%d-%H%M%S-%f')
def digest(data): return hashlib.sha256(data).hexdigest()
def write_atomic(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_bytes(data); os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

@contextmanager
def sqlite_connection(path, *args, **kwargs):
    """SQLite context that commits/rolls back and always closes the connection."""
    connection=sqlite3.connect(path,*args,**kwargs)
    try:
        yield connection
        if connection.in_transaction:connection.commit()
    except BaseException:
        if connection.in_transaction:connection.rollback()
        raise
    finally:
        connection.close()

def protect_original_writer():
    """Keep the original engine's external MD revisions before replacing them."""
    original = DocumentService.write_markdown
    if getattr(original, '_studio', False): return
    def guarded(self, document):
        studio = getattr(self, '_studio_store', None)
        if studio is not None:
            document = document.model_copy(update={'content': studio.compose(document.project)})
        path = self._document_path(document.project)
        ledger = Path(self.settings.home) / 'studio-writes.json'
        try: known = json.loads(ledger.read_text('utf-8'))
        except (FileNotFoundError, ValueError): known = {}
        if path.exists():
            data = path.read_bytes(); old = digest(data)
            if old != digest(document.content.encode('utf-8')) and known.get(str(path)) != old:
                backup = path.parent / '_external_versions' / (stamp() + '-' + path.name)
                write_atomic(backup, data)
        result = original(self, document)
        if path.exists():
            known[str(path)] = digest(path.read_bytes())
            write_atomic(ledger, json.dumps(known, ensure_ascii=False).encode('utf-8'))
        return result
    guarded._studio = True
    DocumentService.write_markdown = guarded


def read_source(path, media_dir=None):
    """Read text in document order, keep fences/nesting; archive keeps full fidelity."""
    path = Path(path)
    if path.stat().st_size > 20 * 1024 * 1024: raise ValueError('单文件上限为 20 MB。')
    if path.suffix.lower() not in ('.md', '.markdown', '.txt', '.docx'):
        raise ValueError('本版支持 Word (.docx)、Markdown 和 TXT。')
    warnings = []
    if path.suffix.lower() != '.docx':
        for enc in ('utf-8-sig','utf-16','gb18030'):
            try:
                text = path.read_text(enc)
                if '\x00' not in text: return text, warnings
            except UnicodeError: pass
        raise ValueError('无法识别文本编码，请另存为 UTF-8。')
    from docx import Document
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph
    from docx.table import Table
    doc = Document(path); parts = []
    warnings.append('Word 转换为可整理文本；复杂版式、批注、公式及合并单元格请查看保留的原文件。')
    for node in doc.element.body:
        if node.tag == qn('w:p'):
            p = Paragraph(node, doc); name = p.style.name if p.style else ''
            h = re.search(r'(?:heading|标题)\s*([1-6])$', name, re.I)
            if name.lower() == 'title': parts.append('# ' + p.text)
            elif h: parts.append('#' * min(6,int(h[1])) + ' ' + p.text)
            else:
                prefix = '- ' if ('list' in name.lower() or node.xpath('.//w:numPr')) else ''
                parts.append(prefix + p.text)
            for blip in node.xpath('.//a:blip'):
                rid = blip.get(qn('r:embed'))
                if rid and rid in doc.part.related_parts:
                    part = doc.part.related_parts[rid]
                    if media_dir:
                        media_dir = Path(media_dir);media_dir.mkdir(parents=True,exist_ok=True)
                        ext = Path(str(part.partname)).suffix or '.png'
                        image = media_dir / (digest(part.blob)[:24] + ext)
                        write_atomic(image, part.blob)
                        parts.append(f'![Word 图片]({image.as_posix()})')
                    else: parts.append('[Word 图片：导入时提取，原图保留在原文件中]')
        elif node.tag == qn('w:tbl'):
            table = Table(node,doc)
            rows = [[c.text.replace('\n',' / ').replace('|','\\|') for c in row.cells] for row in table.rows]
            if rows:
                parts.append('\n'.join([
                    '| ' + ' | '.join(rows[0]) + ' |',
                    '| ' + ' | '.join(['---']*len(rows[0])) + ' |',
                    *('| ' + ' | '.join(row) + ' |' for row in rows[1:]),
                ]))
    return '\n\n'.join(parts), warnings


def split_sections(text):
    """One task per headed section; body retains nested lists, tables and code."""
    result=[]; stack=[]; lines=[]; fence=None; chapter='导入内容'
    def flush():
        body='\n'.join(lines).strip()
        if body: result.append((chapter, body))
        lines.clear()
    for line in text.splitlines():
        stripped=line.lstrip(); fm=re.match(r'^(`{3,}|~{3,})', stripped)
        if fm:
            token=fm[1]
            if fence is None: fence=(token[0],len(token))
            elif token[0]==fence[0] and len(token)>=fence[1]: fence=None
            lines.append(line);continue
        hm=None if fence else re.match(r'^(#{1,6})\s+(.+?)\s*#*$',line)
        if hm:
            flush();level=len(hm[1]);label=hm[2]
            stack=[x for x in stack if x[0]<level];stack.append((level,label))
            chapter=' / '.join(x[1] for x in stack)
        else: lines.append(line)
    flush()
    return result or [('导入内容', text)]


def export_word(text, path, base):
    from docx import Document
    from docx.shared import Inches, Pt
    from docx.oxml.ns import qn
    d=Document();style=d.styles['Normal'];style.font.name='Microsoft YaHei';style.font.size=Pt(10.5)
    style.element.rPr.rFonts.set(qn('w:eastAsia'),'Microsoft YaHei')
    lines=text.splitlines();i=0;fence=False
    tables={table.start_line:table for table in tables_in(text)}
    while i<len(lines):
        line=lines[i];i+=1
        if line.startswith(('```','~~~')):fence=not fence;continue
        if fence:
            p=d.add_paragraph();p.add_run(line).font.name='Consolas';continue
        source_table=tables.get(i-1)
        if source_table is not None:
            cells=(source_table.headers,*source_table.rows)
            table=d.add_table(rows=len(cells),cols=len(source_table.headers));table.style='Table Grid'
            for ri,row in enumerate(cells):
                for ci,value in enumerate(row):table.cell(ri,ci).text=value
            i=source_table.end_line
            continue
        image=re.match(r'^!\[([^\]]*)\]\((.+)\)\s*$',line)
        if image:
            pth=Path(image[2]);pth=pth if pth.is_absolute() else Path(base)/pth
            try:d.add_picture(str(pth),width=Inches(5.8));d.add_paragraph(image[1])
            except Exception:d.add_paragraph('[图片未能嵌入：'+image[1]+']')
            continue
        h=re.match(r'^(#{1,6})\s+(.+)',line)
        if h:d.add_heading(h[2],min(6,len(h[1])));continue
        p=d.add_paragraph()
        bullet=re.match(r'^(\s*)[-*+]\s+(.+)',line)
        if bullet:
            p.style='List Bullet';p.paragraph_format.left_indent=Inches(.18+len(bullet[1])*.06);line=bullet[2]
        for k,chunk in enumerate(re.split(r'\*\*(.*?)\*\*',line)):
            run=p.add_run(chunk);run.bold=bool(k%2)
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.stem+'.'+uuid.uuid4().hex+'.tmp.docx')
    try:d.save(tmp);os.replace(tmp,path)
    finally:tmp.unlink(missing_ok=True)
    return path

class Store:
    def __init__(self, settings):
        self.settings=settings
        protect_original_writer()
        self.service=ReqdocService(settings);self.service.initialize()
        with self.conn() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS studio_notes(doc TEXT PRIMARY KEY,intro TEXT NOT NULL DEFAULT '',outro TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS studio_sources(id TEXT PRIMARY KEY,doc TEXT NOT NULL,name TEXT NOT NULL,stored TEXT NOT NULL,content TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS studio_source_revisions(id INTEGER PRIMARY KEY AUTOINCREMENT,source_id TEXT NOT NULL,doc TEXT NOT NULL,previous_content TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS studio_order(id INTEGER PRIMARY KEY,position INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS studio_trash(id INTEGER PRIMARY KEY,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS studio_drafts(key TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS studio_versions(id INTEGER PRIMARY KEY,doc TEXT NOT NULL,content TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS studio_tree_order(kind TEXT NOT NULL, parent TEXT NOT NULL, item TEXT NOT NULL, position INTEGER NOT NULL, PRIMARY KEY(kind,item));
            ''')
        self.service.documents._studio_store = self
    def conn(self):return sqlite_connection(self.settings.db_path)
    def ensure_studio_schema(self):
        with self.conn() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS studio_notes(doc TEXT PRIMARY KEY,intro TEXT NOT NULL DEFAULT '',outro TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS studio_sources(id TEXT PRIMARY KEY,doc TEXT NOT NULL,name TEXT NOT NULL,stored TEXT NOT NULL,content TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS studio_source_revisions(id INTEGER PRIMARY KEY AUTOINCREMENT,source_id TEXT NOT NULL,doc TEXT NOT NULL,previous_content TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS studio_order(id INTEGER PRIMARY KEY,position INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS studio_trash(id INTEGER PRIMARY KEY,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS studio_drafts(key TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS studio_versions(id INTEGER PRIMARY KEY,doc TEXT NOT NULL,content TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS studio_tree_order(kind TEXT NOT NULL, parent TEXT NOT NULL, item TEXT NOT NULL, position INTEGER NOT NULL, PRIMARY KEY(kind,item));
            ''')
    def notes(self,key):
        with self.conn() as c:return c.execute('SELECT intro,outro FROM studio_notes WHERE doc=?',(key,)).fetchone() or ('','')
    def save_notes(self,key,intro,outro):
        with self.conn() as c:c.execute('INSERT OR REPLACE INTO studio_notes VALUES(?,?,?)',(key,intro,outro))
    def draft(self,key,value=None,delete=False):
        with self.conn() as c:
            if delete:c.execute('DELETE FROM studio_drafts WHERE key=?',(key,));return
            if value is not None:c.execute('INSERT OR REPLACE INTO studio_drafts VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)));return
            row=c.execute('SELECT body FROM studio_drafts WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else None
    def tasks(self,key,trash=False):
        with self.conn() as c:
            removed={r[0] for r in c.execute('SELECT id FROM studio_trash')}
            order=dict(c.execute('SELECT id,position FROM studio_order'))
        rows=self.service.list_requirements(project=key)
        return sorted([r for r in rows if (r.id in removed)==trash],key=lambda r:(order.get(r.id,r.id*1000),r.id))
    def sort_tree_items(self,kind,parent,items,key_getter):
        """Persisted order: preexisting items keep original engine order."""
        with self.conn() as c:
            c.execute('CREATE TABLE IF NOT EXISTS studio_tree_order(kind TEXT NOT NULL, parent TEXT NOT NULL, item TEXT NOT NULL, position INTEGER NOT NULL, PRIMARY KEY(kind,item))')
            order=dict(c.execute('SELECT item,position FROM studio_tree_order WHERE kind=? AND parent=?',(kind,str(parent))))
        indexed=list(enumerate(items))
        indexed.sort(key=lambda entry:(0,order[key_getter(entry[1])]) if key_getter(entry[1]) in order else (1,entry[0]))
        return [row for _,row in indexed]

    def save_tree_order(self,kind,parent,keys):
        """Atomic ordering metadata only; does not rewrite project or document data."""
        if kind not in ('project','document'):raise ValueError('未知层级。')
        parent=str(parent);keys=list(map(str,keys))
        if len(keys)!=len(set(keys)):raise ValueError('排序对象存在重复。')
        with self.conn() as c:
            c.execute('CREATE TABLE IF NOT EXISTS studio_tree_order(kind TEXT NOT NULL, parent TEXT NOT NULL, item TEXT NOT NULL, position INTEGER NOT NULL, PRIMARY KEY(kind,item))')
            c.execute('DELETE FROM studio_tree_order WHERE kind=? AND parent=?',(kind,parent))
            c.executemany('INSERT INTO studio_tree_order(kind,parent,item,position) VALUES(?,?,?,?)',
                          [(kind,parent,k,i*1000) for i,k in enumerate(keys)])

    def reorder(self,key,item_id,delta):
        rows=self.tasks(key);ids=[r.id for r in rows];i=ids.index(item_id);j=max(0,min(len(ids)-1,i+delta))
        ids[i],ids[j]=ids[j],ids[i]
        with self.conn() as c:
            for pos,rid in enumerate(ids):c.execute('INSERT OR REPLACE INTO studio_order VALUES(?,?)',(rid,pos))
    def trash(self,ids,restore=False):
        with self.conn() as c:
            for rid in ids:
                if restore:c.execute('DELETE FROM studio_trash WHERE id=?',(rid,))
                else:c.execute('INSERT OR REPLACE INTO studio_trash VALUES(?,?)',(rid,stamp()))
    def save_task(self,key,title,section,content,rid=None):
        if not title.strip() and not content.strip():raise ValueError('请至少填写标题或正文。')
        title=title.strip() or content.splitlines()[0][:80]
        if rid:
            # Never let an editor opened from an old document update a task in another document.
            owned={row.id for row in self.service.list_requirements(project=key)}
            if rid not in owned:
                raise ValueError('任务不属于当前文档，已拒绝保存。请重新选择文档。')
            return self.service.update(rid,title=title,content=content,section=section or '未分类').requirement
        prepared=self.service.prepare_add(content,project=key,title=title,section=section or '未分类',use_llm=False)
        return self.service.commit_add(prepared).requirement
    def insert_task(self,key,title,section,content,after_id=None,chapter_end=True):
        """创建任务并明确插入位置，不改变其他任务的完成状态。"""
        before=self.tasks(key)
        row=self.save_task(key,title,section,content)
        ids=[item.id for item in before]
        if after_id is not None and after_id in ids:
            position=ids.index(after_id)+1
        elif chapter_end:
            matches=[i for i,item in enumerate(before) if (item.section or '未分类')==(section or '未分类')]
            position=(matches[-1]+1) if matches else len(ids)
        else:
            position=len(ids)
        ids.insert(position,row.id)
        with self.conn() as c:
            for index,task_id in enumerate(ids):
                c.execute('INSERT OR REPLACE INTO studio_order(id,position) VALUES(?,?)',(task_id,index*1000))
        return row
    def compose(self,key,only_open=False,*,rows=None,status_lines=None):
        doc=self.service.document(key);intro,outro=self.notes(key)
        rows=[r for r in (self.tasks(key) if rows is None else rows) if not only_open or not r.done]
        result=[f'# {doc.name}',f'> 项目：{doc.project_name} · 任务 {len(rows)} · 已完成 {sum(r.done for r in rows)}','']
        if intro:result.extend([intro,''])
        section=None
        for r in rows:
            if section!=r.section:section=r.section;result.extend(['## '+(section or '未分类'),''])
            result.append(f'### {r.title}')
            if status_lines is not None:status_lines[r.id]=len(result)
            result.extend([f'> #{r.id} · '+('已完成' if r.done else '未完成'),'',render_requirement_body(r),''])
        if outro:result.extend(['## 补充说明','',outro,''])
        if status_lines is not None:
            offset=0;offsets=[]
            for part in result:
                offsets.append(offset);offset+=part.count('\n')+1
            for rid,index in status_lines.items():status_lines[rid]=offsets[index]
        return '\n'.join(result)
    def snapshot(self,key):
        text=self.compose(key)
        with self.conn() as c:
            last=c.execute('SELECT content FROM studio_versions WHERE doc=? ORDER BY id DESC LIMIT 1',(key,)).fetchone()
            if not last or last[0]!=text:c.execute('INSERT INTO studio_versions(doc,content,created) VALUES(?,?,?)',(key,text,stamp()))
        record = self.service.get_document(key)
        if record is not None:
            self.service.documents.write_markdown(record.model_copy(update={'content': text}))
        else:
            write_atomic(self.service.document_path(key), text.encode('utf-8'))
        return text
    def sources(self,key):
        with self.conn() as c:return c.execute('SELECT id,name,stored,content,created FROM studio_sources WHERE doc=? ORDER BY created DESC',(key,)).fetchall()
    def save_source_text(self,key,source_id,new_content):
        """Update only the source's editable DB copy. Archived file remains immutable."""
        if not isinstance(new_content,str):raise ValueError('资料内容必须为文本。')
        if len(new_content.encode('utf-8'))>20*1024*1024:raise ValueError('修改稿不能超过 20 MB。')
        with self.conn() as c:
            row=c.execute('SELECT content FROM studio_sources WHERE id=? AND doc=?',(source_id,key)).fetchone()
            if row is None:raise ValueError('资料不属于当前文档；已拒绝修改。')
            if row[0]==new_content:return False
            c.execute('INSERT INTO studio_source_revisions(source_id,doc,previous_content,created) VALUES(?,?,?,?)',
                      (source_id,key,row[0],stamp()))
            c.execute('UPDATE studio_sources SET content=? WHERE id=? AND doc=?',(new_content,source_id,key))
        return True


    def _cleanup_studio_refs(self,doc_keys,task_ids):
        """After the engine removes records, clean Studio-only metadata."""
        doc_keys=list(dict.fromkeys(doc_keys));task_ids=list(dict.fromkeys(int(x) for x in task_ids))
        with self.conn() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS studio_tree_order(kind TEXT NOT NULL, parent TEXT NOT NULL, item TEXT NOT NULL, position INTEGER NOT NULL, PRIMARY KEY(kind,item))')
            for key in doc_keys:
                conn.execute('DELETE FROM studio_tree_order WHERE kind=? AND item=?',('document',key))
                conn.execute('DELETE FROM studio_notes WHERE doc=?',(key,))
                conn.execute('DELETE FROM studio_sources WHERE doc=?',(key,))
                conn.execute('DELETE FROM studio_source_revisions WHERE doc=?',(key,))
                conn.execute('DELETE FROM studio_versions WHERE doc=?',(key,))
                drafts=[r[0] for r in conn.execute('SELECT key FROM studio_drafts') if r[0].startswith('task:'+key+':')]
                conn.executemany('DELETE FROM studio_drafts WHERE key=?',[(k,) for k in drafts])
            for tid in task_ids:
                conn.execute('DELETE FROM studio_trash WHERE id=?',(tid,))
                conn.execute('DELETE FROM studio_order WHERE id=?',(tid,))

    def delete_document_permanently(self,key):
        """Delete one document without creating an implicit backup."""
        task_ids=[r.id for r in self.service.list_requirements(project=key)]
        self.service.delete_document(key)
        self._cleanup_studio_refs([key],task_ids)
        return None

    def delete_project_permanently(self,name):
        """Delete the project and its documents without creating a backup."""
        docs=list(self.service.documents_for(name))
        keys=[doc.key for doc in docs]
        task_ids=[r.id for key in keys for r in self.service.list_requirements(project=key)]
        self.service.delete_project(name)
        self._cleanup_studio_refs(keys,task_ids)
        return keys

    def purge_trashed_tasks(self,key,task_ids):
        """Purge only tasks in this document's trash; never purge live tasks."""
        chosen = self._validated_task_ids(task_ids,self.tasks(key,trash=True))
        backup = self.backup()
        self.delete_tasks_permanently(key,chosen,trash=True)
        return backup

    def delete_tasks_permanently(self,key,task_ids,*,trash=False):
        """Remove selected tasks and their Studio metadata from the database."""
        with self.service.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            chosen = self._validated_task_ids(task_ids,self.tasks(key,trash=trash))
            self._delete_task_rows(conn,key,chosen)
            conn.execute('DELETE FROM studio_versions WHERE doc=?',(key,))

    @staticmethod
    def _validated_task_ids(task_ids,rows):
        chosen = set(map(int,task_ids))
        if not chosen:
            raise ValueError('请先选择当前文档中的任务。')
        if not chosen.issubset(row.id for row in rows):
            raise ValueError('选择中有任务不属于当前列表，请重新选择。')
        return chosen

    def _delete_task_rows(self,conn,key,ids):
        self.service.db.delete_requirements(conn,ids)
        conn.executemany('DELETE FROM studio_trash WHERE id=?',[(rid,) for rid in ids])
        conn.executemany('DELETE FROM studio_order WHERE id=?',[(rid,) for rid in ids])
        conn.executemany('DELETE FROM studio_drafts WHERE key=?',[(f'task:{key}:{rid}',) for rid in ids])
        conn.executemany('DELETE FROM studio_sync_conflicts WHERE doc=? AND task_id=?',[(key,rid) for rid in ids])

    def apply_document_task_edits(self,key,completion,removed):
        """Apply edits to the visible status lines as one transaction."""
        with self.service.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            active = {row.id for row in self.tasks(key)}
            removed = removed & active
            if removed:
                self._delete_task_rows(conn,key,removed)
                conn.execute('DELETE FROM studio_versions WHERE doc=?',(key,))
            updated = datetime.now().isoformat(timespec='seconds')
            conn.executemany('UPDATE reqdoc_requirements SET done=?,updated_at=? WHERE id=?',
                             [(int(done),updated,rid) for rid,done in completion.items() if rid in active-removed])

    def replace_tasks(self,key,task_ids,replacements):
        """Commit a split or merge together with removal of the replaced tasks."""
        with self.service.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = self.tasks(key)
            chosen = self._validated_task_ids(task_ids,rows)
            first = next(row.id for row in rows if row.id in chosen)
            new_ids = self.service.commit_task_rows(key,replacements,connection=conn)
            self._delete_task_rows(conn,key,chosen)
            order = []
            for row in rows:
                if row.id == first:
                    order.extend(new_ids)
                if row.id not in chosen:
                    order.append(row.id)
            conn.executemany('INSERT OR REPLACE INTO studio_order(id,position) VALUES(?,?)',
                             [(rid,i*1000) for i,rid in enumerate(order)])

    def archive_source(self,key,path,*,connection=None,rollback=None,include_id=False):
        """Archive one original and return its document-relative extracted text."""
        path=Path(path);sid=uuid.uuid4().hex
        base=self.service.document_folder(key)
        folder=base/'_sources'/sid;folder.mkdir(parents=True)
        if rollback is not None:rollback.callback(shutil.rmtree,folder,ignore_errors=True)
        target=folder/path.name
        try:
            shutil.copy2(path,target)
            text,warnings=read_source(target,folder/'media')
            media_prefix=(folder/'media').as_posix()+'/'
            media_relative=(folder/'media').relative_to(base).as_posix()+'/'
            text=text.replace(media_prefix,media_relative)
            # Copy relative Markdown image references into the archived source folder.
            if path.suffix.lower() in ('.md','.markdown'):
                def image_copy(m):
                    link=m[2]
                    if '://' in link or link.startswith('data:'):return m[0]
                    source=(path.parent/link).resolve()
                    if not source.is_file():warnings.append('图片未找到：'+link);return m[0]
                    dest=folder/'media'/(digest(source.read_bytes())[:24]+source.suffix)
                    dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
                    return f'![{m[1]}]({dest.relative_to(base).as_posix()})'
                text=re.sub(r'!\[([^\]]*)\]\(([^)]+)\)',image_copy,text)
            with (self.conn() if connection is None else nullcontext(connection)) as c:
                c.execute('INSERT INTO studio_sources VALUES(?,?,?,?,?,?)',(sid,key,path.name,str(target.relative_to(self.settings.docs_dir)),text,stamp()))
            return (warnings,text,sid) if include_id else (warnings,text)
        except Exception:
            shutil.rmtree(folder,ignore_errors=True)
            raise
    def import_source(self,key,path,as_tasks=False):
        warnings,text=self.archive_source(key,path)
        if as_tasks:
            for section,body in split_sections(text):self.save_task(key,section.split(' / ')[-1],section,body)
        self.snapshot(key)
        return warnings

    def clone_library_data(self, destination):
        """Copy a consistent database and all document assets to an empty directory."""
        destination = Path(destination)
        if not destination.is_dir() or any(destination.iterdir()):
            raise ValueError('复制目标必须是空文件夹。')
        home = destination / 'home'
        home.mkdir()
        with self.conn() as source, sqlite_connection(home/'reqdoc.db') as copied:
            source.backup(copied)
            if copied.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('复制的数据库未通过完整性检查。')
        for item in self.settings.home.iterdir():
            if item.name in ('reqdoc.db','reqdoc.db-wal','reqdoc.db-shm','studio.lock'):
                continue
            if item.is_dir():shutil.copytree(item,home/item.name)
            elif item.is_file():shutil.copy2(item,home/item.name)
        shutil.copytree(self.settings.docs_dir,destination/'docs')

    def relocate_library(self,destination):
        """Copy the active data to an empty library and switch the startup path."""
        config = self.settings.library_config
        if config is None:
            raise ValueError('此会话通过环境变量指定了文件库路径。')
        target = Path(destination).resolve(strict=True)
        source_root = self.settings.library_root.resolve()
        if target.is_relative_to(source_root) or source_root.is_relative_to(target):
            raise ValueError('请选择与当前文件库分开的文件夹。')
        if not target.is_dir() or any(target.iterdir()):
            raise ValueError('请选择一个空文件夹。')
        if config.resolve().is_relative_to(target):
            raise ValueError('请选择应用配置目录之外的文件夹。')
        with tempfile.TemporaryDirectory(prefix='.reqdoc-move-',dir=target,ignore_cleanup_errors=True) as staging:
            stage = Path(staging)
            self.clone_library_data(stage)
            moved = []
            try:
                for name in ('home','docs'):
                    dest = target/name
                    if dest.exists():
                        raise ValueError('所选目录中出现了同名文件，请重新选择空文件夹。')
                    (stage/name).rename(dest)
                    moved.append(name)
                write_atomic(config,json.dumps({'path':str(target)},ensure_ascii=False).encode('utf-8'))
            except BaseException:
                for name in reversed(moved):
                    (target/name).rename(stage/name)
                raise

    def backup(self,destination=None):
        folder=Path(self.settings.home).parent/'studio-backups';folder.mkdir(parents=True,exist_ok=True)
        path=Path(destination) if destination else folder/(stamp()+'.zip')
        path.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/'reqdoc.db'
            with self.conn() as source,sqlite_connection(db) as target:source.backup(target)
            tmp=path.with_suffix('.tmp')
            try:
                with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED) as z:
                    z.write(db,'reqdoc-data/home/reqdoc.db')
                    rules = self.settings.home/'organize-rules.json'
                    if rules.is_file():
                        z.write(rules,'reqdoc-data/home/organize-rules.json')
                    for f in self.settings.docs_dir.rglob('*'):
                        if f.is_file() and f.resolve() not in (path.resolve(),tmp.resolve()):z.write(f,'reqdoc-data/docs/'+f.relative_to(self.settings.docs_dir).as_posix())
                    z.writestr('backup-info.json',json.dumps({'version':VERSION,'created':stamp(),'note':'数据库、文档、图片和本地整理规则完整备份；API 密钥不包含在备份中。'},ensure_ascii=False))
                os.replace(tmp,path)
            finally:tmp.unlink(missing_ok=True)
        return path
    def restore_copy(self,archive,folder):
        folder=Path(folder)/('reqdoc-restored-'+stamp());folder.mkdir(parents=True)
        with zipfile.ZipFile(archive) as z:
            if 'reqdoc-data/home/reqdoc.db' not in z.namelist():raise ValueError('不是本程序的备份包。')
            for entry in z.infolist():
                name=entry.filename
                if not name.startswith('reqdoc-data/'):continue
                target=(folder/name).resolve()
                if not target.is_relative_to(folder.resolve()):raise ValueError('备份路径无效。')
                if entry.is_dir():target.mkdir(parents=True,exist_ok=True)
                else:write_atomic(target,z.read(entry))
        return folder

class Studio(tk.Tk):
    def __init__(self,store):
        super().__init__();self.store=store;self.s=store.service;self.key=None;self.rid=None;self.loading=False;self.dirty=False;self.notes_dirty=False;self._closing=False
        self.title('reqdoc · 文档与需求工作台 — '+VERSION);self.geometry('1360x880');self.minsize(1100,700)
        self.theme=configure_theme(self);self.configure(bg='#F4F7FB')
        self.protocol('WM_DELETE_WINDOW',self.close)
        self.bind('<Destroy>',self._on_root_destroy,add='+')
        self.build();self.refresh_tree();self._autosave_after_id=self.after(2000,self.autosave)
        self.bind('<Control-s>',lambda e:self.save_current());self.bind('<Control-n>',lambda e:self.new_task())
    def guard(self,fn):
        if getattr(self,'_closing',False):return
        try:return fn()
        except Exception as e:
            logging.exception('Studio action failed')
            if getattr(self,'_closing',False):return
            try:messagebox.showerror('操作未完成',str(e),parent=self)
            except tk.TclError:return
            try:self.status.set('操作未完成；请查看提示。')
            except tk.TclError:pass
    def button(self,parent,text,command,**kw):
        b=ttk.Button(parent,text=text,command=lambda:self.guard(command));b.pack(side='left',padx=3,pady=3,**kw);return b
    def text_box(self,parent):
        frame=ttk.Frame(parent);frame.pack(fill='both',expand=True)
        t=tk.Text(frame,wrap='word',undo=True,font=('Microsoft YaHei',11),padx=14,pady=12,relief='flat',bg='white',fg='#243247',spacing3=5)
        sb=ttk.Scrollbar(frame,command=t.yview);sb.pack(side='right',fill='y');t.configure(yscrollcommand=sb.set);t.pack(fill='both',expand=True)
        return t
    def build(self):
        top=ttk.Frame(self,padding=(16,10));top.pack(fill='x')
        ttk.Label(top,text='reqdoc  /  文档与需求',font=('Microsoft YaHei',18,'bold')).pack(side='left',padx=(0,25))
        self.button(top,'新建项目',self.new_project);self.button(top,'新建文档',self.new_document)
        self.button(top,'备份',self.backup);self.button(top,'恢复备份副本',self.restore_backup)
        self.button(top,'打开文档目录',lambda:self.open_path(self.store.settings.docs_dir))
        self.button(top,'使用说明',self.help)
        panes=ttk.Panedwindow(self,orient='horizontal');panes.pack(fill='both',expand=True,padx=16,pady=(0,10))
        left=ttk.Frame(panes,width=250);panes.add(left,weight=1)
        ttk.Label(left,text='项目 / 文档',font=('Microsoft YaHei',12,'bold')).pack(anchor='w',pady=8)
        self.tree=ttk.Treeview(left,show='tree',selectmode='browse');self.tree.pack(fill='both',expand=True);self.tree.bind('<<TreeviewSelect>>',self.on_tree)
        self.button(left,'重命名',self.rename_selected)
        main=ttk.Frame(panes);panes.add(main,weight=5)
        self.heading=ttk.Label(main,text='先新建项目和文档，或选择左侧已有文档',font=('Microsoft YaHei',15,'bold'));self.heading.pack(anchor='w',pady=8)
        bar=ttk.Frame(main);bar.pack(fill='x')
        self.button(bar,'新增任务',self.new_task);self.button(bar,'导入资料 / 拆分任务',self.import_dialog)
        self.button(bar,'预览汇总',self.show_preview);self.button(bar,'导出 Word',lambda:self.export('docx'));self.button(bar,'导出 Markdown 包',lambda:self.export('zip'))
        self.tabs=ttk.Notebook(main);self.tabs.pack(fill='both',expand=True,pady=8)
        self.tasktab=ttk.Frame(self.tabs,padding=8);self.tabs.add(self.tasktab,text=' 任务与进度 ')
        taskpanes=ttk.Panedwindow(self.tasktab,orient='horizontal');taskpanes.pack(fill='both',expand=True)
        tl=ttk.Frame(taskpanes);taskpanes.add(tl,weight=2)
        searchbar=ttk.Frame(tl);searchbar.pack(fill='x')
        self.query=tk.StringVar();ttk.Entry(searchbar,textvariable=self.query).pack(side='left',fill='x',expand=True)
        self.filter=tk.StringVar(value='全部');cb=ttk.Combobox(searchbar,textvariable=self.filter,values=['全部','未完成','已完成','回收站'],state='readonly',width=9);cb.pack(side='left',padx=5)
        cb.bind('<<ComboboxSelected>>',lambda e:self.refresh_tasks());self.query.trace_add('write',lambda *a:self.refresh_tasks())
        self.list=ttk.Treeview(tl,columns=('state','title','chapter'),show='headings',selectmode='extended')
        for key,label,w in [('state','状态',65),('title','任务',210),('chapter','章节',160)]:self.list.heading(key,text=label);self.list.column(key,width=w,minwidth=55)
        self.list.pack(fill='both',expand=True,pady=8);self.list.bind('<<TreeviewSelect>>',self.on_task)
        bulk=ttk.Frame(tl);bulk.pack(fill='x')
        self.button(bulk,'完成',lambda:self.mark(True));self.button(bulk,'未完成',lambda:self.mark(False));self.button(bulk,'移入回收站',self.trash_tasks)
        bulk2=ttk.Frame(tl);bulk2.pack(fill='x');self.button(bulk2,'恢复',self.restore_tasks);self.button(bulk2,'上移',lambda:self.move(-1));self.button(bulk2,'下移',lambda:self.move(1));self.button(bulk2,'批量改章节',self.change_chapter)
        editor=ttk.Frame(taskpanes,padding=(12,0));taskpanes.add(editor,weight=3)
        self.editlabel=ttk.Label(editor,text='新任务',font=('Microsoft YaHei',12,'bold'));self.editlabel.pack(anchor='w')
        self.titlevar=tk.StringVar();self.chaptervar=tk.StringVar(value='未分类')
        ttk.Label(editor,text='标题').pack(anchor='w');ttk.Entry(editor,textvariable=self.titlevar).pack(fill='x',pady=(0,6))
        ttk.Label(editor,text='章节（可用 / 表示层级）').pack(anchor='w');self.chapterbox=ttk.Combobox(editor,textvariable=self.chaptervar);self.chapterbox.pack(fill='x',pady=(0,6))
        edits=ttk.Frame(editor);edits.pack(fill='x');self.button(edits,'保存任务  Ctrl+S',self.save_task);self.button(edits,'图片',self.add_image);self.button(edits,'粘贴图',self.paste_image);self.button(edits,'撤销',lambda:self.body.edit_undo());self.button(edits,'重做',lambda:self.body.edit_redo())
        self.body=self.text_box(editor);self.body.bind('<<Modified>>',self.modified);self.body.bind('<Control-v>',self.paste_event);self.body.bind('<Control-V>',self.paste_event)
        for var in (self.titlevar,self.chaptervar):var.trace_add('write',self.changed)
        self.notetab=ttk.Frame(self.tabs,padding=12);self.tabs.add(self.notetab,text=' 前言与补充说明 ')
        ttk.Label(self.notetab,text='这些文字独立保存；任务更新不会覆盖。支持 Markdown 标题、列表和图片语法。').pack(anchor='w',pady=5)
        self.button(self.notetab,'保存说明',self.save_notes)
        ttk.Label(self.notetab,text='前言 / 项目背景（出现在任务之前）').pack(anchor='w');self.intro=self.text_box(self.notetab)
        ttk.Label(self.notetab,text='补充说明 / 验收结论（出现在任务之后）').pack(anchor='w');self.outro=self.text_box(self.notetab)
        for t in (self.intro,self.outro):t.bind('<<Modified>>',self.note_modified)
        self.previewtab=ttk.Frame(self.tabs,padding=8);self.tabs.add(self.previewtab,text=' 汇总预览 ')
        pbar=ttk.Frame(self.previewtab);pbar.pack(fill='x');self.onlyopen=tk.BooleanVar(value=False)
        ttk.Checkbutton(pbar,text='只汇总未完成任务',variable=self.onlyopen,command=self.refresh_preview).pack(side='left');self.button(pbar,'刷新预览',self.refresh_preview)
        self.preview=MarkdownView(self.previewtab,self.theme);self.preview.pack(fill='both',expand=True)
        self.sourcetab=ttk.Frame(self.tabs,padding=8);self.tabs.add(self.sourcetab,text=' 原始资料 ')
        self.sourceslist=ttk.Treeview(self.sourcetab,columns=('name','time'),show='headings');self.sourceslist.heading('name',text='原文件');self.sourceslist.heading('time',text='导入时间');self.sourceslist.pack(fill='both',expand=True)
        sb=ttk.Frame(self.sourcetab);sb.pack(fill='x');self.button(sb,'打开原文件',self.open_source);self.button(sb,'查看提取文本',self.view_source);self.button(sb,'从资料提取任务',self.source_to_tasks)
        self.historytab=ttk.Frame(self.tabs,padding=8);self.tabs.add(self.historytab,text=' 文档快照 ')
        ttk.Label(self.historytab,text='快照保存当时的汇总内容；可以复制或导出，不覆盖当前任务。').pack(anchor='w')
        self.history=ttk.Treeview(self.historytab,columns=('id','time'),show='headings');self.history.heading('id',text='版本');self.history.heading('time',text='保存时间');self.history.pack(fill='both',expand=True)
        self.button(self.historytab,'查看快照',self.view_history)
        self.tabs.bind('<<NotebookTabChanged>>',lambda e:self.tab_changed())
        self.status=tk.StringVar(value='就绪 · 本地保存 · 不调用外部 AI');ttk.Label(self,textvariable=self.status,padding=(16,6)).pack(fill='x')
    def changed(self,*a):
        if not self.loading:self.dirty=True
    def modified(self,e=None):
        if self.body.edit_modified():self.changed();self.body.edit_modified(False)
    def note_modified(self,e):
        if e.widget.edit_modified():
            if not self.loading:self.notes_dirty=True
            e.widget.edit_modified(False)
    def put(self,t,text):t.delete('1.0','end');t.insert('1.0',text);t.edit_modified(False);t.edit_reset()
    def require_doc(self):
        if not self.key:raise ValueError('请先创建或选择一个文档。')
    def refresh_tree(self,select=None):
        self.tree.delete(*self.tree.get_children())
        for p in self.s.project_groups():
            pid='p:'+str(p.id);self.tree.insert('', 'end',iid=pid,text=p.name,open=True,values=(p.name,))
            for d in self.s.documents_for(p.name):self.tree.insert(pid,'end',iid='d:'+d.key,text=d.name,values=(p.name,))
        if select and self.tree.exists('d:'+select):self.tree.selection_set('d:'+select);self.tree.see('d:'+select)
    def selected_project(self):
        sel=self.tree.selection()
        if sel:return self.tree.item(sel[0],'values')[0]
        if self.key:return self.s.document(self.key).project_name
        raise ValueError('请先选择或新建项目。')
    def on_tree(self,e=None):
        sel=self.tree.selection()
        if not sel or not sel[0].startswith('d:'):return
        key=sel[0][2:]
        if key==self.key:return
        self.guard(lambda:self.load_doc(key))
    def load_doc(self,key):
        self.keep_draft();self.persist_notes();self.key=key;self.rid=None
        d=self.s.document(key);self.heading.configure(text=d.project_name+' / '+d.name)
        self.loading=True
        intro,outro=self.store.notes(key);self.put(self.intro,intro);self.put(self.outro,outro);self.notes_dirty=False
        self.loading=False;self.filter.set('全部');self.query.set('');self.refresh_tasks();self.load_editor(None);self.refresh_sources();self.refresh_preview();self.refresh_history()
        self.status.set('已打开 '+d.name+' · 草稿每 2 秒保存；正式任务请点击保存')
    def refresh_tasks(self):
        if not hasattr(self,'list'):return
        keep=self.list.selection();self.list.delete(*self.list.get_children())
        if not self.key:return
        rows=self.store.tasks(self.key,self.filter.get()=='回收站');q=self.query.get().casefold()
        for r in rows:
            if q and q not in (r.title+' '+r.content+' '+r.section).casefold():continue
            if self.filter.get()=='未完成' and r.done:continue
            if self.filter.get()=='已完成' and not r.done:continue
            self.list.insert('','end',iid=str(r.id),values=('已完成' if r.done else '未完成',r.title,r.section))
        keep=[x for x in keep if self.list.exists(x)]
        if keep:self.list.selection_set(keep)
        self.chapterbox.configure(values=sorted({r.section for r in rows}))
    def on_task(self,e=None):
        sel=self.list.selection()
        if len(sel)==1 and int(sel[0])!=self.rid:self.guard(lambda:(self.keep_draft(),self.load_editor(int(sel[0]))))
    def draft_key(self):return f'task:{self.key}:{self.rid or "new"}'
    def keep_draft(self):
        if self.key and self.dirty:
            payload={'title':self.titlevar.get(),'section':self.chaptervar.get(),'body':self.body.get('1.0','end-1c')}
            if hasattr(self,'done_var'):payload['done']=bool(self.done_var.get())
            self.store.draft(self.draft_key(),payload)
    def load_editor(self,rid):
        self.rid=rid;draft=self.store.draft(self.draft_key());r=self.s.get_requirement(rid) if rid else None
        self.loading=True
        self.titlevar.set(draft['title'] if draft else r.title if r else '')
        self.chaptervar.set(draft['section'] if draft else r.section if r else '未分类')
        self.put(self.body,draft['body'] if draft else r.content if r else '')
        self.loading=False;self.dirty=bool(draft)
        self.editlabel.configure(text=('任务 #'+str(rid) if rid else '新任务')+(' · 已恢复草稿' if draft else ''))
    def new_task(self):
        self.require_doc();self.keep_draft();self.list.selection_remove(*self.list.selection());self.load_editor(None);self.tabs.select(self.tasktab);self.body.focus_set()
    def save_task(self):
        self.require_doc();oldkey=self.draft_key()
        r=self.store.save_task(self.key,self.titlevar.get(),self.chaptervar.get(),self.body.get('1.0','end-1c'),self.rid)
        self.store.draft(oldkey,delete=True);self.rid=r.id;self.dirty=False;self.store.snapshot(self.key);self.refresh_tasks();self.list.selection_set(str(r.id));self.load_editor(r.id);self.refresh_history();self.status.set('任务已保存；汇总文档已更新')
    def persist_notes(self):
        if self.key and self.notes_dirty:
            self.store.save_notes(self.key,self.intro.get('1.0','end-1c'),self.outro.get('1.0','end-1c'));self.notes_dirty=False
            self.store.snapshot(self.key)
    def save_notes(self):
        self.require_doc();self.persist_notes();self.store.snapshot(self.key);self.status.set('前言与补充说明已保存');self.refresh_history()
    def save_current(self):
        if self.tabs.select()==str(self.notetab):self.guard(self.save_notes)
        else:self.guard(self.save_task)
        return 'break'
    def autosave(self):
        if getattr(self,'_closing',False):return
        try:self.keep_draft();self.persist_notes()
        except Exception as e:
            if not getattr(self,'_closing',False):
                try:self.status.set('草稿保存失败：'+str(e))
                except tk.TclError:pass
        if not getattr(self,'_closing',False):
            try:self._autosave_after_id=self.after(2000,self.autosave)
            except tk.TclError:self._closing=True
    def selected_ids(self):return [int(x) for x in self.list.selection()]
    def mark(self,done):
        for rid in self.selected_ids():self.s.set_done(rid,done)
        if self.key:self.store.snapshot(self.key)
        self.refresh_tasks();self.status.set('完成状态已更新')
    def trash_tasks(self):
        ids=self.selected_ids()
        if not ids:return
        self.keep_draft();self.store.trash(ids);self.refresh_tasks();self.load_editor(None);self.store.snapshot(self.key);self.status.set('已移入回收站，可随时恢复')
    def restore_tasks(self):
        self.store.trash(self.selected_ids(),restore=True);self.refresh_tasks();self.store.snapshot(self.key);self.status.set('任务已恢复')
    def move(self,delta):
        ids=self.selected_ids()
        if len(ids)!=1:raise ValueError('请选择一条任务调整顺序。')
        self.store.reorder(self.key,ids[0],delta);self.refresh_tasks();self.store.snapshot(self.key)
    def change_chapter(self):
        ids=self.selected_ids()
        if not ids:return
        chapter=simpledialog.askstring('批量修改章节','输入章节路径，例如：建筑 / 广场',parent=self)
        if chapter:
            for rid in ids:self.s.update(rid,section=chapter)
            self.refresh_tasks();self.store.snapshot(self.key)
            if self.rid in ids:self.load_editor(self.rid)
    def new_project(self):
        name=simpledialog.askstring('新建项目','项目名称',parent=self)
        if name:
            p=self.s.create_project_group(name);self.refresh_tree();self.tree.selection_set('p:'+str(p.id));self.status.set('项目已创建，请新建文档')
    def new_document(self):
        p=self.selected_project();name=simpledialog.askstring('新建文档','文档名称',parent=self)
        if name:d=self.s.create_document(p,name);self.refresh_tree(d.key)
    def rename_selected(self):
        sel=self.tree.selection()
        if not sel:return
        name=simpledialog.askstring('重命名','新名称',initialvalue=self.tree.item(sel[0],'text'),parent=self)
        if not name:return
        self.keep_draft();self.persist_notes()
        if sel[0].startswith('d:'):self.s.rename_document(sel[0][2:],name)
        else:self.s.rename_project_group(self.selected_project(),name)
        self.refresh_tree(self.key)
        if self.key:self.heading.configure(text=self.s.document(self.key).project_name+' / '+self.s.document(self.key).name)
    def add_image(self):
        self.require_doc();path=filedialog.askopenfilename(parent=self,filetypes=[('图片','*.png *.jpg *.jpeg *.webp *.bmp *.gif')])
        if not path:return
        self.insert_image(Path(path))
    def paste_event(self,event=None):
        try:
            if self.paste_image(silent=True): return 'break'
        except Exception: pass
    def paste_image(self,silent=False):
        self.require_doc()
        from PIL import ImageGrab, Image
        data=ImageGrab.grabclipboard()
        if isinstance(data, Image.Image):
            with tempfile.TemporaryDirectory() as td:
                path=Path(td)/'剪贴板图片.png';data.save(path);self.insert_image(path)
            return True
        if isinstance(data,list):
            count=0
            for p in data:
                if Path(p).suffix.lower() in ('.png','.jpg','.jpeg','.webp','.bmp','.gif'):
                    self.insert_image(Path(p));count+=1
            if count:return True
        if not silent:messagebox.showinfo('粘贴图片','剪贴板中没有可用图片。',parent=self)
        return False
    def insert_image(self,path,description=None):
        from PIL import Image
        with Image.open(path) as im:im.verify()
        if description is None:
            description=simpledialog.askstring('添加图片描述',
                '请输入这张图片的说明（会写入任务正文，可继续编辑）：',
                initialvalue=path.stem,parent=self)
        if description is None:return False
        description=description.strip() or path.stem
        safe=description.replace('\n',' ').replace(']','\\]').replace('[','\\[')
        folder=self.s.document_folder(self.key)/'assets'/'studio';folder.mkdir(parents=True,exist_ok=True)
        out=folder/(digest(path.read_bytes())[:24]+path.suffix.lower());shutil.copy2(path,out)
        rel=out.relative_to(self.s.document_folder(self.key)).as_posix()
        self.body.insert('insert',f'\n![{safe}]({rel})\n\n图片说明：{description}\n');self.changed()
        return True
    def import_dialog(self):
        self.require_doc();path=filedialog.askopenfilename(parent=self,filetypes=[('文档','*.docx *.md *.markdown *.txt')])
        if not path:return
        text,warnings=read_source(path);sections=split_sections(text)
        win=tk.Toplevel(self);win.title('导入预览 — '+Path(path).name);win.geometry('950x700');win.transient(self);win.grab_set()
        ttk.Label(win,text='原文件将保留。请确认用途和拆分结果。',font=('Microsoft YaHei',13,'bold'),padding=12).pack(anchor='w')
        mode=tk.StringVar(value='source')
        ttk.Radiobutton(win,text='只作为原始资料保存，不生成任务',variable=mode,value='source').pack(anchor='w',padx=12)
        ttk.Radiobutton(win,text=f'按章节生成任务（{len(sections)} 条）；章节内部列表、表格和代码保持为一段',variable=mode,value='tasks').pack(anchor='w',padx=12)
        ttk.Label(win,text='\n'.join(warnings) or 'Markdown / TXT 保留正文；嵌套标题用章节路径记录。',wraplength=880,padding=12).pack(anchor='w')
        nb=ttk.Notebook(win);nb.pack(fill='both',expand=True,padx=12)
        f=ttk.Frame(nb);nb.add(f,text='原文提取');t=self.text_box(f);t.insert('1.0',text);t.configure(state='disabled')
        f2=ttk.Frame(nb);nb.add(f2,text='任务拆分预览');t2=self.text_box(f2)
        t2.insert('1.0','\n\n'.join(f'【任务 {i+1}】{section}\n{body}' for i,(section,body) in enumerate(sections)));t2.configure(state='disabled')
        def commit():
            if mode.get()=='tasks' and len(sections)>500:raise ValueError('单次最多拆分 500 条任务，请分批导入。')
            warnings2=self.store.import_source(self.key,path,mode.get()=='tasks');self.refresh_tasks();self.refresh_sources();win.destroy();self.status.set('导入完成；原文件已保留'+('；有图片或格式提示' if warnings2 else ''))
            if warnings2:messagebox.showinfo('导入说明','\n'.join(warnings2),parent=self)
        bottom=ttk.Frame(win,padding=12);bottom.pack(fill='x');self.button(bottom,'确认导入',commit);self.button(bottom,'取消',win.destroy)
    def refresh_sources(self):
        self.sourceslist.delete(*self.sourceslist.get_children())
        if self.key:
            for sid,name,path,body,time in self.store.sources(self.key):self.sourceslist.insert('','end',iid=sid,values=(name,time[:15]))
    def source_row(self):
        sel=self.sourceslist.selection()
        if not sel:raise ValueError('请选择一份原始资料。')
        return next(r for r in self.store.sources(self.key) if r[0]==sel[0])
    def open_source(self):self.open_path(self.store.settings.docs_dir/self.source_row()[2])
    def view_source(self):
        row=self.source_row();self.show_text(row[1],row[3])
    def source_to_tasks(self):
        row=self.source_row();sections=split_sections(row[3])
        if not messagebox.askyesno('从资料生成任务',f'将生成 {len(sections)} 条任务；重复执行会重复添加。继续吗？',parent=self):return
        if len(sections)>500:raise ValueError('单次最多 500 条任务。')
        for section,body in sections:self.store.save_task(self.key,section.split(' / ')[-1],section,body)
        self.store.snapshot(self.key);self.refresh_tasks();self.tabs.select(self.tasktab)
    def refresh_preview(self):
        if self.key:self.persist_notes();self.preview.set_base_dir(self.s.document_folder(self.key));self.preview.set_markdown(self.store.compose(self.key,self.onlyopen.get()))
    def show_preview(self):self.require_doc();self.refresh_preview();self.tabs.select(self.previewtab)
    def tab_changed(self):
        if not self.key:return
        if self.tabs.select()==str(self.previewtab):self.guard(self.refresh_preview)
        if self.tabs.select()==str(self.historytab):self.guard(self.refresh_history)
    def export(self,kind,content=None,*,name=None):
        self.require_doc();self.keep_draft();self.persist_notes()
        if self.dirty and not messagebox.askyesno('有未保存的任务草稿','汇总仅包含已保存任务。仍导出当前已保存内容吗？',parent=self):return
        name=name or self.s.document(self.key).file_stem or '汇总文档'
        path=filedialog.asksaveasfilename(parent=self,initialfile=name+'.'+kind,defaultextension='.'+kind,filetypes=[('Word' if kind=='docx' else 'Markdown 与图片包','*.'+kind)])
        if not path:return
        if content is None:content=self.store.compose(self.key,self.onlyopen.get())
        base=self.s.document_folder(self.key)
        if kind=='docx':export_word(content,path,base)
        else:
            with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
                images={}
                def portable(m):
                    p=Path(m[2]);p=p if p.is_absolute() else base/p
                    if not p.is_file():return m[0]
                    rel='images/'+digest(p.read_bytes())[:24]+p.suffix;images[rel]=p
                    return f'![{m[1]}]({rel})'
                content=re.sub(r'!\[([^\]]*)\]\(([^)]+)\)',portable,content);z.writestr(name+'.md',content)
                for rel,p in images.items():z.write(p,rel)
        self.store.snapshot(self.key);self.status.set('已导出：'+str(path));messagebox.showinfo('导出完成',str(path),parent=self)
    def refresh_history(self):
        self.history.delete(*self.history.get_children())
        if self.key:
            with self.store.conn() as c:rows=c.execute('SELECT id,created FROM studio_versions WHERE doc=? ORDER BY id DESC',(self.key,)).fetchall()
            for rid,time in rows:self.history.insert('','end',iid=str(rid),values=(rid,time[:15]))
    def view_history(self):
        sel=self.history.selection()
        if sel:
            with self.store.conn() as c:row=c.execute('SELECT content FROM studio_versions WHERE id=?',(int(sel[0]),)).fetchone()
            self.show_text('汇总快照 #'+sel[0],row[0])
    def show_text(self,title,body):
        w=tk.Toplevel(self);w.title(title);w.geometry('900x700');t=self.text_box(w);t.insert('1.0',body);t.configure(state='disabled')
        self.button(w,'复制全文',lambda:(self.clipboard_clear(),self.clipboard_append(body)))
    def backup(self):
        self.keep_draft();self.persist_notes();path=self.store.backup();self.status.set('备份完成：'+str(path));messagebox.showinfo('备份完成',str(path),parent=self)
    def restore_backup(self):
        archive=filedialog.askopenfilename(parent=self,title='选择备份包',filetypes=[('备份 ZIP','*.zip')])
        if not archive:return
        folder=filedialog.askdirectory(parent=self,title='选择恢复副本的保存位置')
        if folder:
            out=self.store.restore_copy(archive,folder);messagebox.showinfo('已恢复为独立副本',str(out)+'\n\n当前工作数据未被覆盖。\n关闭程序后，把恢复副本中的 reqdoc-data 复制到一个新解压的测试版程序目录，再启动。',parent=self)
    def open_path(self,path):
        target=Path(path).expanduser()
        try:resolved=target.resolve(strict=True)
        except (OSError,RuntimeError) as exc:
            raise FileNotFoundError('要打开的文件或目录不存在：'+str(target)) from exc
        if sys.platform=='win32':
            try:os.startfile(str(resolved))
            except OSError as exc:
                raise RuntimeError('Windows 无法打开该文件。请检查默认打开程序或文件权限：'+str(resolved)) from exc
        else:self.status.set(str(resolved))
    def help(self):
        self.show_text('测试版使用说明','''1. 新建项目 → 新建文档 → 新增任务。编辑右侧标题、章节、正文，Ctrl+S 保存。
2. 任务可多选标记完成、改章节、移入回收站；单选上移/下移调整汇总顺序。
3. 导入支持两种用途：保留原始资料，或按章节拆成任务。导入前查看预览。
4. 在“前言与补充说明”编写独立内容，汇总自动组合已保存任务；任务更新不覆盖说明。
5. “汇总预览”支持只显示未完成项；Word 与 Markdown 包采用当前筛选。
6. 任务草稿每 2 秒保存；再次打开同一任务或“新任务”会恢复。正式任务仍需点击保存。
7. 回收站只隐藏任务；不会永久删除。文档快照只读，可复制内容。
8. “备份”保存数据库、图片、资料和草稿；“恢复备份副本”恢复到新目录，不覆盖现有数据。
9. 本版不联网调用 AI。Word 复杂版式、公式、批注等以保留的原文件为准。
10. 新版用 Studio 自己的汇总和回收站视图。不要同时打开旧版编辑同一份数据。
11. 编辑正文支持 Markdown；图片可插入，预览查看图文效果。这一版不是完整所见即所得编辑器。
12. 编辑导出的文件不会回写任务；外部文件修改被旧引擎覆盖前会保留到 _external_versions。''')
    def close(self):
        try:self.keep_draft();self.persist_notes()
        except Exception as e:
            if not messagebox.askyesno(
                '保存失败',
                str(e)+'\n\n仍然退出会丢失当前尚未保存的编辑内容。建议选择“否”，留在窗口中重试保存。',
                default=messagebox.NO,
                parent=self,
            ):return
        try:
            if getattr(self,'_autosave_after_id',None):self.after_cancel(self._autosave_after_id)
        except Exception:pass
        self.destroy()


def import_task_title(section,content):
    """Create a concise editable title from an imported task body."""
    label=(section.split(' / ')[-1] or '导入任务').strip()
    body=content.strip()
    if not body:return label
    first=body.splitlines()[0].strip()
    if first.startswith(('![','|','```','~~~','[Word 图片')):return label
    first=re.sub(r'^(?:[-*+ ]+|\d+[.、)）]\s*|[一二三四五六七八九十]+[、.．]\s*)','',first).strip()
    return (first or label)[:80]


def prepare_import_items(paths):
    """Parse selected sources into editable in-memory rows; writes nothing."""
    source_paths=[];rows=[];warnings=[];errors=[]
    for raw_path in paths:
        path=Path(raw_path).resolve()
        try:
            text,notes=read_source(path)
            found_tables=tables_in(text)
            # A structured reference table should be displayed/edited as a table,
            # not mistaken for a single task named after the introductory paragraph.
            source_paths.append(str(path));warnings.extend(f'{path.name}：{x}' for x in notes)
            if found_tables:
                for table in found_tables:
                    if is_task_table(table.headers):
                        rows.extend(table_to_task_items(table,str(path),path.name))
                if not any(is_task_table(t.headers) for t in found_tables):
                    warnings.append(f'{path.name}：识别到 {len(found_tables)} 张资料表、{sum(len(t.rows) for t in found_tables)} 行；按表格归档并显示，不自动生成待办任务。')
                else:
                    warnings.append(f'{path.name}：已识别待办表格，按每一行生成可修改的任务预览。')
            else:
                parts=split_sections(text)
                for index,(section,body) in enumerate(parts):
                    if not section.strip() and not body.strip():continue
                    rows.append({
                        'source_path':str(path),'source_name':path.name,'source_index':index,
                        'title':import_task_title(section,body),'section':section or '未分类',
                        'content':body,'original_content':body,
                    })
            if sum(1 for x in rows if x['source_path']==str(path))>500:
                raise ValueError('超过 500 条任务，请分批导入。')
        except Exception as exc:
            errors.append(f'{path.name}：{exc}')
    return source_paths,rows,warnings,errors


def remap_import_images(edited,original,archived):
    """Point retained/edited Markdown image links at the archived copy."""
    old_links=re.findall(r'!\[[^\]]*\]\([^)]+\)',original)
    new_links=re.findall(r'!\[[^\]]*\]\([^)]+\)',archived)
    for old,new in zip(old_links,new_links):
        if old in edited:edited=edited.replace(old,new,1)
    placeholder='[Word 图片：导入时提取，原图保留在原文件中]'
    for new in new_links:
        if placeholder in edited:edited=edited.replace(placeholder,new,1)
    return edited


def commit_import_to_store(store,target_key,source_paths,rows):
    """Commit a reviewed import to one destination; archive originals first."""
    archived={};warnings=[];errors=[]
    for path in source_paths:
        try:
            notes,text=store.archive_source(target_key,path)
            warnings.extend(notes)
            archived[path]=split_sections(text)
        except Exception as exc:
            errors.append(f'{Path(path).name}：原文件归档失败：{exc}')
    if not archived:
        return {'archived':0,'added':0,'warnings':warnings,'errors':errors}
    added=0
    for row in rows:
        path=row['source_path']
        if path not in archived:continue
        parts=archived[path];index=row.get('source_index',0)
        if row.get('_origin_kind')=='table_row':
            archived_section=row.get('section','未分类')
            content=row.get('content','')
        else:
            if 0<=index<len(parts):archived_section,archived_body=parts[index]
            else:archived_section,archived_body=row.get('section','未分类'),row.get('original_content','')
            original=row.get('original_content','')
            content=row.get('content','')
            if content==original:content=archived_body
            else:content=remap_import_images(content,original,archived_body)
        title=(row.get('title') or '').strip() or import_task_title(row.get('section') or archived_section,content)
        section=(row.get('section') or archived_section or '未分类').strip()
        if not title and not content.strip():
            errors.append(f"{row.get('source_name','资料')}：跳过空任务")
            continue
        try:
            insert_mode=row.get('_insert_mode','chapter')
            if hasattr(store,'insert_task'):
                store.insert_task(target_key,title,section,content,chapter_end=(insert_mode!='document_end'))
            else:
                store.save_task(target_key,title,section,content)
            added+=1
        except Exception as exc:
            errors.append(f"{row.get('source_name','资料')} / {title}：任务写入失败：{exc}")
    try:store.snapshot(target_key)
    except Exception as exc:errors.append('汇总快照更新失败：'+str(exc))
    return {'archived':len(archived),'added':added,'warnings':warnings,'errors':errors}


class ImportPreviewDialog(tk.Toplevel):
    """Two-stage import review: choose destination, edit rows, then commit."""
    def __init__(self,parent,documents,current_key,source_paths,rows,warnings,on_commit):
        super().__init__(parent)
        self.parent=parent;self.documents=list(documents);self.source_paths=list(source_paths)
        self.rows=[dict(x) for x in rows];self.warnings=list(warnings);self.on_commit=on_commit
        self.doc_map={label:key for label,key in self.documents}
        self.current_index=None;self._loading=False;self._dirty=False;self._refreshing=False
        self.title('导入资料 · 检查任务并选择目标文档')
        parent.sized_dialog(self,1080,720);self.transient(parent)
        self.protocol('WM_DELETE_WINDOW',self.cancel)
        self.build(current_key)
        self.refresh_list(0 if self.rows else None)
        self.grab_set()
    def build(self,current_key):
        root=ttk.Frame(self,padding=14);root.pack(fill='both',expand=True)
        ttk.Label(root,text='导入预览',font=('Microsoft YaHei',16,'bold')).pack(anchor='w')
        ttk.Label(root,text=f'已选择 {len(self.source_paths)} 份资料，识别出 {len(self.rows)} 条待办任务。资料表格会作为可编辑表格归档；不属于待办的表格不会自动生成任务。',foreground='#64748B').pack(anchor='w',pady=(3,10))
        dest=ttk.Frame(root);dest.pack(fill='x',pady=(0,8))
        ttk.Label(dest,text='放入文档').pack(side='left',padx=(0,8))
        self.target_var=tk.StringVar()
        self.target_box=ttk.Combobox(dest,textvariable=self.target_var,state='readonly',values=[x[0] for x in self.documents],width=54)
        self.target_box.pack(side='left',fill='x',expand=True)
        for label,key in self.documents:
            if key==current_key:self.target_var.set(label);break
        if not self.target_var.get() and self.documents:self.target_var.set(self.documents[0][0])
        self.target_box.bind('<<ComboboxSelected>>',self.update_insert_chapters)
        insert=ttk.Frame(root);insert.pack(fill='x',pady=(0,8))
        ttk.Label(insert,text='插入方式').pack(side='left',padx=(0,8))
        self.insert_mode_var=tk.StringVar(value='按任务原章节插入到章节末尾')
        self.insert_mode_box=ttk.Combobox(insert,textvariable=self.insert_mode_var,state='readonly',width=30,values=['按任务原章节插入到章节末尾','全部放入指定章节','全部追加到文档末尾'])
        self.insert_mode_box.pack(side='left',padx=(0,10))
        ttk.Label(insert,text='指定章节').pack(side='left',padx=(0,6))
        self.insert_chapter_var=tk.StringVar()
        self.insert_chapter_box=ttk.Combobox(insert,textvariable=self.insert_chapter_var,width=28)
        self.insert_chapter_box.pack(side='left',fill='x',expand=True)
        self.update_insert_chapters()
        self.table_help=ttk.Frame(root);self.table_help.pack(fill='x',pady=(0,6))
        ttk.Label(self.table_help,text='资料里的表格默认只作为参考归档，不自动变成待办任务。',foreground='#52647B').pack(side='left',padx=(0,8))
        self.table_task_button=ttk.Button(self.table_help,text='按表格行生成待编辑任务（可选）',command=lambda:self.guard(self.import_table_rows))
        self.table_task_button.pack(side='left')
        self._table_rows_expanded=False
        if not any('资料表' in warning for warning in self.warnings):self.table_help.pack_forget()
        ttk.Label(root,text='在左侧选择任务，在右侧修改标题、章节和正文。点击“放入所选文档”后才会正式写入；取消不会改动文档。',foreground='#64748B',wraplength=1000).pack(anchor='w',pady=(0,8))
        if self.warnings:
            ttk.Label(root,text='资料提示：'+'；'.join(self.warnings[:3]),foreground='#9A6700',wraplength=1000).pack(anchor='w',pady=(0,8))
        footer=ttk.Frame(root);footer.pack(side='bottom',fill='x',pady=(10,0))
        self.count_label=ttk.Label(footer,text='');self.count_label.pack(side='left')
        self.make_button(footer,'取消',self.cancel,side='right',padx=(6,0))
        self.make_button(footer,'放入所选文档',self.commit,side='right')
        panes=ttk.Panedwindow(root,orient='horizontal');panes.pack(fill='both',expand=True)
        left=ttk.Frame(panes);panes.add(left,weight=2)
        ttk.Label(left,text='任务列表').pack(anchor='w',pady=(0,5))
        lf=ttk.Frame(left);lf.pack(fill='both',expand=True)
        self.task_list=ttk.Treeview(lf,columns=('title','section','source'),show='headings',selectmode='extended')
        for col,label,width in [('title','任务标题',220),('section','章节',150),('source','来源文件',125)]:
            self.task_list.heading(col,text=label);self.task_list.column(col,width=width,minwidth=70,stretch=True)
        sy=ttk.Scrollbar(lf,orient='vertical',command=self.task_list.yview);self.task_list.configure(yscrollcommand=sy.set)
        sy.pack(side='right',fill='y');self.task_list.pack(fill='both',expand=True)
        self.task_list.bind('<<TreeviewSelect>>',self.select_task)
        rowtools=ttk.Frame(left);rowtools.pack(fill='x',pady=(7,0))
        self.make_button(rowtools,'全选 / 取消全选',self.select_all_tasks,side='left')
        self.make_button(rowtools,'删除选中',self.delete_selected_tasks,side='left')
        self.make_button(rowtools,'上移',lambda:self.move_task(-1),side='left')
        self.make_button(rowtools,'下移',lambda:self.move_task(1),side='left')
        right=ttk.Frame(panes,padding=(14,0,0,0));panes.add(right,weight=3)
        ttk.Label(right,text='任务编辑',font=('Microsoft YaHei',12,'bold')).pack(anchor='w',pady=(0,8))
        ttk.Label(right,text='标题').pack(anchor='w')
        self.title_var=tk.StringVar();self.title_entry=ttk.Entry(right,textvariable=self.title_var);self.title_entry.pack(fill='x',pady=(2,8))
        ttk.Label(right,text='章节').pack(anchor='w')
        self.section_var=tk.StringVar();self.section_entry=ttk.Entry(right,textvariable=self.section_var);self.section_entry.pack(fill='x',pady=(2,8))
        ttk.Label(right,text='正文').pack(anchor='w')
        area=ttk.Frame(right);area.pack(fill='both',expand=True)
        self.body=tk.Text(area,wrap='word',undo=True,font=('Microsoft YaHei',10),padx=10,pady=8,relief='flat',bg='white',fg='#243247')
        by=ttk.Scrollbar(area,command=self.body.yview);by.pack(side='right',fill='y')
        self.body.configure(yscrollcommand=by.set);self.body.pack(fill='both',expand=True)
        self.title_var.trace_add('write',self.mark_dirty);self.section_var.trace_add('write',self.mark_dirty)
        self.body.bind('<<Modified>>',self.body_modified)
        self.make_button(right,'应用修改',self.apply_current,side='right',pady=(7,0))
    def import_table_rows(self):
        """Explicitly turn reference-table rows into editable task candidates."""
        if self._table_rows_expanded:return
        from reqdoc_source_table import tables_in
        self.apply_current()
        additions=[]
        for path in self.source_paths:
            text,_=read_source(path)
            for tab in tables_in(text):
                for i,cells in enumerate(tab.rows):
                    title=' · '.join(value for value in cells[:2] if value).strip()[:80] or f'表格第 {i+1} 行'
                    content='\n'.join(f'{header}：{value}' for header,value in zip(tab.headers,cells))
                    additions.append({'source_path':path,'source_name':Path(path).name,'source_index':-1,
                                      'title':title,'section':'表格 / '+Path(path).stem,
                                      'content':content,'original_content':content,'_origin_kind':'table_row'})
        if not additions:
            messagebox.showinfo('表格转任务','没有识别到可转成任务的 Markdown 表格。',parent=self)
            return
        if len(self.rows)+len(additions)>500:raise ValueError('一次导入最多 500 条任务，请先缩小表格范围。')
        self.rows.extend(additions)
        self._table_rows_expanded=True;self.table_task_button.configure(state='disabled')
        self.refresh_list(len(self.rows)-len(additions))
        self.parent.status.set(f'已将 {len(additions)} 条表格记录暂时转换为任务候选；尚未写入数据库。')

    def update_insert_chapters(self,event=None):
        label=self.target_var.get()
        target=self.doc_map.get(label)
        chapters=[]
        if target:
            try:chapters=sorted({(r.section or '未分类') for r in self.parent.store.tasks(target)})
            except Exception:chapters=[]
        chapters=sorted(set(chapters)|{(r.get('section') or '未分类') for r in self.rows})
        self.insert_chapter_box.configure(values=chapters)
        if self.insert_chapter_var.get() not in chapters:
            self.insert_chapter_var.set(chapters[0] if chapters else '未分类')

    def select_all_tasks(self):
        items=self.task_list.get_children()
        if not items:return
        selected=set(self.task_list.selection())
        self._refreshing=True
        if len(selected)==len(items):self.task_list.selection_remove(*items)
        else:self.task_list.selection_set(items)
        self._refreshing=False
        if self.task_list.selection():
            first=int(self.task_list.selection()[0])
            if first!=self.current_index:self.apply_current();self.load_task(first)

    def delete_selected_tasks(self):
        selected=sorted((int(x) for x in self.task_list.selection()),reverse=True)
        if not selected:return
        self.apply_current()
        for index in selected:
            if 0<=index<len(self.rows):self.rows.pop(index)
        next_index=min(min(selected),len(self.rows)-1) if self.rows else None
        self.refresh_list(next_index)

    def make_button(self,parent,label,command,**pack_options):
        button=ttk.Button(parent,text=label,command=lambda:self.guard(command))
        pack_options.setdefault('padx',3);pack_options.setdefault('pady',3)
        button.pack(**pack_options)
        return button
    def guard(self,fn):
        try:return fn()
        except Exception as exc:messagebox.showerror('导入操作未完成',str(exc),parent=self)
    def mark_dirty(self,*args):
        if not self._loading:self._dirty=True
    def body_modified(self,event=None):
        if self.body.edit_modified():
            if not self._loading:self._dirty=True
            self.body.edit_modified(False)
    def apply_current(self):
        if self.current_index is None:return
        row=self.rows[self.current_index]
        row['title']=self.title_var.get().strip()
        row['section']=self.section_var.get().strip() or '未分类'
        row['content']=self.body.get('1.0','end-1c')
        if not row['title'] and row['content'].strip():row['title']=import_task_title(row['section'],row['content'])
        self._dirty=False
        iid=str(self.current_index)
        if self.task_list.exists(iid):self.task_list.item(iid,values=(row['title'],row['section'],row['source_name']))
    def load_task(self,index):
        self.current_index=index;self._loading=True
        if index is None:
            self.title_var.set('');self.section_var.set('');self.body.delete('1.0','end')
        else:
            row=self.rows[index]
            self.title_var.set(row['title']);self.section_var.set(row['section'])
            self.body.delete('1.0','end');self.body.insert('1.0',row['content'])
        self.body.edit_modified(False);self._loading=False;self._dirty=False
    def refresh_list(self,select_index=None):
        self._refreshing=True;self.current_index=None;self.task_list.delete(*self.task_list.get_children())
        for i,row in enumerate(self.rows):
            self.task_list.insert('','end',iid=str(i),values=(row['title'],row['section'],row['source_name']))
        self._refreshing=False
        self.count_label.configure(text=f'{len(self.rows)} 条待导入任务 · {len(self.source_paths)} 份原始资料')
        if select_index is not None and 0<=select_index<len(self.rows):
            self.task_list.selection_set(str(select_index));self.task_list.see(str(select_index));self.load_task(select_index)
        else:self.load_task(None)
    def select_task(self,event=None):
        if self._refreshing:return
        selected=self.task_list.selection()
        if not selected:return
        index=int(selected[0])
        if index==self.current_index:return
        self.apply_current();self.load_task(index)
    def delete_task(self):
        self.delete_selected_tasks()
    def move_task(self,delta):
        selected=self.task_list.selection()
        if not selected:return
        index=int(selected[0]);new=max(0,min(len(self.rows)-1,index+delta))
        if new==index:return
        self.apply_current();self.rows[index],self.rows[new]=self.rows[new],self.rows[index]
        self.refresh_list(new)
    def commit(self):
        self.apply_current()
        for i,row in enumerate(self.rows):
            if not row['title'].strip() and not row['content'].strip():
                raise ValueError(f'第 {i+1} 条任务标题和正文都为空。')
        target=self.doc_map.get(self.target_var.get())
        if not target:raise ValueError('请先选择要放入的文档。')
        mode=self.insert_mode_var.get()
        for row in self.rows:
            if mode=='全部放入指定章节':row['section']=self.insert_chapter_var.get().strip() or '未分类'
            row['_insert_mode']='document_end' if mode=='全部追加到文档末尾' else 'chapter'
        result=self.on_commit(target,self.source_paths,self.rows)
        if not result or not result.get('archived'):
            details='\n'.join(result.get('errors',[])) if result else '没有资料成功归档。'
            raise ValueError(details or '没有资料成功归档。')
        self.destroy()
    def cancel(self):self.destroy()


# Test 3: compact document/task workspace with explicit import destination confirmation.
_section_parser = split_sections
def split_sections(text):
    result=[]
    for section,body in _section_parser(text):
        lines=body.splitlines();starts=[];fence=None
        for i,line in enumerate(lines):
            m=re.match(r'^\s*(`{3,}|~{3,})',line)
            if m:
                if fence is None:fence=m[1][0]
                elif m[1][0]==fence:fence=None
                continue
            if fence:continue
            if re.match(r'^(?:[-*+]\s+|\d+[.、)）]\s*|[一二三四五六七八九十]+[、.．]\s*|[^\s:：]{2,30}[:：]\s*(?:/Script/|/Game/))',line):starts.append(i)
        if len(starts)>=2:
            if starts[0]>0 and '\n'.join(lines[:starts[0]]).strip():
                # Keep introductory context with the first task.
                starts[0]=0
            for a,b in zip(starts,starts[1:]+[len(lines)]):
                item='\n'.join(lines[a:b]).strip()
                if item:result.append((section,item))
        elif section=='导入内容' and not re.search(r'```|~~~|^\s*\|',body,re.M):
            blocks=[b.strip() for b in re.split(r'\n\s*\n',body) if b.strip()]
            result.extend((section,b) for b in blocks)
        else:result.append((section,body))
    return result

# Give each imported task a useful title instead of repeating the chapter.
_save_original = Store.save_task
def _save_import_title(self,key,title,section,content,rid=None):
    if rid is None and title==section.split(' / ')[-1] and content.strip():
        first=content.strip().splitlines()[0]
        if first.startswith(('![','|','```','~~~')):first=section.split(' / ')[-1]
        first=re.sub(r'^(?:[-*+]\s+|\d+[.、)）]\s*)','',first)
        title=first[:80]
    return _save_original(self,key,title,section,content,rid)
Store.save_task=_save_import_title

class CompactStudioV4(Studio):
    """测试版 4：旧版界面，保留其成熟的任务和导入组件供新版继承。"""
    NAV_ITEMS = [
        ('工作台', 'home'), ('项目', 'projects'), ('文档', 'documents'),
        ('导入中心', 'import'), ('任务整理', 'tasks'), ('进度统计', 'progress'),
        ('导出', 'export'), ('设置', 'settings'),
    ]

    def build(self):
        self.title('reqdoc · 文档与任务工作台 — 测试版 4')
        self.geometry(f'{min(1480,self.winfo_screenwidth()-40)}x{min(900,self.winfo_screenheight()-80)}')
        self.minsize(1120, 700)
        self.checked_ids=set();self.visible_ids=[];self._refreshing_tasks=False
        self.insert_after_id=None;self.progress_chapter_map={}
        self.configure(bg='#F2F5FA')

        shell=tk.Frame(self,bg='#F2F5FA')
        shell.pack(fill='both',expand=True)
        self.nav=tk.Frame(shell,bg='#173252',width=158)
        self.nav.pack(side='left',fill='y');self.nav.pack_propagate(False)
        tk.Label(self.nav,text='reqdoc',font=('Microsoft YaHei',23,'bold'),bg='#173252',fg='white').pack(anchor='w',padx=18,pady=(22,0))
        tk.Label(self.nav,text='文档与任务',font=('Microsoft YaHei',10),bg='#173252',fg='#C7D7EB').pack(anchor='w',padx=20,pady=(0,22))
        self.nav_buttons={}
        for label,page in self.NAV_ITEMS:
            b=tk.Button(self.nav,text='  '+label,anchor='w',font=('Microsoft YaHei',10),
                        bg='#173252',fg='white',activebackground='#2B68B7',activeforeground='white',
                        relief='flat',bd=0,padx=10,pady=10,cursor='hand2',
                        command=lambda p=page:self.show_page(p))
            b.pack(fill='x',padx=9,pady=2);self.nav_buttons[page]=b
        tk.Frame(self.nav,bg='#45617F',height=1).pack(fill='x',padx=16,pady=(18,12))
        tk.Label(self.nav,text='本地数据 · 自动草稿',font=('Microsoft YaHei',8),bg='#173252',fg='#AFC1D7').pack(side='bottom',anchor='w',padx=18,pady=16)

        body=ttk.Frame(shell);body.pack(side='left',fill='both',expand=True)
        self.project_panel=ttk.Frame(body,width=238,padding=(12,14))
        self.project_panel.pack(side='left',fill='y');self.project_panel.pack_propagate(False)
        ttk.Label(self.project_panel,text='项目与文档',font=('Microsoft YaHei',12,'bold')).pack(anchor='w',pady=(0,10))
        create=ttk.Frame(self.project_panel);create.pack(fill='x',pady=(0,8))
        ttk.Button(create,text='＋ 项目',command=lambda:self.guard(self.new_project)).pack(side='left',fill='x',expand=True,padx=(0,4))
        ttk.Button(create,text='＋ 文档',command=lambda:self.guard(self.new_document)).pack(side='left',fill='x',expand=True,padx=(4,0))
        treeframe=ttk.Frame(self.project_panel);treeframe.pack(fill='both',expand=True)
        self.tree=ttk.Treeview(treeframe,show='tree',selectmode='browse')
        tree_y=ttk.Scrollbar(treeframe,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_y.set);tree_y.pack(side='right',fill='y');self.tree.pack(fill='both',expand=True)
        self.tree.bind('<<TreeviewSelect>>',self.on_tree)
        ttk.Button(self.project_panel,text='重命名所选项目 / 文档',command=lambda:self.guard(self.rename_selected)).pack(fill='x',pady=(8,0))

        self.pages_host=ttk.Frame(body,padding=(16,14))
        self.pages_host.pack(side='left',fill='both',expand=True)
        self.pages={}
        for name in ('home','projects','documents','import','tasks','progress','export','settings'):
            frame=ttk.Frame(self.pages_host)
            self.pages[name]=frame
        self.build_home_page()
        self.build_projects_page()
        self.build_documents_page()
        self.build_import_page()
        self.build_tasks_page()
        self.build_progress_page()
        self.build_export_page()
        self.build_settings_page()
        self.build_aux()
        self.status=tk.StringVar(value='就绪 · 数据保存在本机')
        ttk.Label(self,textvariable=self.status,padding=(12,6)).pack(fill='x')
        self.show_page('projects')
        self._refresh_tree_after_id=self.after(50,self.refresh_tree)

    def show_page(self,name):
        if name not in self.pages:return
        for page in self.pages.values():page.pack_forget()
        self.pages[name].pack(fill='both',expand=True)
        for page,button in self.nav_buttons.items():
            button.configure(bg='#2867B8' if page==name else '#173252')
        if name=='home':self.refresh_home()
        elif name=='tasks':self.refresh_tasks()
        elif name=='progress':self.refresh_progress()
        elif name=='projects':self.refresh_projects_page()
        elif name=='documents':self.refresh_documents_page()
        elif name=='import':self.refresh_import_page()
        elif name=='export':self.refresh_export_page()

    def page_title(self,parent,title,subtitle=''):
        ttk.Label(parent,text=title,font=('Microsoft YaHei',17,'bold')).pack(anchor='w',pady=(0,3))
        if subtitle:ttk.Label(parent,text=subtitle,foreground='#64748B',wraplength=1050).pack(anchor='w',pady=(0,12))

    def action_card(self,parent,title,description,command):
        card=tk.Frame(parent,bg='#EAF2FF',highlightbackground='#C8D9F2',highlightthickness=1,cursor='hand2')
        card.pack(side='left',fill='both',expand=True,padx=(0,10),ipady=12)
        title_label=tk.Label(card,text=title,font=('Microsoft YaHei',13,'bold'),bg='#EAF2FF',fg='#163B70',anchor='w')
        title_label.pack(fill='x',padx=16,pady=(8,4))
        desc=tk.Label(card,text=description,font=('Microsoft YaHei',9),bg='#EAF2FF',fg='#536A87',anchor='w',justify='left',wraplength=360)
        desc.pack(fill='x',padx=16,pady=(0,8))
        for widget in (card,title_label,desc):widget.bind('<Button-1>',lambda e,fn=command:self.guard(fn))
        return card

    def build_home_page(self):
        page=self.pages['home']
        self.heading=ttk.Label(page,text='选择文档，开始整理任务',font=('Microsoft YaHei',18,'bold'))
        self.heading.pack(anchor='w',pady=(2,2))
        self.breadcrumb=ttk.Label(page,text='先在左侧新建项目和文档，或选择已有文档。',foreground='#64748B')
        self.breadcrumb.pack(anchor='w',pady=(0,18))
        cards=ttk.Frame(page);cards.pack(fill='x',pady=(0,20))
        self.action_card(cards,'✎  手写录入','逐条创建任务，填写标题、章节和正文。适合少量任务或精确补充。',self.start_manual)
        self.action_card(cards,'↓  导入资料','从 Word、Markdown 或 TXT 识别任务，先预览、修改，再选择文档写入。',self.import_dialog)
        ttk.Label(page,text='当前文档概览',font=('Microsoft YaHei',12,'bold')).pack(anchor='w',pady=(0,8))
        stats=ttk.Frame(page);stats.pack(fill='x',pady=(0,14))
        self.home_total=tk.StringVar(value='0');self.home_done=tk.StringVar(value='0');self.home_open=tk.StringVar(value='0');self.home_rate=tk.StringVar(value='0%')
        for label,var in [('任务总数',self.home_total),('已完成',self.home_done),('未完成',self.home_open),('完成率',self.home_rate)]:
            box=ttk.LabelFrame(stats,text=label,padding=12);box.pack(side='left',fill='both',expand=True,padx=(0,8))
            ttk.Label(box,textvariable=var,font=('Microsoft YaHei',18,'bold')).pack(anchor='center')
        ttk.Label(page,text='常用操作',font=('Microsoft YaHei',12,'bold')).pack(anchor='w',pady=(2,8))
        quick=ttk.Frame(page);quick.pack(fill='x')
        ttk.Button(quick,text='打开任务整理',command=lambda:self.show_page('tasks')).pack(side='left',padx=(0,8))
        ttk.Button(quick,text='查看完成进度',command=lambda:self.show_page('progress')).pack(side='left',padx=(0,8))
        ttk.Button(quick,text='查看文档汇总',command=lambda:self.guard(self.show_preview)).pack(side='left')
        self.home_hint=ttk.Label(page,text='尚未选择文档。请先新建项目和文档。',foreground='#64748B')
        self.home_hint.pack(anchor='w',pady=(22,0))

    def build_projects_page(self):
        page=self.pages['projects'];self.page_title(page,'项目管理','创建项目并在项目下组织多份文档。')
        bar=ttk.Frame(page);bar.pack(fill='x',pady=(0,10))
        ttk.Button(bar,text='＋ 新建项目',command=lambda:self.guard(self.new_project)).pack(side='left',padx=(0,8))
        ttk.Button(bar,text='＋ 在所选项目中新建文档',command=lambda:self.guard(self.new_document)).pack(side='left')
        self.project_list=ttk.Treeview(page,columns=('name','documents'),show='headings',height=14)
        self.project_list.heading('name',text='项目名称');self.project_list.heading('documents',text='文档数量')
        self.project_list.column('name',width=380);self.project_list.column('documents',width=120,anchor='center')
        self.project_list.pack(fill='both',expand=True)
        self.project_list.bind('<Double-1>',self.open_project_from_list)

    def build_documents_page(self):
        page=self.pages['documents'];self.page_title(page,'文档管理','选择文档后，可以手写录入、导入资料、插入任务或查看完成情况。')
        bar=ttk.Frame(page);bar.pack(fill='x',pady=(0,10))
        ttk.Button(bar,text='＋ 新建文档',command=lambda:self.guard(self.new_document)).pack(side='left',padx=(0,8))
        ttk.Button(bar,text='打开所选文档',command=lambda:self.guard(self.open_selected_document)).pack(side='left')
        self.document_list=ttk.Treeview(page,columns=('project','document','tasks','done'),show='headings',height=16)
        for col,label,width in [('project','所属项目',200),('document','文档名称',300),('tasks','任务数',90),('done','已完成',90)]:
            self.document_list.heading(col,text=label);self.document_list.column(col,width=width,anchor='w' if col in ('project','document') else 'center')
        self.document_list.pack(fill='both',expand=True)
        self.document_list.bind('<Double-1>',lambda e:self.guard(self.open_selected_document))

    def build_import_page(self):
        page=self.pages['import'];self.page_title(page,'导入中心','导入文件后选择形成可编辑表格/文档，或直接形成任务；原始文件只读归档。')
        panel=ttk.LabelFrame(page,text='资料导入',padding=18);panel.pack(fill='x',pady=(0,14))
        ttk.Label(panel,text='支持格式：Word（.docx）、Markdown（.md / .markdown）、文本（.txt）',font=('Microsoft YaHei',10)).pack(anchor='w',pady=(0,8))
        ttk.Label(panel,text='支持一次选择多份资料；导入时选择形成工作文档/表格，或从内容生成任务。',foreground='#64748B').pack(anchor='w',pady=(0,12))
        ttk.Button(panel,text='选择资料并导入',command=lambda:self.guard(self.import_dialog)).pack(anchor='w')
        ttk.Label(page,text='最近归档的原始资料',font=('Microsoft YaHei',12,'bold')).pack(anchor='w',pady=(8,6))
        self.import_sources=ttk.Treeview(page,columns=('name','time'),show='headings',height=10)
        self.import_sources.heading('name',text='原始文件');self.import_sources.heading('time',text='归档时间')
        self.import_sources.column('name',width=500);self.import_sources.column('time',width=180)
        self.import_sources.pack(fill='both',expand=True)
        self.import_sources.bind('<Double-1>',lambda e:self.guard(self.open_import_source))

    def build_tasks_page(self):
        page=self.pages['tasks']
        self.tasks_heading=ttk.Label(page,text='任务整理',font=('Microsoft YaHei',17,'bold'));self.tasks_heading.pack(anchor='w',pady=(0,3))
        self.tasks_subtitle=ttk.Label(page,text='选择任务进行编辑，或使用全选和批量操作。',foreground='#64748B');self.tasks_subtitle.pack(anchor='w',pady=(0,10))
        toolbar=ttk.Frame(page);toolbar.pack(fill='x',pady=(0,8))
        self.query=tk.StringVar();ttk.Entry(toolbar,textvariable=self.query).pack(side='left',fill='x',expand=True,padx=(0,8))
        self.filter=tk.StringVar(value='全部')
        cb=ttk.Combobox(toolbar,textvariable=self.filter,values=['全部','未完成','已完成','回收站'],width=10,state='readonly');cb.pack(side='left',padx=(0,8))
        cb.bind('<<ComboboxSelected>>',lambda e:(self.checked_ids.clear(),self.refresh_tasks()))
        ttk.Button(toolbar,text='＋ 新增任务',command=lambda:self.guard(self.new_task)).pack(side='left',padx=(0,8))
        ttk.Button(toolbar,text='在选中任务后插入',command=lambda:self.guard(self.insert_after_selected)).pack(side='left')

        panes=ttk.Panedwindow(page,orient='horizontal');panes.pack(fill='both',expand=True)
        left=ttk.Frame(panes);panes.add(left,weight=3)
        list_frame=ttk.Frame(left);list_frame.pack(fill='both',expand=True)
        self.list=ttk.Treeview(list_frame,columns=('check','state','title','chapter'),show='headings',selectmode='extended')
        for col,label,width in [('check','选择',52),('state','状态',76),('title','任务标题',290),('chapter','所属章节',190)]:
            self.list.heading(col,text=label);self.list.column(col,width=width,minwidth=55,stretch=col in ('title','chapter'),anchor='center' if col in ('check','state') else 'w')
        sy=ttk.Scrollbar(list_frame,orient='vertical',command=self.list.yview);self.list.configure(yscrollcommand=sy.set)
        sy.pack(side='right',fill='y');self.list.pack(fill='both',expand=True)
        self.list.bind('<Button-1>',self.task_cell_click)
        self.list.bind('<<TreeviewSelect>>',self.on_task_selected)
        self.list.bind('<Double-1>',lambda e:self.guard(self.edit_selected))
        self.query.trace_add('write',lambda *a:(self.checked_ids.clear(),self.refresh_tasks()))
        actions=ttk.Frame(left);actions.pack(fill='x',pady=(8,0))
        ttk.Button(actions,text='全选 / 取消全选',command=self.select_all_visible).pack(side='left',padx=(0,6))
        ttk.Button(actions,text='清除选择',command=self.clear_selection).pack(side='left',padx=(0,6))
        ttk.Button(actions,text='标记完成',command=lambda:self.guard(lambda:self.mark(True))).pack(side='left',padx=(0,6))
        ttk.Button(actions,text='标记未完成',command=lambda:self.guard(lambda:self.mark(False))).pack(side='left',padx=(0,6))
        ttk.Button(actions,text='批量改章节',command=lambda:self.guard(self.change_chapter)).pack(side='left',padx=(0,6))
        ttk.Button(actions,text='移入回收站',command=lambda:self.guard(self.trash_tasks)).pack(side='left')
        self.count=ttk.Label(actions,text='0 条任务');self.count.pack(side='right')

        editor=ttk.LabelFrame(panes,text='任务编辑',padding=12);panes.add(editor,weight=4)
        self.editor=editor
        self.editlabel=ttk.Label(editor,text='新任务',font=('Microsoft YaHei',12,'bold'));self.editlabel.pack(anchor='w',pady=(0,8))
        ttk.Label(editor,text='任务标题').pack(anchor='w')
        self.titlevar=tk.StringVar();ttk.Entry(editor,textvariable=self.titlevar).pack(fill='x',pady=(3,8))
        ttk.Label(editor,text='所属章节（可用 / 表示层级）').pack(anchor='w')
        self.chaptervar=tk.StringVar(value='未分类');self.chapterbox=ttk.Combobox(editor,textvariable=self.chaptervar);self.chapterbox.pack(fill='x',pady=(3,8))
        insertrow=ttk.Frame(editor);insertrow.pack(fill='x',pady=(0,8))
        ttk.Label(insertrow,text='新任务插入位置').pack(side='left',padx=(0,8))
        self.insert_mode_var=tk.StringVar(value='章节末尾')
        self.insert_mode_box=ttk.Combobox(insertrow,textvariable=self.insert_mode_var,values=['章节末尾','选中任务之后','文档末尾'],state='readonly',width=18)
        self.insert_mode_box.pack(side='left')
        self.done_var=tk.BooleanVar(value=False)
        ttk.Checkbutton(editor,text='此任务已完成',variable=self.done_var,command=self.changed).pack(anchor='w',pady=(0,8))
        ttk.Label(editor,text='任务正文').pack(anchor='w')
        self.body=self.text_box(editor);self.body.bind('<<Modified>>',self.modified)
        self.body.bind('<Control-v>',self.paste_event);self.body.bind('<Control-V>',self.paste_event)
        for var in (self.titlevar,self.chaptervar):var.trace_add('write',self.changed)
        self.done_var.trace_add('write',self.changed)
        editactions=ttk.Frame(editor);editactions.pack(fill='x',pady=(8,0))
        ttk.Button(editactions,text='保存任务',command=lambda:self.guard(self.save_task)).pack(side='left',padx=(0,6))
        ttk.Button(editactions,text='插入图片到光标处',command=lambda:self.guard(self.add_image)).pack(side='left',padx=(0,6))
        ttk.Button(editactions,text='粘贴图片到光标处',command=lambda:self.guard(self.paste_image)).pack(side='left',padx=(0,6))
        ttk.Button(editactions,text='预览正文',command=lambda:self.guard(self.preview_task_body)).pack(side='left',padx=(0,6))
        ttk.Button(editactions,text='取消本次编辑',command=lambda:self.guard(self.cancel_edit)).pack(side='right')

    def build_progress_page(self):
        page=self.pages['progress'];self.page_title(page,'完成进度','按文档和章节查看任务完成情况；可以直接在下方切换任务状态。')
        cards=ttk.Frame(page);cards.pack(fill='x',pady=(0,10))
        self.progress_total=tk.StringVar(value='0');self.progress_done=tk.StringVar(value='0');self.progress_open=tk.StringVar(value='0');self.progress_rate=tk.StringVar(value='0%')
        for label,var in [('全部任务',self.progress_total),('已完成',self.progress_done),('未完成',self.progress_open),('完成率',self.progress_rate)]:
            box=ttk.LabelFrame(cards,text=label,padding=10);box.pack(side='left',fill='both',expand=True,padx=(0,8))
            ttk.Label(box,textvariable=var,font=('Microsoft YaHei',17,'bold')).pack(anchor='center')
        self.progress_bar=ttk.Progressbar(page,orient='horizontal',mode='determinate',maximum=100);self.progress_bar.pack(fill='x',pady=(0,12))
        split=ttk.Panedwindow(page,orient='vertical');split.pack(fill='both',expand=True)
        chapter_box=ttk.LabelFrame(split,text='章节完成情况',padding=8);split.add(chapter_box,weight=2)
        self.progress_chapters=ttk.Treeview(chapter_box,columns=('chapter','total','done','pending','rate'),show='headings',height=7)
        for col,label,width in [('chapter','章节',360),('total','任务总数',100),('done','已完成',100),('pending','未完成',100),('rate','完成率',100)]:
            self.progress_chapters.heading(col,text=label);self.progress_chapters.column(col,width=width,anchor='w' if col=='chapter' else 'center')
        self.progress_chapters.pack(fill='both',expand=True)
        self.progress_chapters.bind('<<TreeviewSelect>>',lambda e:self.refresh_progress_tasks())
        task_box=ttk.LabelFrame(split,text='章节内任务（点击状态可直接切换）',padding=8);split.add(task_box,weight=3)
        self.progress_tasks=ttk.Treeview(task_box,columns=('state','title','chapter'),show='headings',selectmode='extended',height=8)
        for col,label,width in [('state','状态',100),('title','任务标题',420),('chapter','章节',250)]:
            self.progress_tasks.heading(col,text=label);self.progress_tasks.column(col,width=width,anchor='w')
        self.progress_tasks.pack(fill='both',expand=True)
        self.progress_tasks.bind('<Button-1>',self.progress_task_click)
        self.progress_tasks.bind('<Double-1>',lambda e:self.guard(self.edit_progress_task))
        tools=ttk.Frame(task_box);tools.pack(fill='x',pady=(7,0))
        ttk.Button(tools,text='标记完成',command=lambda:self.guard(lambda:self.mark_progress(True))).pack(side='left',padx=(0,6))
        ttk.Button(tools,text='标记未完成',command=lambda:self.guard(lambda:self.mark_progress(False))).pack(side='left')

    def build_export_page(self):
        page=self.pages['export'];self.page_title(page,'文档导出','导出前可以先预览文档汇总。只导出当前已保存的任务内容。')
        box=ttk.LabelFrame(page,text='导出当前文档',padding=18);box.pack(fill='x')
        ttk.Button(box,text='预览文档汇总',command=lambda:self.guard(self.show_preview)).pack(anchor='w',pady=5)
        ttk.Button(box,text='导出 Word 文档',command=lambda:self.guard(lambda:self.export('docx'))).pack(anchor='w',pady=5)
        ttk.Button(box,text='导出 Markdown 与图片包',command=lambda:self.guard(lambda:self.export('zip'))).pack(anchor='w',pady=5)
        ttk.Label(box,text='导出内容会按照当前任务顺序和章节组织。原始导入文件不会被覆盖。',foreground='#64748B').pack(anchor='w',pady=(10,0))

    def build_settings_page(self):
        page=self.pages['settings'];self.page_title(page,'设置与数据维护','本程序在本机保存项目、文档、任务、导入原件和备份。')
        box=ttk.LabelFrame(page,text='数据维护',padding=18);box.pack(fill='x')
        ttk.Button(box,text='备份全部数据',command=lambda:self.guard(self.backup)).pack(anchor='w',pady=5)
        ttk.Button(box,text='恢复为独立副本',command=lambda:self.guard(self.restore_backup)).pack(anchor='w',pady=5)
        ttk.Button(box,text='打开文档数据目录',command=lambda:self.guard(lambda:self.open_path(self.store.settings.docs_dir))).pack(anchor='w',pady=5)
        ttk.Button(box,text='查看原始资料归档',command=lambda:self.guard(lambda:self.open_aux(self.sourcetab))).pack(anchor='w',pady=5)
        ttk.Button(box,text='查看文档快照',command=lambda:self.guard(lambda:self.open_aux(self.historytab))).pack(anchor='w',pady=5)
        ttk.Button(box,text='使用说明',command=lambda:self.guard(self.help)).pack(anchor='w',pady=5)

    def sized_dialog(self,win,w,h):
        sw=self.winfo_screenwidth();sh=self.winfo_screenheight();w=min(w,sw-60);h=min(h,sh-100)
        win.geometry(f'{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//2)}');win.minsize(min(560,w),min(400,h))

    def build_aux(self):
        self.aux=tk.Toplevel(self);self.aux.withdraw();self.aux.title('文档附属工具');self.aux.transient(self);self.sized_dialog(self.aux,900,700)
        self.aux.protocol('WM_DELETE_WINDOW',lambda:(self.persist_notes(),self.aux.withdraw()))
        self.tabs=ttk.Notebook(self.aux);self.tabs.pack(fill='both',expand=True,padx=12,pady=12)
        self.tasktab=ttk.Frame(self.tabs);self.tabs.add(self.tasktab,text='任务');self.tabs.hide(self.tasktab)
        self.notetab=ttk.Frame(self.tabs,padding=12);self.tabs.add(self.notetab,text='文档说明')
        ttk.Label(self.notetab,text='前言（任务之前）').pack(anchor='w');self.intro=self.text_box(self.notetab)
        ttk.Label(self.notetab,text='补充说明（任务之后）').pack(anchor='w');self.outro=self.text_box(self.notetab)
        for t in (self.intro,self.outro):t.bind('<<Modified>>',self.note_modified)
        self.previewtab=ttk.Frame(self.tabs,padding=8);self.tabs.add(self.previewtab,text='汇总预览');self.onlyopen=tk.BooleanVar(value=False)
        ttk.Checkbutton(self.previewtab,text='只汇总未完成任务（也用于导出）',variable=self.onlyopen,command=self.refresh_preview).pack(anchor='w')
        self.preview=MarkdownView(self.previewtab,self.theme);self.preview.pack(fill='both',expand=True)
        self.sourcetab=ttk.Frame(self.tabs,padding=8);self.tabs.add(self.sourcetab,text='原始资料')
        foot=ttk.Frame(self.sourcetab);foot.pack(side='bottom',fill='x')
        self.button(foot,'打开原文件',self.open_source);self.button(foot,'查看提取文本',self.view_source)
        self.sourceslist=ttk.Treeview(self.sourcetab,columns=('name','time'),show='headings')
        self.sourceslist.heading('name',text='原始文件');self.sourceslist.heading('time',text='归档时间');self.sourceslist.pack(fill='both',expand=True)
        self.historytab=ttk.Frame(self.tabs,padding=8);self.tabs.add(self.historytab,text='文档快照')
        foot=ttk.Frame(self.historytab);foot.pack(side='bottom',fill='x');self.button(foot,'查看快照',self.view_history)
        self.history=ttk.Treeview(self.historytab,columns=('id','time'),show='headings')
        self.history.heading('id',text='版本');self.history.heading('time',text='时间');self.history.pack(fill='both',expand=True)
        self.tabs.bind('<<NotebookTabChanged>>',lambda e:self.tab_changed())

    def refresh_tree(self,select=None):
        super().refresh_tree(select)
        if hasattr(self,'project_list'):self.refresh_projects_page()
        if hasattr(self,'document_list'):self.refresh_documents_page()

    def on_tree(self,e=None):
        sel=self.tree.selection()
        if not sel:return
        item=sel[0]
        if item.startswith('d:'):
            key=item[2:]
            if key!=self.key:self.guard(lambda:self.load_doc(key))
            self.show_page('home')
        elif item.startswith('p:'):
            self.show_page('projects')
            self.status.set('已选择项目；可以在此项目下新建文档。')

    def load_doc(self,key):
        super().load_doc(key)
        doc=self.s.document(key)
        self.breadcrumb.configure(text=f'项目：{doc.project_name}   /   文档：{doc.name}')
        self.tasks_heading.configure(text=f'任务整理  /  {doc.name}')
        self.tasks_subtitle.configure(text='可全选、批量标记完成、修改章节；也可以在已有章节中插入新任务。')
        self.refresh_home();self.refresh_progress();self.refresh_import_page()

    def new_project(self):
        name=simpledialog.askstring('新建项目','请输入项目名称',parent=self)
        if name:
            project=self.s.create_project_group(name.strip())
            self.refresh_tree();self.tree.selection_set('p:'+str(project.id));self.show_page('projects')
            self.status.set('项目已创建。下一步请在项目中创建文档。')

    def new_document(self):
        try:project_name=self.selected_project()
        except Exception:
            messagebox.showinfo('新建文档','请先在左侧选择一个项目；如果还没有项目，请先新建项目。',parent=self);return
        name=simpledialog.askstring('新建文档','请输入文档名称',parent=self)
        if name:
            doc=self.s.create_document(project_name,name.strip())
            self.refresh_tree(doc.key);self.load_doc(doc.key);self.show_page('home')
            self.status.set('文档已创建。请选择“手写录入”或“导入资料”开始添加任务。')

    def open_project_from_list(self,event=None):
        sel=self.project_list.selection()
        if not sel:return
        project=self.project_list.item(sel[0],'values')[0]
        for item in self.tree.get_children():
            if self.tree.item(item,'text')==project:
                self.tree.selection_set(item);self.tree.see(item);break

    def open_selected_document(self):
        sel=self.document_list.selection()
        if not sel:raise ValueError('请先选择一份文档。')
        project,name=self.document_list.item(sel[0],'values')[:2]
        for p in self.tree.get_children():
            if self.tree.item(p,'text')==project:
                for d in self.tree.get_children(p):
                    if self.tree.item(d,'text')==name:
                        key=d[2:] if d.startswith('d:') else self.tree.item(d,'values')[1]
                        if d.startswith('d:'):key=d[2:]
                        self.tree.selection_set(d);self.tree.see(d);self.load_doc(key);self.show_page('home');return
        raise ValueError('没有找到对应文档，请刷新后重试。')

    def refresh_projects_page(self):
        if not hasattr(self,'project_list'):return
        self.project_list.delete(*self.project_list.get_children())
        for p in self.s.project_groups():
            docs=self.s.documents_for(p.name)
            self.project_list.insert('','end',iid='project-'+str(p.id),values=(p.name,len(docs)))

    def refresh_documents_page(self):
        if not hasattr(self,'document_list'):return
        self.document_list.delete(*self.document_list.get_children())
        for p in self.s.project_groups():
            for d in self.s.documents_for(p.name):
                rows=self.store.tasks(d.key);done=sum(bool(r.done) for r in rows)
                self.document_list.insert('','end',iid='document-'+d.key,values=(p.name,d.name,len(rows),done))

    def refresh_import_page(self):
        if not hasattr(self,'import_sources'):return
        self.import_sources.delete(*self.import_sources.get_children())
        if self.key:
            for sid,name,path,body,time in self.store.sources(self.key):
                self.import_sources.insert('','end',iid=sid,values=(name,time[:19]))

    def open_import_source(self):
        sel=self.import_sources.selection()
        if not sel:raise ValueError('请先选择一份已归档的原始资料。')
        row=next(r for r in self.store.sources(self.key) if r[0]==sel[0])
        self.open_path(self.store.settings.docs_dir/row[2])

    def refresh_export_page(self):
        pass

    def refresh_home(self):
        if not hasattr(self,'home_total'):return
        if not self.key:
            self.home_total.set('0');self.home_done.set('0');self.home_open.set('0');self.home_rate.set('0%')
            self.home_hint.configure(text='尚未选择文档。请先在左侧创建项目和文档。')
            return
        rows=self.store.tasks(self.key);total=len(rows);done=sum(bool(r.done) for r in rows);pending=total-done
        self.home_total.set(str(total));self.home_done.set(str(done));self.home_open.set(str(pending));self.home_rate.set(f'{done/total:.0%}' if total else '0%')
        self.home_hint.configure(text=f'当前文档共有 {total} 条任务，已完成 {done} 条，未完成 {pending} 条。')
        doc=self.s.document(self.key);self.heading.configure(text=doc.name)
        self.breadcrumb.configure(text=f'项目：{doc.project_name}   /   文档：{doc.name}')

    def refresh_tasks(self):
        if not hasattr(self,'list'):return
        keep=self.list.selection();self._refreshing_tasks=True;self.list.delete(*self.list.get_children());self.visible_ids=[]
        if self.key:
            is_trash=self.filter.get()=='回收站'
            rows=self.store.tasks(self.key,trash=is_trash);query=self.query.get().casefold()
            for r in rows:
                if query and query not in (r.title+' '+r.content+' '+r.section).casefold():continue
                if self.filter.get()=='未完成' and r.done:continue
                if self.filter.get()=='已完成' and not r.done:continue
                self.visible_ids.append(r.id)
                self.list.insert('','end',iid=str(r.id),values=('☑' if r.id in self.checked_ids else '☐','已完成' if r.done else '未完成',r.title,r.section))
        keep=[x for x in keep if self.list.exists(x)]
        if keep:self.list.selection_set(keep)
        self._refreshing_tasks=False
        if self.key:
            self.chapterbox.configure(values=sorted({r.section or '未分类' for r in self.store.tasks(self.key)}))
        self.update_count();self.refresh_home()
        # 搜索/筛选任务时不重复扫描全部项目；统计和文档列表在各自页面打开时刷新。
        if hasattr(self,'progress_chapters') and self.pages['progress'].winfo_manager():self.refresh_progress()
        if hasattr(self,'document_list') and self.pages['documents'].winfo_manager():self.refresh_documents_page()

    def update_count(self):
        if not hasattr(self,'count'):return
        selected=len(self.checked_ids) if self.checked_ids else len(self.list.selection())
        self.count.configure(text=f'{len(self.list.get_children())} 条显示 · 已选 {selected} 条')

    def task_cell_click(self,event):
        row=self.list.identify_row(event.y);column=self.list.identify_column(event.x)
        if not row:return
        rid=int(row)
        if column=='#1':
            if rid in self.checked_ids:self.checked_ids.remove(rid)
            else:self.checked_ids.add(rid)
            values=list(self.list.item(row,'values'));values[0]='☑' if rid in self.checked_ids else '☐';self.list.item(row,values=values)
            self.update_count();return 'break'
        if column=='#2' and self.key:
            task=self.s.get_requirement(rid);new_done=not bool(task.done);self.s.set_done(rid,new_done)
            if self.rid==rid:
                self.loading=True;self.done_var.set(new_done);self.loading=False
            self.store.snapshot(self.key);self.refresh_tasks();self.status.set('任务完成状态已切换。');return 'break'

    def select_all_visible(self):
        ids=set(self.visible_ids)
        if ids and ids.issubset(self.checked_ids):self.checked_ids.difference_update(ids)
        else:self.checked_ids.update(ids)
        self.refresh_tasks();self.update_count()

    def clear_selection(self):
        self.checked_ids.clear()
        if self.list.selection():self.list.selection_remove(*self.list.selection())
        self.refresh_tasks();self.update_count()

    def selected_ids(self):
        if self.checked_ids:return sorted(self.checked_ids)
        return [int(x) for x in self.list.selection()]

    def on_task_selected(self,event=None):
        if self._refreshing_tasks:return
        selected=self.list.selection()
        if len(selected)==1:
            rid=int(selected[0])
            if rid!=self.rid:self.guard(lambda:self.load_editor(rid))
        self.update_count()

    def load_editor(self,rid):
        super().load_editor(rid)
        draft=self.store.draft(self.draft_key());row=self.s.get_requirement(rid) if rid else None
        self.loading=True
        self.done_var.set(bool(draft.get('done',False)) if draft else bool(row.done) if row else False)
        self.loading=False
        self.insert_mode_var.set('章节末尾')
        self.insert_mode_box.configure(state='readonly' if rid is None else 'disabled')
        self.editlabel.configure(text=('编辑任务 #'+str(rid) if rid else '新建任务')+(' · 草稿已恢复' if draft else ''))

    def new_task(self):
        self.require_doc();self.keep_draft();self.insert_after_id=None
        selected=self.list.selection();chapter='未分类'
        if len(selected)==1:
            row=self.s.get_requirement(int(selected[0]));chapter=row.section or '未分类'
        self.load_editor(None);self.chaptervar.set(chapter);self.done_var.set(False);self.insert_mode_var.set('章节末尾')
        self.show_page('tasks');self.titlevar.set('');self.body.focus_set()

    def start_manual(self):
        self.require_doc();self.show_page('tasks');self.new_task()

    def insert_after_selected(self):
        self.require_doc();ids=[int(x) for x in self.list.selection()]
        if len(ids)!=1:raise ValueError('请先在任务列表中单击选择一条任务，再使用“在选中任务后插入”。')
        row=self.s.get_requirement(ids[0]);self.keep_draft();self.insert_after_id=row.id
        self.load_editor(None);self.chaptervar.set(row.section or '未分类');self.insert_mode_var.set('选中任务之后')
        self.titlevar.set('');self.body.delete('1.0','end');self.done_var.set(False);self.show_page('tasks');self.body.focus_set()
        self.status.set(f'新任务将插入到“{row.title}”之后。')

    def edit_selected(self):
        ids=[int(x) for x in self.list.selection()]
        if len(ids)!=1:raise ValueError('请在任务列表中单击选择一条任务进行编辑。')
        self.keep_draft();self.load_editor(ids[0]);self.show_page('tasks');self.body.focus_set()

    def save_task(self):
        self.require_doc();oldkey=self.draft_key();title=self.titlevar.get();section=self.chaptervar.get() or '未分类';content=self.body.get('1.0','end-1c')
        if self.rid:
            row=self.store.save_task(self.key,title,section,content,self.rid)
        else:
            mode=self.insert_mode_var.get();after=None;chapter_end=True
            if mode=='选中任务之后':
                after=self.insert_after_id
                if after is None:
                    selected=self.list.selection()
                    if len(selected)==1:after=int(selected[0])
                if after is None:raise ValueError('请先选择一条任务，或改用“章节末尾”。')
            elif mode=='文档末尾':chapter_end=False
            row=self.store.insert_task(self.key,title,section,content,after_id=after,chapter_end=chapter_end)
        self.s.set_done(row.id,bool(self.done_var.get()))
        self.store.draft(oldkey,delete=True);self.rid=row.id;self.dirty=False;self.insert_after_id=None
        self.store.snapshot(self.key);self.checked_ids.clear();self.refresh_tasks()
        if self.list.exists(str(row.id)):self.list.selection_set(str(row.id));self.list.see(str(row.id))
        self.load_editor(row.id);self.refresh_history();self.refresh_progress();self.refresh_home()
        self.status.set('任务已保存；完成状态和文档汇总已更新。')

    def cancel_edit(self):
        oldkey=self.draft_key();self.store.draft(oldkey,delete=True);self.insert_after_id=None
        selected=self.list.selection()
        if len(selected)==1:self.load_editor(int(selected[0]))
        else:self.load_editor(None)
        self.status.set('已取消本次编辑；尚未保存的内容已清除。')

    def open_editor(self):
        self.show_page('tasks');self.body.focus_set()

    def close_editor(self):
        self.keep_draft()

    def save_and_close(self):
        self.save_task()

    def save_current(self):
        self.guard(self.save_task);return 'break'

    def open_aux(self,tab):
        self.require_doc();self.persist_notes();self.refresh_preview();self.refresh_sources();self.refresh_history()
        self.tabs.select(tab);self.aux.deiconify();self.aux.lift()

    def show_preview(self):
        self.open_aux(self.previewtab)

    def mark(self,done):
        ids=self.selected_ids()
        if not ids:raise ValueError('请先勾选或选择任务。')
        for rid in ids:self.s.set_done(rid,done)
        if self.rid in ids:
            self.loading=True;self.done_var.set(bool(done));self.loading=False
        self.store.snapshot(self.key);self.refresh_tasks();self.status.set(f'已将 {len(ids)} 条任务标记为'+('已完成。' if done else '未完成。'))

    def trash_tasks(self):
        ids=self.selected_ids()
        if not ids:raise ValueError('请先勾选或选择任务。')
        self.keep_draft();self.store.trash(ids);self.checked_ids.clear();self.refresh_tasks();self.load_editor(None)
        self.store.snapshot(self.key);self.refresh_progress();self.status.set(f'已将 {len(ids)} 条任务移入回收站，可随时恢复。')

    def restore_tasks(self):
        ids=self.selected_ids()
        if not ids:raise ValueError('请先选择回收站中的任务。')
        self.store.trash(ids,restore=True);self.checked_ids.clear();self.filter.set('全部');self.refresh_tasks();self.load_editor(None)
        self.store.snapshot(self.key);self.refresh_progress();self.status.set(f'已恢复 {len(ids)} 条任务；已返回全部任务列表。')

    def move(self,delta):
        super().move(delta);self.refresh_tasks();self.status.set('任务顺序已更新。')

    def import_dialog(self):
        paths=filedialog.askopenfilenames(parent=self,title='选择要导入的资料（可多选）',filetypes=[('Word / Markdown / TXT','*.docx *.md *.markdown *.txt')])
        if paths:self.import_paths(paths)

    def import_paths(self,paths):
        """One preview workflow for file dialog and Explorer file drops."""
        documents=[]
        for project in self.s.project_groups():
            for doc in self.s.documents_for(project.name):documents.append((f'{project.name} / {doc.name}',doc.key))
        if not documents:
            messagebox.showinfo('导入资料','请先新建项目和文档，再导入资料。',parent=self);return
        from reqdoc_win_drop import select_drop_files
        paths,skipped=select_drop_files(tuple(str(p) for p in paths))
        if skipped:messagebox.showwarning('部分文件未导入','不支持的文件：'+'、'.join(skipped[:12])+'\n目前支持 docx、txt、md、markdown。',parent=self)
        if not paths:return
        source_paths,rows,warnings,errors=prepare_import_items(paths)
        if errors:messagebox.showwarning('部分资料无法解析','以下文件未加入预览：\n'+'\n'.join(errors),parent=self)
        if not source_paths:raise ValueError('没有可导入的资料。请检查文件格式或编码。')
        self.import_preview=ImportPreviewDialog(self,documents,self.key,source_paths,rows,warnings,self.commit_import_preview)
        self.status.set(f'已解析 {len(source_paths)} 份资料、{len(rows)} 条任务；等待检查和确认。')

    def commit_import_preview(self,target_key,source_paths,rows):
        self.keep_draft();self.persist_notes();self.configure(cursor='watch')
        self.status.set('正在归档原始资料并写入所选文档…');self.update_idletasks()
        try:result=commit_import_to_store(self.store,target_key,source_paths,rows)
        finally:self.configure(cursor='')
        if not result['archived']:raise ValueError('没有资料成功归档。\n'+'\n'.join(result['errors']))
        self.checked_ids.clear();self.refresh_tree(target_key);self.load_doc(target_key);self.show_page('tasks')
        # Import may target the already-open document. Force refresh even if
        # load_doc returned early, and reset a stale '回收站' filter.
        self.filter.set('全部');self.query.set('');self.checked_ids.clear()
        self.refresh_tasks();self.refresh_progress()
        doc=self.s.document(target_key)
        self.status.set(f'已归档 {result["archived"]} 份资料，生成 {result["added"]} 条任务。可在左侧表格页查看或编辑资料。')
        messages=result['warnings']+result['errors']
        if messages:messagebox.showwarning('导入结果','已归档 '+str(result['archived'])+' 份原始资料，写入 '+str(result['added'])+' 条任务。\n\n'+'\n'.join(messages),parent=self)
        return result

    def refresh_progress(self):
        if not hasattr(self,'progress_chapters'):return
        previous=None
        selected_before=self.progress_chapters.selection()
        if selected_before:previous=self.progress_chapter_map.get(selected_before[0])
        self.progress_chapters.delete(*self.progress_chapters.get_children());self.progress_tasks.delete(*self.progress_tasks.get_children());self.progress_chapter_map={}
        rows=self.store.tasks(self.key) if self.key else []
        total=len(rows);done=sum(bool(r.done) for r in rows);pending=total-done;rate=(done/total*100) if total else 0
        self.progress_total.set(str(total));self.progress_done.set(str(done));self.progress_open.set(str(pending));self.progress_rate.set(f'{rate:.0f}%');self.progress_bar.configure(value=rate)
        groups={}
        for row in rows:groups.setdefault(row.section or '未分类',[]).append(row)
        for i,(chapter,items) in enumerate(sorted(groups.items(),key=lambda x:x[0].casefold())):
            d=sum(bool(r.done) for r in items);n=len(items);pct=d/n*100 if n else 0;iid='chapter-'+str(i)
            self.progress_chapter_map[iid]=chapter
            self.progress_chapters.insert('','end',iid=iid,values=(chapter,n,d,n-d,f'{pct:.0f}%'))
        if self.progress_chapters.get_children():
            chosen=next((iid for iid,chapter in self.progress_chapter_map.items() if chapter==previous),None)
            if chosen is None:chosen=self.progress_chapters.get_children()[0]
            self.progress_chapters.selection_set(chosen)
        self.refresh_progress_tasks()

    def refresh_progress_tasks(self):
        if not hasattr(self,'progress_tasks'):return
        self.progress_tasks.delete(*self.progress_tasks.get_children())
        chapter=None;selected=self.progress_chapters.selection() if hasattr(self,'progress_chapters') else []
        if selected:chapter=self.progress_chapter_map.get(selected[0])
        rows=self.store.tasks(self.key) if self.key else []
        for row in rows:
            if chapter is not None and (row.section or '未分类')!=chapter:continue
            self.progress_tasks.insert('','end',iid=str(row.id),values=('已完成' if row.done else '未完成',row.title,row.section))

    def progress_task_click(self,event):
        row=self.progress_tasks.identify_row(event.y);column=self.progress_tasks.identify_column(event.x)
        if row and column=='#1':
            rid=int(row);task=self.s.get_requirement(rid);new_done=not bool(task.done);self.s.set_done(rid,new_done)
            if self.rid==rid:
                self.loading=True;self.done_var.set(new_done);self.loading=False
            self.store.snapshot(self.key);self.refresh_progress();self.refresh_tasks();return 'break'

    def mark_progress(self,done):
        selected=self.progress_tasks.selection()
        if not selected:raise ValueError('请先选择一条或多条任务。')
        for rid in selected:self.s.set_done(int(rid),done)
        if self.rid is not None and str(self.rid) in selected:
            self.loading=True;self.done_var.set(bool(done));self.loading=False
        self.store.snapshot(self.key);self.refresh_progress();self.refresh_tasks()
        self.status.set(f'已更新 {len(selected)} 条任务的完成状态。')

    def edit_progress_task(self,event=None):
        selected=self.progress_tasks.selection()
        if len(selected)!=1:raise ValueError('请只选择一条任务。')
        self.load_editor(int(selected[0]));self.show_page('tasks')

    def show_preview(self):
        self.require_doc();self.open_aux(self.previewtab)

    def show_trash(self):
        self.filter.set('回收站');self.checked_ids.clear();self.refresh_tasks();self.show_page('tasks')

    def split_task(self):
        ids=self.selected_ids()
        if len(ids)!=1:raise ValueError('请选择一条任务拆分。')
        r=self.s.get_requirement(ids[0]);win=tk.Toplevel(self);win.title('拆分任务');win.transient(self);self.sized_dialog(win,820,680);win.grab_set()
        win.columnconfigure(0,weight=1);win.rowconfigure(1,weight=1)
        ttk.Label(win,text='在需要拆开的地方插入单独一行 --- 。确认后，新任务将替换原任务。',padding=12,wraplength=700).grid(row=0,column=0,sticky='ew')
        frame=ttk.Frame(win,padding=12);frame.grid(row=1,column=0,sticky='nsew');text=self.text_box(frame);text.insert('1.0',r.content)
        footer=ttk.Frame(win,padding=12);footer.grid(row=2,column=0,sticky='ew')
        def commit():
            blocks=[x.strip() for x in re.split(r'^\s*---\s*$',text.get('1.0','end-1c'),flags=re.M) if x.strip()]
            if len(blocks)<2:raise ValueError('至少需要两个部分，请插入一行 --- 。')
            replacements=[{'title':f'{r.title}（{i+1}）','section':r.section,'content':block} for i,block in enumerate(blocks)]
            self.store.replace_tasks(self.key,[r.id],replacements)
            self.checked_ids.clear();self.store.snapshot(self.key);self.refresh_tasks();self.refresh_progress();win.destroy()
        self.button(footer,'确认拆分',commit);self.button(footer,'取消',win.destroy)

    def merge_tasks(self):
        ids=self.selected_ids()
        if len(ids)<2:raise ValueError('请勾选至少两条任务。')
        rows=[r for r in self.store.tasks(self.key) if r.id in ids]
        if len(rows)<2:raise ValueError('请从正常任务列表选择任务。')
        name=simpledialog.askstring('合并任务','合并后的任务将替换所选任务。请输入标题。',initialvalue=rows[0].title,parent=self)
        if not name:return
        body='\n\n'.join('#### '+r.title+'\n'+r.content for r in rows)
        self.store.replace_tasks(self.key,ids,[{'title':name,'section':rows[0].section,'content':body}])
        self.store.snapshot(self.key)
        self.checked_ids.clear();self.refresh_tasks();self.refresh_progress();self.status.set('已合并任务。')

    def help(self):
        self.show_text('reqdoc 测试版 4 使用说明','''一、基本结构
左侧第一栏是功能导航；第二栏是项目与文档大纲。选择文档后，右侧工作区显示该文档的功能页面。

二、新建文档
新建项目 → 新建文档 → 在工作台选择“手写录入”或“导入资料”。

三、手写录入与插入任务
手写录入可逐条添加任务。新任务默认插入到所属章节末尾；也可以选择“选中任务之后”或“文档末尾”。在任务列表中先选择一条任务，再点击“在选中任务后插入”，可以在原有文档中间增加新任务。

四、导入资料
导入中心支持 Word（.docx）、Markdown 和 TXT。先选择资料，程序会显示任务预览。预览中可以全选、删除选中项、修改标题、章节和正文，再选择目标文档及插入方式。只有点击“放入所选文档”才会正式写入；取消不会修改文档。

五、任务整理
任务列表第一列可勾选任务；“全选 / 取消全选”用于批量选择。可批量标记完成、未完成、修改章节或移入回收站。点击状态列也可以直接切换完成状态。

六、完成进度
进度统计显示文档总任务数、已完成、未完成和完成率，并按章节统计。下方任务列表可直接点击状态切换完成情况。

七、保存与备份
任务草稿会自动保存；正式修改请点击“保存任务”。备份会保存数据库、文档、原始资料、图片和草稿。恢复操作会生成独立副本，不覆盖当前数据。

八、注意事项
本版不调用外部 AI。Word 复杂版式、批注、公式等请以归档的原文件为准。请勿同时用多个程序版本打开同一份 reqdoc-data。''')

    def _on_root_destroy(self,event):
        if getattr(event,'widget',None) is self:
            self._closing=True
            self._update_in_progress=False

    def close(self):
        if getattr(self,'_closing',False):return
        try:self.keep_draft();self.persist_notes()
        except Exception as exc:
            try:
                if not messagebox.askyesno(
                    '保存失败',
                    str(exc)+'\n\n仍然退出会丢失当前尚未保存的编辑内容。建议选择“否”，留在窗口中重试保存。',
                    default=messagebox.NO,
                    parent=self,
                ):return
            except tk.TclError:pass
        self._closing=True
        self._update_in_progress=False
        try:
            if getattr(self,'_autosave_after_id',None):self.after_cancel(self._autosave_after_id)
        except Exception:pass
        try:self.destroy()
        except tk.TclError:pass



# ---------------------------------------------------------------------------
# 测试版 5：单导航主界面、独立项目/文档页面、全局进度和数据迁移。
# 旧版 CompactStudioV4 仅作为功能基类，不再创建第二栏固定项目树。
class CompactStudio(CompactStudioV4):
    """测试版 7：四模块信息架构、合并项目文档管理、按需任务编辑。"""
    NAV_ITEMS = [
        ('项目与文档', 'projects'),
        ('任务处理', 'tasks'),
        ('进度统计', 'progress'),
        ('设置与数据', 'settings'),
    ]

    def report_callback_exception(self,exc,value,tb):
        logging.error('Unhandled Tk callback',exc_info=(exc,value,tb))
        if getattr(self,'_closing',False):return
        try:self.status.set('发生界面错误；详细信息已写入 studio.log。')
        except Exception:pass
        try:messagebox.showerror('程序操作异常',str(value)+'\n\n详细信息已记录在 studio.log 中。',parent=self)
        except Exception:pass

    def build(self):
        self.title(f'文档工作台 · {VERSION}')
        sw=self.winfo_screenwidth();sh=self.winfo_screenheight()
        self.geometry(f'{min(1420,max(900,sw-50))}x{min(880,max(620,sh-100))}')
        self.minsize(900,600)
        self.checked_ids=set();self.visible_ids=[];self._refreshing_tasks=False;self._task_focus_id=None
        self.insert_after_id=None;self.progress_chapter_map={};self.progress_group_map={};self.progress_task_doc_map={}
        self.current_project_name=None;self.document_map={};self.import_source_map={}
        # V8.0.20 candidate: document-first shell; preserve existing page and task widgets.
        self.configure(bg='#F5F6F9')
        shell=tk.Frame(self,bg='#F5F6F9')
        shell.pack(fill='both',expand=True)

        # A compact top toolbar replaces the old full-height blue navigation rail.
        top=tk.Frame(shell,bg='#FFFFFF',height=58)
        top.pack(side='top',fill='x')
        top.pack_propagate(False)
        tk.Label(top,text='ReqDoc',font=('Microsoft YaHei UI',16,'bold'),
                 bg='#FFFFFF',fg='#202938').pack(side='left',padx=(18,22))
        self.nav=tk.Frame(top,bg='#FFFFFF')
        self.nav.pack(side='left',fill='y')
        self.nav_buttons={}
        for label,page in self.NAV_ITEMS:
            button=tk.Button(
                self.nav,text=label,font=('Microsoft YaHei UI',9),
                bg='#FFFFFF',fg='#586478',activebackground='#EEEAFB',
                activeforeground='#5541AD',bd=0,relief='flat',
                padx=11,pady=10,cursor='hand2',
                command=lambda p=page:self.show_page(p),
            )
            button.pack(side='left',padx=(0,2),pady=8)
            self.nav_buttons[page]=button
        ttk.Button(top,text='＋ 新建文档',
                   command=lambda:self.guard(self.new_document)).pack(side='right',padx=(0,16),pady=9)
        tk.Frame(shell,bg='#E9EBF1',height=1).pack(side='top',fill='x')

        # Persistent document tree on the left, like a document editor rather than a
        # separate project-management page. Search filters the tree only, not stored data.
        body=tk.Frame(shell,bg='#FFFFFF')
        body.pack(side='top',fill='both',expand=True)
        self.doc_sidebar=tk.Frame(body,bg='#F8F9FC',width=242)
        self.doc_sidebar.pack(side='left',fill='y')
        self.doc_sidebar.pack_propagate(False)
        sidebar_header=tk.Frame(self.doc_sidebar,bg='#F8F9FC')
        sidebar_header.pack(fill='x',padx=14,pady=(18,9))
        tk.Label(sidebar_header,text='文档',font=('Microsoft YaHei UI',12,'bold'),
                 bg='#F8F9FC',fg='#303846').pack(side='left')
        ttk.Button(sidebar_header,text='＋',width=3,
                   command=lambda:self.guard(self.new_project)).pack(side='right')
        ttk.Button(sidebar_header,text='▤',width=3,
                   command=lambda:self.guard(self.new_document)).pack(side='right',padx=(0,5))
        self.sidebar_query=tk.StringVar()
        sidebar_search=ttk.Entry(self.doc_sidebar,textvariable=self.sidebar_query)
        sidebar_search.pack(fill='x',padx=14,pady=(0,10))
        self.sidebar_query.trace_add('write',lambda *args:self.refresh_sidebar_tree())
        sidebar_tree_area=ttk.Frame(self.doc_sidebar,style='Sidebar.TFrame')
        sidebar_tree_area.pack(fill='both',expand=True,padx=(8,5))
        self.tree=ttk.Treeview(sidebar_tree_area,show='tree',selectmode='browse',
                               style='Sidebar.Treeview',height=20)
        sidebar_scroll=ttk.Scrollbar(sidebar_tree_area,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=sidebar_scroll.set)
        sidebar_scroll.pack(side='right',fill='y')
        self.tree.pack(side='left',fill='both',expand=True)
        self.tree.bind('<<TreeviewSelect>>',
                       lambda event:self.guard(self.sidebar_tree_select))
        tk.Label(self.doc_sidebar,text='本地文档 · 不联网',
                 bg='#F8F9FC',fg='#8993A4',
                 font=('Microsoft YaHei UI',8)).pack(side='bottom',anchor='w',padx=17,pady=14)
        tk.Frame(body,bg='#E9EBF1',width=1).pack(side='left',fill='y')

        # The working document occupies the central canvas. Task context is the
        # right-hand inspector built by reqdoc_working_document.build_tasks_page.
        self.workspace=ttk.Frame(body)
        self.workspace.pack(side='left',fill='both',expand=True)
        context=ttk.Frame(self.workspace,padding=(22,12,20,8))
        context.pack(fill='x')
        self.context_label=ttk.Label(
            context,text='当前文档：尚未选择',foreground='#788394',
            font=('Microsoft YaHei UI',9),
        )
        self.context_label.pack(side='left',fill='x',expand=True)
        ttk.Button(context,text='显示项目列表',
                   command=lambda:self.show_page('projects')).pack(side='right')
        self.pages_host=ttk.Frame(self.workspace,padding=(22,6,20,18))
        self.pages_host.pack(fill='both',expand=True)
        self.pages={
            name:ttk.Frame(self.pages_host)
            for name in ('home','projects','documents','import','tasks','progress','export','settings')
        }
        self.build_home_page()
        self.build_projects_page()
        self.build_documents_page()
        self.build_import_page()
        self.build_tasks_page()
        self.build_progress_page()
        self.build_export_page()
        self.build_settings_page()
        self.build_aux()
        self.status=tk.StringVar(value='就绪 · 数据保存在本机')
        ttk.Label(self,textvariable=self.status,style='Status.TLabel').pack(fill='x')
        self.show_page('projects')
        self._refresh_tree_after_id=self.after(50,self.refresh_tree)
        self._file_drop=None
        self._drop_after_id=self.after(240,self.install_file_drop) if sys.platform=='win32' else None

    def refresh_sidebar_tree(self):
        """Update the visible project/document navigator without editing project data."""
        if not hasattr(self,'tree') or not hasattr(self,'s'):
            return
        self.refresh_tree()

    def sidebar_tree_select(self):
        if getattr(self,'_sidebar_refreshing',False):
            return
        selection=self.tree.selection()
        if not selection:
            return
        item=selection[0]
        if item.startswith('d:'):
            key=item[2:]
            if key!=self.key:
                self.load_doc(key)
            self.show_page('tasks')
        elif item.startswith('p:'):
            values=self.tree.item(item,'values')
            if values:
                self.current_project_name=values[0]
            self.show_page('projects')

    def install_file_drop(self):
        """Register Explorer drops on the window and the document editor."""
        self._drop_after_id=None
        if sys.platform!='win32':return False
        if self._file_drop:return True
        bridge=None
        try:
            from reqdoc_win_drop import WindowsFileDrop
            bridge=WindowsFileDrop(self,self._accept_dropped_files)
            total=bridge.install_on(self,include_parent=True)
            editor=getattr(self,'working_text',None)
            if editor is not None:
                total+=bridge.install_on(editor)
            if not total:raise RuntimeError('未能注册任何可拖入区域。')
            self._file_drop=bridge
            self.status.set('可将 docx、txt、md 文件拖入窗口，选择形成工作文档或任务。')
            return True
        except Exception as exc:
            if bridge is not None:
                try:bridge.close()
                except Exception:pass
            logging.warning('Explorer drag/drop unavailable: %s',exc)
            self.status.set('文件拖放暂不可用，可点击“导入文件”；详情请查看程序日志。')
            return False

    def _accept_dropped_files(self,paths,errors):
        if self.grab_current() is not None:
            self.status.set('请先关闭当前对话框，再拖入文件。')
            return
        if errors:
            messagebox.showwarning('文件拖入', '\n'.join(errors),parent=self)
            return
        if paths:self.guard(lambda:self.import_paths(paths))

    def page_title(self,parent,title,subtitle=''):
        title_label=ttk.Label(parent,text=title,font=('Microsoft YaHei',17,'bold'))
        title_label.pack(anchor='w',pady=(0,3))
        if subtitle:
            sub=ttk.Label(parent,text=subtitle,foreground='#64748B',wraplength=1000,justify='left')
            sub.pack(anchor='w',pady=(0,12))
            parent.bind('<Configure>',lambda e,w=sub: w.configure(wraplength=max(300,e.width-24)),add='+')

    def build_projects_page(self):
        page=self.pages['projects']
        self.page_title(page,'项目与文档','在同一页面管理项目及其文档。展开项目即可选择文档，双击文档直接进入任务处理。')
        row=ttk.Frame(page);row.pack(fill='x',pady=(0,8))
        actions=[
            ('＋ 新建项目',self.new_project),
            ('＋ 新建文档',self.new_document),
            ('重命名选中项',self.rename_project_or_document),
            ('删除选中项',self.delete_project_or_document),
            ('打开文档 / 展开项目',self.open_project_from_list),
        ]
        for label,fn in actions:
            ttk.Button(row,text=label,command=lambda f=fn:self.guard(f)).pack(side='left',padx=(0,8))
        self.project_context=ttk.Label(page,text='可新建、重命名和删除项目与文档。',foreground='#64748B')
        self.project_context.pack(anchor='w',pady=(0,6))
        frame=ttk.Frame(page);frame.pack(fill='both',expand=True)
        # First (hidden) value stores project name to keep compatibility with existing services.
        self.project_list=ttk.Treeview(frame,columns=('project','documents','tasks','done','rate'),show='tree headings',selectmode='browse')
        self.project_list.heading('#0',text='项目 / 文档')
        self.project_list.column('#0',width=510,minwidth=230,stretch=True)
        self.project_list.heading('project',text='')
        self.project_list.column('project',width=1,minwidth=1,stretch=False)
        for key,title,width in [('documents','文档数',80),('tasks','任务数',90),('done','已完成',90),('rate','完成率',90)]:
            self.project_list.heading(key,text=title)
            self.project_list.column(key,width=width,minwidth=65,anchor='center',stretch=False)
        sb=ttk.Scrollbar(frame,orient='vertical',command=self.project_list.yview)
        self.project_list.configure(yscrollcommand=sb.set)
        sb.pack(side='right',fill='y');self.project_list.pack(fill='both',expand=True)
        self.project_list.bind('<<TreeviewSelect>>',self.on_project_selected)
        self.project_list.bind('<Double-1>',lambda e:self.guard(self.open_project_from_list))
        self.project_list.bind('<ButtonPress-1>',self.project_drag_press,add='+')
        self.project_list.bind('<B1-Motion>',self.project_drag_motion,add='+')
        self.project_list.bind('<ButtonRelease-1>',self.project_drag_release,add='+')
        self._project_drag=None
        bottom=ttk.Frame(page);bottom.pack(fill='x',pady=(10,0))
        ttk.Button(bottom,text='为选中文档导入资料',command=lambda:self.guard(self.import_for_tree_selection)).pack(side='left',padx=(0,6))
        ttk.Button(bottom,text='打开当前任务处理',command=lambda:self.show_page('tasks')).pack(side='left')

    def build_documents_page(self):
        page=self.pages['documents']
        self.page_title(page,'文档管理','文档属于项目。展开项目即可查看、打开和管理该项目下的文档。')
        bar=ttk.Frame(page);bar.pack(fill='x',pady=(0,10))
        ttk.Label(bar,text='筛选项目').pack(side='left',padx=(0,6))
        self.doc_project_var=tk.StringVar(value='全部项目')
        self.doc_project_box=ttk.Combobox(bar,textvariable=self.doc_project_var,state='readonly',width=28)
        self.doc_project_box.pack(side='left',padx=(0,10))
        self.doc_project_box.bind('<<ComboboxSelected>>',lambda e:self.refresh_documents_page())
        ttk.Button(bar,text='＋ 在所选项目中新建文档',command=lambda:self.guard(self.new_document)).pack(side='left',padx=(0,8))
        ttk.Button(bar,text='打开所选文档',command=lambda:self.guard(self.open_selected_document)).pack(side='left',padx=(0,8))
        ttk.Button(bar,text='重命名文档',command=lambda:self.guard(self.rename_document_selected)).pack(side='left')
        self.document_list=ttk.Treeview(page,columns=('tasks','done','rate'),show='tree headings',height=16,selectmode='browse')
        self.document_list.heading('#0',text='项目 / 文档')
        for col,label,width in [('tasks','任务数',100),('done','已完成',100),('rate','完成率',110)]:
            self.document_list.heading(col,text=label)
            self.document_list.column(col,width=width,minwidth=75,stretch=False,anchor='center')
        self.document_list.column('#0',width=520,minwidth=220,stretch=True)
        self.document_list.pack(fill='both',expand=True)
        self.document_list.bind('<Double-1>',lambda e:self.guard(self.open_selected_document))
        self.document_list.bind('<<TreeviewSelect>>',self.on_document_tree_select)
        self.document_map={};self.document_project_map={}

    def build_tasks_page(self):
        page=self.pages['tasks']
        self.tasks_heading=ttk.Label(page,text='任务整理',font=('Microsoft YaHei',17,'bold'))
        self.tasks_heading.pack(anchor='w',pady=(0,3))
        self.tasks_subtitle=ttk.Label(page,text='这里显示软件整理后的任务。选择任务后直接编辑；原始资料只作为只读参考。',foreground='#64748B')
        self.tasks_subtitle.pack(anchor='w',pady=(0,8))

        selectors=ttk.LabelFrame(page,text='当前处理对象',padding=8)
        selectors.pack(fill='x',pady=(0,8))
        ttk.Label(selectors,text='项目').pack(side='left',padx=(0,6))
        self.task_project_var=tk.StringVar()
        self.task_project_box=ttk.Combobox(selectors,textvariable=self.task_project_var,state='readonly',width=25)
        self.task_project_box.pack(side='left',padx=(0,12))
        self.task_project_box.bind('<<ComboboxSelected>>',self.on_task_project_selected)
        ttk.Label(selectors,text='文档').pack(side='left',padx=(0,6))
        self.task_document_var=tk.StringVar()
        self.task_document_box=ttk.Combobox(selectors,textvariable=self.task_document_var,state='readonly')
        self.task_document_box.pack(side='left',fill='x',expand=True)
        self.task_document_box.bind('<<ComboboxSelected>>',self.on_task_document_selected)
        self.task_doc_map={};self._task_selector_loading=False;self._task_source_loading=False
        self.selected_source_id=None;self._task_sashes_initialized=False
        self.editor_open=True;self.source_panel_visible=True
        docactions=ttk.Frame(page);docactions.pack(fill='x',pady=(0,6))
        ttk.Button(docactions,text='导入资料',command=lambda:self.guard(self.import_dialog)).pack(side='left',padx=(0,6))
        ttk.Button(docactions,text='查看文档汇总',command=lambda:self.guard(self.show_preview)).pack(side='left',padx=(0,6))
        self.source_toggle_button=ttk.Button(docactions,text='隐藏文档预览',command=lambda:self.guard(self.toggle_source_panel))
        self.source_toggle_button.pack(side='left',padx=(0,6))
        docmenu=ttk.Menubutton(docactions,text='文档操作 ▾')
        menu=tk.Menu(docmenu,tearoff=False)
        menu.add_command(label='导出 Word',command=lambda:self.guard(lambda:self.export('docx')))
        menu.add_command(label='导出 Markdown 图片包',command=lambda:self.guard(lambda:self.export('zip')))
        docmenu['menu']=menu;docmenu.pack(side='left',padx=(0,6))

        self.tasks_split=ttk.Panedwindow(page,orient='horizontal')
        self.tasks_split.pack(fill='both',expand=True)
        source_panel=ttk.LabelFrame(self.tasks_split,text='文档预览 / 原始资料（只读）',padding=7);self.task_source_panel=source_panel
        sourcebar=ttk.Frame(source_panel);sourcebar.pack(fill='x',pady=(0,6))
        self.task_source_var=tk.StringVar(value='')
        self.task_source_box=ttk.Combobox(sourcebar,textvariable=self.task_source_var,state='readonly')
        self.task_source_box.pack(side='left',fill='x',expand=True,padx=(0,6))
        self.task_source_box.bind('<<ComboboxSelected>>',self.on_task_source_selected)
        self.task_source_open=ttk.Button(sourcebar,text='打开原文件',command=lambda:self.guard(self.open_task_source),state='disabled')
        self.task_source_open.pack(side='left')
        source_actions=ttk.Frame(source_panel);source_actions.pack(fill='x',pady=(0,5))
        self.task_source_edit=ttk.Button(source_actions,text='编辑资料 / 表格',command=lambda:self.guard(self.edit_task_source),state='disabled')
        # Original source material is deliberately not exposed as an editable object.
        # The processed task records below are the editable working result.
        self.task_table_to_tasks_button=ttk.Button(source_actions,text='表格行转任务',command=lambda:self.guard(self.table_rows_to_tasks),state='disabled')
        self.task_table_to_tasks_button.pack(side='left')
        self.task_source_meta=ttk.Label(source_panel,text='选择文档后显示原始资料。',foreground='#64748B')
        self.task_source_meta.pack(anchor='w',pady=(0,5))
        self.task_source_book=ttk.Notebook(source_panel);self.task_source_book.pack(fill='both',expand=True)
        source_read=ttk.Frame(self.task_source_book)
        self.task_source_book.add(source_read,text='阅读')
        self.task_source_view=MarkdownView(source_read,self.theme)
        self.task_source_view.pack(fill='both',expand=True)
        self.task_table_page=ttk.Frame(self.task_source_book,padding=5)
        self.task_source_book.add(self.task_table_page,text='表格')
        table_ops=ttk.Frame(self.task_table_page);table_ops.pack(fill='x',pady=(0,5))
        ttk.Button(table_ops,text='复制整张表',command=lambda:self.guard(self.copy_current_table)).pack(side='left',padx=(0,5))
        ttk.Button(table_ops,text='复制选中行',command=lambda:self.guard(lambda:self.copy_table_rows(self.task_table_grid))).pack(side='left',padx=(0,5))
        ttk.Button(table_ops,text='复制 Markdown',command=lambda:self.guard(self.copy_current_table_markdown)).pack(side='left')
        self.task_table_grid=ttk.Treeview(self.task_table_page,show='headings')
        scroll_x=ttk.Scrollbar(self.task_table_page,orient='horizontal',command=self.task_table_grid.xview)
        scroll_y=ttk.Scrollbar(self.task_table_page,orient='vertical',command=self.task_table_grid.yview)
        self.task_table_grid.configure(xscrollcommand=scroll_x.set,yscrollcommand=scroll_y.set)
        scroll_y.pack(side='right',fill='y');scroll_x.pack(side='bottom',fill='x')
        self.task_table_grid.pack(fill='both',expand=True)
        self.task_table_grid.bind('<Double-1>',lambda e:self.guard(self.edit_task_source))
        self.task_table_grid.bind('<Control-c>',lambda e:self.guard(lambda:self.copy_table_rows(self.task_table_grid)) or 'break')
        self.task_table_grid.bind('<Control-C>',lambda e:self.guard(lambda:self.copy_table_rows(self.task_table_grid)) or 'break')
        self.task_source_book.tab(self.task_table_page,state='disabled')
        ttk.Label(source_panel,text='原始资料只读保留；没有原始资料时显示当前文档汇总。',foreground='#64748B',wraplength=520).pack(anchor='w',pady=(5,0))
        self.tasks_split.add(source_panel,weight=1)

        right=ttk.Frame(self.tasks_split);self.task_right_panel=right;self.tasks_split.add(right,weight=4)
        self.tasks_context=ttk.Label(right,text='尚未选择文档。请使用上方项目和文档选择器。',foreground='#64748B')
        self.tasks_context.pack(anchor='w',pady=(0,6))
        task_split=ttk.Panedwindow(right,orient='vertical');task_split.pack(fill='both',expand=True)
        self.task_vertical_split=task_split

        task_list_panel=ttk.LabelFrame(task_split,text='整理结果 / 任务列表',padding=7);self.task_list_panel=task_list_panel;task_split.add(task_list_panel,weight=3)
        toolbar=ttk.Frame(task_list_panel);toolbar.pack(fill='x',pady=(0,6))
        self.query=tk.StringVar()
        ttk.Entry(toolbar,textvariable=self.query).pack(side='left',fill='x',expand=True,padx=(0,7))
        self.filter=tk.StringVar(value='全部')
        cb=ttk.Combobox(toolbar,textvariable=self.filter,values=['全部','未完成','已完成','回收站'],width=9,state='readonly')
        cb.pack(side='left',padx=(0,7));cb.bind('<<ComboboxSelected>>',lambda e:(self.checked_ids.clear(),self.refresh_tasks()))
        primary=ttk.Frame(task_list_panel);primary.pack(fill='x',pady=(0,5))
        primary_items=[('＋ 新增任务',self.new_task),('选中后插入',self.insert_after_selected),
                       ('编辑任务',self.edit_selected),('添加图片',self.add_image),('粘贴图片',self.paste_image)]
        for start in (0,3):
            row=ttk.Frame(primary);row.pack(fill='x',anchor='w')
            for label,action in primary_items[start:start+3]:
                style = 'Accent.TButton' if label == '＋ 新增任务' else 'TButton'
                ttk.Button(row,text=label,style=style,command=lambda fn=action:self.guard(fn)).pack(side='left',padx=(0,6),pady=2)
        listframe=ttk.Frame(task_list_panel);listframe.pack(fill='both',expand=True)
        self.list=ttk.Treeview(listframe,columns=('check','state','title','chapter'),show='headings',selectmode='extended')
        for col,label,width in [('check','选择',48),('state','状态',76),('title','任务标题',250),('chapter','所属章节',160)]:
            self.list.heading(col,text=label)
            self.list.column(col,width=width,minwidth=55,stretch=col in ('title','chapter'),anchor='center' if col in ('check','state') else 'w')
        sy=ttk.Scrollbar(listframe,orient='vertical',command=self.list.yview)
        sx=ttk.Scrollbar(listframe,orient='horizontal',command=self.list.xview)
        self.list.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        sy.pack(side='right',fill='y');sx.pack(side='bottom',fill='x');self.list.pack(fill='both',expand=True)
        self.list.bind('<Button-1>',self.task_cell_click)
        self.list.bind('<<TreeviewSelect>>',self.on_task_selected)
        self.list.bind('<Double-1>',lambda e:self.guard(self.edit_selected))
        self.query.trace_add('write',lambda *a:(self.checked_ids.clear(),self.refresh_tasks()))

        actions=ttk.Frame(task_list_panel);actions.pack(fill='x',pady=(6,0))
        quick=ttk.Frame(actions);quick.pack(fill='x')
        for i,(label,command) in enumerate([
            ('全选 / 取消全选',self.select_all_visible),
            ('标记完成',lambda:self.mark(True)),
            ('标记未完成',lambda:self.mark(False)),
            ('移入回收站',self.trash_tasks),
        ]):
            ttk.Button(quick,text=label,command=lambda fn=command:self.guard(fn)).grid(row=0,column=i,sticky='ew',padx=2,pady=2)
            quick.grid_columnconfigure(i,weight=1)
        secondary=ttk.Frame(actions);secondary.pack(fill='x')
        extra=ttk.Menubutton(secondary,text='更多任务操作 ▾')
        extra.pack(side='left',padx=2,pady=2)
        more_menu=tk.Menu(extra,tearoff=False)
        for label,command in [
            ('清除选择',self.clear_selection),('批量改章节',self.change_chapter),
            ('恢复回收站任务',self.restore_tasks),('上移',lambda:self.move(-1)),
            ('下移',lambda:self.move(1)),('拆分任务',self.split_task),
            ('合并任务',self.merge_tasks),('打开回收站',self.show_trash),
            ('隐藏编辑器',self.hide_editor),
        ]:
            more_menu.add_command(label=label,command=lambda fn=command:self.guard(fn))
        extra.configure(menu=more_menu)
        ttk.Button(secondary,text='彻底删除选中',command=lambda:self.guard(self.delete_trashed_selected)).pack(side='left',padx=2,pady=2)
        ttk.Button(secondary,text='清空回收站',command=lambda:self.guard(self.empty_task_trash)).pack(side='left',padx=2,pady=2)
        self.count=ttk.Label(secondary,text='0 条任务');self.count.pack(side='right',padx=3)

        editor=ttk.LabelFrame(task_split,text='整理结果 · 任务编辑',padding=10);self.task_editor_panel=editor;task_split.add(editor,weight=2)
        self.editor=editor
        self.editlabel=ttk.Label(editor,text='新任务',font=('Microsoft YaHei',11,'bold'));self.editlabel.pack(anchor='w',pady=(0,5))
        ttk.Label(editor,text='标题').pack(anchor='w')
        self.titlevar=tk.StringVar();ttk.Entry(editor,textvariable=self.titlevar).pack(fill='x',pady=(2,5))
        ttk.Label(editor,text='章节').pack(anchor='w')
        self.chaptervar=tk.StringVar(value='未分类');self.chapterbox=ttk.Combobox(editor,textvariable=self.chaptervar)
        self.chapterbox.pack(fill='x',pady=(2,5))
        insertrow=ttk.Frame(editor);insertrow.pack(fill='x',pady=(0,5))
        ttk.Label(insertrow,text='插入位置').pack(side='left',padx=(0,6))
        self.insert_mode_var=tk.StringVar(value='章节末尾')
        self.insert_mode_box=ttk.Combobox(insertrow,textvariable=self.insert_mode_var,values=['章节末尾','选中任务之后','文档末尾'],state='readonly',width=16)
        self.insert_mode_box.pack(side='left')
        self.done_var=tk.BooleanVar(value=False)
        ttk.Checkbutton(editor,text='此任务已完成',variable=self.done_var,command=self.changed).pack(anchor='w',pady=(0,5))
        ttk.Label(editor,text='任务正文').pack(anchor='w')
        ttk.Label(editor,text='图片插入位置：先在正文中单击放置光标，再使用下方图片按钮。',foreground='#64748B').pack(anchor='w',pady=(0,4))
        self.body=self.text_box(editor);self.body.bind('<<Modified>>',self.modified)
        self.body.bind('<Control-v>',self.paste_event);self.body.bind('<Control-V>',self.paste_event)
        for var in (self.titlevar,self.chaptervar):var.trace_add('write',self.changed)
        self.done_var.trace_add('write',self.changed)
        editactions=ttk.Frame(editor);editactions.pack(fill='x',pady=(5,0))
        ttk.Button(editactions,text='保存任务',style='Accent.TButton',command=lambda:self.guard(self.save_task)).pack(side='left',padx=(0,5))
        ttk.Button(editactions,text='插入图片',command=lambda:self.guard(self.add_image)).pack(side='left',padx=(0,5))
        ttk.Button(editactions,text='粘贴图片',command=lambda:self.guard(self.paste_image)).pack(side='left',padx=(0,5))
        ttk.Button(editactions,text='取消本次编辑',command=lambda:self.guard(self.cancel_edit)).pack(side='right')
        # Keep the editor visible beside the document preview and task list.

    def position_task_sashes(self):
        if self._task_sashes_initialized:return
        width=self.tasks_split.winfo_width();height=self.task_vertical_split.winfo_height()
        if width<=500:return
        if self.source_panel_visible:self.tasks_split.sashpos(0,int(width*0.32))
        if self.editor_open and height>300:self.task_vertical_split.sashpos(0,int(height*0.56))
        self._task_sashes_initialized=True

    def show_editor(self):
        self.require_doc()
        if not self.editor_open:
            self.task_vertical_split.add(self.task_editor_panel,weight=2)
            self.editor_open=True
            self._editor_sash_after_id=self.after_idle(self.position_editor_sash)

    def position_editor_sash(self):
        if self.editor_open and self.task_vertical_split.winfo_height()>250:
            # Keep enough room for both the task list and the editor on ordinary screens.
            self.task_vertical_split.sashpos(0,int(self.task_vertical_split.winfo_height()*0.43))
            self.body.focus_set()

    def hide_editor(self):
        if self.editor_open:
            self.keep_draft()
            self.task_vertical_split.forget(self.task_editor_panel)
            self.editor_open=False

    def toggle_source_panel(self):
        if self.source_panel_visible:
            self.tasks_split.forget(self.task_source_panel)
            self.source_panel_visible=False
            self.source_toggle_button.configure(text='查看文档预览')
        else:
            self.tasks_split.insert(0,self.task_source_panel,weight=1)
            self.source_panel_visible=True
            self.source_toggle_button.configure(text='隐藏文档预览')
            self._source_sash_after_id=self.after(
                80,lambda:self.tasks_split.sashpos(0,int(self.tasks_split.winfo_width()*0.32))
            )

    def new_task(self):
        self.require_doc()
        # Users can create a task while watching the trash or a narrow search.
        # Switch to live tasks first, otherwise saving appears to do nothing.
        self.checked_ids.clear();self.filter.set('全部');self.query.set('')
        self.refresh_tasks()
        CompactStudioV4.new_task(self)
        self._task_focus_id=None
        self.show_editor()
        self.status.set('新建任务编辑器已打开。填写标题、章节和正文；图片可插入光标位置，完成后点击「保存任务」。')

    def focused_task_id(self):
        """Resolve the one task targeted by edit/insert/image actions.

        Checkbox selections are for batch operations. A focused row takes priority
        so stale checked IDs cannot redirect a single-task action to another task.
        """
        if not self.key or not hasattr(self, 'list'):
            return None
        # An open new-task editor is an explicit target; don't fall back to the
        # previously highlighted list row while the user is composing a new task.
        if self.editor_open and self.rid is None:
            return None
        visible=set(self.visible_ids)
        selection={int(x) for x in self.list.selection() if str(x).isdigit()}
        focus=self.list.focus()
        # The row currently highlighted in the UI always beats an older focus
        # value or a checkbox selection used for a previous batch operation.
        if focus and str(focus).isdigit() and int(focus) in selection:
            return int(focus)
        if len(selection)==1:
            return next(iter(selection))
        if focus and str(focus).isdigit() and int(focus) in visible and int(focus) in self.checked_ids:
            return int(focus)
        active=getattr(self,'_task_focus_id',None)
        if active in visible:
            return active
        checked=set(self.checked_ids)&visible
        if len(checked)==1:
            return next(iter(checked))
        if self.editor_open and self.rid in visible:
            return self.rid
        return None

    def insert_after_selected(self):
        self.require_doc()
        if self.filter.get()=='回收站':
            raise ValueError('不能在回收站任务后插入，请先恢复任务或切换到「全部」。')
        rid=self.focused_task_id()
        if rid is None:
            raise ValueError('请单击一条任务标题行，或只勾选一条任务，作为插入位置。')
        row=self.s.get_requirement(rid);self.keep_draft()
        self.insert_after_id=row.id
        self.checked_ids.clear()
        self.load_editor(None)
        self.chaptervar.set(row.section or '未分类')
        self.insert_mode_var.set('选中任务之后')
        self.titlevar.set('');self.put(self.body,'');self.done_var.set(False)
        self._task_focus_id=None
        self.show_page('tasks');self.show_editor();self.body.focus_set()
        self.status.set(f'新任务将插入到「{row.title}」之后。')

    def edit_selected(self):
        self.require_doc()
        rid=self.focused_task_id()
        if rid is None:
            raise ValueError('请单击一条任务标题行，或只勾选一条任务，再点击「编辑任务」。')
        self.keep_draft();self.load_editor(rid);self.show_page('tasks')
        self.show_editor();self.body.focus_set()

    def _prepare_image_edit(self):
        """Prepare the selected task editor while preserving the text insertion mark."""
        self.require_doc()
        rid=self.focused_task_id()
        if rid is not None:
            if self.rid != rid:
                self.keep_draft()
                self.load_editor(rid)
                # A newly loaded task starts at the end; users can click elsewhere first.
                self.body.mark_set('insert','end-1c')
        elif self.editor_open:
            # The open new-task editor is the explicit target; keep its caret.
            pass
        else:
            raise ValueError('请先选择一条任务，或点击「新增任务」打开任务编辑器。')
        self.show_page('tasks');self.show_editor();self.body.focus_set()
        return self.rid

    def add_image(self):
        self.require_doc()
        if self.focused_task_id() is None and not (self.editor_open and self.rid is None):
            raise ValueError('请先选择一条任务，或点击「新增任务」打开任务编辑器。')
        # Open the picker first; cancelling does not modify the task or its caret.
        path=filedialog.askopenfilename(parent=self,filetypes=[('图片','*.png *.jpg *.jpeg *.webp *.bmp *.gif')])
        if not path:return False
        self._prepare_image_edit()
        result=Studio.insert_image(self,Path(path))
        if result:self.status.set('图片已插入正文光标位置；点击「保存任务」保存，可用「预览正文」检查图片位置。')
        return result

    def _insert_clipboard_images(self,data,silent=False,at_end=False):
        from PIL import Image
        image_exts=('.png','.jpg','.jpeg','.webp','.bmp','.gif')
        if isinstance(data,Image.Image):
            with tempfile.TemporaryDirectory() as td:
                path=Path(td)/'剪贴板图片.png';data.save(path)
                if at_end:self.body.mark_set('insert','end-1c')
                return self.insert_image(path)
        if isinstance(data,list):
            paths=[Path(item) for item in data if isinstance(item,(str,os.PathLike)) and Path(item).suffix.lower() in image_exts]
            if paths:
                inserted=0
                for path in paths:
                    if at_end:self.body.mark_set('insert','end-1c')
                    if self.insert_image(path):inserted+=1
                return inserted>0
        if not silent:messagebox.showinfo('粘贴图片','剪贴板中没有可用图片。',parent=self)
        return False

    def paste_image(self,silent=False):
        self.require_doc()
        from PIL import ImageGrab, Image
        data=ImageGrab.grabclipboard()
        has_image=isinstance(data,Image.Image) or (isinstance(data,list) and any(
            isinstance(item,(str,os.PathLike)) and Path(item).suffix.lower() in ('.png','.jpg','.jpeg','.webp','.bmp','.gif') for item in data))
        if not has_image:
            if not silent:messagebox.showinfo('粘贴图片','剪贴板中没有可用图片。',parent=self)
            return False
        if self.focused_task_id() is None and not (self.editor_open and self.rid is None):
            raise ValueError('请先选择一条任务，或点击「新增任务」打开任务编辑器。')
        self._prepare_image_edit()
        result=self._insert_clipboard_images(data,silent=True,at_end=False)
        if result:self.status.set('剪贴板图片已插入正文光标位置；点击「保存任务」保存。')
        return result

    def paste_event(self,event=None):
        # Ctrl+V inside the editor preserves the caret; toolbar paste appends at end.
        try:
            from PIL import ImageGrab, Image
            data=ImageGrab.grabclipboard()
            has_image=isinstance(data,Image.Image) or (isinstance(data,list) and any(
                isinstance(item,(str,os.PathLike)) and Path(item).suffix.lower() in ('.png','.jpg','.jpeg','.webp','.bmp','.gif') for item in data))
            if has_image and self._insert_clipboard_images(data,silent=True,at_end=False):return 'break'
        except Exception as exc:
            logging.exception('Paste image failed')
            self.status.set('粘贴图片失败：'+str(exc))
        return None

    def preview_task_body(self):
        """Render the current task Markdown, including images, in a read-only preview."""
        self.require_doc()
        content=self.body.get('1.0','end-1c')
        win=tk.Toplevel(self);win.title('任务正文预览 — '+(self.titlevar.get().strip() or '未命名任务'))
        self.sized_dialog(win,900,720)
        view=MarkdownView(win,self.theme);view.pack(fill='both',expand=True,padx=10,pady=10)
        view.set_base_dir(self.s.document_folder(self.key))
        view.set_markdown(content or '（当前任务正文为空）')
        return win

    def save_task(self):
        result=CompactStudioV4.save_task(self)
        self._task_focus_id=self.rid
        self.hide_editor()
        return result

    def cancel_edit(self):
        CompactStudioV4.cancel_edit(self)
        self.hide_editor()

    def edit_progress_task(self,event=None):
        CompactStudioV4.edit_progress_task(self,event)
        self.show_editor()

    def refresh_task_selectors(self,select_key=None):
        if not hasattr(self,'task_project_box'):return
        projects=[p.name for p in self.s.project_groups()]
        key=select_key if select_key is not None else self.key
        project_name=None
        if key:
            try:project_name=self.s.document(key).project_name
            except Exception:project_name=None
        if project_name not in projects:
            project_name=self.current_project_name if self.current_project_name in projects else (projects[0] if projects else '')
        self._task_selector_loading=True
        self.task_project_box.configure(values=projects);self.task_project_var.set(project_name or '')
        docs=self.s.documents_for(project_name) if project_name else []
        labels=[];self.task_doc_map={};duplicates={}
        for doc in docs:duplicates[doc.name]=duplicates.get(doc.name,0)+1
        for doc in docs:
            label=doc.name+(f' [{doc.key[:8]}]' if duplicates[doc.name]>1 else '')
            labels.append(label);self.task_doc_map[label]=doc.key
        self.task_document_box.configure(values=labels)
        selected=next((label for label,doc_key in self.task_doc_map.items() if doc_key==key),None)
        if selected is None and labels and not key:selected=labels[0]
        self.task_document_var.set(selected or '')
        self.task_document_box.configure(state='readonly' if labels else 'disabled')
        self._task_selector_loading=False
        return labels

    def on_task_project_selected(self,event=None):
        if self._task_selector_loading:return
        project=self.task_project_var.get()
        if not project:return
        self.keep_draft();self.persist_notes();self.current_project_name=project
        self.checked_ids.clear();self.visible_ids=[];self.insert_after_id=None;self.selected_source_id=None;self._task_focus_id=None
        labels=self.refresh_task_selectors('')
        if labels:self.on_task_document_selected()
        else:self.clear_task_document('该项目下还没有文档。请先创建文档。')

    def on_task_document_selected(self,event=None):
        if self._task_selector_loading:return
        key=self.task_doc_map.get(self.task_document_var.get())
        if not key:return
        if key!=self.key:self.load_doc(key)
        else:self.refresh_task_source()

    def clear_task_document(self,message='尚未选择文档。'):
        self.keep_draft();self.persist_notes()
        self.key=None;self.rid=None;self.checked_ids.clear();self.visible_ids=[];self.insert_after_id=None;self.selected_source_id=None;self._task_focus_id=None
        self._refreshing_tasks=True;self.list.delete(*self.list.get_children());self._refreshing_tasks=False
        self.load_editor(None);self.tasks_context.configure(text=message)
        self.task_source_var.set('');self.task_source_box.configure(values=[])
        self.task_source_open.configure(state='disabled');self.task_source_edit.configure(state='disabled');self.task_table_to_tasks_button.configure(state='disabled')
        self.task_source_meta.configure(text=message);self.task_source_view.set_markdown(message)
        self._refresh_source_table('')
        self.refresh_home()
        if hasattr(self,'context_label'):self.context_label.configure(text='当前文档：尚未选择')

    def refresh_task_source(self):
        if not hasattr(self,'task_source_view'):return
        if not self.key:
            self._task_source_loading=True
            self.selected_source_id=None;self.task_source_map={}
            self.task_source_var.set('');self.task_source_box.configure(values=[])
            self.task_source_meta.configure(text='请先选择项目和文档。')
            self.task_source_view.set_markdown('请先选择项目和文档。')
            self.task_source_open.configure(state='disabled')
            self.task_source_edit.configure(state='disabled')
            self.task_table_to_tasks_button.configure(state='disabled')
            self._refresh_source_table('')
            self._task_source_loading=False
            return
        sources=self.store.sources(self.key);self._task_source_loading=True;self.task_source_map={}
        if sources:
            labels=[]
            for sid,name,stored,content,created in sources:
                label=f'{name} · {created[:16]}'
                if label in self.task_source_map:label+=f' [{sid[:6]}]'
                labels.append(label);self.task_source_map[label]=sid
            selected=next((label for label,sid in self.task_source_map.items() if sid==self.selected_source_id),None)
            if selected is None:selected=labels[0];self.selected_source_id=self.task_source_map[selected]
            self.task_source_box.configure(values=labels,state='readonly');self.task_source_var.set(selected)
            row=next(r for r in sources if r[0]==self.selected_source_id)
            self.task_source_view.set_base_dir(self.s.document_folder(self.key));self.task_source_view.set_markdown(row[3])
            self.task_source_meta.configure(text=f'来源文件：{row[1]} · 提取文本只读预览（原件已归档）')
            self.task_source_open.configure(state='normal');self.task_source_edit.configure(state='disabled')
            self._refresh_source_table(row[3])
        else:
            self.selected_source_id=None;self.task_source_map={}
            self.task_source_box.configure(values=['文档任务汇总'],state='disabled');self.task_source_var.set('文档任务汇总')
            self.task_source_view.set_base_dir(self.s.document_folder(self.key));self.task_source_view.set_markdown(self.store.compose(self.key))
            self.task_source_meta.configure(text='当前文档没有关联原始资料，正在显示任务汇总内容。')
            self.task_source_open.configure(state='disabled');self.task_source_edit.configure(state='disabled');self.task_table_to_tasks_button.configure(state='disabled')
            self._refresh_source_table(self.store.compose(self.key))
        self._task_source_loading=False

    def _refresh_source_table(self,content):
        from reqdoc_source_table import tables_in
        old_tab=self.task_source_book.select()
        blocks=tables_in(content)
        grid=self.task_table_grid
        grid.delete(*grid.get_children())
        if not blocks:
            grid.configure(displaycolumns=())
            grid.configure(columns=())
            self.task_source_book.tab(self.task_table_page,state='disabled')
            self.task_table_to_tasks_button.configure(state='disabled')
            if old_tab==str(self.task_table_page):self.task_source_book.select(0)
            return
        table=blocks[0]
        cols=['col'+str(i) for i in range(len(table.headers))]
        grid.configure(displaycolumns=())
        grid.configure(columns=cols)
        grid.configure(displaycolumns=cols)
        for column,header in zip(cols,table.headers):
            grid.heading(column,text=header)
            grid.column(column,width=max(145,min(280,len(header)*16)),minwidth=95,stretch=True)
        for i,row in enumerate(table.rows):
            grid.insert('','end',iid=str(i),values=row)
        self.task_source_book.tab(self.task_table_page,state='normal')
        self.task_table_to_tasks_button.configure(state='normal' if self.selected_source_id else 'disabled')
        if old_tab==str(self.task_table_page):
            self.task_source_book.select(self.task_table_page)
        elif self.selected_source_id and not getattr(self,'_auto_table_seen',{}).get(self.selected_source_id):
            self.task_source_book.select(self.task_table_page)
            if not hasattr(self,'_auto_table_seen'):self._auto_table_seen={}
            self._auto_table_seen[self.selected_source_id]=True

    def _table_to_clipboard(self,text):
        self.clipboard_clear();self.clipboard_append(text)
        self.update_idletasks()
        self.status.set('表格已复制，可粘贴到 Excel、记事本或其他程序。')
        return 'break'

    def copy_current_table(self):
        from reqdoc_table_clipboard import rows_to_tsv
        grid=self.task_table_grid
        cols=grid['columns']
        if not cols:raise ValueError('当前没有可复制的表格。')
        headers=[grid.heading(col,'text') for col in cols]
        rows=[grid.item(iid,'values') for iid in grid.get_children()]
        return self._table_to_clipboard(rows_to_tsv(headers,rows))

    def copy_table_rows(self,grid):
        from reqdoc_table_clipboard import rows_to_tsv
        selected=set(grid.selection())
        if not selected:raise ValueError('请先选中要复制的表格行；复制整表请点击「复制整张表」。')
        rows=[grid.item(iid,'values') for iid in grid.get_children() if iid in selected]
        return self._table_to_clipboard(rows_to_tsv([],rows))

    def copy_current_table_markdown(self):
        from reqdoc_source_table import table_to_markdown
        grid=self.task_table_grid
        cols=grid['columns']
        if not cols:raise ValueError('当前没有可复制的表格。')
        headers=[grid.heading(col,'text') for col in cols]
        rows=[list(grid.item(iid,'values')) for iid in grid.get_children()]
        return self._table_to_clipboard(table_to_markdown(headers,rows))

    def edit_task_source(self):
        raise ValueError('原始资料为只读归档，不在这里编辑。请在任务列表中编辑软件整理后的任务。')

    def table_rows_to_tasks(self):
        if not self.key or not self.selected_source_id:
            raise ValueError('请先选择一份资料表格。')
        source=next((r for r in self.store.sources(self.key) if r[0]==self.selected_source_id),None)
        if not source:raise ValueError('所选资料已经不存在。')
        tables=tables_in(source[3])
        if not tables:raise ValueError('这份资料中未识别到表格。')
        table=tables[0]
        if not table.rows:raise ValueError('当前表格没有可转换的数据行。')
        if len(table.rows)>500:raise ValueError('最多一次转换 500 行。')
        cols='\n'.join(f'{i+1}：{name}' for i,name in enumerate(table.headers))
        title_col=simpledialog.askinteger('将表格转换成任务',
            '选择任务标题对应的列（输入数字）：\n'+cols,
            parent=self,minvalue=1,maxvalue=len(table.headers))
        if title_col is None:return
        if not messagebox.askyesno('确认生成任务',
            f'将第一张表的 {len(table.rows)} 行转换为待办任务。\n转换不会删除资料表。\n重复执行会创建重复任务，是否继续？',parent=self):return
        items=table_to_task_items(table,'','',title_column=title_col-1)
        created=0
        for item in items:
            self.store.insert_task(self.key,item['title'],item['section'],item['content'],chapter_end=True)
            created+=1
        if created:
            self.store.snapshot(self.key);self.refresh_tasks()
            self.refresh_progress()
        self.status.set(f'已从表格生成 {created} 条任务，可以逐条标记和编辑。')

    def on_task_source_selected(self,event=None):
        if self._task_source_loading:return
        self.selected_source_id=self.task_source_map.get(self.task_source_var.get())
        self.refresh_task_source()

    def archived_source_path(self,stored):
        root=Path(self.store.settings.docs_dir).resolve()
        raw=Path(str(stored)).expanduser()
        target=(raw if raw.is_absolute() else root/raw).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError('原始资料路径不在当前文档归档目录内，已阻止打开。') from exc
        if not target.is_file():
            raise FileNotFoundError('原始资料文件不存在或已移动：'+str(target))
        return target

    def open_task_source(self):
        if not self.key or not self.selected_source_id:
            raise ValueError('当前文档没有可打开的原始文件。')
        row=next((r for r in self.store.sources(self.key) if r[0]==self.selected_source_id),None)
        if not row:
            raise ValueError('原始资料记录已变化，请刷新后重试。')
        self.open_path(self.archived_source_path(row[2]))

    def on_document_tree_select(self,event=None):
        sel=self.document_list.selection()
        if not sel:return
        iid=sel[0]
        if iid.startswith('p:'):self.current_project_name=self.document_list.item(iid,'text')
        else:self.current_project_name=self.document_project_map.get(iid,self.current_project_name)

    def build_progress_page(self):
        page=self.pages['progress']
        self.page_title(page,'进度统计','选择全部项目或单独项目；点击下方统计分组查看任务。')
        filters=ttk.LabelFrame(page,text='统计项目',padding=9)
        filters.pack(fill='x',pady=(0,10))
        ttk.Label(filters,text='项目').pack(side='left',padx=(0,6))
        self.progress_scope_var=tk.StringVar(value='全部项目')
        self.progress_scope_box=ttk.Combobox(filters,textvariable=self.progress_scope_var,values=['全部项目'],state='readonly',width=32)
        self.progress_scope_box.pack(side='left',padx=(0,10))
        self.progress_scope_box.bind('<<ComboboxSelected>>',lambda e:self.refresh_progress(reset_group=True))
        ttk.Button(filters,text='刷新统计',command=lambda:self.refresh_progress(reset_group=True)).pack(side='right')
        cards=ttk.Frame(page);cards.pack(fill='x',pady=(0,9))
        self.progress_total=tk.StringVar(value='—');self.progress_done=tk.StringVar(value='—');self.progress_open=tk.StringVar(value='—');self.progress_rate=tk.StringVar(value='—')
        for label,var in [('任务总数',self.progress_total),('已完成',self.progress_done),('未完成',self.progress_open),('完成率',self.progress_rate)]:
            box=ttk.LabelFrame(cards,text=label,padding=8);box.pack(side='left',fill='both',expand=True,padx=(0,7))
            ttk.Label(box,textvariable=var,font=('Microsoft YaHei',16,'bold')).pack(anchor='center')
        self.progress_bar=ttk.Progressbar(page,orient='horizontal',mode='determinate',maximum=100);self.progress_bar.pack(fill='x',pady=(0,9))
        split=ttk.Panedwindow(page,orient='vertical');split.pack(fill='both',expand=True)
        group_box=ttk.LabelFrame(split,text='项目 / 文档统计分组',padding=7);split.add(group_box,weight=2)
        self.progress_chapters=ttk.Treeview(group_box,columns=('group','total','done','pending','rate'),show='headings',height=6)
        for col,label,width in [('group','统计分组',390),('total','任务总数',90),('done','已完成',90),('pending','未完成',90),('rate','完成率',90)]:
            self.progress_chapters.heading(col,text=label);self.progress_chapters.column(col,width=width,minwidth=70,stretch=col=='group',anchor='w' if col=='group' else 'center')
        self.progress_chapters.pack(fill='both',expand=True);self.progress_chapters.bind('<<TreeviewSelect>>',lambda e:self.refresh_progress_tasks())
        task_box=ttk.LabelFrame(split,text='任务明细（点击状态可切换；双击任务可编辑）',padding=7);split.add(task_box,weight=3)
        self.progress_tasks=ttk.Treeview(task_box,columns=('state','title','chapter','document'),show='headings',selectmode='extended',height=7)
        for col,label,width in [('state','状态',85),('title','任务标题',330),('chapter','章节',210),('document','所属文档',220)]:
            self.progress_tasks.heading(col,text=label);self.progress_tasks.column(col,width=width,minwidth=65,stretch=col in ('title','chapter','document'),anchor='w' if col!='state' else 'center')
        self.progress_tasks.pack(fill='both',expand=True);self.progress_tasks.bind('<Button-1>',self.progress_task_click);self.progress_tasks.bind('<Double-1>',lambda e:self.guard(self.edit_progress_task))
        tools=ttk.Frame(task_box);tools.pack(fill='x',pady=(6,0))
        ttk.Button(tools,text='标记完成',command=lambda:self.guard(lambda:self.mark_progress(True))).pack(side='left',padx=(0,6))
        ttk.Button(tools,text='标记未完成',command=lambda:self.guard(lambda:self.mark_progress(False))).pack(side='left')
        self.progress_hint=ttk.Label(page,text='尚无可统计的任务。',foreground='#64748B');self.progress_hint.pack(anchor='w',pady=(5,0))

    def build_export_page(self):
        page=self.pages['export'];self.page_title(page,'文档导出','选择一份文档后，预览或导出其已保存任务。')
        self.export_doc_label=ttk.Label(page,text='当前没有选择文档。',foreground='#64748B');self.export_doc_label.pack(anchor='w',pady=(0,12))
        box=ttk.LabelFrame(page,text='导出操作',padding=18);box.pack(fill='x')
        ttk.Button(box,text='预览文档汇总',command=lambda:self.guard(self.show_preview)).pack(anchor='w',pady=5)
        ttk.Button(box,text='导出 Word 文档',command=lambda:self.guard(lambda:self.export('docx'))).pack(anchor='w',pady=5)
        ttk.Button(box,text='导出 Markdown 与图片包',command=lambda:self.guard(lambda:self.export('zip'))).pack(anchor='w',pady=5)
        ttk.Label(box,text='导出按照当前任务顺序和章节组织；原始导入文件不会被覆盖。',foreground='#64748B').pack(anchor='w',pady=(10,0))

    def build_settings_page(self):
        page=self.pages['settings'];self.page_title(page,'设置与数据维护','V8 数据独立保存在本机用户目录；迁移只接受经过校验的 V8 数据目录或备份，不覆盖现有项目。')
        box=ttk.LabelFrame(page,text='数据备份与迁移',padding=16);box.pack(fill='x',pady=(0,12))
        ttk.Button(box,text='备份全部数据',command=lambda:self.guard(self.backup)).pack(anchor='w',pady=4)
        ttk.Button(box,text='从 V8 备份 ZIP 导入数据',command=lambda:self.guard(self.migrate_from_backup)).pack(anchor='w',pady=4)
        ttk.Button(box,text='从另一份 V8 数据目录导入',command=lambda:self.guard(self.migrate_from_folder)).pack(anchor='w',pady=4)
        ttk.Button(box,text='恢复备份为独立副本（不覆盖当前数据）',command=lambda:self.guard(self.restore_backup)).pack(anchor='w',pady=4)
        ttk.Button(box,text='打开当前数据目录',command=lambda:self.guard(lambda:self.open_path(Path(self.store.settings.home).parent))).pack(anchor='w',pady=4)
        ttk.Button(box,text='打开文档目录',command=lambda:self.guard(lambda:self.open_path(self.store.settings.docs_dir))).pack(anchor='w',pady=4)
        ttk.Button(box,text='使用说明',command=lambda:self.guard(self.help)).pack(anchor='w',pady=4)
        ttk.Label(page,text='导入前会自动备份当前数据；目标已有项目或文档时会拒绝覆盖。旧版数据库格式不会被强行套用到 V8。',foreground='#64748B',wraplength=980,justify='left').pack(anchor='w',pady=8)
        ttk.Label(page,text='当前数据位置：'+str(Path(self.store.settings.home).parent),foreground='#52647B',wraplength=980,justify='left').pack(anchor='w',pady=(8,3))
        ttk.Label(page,text='重要：V8 用户数据位于 %LOCALAPPDATA%\\ReqDocStudio\\data，与程序文件分离。软件更新不覆盖该目录；卸载或清理前请先备份。',foreground='#9A5C22',wraplength=980,justify='left').pack(anchor='w',pady=(3,8))
        update_box=ttk.LabelFrame(page,text='软件更新 · GitHub',padding=12);update_box.pack(fill='x',pady=(10,0))
        self.update_status_var=tk.StringVar(value=f'当前版本：V{APP_VERSION} · GitHub 单仓库更新')
        ttk.Label(update_box,textvariable=self.update_status_var,foreground='#52647B').pack(side='left',fill='x',expand=True)
        self.update_button=ttk.Button(update_box,text='检查 GitHub 更新',command=lambda:self.guard(self.check_github_update))
        self.update_button.pack(side='right')
        self._update_queue=None;self._update_in_progress=False;self._update_poll_after_id=None


    def check_github_update(self):
        """Read public update manifest in a worker; never block Tk for network IO."""
        import queue, threading
        if getattr(self,'_closing',False) or getattr(self,'_update_in_progress',False):return
        self._update_in_progress=True
        self.update_button.configure(state='disabled')
        self.update_status_var.set('正在检查 GitHub 最新版本…')
        self._update_queue=queue.Queue()
        def worker():
            try:
                from reqdoc_updater import fetch_latest
                data=fetch_latest()
                self._update_queue.put(('found',data))
            except Exception as exc:
                self._update_queue.put(('error',str(exc)))
        threading.Thread(target=worker,name='reqdoc-update-check',daemon=True).start()
        self._update_poll_after_id=self.after(180,self._poll_github_update)

    def _poll_github_update_impl(self):
        import queue
        if getattr(self,'_closing',False):return
        self._update_poll_after_id=None
        q=self._update_queue
        if q is None:return
        received=False
        while True:
            try:kind,payload=q.get_nowait()
            except queue.Empty:break
            received=True
            if kind=='progress':
                got,total=payload
                self.update_status_var.set(f'正在下载更新：{got / 1048576:.1f} / {total / 1048576:.1f} MB')
            elif kind=='error':
                self._update_in_progress=False;self.update_button.configure(state='normal')
                self.update_status_var.set('更新失败：'+payload[:180])
                messagebox.showwarning('GitHub 更新检查',payload,parent=self)
            elif kind=='found':
                from reqdoc_updater import APP_VERSION, update_available, version_tuple
                self._update_in_progress=False;self.update_button.configure(state='normal')
                if not update_available(payload):
                    self.update_status_var.set(f'已是最新版本：{APP_VERSION}')
                    messagebox.showinfo('检查完成',f'当前版本 {APP_VERSION} 已是最新发布版本。',parent=self)
                elif version_tuple(payload['minimum_supported_version'])>version_tuple(APP_VERSION):
                    self.update_status_var.set('需要先安装中间兼容版本')
                    messagebox.showwarning('更新不可直接安装','当前版本低于直接升级支持范围，请从 GitHub Releases 获取完整安装包。',parent=self)
                else:
                    self.update_status_var.set('发现版本 '+payload['version'])
                    notes=payload.get('notes','')
                    if messagebox.askyesno('发现新版本',f'发现新版本：{payload["version"]}\n当前版本：{APP_VERSION}\n\n{notes}\n\n现在下载并安装吗？\n更新前会自动备份数据。',parent=self):
                        self._begin_github_download(payload)
            elif kind=='downloaded':
                self._update_in_progress=False;self.update_button.configure(state='normal')
                self.update_status_var.set('已下载并通过 SHA-256 和 ZIP 校验')
                if messagebox.askyesno('准备安装更新','安装包已经验证完毕。\n\n现在备份数据、退出程序并安装更新吗？',parent=self):
                    self._install_github_update(payload)
        if self._update_in_progress and not getattr(self,'_closing',False):
            try:self._update_poll_after_id=self.after(180,self._poll_github_update)
            except tk.TclError:
                self._closing=True
                self._update_in_progress=False

    def _poll_github_update(self):
        """Guard the asynchronous updater callback against a destroyed Tk root."""
        if getattr(self, '_closing', False):
            return
        try:
            self._poll_github_update_impl()
        except tk.TclError:
            self._closing = True
            self._update_in_progress = False
            self._update_poll_after_id = None
        except Exception as exc:
            logging.exception('GitHub update polling failed')
            if getattr(self, '_closing', False):
                return
            self._update_in_progress = False
            self._update_poll_after_id = None
            try:
                self.update_button.configure(state='normal')
                self.update_status_var.set('更新检查异常：' + str(exc)[:180])
            except tk.TclError:
                self._closing = True

    def _begin_github_download(self,manifest):
        import queue,threading
        if getattr(self,'_closing',False) or getattr(self,'_update_in_progress',False):return
        self._update_in_progress=True;self.update_button.configure(state='disabled')
        self.update_status_var.set('准备下载更新…')
        self._update_queue=queue.Queue()
        def worker():
            try:
                from reqdoc_updater import download_update
                def progress(got,total):self._update_queue.put(('progress',(got,total)))
                path=download_update(manifest,progress=progress)
                self._update_queue.put(('downloaded',path))
            except Exception as exc:self._update_queue.put(('error',str(exc)))
        threading.Thread(target=worker,name='reqdoc-update-download',daemon=True).start()
        self._update_poll_after_id=self.after(180,self._poll_github_update)

    def _install_github_update(self,package):
        import subprocess
        from reqdoc_updater import APP_VERSION
        if sys.platform!='win32':raise ValueError('自动安装功能仅适用于 Windows。')
        install_dir=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent.parent
        if not (install_dir/'ReqDocStudio.exe').is_file():
            raise ValueError('当前程序目录不是有效的 ReqDoc Studio V8 安装目录。')
        from reqdoc_updater import validate_zip
        validate_zip(package)
        with zipfile.ZipFile(package,'r') as archive:
            updater_bytes=archive.read('updater.ps1')
        if not updater_bytes or len(updater_bytes)>65536:
            raise ValueError('更新包中的独立更新器内容无效。')
        self.keep_draft();self.persist_notes()
        data_backup=self.store.backup()
        import hashlib
        h=hashlib.sha256()
        with Path(package).open('rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
        powershell=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe'
        if not powershell.is_file():raise ValueError('未找到 Windows PowerShell，无法启动独立更新器。')
        # Run the updater shipped inside the verified package, not a possibly stale
        # bootstrap script from the currently installed V8.0.0 directory.
        staged_script=Path(package).parent/'updater-run.ps1'
        write_atomic(staged_script,updater_bytes)
        args=[str(powershell),'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
              '-File',str(staged_script),'-InstallDir',str(install_dir),'-PackagePath',str(package),
              '-AppPid',str(os.getpid()),'-Sha256',h.hexdigest()]
        subprocess.Popen(args,cwd=str(install_dir),creationflags=subprocess.CREATE_NO_WINDOW)
        self.status.set('数据已备份：'+str(data_backup)+'。正在退出并安装更新。')
        self.close()

    def show_page(self,name):
        if name=='home':name='projects'
        if name in ('import','export'):name='tasks'
        # The retired 'documents' page is retained for compatibility tests but is not navigable.
        super().show_page(name)
        # Legacy base class updates button backgrounds as if this were a dark
        # sidebar. Restore the new topbar's active/inactive states after routing.
        for page,button in self.nav_buttons.items():
            active=page==name
            button.configure(bg='#EEEAFB' if active else '#FFFFFF',
                             fg='#5541AD' if active else '#586478',
                             activebackground='#E4DDF8')
        if name=='tasks':
            self.refresh_task_selectors(self.key)
            if not self.key and self.task_doc_map:self.on_task_document_selected()
        if hasattr(self,'context_label'):
            if self.key:
                d=self.s.document(self.key)
                self.context_label.configure(text=f'当前文档：{d.project_name} / {d.name}')
            else:self.context_label.configure(text='当前文档：尚未选择')

    def refresh_tree(self,select=None):
        if not hasattr(self,'tree'):
            return
        self._sidebar_refreshing=True
        try:
            previously_selected=self.tree.selection()
            opened={
                iid for iid in self.tree.get_children()
                if self.tree.item(iid,'open')
            }
            query=self.sidebar_query.get().strip().casefold() if hasattr(self,'sidebar_query') else ''
            self.tree.delete(*self.tree.get_children())
            for project in self.s.project_groups():
                children=list(self.s.documents_for(project.name))
                project_match=query in project.name.casefold() if query else True
                visible=children if project_match else [
                    doc for doc in children if query in doc.name.casefold()
                ]
                if not project_match and not visible:
                    continue
                pid='p:'+str(project.id)
                self.tree.insert('', 'end',iid=pid,text=project.name,
                                 open=True if query else (not opened or pid in opened),
                                 values=(project.name,))
                for doc in visible:
                    self.tree.insert(pid,'end',iid='d:'+doc.key,
                                     text=doc.name,values=(project.name,doc.key))
            desired='d:'+select if select else (previously_selected[0] if previously_selected else '')
            if desired and self.tree.exists(desired):
                self.tree.selection_set(desired)
                self.tree.see(desired)
        finally:
            self._sidebar_refreshing=False
        if hasattr(self,'project_list'):self.refresh_projects_page()
        if hasattr(self,'document_list'):self.refresh_documents_page()
        if hasattr(self,'import_sources'):self.refresh_import_page()
        if hasattr(self,'progress_chapters'):self.refresh_progress()
        if hasattr(self,'task_project_box'):self.refresh_task_selectors(self.key)

    def selected_project(self):
        if self.pages['projects'].winfo_manager() and self.project_list.selection():
            iid=self.project_list.selection()[0]
            if iid.startswith('d:'):
                return self.s.document(iid[2:]).project_name
            if iid.startswith('project-'):
                return self.project_list.item(iid,'values')[0]
        if self.pages['tasks'].winfo_manager() and hasattr(self,'task_project_var'):
            value=self.task_project_var.get()
            if value and any(p.name==value for p in self.s.project_groups()):return value
        if self.pages['documents'].winfo_manager() and self.doc_project_var.get()!='全部项目':
            return self.doc_project_var.get()
        if self.current_project_name and any(p.name==self.current_project_name for p in self.s.project_groups()):
            return self.current_project_name
        if self.key:return self.s.document(self.key).project_name
        projects=self.s.project_groups()
        if len(projects)==1:return projects[0].name
        raise ValueError('请先在“项目与文档”中选择一个项目。')

    def refresh_projects_page(self):
        if not hasattr(self,'project_list'):return
        old=self.project_list.selection();selected=old[0] if old else None
        opened={iid:self.project_list.item(iid,'open') for iid in self.project_list.get_children()}
        self._refreshing_project_tree=True
        try:
            self.project_list.delete(*self.project_list.get_children())
            initial=None
            projects=list(self.s.project_groups())
            if hasattr(self.store,'sort_tree_items'):
                projects=self.store.sort_tree_items('project','root',projects,lambda p:str(p.id))
            for project in projects:
                docs=list(self.s.documents_for(project.name))
                if hasattr(self.store,'sort_tree_items'):
                    docs=self.store.sort_tree_items('document',str(project.id),docs,lambda d:d.key)
                rows_by_doc={doc.key:self.store.tasks(doc.key) for doc in docs}
                rows=[r for doc in docs for r in rows_by_doc[doc.key]]
                done=sum(bool(task.done) for task in rows)
                pid='project-'+str(project.id)
                self.project_list.insert('','end',iid=pid,text=project.name,
                    values=(project.name,len(docs),len(rows),done,f'{done/len(rows):.0%}' if rows else '—'),open=opened.get(pid,True))
                for doc in docs:
                    tasks=rows_by_doc[doc.key];d=sum(bool(x.done) for x in tasks)
                    self.project_list.insert(pid,'end',iid='d:'+doc.key,text=doc.name,
                        values=(project.name,'',len(tasks),d,f'{d/len(tasks):.0%}' if tasks else '—'))
                if project.name==self.current_project_name:initial=pid
            if selected and self.project_list.exists(selected):self.project_list.selection_set(selected)
            elif self.key and self.project_list.exists('d:'+self.key):self.project_list.selection_set('d:'+self.key)
            elif initial and self.project_list.exists(initial):self.project_list.selection_set(initial)
        finally:self._refreshing_project_tree=False

    def project_drag_press(self,event):
        row=self.project_list.identify_row(event.y)
        self._project_drag={'source':row,'start_x':event.x,'start_y':event.y,'moving':False} if row else None

    def project_drag_motion(self,event):
        data=getattr(self,'_project_drag',None)
        if not data:return
        if max(abs(event.x-data['start_x']),abs(event.y-data['start_y']))<7:return
        data['moving']=True
        row=self.project_list.identify_row(event.y)
        if row and row!=data['source']:
            self.status.set('松开鼠标移动位置：'+self.project_list.item(row,'text'))

    def project_drag_release(self,event):
        data=getattr(self,'_project_drag',None);self._project_drag=None
        if not data or not data['moving']:return
        target=self.project_list.identify_row(event.y)
        if not target or not self.project_list.exists(data['source']):return
        source=data['source']
        if source==target:return
        def act():
            self.keep_draft();self.persist_notes()
            parent_source=self.project_list.parent(source)
            parent_target=self.project_list.parent(target)
            # A project may only be reordered alongside projects.
            if source.startswith('project-'):
                target_project=target if target.startswith('project-') else parent_target
                keys=list(self.project_list.get_children())
                if target_project==source or target_project not in keys:return
                keys.remove(source)
                at=keys.index(target_project)
                if target==target_project:
                    bbox=self.project_list.bbox(target_project)
                    if bbox and event.y>bbox[1]+bbox[3]//2:at+=1
                keys.insert(at,source)
                self.store.save_tree_order('project','root',[key[8:] for key in keys])
                self.refresh_projects_page();self.status.set('项目顺序已保存。')
                return
            # Documents may reorder within the same project, or drop onto its parent to move to the end.
            if not source.startswith('d:'):return
            dest=target if target.startswith('project-') else parent_target
            if not dest.startswith('project-'):return
            if dest!=parent_source:
                # The bundled engine has no verified, atomic move-document contract.
                # A visual-only reparent would split the tree and actual document ownership.
                raise ValueError('跨项目移动尚未通过数据迁移验证。此次拖动已取消，文档和任务没有变动。')
            keys=list(self.project_list.get_children(parent_source))
            keys.remove(source)
            if target==dest:at=len(keys)
            else:
                at=keys.index(target)
                bbox=self.project_list.bbox(target)
                if bbox and event.y>bbox[1]+bbox[3]//2:at+=1
            keys.insert(at,source)
            self.store.save_tree_order('document',parent_source[8:],[key[2:] for key in keys])
            self.refresh_projects_page();self.status.set('文档顺序已保存。')
        self.guard(act)

    def on_project_selected(self,event=None):
        if getattr(self,'_refreshing_project_tree',False):return
        if not hasattr(self,'project_list'):return
        selected=self.project_list.selection()
        if not selected:return
        iid=selected[0]
        if iid.startswith('d:'):
            key=iid[2:];doc=self.s.document(key)
            self.current_project_name=doc.project_name
            self.project_context.configure(text=f'已选择文档：{doc.project_name} / {doc.name}。双击文档可直接打开任务处理。')
        else:
            name=self.project_list.item(iid,'values')[0]
            self.current_project_name=name
            self.project_context.configure(text=f'已选择项目：{name}。可直接新建该项目的文档。')

    def open_project_from_list(self,event=None):
        selected=self.project_list.selection()
        if not selected:raise ValueError('请选择一个项目或文档。')
        iid=selected[0]
        if iid.startswith('d:'):
            self.load_doc(iid[2:]);self.show_page('tasks')
        elif iid.startswith('project-'):
            self.project_list.item(iid,open=not bool(self.project_list.item(iid,'open')))
            self.on_project_selected()

    def refresh_documents_page(self):
        if not hasattr(self,'document_list'):return
        projects=self.s.project_groups();project_names=['全部项目']+[p.name for p in projects]
        self.doc_project_box.configure(values=project_names)
        if self.doc_project_var.get() not in project_names:
            self.doc_project_var.set(self.current_project_name if self.current_project_name in project_names else '全部项目')
        filter_name=self.doc_project_var.get()
        previous=self.document_list.selection();previous_key=previous[0][2:] if previous and previous[0].startswith('d:') else None
        self.document_list.delete(*self.document_list.get_children());self.document_map={};self.document_project_map={}
        for project in projects:
            if filter_name!='全部项目' and project.name!=filter_name:continue
            docs=self.s.documents_for(project.name);project_rows=[]
            for doc in docs:project_rows.extend(self.store.tasks(doc.key))
            total=len(project_rows);done_total=sum(bool(r.done) for r in project_rows);pid='p:'+str(project.id)
            self.document_list.insert('','end',iid=pid,text=project.name,values=(total,done_total,f'{done_total/total:.0%}' if total else '—'),open=True)
            for doc in docs:
                rows=self.store.tasks(doc.key);done=sum(bool(r.done) for r in rows);iid='d:'+doc.key
                self.document_map[iid]=doc.key;self.document_project_map[iid]=project.name
                self.document_list.insert(pid,'end',iid=iid,text=doc.name,values=(len(rows),done,f'{done/len(rows):.0%}' if rows else '—'))
        if previous_key and self.document_list.exists('d:'+previous_key):self.document_list.selection_set('d:'+previous_key)

    def open_selected_document(self):
        sel=self.document_list.selection()
        if not sel:raise ValueError('请先选择一份文档。')
        iid=sel[0]
        if iid.startswith('p:'):
            project=self.document_list.item(iid,'text');self.current_project_name=project;self.doc_project_var.set(project);self.refresh_documents_page()
            self.status.set(f'已选择项目“{project}”。请展开项目并选择其中一份文档。')
            return
        key=self.document_map.get(iid)
        if not key:raise ValueError('请选择项目下的文档，而不是项目标题。')
        self.load_doc(key);self.show_page('home')

    def new_project(self):
        name=simpledialog.askstring('新建项目','请输入项目名称',parent=self)
        if not name or not name.strip():return
        project=self.s.create_project_group(name.strip());self.current_project_name=project.name
        self.refresh_tree();self.show_page('projects');self.status.set('项目已创建。请选择该项目并新建文档。')

    def new_document(self):
        project_name=self.selected_project()
        name=simpledialog.askstring('在项目下新建文档',f'所属项目：{project_name}\n请输入文档名称',parent=self)
        if not name or not name.strip():return
        doc=self.s.create_document(project_name,name.strip())
        self.current_project_name=project_name
        self.refresh_tree(doc.key)
        project=next((p for p in self.s.project_groups() if p.name==project_name),None)
        if project and self.project_list.exists('project-'+str(project.id)):
            project_iid='project-'+str(project.id)
            self.project_list.item(project_iid,open=True)
            document_iid='d:'+doc.key
            if self.project_list.exists(document_iid):
                self.project_list.selection_set(document_iid)
                self.project_list.focus(document_iid)
                self.project_list.see(document_iid)
                self.on_project_selected()
        self.show_page('projects')
        self.status.set('已新建文档：'+project_name+' / '+doc.name+'。双击该文档进入任务处理。')

    def delete_project_or_document(self):
        sel=self.project_list.selection()
        if not sel:raise ValueError('请在项目树中选择要删除的项目或文档。')
        item=sel[0]
        self.keep_draft();self.persist_notes()
        if item.startswith('d:'):
            key=item[2:];doc=self.s.document(key)
            count=len(self.s.list_requirements(project=key))
            if not messagebox.askyesno(
                '删除文档',
                f'确定删除文档「{doc.name}」吗？\n所属项目：{doc.project_name}\n包含任务：{count} 条\n\n删除后无法从本机恢复。确定继续？',
                icon='warning',parent=self
            ):return
            self.store.delete_document_permanently(key)
            affected=(self.key==key)
            deleted_kind='文档'
        elif item.startswith('project-'):
            project=self.project_list.item(item,'values')[0]
            docs=list(self.s.documents_for(project))
            count=sum(len(self.s.list_requirements(project=d.key)) for d in docs)
            if not messagebox.askyesno(
                '删除整个项目',
                f'确定删除整个项目「{project}」吗？\n将同时删除 {len(docs)} 份文档、{count} 条任务及归档资料。\n\n此操作不可撤销。',
                icon='warning',parent=self
            ):return
            keys=self.store.delete_project_permanently(project)
            affected=(self.key in keys)
            deleted_kind='项目'
        else:
            raise ValueError('请先选择项目或文档。')
        if affected:
            self.key=None;self.rid=None;self.checked_ids.clear();self.visible_ids=[];self._task_focus_id=None
            self.insert_after_id=None;self.selected_source_id=None
            self.hide_editor();self.clear_task_document('原文档已删除。请重新选择文档。')
        self.current_project_name=None
        self.refresh_tree();self.refresh_tasks();self.refresh_task_selectors('')
        self.status.set(f'{deleted_kind}已删除。')
        messagebox.showinfo('删除完成',f'已删除所选{deleted_kind}。',parent=self)

    def import_for_tree_selection(self):
        selected=self.project_list.selection()
        if not selected or not selected[0].startswith('d:'):
            raise ValueError('请先展开项目并选择要导入资料的文档。')
        key=selected[0][2:]
        if key!=self.key:self.load_doc(key)
        self.import_dialog()

    def rename_project_or_document(self):
        selected=self.project_list.selection()
        if not selected:raise ValueError('请先选中项目或文档。')
        if selected[0].startswith('d:'):return self.rename_document_selected()
        return self.rename_project_selected()

    def rename_project_selected(self):
        selected=self.project_list.selection()
        if not selected or not selected[0].startswith('project-'):
            raise ValueError('请在项目树中选择项目后再重命名。')
        old=self.project_list.item(selected[0],'values')[0]
        new=simpledialog.askstring('重命名项目','新项目名称',initialvalue=old,parent=self)
        if not new or not new.strip():return
        self.s.rename_project_group(old,new.strip())
        if self.current_project_name==old:self.current_project_name=new.strip()
        self.refresh_tree();self.status.set('项目已重命名。')

    def rename_document_selected(self):
        selected=self.project_list.selection()
        if not selected or not selected[0].startswith('d:'):
            raise ValueError('请在项目树中选择一份文档。')
        key=selected[0][2:];old=self.s.document(key).name
        new=simpledialog.askstring('重命名文档','新文档名称',initialvalue=old,parent=self)
        if not new or not new.strip():return
        self.s.rename_document(key,new.strip())
        self.refresh_tree(key);self.refresh_task_selectors(key)
        if self.key==key:self.load_doc(key)
        self.status.set('文档已重命名。')

    def load_doc(self,key):
        if key==self.key:
            self.refresh_task_selectors(key);self.refresh_task_source();return
        # 文档切换时先隔离所有临时状态，避免跨文档残留。
        self.checked_ids.clear();self.visible_ids=[];self.insert_after_id=None;self._task_focus_id=None
        self.progress_task_doc_map={};self.selected_source_id=None
        if hasattr(self,'task_vertical_split'):self.hide_editor()
        CompactStudioV4.load_doc(self,key)
        self.current_project_name=self.s.document(key).project_name
        self.refresh_projects_page();self.refresh_documents_page();self.refresh_import_page();self.refresh_export_page()
        self.refresh_task_selectors(key)
        self.context_label.configure(text=f'当前文档：{self.s.document(key).project_name} / {self.s.document(key).name}')
        self.refresh_task_source()

    def refresh_home(self):
        if not hasattr(self,'home_total'):return
        if not self.key:
            for var in (self.home_total,self.home_done,self.home_open,self.home_rate):var.set('—')
            self.home_hint.configure(text='尚未选择文档。请先到“项目”页面创建项目，再到“文档”页面创建或打开文档。')
            self.heading.configure(text='选择文档，开始整理任务');self.breadcrumb.configure(text='新建文档后，可以选择手写录入或导入资料。')
            return
        rows=self.store.tasks(self.key);total=len(rows);done=sum(bool(r.done) for r in rows);pending=total-done
        self.home_total.set(str(total));self.home_done.set(str(done));self.home_open.set(str(pending));self.home_rate.set(f'{done/total:.0%}' if total else '0%')
        doc=self.s.document(self.key);self.heading.configure(text=doc.name);self.breadcrumb.configure(text=f'项目：{doc.project_name}  /  文档：{doc.name}')
        self.home_hint.configure(text=f'当前文档共有 {total} 条任务，已完成 {done} 条，未完成 {pending} 条。')

    def refresh_tasks(self):
        if not hasattr(self,'list'):return
        CompactStudioV4.refresh_tasks(self)
        selected=self.list.selection()
        if len(selected)==1:self._task_focus_id=int(selected[0])
        elif getattr(self,'_task_focus_id',None) not in set(self.visible_ids):self._task_focus_id=None
        if hasattr(self,'tasks_context'):
            if self.key:
                doc=self.s.document(self.key)
                self.tasks_context.configure(text=f'正在处理：{doc.project_name} / {doc.name}')
            else:
                self.tasks_context.configure(text='请选择项目和文档后开始处理任务。')
        if hasattr(self,'task_source_view') and self.key:self.refresh_task_source()

    def update_count(self):
        if not hasattr(self,'count'):return
        checked=set(self.checked_ids)&set(self.visible_ids)
        active=self.focused_task_id()
        parts=[f'{len(self.list.get_children())} 条显示']
        if checked:parts.append(f'勾选 {len(checked)} 条')
        if active is not None:parts.append('当前行已选')
        elif not checked:parts.append(f'已选 {len(self.list.selection())} 条')
        self.count.configure(text=' · '.join(parts))

    def selected_ids(self):
        """Resolve batch selection, pruning stale IDs after document/filter changes."""
        if not self.key:return []
        is_trash=self.filter.get()=='回收站'
        visible=set(self.visible_ids)
        allowed={r.id for r in self.store.tasks(self.key,trash=is_trash)}
        self.checked_ids.intersection_update(allowed & visible)
        candidates=self.checked_ids if self.checked_ids else {int(x) for x in self.list.selection()}
        return sorted(set(candidates)&allowed&visible)

    def on_task_selected(self,event=None):
        if self._refreshing_tasks:return
        selected=self.list.selection()
        if len(selected)==1:
            rid=int(selected[0]);self.list.focus(str(rid));self._task_focus_id=rid
            if rid!=self.rid:
                # Preserve unsaved edits before switching the editor to another row.
                self.guard(lambda:(self.keep_draft(),self.load_editor(rid)))
        self.update_count()

    def task_cell_click(self,event):
        if not self.key:return 'break'
        row=self.list.identify_row(event.y);column=self.list.identify_column(event.x)
        if not row:return
        rid=int(row)
        if rid not in self.visible_ids:return 'break'
        self._task_focus_id=rid
        if column=='#1':
            if rid in self.checked_ids:self.checked_ids.remove(rid)
            else:self.checked_ids.add(rid)
            values=list(self.list.item(row,'values'));values[0]='☑' if rid in self.checked_ids else '☐';self.list.item(row,values=values)
            self.update_count();return 'break'
        if column=='#2' and self.key:
            task=self.s.get_requirement(rid);new_done=not bool(task.done);self.s.set_done(rid,new_done)
            if self.rid==rid:
                self.loading=True;self.done_var.set(new_done);self.loading=False
            self.store.snapshot(self.key);self.refresh_tasks();self.status.set('任务完成状态已切换。');return 'break'

    def load_editor(self,rid):
        CompactStudioV4.load_editor(self,rid)
        draft=self.store.draft(self.draft_key());row=self.s.get_requirement(rid) if rid else None
        self.loading=True
        # 旧版草稿没有 done 字段时继承数据库原状态，不得把已完成任务改回未完成。
        self.done_var.set(bool(draft['done']) if draft and 'done' in draft else bool(row.done) if row else False)
        self.loading=False
        self.insert_mode_var.set('章节末尾');self.insert_mode_box.configure(state='readonly' if rid is None else 'disabled')
        self.editlabel.configure(text=('编辑任务 #'+str(rid) if rid else '新建任务')+(' · 草稿已恢复' if draft else ''))

    def save_current(self):
        # 主窗口快捷键固定保存当前任务；不再依赖隐藏辅助窗口的 Notebook 标签状态。
        if self.key and self.rid is not None:self.guard(self.save_task)
        elif self.key and self.dirty:self.guard(self.save_task)
        else:self.status.set('当前没有待保存的任务编辑内容。')
        return 'break'

    def trash_tasks(self):
        ids=self.selected_ids()
        if not ids:raise ValueError('请先选择当前文档中的任务。')
        self.keep_draft();self.store.trash(ids);self.checked_ids.clear();self.refresh_tasks();self.load_editor(None)
        self.store.snapshot(self.key);self.refresh_progress();self.status.set(f'已将 {len(ids)} 条任务移入回收站。')

    def restore_tasks(self):
        if self.filter.get()!='回收站':raise ValueError('请先把任务筛选切换为“回收站”，再选择要恢复的任务。')
        ids=self.selected_ids()
        if not ids:raise ValueError('请先选择回收站中的任务。')
        self.store.trash(ids,restore=True);self.checked_ids.clear();self.filter.set('全部');self.refresh_tasks();self.load_editor(None)
        self.store.snapshot(self.key);self.refresh_progress();self.status.set(f'已恢复 {len(ids)} 条任务。')


    def _purge_tasks_with_confirmation(self,ids):
        if self.filter.get()!='回收站':
            raise ValueError('请先将筛选切换到「回收站」。')
        if not ids:raise ValueError('回收站中没有选中的任务。')
        if not messagebox.askyesno('彻底删除任务',
            f'将从当前数据库中彻底删除 {len(ids)} 条任务，并清除当前文档的旧快照。\n会先创建完整备份；旧备份中仍可能存在这些内容。\n\n确定继续？',
            icon='warning',parent=self):return
        self.keep_draft()
        backup=self.store.purge_trashed_tasks(self.key,ids)
        self.checked_ids.clear()
        if self.rid in ids:self.rid=None
        self.load_editor(None);self.refresh_tasks();self.store.snapshot(self.key)
        self.refresh_progress()
        self.status.set(f'已彻底删除 {len(ids)} 条任务，备份：{backup}')

    def delete_trashed_selected(self):
        self._purge_tasks_with_confirmation(self.selected_ids())

    def empty_task_trash(self):
        self.require_doc()
        ids=[r.id for r in self.store.tasks(self.key,trash=True)]
        self._purge_tasks_with_confirmation(ids)

    def refresh_import_page(self):
        if not hasattr(self,'import_sources'):return
        self.import_sources.delete(*self.import_sources.get_children());self.import_source_map={}
        for project in self.s.project_groups():
            for doc in self.s.documents_for(project.name):
                for sid,name,path,body,time in self.store.sources(doc.key):
                    self.import_source_map[sid]=(doc.key,path,project.name,doc.name)
                    self.import_sources.insert('','end',iid=sid,values=(name,project.name+' / '+doc.name,time[:19]))

    def open_import_source(self):
        sel=self.import_sources.selection()
        if not sel:raise ValueError('请先选择一份已归档的原始资料。')
        item=self.import_source_map.get(sel[0])
        if not item:raise ValueError('资料记录已变化，请刷新后重试。')
        self.open_path(self.archived_source_path(item[1]))

    def refresh_export_page(self):
        if not hasattr(self,'export_doc_label'):return
        if not self.key:self.export_doc_label.configure(text='当前没有选择文档。请先到“文档”页面打开文档。')
        else:
            d=self.s.document(self.key);rows=self.store.tasks(self.key)
            self.export_doc_label.configure(text=f'当前导出目标：{d.project_name} / {d.name}    ·    {len(rows)} 条任务')

    def _progress_documents(self):
        docs=[]
        for p in self.s.project_groups():
            for d in self.s.documents_for(p.name):docs.append((p.name,d.name,d.key))
        return docs

    def refresh_progress(self,reset_group=False):
        if not hasattr(self,'progress_chapters'):return
        docs=self._progress_documents()
        projects=list(dict.fromkeys(x[0] for x in docs))
        options=['全部项目']+projects
        self.progress_scope_box.configure(values=options)
        scope=self.progress_scope_var.get()
        if scope not in options:
            scope='全部项目';self.progress_scope_var.set(scope)
        selected_docs=docs if scope=='全部项目' else [item for item in docs if item[0]==scope]
        entries=[]
        for project,name,key in selected_docs:
            for row in self.store.tasks(key):entries.append((project,name,key,row))
        total=len(entries);done=sum(bool(x[3].done) for x in entries);pending=total-done
        if total:
            self.progress_total.set(str(total));self.progress_done.set(str(done));self.progress_open.set(str(pending));self.progress_rate.set(f'{done/total:.0%}')
            self.progress_bar.configure(value=done/total*100)
            self.progress_hint.configure(text=f'当前项目：{scope} · 共 {len(selected_docs)} 份文档、{total} 条任务。')
        else:
            for var in (self.progress_total,self.progress_done,self.progress_open,self.progress_rate):var.set('0')
            self.progress_bar.configure(value=0);self.progress_hint.configure(text='当前项目没有文档或任务。可以先创建文档并添加任务。')
        groups={}
        if scope=='全部项目':
            for project in projects:groups.setdefault(('project',project),[])
            for project,name,key,row in entries:groups.setdefault(('project',project),[]).append((project,name,key,row))
        else:
            for project,name,key in selected_docs:groups.setdefault(('document',key),[])
            for project,name,key,row in entries:groups.setdefault(('document',key),[]).append((project,name,key,row))
        previous=None
        if not reset_group and self.progress_chapters.selection():previous=self.progress_chapters.selection()[0]
        self.progress_chapters.delete(*self.progress_chapters.get_children());self.progress_group_map={}
        for i,(group_key,items) in enumerate(groups.items()):
            kind,value=group_key
            if kind=='project':label=value
            else:
                meta=next(((project,name) for project,name,key in selected_docs if key==value),('',''))
                label=f'{meta[0]} / {meta[1]}'
            n=len(items);d=sum(bool(x[3].done) for x in items);iid='group-'+str(i)
            self.progress_group_map[iid]={'kind':kind,'value':value,'items':items}
            self.progress_chapters.insert('','end',iid=iid,values=(label,n,d,n-d,f'{d/n:.0%}' if n else '—'))
        children=self.progress_chapters.get_children()
        if children:
            chosen=previous if previous in children else children[0]
            self.progress_chapters.selection_set(chosen)
        else:
            self.progress_tasks.delete(*self.progress_tasks.get_children());self.progress_task_doc_map={}
        self.refresh_progress_tasks()

    def refresh_progress_tasks(self):
        if not hasattr(self,'progress_tasks'):return
        self.progress_tasks.delete(*self.progress_tasks.get_children());self.progress_task_doc_map={}
        selected=self.progress_chapters.selection()
        if not selected:return
        group=self.progress_group_map.get(selected[0])
        if not group:return
        for project,name,key,row in group['items']:
            self.progress_task_doc_map[row.id]=key
            self.progress_tasks.insert('','end',iid=str(row.id),values=('已完成' if row.done else '未完成',row.title,row.section or '未分类',name))

    def progress_task_click(self,event):
        row=self.progress_tasks.identify_row(event.y);column=self.progress_tasks.identify_column(event.x)
        if row and column=='#1':
            rid=int(row);key=self.progress_task_doc_map.get(rid)
            if not key:return 'break'
            task=self.s.get_requirement(rid);self.s.set_done(rid,not bool(task.done));self.store.snapshot(key)
            if self.rid==rid:
                self.loading=True;self.done_var.set(not bool(task.done));self.loading=False
            self.refresh_progress();self.refresh_tasks();return 'break'

    def mark_progress(self,done):
        selected=self.progress_tasks.selection()
        if not selected:raise ValueError('请先选择一条或多条任务。')
        affected=set()
        for item in selected:
            rid=int(item);key=self.progress_task_doc_map.get(rid)
            if not key:continue
            self.s.set_done(rid,done);affected.add(key)
        for key in affected:self.store.snapshot(key)
        if self.rid is not None and str(self.rid) in selected:
            self.loading=True;self.done_var.set(bool(done));self.loading=False
        self.refresh_progress();self.refresh_tasks();self.status.set(f'已更新 {len(selected)} 条任务的完成状态。')

    def edit_progress_task(self,event=None):
        selected=self.progress_tasks.selection()
        if len(selected)!=1:raise ValueError('请只选择一条任务。')
        rid=int(selected[0]);key=self.progress_task_doc_map.get(rid)
        if not key:raise ValueError('无法确定任务所属文档。')
        if key!=self.key:self.load_doc(key)
        self.load_editor(rid);self.show_page('tasks')

    def refresh_home_and_related(self):
        self.refresh_home();self.refresh_progress();self.refresh_documents_page();self.refresh_export_page()

    def migrate_from_folder(self):
        folder=filedialog.askdirectory(parent=self,title='选择旧版 reqdoc-data 文件夹（或包含它的旧版目录）')
        if not folder:return
        result=self.store.import_legacy_data(folder)
        self.checked_ids.clear();self.key=None;self.current_project_name=None;self.refresh_tree();self.show_page('projects')
        self.status.set(f'迁移完成：{result["projects"]} 个项目、{result["documents"]} 份文档。迁移前备份：{result["backup"]}')
        messagebox.showinfo('数据迁移完成',f'已迁移 {result["projects"]} 个项目、{result["documents"]} 份文档。\n\n迁移前备份：\n{result["backup"]}',parent=self)

    def migrate_from_backup(self):
        path=filedialog.askopenfilename(parent=self,title='选择 reqdoc 备份 ZIP',filetypes=[('reqdoc 备份','*.zip')])
        if not path:return
        result=self.store.import_legacy_data(path)
        self.checked_ids.clear();self.key=None;self.current_project_name=None;self.refresh_tree();self.show_page('projects')
        self.status.set(f'迁移完成：{result["projects"]} 个项目、{result["documents"]} 份文档。')
        messagebox.showinfo('备份迁移完成',f'已迁移 {result["projects"]} 个项目、{result["documents"]} 份文档。\n\n迁移前备份：\n{result["backup"]}',parent=self)

    def help(self):
        self.show_text(f'{VERSION} 使用说明',"""【项目与文档】按住项目或文档行拖动可调整同级顺序。选中项目后新建文档；新建后仍停留在项目与文档页，双击文档进入任务处理。

【任务处理】默认同时显示文档预览、任务列表和任务编辑器。左侧显示只读原始资料；没有原始资料时显示当前文档汇总。右侧可编辑整理后的任务。

【按需编辑】单击任务可加载编辑内容；新增任务、编辑任务、插入图片和粘贴图片均在当前工作区完成。正文支持 Markdown。保存后更新任务记录和文档汇总。

【导入与导出】导入前可以预览、编辑拆分任务并选择目标文档。文档操作保留 Word 与 Markdown 图片包导出；文档说明编辑和归档资料管理入口已移除。

【原始资料】归档原始文件只读保留；需要核对来源时在文档预览区查看或打开原件。任务编辑只修改软件整理后的工作结果。

【进度】统计项目选择器只保留“全部项目”和具体项目名称；选择单个项目后，下方按文档分组，选择分组查看任务。

【删除】删除项目会一并删除项目下的文档、任务及归档资料。原始资料归档不会因编辑任务而改变。

【数据安全】用户数据位于 %LOCALAPPDATA%\\ReqDocStudio\\data，与程序文件分离。更新程序不覆盖用户数据；不要删除最后一份有效数据目录。""")

    def close(self):
        if getattr(self,'_closing',False):return
        try:self.keep_draft();self.persist_notes()
        except Exception as exc:
            try:
                if not messagebox.askyesno(
                    '保存失败',
                    str(exc)+'\n\n仍然退出会丢失当前尚未保存的编辑内容。建议选择“否”，留在窗口中重试保存。',
                    default=messagebox.NO,
                    parent=self,
                ):return
            except tk.TclError:pass
        self._closing=True
        self._update_in_progress=False
        for handle in ('_autosave_after_id','_refresh_tree_after_id','_task_sash_after_id',
                       '_editor_sash_after_id','_source_sash_after_id','_update_poll_after_id','_drop_after_id',
                       '_working_preview_after_id','_working_task_edit_after_id'):
            task=getattr(self,handle,None)
            if task:
                try:self.after_cancel(task)
                except Exception:pass
        if getattr(self,'_file_drop',None):
            try:self._file_drop.close()
            except Exception:pass
        try:self.destroy()
        except tk.TclError:pass


# ---------------------------------------------------------------------------
# 测试版 5：单导航主界面、独立项目/文档页面、全局进度和数据迁移。
# 旧版 CompactStudioV4 仅作为功能基类，不再创建第二栏固定项目树。
# ---------------------------------------------------------------------------

def _store_import_legacy_data(self, source, from_backup=False):
    """把旧版 reqdoc-data 或备份 ZIP 安全迁移到当前空数据目录。"""
    source=Path(source).expanduser().resolve();temp_root=None
    try:
        if source.is_file():
            if source.suffix.lower()!='.zip':raise ValueError('请选择旧版 reqdoc-data 文件夹或 reqdoc 备份 ZIP。')
            temp_root=Path(tempfile.mkdtemp(prefix='reqdoc-migrate-'))
            extracted=self.restore_copy(source,temp_root);source_root=extracted/'reqdoc-data'
        else:
            source_root=source
            if (source_root/'reqdoc-data').is_dir():source_root=source_root/'reqdoc-data'
            if source_root.name.lower()=='home' and (source_root.parent/'docs').is_dir():source_root=source_root.parent
        src_db=source_root/'home'/'reqdoc.db';src_docs=source_root/'docs'
        if not src_db.is_file():raise ValueError('没有找到 home/reqdoc.db。请选择旧版 reqdoc-data 文件夹，而不是程序安装包目录。')
        if source_root.resolve()==self.settings.home.parent.resolve():raise ValueError('所选目录就是当前版本的数据目录，无需迁移。')
        if self.service.project_groups():raise ValueError('当前版本已经有项目。为避免覆盖，本次迁移已停止。')
        target_docs=Path(self.settings.docs_dir);target_docs.parent.mkdir(parents=True,exist_ok=True)
        existing=[x for x in target_docs.iterdir() if x.name!='.keep'] if target_docs.exists() else []
        if existing:raise ValueError('当前文档目录并非空目录。为避免覆盖，本次迁移已停止。')
        with sqlite_connection(f'file:{src_db.as_posix()}?mode=ro',uri=True) as check:
            integrity=check.execute('PRAGMA integrity_check').fetchone()[0]
            if integrity!='ok':raise ValueError('旧版数据库完整性检查失败：'+str(integrity))
            tables={row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required={'reqdoc_projects','reqdoc_documents','reqdoc_requirements'}
            if not required.issubset(tables):
                raise ValueError('该数据库不是 V8 数据格式。为避免误迁移或丢失旧数据，本次已停止；旧版数据库需要单独转换。')
        backup_path=self.backup()
        stage=Path(tempfile.mkdtemp(prefix='reqdoc-migration-stage-',dir=str(target_docs.parent)))
        try:
            stage_db=stage/'new.db';old_db=stage/'old.db';stage_docs=stage/'docs';stage_docs.mkdir()
            with sqlite_connection(f'file:{src_db.as_posix()}?mode=ro',uri=True) as source_conn,sqlite_connection(stage_db) as target_conn:source_conn.backup(target_conn)
            with sqlite_connection(stage_db) as check:
                if check.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('迁移副本数据库校验失败。')
            with sqlite_connection(self.settings.db_path) as current_conn,sqlite_connection(old_db) as backup_conn:current_conn.backup(backup_conn)
            if src_docs.exists():
                for item in src_docs.iterdir():
                    dest=stage_docs/item.name
                    if item.is_dir():shutil.copytree(item,dest)
                    elif item.is_file():shutil.copy2(item,dest)
            tmp_db=Path(self.settings.db_path).with_name('reqdoc-migration-'+uuid.uuid4().hex+'.db')
            shutil.copy2(stage_db,tmp_db)
            with sqlite_connection(tmp_db) as check:
                if check.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('最终数据库副本校验失败。')
            target_docs.mkdir(parents=True,exist_ok=True)
            old_docs=target_docs.with_name(target_docs.name+'.pre-migration-'+stamp())
            moved_old_docs=False;installed_new_docs=False;installed_new_db=False
            try:
                os.replace(target_docs,old_docs);moved_old_docs=True
                os.replace(stage_docs,target_docs);installed_new_docs=True
                for suffix in ('-wal','-shm'):Path(str(self.settings.db_path)+suffix).unlink(missing_ok=True)
                os.replace(tmp_db,self.settings.db_path);installed_new_db=True
                # 旧版数据库可能缺少后续版本新增的工作台表；补齐表结构但不重置已有数据。
                if hasattr(self.service,'initialize'):self.service.initialize()
                self.ensure_studio_schema()
                # 让当前服务实际读取迁移后的数据库；若初始化/读取失败，回滚数据库和文档目录。
                projects_list=self.service.project_groups()
                docs_count=sum(len(self.service.documents_for(project.name)) for project in projects_list)
            except Exception:
                if installed_new_db:
                    restore_db=Path(self.settings.db_path).with_name('reqdoc-rollback-'+uuid.uuid4().hex+'.db')
                    shutil.copy2(old_db,restore_db);os.replace(restore_db,self.settings.db_path)
                if installed_new_docs and target_docs.exists():shutil.rmtree(target_docs,ignore_errors=True)
                if moved_old_docs and old_docs.exists():os.replace(old_docs,target_docs)
                tmp_db.unlink(missing_ok=True)
                raise
            else:
                shutil.rmtree(old_docs,ignore_errors=True)
                (target_docs/'.keep').unlink(missing_ok=True)
                return {'projects':len(projects_list),'documents':docs_count,'backup':str(backup_path),'source':str(source_root)}
        finally:shutil.rmtree(stage,ignore_errors=True)
    finally:
        if temp_root:shutil.rmtree(temp_root,ignore_errors=True)


Store.import_legacy_data=_store_import_legacy_data



def single_instance(home):
    handle=open(Path(home)/'studio.lock','a+b')
    if handle.tell()==0:handle.write(b'0');handle.flush()
    handle.seek(0)
    try:
        if sys.platform=='win32':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close();raise RuntimeError('这份数据已经由另一个 Studio 窗口打开，请先关闭那个窗口。')
    return handle

def main():
    from reqdoc_working_document import install as install_working_document
    from reqdoc.bootstrap import prepare_runtime
    import faulthandler

    install_working_document(CompactStudio, Store)
    prepare_runtime()
    if sys.platform=='win32':
        import ctypes
        try:ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:pass
    while True:
        settings = None
        try:
            settings = Settings.from_env()
            settings.ensure_directories()
            logging.basicConfig(filename=str(settings.home/'studio.log'),encoding='utf-8',level=logging.INFO,force=True)
            with ExitStack() as stack:
                try:
                    fatal_log = stack.enter_context(open(settings.home/'studio-fatal.log','a',encoding='utf-8',buffering=1))
                    faulthandler.enable(file=fatal_log,all_threads=True)
                    stack.callback(faulthandler.disable)
                except OSError:
                    logging.exception('Fatal error logging unavailable')
                stack.enter_context(single_instance(settings.home))
                store = Store(settings)
                if settings.portable_target is not None:
                    from reqdoc_library import migrate_portable_library
                    settings = migrate_portable_library(store, settings.portable_target)
                    stack.enter_context(single_instance(settings.home))
                    logging.basicConfig(filename=str(settings.home/'studio.log'),encoding='utf-8',level=logging.INFO,force=True)
                    store = Store(settings)
                app = CompactStudio(store)
                app.mainloop()
            if not getattr(app,'_library_changed',False):
                return
        except Exception as e:
            logging.exception('Startup failed')
            root = tk.Tk()
            root.withdraw()
            log = '\n日志：'+str(settings.home/'studio.log') if settings else ''
            messagebox.showerror('启动失败',str(e)+log,parent=root)
            root.destroy()
            return

if __name__=='__main__':main()
