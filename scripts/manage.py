#!/usr/bin/env python3
"""Manage the project's background search service. Never opens a browser."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
NAMES = ("media-pansou",)


def run(*args, capture=False):
    return subprocess.run(["/usr/local/bin/container", *args], check=True, text=True, capture_output=capture)


def inventory():
    items = {x["id"]: x for x in json.loads(run("list", "--all", "--format", "json", capture=True).stdout)}
    for name in NAMES:
        if name in items and items[name]["configuration"].get("labels", {}).get("project") != "cloud-storage-search":
            raise RuntimeError("拒绝管理同名但不属于本项目的容器：" + name)
    return items


def initialize():
    for name in ("", "secrets", "plans", "reports", "pansou-cache"):
        path = RUNTIME / name
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)


def up():
    initialize()
    current = inventory()
    if "media-pansou" not in current:
        image = json.loads((ROOT / "config/images.lock.json").read_text())["pansou"]["image"]
        run("run", "-d", "--name", "media-pansou", "--cpus", "2", "--memory", "512M",
            "-l", "project=cloud-storage-search", "-p", "127.0.0.1:8888:8888",
            "-v", f"{RUNTIME / 'pansou-cache'}:/app/cache",
            "-e", "ENABLED_PLUGINS=quark4k,quarksoo,quarktv,pansearch,zhizhen",
            "-e", "CHANNELS=Quark_Movies,QuarkFree,quark_res", "-e", "CONCURRENCY=8",
            "-e", "ASYNC_MAX_BACKGROUND_WORKERS=8", "-e", "ASYNC_MAX_BACKGROUND_TASKS=32",
            "-e", "PLUGIN_TIMEOUT=15", image)
    elif current["media-pansou"]["status"]["state"] != "running":
        run("start", "media-pansou")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urlopen("http://127.0.0.1:8888/api/health", timeout=3) as response:
                if response.status == 200:
                    print("后台搜索服务就绪；对话任务入口为 scripts/media.py，没有网页操作。")
                    return
        except OSError:
            time.sleep(1)
    raise RuntimeError("后台搜索服务未通过健康检查")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "up", "status", "stop"))
    action = parser.parse_args().action
    if action == "init":
        initialize()
        print("后台任务数据目录已就绪。")
    elif action == "up":
        up()
    elif action == "status":
        items = inventory()
        print(json.dumps({name: items.get(name, {}).get("status", {}).get("state", "not_created") for name in NAMES}))
    else:
        items = inventory()
        for name in reversed(NAMES):
            if items.get(name, {}).get("status", {}).get("state") == "running":
                run("stop", name)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
