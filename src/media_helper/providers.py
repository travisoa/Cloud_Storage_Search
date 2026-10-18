"""Small Quark storage adapter for the headless workflow.

Credentials are read only from an explicit local secret file or QUARK_COOKIE.
No vendor code is imported, no AList credentials are read, and no request or
response is logged. The caller owns idempotency and file conflict decisions.
Share references contain temporary secrets and must never enter an audit log.
"""

from __future__ import annotations

from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Callable, Protocol
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener
import uuid


class ProviderError(RuntimeError):
    """A sanitized failure; evidence contains identifiers, never HTTP payloads."""

    def __init__(self, message: str, *, step: str = "provider", task_id: str | None = None,
                 evidence: dict | None = None, code: int | None = None):
        super().__init__(message)
        self.step = step
        self.task_id = task_id
        self.evidence = dict(evidence or {})
        self.code = code


class AuthenticationError(ProviderError):
    pass


class RiskControlError(ProviderError):
    pass


class QuotaError(ProviderError):
    pass


class UnknownOutcome(ProviderError):
    """A write may have happened. Reconcile using evidence; never repeat it."""


class IncompleteListingError(ProviderError):
    pass


class Transport(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str],
                body: bytes | None, timeout: float) -> dict: ...


class StorageProvider(Protocol):
    capabilities: dict[str, bool]

    def list_files(self, parent_id: str) -> list[dict]: ...
    def share_files(self, url: str, password: str | None = None, parent_id: str = "0") -> dict: ...
    def save_files(self, share_ref: dict, files: list[dict], target_parent_id: str, *,
                   on_submitted: Callable[[dict], None] | None = None) -> dict: ...
    def task_status(self, task_id: str) -> dict: ...
    def mkdir(self, parent_id: str, name: str) -> dict: ...
    def rename(self, fid: str, name: str) -> dict: ...
    def move(self, fid: str, parent_id: str) -> dict: ...
    def file_info(self, fid: str, parent_id: str | None = None) -> dict: ...
    def trash(self, fid: str) -> dict: ...
    def extract_archive(self, fid: str, target_parent_id: str, *,
                        on_submitted: Callable[[dict], None] | None = None) -> dict: ...
    def archive_task_status(self, task_id: str) -> dict: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """Single-attempt urllib transport. Cookies cannot follow redirects."""

    MAX_RESPONSE_BYTES = 16 * 1024 * 1024

    def __init__(self):
        self._opener = build_opener(_NoRedirect(), HTTPCookieProcessor(CookieJar()))

    def request(self, method, url, *, headers, body, timeout):
        request = Request(url, data=body, headers=headers, method=method)
        try:
            response = self._opener.open(request, timeout=timeout)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(self.MAX_RESPONSE_BYTES + 1)
            if len(raw) > self.MAX_RESPONSE_BYTES:
                raise ValueError("Response exceeds limit")
            http_status = response.getcode()
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            # Preserve an HTTP authentication/risk status without its raw body.
            if http_status >= 400:
                return {"_http_status": http_status, "code": http_status}
            raise ValueError("Invalid JSON response") from None
        if not isinstance(result, dict):
            raise ValueError("Invalid JSON response")
        result["_http_status"] = http_status
        return result


_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def _identifier(value: Any, step: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProviderError("Invalid file, folder, share or task identifier", step=step)
    return value


def _name(value: Any, step: str) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 255
            or value in {".", ".."} or any(c in value for c in "/\\")
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ProviderError("Invalid file or directory name", step=step)
    return value


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,12}", value):
        return int(value)
    return None


