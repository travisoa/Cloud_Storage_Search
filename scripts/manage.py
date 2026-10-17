#!/usr/bin/env python3
"""Manage the project's background search service. Never opens a browser."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
NAMES = ("media-pansou",)
PROJECT_LABEL = "project=cloud-storage-search"
RUNTIME_ENV = "MEDIA_HELPER_CONTAINER_RUNTIME"
KINDS = ("container", "docker", "podman")
APPLE_CONTAINER = "/usr/local/bin/container"


def detect_runtime(env=None, which=shutil.which, platform=None):
    """Return (kind, executable): explicit env override, else Apple container, docker, podman."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    explicit = env.get(RUNTIME_ENV, "").strip()
    if explicit:
        kind = Path(explicit).name
        if kind not in KINDS:
            raise RuntimeError(f"{RUNTIME_ENV} 只支持 {'/'.join(KINDS)}（命令名或可执行文件路径）：{explicit}")
        path = which(explicit)
        if not path:
            raise RuntimeError(f"{RUNTIME_ENV} 指定的容器运行时不可执行：{explicit}")
        return kind, path
    for kind in KINDS:
        if kind == "container":
            # The name `container` is only Apple's runtime on macOS.
            path = (which("container") or which(APPLE_CONTAINER)) if platform == "darwin" else None
        else:
            path = which(kind)
        if path:
            return kind, path
    raise RuntimeError(f"未找到容器运行时（Apple container、docker、podman）；可用 {RUNTIME_ENV} 指定。")


class Runtime:
    def __init__(self, kind, path, runner=subprocess.run):
        self.kind, self.path, self.runner = kind, path, runner

    def run(self, *args, capture=False):
        return self.runner([self.path, *args], check=True, text=True, capture_output=capture)

    def _records(self):
        """Yield (name, labels, state) for this project's container names, in a runtime-neutral shape."""
        if self.kind == "container":
            for item in json.loads(self.run("list", "--all", "--format", "json", capture=True).stdout or "[]"):
                if item.get("id") in NAMES:
                    status = item.get("status")
                    state = status.get("state") if isinstance(status, dict) else status
                    yield item["id"], (item.get("configuration") or {}).get("labels") or {}, state
            return
        for name in NAMES:
            listed = self.run("ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}", capture=True).stdout
            if name not in listed.split():
                continue
            details = json.loads(self.run("container", "inspect", name, capture=True).stdout)[0]
            yield name, (details.get("Config") or {}).get("Labels") or {}, (details.get("State") or {}).get("Status")

    def inventory(self):
        key, value = PROJECT_LABEL.split("=", 1)
        items = {}
        for name, labels, state in self._records():
            if labels.get(key) != value:
                raise RuntimeError("拒绝管理同名但不属于本项目的容器：" + name)
            items[name] = {"state": state or "unknown", "labels": labels}
        return items

    def pansou_run_args(self, image, cache):
        # SELinux hosts (common with Podman) need the bind mount relabelled for the container.
        mount = f"{cache}:/app/cache" + (":Z" if self.kind == "podman" else "")
        return ["run", "-d", "--name", "media-pansou", "--cpus", "2", "--memory", "512M",
                "-l", PROJECT_LABEL, "-p", "127.0.0.1:8888:8888",
                "-v", mount,
                "-e", "ENABLED_PLUGINS=quark4k,quarksoo,quarktv,pansearch,zhizhen",
                "-e", "CHANNELS=Quark_Movies,QuarkFree,quark_res", "-e", "CONCURRENCY=8",
                "-e", "ASYNC_MAX_BACKGROUND_WORKERS=8", "-e", "ASYNC_MAX_BACKGROUND_TASKS=32",
                "-e", "PLUGIN_TIMEOUT=15", image]


def initialize():
    for name in ("", "secrets", "plans", "reports", "pansou-cache"):
        path = RUNTIME / name
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)


def up(runtime):
    initialize()
    current = runtime.inventory()
    if "media-pansou" not in current:
        image = json.loads((ROOT / "config/images.lock.json").read_text())["pansou"]["image"]
        runtime.run(*runtime.pansou_run_args(image, RUNTIME / "pansou-cache"))
    elif current["media-pansou"]["state"] != "running":
        runtime.run("start", "media-pansou")
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
        return
    runtime = Runtime(*detect_runtime())
    if action == "up":
        up(runtime)
    elif action == "status":
        items = runtime.inventory()
        print(json.dumps({name: items.get(name, {}).get("state", "not_created") for name in NAMES}))
    else:
        items = runtime.inventory()
        for name in reversed(NAMES):
            if items.get(name, {}).get("state") == "running":
                runtime.run("stop", name)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
