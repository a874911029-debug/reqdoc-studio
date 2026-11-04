from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import reqdoc_updater as updater  # noqa: E402


# 7.5.3 only understands updater-bootstrap (three files).
# First upgrade to 7.5.4; a subsequent version may switch to module-patch.
PACKAGE_TYPE = "updater-bootstrap"


def build(output_dir: Path) -> tuple[Path, Path]:
    version = updater.APP_VERSION
    updater.version_tuple(version)
    if PACKAGE_TYPE not in updater.PACKAGE_FILES:
        raise ValueError("Unsupported ReqDoc update package type")
    output_dir.mkdir(parents=True, exist_ok=True)

    package_name = f"reqdoc-update-v{version}.zip"
    package_path = output_dir / package_name
    feed = (
        updater.LEGACY_FEED_URL if PACKAGE_TYPE == "updater-bootstrap"
        else updater.FEED_URL
    )
    feed_filename = (
        "latest.json" if PACKAGE_TYPE == "updater-bootstrap"
        else "module-latest.json"
    )
    metadata = {
        "name": "ReqDoc",
        "version": version,
        "channel": "stable",
        "feed": feed,
        "package_type": PACKAGE_TYPE,
    }

    sources = {
        "_internal/reqdoc_updater.py": ROOT / "src/reqdoc_updater.py",
        "updater.ps1": ROOT / "updater.ps1",
    }
    if PACKAGE_TYPE == "module-patch":
        for name in updater.MODULE_FILES:
            sources[name] = ROOT / "src" / Path(name).name
    expected_sources = updater.PACKAGE_FILES[PACKAGE_TYPE] - {"version.json"}
    if set(sources) != expected_sources:
        raise ValueError("Update source paths differ from fixed allowlist")
    for source in sources.values():
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(f"Required update source missing or linked: {source}")

    with zipfile.ZipFile(
        package_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in sorted(sources):
            archive.write(sources[path], path)
        archive.writestr(
            "version.json",
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        )

    updater.validate_zip(package_path, expected_version=version)
    digest = hashlib.sha256(package_path.read_bytes()).hexdigest()
    size = package_path.stat().st_size
    manifest = {
        "schema": 2,
        "version": version,
        "asset": {
            "url": updater.PACKAGE_BASE_URL + package_name,
            "sha256": digest,
            "size": size,
        },
        "package_type": PACKAGE_TYPE,
        "feed_path": feed_filename,
        "notes": (
            "V7.5.4 更新器兼容性引导：此包仅替换更新器，不变更主程序或用户资料。"
            if PACKAGE_TYPE == "updater-bootstrap" else
            "主程序 Python 模块增量包；仅更新允许的程序文件，不含用户资料。"
        ),
        "minimum_supported_version": "7.5.3",
        "source_commit": os.environ.get("GITHUB_SHA", ""),
    }
    manifest_path = output_dir / feed_filename
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return package_path, manifest_path


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "dist"))
    args = parser.parse_args()

    package, manifest = build(Path(args.output_dir))
    print(f"Built: {package}")
    print(f"Manifest: {manifest}")
    print(f"Package size: {package.stat().st_size} bytes")


if __name__ == "__main__":
    main()
