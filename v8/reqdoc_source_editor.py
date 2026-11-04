"""Source working-copy editor with an optional visual pipe-table editor (Tk)."""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from reqdoc_source_table import tables_in, replace_table


class SourceEditDialog(tk.Toplevel):
    def __init__(self, parent, title, content, on_save):
        super().__init__(parent)
        self.title('修改已导入资料 · '+title)
        self.geometry('1060x750')
        self.minsize(700,490)
        self.transient(parent)
        self.on_save=on_save
        self.last_tab=None;self.table_info=None
        self.columns=[];self.rows=[];self._cell_entry=None;self._switching=False
        root=ttk.Frame(self,padding=12);root.pack(fill='both',expand=True)
        ttk.Label(root,text='编辑已导入资料',font=('Microsoft YaHei',15,'bold')).pack(anchor='w')
        ttk.Label(root,text='编辑的是软件内的工作副本，原始文件保持不变。这里修改资料不会自动重写已有任务。',foreground='#64748B').pack(anchor='w',pady=(4,9))
        self.tabs=ttk.Notebook(root);self.tabs.pack(fill='both',expand=True)
        self.table_frame=ttk.Frame(self.tabs,padding=7);self.text_frame=ttk.Frame(self.tabs,padding=7)
        self.tabs.add(self.table_frame,text='表格编辑');self.tabs.add(self.text_frame,text='全文编辑')
        self._build_table()
        text_host=ttk.Frame(self.text_frame);text_host.pack(fill='both',expand=True)
        self.text=tk.Text(text_host,wrap='none',undo=True,font=('Microsoft YaHei',10),padx=9,pady=9)
        x=ttk.Scrollbar(text_host,orient='horizontal',command=self.text.xview)
        y=ttk.Scrollbar(text_host,orient='vertical',command=self.text.yview)
        self.text.configure(xscrollcommand=x.set,yscrollcommand=y.set)
        y.pack(side='right',fill='y');x.pack(side='bottom',fill='x');self.text.pack(fill='both',expand=True)
        self.text.insert('1.0',content)
        actions=ttk.Frame(root);actions.pack(fill='x',pady=(10,0))
        ttk.Button(actions,text='保存修改稿',command=self.save).pack(side='right',padx=(6,0))
        ttk.Button(actions,text='取消',command=self.destroy).pack(side='right')
        self.info=ttk.Label(actions,text='',foreground='#64748B');self.info.pack(side='left')
        self.tabs.bind('<<NotebookTabChanged>>',self._tab_changed)
        self._load_table_from_text()
        if not self.table_info:self.tabs.select(self.text_frame)
        self.last_tab=self.tabs.select()
        self.protocol('WM_DELETE_WINDOW',self.destroy)
        self.grab_set()

    def _build_table(self):
        toolbar=ttk.Frame(self.table_frame);toolbar.pack(fill='x',pady=(0,6))
        for label,action in [('添加行',self.add_row),('删除选中行',self.remove_rows),('添加列',self.add_column),('重命名列',self.rename_column),('删除列',self.remove_column)]:
            ttk.Button(toolbar,text=label,command=action).pack(side='left',padx=(0,6))
        copybar=ttk.Frame(self.table_frame);copybar.pack(fill='x',pady=(0,5))
        ttk.Button(copybar,text='复制选中行',command=self.copy_selected_rows).pack(side='left',padx=(0,5))
        ttk.Button(copybar,text='复制整张表',command=self.copy_all_table).pack(side='left',padx=(0,5))
        ttk.Button(copybar,text='复制 Markdown',command=self.copy_markdown).pack(side='left')
        ttk.Label(self.table_frame,text='双击单元格修改内容。全文的其他段落会保留；表格之外的内容请切到“全文编辑”。',foreground='#64748B').pack(anchor='w',pady=(0,7))
        area=ttk.Frame(self.table_frame);area.pack(fill='both',expand=True)
        self.grid=ttk.Treeview(area,show='headings',selectmode='extended')
        vx=ttk.Scrollbar(area,orient='vertical',command=self.grid.yview)
        hx=ttk.Scrollbar(area,orient='horizontal',command=self.grid.xview)
        self.grid.configure(yscrollcommand=vx.set,xscrollcommand=hx.set)
        vx.pack(side='right',fill='y');hx.pack(side='bottom',fill='x');self.grid.pack(fill='both',expand=True)
        self.grid.bind('<Double-1>',self._edit_cell)
        self.grid.bind('<Control-c>',lambda e:self.copy_selected_rows())
        self.grid.bind('<Control-C>',lambda e:self.copy_selected_rows())

    def _tab_changed(self,event=None):
        if self._switching:return
        selected=self.tabs.select()
        if selected==self.last_tab:return
        if self.last_tab==str(self.table_frame):
            self._apply_table_to_text()
        if selected==str(self.table_frame):
            self._load_table_from_text()
        self.last_tab=selected

    def _load_table_from_text(self):
        text=self.text.get('1.0','end-1c')
        blocks=tables_in(text)
        self.table_info=blocks[0] if blocks else None
        if self.table_info:
            self.columns=list(self.table_info.headers)
            self.rows=[list(r) for r in self.table_info.rows]
            self._refresh_grid()
            self.info.configure(text=f'已识别 {len(blocks)} 张表格，当前编辑第 1 张 · {len(self.rows)} 行')
        else:
            self.columns=[];self.rows=[];self._refresh_grid()
            self.info.configure(text='未识别到 Markdown 表格，请在全文编辑中使用 | 表头 | 表头 | 的语法。')

    def _refresh_grid(self):
        self.grid.delete(*self.grid.get_children())
        cols=['c'+str(i) for i in range(len(self.columns))]
        self.grid.configure(columns=cols,displaycolumns=cols)
        for i,(ident,name) in enumerate(zip(cols,self.columns)):
            self.grid.heading(ident,text=name or f'第{i+1}列')
            self.grid.column(ident,width=min(270,max(130,len(name)*15)),minwidth=90,stretch=True)
        for i,row in enumerate(self.rows):self.grid.insert('','end',iid=str(i),values=row)

    def _copy(self,content):
        self._flush_cell()
        self.clipboard_clear();self.clipboard_append(content);self.update_idletasks()
        self.info.configure(text='已复制；可粘贴到 Excel、文档或其他程序。')
        return 'break'

    def copy_selected_rows(self):
        from reqdoc_table_clipboard import rows_to_tsv
        self._flush_cell()
        chosen={int(x) for x in self.grid.selection()}
        if not chosen:
            messagebox.showinfo('复制表格','请先选中一行或多行；复制全部请点击「复制整张表」。',parent=self)
            return 'break'
        return self._copy(rows_to_tsv([], [self.rows[i] for i in range(len(self.rows)) if i in chosen]))

    def copy_all_table(self):
        from reqdoc_table_clipboard import rows_to_tsv
        self._flush_cell()
        return self._copy(rows_to_tsv(self.columns,self.rows))

    def copy_markdown(self):
        from reqdoc_source_table import table_to_markdown
        self._flush_cell()
        return self._copy(table_to_markdown(self.columns,self.rows))

    def _flush_cell(self):
        entry=self._cell_entry
        if not entry:return
        try:
            i,j=entry._rowcol
            if 0<=i<len(self.rows) and 0<=j<len(self.columns):
                self.rows[i][j]=entry.get();self.grid.item(str(i),values=self.rows[i])
        finally:
            self._cell_entry=None
            try:entry.destroy()
            except tk.TclError:pass

    def _edit_cell(self,event):
        if not self.table_info:return
        row=self.grid.identify_row(event.y)
        column=self.grid.identify_column(event.x)
        if not row or not column:return
        index=int(column.replace('#',''))-1
        if index<0 or index>=len(self.columns):return
        self._flush_cell()
        rect=self.grid.bbox(row,column)
        if not rect:return
        x,y,w,h=rect
        entry=ttk.Entry(self.grid)
        entry._rowcol=(int(row),index)
        entry.insert(0,self.rows[int(row)][index]);entry.place(x=x,y=y,width=max(75,w),height=h)
        self._cell_entry=entry
        entry.focus_set();entry.select_range(0,'end')
        entry.bind('<Return>',lambda e:self._flush_cell())
        entry.bind('<FocusOut>',lambda e:self._flush_cell())
        entry.bind('<Escape>',lambda e:self._cancel_cell())

    def _cancel_cell(self):
        entry=self._cell_entry;self._cell_entry=None
        if entry:entry.destroy()

    def add_row(self):
        if not self.columns:
            messagebox.showinfo('添加表格行','请先在全文中建立一张 Markdown 表格。',parent=self);return
        self._flush_cell();self.rows.append(['']*len(self.columns));self._refresh_grid()
        self.grid.selection_set(str(len(self.rows)-1));self.grid.see(str(len(self.rows)-1))

    def remove_rows(self):
        self._flush_cell();chosen={int(x) for x in self.grid.selection()}
        self.rows=[row for i,row in enumerate(self.rows) if i not in chosen];self._refresh_grid()

    def add_column(self):
        if not self.columns:return
        name=simpledialog.askstring('添加一列','请输入新列名',parent=self)
        if not name or not name.strip():return
        self._flush_cell();self.columns.append(name.strip())
        for row in self.rows:row.append('')
        self._refresh_grid()

    def rename_column(self):
        if not self.columns:return
        number=simpledialog.askinteger('修改列名',f'修改第几列？（1 ～ {len(self.columns)}）',parent=self,minvalue=1,maxvalue=len(self.columns))
        if number is None:return
        name=simpledialog.askstring('修改列名','请输入新列名',initialvalue=self.columns[number-1],parent=self)
        if not name or not name.strip():return
        self._flush_cell();self.columns[number-1]=name.strip();self._refresh_grid()

    def remove_column(self):
        if len(self.columns)<=2:
            messagebox.showwarning('不能删除列','表格至少保留两列。',parent=self);return
        number=simpledialog.askinteger('删除一列',f'删除第几列？（1 ～ {len(self.columns)}）',parent=self,minvalue=1,maxvalue=len(self.columns))
        if number is None:return
        self._flush_cell();self.columns.pop(number-1)
        for row in self.rows:row.pop(number-1)
        self._refresh_grid()

    def _apply_table_to_text(self):
        if not self.table_info:return
        self._flush_cell()
        content=self.text.get('1.0','end-1c')
        edited=replace_table(content,self.table_info,self.columns,self.rows)
        if edited!=content:
            self.text.delete('1.0','end');self.text.insert('1.0',edited)
        self.table_info=tables_in(edited)[0] if tables_in(edited) else None

    def save(self):
        if self.tabs.select()==str(self.table_frame):self._apply_table_to_text()
        content=self.text.get('1.0','end-1c')
        try:self.on_save(content)
        except Exception as exc:
            messagebox.showerror('保存失败',str(exc),parent=self);return
        self.destroy()
