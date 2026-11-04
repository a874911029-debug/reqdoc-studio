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


def build(output_dir: Path) -> tuple[Path, Path]:
    version = updater.APP_VERSION
    updater.version_tuple(version)
    output_dir.mkdir(parents=True, exist_ok=True)

    package_name = f"reqdoc-update-v{version}.zip"
    package_path = output_dir / package_name
    metadata = {
        "name": "ReqDoc",
        "version": version,
        "channel": "stable",
        "feed": updater.FEED_URL,
        "package_type": "updater-bootstrap",
    }

    with zipfile.ZipFile(
        package_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        archive.write(ROOT / "src/reqdoc_updater.py", "_internal/reqdoc_updater.py")
        archive.write(ROOT / "updater.ps1", "updater.ps1")
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
        "notes": (
            "首次迁移更新器：从 GitHub Releases 改为自动更新分支。"
            "本次只替换更新器，不覆盖主程序、Python 运行库和用户数据。"
        ),
        "minimum_supported_version": "7.5.2",
        "source_commit": os.environ.get("GITHUB_SHA", ""),
    }

    manifest_path = output_dir / "latest.json"
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
