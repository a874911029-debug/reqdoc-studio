"""Editable Markdown/GFM pipe tables; preserve arbitrary surrounding document text.

The app treats imported text as a working copy. This module never writes the archived original.
"""
from __future__ import annotations
from dataclasses import dataclass
import re

_SEPARATOR=re.compile(r'^:?-{3,}:?$')


def _cells(line: str) -> list[str]:
    """Split a Markdown table row on unescaped |; leave escaped pipes in text."""
    raw=line.strip()
    if raw.startswith('|'):raw=raw[1:]
    if raw.endswith('|') and not raw.endswith(r'\|'):raw=raw[:-1]
    cells=[];current=[];escaped=False
    for ch in raw:
        if ch=='|' and not escaped:
            cells.append(''.join(current).strip().replace(r'\|','|'))
            current=[]
        else:
            current.append(ch)
        if ch=='\\' and not escaped:escaped=True
        else:escaped=False
    cells.append(''.join(current).strip().replace(r'\|','|'))
    return cells


def _is_separator(line: str, length: int) -> bool:
    cells=_cells(line)
    return len(cells)==length and all(_SEPARATOR.fullmatch(c.strip()) for c in cells)


@dataclass(frozen=True)
class PipeTable:
    start_line: int
    end_line: int     # exclusive
    headers: tuple[str,...]
    rows: tuple[tuple[str,...], ...]


def tables_in(text: str) -> list[PipeTable]:
    lines=text.splitlines(keepends=True);found=[];i=0
    fence=None
    while i+1<len(lines):
        content=lines[i].strip()
        f=re.match(r'^(`{3,}|~{3,})',content)
        if f:
            if fence is None:fence=f[1][0]
            elif f[1][0]==fence:fence=None
            i+=1;continue
        if fence or '|' not in content or '|' not in lines[i+1]:
            i+=1;continue
        headers=_cells(content)
        if len(headers)<2 or not _is_separator(lines[i+1].strip(),len(headers)):
            i+=1;continue
        j=i+2;rows=[]
        while j<len(lines) and '|' in lines[j]:
            cells=_cells(lines[j])
            if len(cells)!=len(headers):break
            rows.append(tuple(cells));j+=1
        found.append(PipeTable(i,j,tuple(headers),tuple(rows)))
        i=j
    return found


def table_to_markdown(headers:list[str],rows:list[list[str]],newline='\n')->str:
    if not headers or len(headers)<2:raise ValueError('表格至少需要两列。')
    count=len(headers)
    if any(len(row)!=count for row in rows):raise ValueError('表格列数不一致。')
    def cell(value):
        return str(value).replace('|',r'\|').replace('\r',' ').replace('\n',' ')
    result=['| '+' | '.join(cell(x) for x in headers)+' |',
            '| '+' | '.join('---' for _ in headers)+' |']
    result.extend('| '+' | '.join(cell(x) for x in row)+' |' for row in rows)
    return newline.join(result)


def replace_table(text:str,table:PipeTable,headers:list[str],rows:list[list[str]])->str:
    lines=text.splitlines(keepends=True)
    if table.start_line<0 or table.end_line>len(lines):raise ValueError('表格范围已过期。')
    prefix=''.join(lines[:table.start_line]);old=''.join(lines[table.start_line:table.end_line]);suffix=''.join(lines[table.end_line:])
    newline='\r\n' if '\r\n' in old else '\n'
    rendered=table_to_markdown(headers,rows,newline)
    if old.endswith(('\r\n','\n')):rendered+=newline
    return prefix+rendered+suffix

# Only a table with an explicit task-like title column is eligible to become
# tasks automatically. A reference table with categories/search phrases is not.
_TASK_TITLE_HEADERS={
    '任务','任务名称','任务标题','待办','待办事项','待办任务','工作项',
    '需求','需求标题','todo','task','task title','action item',
}


def task_title_column(headers) -> int | None:
    for index,header in enumerate(headers):
        key=re.sub(r'\s+',' ',str(header).strip().lower())
        if key in _TASK_TITLE_HEADERS:return index
    return None


def is_task_table(headers) -> bool:
    return task_title_column(headers) is not None


def table_to_task_items(table: PipeTable, source_path: str, source_name: str,
                        title_column: int | None = None) -> list[dict]:
    index=task_title_column(table.headers) if title_column is None else title_column
    if index is None or index<0 or index>=len(table.headers):
        raise ValueError('必须指定有效的任务标题列。')
    section_headers = {
        '章节', '所属章节', '分类', '任务分类',
        'section', 'task section', 'category', 'task category', 'group',
    }
    section_col=next((i for i,h in enumerate(table.headers)
                      if re.sub(r'\s+',' ',str(h).strip().lower()) in section_headers),None)
    rows=[]
    for row_index,values in enumerate(table.rows):
        title=values[index].strip()
        if not title:continue
        section=values[section_col].strip() if section_col is not None else '表格导入'
        body='\n'.join(f'{h}：{value}' for i,(h,value) in enumerate(zip(table.headers,values))
                       if i!=index and value.strip() and i!=section_col)
        rows.append({'source_path':source_path,'source_name':source_name,
                     'source_index':row_index,'title':title[:80],
                     'section':section or '表格导入','content':body,
                     'original_content':body,'_origin_kind':'table_row'})
    return rows
