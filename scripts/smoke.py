#!/usr/bin/env python3
"""Read-only background smoke checks; never starts a browser or writes to a cloud."""
import argparse
import json
from pathlib import Path
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-quark", action="store_true", help="read the bound library root using the configured credential reference")
    parser.add_argument("--search", help="optional title to search in the local background service")
    args = parser.parse_args()
    config = json.loads((ROOT / "config/headless.json").read_text())
    assert config["mode"] == "headless" and config["open_browser"] is False
    with urlopen(config["pansou_url"] + "/api/health", timeout=5) as response:
        assert response.status == 200
    result = {"mode": "headless", "search_ready": True, "browser_opened": False, "cloud_writes": 0}
    if args.check_quark:
        from media_helper.headless_cli import _storage
        entries = _storage(config).list_files(config["library_root_id"])
        result["library_entries"] = len(entries)
        result["credential_source"] = config.get("credential_source", {}).get("type", "file_or_environment")
    if args.search:
        from media_helper.search import PanSouSearch
        result["search"] = {"title": args.search, "candidates": len(PanSouSearch(config["pansou_url"]).search(args.search))}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
