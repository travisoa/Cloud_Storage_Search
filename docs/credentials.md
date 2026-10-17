# 密钥存储与配置

本项目用到两类凭据：夸克网盘 Cookie（读写网盘）和可选的 TMDB 读取令牌（作品身份与日程）。凭据不进 Git、命令行参数、日志、任务记录或报告；配置文件只保存“去哪里取”的引用。

## 存放位置

| 凭据 | 来源 | 位置 |
| --- | --- | --- |
| 默认影库的夸克 Cookie | AList 挂载引用，或私有文件 | `config/headless.json` 的 `credential_source`：`{"type": "alist", "storage_id": N}` 运行时从 AList 读取；`{"type": "file"}` 读 `quark.json` |
| 其他网盘连接的 Cookie | AList 挂载引用，或私有文件 | `runtime/libraries.json` 记录引用；文件型为 `drives/<网盘ID>.json` |
| TMDB 令牌 | 私有文件 | `tmdb.json`（`read_access_token`，或用 `--api-key` 保存的 `api_key`） |

私有文件都在同一个凭据目录：默认是项目内被忽略的 `runtime/secrets/`，可用环境变量 `MEDIA_HELPER_SECRETS_DIR` 改到别处。读取时要求目录为 0700、文件为 0600、属于当前运行用户且不是符号链接，不满足就拒绝读取。文件是 JSON 明文，安全性依赖文件权限，不做加密；这样在 macOS 和 Linux 上用法一致。

AList 引用模式在运行时通过 Apple Container 的 `container exec alist` 只读取 AList 数据库，Cookie 只进内存、不写入本项目。这种模式只适用于本机已有 AList 容器的 macOS 环境；其他环境请用私有文件模式。

## 配置命令

```bash
# 默认影库：引用 AList 挂载，或改用私有 Cookie 文件
python3 scripts/media.py configure-alist --storage-id 1
python3 scripts/media.py configure-quark

# 新网盘连接：私有 Cookie 文件；过期后替换
python3 scripts/media.py library connect --drive-id family --cookie
python3 scripts/media.py library credential --drive-id family

# TMDB
python3 scripts/media.py configure-tmdb

# 只读检查
python3 scripts/media.py doctor --check-quark
```

在终端运行时隐藏输入；没有终端的服务器可从标准输入传一行，例如 `printf '%s\n' "$COOKIE" | python3 scripts/media.py configure-quark`。`library connect --cookie` 和 `library credential` 保存前会只读列出网盘根目录；替换 Cookie 时还要求仍能看到该网盘已登记的影库目录，防止换成另一个账号。

环境变量 `QUARK_COOKIE`、`TMDB_READ_ACCESS_TOKEN`、`TMDB_API_KEY` 会临时覆盖默认影库的文件凭据，适合一次性调试。定时任务（如 heartbeat）不一定继承交互 shell 的环境变量，常规运行请用文件。

## 部署到其他机器

1. 复制项目，不复制 `runtime/`。
2. 设置 `MEDIA_HELPER_SECRETS_DIR` 指向只有运行用户可读的目录（可选，默认 `runtime/secrets/`）。
3. 用 `configure-quark`、`configure-tmdb` 重新录入凭据，并在 `config/headless.json` 填入影库根目录和分类目录 ID（可从 `config/headless.example.json` 复制）。
4. 运行 `doctor --check-quark` 确认可读。

`scripts/manage.py` 目前用 Apple Container 管理 PanSou；在 Linux 上需自行用 Docker 或 Podman 运行同一 PanSou 镜像，并把 `pansou_url` 指向它。
