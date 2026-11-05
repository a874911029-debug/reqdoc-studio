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
                "notes": "完善一键整理：无标题散记分块、正文相似归类、可保存的关键词规则、重复合并、数值与否定差异核对。提供归类说明和待归类内容，后台运行可取消，结果单独保存。",
                "source_commit": os.environ.get("GITHUB_SHA", "")}
    (dist / "v8-latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"V8 package: {output.name} size={asset['size']} sha256={asset['sha256']}")
if __name__ == "__main__":
    main()
