"""ReqDoc incremental updater channel (no Tk dependencies)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

APP_VERSION = "7.5.4"
PUBLIC_OWNER = "a874911029-debug"
UPDATE_REPO = "reqdoc-studio"
UPDATE_BRANCH = "updates"
# Old 7.5.3 clients consume this forever-pinned bootstrap feed.
LEGACY_FEED_URL = (
    f"https://raw.githubusercontent.com/{PUBLIC_OWNER}/{UPDATE_REPO}/"
    f"{UPDATE_BRANCH}/latest.json"
)
# Only 7.5.4+ clients read this second-stage feature/module feed.
FEED_URL = (
    f"https://raw.githubusercontent.com/{PUBLIC_OWNER}/{UPDATE_REPO}/"
    f"{UPDATE_BRANCH}/module-latest.json"
)
PACKAGE_BASE_URL = (
    f"https://raw.githubusercontent.com/{PUBLIC_OWNER}/{UPDATE_REPO}/"
    f"{UPDATE_BRANCH}/"
)

MAX_ZIP_BYTES = 4 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
MAX_ENTRY_BYTES = 4 * 1024 * 1024

UPDATER_FILES = frozenset({
    "_internal/reqdoc_updater.py",
    "updater.ps1",
    "version.json",
})
MODULE_FILES = frozenset({
    "_internal/reqdoc_studio.py",
    "_internal/reqdoc_source_table.py",
    "_internal/reqdoc_source_editor.py",
    "_internal/reqdoc_win_drop.py",
    "_internal/reqdoc_table_clipboard.py",
})
PACKAGE_FILES = {
    "updater-bootstrap": UPDATER_FILES,
    "module-patch": UPDATER_FILES | MODULE_FILES,
}
ALLOWED_FILES = PACKAGE_FILES["module-patch"]
MAX_FILES = len(ALLOWED_FILES)


class UpdateError(RuntimeError):
    pass


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise UpdateError("更新版本号格式无效。")
    parts = tuple(int(s) for s in value.split("."))
    if any(n > 100000 for n in parts):
        raise UpdateError("更新版本号不在允许范围内。")
    return parts


def validate_manifest(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("schema") != 2:
        raise UpdateError("增量更新清单格式不受支持。")
    version = payload.get("version")
    version_tuple(version)
    asset = payload.get("asset")
    if not isinstance(asset, dict):
        raise UpdateError("更新清单缺少安装包信息。")
    expected_url = PACKAGE_BASE_URL + f"reqdoc-update-v{version}.zip"
    url = asset.get("url")
    if not isinstance(url, str) or url != expected_url:
        raise UpdateError("安装包地址不属于指定的 ReqDoc 更新通道。")
    sha = asset.get("sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", sha):
        raise UpdateError("安装包 SHA-256 校验值缺失或格式错误。")
    size = asset.get("size")
    if type(size) is not int or not (1024 <= size <= MAX_ZIP_BYTES):
        raise UpdateError("增量更新包体积异常。")
    notes = payload.get("notes", "")
    if not isinstance(notes, str):
        notes = ""
    minimum = payload.get("minimum_supported_version", "7.5.2")
    version_tuple(minimum)
    commit = payload.get("source_commit", "")
    if commit and (
        not isinstance(commit, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", commit)
    ):
        raise UpdateError("更新清单中的源码提交标识无效。")
    return {
        "schema": 2,
        "version": version,
        "asset": {"url": url, "sha256": sha.lower(), "size": size},
        "notes": notes[:1000],
        "minimum_supported_version": minimum,
        "source_commit": commit,
    }


def fetch_latest(timeout: float = 15) -> dict:
    url = FEED_URL + "?_=" + str(int(time.time()))
    req = Request(
        url,
        headers={
            "User-Agent": "ReqDoc-Updater/8",
            "Accept": "application/json",
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read(262145)
            if len(data) > 262144:
                raise UpdateError("GitHub 更新清单异常过大。")
    except HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("自动更新通道尚未初始化，暂时没有可用构建。") from exc
        if exc.code in (403, 429):
            raise UpdateError("GitHub 更新服务暂时限制了请求，请稍后重试。") from exc
        raise UpdateError(f"GitHub 更新检查失败（HTTP {exc.code}）。") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise UpdateError("无法连接 GitHub 更新服务，请检查网络连接。") from exc
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise UpdateError("GitHub 返回的更新清单无法解析。") from exc
    return validate_manifest(payload)


def update_available(manifest: dict, current_version: str = APP_VERSION) -> bool:
    return version_tuple(manifest["version"]) > version_tuple(current_version)


def validate_zip(zip_path: Path, expected_version: str | None = None) -> None:
    """Allow exact updater/bootstrap or module payloads, never executables or user data."""
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError("更新包不是有效的 ZIP 文件。") from exc
    with archive as z:
        infos = z.infolist()
        if len(infos) not in {len(files) for files in PACKAGE_FILES.values()}:
            raise UpdateError("增量更新包的文件数量与支持的包类型不匹配。")
        seen: set[str] = set()
        present: set[str] = set()
        total_uncompressed = 0
        for info in infos:
            name = info.filename
            if not name or "\\" in name or name.startswith("/") or "\x00" in name:
                raise UpdateError("更新包包含非法文件路径。")
            if info.is_dir():
                raise UpdateError("增量更新包不允许包含目录条目。")
            parts = PurePosixPath(name).parts
            normalized = "/".join(parts)
            if (
                not parts
                or normalized != name
                or any(part in ("..", ".") for part in parts)
                or ":" in name
            ):
                raise UpdateError("更新包包含路径穿越风险。")
            folded = normalized.casefold()
            if folded in seen:
                raise UpdateError("更新包中存在重复或大小写冲突的路径。")
            seen.add(folded)
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) == stat.S_IFLNK:
                raise UpdateError("更新包不允许符号链接。")
            if normalized not in ALLOWED_FILES:
                raise UpdateError(f"增量更新包出现意外文件：{normalized}")
            if info.file_size > MAX_ENTRY_BYTES:
                raise UpdateError("更新包单个文件体积超过安全限制。")
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise UpdateError("更新包解压体积超过安全限制。")
            if info.compress_size and info.file_size > max(
                1000 * info.compress_size, 100 * 1024 * 1024
            ):
                raise UpdateError("更新包压缩比异常。")
            present.add(normalized)
        if "version.json" not in present:
            raise UpdateError("增量更新包缺少 version.json。")
        try:
            metadata = json.loads(z.read("version.json").decode("utf-8"))
        except (KeyError, UnicodeError, ValueError) as exc:
            raise UpdateError("更新包版本信息无法读取。") from exc
        if not isinstance(metadata, dict):
            raise UpdateError("更新包版本信息必须是 JSON 对象。")
        package_type = metadata.get("package_type")
        required = PACKAGE_FILES.get(package_type) if isinstance(package_type, str) else None
        if required is None:
            raise UpdateError("不支持的更新包类型。")
        if present != required:
            missing = required - present
            unexpected = present - required
            raise UpdateError(
                "更新包文件清单与包类型不匹配；缺少："
                + "、".join(sorted(missing))
                + "；额外："
                + "、".join(sorted(unexpected))
            )
        package_version = metadata.get("version")
        version_tuple(package_version)
        if expected_version and package_version != expected_version:
            raise UpdateError("更新包内部版本与更新清单不一致。")
        expected_feed = (
            LEGACY_FEED_URL if package_type == "updater-bootstrap" else FEED_URL
        )
        if metadata.get("name") != "ReqDoc" or metadata.get("feed") != expected_feed:
            raise UpdateError("更新包的产品标识或更新通道不匹配。")
        # Package files are checked against the exact, immutable type allowlist above.
        if z.testzip() is not None:
            raise UpdateError("更新包 CRC 校验失败。")


def download_update(
    manifest: dict,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    validated = validate_manifest(manifest)
    asset = validated["asset"]
    work = Path(tempfile.mkdtemp(prefix="reqdoc-update-"))
    package = work / f"reqdoc-update-v{validated['version']}.zip"
    sha = hashlib.sha256()
    total = 0
    req = Request(
        asset["url"],
        headers={"User-Agent": "ReqDoc-Updater/8", "Cache-Control": "no-cache"},
    )
    try:
        with urlopen(req, timeout=30) as resp, package.open("wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ZIP_BYTES:
                    raise UpdateError("下载体积超出安全限制。")
                out.write(chunk)
                sha.update(chunk)
                if progress:
                    progress(total, asset["size"])
        if total != asset["size"]:
            raise UpdateError("下载大小与更新清单不一致。")
        if sha.hexdigest() != asset["sha256"]:
            raise UpdateError("SHA-256 校验失败，拒绝安装更新。")
        validate_zip(package, expected_version=validated["version"])
        return package
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
