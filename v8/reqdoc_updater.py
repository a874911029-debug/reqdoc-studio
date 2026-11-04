"""Compatibility surface for the original UI's update controls, V8-only."""
from __future__ import annotations
import json
import zipfile
from pathlib import Path

from version import VERSION
from update_client import (
    UpdateError,
    version_key,
    fetch_manifest,
    download_package,
    validate_package,
)

APP_VERSION = VERSION


def version_tuple(value):
    return version_key(value)


def fetch_latest():
    return fetch_manifest()


def update_available(manifest):
    return version_key(manifest["version"]) > version_key(APP_VERSION)


def download_update(manifest, progress=None):
    return download_package(manifest, progress=progress)


def validate_zip(package):
    try:
        with zipfile.ZipFile(package) as archive:
            metadata = json.loads(archive.read("version.json").decode("utf-8"))
        version = metadata["version"]
    except Exception as exc:
        raise UpdateError("更新包版本信息读取失败。") from exc
    return validate_package(Path(package), version)