class QuarkProvider:
    provider = "quark"
    BASE_URL = "https://drive-pc.quark.cn"
    USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) quark-cloud-drive/3.14.2 Chrome/112.0.5615.165 "
                  "Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch")
    capabilities = {"list": True, "share_list": True, "save": True, "mkdir": True,
                    "rename": True, "file_info": True, "task_status": True, "trash": True,
                    "save_submission_callback": True,
                    "archive_extract": True, "archive_submission_callback": True,
                    "move": True, "delete": False, "cross_provider_move": False}

    def __init__(self, cookie: str | None = None,
                 credential_path: str | Path = "runtime/secrets/quark.json", *,
                 transport: Transport | None = None, timeout: float = 20,
                 poll_attempts: int = 30, poll_interval: float = 1,
                 sleep: Callable[[float], None] = time.sleep,
                 page_size: int = 200, max_pages: int = 1000):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120
                or isinstance(poll_attempts, bool) or not isinstance(poll_attempts, int) or not 1 <= poll_attempts <= 300
                or not isinstance(poll_interval, (int, float)) or not 0 <= poll_interval <= 60
                or isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 500
                or isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 10000):
            raise ValueError("Invalid bounded network, polling or pagination settings")
        if cookie is not None:
            self.credential_source = "explicit"
        elif os.environ.get("QUARK_COOKIE"):
            cookie = os.environ["QUARK_COOKIE"]
            self.credential_source = "environment"
        else:
            self.credential_source = "file"
            try:
                path = Path(credential_path)
                if stat.S_IMODE(path.stat().st_mode) & 0o077:
                    raise AuthenticationError("Quark credential file must be private (0600)", step="credentials")
                with path.open(encoding="utf-8") as stream:
                    credential = json.load(stream)
                cookie = credential.get("cookie") if isinstance(credential, dict) else None
            except AuthenticationError:
                raise
            except (OSError, ValueError, UnicodeError):
                raise AuthenticationError("Quark credentials are not configured or readable", step="credentials") from None
        if (not isinstance(cookie, str) or not cookie.strip() or len(cookie) > 65536
                or "\r" in cookie or "\n" in cookie):
            raise AuthenticationError("Quark credential cookie is missing or invalid", step="credentials")
        self._cookie = cookie.strip()
        self._transport = transport or UrllibTransport()
        self.timeout = float(timeout)
        self.poll_attempts = poll_attempts
        self.poll_interval = float(poll_interval)
        self._sleep = sleep
        self.page_size = page_size
        self.max_pages = max_pages
        self._parents: dict[str, str] = {}

    def __repr__(self):
        return "<QuarkProvider configured=True>"

    def _request(self, method: str, path: str, *, query: dict | None = None,
                 payload: dict | None = None, step: str,
                 mutation: bool = False, evidence: dict | None = None) -> dict:
        params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        params.update(query or {})
        url = self.BASE_URL + path + "?" + urlencode(params)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        proof = {"request_id": uuid.uuid4().hex, "method": method, "endpoint": path, **(evidence or {})}
        try:
            response = self._transport.request(method, url, headers={
                "Cookie": self._cookie, "User-Agent": self.USER_AGENT,
                "Content-Type": "application/json", "Accept": "application/json",
                "Referer": "https://pan.quark.cn/", "Origin": "https://pan.quark.cn"},
                body=body, timeout=self.timeout)
        except Exception as exc:
            http_status = _integer(getattr(exc, "code", None)) if isinstance(exc, HTTPError) else None
            error_class = (AuthenticationError if http_status in {401, 403} else
                           RiskControlError if http_status == 429 else
                           UnknownOutcome if mutation else ProviderError)
            raise error_class("Quark request failed; operation was not retried", step=step, evidence=proof,
                              code=http_status) from None
        if not isinstance(response, dict):
            error_class = UnknownOutcome if mutation else ProviderError
            raise error_class("Quark returned an invalid response; operation was not retried", step=step, evidence=proof)
        http_status = _integer(response.get("_http_status"))
        code = _integer(response.get("code"))
        status = _integer(response.get("status"))
        if any(response.get(key) is not None and _integer(response[key]) is None
               for key in ("code", "status", "_http_status")):
            error_class = UnknownOutcome if mutation else ProviderError
            raise error_class("Quark returned invalid success markers", step=step, evidence=proof)
        # Only inspect remote messages for classification; never return them.
        message = str(response.get("message") or response.get("msg") or "")[:2000].casefold()
        risk = any(word in message for word in ("captcha", "risk", "风控", "人机", "验证码", "频繁", "安全验证"))
        auth = any(word in message for word in ("unauthorized", "login", "cookie", "登录", "身份失效", "token expired"))
        failed = (http_status is not None and http_status != 200) or code not in {None, 0} or status not in {None, 200}
        # At least one explicit success marker is required, even if data exists.
        if not failed and code is None and status is None:
            error_class = UnknownOutcome if mutation else ProviderError
            raise error_class("Quark response lacks a success marker", step=step, evidence=proof)
        if failed:
            if risk or http_status == 429 or code == 429:
                error_class = RiskControlError
                text = "Quark requested verification or rate limiting; automatic operations stopped"
            elif code in {32003, 32004}:
                error_class = QuotaError
                text = "Quark storage space is insufficient; free space or increase capacity before trying again"
            elif (path == "/1/clouddrive/archive/unarchive" and http_status in {None, 200}
                  and code == 31001 and not auth):
                # This endpoint uses 31001 for its paid extraction requirement
                # (vendor cloud_unarchive). Keep other endpoints' auth mapping.
                error_class = ProviderError
                text = "Quark archive extraction requires a supported membership; automatic retry stopped"
            elif (path == "/1/clouddrive/archive/unarchive" and http_status in {None, 200}
                  and code == 23008 and "doloading" in message and not auth):
                # A processing file provides no task ownership evidence. Never
                # reproduce vendor's automatic second POST for this response.
                error_class = UnknownOutcome
                text = "Quark archive is processing without task evidence; do not resubmit automatically"
            elif (step == "share_files" and code is not None and 41000 <= code < 42000
                  and http_status in {None, 200, 403, 404} and not auth):
                # 41xxx describes the shared link itself (expired, cancelled,
                # sharer banned). The account remains valid; skip this share.
                error_class = ProviderError
                text = "Quark share is unavailable or restricted; skipped without retry"
            elif auth or http_status in {401, 403} or code in {401, 403, 31001, 31002}:
                error_class = AuthenticationError
                text = "Quark authentication was rejected; automatic operations stopped"
            elif mutation and (http_status is not None and http_status >= 500 or status is not None and status >= 500):
                error_class = UnknownOutcome
                text = "Quark write result is unknown; do not repeat before reconciliation"
            else:
                error_class = ProviderError
                text = "Quark rejected the operation; operation was not retried"
            raise error_class(text, step=step, evidence=proof, code=code)
        return response

    def _normalize(self, raw: dict, parent_id: str, *, share: bool = False) -> dict:
        if not isinstance(raw, dict):
            raise ProviderError("Quark returned an invalid file record", step="list_files")
        fid = _identifier(raw.get("fid"), "list_files")
        name = raw.get("file_name", raw.get("name"))
        if not isinstance(name, str) or not name:
            raise ProviderError("Quark returned a file without a name", step="list_files")
        is_dir = raw.get("dir")
        if not isinstance(is_dir, bool):
            file_type = _integer(raw.get("file_type"))
            if file_type in {0, 1}:
                is_dir = file_type == 0
            else:
                raise ProviderError("Quark returned an unknown file type", step="list_files")
        size = _integer(raw.get("size"))
        item = {"fid": fid, "name": name, "file_name": name, "parent_id": parent_id,
                "is_dir": is_dir, "dir": is_dir, "size": size if size is not None and size >= 0 else None}
        modified = _integer(raw.get("updated_at", raw.get("last_update_at")))
        if modified is not None:
            item["updated_at"] = modified
        if share:
            token = raw.get("share_fid_token")
            if isinstance(token, str) and token:
                item["share_fid_token"] = token
        else:
            self._parents[fid] = parent_id
        return item

    def _list_pages(self, path: str, parent_id: str, *, query: dict | None = None, share: bool = False) -> list[dict]:
        merged: list[dict] = []
        seen: set[str] = set()
        expected_total = None
        step = "share_files" if share else "list_files"
        for page in range(1, self.max_pages + 1):
            params = {"pdir_fid": parent_id, "_page": page, "_size": self.page_size,
                      "_fetch_total": "1", "_fetch_sub_dirs": "0", "_sort": "file_type:asc,updated_at:desc"}
            params.update(query or {})
            response = self._request("GET", path, query=params, step=step)
            data = response.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise ProviderError("Quark returned an invalid directory listing", step=step)
            metadata = response.get("metadata") or {}
            total = _integer(metadata.get("_total")) if isinstance(metadata, dict) else None
            if total is not None and total >= 0:
                if expected_total is not None and total != expected_total:
                    raise IncompleteListingError("Directory changed during pagination; listing is incomplete", step=step)
                expected_total = total
            rows = data["list"]
            if not rows:
                if expected_total is not None and len(merged) < expected_total:
                    raise IncompleteListingError("Directory pagination ended before the reported total", step=step)
                return merged
            before = len(merged)
            for raw in rows:
                item = self._normalize(raw, parent_id, share=share)
                if item["fid"] in seen:
                    raise IncompleteListingError("Directory pagination contains duplicate file identifiers; listing is incomplete", step=step)
                seen.add(item["fid"])
                merged.append(item)
            if len(merged) == before:
                raise IncompleteListingError("Directory pagination repeated a page; listing is incomplete", step=step)
            if expected_total is not None:
                if len(merged) > expected_total:
                    raise IncompleteListingError("Directory contents exceed the reported total", step=step)
                if len(merged) == expected_total:
                    return merged
        raise IncompleteListingError("Directory exceeded the bounded page limit; listing is incomplete", step=step)

    def list_files(self, parent_id: str) -> list[dict]:
        parent_id = _identifier(parent_id, "list_files")
        return self._list_pages("/1/clouddrive/file/sort", parent_id)

    def share_files(self, url: str, password: str | None = None, parent_id: str = "0") -> dict:
        if not isinstance(url, str):
            raise ProviderError("A canonical Quark share URL is required", step="share_files")
        try:
            parsed = urlsplit(url)
            match = re.fullmatch(r"/s/([A-Za-z0-9]+)(?:/)?", parsed.path)
            valid = parsed.scheme == "https" and parsed.hostname == "pan.quark.cn" and not parsed.username and not parsed.password
        except ValueError:
            valid, match = False, None
        if not valid or not match:
            raise ProviderError("A canonical HTTPS pan.quark.cn share URL is required; redirects are not followed", step="share_files")
        share_id = match.group(1)
        password = password if password is not None else parse_qs(parsed.query).get("pwd", [""])[0]
        if not isinstance(password, str) or len(password) > 128 or any(ord(c) < 32 for c in password):
            raise ProviderError("Invalid share extraction code", step="share_files")
        parent_id = _identifier(parent_id, "share_files")
        response = self._request("POST", "/1/clouddrive/share/sharepage/token",
                                 payload={"pwd_id": share_id, "passcode": password}, step="share_files")
        data = response.get("data") or {}
        stoken = data.get("stoken") if isinstance(data, dict) else None
        if not isinstance(stoken, str) or not stoken:
            raise ProviderError("Quark share token was not returned", step="share_files")
        return self.share_directory({"provider": "quark", "share_id": share_id, "pwd_id": share_id,
                                     "stoken": stoken}, parent_id)

    def share_directory(self, share_ref: dict, parent_id: str = "0") -> dict:
        """List another directory with a token held by the current traversal.

        No token is cached on the provider or refreshed automatically. An
        expired token stops this enumeration through the usual provider error.
        """
        step = "share_files"
        if not isinstance(share_ref, dict) or share_ref.get("provider") not in {None, "quark"}:
            raise ProviderError("Invalid Quark share reference", step=step)
        share_id = _identifier(share_ref.get("share_id", share_ref.get("pwd_id")), step)
        parent_id = _identifier(parent_id, step)
        stoken = share_ref.get("stoken")
        if not isinstance(stoken, str) or not stoken:
            raise ProviderError("Select a valid share reference", step=step)
        files = self._list_pages("/1/clouddrive/share/sharepage/detail", parent_id, share=True,
                                 query={"pwd_id": share_id, "stoken": stoken, "force": "0", "_fetch_banner": "0", "_fetch_share": "1"})
        return {"share_ref": {"provider": "quark", "share_id": share_id, "pwd_id": share_id,
                              "stoken": stoken, "parent_id": parent_id}, "files": files}

    def task_status(self, task_id: str) -> dict:
        task_id = _identifier(task_id, "task_status")
        try:
            response = self._request("GET", "/1/clouddrive/task", query={"task_id": task_id, "retry_index": 0}, step="task_status")
        except ProviderError as exc:
            exc.task_id = task_id
            exc.evidence["task_id"] = task_id
            raise
        data = response.get("data") or {}
        if not isinstance(data, dict):
            raise ProviderError("Quark returned an invalid task status", step="task_status", task_id=task_id)
        status = _integer(data.get("status"))
        state = {0: "pending", 1: "pending", 2: "done", 4: "failed"}.get(status, "unknown")
        save_as = data.get("save_as") or {}
        fids = save_as.get("save_as_top_fids", []) if isinstance(save_as, dict) else []
        if not isinstance(fids, list):
            raise ProviderError("Quark returned invalid saved file identifiers", step="task_status", task_id=task_id)
        try:
            saved_fids = [_identifier(fid, "task_status") for fid in fids]
        except ProviderError:
            raise ProviderError("Quark returned invalid saved file identifiers", step="task_status", task_id=task_id) from None
        if len(saved_fids) != len(set(saved_fids)):
            raise ProviderError("Quark returned duplicate saved file identifiers", step="task_status", task_id=task_id)
        return {"task_id": task_id, "status": state, "remote_status": status, "saved_fids": saved_fids}

    def save_files(self, share_ref: dict, files: list[dict], target_parent_id: str, *,
                   on_submitted: Callable[[dict], None] | None = None) -> dict:
        """Submit once, notify the caller of safe task evidence, then poll.

        A caller that persists operations should durably record the evidence in
        ``on_submitted``. The callback runs before the first status request and
        receives no share credentials. Any callback failure stops polling and
        preserves the task identifier for read-only reconciliation.
        """
        step = "save_files"
        if on_submitted is not None and not callable(on_submitted):
            raise ProviderError("Save submission callback must be callable", step=step)
        target_parent_id = _identifier(target_parent_id, step)
        if not isinstance(share_ref, dict) or share_ref.get("provider") not in {None, "quark"}:
            raise ProviderError("Invalid Quark share reference", step=step)
        share_id = _identifier(share_ref.get("share_id", share_ref.get("pwd_id")), step)
        stoken = share_ref.get("stoken")
        parent_id = _identifier(share_ref.get("parent_id", "0"), step)
        if not isinstance(stoken, str) or not stoken or not isinstance(files, list) or not files:
            raise ProviderError("Select source files with a valid share reference", step=step)
        source_fids, tokens = [], []
        for item in files:
            if not isinstance(item, dict):
                raise ProviderError("Invalid selected share file", step=step)
            fid = _identifier(item.get("fid"), step)
            token = item.get("share_fid_token")
            if not isinstance(token, str) or not token:
                raise ProviderError("Selected file is missing its share file token", step=step)
            if item.get("parent_id", parent_id) != parent_id:
                raise ProviderError("Selected share files must belong to the same source directory", step=step)
            if fid in source_fids:
                raise ProviderError("Duplicate source files in one save request", step=step)
            source_fids.append(fid)
            tokens.append(token)
        evidence = {"source_fids": source_fids, "target_parent_id": target_parent_id}
        response = self._request("POST", "/1/clouddrive/share/sharepage/save", query={"app": "clouddrive"},
                                 payload={"fid_list": source_fids, "fid_token_list": tokens, "to_pdir_fid": target_parent_id,
                                          "pwd_id": share_id, "stoken": stoken, "pdir_fid": parent_id, "scene": "link"},
                                 step=step, mutation=True, evidence=evidence)
        data = response.get("data") or {}
        task_id = data.get("task_id") if isinstance(data, dict) else None
        try:
            task_id = _identifier(task_id, step)
        except ProviderError:
            raise UnknownOutcome("Quark accepted a save but returned no usable task identifier; reconcile target directory",
                                 step=step, evidence=evidence) from None
        evidence["task_id"] = task_id
        if on_submitted is not None:
            try:
                on_submitted({"task_id": task_id, "source_fids": list(source_fids),
                              "target_parent_id": target_parent_id})
            except BaseException:
                # Even an interrupted persistence callback cannot undo the POST.
                # Never poll, retry, or include the callback's exception text.
                raise UnknownOutcome("Save was submitted but recording its task failed; reconcile using its task identifier",
                                     step=step, task_id=task_id, evidence=evidence) from None
        for index in range(self.poll_attempts):
            try:
                result = self.task_status(task_id)
            except (AuthenticationError, RiskControlError) as exc:
                exc.evidence.update(evidence)
                raise
            except ProviderError:
                raise UnknownOutcome("Save was submitted but its result could not be determined; do not repeat it",
                                     step=step, task_id=task_id, evidence=evidence) from None
            if result["status"] == "done":
                return {**result, **evidence, "status": "done"}
            if result["status"] == "failed":
                evidence["saved_fids"] = result["saved_fids"]
                raise ProviderError("Quark save task failed; do not repeat before reconciling partial files",
                                    step=step, task_id=task_id, evidence=evidence)
            if result["status"] == "unknown":
                raise UnknownOutcome("Save task returned an unrecognized state; do not repeat it",
                                     step=step, task_id=task_id, evidence=evidence)
            if index + 1 < self.poll_attempts:
                self._sleep(self.poll_interval)
        raise UnknownOutcome("Save task did not finish within the polling limit; resume using its task identifier",
                             step=step, task_id=task_id, evidence=evidence)

    def mkdir(self, parent_id: str, name: str) -> dict:
        parent_id, name = _identifier(parent_id, "mkdir"), _name(name, "mkdir")
        evidence = {"parent_id": parent_id, "name": name}
        response = self._request("POST", "/1/clouddrive/file", payload={
            "pdir_fid": parent_id, "file_name": name, "dir_path": "", "dir_init_lock": False},
            step="mkdir", mutation=True, evidence=evidence)
        data = response.get("data") or {}
        try:
            fid = _identifier(data.get("fid") if isinstance(data, dict) else None, "mkdir")
        except ProviderError:
            raise UnknownOutcome("Quark accepted directory creation without a usable file identifier; reconcile parent directory",
                                 step="mkdir", evidence=evidence) from None
        return self._normalize({"fid": fid, "file_name": name, "dir": True, "size": 0}, parent_id)

    def archive_task_status(self, task_id: str) -> dict:
        """Read only the documented unarchive task result; discard messages."""
        task_id = _identifier(task_id, "archive_task_status")
        try:
            response = self._request("GET", "/1/clouddrive/task",
                                     query={"task_id": task_id, "retry_index": 0}, step="archive_task_status")
        except ProviderError as exc:
            exc.task_id = task_id
            raise
        data = response.get("data")
        if not isinstance(data, dict):
            raise UnknownOutcome("Invalid archive task result; reconcile using its identifier",
                                 step="archive_task_status", task_id=task_id)
        if "task_id" in data and data["task_id"] != task_id:
            raise UnknownOutcome("Archive task response does not match the requested identifier",
                                 step="archive_task_status", task_id=task_id)
        status = {0: "pending", 1: "pending", 2: "done", 4: "failed"}.get(_integer(data.get("status")), "unknown")
        details = data.get("unarchive_result") or {}
        if not isinstance(details, dict):
            raise UnknownOutcome("Invalid archive extraction evidence", step="archive_task_status", task_id=task_id)
        rows = details.get("list", [])
        if not isinstance(rows, list):
            raise UnknownOutcome("Invalid extracted file identifiers", step="archive_task_status", task_id=task_id)
        fids = []
        for row in rows:
            try:
                fid = _identifier(row.get("fid") if isinstance(row, dict) else None, "archive_task_status")
            except ProviderError:
                raise UnknownOutcome("Invalid extracted file identifiers", step="archive_task_status", task_id=task_id) from None
            if fid in fids:
                raise UnknownOutcome("Duplicate extracted file identifiers", step="archive_task_status", task_id=task_id)
            fids.append(fid)
        result = {"task_id": task_id, "status": status, "extracted_fids": fids}
        if status == "failed":
            # Only classify; neither messages nor embedded passwords are returned.
            message = str(details.get("message") or data.get("message") or response.get("message") or "").casefold()
            reason = ("password_required" if any(value in message for value in ("password", "密码", "加密")) else
                      "multipart_requires_review" if any(value in message for value in ("volume", "分卷")) else
                      "membership_required" if any(value in message for value in ("svip", "会员")) else
                      "unsupported_archive" if any(value in message for value in ("unsupported", "not support", "不支持")) else
                      "archive_task_failed")
            result["reason"] = reason
        return result

    def extract_archive(self, fid: str, target_parent_id: str, *, on_submitted=None) -> dict:
        """Submit once; persist the returned task before bounded status polling.

        Evidence: vendor/qasx cloud_unarchive/query_unarchive_task. No password,
        volume, overwrite, deletion or inferred extraction parameters are sent.
        """
        step = "extract_archive"
        fid = _identifier(fid, step)
        target_parent_id = _identifier(target_parent_id, step)
        if on_submitted is not None and not callable(on_submitted):
            raise ProviderError("Archive submission callback must be callable", step=step)
        evidence = {"source_fid": fid, "target_parent_id": target_parent_id}
        response = self._request("POST", "/1/clouddrive/archive/unarchive",
                                 payload={"fid": fid, "select_mode": 1, "to_pdir_fid": target_parent_id},
                                 step=step, mutation=True, evidence=evidence)
        data = response.get("data") or {}
        try:
            task_id = _identifier(data.get("task_id") if isinstance(data, dict) else None, step)
        except ProviderError:
            raise UnknownOutcome("Archive submission returned no task identifier; do not repeat it",
                                 step=step, evidence=evidence) from None
        evidence["task_id"] = task_id
        if on_submitted is not None:
            try:
                on_submitted(dict(evidence))
            except BaseException:
                raise UnknownOutcome("Archive was submitted but recording its task failed; do not repeat it",
                                     step=step, task_id=task_id, evidence=evidence) from None
        for index in range(self.poll_attempts):
            try:
                result = self.archive_task_status(task_id)
            except (AuthenticationError, RiskControlError) as exc:
                exc.task_id = task_id
                raise
            except ProviderError:
                raise UnknownOutcome("Archive task outcome is unknown; do not repeat it",
                                     step=step, task_id=task_id, evidence=evidence) from None
            if result["status"] in {"done", "failed"}:
                return {**result, **evidence}
            if result["status"] == "unknown":
                break
            if index + 1 < self.poll_attempts:
                self._sleep(self.poll_interval)
        raise UnknownOutcome("Archive task did not complete within the polling limit; reconcile its task identifier",
                             step=step, task_id=task_id, evidence=evidence)

    def rename(self, fid: str, name: str) -> dict:
        fid, name = _identifier(fid, "rename"), _name(name, "rename")
        self._request("POST", "/1/clouddrive/file/rename", payload={"fid": fid, "file_name": name},
                      step="rename", mutation=True, evidence={"fid": fid, "name": name})
        return {"fid": fid, "name": name, "file_name": name, "status": "done"}

    def move(self, fid: str, parent_id: str) -> dict:
        """Submit one in-account move; callers verify the unchanged fid by GET.

        This response is submission evidence, not proof that the destination
        listing is already consistent. Unknown outcomes are never retried here.
        """
        fid, parent_id = _identifier(fid, "move"), _identifier(parent_id, "move")
        if fid == parent_id:
            raise ProviderError("A file cannot be moved into itself", step="move")
        response = self._request("POST", "/1/clouddrive/file/move", payload={
            "action_type": 1, "exclude_fids": [], "filelist": [fid], "to_pdir_fid": parent_id},
            step="move", mutation=True, evidence={"fid": fid, "target_parent_id": parent_id})
        result = {"fid": fid, "target_parent_id": parent_id, "status": "submitted"}
        data = response.get("data")
        if isinstance(data, dict) and data.get("task_id") is not None:
            try:
                result["task_id"] = _identifier(data["task_id"], "move")
            except ProviderError:
                raise UnknownOutcome("Move returned an invalid task identifier; verify the original file by fid",
                                     step="move", evidence=result) from None
        return result

    def trash(self, fid: str) -> dict:
        """Move one file to the Quark recycle bin (recoverable); never empties it.

        Evidence: vendor/qasx delete() uses this endpoint; permanent removal is
        the separate recycle/remove endpoint, which this project never calls.
        Callers verify the fid is gone from its parent listing.
        """
        fid = _identifier(fid, "trash")
        response = self._request("POST", "/1/clouddrive/file/delete", payload={
            "action_type": 2, "filelist": [fid], "exclude_fids": []},
            step="trash", mutation=True, evidence={"fid": fid})
        result = {"fid": fid, "status": "submitted"}
        data = response.get("data")
        if isinstance(data, dict) and data.get("task_id") is not None:
            try:
                result["task_id"] = _identifier(data["task_id"], "trash")
            except ProviderError:
                raise UnknownOutcome("Recycle request returned an invalid task identifier; verify by listing",
                                     step="trash", evidence=result) from None
        return result

    def file_info(self, fid: str, parent_id: str | None = None) -> dict:
        """Verify through a complete parent listing, not an unverified API route."""
        fid = _identifier(fid, "file_info")
        parent_id = parent_id if parent_id is not None else self._parents.get(fid)
        if parent_id is None:
            raise ProviderError("Provide parent_id to verify a file not previously listed by this provider", step="file_info")
        for item in self.list_files(parent_id):
            if item["fid"] == fid:
                return item
        raise ProviderError("File is absent from the verified parent directory", step="file_info", evidence={"fid": fid, "parent_id": parent_id})
