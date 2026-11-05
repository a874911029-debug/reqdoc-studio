"""Windows Explorer file drops for Tk using Win32 WM_DROPFILES.

No extra Python dependency. Best-effort only; File > Import remains available if
Windows prevents drag/drop. No elevation is requested or supported.
"""
from __future__ import annotations
from collections import deque
from pathlib import Path
import sys

_ALLOWED={'.docx','.md','.markdown','.txt'}


def select_drop_files(paths):
    """Validate imported paths from dialog/drop; preserve Unicode Windows paths."""
    paths = list(paths)
    if len(paths) > 20:
        raise ValueError("一次最多拖入 20 个文件，请分批导入。")
    kept = []
    rejected = []
    seen = set()
    for raw in paths:
        try:
            path = Path(str(raw))
            name = path.name or str(raw)
            if path.suffix.lower() not in _ALLOWED:
                rejected.append(name)
                continue
            # Strict resolution rejects vanished, inaccessible and malformed paths
            # before the import UI opens. pathlib uses the Unicode Win32 APIs.
            resolved_path = path.resolve(strict=True)
            if not resolved_path.is_file():
                rejected.append(name)
                continue
            resolved = str(resolved_path)
            key = resolved.casefold()
            if key not in seen:
                seen.add(key)
                kept.append(resolved)
        except (OSError, RuntimeError, TypeError, ValueError):
            try:
                rejected.append(Path(str(raw)).name or str(raw))
            except (OSError, RuntimeError, TypeError, ValueError):
                rejected.append(str(raw))
    return kept, rejected


class WindowsFileDrop:
    """Collect Explorer drops in Win32 and deliver them from the Tk event loop."""
    WM_DROPFILES=0x0233
    WM_NCDESTROY=0x0082
    SUBCLASS_ID=1

    def __init__(self,root,callback):
        self.root=root
        self.callback=callback
        self._handles=set()
        self._pending=deque()
        self._timer=None
        self._closed=False
        if sys.platform!='win32':raise OSError('拖放支持仅适用于 Windows。')
        import ctypes
        from ctypes import wintypes
        self.ctypes=ctypes
        self.user32=ctypes.WinDLL('user32',use_last_error=True)
        self.shell32=ctypes.WinDLL('shell32',use_last_error=True)
        controls=ctypes.WinDLL('comctl32',use_last_error=True)
        message_args=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
        proc_type=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,*message_args,ctypes.c_size_t,ctypes.c_size_t)
        self._proc=proc_type(self._window_message)
        self._set_subclass=controls.SetWindowSubclass
        self._set_subclass.argtypes=[wintypes.HWND,proc_type,ctypes.c_size_t,ctypes.c_size_t]
        self._set_subclass.restype=wintypes.BOOL
        self._remove_subclass=controls.RemoveWindowSubclass
        self._remove_subclass.argtypes=[wintypes.HWND,proc_type,ctypes.c_size_t]
        self._remove_subclass.restype=wintypes.BOOL
        self._forward=controls.DefSubclassProc
        self._forward.argtypes=message_args
        self._forward.restype=ctypes.c_ssize_t
        self.user32.GetParent.argtypes=[wintypes.HWND]
        self.user32.GetParent.restype=wintypes.HWND
        self.shell32.DragAcceptFiles.argtypes=[wintypes.HWND,wintypes.BOOL]
        self.shell32.DragAcceptFiles.restype=None
        self.shell32.DragQueryFileW.argtypes=[wintypes.HANDLE,wintypes.UINT,wintypes.LPWSTR,wintypes.UINT]
        self.shell32.DragQueryFileW.restype=wintypes.UINT
        self.shell32.DragFinish.argtypes=[wintypes.HANDLE]
        self.shell32.DragFinish.restype=None

    def install_on(self,widget,include_parent=False):
        if self._closed:
            return 0
        widget.update_idletasks()
        hwnd=int(widget.winfo_id())
        hwnds=[hwnd]
        if include_parent:
            parent=self.user32.GetParent(hwnd)
            if parent:hwnds.append(int(parent))
        count=0
        for handle in dict.fromkeys(hwnds):
            if handle not in self._handles and self._set_subclass(handle,self._proc,self.SUBCLASS_ID,0):
                self._handles.add(handle)
                self.shell32.DragAcceptFiles(handle,True)
                count+=1
        if self._handles and self._timer is None:
            self._timer=self.root.after(80,self._poll)
        return count

    def _window_message(self,hwnd,msg,wparam,lparam,_subclass_id,_data):
        if msg==self.WM_NCDESTROY:
            self._remove_subclass(hwnd,self._proc,self.SUBCLASS_ID)
            self._handles.discard(hwnd)
        if msg!=self.WM_DROPFILES:
            return self._forward(hwnd,msg,wparam,lparam)

        paths=[]
        errors=[]
        try:
            total=self.shell32.DragQueryFileW(wparam,0xFFFFFFFF,None,0)
            if total>20:
                errors.append('一次最多拖入 20 个文件。')
            else:
                for index in range(total):
                    size=self.shell32.DragQueryFileW(wparam,index,None,0)
                    buffer=self.ctypes.create_unicode_buffer(size+1)
                    if not self.shell32.DragQueryFileW(wparam,index,buffer,size+1):
                        raise OSError('Windows 未返回完整路径。')
                    paths.append(buffer.value)
        except Exception as exc:
            errors.append('无法读取拖入文件：'+str(exc))
        finally:
            self.shell32.DragFinish(wparam)
        if not self._closed:
            self._pending.append((tuple(paths),tuple(errors)))
        return 0

    def _poll(self):
        self._timer=None
        try:
            if self._pending:
                self.callback(*self._pending.popleft())
        finally:
            if not self._closed and self._timer is None:
                self._timer=self.root.after(80,self._poll)

    def close(self):
        self._closed=True
        for handle in tuple(self._handles):
            self.shell32.DragAcceptFiles(handle,False)
            if self._remove_subclass(handle,self._proc,self.SUBCLASS_ID):
                self._handles.discard(handle)
        if self._timer is not None:
            self.root.after_cancel(self._timer)
            self._timer=None
        self._pending.clear()
