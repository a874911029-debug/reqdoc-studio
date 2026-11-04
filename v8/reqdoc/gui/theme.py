from tkinter import ttk


def configure_theme(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("Treeview", rowheight=27, font=("Microsoft YaHei UI", 9))
    style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
    style.configure("TButton", padding=(9, 5), font=("Microsoft YaHei UI", 9))
    style.configure("TLabel", font=("Microsoft YaHei UI", 9))
    return style
