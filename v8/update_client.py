"""Verified in-app updater for ReqDoc Studio V8 and newer only."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from urllib.request import Request, urlopen

from version import VERSION

FEED = "https://raw.githubusercontent.com/a874911029-debug/reqdoc-studio/updates/v8-latest.json"
BASE = "https://raw.githubusercontent.com/a874911029-debug/reqdoc-studio/updates/"
ALLOWED = {"ReqDocStudio.exe", "updater.ps1", "version.json"}
MAX_ARCHIVE = 140 * 1024 * 1024
MAX_UNCOMPRESSED = 300 * 1024 * 1024


class UpdateError(ValueError):
    pass


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,5}\.\d{1,5}\.\d{1,5}", value):
        raise UpdateError("非法版本号。")
    return tuple(map(int, value.split(".")))


def validate_feed(data):
    if not isinstance(data, dict) or data.get("schema") != 8:
        raise UpdateError("不是受支持的 V8 更新清单。")
    version = data.get("version")
    if version_key(version)[0] != 8:
        raise UpdateError("更新通道仅支持 V8 系列。")
    asset = data.get("asset")
    if not isinstance(asset, dict):
        raise UpdateError("缺少更新文件信息。")
    url = BASE + f"reqdoc-v{version}-win64.zip"
    if asset.get("url") != url:
        raise UpdateError("更新下载地址不属于指定的 GitHub 通道。")
    if not isinstance(asset.get("sha256"), str) or not re.fullmatch(r"[a-fA-F0-9]{64}", asset["sha256"]):
        raise UpdateError("更新包 SHA-256 无效。")
    if type(asset.get("size")) is not int or not (1024 <= asset["size"] <= MAX_ARCHIVE):
        raise UpdateError("更新包体积无效。")
    minimum = data.get("minimum_supported_version", "8.0.0")
    if version_key(minimum)[0] != 8 or version_key(minimum) > version_key(version):
        raise UpdateError("最低支持版本与更新版本不匹配。")
    return {"version": version, "minimum_supported_version": minimum, "asset": asset, "notes": str(data.get("notes", ""))[:400]}


def fetch_manifest():
    req = Request(FEED + "?refresh=1", headers={
        "User-Agent": "ReqDocStudio-V8", "Cache-Control": "no-cache",
    })
    with urlopen(req, timeout=20) as resp:
        payload = resp.read(16385)
        if len(payload) > 16384:
            raise UpdateError("更新信息过大。")
    return validate_feed(json.loads(payload.decode("utf-8")))


def validate_package(filename: Path, expected_version: str):
    with zipfile.ZipFile(filename) as z:
        entries = z.infolist()
        if {item.filename for item in entries} != ALLOWED or len(entries) != len(ALLOWED):
            raise UpdateError("更新包存在缺失文件或未授权路径。")
        total = 0
        for item in entries:
            if item.is_dir() or "\\" in item.filename or ":" in item.filename:
                raise UpdateError("更新包路径不允许。")
            mode = item.external_attr >> 16
            if stat.S_IFMT(mode) == stat.S_IFLNK:
                raise UpdateError("更新包不允许链接。")
            if item.file_size > MAX_UNCOMPRESSED:
                raise UpdateError("更新包单文件过大。")
            total += item.file_size
        if total > MAX_UNCOMPRESSED or z.testzip():
            raise UpdateError("更新包校验失败。")
        metadata = json.loads(z.read("version.json").decode("utf-8"))
        if not isinstance(metadata, dict) or metadata.get("name") != "ReqDocStudio" or metadata.get("version") != expected_version:
            raise UpdateError("更新包内部版本或产品名称错误。")
    return True


def download_package(manifest: dict, progress=None) -> Path:
    manifest = validate_feed({"schema": 8, **manifest})
    dest = Path(tempfile.mkdtemp(prefix="reqdoc-v8-update-")) / f"reqdoc-v{manifest['version']}-win64.zip"
    sha = hashlib.sha256()
    total = 0
    req = Request(manifest["asset"]["url"], headers={"User-Agent": "ReqDocStudio-V8"})
    try:
        with urlopen(req, timeout=60) as inp, dest.open("wb") as out:
            while chunk := inp.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE:
                    raise UpdateError("下载文件超出大小上限。")
                sha.update(chunk)
                out.write(chunk)
                if progress:
                    progress(total, manifest["asset"]["size"])
        if total != manifest["asset"]["size"] or sha.hexdigest().lower() != manifest["asset"]["sha256"].lower():
            raise UpdateError("下载文件大小或 SHA-256 校验失败。")
        validate_package(dest, manifest["version"])
        return dest
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def install_and_exit(package: Path, app_root: Path):
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise UpdateError("只有 Windows 正式安装版可以执行自动更新。")
    install_dir = Path(sys.executable).resolve().parent
    installer = install_dir / "updater.ps1"
    if not installer.is_file():
        raise UpdateError("当前安装目录缺少独立更新器，请使用完整 V8 安装包。")
    powershell = Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not powershell.is_file():
        raise UpdateError("未找到 Windows PowerShell。")
    sha = hashlib.sha256(Path(package).read_bytes()).hexdigest()
    # Copy updater before replacing installed updater.ps1 itself.
    staged = Path(package).parent / "run-updater.ps1"
    staged.write_bytes(installer.read_bytes())
    args = [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(staged), "-InstallDir", str(install_dir), "-Package", str(package),
            "-AppPid", str(os.getpid()), "-Sha256", sha]
    subprocess.Popen(args, cwd=str(install_dir), creationflags=0x08000000)
    return True
