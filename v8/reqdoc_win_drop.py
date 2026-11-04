"""Windows Explorer file drops for Tk using Win32 WM_DROPFILES.

No extra Python dependency. Best-effort only; File > Import remains available if
Windows prevents drag/drop. No elevation is requested or supported.
"""
from __future__ import annotations
from pathlib import Path
import sys

_ALLOWED={'.docx','.md','.markdown','.txt'}


def select_drop_files(paths):
    """Validate imported paths from dialog/drop; never accept dirs or scripts."""
    paths=list(paths)
    if len(paths)>20:raise ValueError('一次最多拖入 20 个文件，请分批导入。')
    kept=[];rejected=[];seen=set()
    for raw in paths:
        path=Path(str(raw))
        if path.suffix.lower() not in _ALLOWED or not path.is_file():
            rejected.append(path.name or str(path))
            continue
        resolved=str(path.resolve())
        key=resolved.casefold()
        if key not in seen:
            seen.add(key);kept.append(resolved)
    return kept,rejected


class WindowsFileDrop:
    """Register individual Tk window handles and restore subclassing on close."""
    WM_DROPFILES=0x0233

    def __init__(self,root,callback):
        self.root=root
        self.callback=callback
        self._hooks=[]
        if sys.platform!='win32':raise OSError('拖放支持仅适用于 Windows。')
        import ctypes
        from ctypes import wintypes
        self.ctypes=ctypes
        self.wintypes=wintypes
        self.user32=ctypes.WinDLL('user32',use_last_error=True)
        self.shell32=ctypes.WinDLL('shell32',use_last_error=True)
        is64=ctypes.sizeof(ctypes.c_void_p)==8
        self.set_window_long=getattr(self.user32,'SetWindowLongPtrW' if is64 else 'SetWindowLongW')
        self.set_window_long.argtypes=[wintypes.HWND,ctypes.c_int,ctypes.c_ssize_t]
        self.set_window_long.restype=ctypes.c_ssize_t
        self.user32.GetParent.argtypes=[wintypes.HWND]
        self.user32.GetParent.restype=wintypes.HWND
        self.user32.CallWindowProcW.argtypes=[ctypes.c_void_p,wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
        self.user32.CallWindowProcW.restype=ctypes.c_ssize_t
        self.shell32.DragAcceptFiles.argtypes=[wintypes.HWND,wintypes.BOOL]
        self.shell32.DragQueryFileW.argtypes=[wintypes.HANDLE,wintypes.UINT,wintypes.LPWSTR,wintypes.UINT]
        self.shell32.DragQueryFileW.restype=wintypes.UINT
        self.shell32.DragFinish.argtypes=[wintypes.HANDLE]
        self.proc_type=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)

    def install_on(self,widget,include_parent=False):
        widget.update_idletasks()
        hwnd=int(widget.winfo_id())
        hwnds=[hwnd]
        if include_parent:
            parent=self.user32.GetParent(hwnd)
            if parent:hwnds.append(int(parent))
        count=0
        for handle in dict.fromkeys(hwnds):
            if any(x['hwnd']==handle for x in self._hooks):continue
            if self._install_one(handle):count+=1
        return count

    def _install_one(self,handle):
        ctypes=self.ctypes
        previous=[0]
        def callback(hwnd,msg,wparam,lparam):
            if msg==self.WM_DROPFILES:
                paths=[];errors=[]
                try:
                    total=int(self.shell32.DragQueryFileW(wparam,0xFFFFFFFF,None,0))
                    if total>20:errors.append('一次最多拖入 20 个文件。')
                    for i in range(min(total,20)):
                        size=int(self.shell32.DragQueryFileW(wparam,i,None,0))
                        buffer=ctypes.create_unicode_buffer(size+1)
                        self.shell32.DragQueryFileW(wparam,i,buffer,size+1)
                        paths.append(buffer.value)
                except Exception as exc:
                    errors.append('无法读取拖入文件：'+str(exc))
                finally:
                    self.shell32.DragFinish(wparam)
                try:
                    self.root.after_idle(lambda paths=tuple(paths),errors=tuple(errors):self.callback(paths,errors))
                except Exception:pass
                return 0
            return self.user32.CallWindowProcW(previous[0],hwnd,msg,wparam,lparam)
        proc=self.proc_type(callback)
        ctypes.set_last_error(0)
        old=self.set_window_long(handle,-4,ctypes.cast(proc,ctypes.c_void_p).value)
        if not old:
            # If OS rejects this handle, leave standard file picker functional.
            return False
        previous[0]=old
        self.shell32.DragAcceptFiles(handle,True)
        self._hooks.append({'hwnd':handle,'proc':proc,'old':old})
        return True

    def close(self):
        for hook in reversed(self._hooks):
            try:
                self.shell32.DragAcceptFiles(hook['hwnd'],False)
                self.set_window_long(hook['hwnd'],-4,hook['old'])
            except Exception:pass
        self._hooks.clear()
