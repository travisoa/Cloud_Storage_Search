"""Container runtime detection and command assembly for scripts/manage.py, without real containers."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "manage.py"
spec = importlib.util.spec_from_file_location("manage_script", SCRIPT)
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)

IMAGE = "ghcr.io/fish2018/pansou@sha256:abc"


def which_of(*available):
    return lambda name: name if name in available else None


class FakeRunner:
    """Record argv and answer list/ps/inspect from canned per-runtime JSON."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, argv, check, text, capture_output):
        self.calls.append(argv)
        for prefix, stdout in self.responses.items():
            if tuple(argv[1:1 + len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, 0, stdout, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def apple_list(*items):
    return json.dumps([{"id": name, "configuration": {"id": name, "labels": labels},
                        "status": {"networks": [], "state": state}} for name, labels, state in items])


def engine_inspect(name, labels, status):
    return json.dumps([{"Name": "/" + name, "Config": {"Labels": labels}, "State": {"Status": status}}])


OWN = {"project": "cloud-storage-search"}


class DetectRuntimeTests(unittest.TestCase):
    def test_prefers_apple_container_on_macos(self):
        kind, path = manage.detect_runtime({}, which_of("container", "docker", "podman"), "darwin")
        self.assertEqual((kind, path), ("container", "container"))

    def test_falls_back_to_default_apple_path_when_not_on_path(self):
        kind, path = manage.detect_runtime({}, which_of(manage.APPLE_CONTAINER, "docker"), "darwin")
        self.assertEqual((kind, path), ("container", manage.APPLE_CONTAINER))

    def test_linux_ignores_container_name_and_prefers_docker_then_podman(self):
        self.assertEqual(manage.detect_runtime({}, which_of("container", "docker", "podman"), "linux")[0], "docker")
        self.assertEqual(manage.detect_runtime({}, which_of("container", "podman"), "linux")[0], "podman")

    def test_environment_override_by_name_or_path(self):
        env = {manage.RUNTIME_ENV: "podman"}
        self.assertEqual(manage.detect_runtime(env, which_of("docker", "podman"), "darwin"), ("podman", "podman"))
        env = {manage.RUNTIME_ENV: "/opt/bin/docker"}
        self.assertEqual(manage.detect_runtime(env, which_of("/opt/bin/docker"), "linux"), ("docker", "/opt/bin/docker"))

    def test_environment_override_rejects_unknown_or_missing_runtime(self):
        with self.assertRaisesRegex(RuntimeError, "只支持"):
            manage.detect_runtime({manage.RUNTIME_ENV: "nerdctl"}, which_of("nerdctl"), "linux")
        with self.assertRaisesRegex(RuntimeError, "不可执行"):
            manage.detect_runtime({manage.RUNTIME_ENV: "podman"}, which_of("docker"), "linux")

    def test_no_runtime_found(self):
        with self.assertRaisesRegex(RuntimeError, manage.RUNTIME_ENV):
            manage.detect_runtime({}, which_of(), "linux")


class InventoryTests(unittest.TestCase):
    def test_apple_list_keeps_only_project_names(self):
        runner = FakeRunner({("list",): apple_list(("alist", {}, "running"), ("media-pansou", OWN, "stopped"))})
        items = manage.Runtime("container", "/usr/local/bin/container", runner).inventory()
        self.assertEqual(items, {"media-pansou": {"state": "stopped", "labels": OWN}})
        self.assertEqual(runner.calls, [["/usr/local/bin/container", "list", "--all", "--format", "json"]])

    def test_docker_and_podman_use_exact_name_filter_then_inspect(self):
        for kind in ("docker", "podman"):
            runner = FakeRunner({("ps",): "media-pansou\n",
                                 ("container", "inspect"): engine_inspect("media-pansou", OWN, "exited")})
            items = manage.Runtime(kind, kind, runner).inventory()
            self.assertEqual(items["media-pansou"]["state"], "exited")
            self.assertEqual(runner.calls, [
                [kind, "ps", "-a", "--filter", "name=^media-pansou$", "--format", "{{.Names}}"],
                [kind, "container", "inspect", "media-pansou"]])

    def test_engine_absent_container_is_not_inspected(self):
        runner = FakeRunner({("ps",): ""})
        self.assertEqual(manage.Runtime("docker", "docker", runner).inventory(), {})
        self.assertEqual(len(runner.calls), 1)

    def test_engine_null_labels_are_rejected_as_foreign(self):
        runner = FakeRunner({("ps",): "media-pansou\n",
                             ("container", "inspect"): engine_inspect("media-pansou", None, "running")})
        with self.assertRaisesRegex(RuntimeError, "不属于本项目"):
            manage.Runtime("podman", "podman", runner).inventory()

    def test_apple_foreign_container_with_same_name_is_rejected(self):
        runner = FakeRunner({("list",): apple_list(("media-pansou", {"project": "other"}, "running"))})
        with self.assertRaisesRegex(RuntimeError, "不属于本项目"):
            manage.Runtime("container", "container", runner).inventory()


class CommandAssemblyTests(unittest.TestCase):
    def test_run_args_keep_binding_limits_label_and_environment(self):
        cache = Path("/srv/app/runtime/pansou-cache")
        args = manage.Runtime("docker", "docker", FakeRunner()).pansou_run_args(IMAGE, cache)
        self.assertEqual(args[:2], ["run", "-d"])
        self.assertEqual(args[-1], IMAGE)
        pairs = dict(zip(args[2:-1:2], args[3:-1:2]))
        self.assertEqual(pairs["--name"], "media-pansou")
        self.assertEqual(pairs["--cpus"], "2")
        self.assertEqual(pairs["--memory"], "512M")
        self.assertEqual(pairs["-l"], "project=cloud-storage-search")
        self.assertEqual(pairs["-p"], "127.0.0.1:8888:8888")
        self.assertEqual(pairs["-v"], f"{cache}:/app/cache")
        envs = [args[i + 1] for i, value in enumerate(args) if value == "-e"]
        self.assertIn("ENABLED_PLUGINS=quark4k,quarksoo,quarktv,pansearch,zhizhen", envs)
        self.assertIn("PLUGIN_TIMEOUT=15", envs)
        self.assertEqual(len(envs), 6)

    def test_podman_relabels_cache_mount_others_do_not(self):
        cache = Path("/srv/cache")
        for kind, mount in (("podman", "/srv/cache:/app/cache:Z"), ("docker", "/srv/cache:/app/cache"),
                            ("container", "/srv/cache:/app/cache")):
            args = manage.Runtime(kind, kind, FakeRunner()).pansou_run_args(IMAGE, cache)
            self.assertEqual(args[args.index("-v") + 1], mount)

    def test_run_prefixes_runtime_executable(self):
        runner = FakeRunner()
        manage.Runtime("podman", "/usr/bin/podman", runner).run("stop", "media-pansou")
        self.assertEqual(runner.calls, [["/usr/bin/podman", "stop", "media-pansou"]])


if __name__ == "__main__":
    unittest.main()
