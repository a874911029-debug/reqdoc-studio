"""Assemble the V8 Windows EXE, fixed-file archive and version manifest."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v8"))
from version import VERSION  # noqa: E402
from update_client import BASE, FEED, validate_package  # noqa: E402

def main():
    dist = ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    executable = dist / "ReqDocStudio.exe"
    if not executable.is_file():
        raise FileNotFoundError("Build ReqDocStudio.exe before assembling release")
    if executable.stat().st_size > 125 * 1024 * 1024:
        raise ValueError("Executable exceeds bounded Github update package size")
    installer = ROOT / "v8" / "updater.ps1"
    version_json = json.dumps({"name": "ReqDocStudio", "version": VERSION, "feed": FEED},
                              ensure_ascii=False, indent=2) + "\n"
    (dist / "version.json").write_text(version_json, "utf-8")
    (dist / "updater.ps1").write_bytes(installer.read_bytes())
    output = dist / f"reqdoc-v{VERSION}-win64.zip"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name in ("ReqDocStudio.exe", "updater.ps1", "version.json"):
            z.write(dist / name, name)
    validate_package(output, VERSION)
    if output.stat().st_size > 140 * 1024 * 1024:
        raise ValueError("Update package exceeds 140MB cap")
    asset = {"url": BASE + output.name, "size": output.stat().st_size,
             "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    manifest = {"schema": 8, "version": VERSION, "minimum_supported_version": "8.0.0", "asset": asset,
                "notes": "文档工作台 V8.0.5：新增独立可编辑工作文档；导入文件可形成表格/文档或任务；图片插入与粘贴写入工作文档；任务处理采用 60/40 文档与任务布局；进度分组改为左侧竖向导航，任务明细支持文本选择复制。",
                "source_commit": os.environ.get("GITHUB_SHA", "")}
    (dist / "v8-latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"V8 package: {output.name} size={asset['size']} sha256={asset['sha256']}")
if __name__ == "__main__":
    main()
