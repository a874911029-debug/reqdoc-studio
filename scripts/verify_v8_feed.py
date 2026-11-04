"""Verify that the public V8 feed serves the exact release and its downloadable ZIP."""
from __future__ import annotations
import sys
import time

from update_client import download_package, fetch_manifest
from version import VERSION


def main():
    last_error = None
    for attempt in range(1, 7):
        try:
            manifest = fetch_manifest()
            if manifest["version"] != VERSION:
                raise RuntimeError(
                    f"Live feed version {manifest['version']} does not match build {VERSION}"
                )
            package = download_package(manifest)
            print(
                f"LIVE FEED PASS: version={manifest['version']} "
                f"package={package.name} size={manifest['asset']['size']} "
                f"sha256={manifest['asset']['sha256']}"
            )
            return 0
        except Exception as exc:
            last_error = exc
            print(f"Live feed verification attempt {attempt}/6 failed: {exc}", flush=True)
            if attempt < 6:
                time.sleep(5)
    raise RuntimeError(f"Live V8 feed verification failed: {last_error}")


if __name__ == "__main__":
    sys.exit(main())
