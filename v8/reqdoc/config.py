from __future__ import annotations
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    home: Path
    docs_dir: Path
    library_config: Path | None = None
    portable: bool = False
    portable_target: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.home / "reqdoc.db"

    @property
    def library_root(self) -> Path:
        return self.home.parent

    @classmethod
    def from_env(cls):
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share"))
        override = os.environ.get("REQDOC_V8_DATA")
        if override:
            data_root = Path(override)
            return cls(home=data_root / "home", docs_dir=data_root / "docs")
        portable_root = None
        if getattr(sys, "frozen", False):
            portable_root = Path(sys.executable).resolve().parent / "总文档库"
            if (portable_root / "home" / "reqdoc.db").is_file():
                if not (portable_root / "docs").is_dir():
                    raise ValueError(f"资料库缺少文档目录：{portable_root}")
                return cls(home=portable_root / "home", docs_dir=portable_root / "docs", portable=True)
        config = root / "ReqDocStudio" / "library.json"
        try:
            entry = json.loads(config.read_text("utf-8"))
            data_root = Path(entry["path"])
            if not data_root.is_absolute():
                raise ValueError("文件库路径必须是绝对路径。")
        except FileNotFoundError:
            data_root = config.parent / "data"
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"文件库路径配置无效：{config}") from exc
        else:
            if not (data_root / "home" / "reqdoc.db").is_file() or not (data_root / "docs").is_dir():
                raise ValueError(f"无法访问文件库：{data_root}。请检查该目录或重新连接存储设备。")
        if portable_root is not None:
            if (data_root / "home" / "reqdoc.db").is_file():
                return cls(home=data_root / "home", docs_dir=data_root / "docs", library_config=config,
                           portable_target=portable_root)
            return cls(home=portable_root / "home", docs_dir=portable_root / "docs", portable=True)
        return cls(home=data_root / "home", docs_dir=data_root / "docs", library_config=config)

    def ensure_directories(self):
        self.home.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        (self.home / "logs").mkdir(parents=True, exist_ok=True)
