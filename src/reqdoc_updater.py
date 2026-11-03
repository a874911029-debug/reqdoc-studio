"""ReqDoc GitHub update feed and secure ZIP staging (no Tk dependencies).

Updates are opt-in.  The source and public releases now live in the same repository.
The current portable data directory is never included in the update file plan.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

APP_VERSION = "7.5.0"
PUBLIC_OWNER = "a874911029-debug"
UPDATE_REPO = "reqdoc-studio"
FEED_URL = f"https://api.github.com/repos/{PUBLIC_OWNER}/{UPDATE_REPO}/releases/latest"
RELEASE_PREFIX = f"https://github.com/{PUBLIC_OWNER}/{UPDATE_REPO}/releases/download/"
MAX_ZIP_BYTES = 300 * 1024 * 1024
MAX_FILES = 10000
MAX_UNCOMPRESSED_BYTES = 800 * 1024 * 1024
MAX_ENTRY_BYTES = 256 * 1024 * 1024

class UpdateError(RuntimeError):
    pass


def version_tuple(value: str) -> tuple[int, int, int]:    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise UpdateError("更新版本号格式无效。")
    parts = tuple(int(s) for s in value.split("."))
    if any(n > 100000 for n in parts):
        raise UpdateError("更新版本号不在允许范围内。")
    return parts


def validate_manifest(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise UpdateError("更新清单格式不受支持。")
    version = payload.get("version")
    version_tuple(version)
    asset = payload.get("asset")
    if not isinstance(asset, dict):
        raise UpdateError("更新清单缺少安装包信息。")
    url = asset.get("url")
    expected_prefix = RELEASE_PREFIX + f"v{version}/"
    if not isinstance(url, str) or not url.startswith(expected_prefix):
        raise UpdateError("安装包地址不属于指定的 GitHub 发布仓库。")
    if "?" in url or "#" in url or "\\" in url or ".." in url:
        raise UpdateError("安装包地址包含无效字符。")
    filename = url[len(expected_prefix):]
    if not re.fullmatch(r"reqdoc-studio-win64-v\d+\.\d+\.\d+\.zip", filename):
        raise UpdateError("安装包名称与版本不匹配。")
    if filename != f"reqdoc-studio-win64-v{version}.zip":
        raise UpdateError("安装包版本名称不一致。")
    sha = asset.get("sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", sha):
        raise UpdateError("安装包 SHA-256 校验值缺失或格式错误。")
    size = asset.get("size")
    if type(size) is not int or not (1024 <= size <= MAX_ZIP_BYTES):
        raise UpdateError("更新包体积异常。")
    notes = payload.get("notes", "")
    if not isinstance(notes, str):notes = ""    minimum = payload.get("minimum_supported_version", "7.1.0")
    version_tuple(minimum)
    return {
        "version": version,
        "asset": {"url": url, "sha256": sha.lower(), "size": size},
        "notes": notes[:1000],
        "minimum_supported_version": minimum,
    }


def manifest_from_release(release: dict) -> dict:
    """Convert GitHub Releases metadata into the old verified-manifest model.

    No additional latest.json is needed. GitHub's published SHA-256 asset digest
    is mandatory; the updater refuses releases without a digest.
    """
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise UpdateError("GitHub 尚未发布可安装的稳定版本。")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError("GitHub Release 版本标签无效。")
    version = tag[1:]
    version_tuple(version)
    expected_file = f"reqdoc-studio-win64-v{version}.zip"
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("GitHub Release 没有安装包列表。")
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == expected_file]
    if len(matches) != 1:
        raise UpdateError("GitHub Release 尚未上传符合版本号的 Windows 安装包。")
    asset = matches[0]
    digest = asset.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise UpdateError("GitHub 尚未提供安装包 SHA-256 摘要；拒绝安装未经校验的文件。")
    manifest = {        "schema": 1,
        "version": version,
        "asset": {
            "url": asset.get("browser_download_url"),
            "sha256": digest.removeprefix("sha256:"),
            "size": asset.get("size"),
        },
        "notes": release.get("body") or "",
        "minimum_supported_version": "7.1.0",
    }
    return validate_manifest(manifest)


def fetch_latest(timeout: float = 10) -> dict:
    req = Request(FEED_URL, headers={
        "User-Agent": "ReqDoc-Updater/7",
        "Accept": "application/vnd.github+json",
        "Cache-Control": "no-cache",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read(262145)
            if len(data) > 262144:
                raise UpdateError("GitHub Release 信息异常过大。")
    except HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("GitHub 仓库还没有发布 Release（404）；首次发布后才可以应用内更新。") from exc
        raise UpdateError(f"GitHub 更新检查失败（HTTP {exc.code}）。") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise UpdateError("无法连接 GitHub 更新服务，请检查网络连接。") from exc
    try:
        release = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise UpdateError("GitHub 返回的 Release 信息无法解析。") from exc    return manifest_from_release(release)


def update_available(manifest: dict, current_version: str = APP_VERSION) -> bool:
    return version_tuple(manifest["version"]) > version_tuple(current_version)


def validate_zip(zip_path: Path) -> None:
    """Reject zip-slip, duplicate paths, symlinks, data files and unexpected top-level content."""
    with zipfile.ZipFile(zip_path) as z:
        infos=z.infolist()
        if len(infos) > MAX_FILES:raise UpdateError("更新包包含过多文件。")
        seen=set()
        present=set()
        uncompressed=0
        total_uncompressed=0
        for info in infos:
            total_uncompressed+=info.file_size
            if info.file_size>MAX_ENTRY_BYTES or total_uncompressed>MAX_UNCOMPRESSED_BYTES:
                raise UpdateError("更新包解压体积超过安全限制。")
            if info.compress_size and info.file_size>max(1000*info.compress_size, 100*1024*1024):
                raise UpdateError("更新包压缩比异常。")
            name=info.filename
            if not name or "\\" in name or name.startswith("/") or "\x00" in name:
                raise UpdateError("更新包包含非法文件路径。")
            parts=PurePosixPath(name).parts
            if not parts or any(part in ("..",".") for part in parts) or ":" in name:
                raise UpdateError("更新包包含路径穿越风险。")
            normalized="/".join(parts)
            folded=normalized.casefold()
            if folded in seen:raise UpdateError("更新包中存在重复或大小写冲突的路径。")
            seen.add(folded)
            uncompressed+=info.file_size
            if uncompressed>800*1024*1024:raise UpdateError("更新包解压后的体积超出安全限制。")
            mode=info.external_attr >> 16            if stat.S_IFMT(mode) == stat.S_IFLNK:
                raise UpdateError("更新包不允许符号链接。")
            if info.is_dir():continue
            top=parts[0]
            if top=="reqdoc-data":
                if normalized not in ("reqdoc-data/docs/.keep","reqdoc-data/home/.keep"):
                    raise UpdateError("更新包不得携带用户数据。")
                continue
            if top=="_internal":
                if len(parts)<2:raise UpdateError("运行库目录不完整。")
            elif normalized not in ("reqdoc-gui.exe","updater.ps1","version.json","README.txt"):
                raise UpdateError(f"更新包出现意外文件：{normalized}")
            present.add(normalized)
        for required in ("reqdoc-gui.exe","updater.ps1","version.json"):
            if required not in present:raise UpdateError(f"更新包缺少必要文件：{required}")
        for module in ("reqdoc_studio", "reqdoc_updater"):
            if not any(f"_internal/{module}.{ext}" in present for ext in ("pyc", "py")):
                raise UpdateError(f"更新包缺少必要模块：{module}")
        if z.testzip() is not None:raise UpdateError("更新包 CRC 校验失败。")


def download_update(manifest: dict, progress: Callable[[int, int], None] | None = None) -> Path:
    validated=validate_manifest({"schema":1,**manifest})
    asset=validated["asset"]
    work=Path(tempfile.mkdtemp(prefix="reqdoc-update-"))
    package=work / f"reqdoc-studio-win64-v{validated['version']}.zip"
    sha=hashlib.sha256();total=0
    req=Request(asset["url"],headers={"User-Agent":"ReqDoc-Updater/7"})
    try:
        with urlopen(req,timeout=30) as resp,package.open("wb") as out:
            while True:
                chunk=resp.read(1024*1024)
                if not chunk:break
                total+=len(chunk)
                if total>MAX_ZIP_BYTES:raise UpdateError("下载体积超出安全限制。")                out.write(chunk);sha.update(chunk)
                if progress:progress(total,asset["size"])
        if total!=asset["size"]:raise UpdateError("下载大小与发布清单不一致。")
        if sha.hexdigest()!=asset["sha256"]:raise UpdateError("SHA-256 校验失败，拒绝安装更新。")
        validate_zip(package)
        return package
    except Exception:
        shutil.rmtree(work,ignore_errors=True)
        raise