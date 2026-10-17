"""Conversation-facing commands. All discovery and file operations stay headless."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from urllib.request import urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]


def parse_request(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请提供剧名")
    quoted = re.search(r"《([^》]+)》", text)
    outside = re.sub(r"《[^》]*》", " ", text)
    if quoted:
        title, context = quoted.group(1), outside
    else:
        title = re.split(r"(?:存到|保存到|转存到|并重命名|补齐缺集)", text, maxsplit=1)[0]
        # Strip only command-position chatter. A numeric or command-like word
        # inside the actual title is never globally substituted.
        title = re.sub(r"^\s*(?:(?:好的|好|可以|行)[，,\s]+)?(?:我们(?:先)?(?:测试(?:一下|下)?|试(?:一下|下)?)[，,\s]+)?", "", title)
        command = re.match(r"^(?:请|麻烦)?(?:你)?(?:帮我|给我)?\s*(?:搜索一下|查询一下|查询下|只查询|只查|搜索|查询|查一下|查下|查找|找一下|找下|搜一下|搜|补齐|追更|看看|看下)\s*", title)
        if command:
            title = title[command.end():]
            title = re.sub(r"^(?:韩剧|日剧|美剧|英剧|国产剧|电视剧|电影|影片)\s+", "", title)
        title = title.strip(" ：:，,。；;\n")
        context = title
    season_match = re.search(r"(?:S(\d{1,2})(?![\dEe])|第\s*(\d{1,2})\s*季)", context, re.I)
    # A year must be a separate field, never a numeric part of a title.
    year_match = re.search(r"(?:^|[\s(（])((?:19|20)\d{2})(?=$|[\s)），,。；;])", context)
    if not quoted and year_match and not (context[:year_match.start()] + context[year_match.end():]).strip(" ()（）"):
        year_match = None
    if not quoted:
        if season_match:
            title = title.replace(season_match.group(0), "", 1)
        if year_match:
            title = re.sub(r"[（(]?" + year_match.group(1) + r"[）)]?", "", title, count=1)
    title = title.strip(" ._-()（）")
    if not title or len(title) > 160 or "/" in title or "\\" in title:
        raise ValueError("无法从请求确定剧名，请明确作品名称")
    readonly = bool(re.search(r"只查询|仅查询|只查|仅查|先查|先看|预览|计划|暂不|不要|别转存|别保存|不转存|不保存|不补齐|不追更", outside))
    result = {"title": title, "request_text": text,
              "save_requested": not readonly and bool(re.search(r"转存|保存|存到|补齐|追更", outside))}
    # Only the words outside the quoted title decide the media type.
    if re.search(r"电影|影片|(?<![a-z])movie(?![a-z])", outside, re.I) and not re.search(r"电视剧|剧集|追更|第\s*\d+\s*季", outside):
        result["kind"] = "movie"
    if season_match and result.get("kind") != "movie":
        result["season"] = int(season_match.group(1) or season_match.group(2))
    if year_match:
        result["year"] = int(year_match.group(1))
    return result


def _normalize(title):
    return re.sub(r"[\s._\-·:：()（）]", "", unicodedata.normalize("NFC", str(title))).casefold()


def choose_identity(title, candidates, year=None):
    """An exact, unique identity may be selected; list ordering is never evidence."""
    exact = []
    for item in candidates:
        names = [item.get("title"), item.get("name"), item.get("original_title"), item.get("original_name")]
        names.extend(item.get("aliases") or [])
        matches = any(_normalize(name) == _normalize(title) for name in names if isinstance(name, str))
        if matches and (year is None or str(item.get("year")) == str(year)):
            exact.append(item)
    unchecked = any(item.get("aliases_checked") is False and (year is None or item.get("year") in {None, year}) for item in candidates)
    return exact[0] if len(exact) == 1 and not unchecked else None


def _config():
    path = ROOT / "config/headless.json"
    if not path.is_file():
        raise ValueError("缺少 config/headless.json；请从 config/headless.example.json 复制并填写影库目录 ID")
    return json.loads(path.read_text())


def _private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _configured(name, env):
    from .credentials import exists
    return bool(os.environ.get(env)) or exists(name)


def _quark_ready(config):
    source = config.get("credential_source", {})
    return bool(source.get("type") == "alist" and source.get("storage_id") is not None) or _configured("quark.json", "QUARK_COOKIE")


def _storage(config):
    from .providers import QuarkProvider
    source = config.get("credential_source", {})
    if source.get("type") == "alist":
        from .alist_credentials import AListCredentials
        with AListCredentials() as reader:
            cookie = reader.load_cookie(source["storage_id"])
        storage = QuarkProvider(cookie=cookie)
        storage.credential_source = "alist"
        storage.account_scope = "quark-root:" + str(config["library_root_id"])
        return storage
    if os.environ.get("QUARK_COOKIE"):
        storage = QuarkProvider()
    else:
        from .credentials import read_secret
        storage = QuarkProvider(cookie=read_secret("quark.json").get("cookie"))
    storage.account_scope = "quark-root:" + str(config["library_root_id"])
    return storage


def _metadata(source):
    from .catalog import TMDBProvider, TVmazeProvider
    if source == "auto":
        source = "tmdb" if _configured("tmdb.json", "TMDB_READ_ACCESS_TOKEN") or os.environ.get("TMDB_API_KEY") else "tvmaze"
    if source == "tmdb":
        if os.environ.get("TMDB_READ_ACCESS_TOKEN") or os.environ.get("TMDB_API_KEY"):
            return TMDBProvider(read_access_token=os.environ.get("TMDB_READ_ACCESS_TOKEN"), api_key=os.environ.get("TMDB_API_KEY")), "tmdb"
        from .credentials import secret_path
        return TMDBProvider.from_credentials_file(secret_path("tmdb.json")), "tmdb"
    return TVmazeProvider(), "tvmaze"


def _bindings():
    path = ROOT / "runtime/series-bindings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _series_seasons(series):
    output = []
    for item in series.get("seasons") or []:
        season = item.get("season", item.get("season_number", item.get("number")))
        if isinstance(season, int) and 1 <= season <= 99:
            output.append(season)
    if not output and series.get("season_count", series.get("number_of_seasons")) == 1:
        output = [1]
    return sorted(set(output))


def _resolve(request, metadata, source):
    request = dict(request)
    request["metadata_source"] = source
    if not request.get("series_id"):
        candidates = metadata.search_tv(request["title"], request.get("year"))
        matched = choose_identity(request["title"], candidates, request.get("year"))
        if not matched:
            return None, {"status": "NEEDS_INPUT", "reason": "作品身份尚未唯一确认，不能自动选第一条", "title": request["title"], "metadata_source": source,
                          "metadata_candidates": candidates, "hint": "可通过对话确认作品年份、ID；缺少中文条目时配置 TMDB。"}
        request["series_id"] = matched["id"]
        if not request.get("year"):
            request["year"] = matched.get("year")
    series = metadata.get_series(request["series_id"])
    names = [series.get("title"), series.get("name"), series.get("original_title"), series.get("original_name"), *(series.get("aliases") or [])]
    actual_id = series.get("id", series.get("source_id"))
    if (str(actual_id) != str(request["series_id"]) or not any(_normalize(name) == _normalize(request["title"]) for name in names if isinstance(name, str))
            or (request.get("year") and series.get("year") and str(request["year"]) != str(series["year"]))):
        return None, {"status": "NEEDS_INPUT", "reason": "指定作品 ID 与片名或年份不一致，未保存绑定", "metadata_candidates": [series]}
    if not request.get("year"):
        request["year"] = series.get("year")
    if not request.get("season"):
        seasons = _series_seasons(series)
        if len(seasons) != 1:
            return None, {"status": "NEEDS_INPUT", "reason": "需要确认要补齐哪一季", "series_id": request["series_id"],
                          "metadata_source": source, "seasons": seasons, "title": request["title"]}
        request["season"] = seasons[0]
    if not request.get("year"):
        return None, {"status": "NEEDS_INPUT", "reason": "元数据中没有明确年份，需要确认作品身份"}
    request["identity_confirmed"] = True
    return request, None


def _profile_store():
    from .profiles import ProfileStore
    return ProfileStore(ROOT / "runtime/series-profiles.json")


def _prepare_identity(request, config):
    """Read profiles under the workflow lock, and validate aliases remotely."""
    from .profiles import ProfileMetadata
    request = dict(request)
    if request.pop('version_policy_key', None) is not None:
        request.pop('version_policy', None)
        request.pop('quality_preference', None)
    if request.get("library_root_id") not in {None, config["library_root_id"]}:
        raise ValueError("请求根目录与当前影库配置不一致")
    request["library_root_id"] = config["library_root_id"]
    profile = _profile_store().match(request)
    if profile:
        # One stable display title is used for this series, regardless of which
        # confirmed alias appeared in a subsequent conversational request.
        request.update(title=profile["title"], series_id=profile["series_id"],
                       metadata_source=profile["metadata_source"], year=profile["year"])
        request.setdefault("season", profile["season"])
    else:
        binding = _bindings().get(_normalize(request["title"]), {})
        compatible = ((not request.get("year") or str(request["year"]) == str(binding.get("year")))
                      and request.get("metadata_source") in {None, "auto", binding.get("metadata_source")})
        if compatible:
            for field in ("series_id", "metadata_source", "year", "season"):
                if not request.get(field) or request.get(field) == "auto":
                    if binding.get(field) is not None:
                        request[field] = binding[field]
    metadata, source = _metadata(request.get("metadata_source") or config["metadata_source"])
    if source == 'tmdb' and ((profile is None and callable(getattr(metadata, 'get_identity', None)))
                             or (profile is not None and request.get('external_ids') is not None)):
        from .identity_match import resolve_exact, MatchedMetadata
        # An existing local alias binding does not override an explicit public
        # ID constraint carried by a replayed task. Confirmed profile names can
        # identify its localized title, but the public IDs are checked afresh.
        match_aliases = ([*profile.get('aliases', []), profile['canonical_title']]
                         if profile else request.get('aliases', []))
        matched = resolve_exact(metadata, title=request['title'], year=request.get('year'), kind='tv',
                                tmdb_id=request.get('series_id'), aliases=match_aliases,
                                external_ids=request.get('external_ids'))
        if matched['status'] != 'MATCHED':
            return None, {**matched, 'metadata_source': 'tmdb', 'reason': matched.get('reason', 'exact_tmdb_identity_required')}, metadata, profile
        request['series_id'] = str(matched['identity']['id'])
        request['year'] = matched['identity']['year']
        request['tmdb_match'] = {'basis': matched.get('basis', 'exact_identity'),
                                 'tmdb_id': request['series_id'], 'search_complete': matched.get('search_complete')}
        metadata = MatchedMetadata(metadata, matched)
    if profile:
        metadata = ProfileMetadata(metadata, profile)
    resolved, issue = _resolve(request, metadata, source)
    if issue:
        return None, issue, metadata, profile
    if profile:
        resolved["canonical_title"] = profile["canonical_title"]
        resolved["aliases"] = list(profile["aliases"])
        resolved["identity_profile"] = {"profile_key": profile["profile_key"],
                                        "canonical_title": profile["canonical_title"],
                                        "evidence_urls": profile["evidence_urls"],
                                        "verified_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
        if profile.get("quality_preference") and not resolved.get("quality_preference"):
            resolved["quality_preference"] = list(profile["quality_preference"])
    bindings = _bindings()
    bindings[_normalize(resolved["title"])] = {key: resolved[key] for key in ("title", "series_id", "metadata_source", "year", "season")}
    _private_json(ROOT / "runtime/series-bindings.json", bindings)
    category = config.get("categories", {}).get("tv", {}).get("folder_id")
    if category:
        resolved.setdefault("category_folder_id", category)
    resolved.setdefault("as_of", datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
    resolved.setdefault("include_media_tags", config.get("include_media_tags", True))
    from .version_policy import attach
    resolved = attach(resolved, ROOT, preset=config.get("version_preset"))
    return resolved, None, metadata, profile


def _prepare_movie(request, config):
    """Confirm one TMDB movie; title search alone needs a year, never rank."""
    from .identity_match import resolve_exact
    request = dict(request)
    if request.get("library_root_id") not in {None, config["library_root_id"]}:
        raise ValueError("请求根目录与当前影库配置不一致")
    request.update(library_root_id=config["library_root_id"], metadata_source="tmdb", kind="movie")
    request.pop("season", None)
    if not (_configured("tmdb.json", "TMDB_READ_ACCESS_TOKEN") or os.environ.get("TMDB_API_KEY")):
        return None, {"status": "NEEDS_AUTH", "reason": "电影身份需要 TMDB；请在本机终端运行 configure-tmdb，无需网页",
                      "kind": "movie"}
    metadata, _ = _metadata("tmdb")
    if not request.get("movie_id") and not request.get("year"):
        result = metadata.search_identity("movie", request["title"], None)
        candidates = [{key: row.get(key) for key in ("id", "title", "original_title", "year")}
                      for row in result["results"][:10]]
        return None, {"status": "NEEDS_INPUT", "reason": "电影需要年份或 TMDB ID 才能唯一确认，不按搜索排序自动选择",
                      "kind": "movie", "title": request["title"], "metadata_candidates": candidates,
                      "search_complete": result.get("complete")}
    matched = resolve_exact(metadata, title=request["title"], year=request.get("year"), kind="movie",
                            tmdb_id=request.get("movie_id"), aliases=request.get("aliases", []))
    if matched["status"] != "MATCHED":
        return None, {**matched, "kind": "movie", "metadata_source": "tmdb",
                      "reason": matched.get("reason") or "exact_tmdb_identity_required"}
    identity = matched["identity"]
    names = [identity.get("title"), identity.get("original_title"), *identity.get("aliases", []),
             *request.get("aliases", [])]
    request.update(movie_id=str(identity["id"]), year=identity["year"],
                   aliases=[name for name in dict.fromkeys(names) if isinstance(name, str) and name.strip()
                            and name != request["title"] and not any(c in name for c in '/\\')],
                   tmdb_match={"basis": matched.get("reason"), "tmdb_id": str(identity["id"]),
                               "search_complete": matched.get("search_complete")},
                   identity_confirmed=True)
    category = config.get("categories", {}).get("movie", {}).get("folder_id")
    if category:
        request.setdefault("category_folder_id", category)
    request.setdefault("include_media_tags", config.get("include_media_tags", True))
    from .version_policy import attach
    return attach(request, ROOT, preset=config.get("version_preset")), None


def _execute_movie(task, store, config, *, apply=False, reconcile_only=False):
    from .journal import JobJournal, safe_record
    from .search import PanSouSearch
    from . import movie_workflow
    journal = None
    try:
        resolved, issue = _prepare_movie(task["resolved_request"], config)
        if issue:
            return store.finish(task, issue)
        task["resolved_request"] = resolved
        task["apply_authorized"] = bool(task.get("apply_authorized") or apply)
        task["last_execution_apply"] = bool(apply)
        store.save(task)
        if not _quark_ready(config):
            return store.finish(task, {"status": "NEEDS_AUTH", "reason": "尚未绑定可用夸克凭据；可复用 AList 挂载，不需要网页",
                                      "resolved_identity": resolved,
                                      "local_setup_command": "python3 scripts/media.py configure-alist"})
        storage = _storage(config)
        journal = JobJournal(ROOT / "runtime/jobs.sqlite")
        if reconcile_only:
            result = movie_workflow.reconcile(resolved, storage, journal)
        else:
            result = movie_workflow.run(resolved, storage, PanSouSearch(config["pansou_url"]), journal, apply=apply)
        result = store.finish(task, result, resolved_request=resolved)
        name = "movie-latest-reconcile.json" if reconcile_only else "movie-latest-apply.json" if apply else "movie-latest-plan.json"
        _private_json(ROOT / "runtime/reports" / name, safe_record(result))
        return result
    except Exception as exc:
        return store.finish(task, _safe_failure(exc))
    finally:
        if journal:
            journal.close()


def _safe_failure(exc):
    from .providers import ProviderError
    from .catalog import MetadataError
    safe = isinstance(exc, (ProviderError, MetadataError, ValueError, RuntimeError)) and not isinstance(exc, OSError)
    return {"status": "FAILED", "reason": str(exc) if safe else "后台任务未完成，请检查本机配置和运行记录",
            "error_type": type(exc).__name__, "browser_opened": False}


def _execute_task(task, store, config, *, apply=False, reconcile_only=False, remember_quality=False):
    if task["resolved_request"].get("kind") == "movie":
        return _execute_movie(task, store, config, apply=apply, reconcile_only=reconcile_only)
    from .journal import JobJournal
    from .search import PanSouSearch
    from . import workflow
    journal = None
    try:
        resolved, issue, metadata, profile = _prepare_identity(task["resolved_request"], config)
        if issue:
            return store.finish(task, issue)
        # Persist exact resolved scope and confirmation before any cloud writes.
        task["resolved_request"] = resolved
        task["apply_authorized"] = bool(task.get("apply_authorized") or apply)
        task["last_execution_apply"] = bool(apply)
        store.save(task)
        if not _quark_ready(config):
            return store.finish(task, {"status": "NEEDS_AUTH", "reason": "尚未绑定可用夸克凭据；可复用 AList 挂载，不需要网页",
                                      "resolved_identity": resolved,
                                      "local_setup_command": "python3 scripts/media.py configure-alist"})
        storage = _storage(config)
        journal = JobJournal(ROOT / "runtime/jobs.sqlite")
        if reconcile_only:
            result = workflow.reconcile(resolved, storage, metadata, journal)
        else:
            result = workflow.run(resolved, storage, metadata, PanSouSearch(config["pansou_url"]), journal, apply=apply)
        if remember_quality:
            if not profile:
                result["quality_preference_saved"] = False
                result["quality_preference_note"] = "需要先通过 bind-series 保存经核验的本剧身份，未改全局偏好"
            elif resolved.get("quality_preference"):
                _profile_store().remember_quality(profile, resolved["quality_preference"])
                result["quality_preference_saved"] = True
                result["quality_preference_scope"] = profile["profile_key"]
        result = store.finish(task, result, resolved_request=resolved)
        report_name = "latest-reconcile.json" if reconcile_only else "latest-apply.json" if apply else "latest-plan.json"
        from .journal import safe_record
        _private_json(ROOT / "runtime/reports" / report_name, safe_record(result))
        return result
    except Exception as exc:
        return store.finish(task, _safe_failure(exc))
    finally:
        if journal:
            journal.close()


def _run(request, apply=False, config=None):
    from .journal import run_lock
    from .tasks import TaskStore
    config = _config() if config is None else config
    with run_lock(ROOT / "runtime/workflow.lock"):
        store = TaskStore(ROOT / "runtime/tasks", config["library_root_id"])
        task = store.create(request, apply_authorized=apply)
        return _execute_task(task, store, config, apply=apply)


def _task_command(args, config=None):
    from .journal import run_lock
    from .tasks import TaskStore, confirmation_request
    config = _config() if config is None else config
    with run_lock(ROOT / "runtime/workflow.lock"):
        store = TaskStore(ROOT / "runtime/tasks", config["library_root_id"])
        task = store.load(args.task_id)
        if args.command == "status":
            return {"status": "TASK_STATUS", "task_id": task["task_id"], "task": task, "browser_opened": False}
        if args.command == "reconcile":
            return _execute_task(task, store, config, reconcile_only=True)
        if args.command == "resume":
            return _execute_task(task, store, config, apply=args.apply)
        selection = json.loads(args.selection) if args.selection else None
        if task["resolved_request"].get("kind") == "movie":
            from .tasks import movie_confirmation_request
            request, decision = movie_confirmation_request(task, quality=args.quality, selection=selection)
            decision["apply_authorized"] = bool(args.apply)
            task["decisions"].append(decision)
            task["resolved_request"] = request
            store.save(task)
            return _execute_task(task, store, config, apply=args.apply)
        request, decision = confirmation_request(task, quality=args.quality,
                                                numeric_episodes=args.numeric_episodes, selection=selection)
        # --apply is explicit fresh authorization, including for a prior plan.
        # Merely inspecting/choosing candidates never starts cloud mutations.
        decision["apply_authorized"] = bool(args.apply)
        decision["remember_quality"] = bool(args.remember_quality)
        task["decisions"].append(decision)
        task["resolved_request"] = request
        store.save(task)
        return _execute_task(task, store, config, apply=args.apply, remember_quality=args.remember_quality)


def main(argv=None):
    parser = argparse.ArgumentParser(description="后台影库助手：已有集数→预计播出→缺集→搜索→转存→命名，不打开网页")
    commands = parser.add_subparsers(dest="command", required=True)
    from .watch_cli import add_parser
    add_parser(commands)
    from .library_cli import add_parser as add_library_parser
    add_library_parser(commands)
    from .match_cli import add_parser as add_match_parser
    from .policy_cli import add_parser as add_policy_parser
    from .health_cli import add_parser as add_health_parser
    from .archive_cli import add_parser as add_archive_parser
    from .tidy_cli import add_parser as add_tidy_parser
    for add in (add_match_parser, add_policy_parser, add_health_parser, add_archive_parser, add_tidy_parser):
        add(commands)
    doctor = commands.add_parser("doctor", help="inspect configuration and the background search service")
    doctor.add_argument("--check-quark", action="store_true", help="read the configured library root, without writes")
    commands.add_parser("configure-quark", help="enter Cookie privately in this terminal, once")
    reuse = commands.add_parser("configure-alist", help="bind an existing AList Quark mount; credentials stay in memory")
    reuse.add_argument("--storage-id", type=int)
    commands.add_parser("alist-status", help="show mount names and credential availability, never credential values")
    tmdb = commands.add_parser("configure-tmdb", help="enter a TMDB read token privately in this terminal")
    tmdb.add_argument("--api-key", action="store_true")
    discover = commands.add_parser("discover", help="look up metadata candidates; never opens a web page")
    discover.add_argument("title")
    discover.add_argument("--year", type=int)
    discover.add_argument("--metadata-source", choices=("auto", "tmdb", "tvmaze"), default="auto")
    bind = commands.add_parser("bind-series", help="verify a remote series identity and save confirmed aliases for this series")
    bind.add_argument("--title", required=True)
    bind.add_argument("--canonical-title", required=True)
    bind.add_argument("--series-id", required=True)
    bind.add_argument("--year", type=int, required=True)
    bind.add_argument("--season", type=int, required=True)
    bind.add_argument("--metadata-source", choices=("tmdb", "tvmaze"), required=True)
    bind.add_argument("--alias", action="append", default=[])
    bind.add_argument("--evidence-url", action="append", required=True)
    bind.add_argument("--quality-preference", action="append", default=[])
    for command in ("status", "reconcile", "resume"):
        help_text = {"status": "read a local task", "reconcile": "reconcile a task using read-only cloud calls",
                     "resume": "continue the same task; cloud writes require --apply"}[command]
        command_parser = commands.add_parser(command, help=help_text)
        command_parser.add_argument("task_id")
        command_parser.add_argument('--library-id', help='use this initialized library without changing the default')
        if command == "resume":
            command_parser.add_argument("--apply", action="store_true", help="authorize continuing the persisted operation; omitted means read-only planning")
    confirm = commands.add_parser("confirm", help="confirm observed candidates and continue the same task")
    confirm.add_argument("task_id")
    confirm.add_argument('--library-id')
    confirm.add_argument("--quality", choices=("2160p", "1080p", "720p", "480p", "4K", "4k"))
    confirm.add_argument("--numeric-episodes", action="store_true", help="confirm observed bare numeric basenames as episodes of this season")
    confirm.add_argument("--selection", help='explicit episode-to-candidate JSON, e.g. {"S01E01":"candidate-id"}')
    confirm.add_argument("--apply", action="store_true", help="authorize saving/renaming; omitted means read-only planning")
    confirm.add_argument("--remember-quality", action="store_true", help="save this series preference after live identity validation")
    normalize_saved = commands.add_parser("normalize-saved", help="align only this project's verified newly saved files with their series directory")
    normalize_saved.add_argument("task_id")
    normalize_saved.add_argument('--library-id')
    normalize_saved.add_argument("--apply", action="store_true", help="authorize renaming verified new files; omitted means read-only planning")
    sync = commands.add_parser("sync", help="plan or execute a confirmed series request")
    sync.add_argument("--title", required=True)
    sync.add_argument("--year", type=int)
    sync.add_argument("--season", type=int)
    sync.add_argument("--series-id")
    sync.add_argument("--metadata-source", choices=("auto", "tmdb", "tvmaze"), default="auto")
    sync.add_argument("--target-folder-id")
    sync.add_argument("--category-folder-id")
    sync.add_argument("--library-root-is-series-category", action="store_true", default=None)
    sync.add_argument("--apply", action="store_true", help="execute the saving/renaming request authorized by the user")
    natural = commands.add_parser("request", help="run a conversational title request")
    natural.add_argument("text")
    natural.add_argument("--plan", action="store_true", help="read and plan without writes, even if the request asks to save")
    natural.add_argument("--kind", choices=("tv", "movie"), help="override the media type inferred from the request")
    args = parser.parse_args(argv)
    try:
        if args.command in {'match', 'policy', 'health', 'archive', 'tidy'}:
            from importlib import import_module
            module = import_module('.' + args.command + '_cli', __package__)
            result = module.run_command(args, _config(), ROOT)
        elif args.command == 'library':
            from .library_cli import run_command
            result = run_command(args, _config(), ROOT)
        elif args.command == 'watch':
            from .watch_cli import run_command
            result = run_command(args, _config(), ROOT)
        elif args.command in {"alist-status", "configure-alist"}:
            from .alist_credentials import AListCredentials
            with AListCredentials() as reader:
                mounts = reader.list_safe()
                if args.command == "alist-status":
                    result = {"status": "ALIST_MOUNTS", "mounts": mounts, "credential_values_displayed": False, "browser_opened": False}
                else:
                    eligible = [item for item in mounts if str(item["driver"]).casefold() == "quark" and item["cookie_present"] and not item["disabled"]]
                    selected = [item for item in eligible if args.storage_id is None or str(item["storage_id"]) == str(args.storage_id)]
                    if len(selected) != 1:
                        result = {"status": "NEEDS_INPUT", "reason": "需选择唯一的已启用 Cookie 类型夸克挂载", "mounts": mounts}
                    else:
                        from .providers import QuarkProvider
                        config = _config()
                        provider = QuarkProvider(cookie=reader.load_cookie(selected[0]["storage_id"]))
                        entries = provider.list_files(config["library_root_id"])
                        config["credential_source"] = {"type": "alist", "container": "alist", "storage_id": selected[0]["storage_id"], "mount_path": selected[0]["mount_path"]}
                        _private_json(ROOT / "config/headless.json", config)
                        result = {"status": "CONFIGURED", "credential_source": "alist", "mount_path": selected[0]["mount_path"],
                                  "library_entries": len(entries), "credential_copied_to_disk": False, "browser_opened": False}
        elif args.command in {"configure-quark", "configure-tmdb"}:
            from .credentials import read_input, write_secret
            key = "cookie" if args.command == "configure-quark" else "api_key" if args.api_key else "read_access_token"
            # A terminal prompt hides the value; on a headless host one stdin
            # line is accepted. Values are never taken from command arguments.
            value = read_input("夸克 Cookie（隐藏输入）：" if key == "cookie" else "TMDB 密钥（隐藏输入）：")
            filename = "quark.json" if key == "cookie" else "tmdb.json"
            write_secret(filename, {key: value})
            if key == "cookie":
                config = _config()
                config["credential_source"] = {"type": "file"}
                _private_json(ROOT / "config/headless.json", config)
            result = {"status": "CONFIGURED", "credential": args.command.removeprefix("configure-"), "value_displayed": False}
        elif args.command == "doctor":
            config = _config()
            try:
                with urlopen(config["pansou_url"] + "/api/health", timeout=4) as response:
                    search_ok = response.status == 200
            except OSError:
                search_ok = False
            result = {"mode": "headless", "browser_opened": False, "search_ready": search_ok,
                      "quark_configured": _quark_ready(config), "credential_source": config.get("credential_source", {}).get("type", "file_or_environment"),
                      "tmdb_configured": _configured("tmdb.json", "TMDB_READ_ACCESS_TOKEN") or bool(os.environ.get("TMDB_API_KEY")),
                      "library_root_configured": bool(config.get("library_root_id"))}
            if args.check_quark:
                files = _storage(config).list_files(config["library_root_id"])
                result["library_entries"] = len(files)
        elif args.command == "discover":
            metadata, source = _metadata(args.metadata_source)
            result = {"status": "CANDIDATES", "metadata_source": source, "candidates": metadata.search_tv(args.title, args.year), "browser_opened": False}
        elif args.command == "bind-series":
            from .journal import run_lock
            with run_lock(ROOT / "runtime/workflow.lock"):
                metadata, source = _metadata(args.metadata_source)
                profile = _profile_store().bind(metadata=metadata, source=source, series_id=args.series_id,
                                                year=args.year, season=args.season, title=args.title,
                                                canonical_title=args.canonical_title, aliases=args.alias,
                                                evidence_urls=args.evidence_url, quality_preference=args.quality_preference)
                result = {"status": "SERIES_BOUND", "profile": profile, "browser_opened": False,
                          "cloud_writes_performed": False}
        elif args.command == "normalize-saved":
            from .saved_naming_cli import run_task
            if args.library_id:
                from .library_registry import LibraryRegistry
                selected = LibraryRegistry(ROOT / 'runtime/libraries.json').config_for(args.library_id, _config())
                result = run_task(args.task_id, apply=args.apply, config=selected)
            else:
                result = run_task(args.task_id, apply=args.apply)
        elif args.command in {"status", "confirm", "reconcile", "resume"}:
            if args.library_id:
                from .library_registry import LibraryRegistry
                selected = LibraryRegistry(ROOT / 'runtime/libraries.json').config_for(args.library_id, _config())
                result = _task_command(args, config=selected)
            else:
                result = _task_command(args)
        else:
            if args.command == "request":
                request = parse_request(args.text)
                apply = request.pop("save_requested") and not args.plan
                if args.kind == "tv":
                    request.pop("kind", None)
                elif args.kind == "movie":
                    request["kind"] = "movie"
                    request.pop("season", None)
            else:
                request = {key: value for key, value in vars(args).items() if key not in {"command", "apply"} and value is not None}
                apply = args.apply
            result = _run(request, apply=apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3 if result.get("status") in {"NEEDS_INPUT", "NEEDS_AUTH", "UNKNOWN", "RISK_CONTROL"} else 1 if result.get("status") in {"FAILED", "PARTIAL"} else 0
    except Exception as exc:
        # Provider messages are intentionally sanitized; never print request URLs/headers or tracebacks.
        from .providers import ProviderError
        try:
            from .catalog import MetadataError
        except ImportError:
            MetadataError = ValueError
        safe = isinstance(exc, (ProviderError, MetadataError, ValueError, RuntimeError)) and not isinstance(exc, OSError)
        print(json.dumps({"status": "FAILED", "reason": str(exc) if safe else "后台任务未完成，请检查本机配置和运行记录", "error_type": type(exc).__name__, "browser_opened": False}, ensure_ascii=False))
        return 1
