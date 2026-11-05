from tkinter import ttk


def configure_theme(root):
    """Apply a restrained light workspace theme with a navy navigation shell."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    font = ("Microsoft YaHei UI", 9)
    heading_font = ("Microsoft YaHei UI", 9, "bold")
    ink = "#26384D"
    muted = "#64758B"
    white = "#FFFFFF"
    border = "#DFE6EF"
    canvas = "#F2F5FA"
    accent = "#2F6FB3"
    accent_hover = "#EAF2FC"

    # The app shell stays light grey; the content workspace and editors are white.
    style.configure("TFrame", background=white)
    style.configure("Sidebar.TFrame", background="#F8F9FC")
    style.configure(
        "Sidebar.Treeview", background="#F8F9FC", fieldbackground="#F8F9FC",
        foreground=ink, rowheight=32, borderwidth=0,
        font=("Microsoft YaHei UI", 9),
    )
    style.map(
        "Sidebar.Treeview", background=[("selected", "#EDEAFB")],
        foreground=[("selected", "#5541AD")],
    )

    style.configure("TLabel", background=white, foreground=ink, font=font)
    style.configure("TLabelframe", background=white, bordercolor=border, relief="solid")
    style.configure("TLabelframe.Label", background=white, foreground="#1F3B59", font=heading_font)
    style.configure(
        "TButton", padding=(10, 6), font=font,
        background="#F8FAFC", foreground="#29445F", bordercolor=border,
        focusthickness=1, focuscolor="#A8C6E8",
    )
    style.map(
        "TButton",
        background=[("disabled", "#F1F3F6"), ("pressed", "#DCE9F7"), ("active", accent_hover)],
        foreground=[("disabled", "#9AA6B4"), ("active", "#1F4E7A")],
    )
    style.configure(
        "Accent.TButton", padding=(11, 6), font=heading_font,
        background=accent, foreground=white, bordercolor=accent,
    )
    style.map(
        "Accent.TButton",
        background=[("disabled", "#B8C7D7"), ("pressed", "#245B96"), ("active", "#3C7FC4")],
        foreground=[("disabled", "#F5F7FA"), ("active", white)],
    )
    style.configure(
        "Treeview", rowheight=30, font=("Microsoft YaHei UI", 9),
        background=white, fieldbackground=white, foreground=ink,
        bordercolor=border, lightcolor=border, darkcolor=border,
    )
    style.configure(
        "Treeview.Heading", font=heading_font, background="#F1F5F9",
        foreground="#354B63", padding=(8, 7), relief="flat",
    )
    style.map(
        "Treeview",
        background=[("selected", "#DCEBFF")],
        foreground=[("selected", "#183B63")],
    )
    style.configure("TEntry", fieldbackground=white, foreground=ink, padding=(7, 5))
    style.configure("TCombobox", fieldbackground=white, foreground=ink, padding=(7, 5))
    style.map("TCombobox", fieldbackground=[("readonly", white)], foreground=[("readonly", ink)])
    style.configure("TCheckbutton", background=white, foreground=ink, font=font)
    style.map("TCheckbutton", background=[("active", white)])
    style.configure("TNotebook", background=white, borderwidth=0)
    style.configure(
        "TNotebook.Tab", padding=(12, 7), background="#EEF2F7",
        foreground=muted, font=font,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", white), ("active", accent_hover)],
        foreground=[("selected", "#1F5C94"), ("active", "#1F4E7A")],
    )
    style.configure("TProgressbar", troughcolor="#E5ECF4", background=accent)
    style.configure("TSeparator", background=border)
    style.configure("Status.TLabel", background=canvas, foreground=muted, padding=(12, 6), font=("Microsoft YaHei UI", 8))
    return style
