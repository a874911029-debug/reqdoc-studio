from __future__ import annotations

import tkinter as tk
import unittest

from reqdoc.gui.theme import configure_theme


class WorkspaceThemeTests(unittest.TestCase):
    def test_light_workspace_and_primary_action_styles_are_registered(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)

        style = configure_theme(root)

        self.assertEqual(style.lookup("TFrame", "background"), "#FFFFFF")
        self.assertEqual(style.lookup("Treeview", "fieldbackground"), "#FFFFFF")
        self.assertEqual(style.lookup("Status.TLabel", "background"), "#F2F5FA")
        self.assertEqual(style.lookup("Accent.TButton", "background"), "#2F6FB3")
        self.assertEqual(int(style.lookup("Treeview", "rowheight")), 30)


if __name__ == "__main__":
    unittest.main()
