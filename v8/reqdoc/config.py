from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    home: Path
    docs_dir: Path

    @property
    def db_path(self) -> Path:
        return self.home / "reqdoc.db"

    @classmethod
    def from_env(cls):
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share"))
        data_root = Path(os.environ.get("REQDOC_V8_DATA") or (root / "ReqDocStudio" / "data"))
        return cls(home=data_root / "home", docs_dir=data_root / "docs")

    def ensure_directories(self):
        self.home.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        (self.home / "logs").mkdir(parents=True, exist_ok=True)
