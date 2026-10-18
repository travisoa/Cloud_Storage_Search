"""Resolve a selected connection once; never fall back to another account."""
import os


def credential_source(config):
    source = config.get("credential_source") or {"type": "file"}
    if source.get("type") == "alist":
        if type(source.get("storage_id")) is not int or source["storage_id"] < 1:
            raise ValueError("需要明确的 AList 夸克挂载 ID")
        return {"type": "alist", "storage_id": source["storage_id"]}
    if source.get("type") != "file":
        raise ValueError("不支持此网盘凭据来源")
    from .credentials import secret_path
    name = source.get("name", "quark.json")
    secret_path(name)  # Validate the reference without reading a credential.
    if name == "drives/current.json":
        raise ValueError("current 是默认网盘的保留名称")
    return {"type": "file", "name": name}


def account_scope(config):
    source = credential_source(config)
    if source["type"] == "alist":
        return "quark:alist-storage:" + str(source["storage_id"])
    name = source["name"]
    if name == "quark.json":
        if os.environ.get("QUARK_COOKIE"):
            return "quark:environment:QUARK_COOKIE"
        return "quark:file:current"
    if name.startswith("drives/"):
        return "quark:file:" + name[len("drives/"):-len(".json")]
    return "quark:file:" + name


def ready(config):
    from .credentials import exists
    source = credential_source(config)
    return (source["type"] == "alist" or
            (source["name"] == "quark.json" and bool(os.environ.get("QUARK_COOKIE"))) or
            exists(source["name"]))


def storage(config, *, cookie=None):
    from .providers import QuarkProvider
    source = credential_source(config)
    if cookie is None:
        if source["type"] == "alist":
            from .alist_credentials import AListCredentials
            with AListCredentials() as reader:
                cookie = reader.load_cookie(source["storage_id"])
        elif source["name"] == "quark.json" and os.environ.get("QUARK_COOKIE"):
            cookie = os.environ["QUARK_COOKIE"]
        else:
            from .credentials import read_secret
            cookie = read_secret(source["name"]).get("cookie")
    # None would activate QuarkProvider's environment/default-file fallback.
    if not isinstance(cookie, str) or not cookie.strip():
        raise ValueError("所选网盘凭据中没有有效 Cookie")
    provider = QuarkProvider(cookie=cookie)
    provider.credential_source = source["type"]
    provider.account_scope = account_scope(config)
    root = config.get("library_root_id")
    provider.legacy_account_scopes = ("quark-root:" + str(root),) if root is not None else ()
    return provider
