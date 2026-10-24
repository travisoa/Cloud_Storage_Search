# 凭据存储与配置

凭据包括夸克网盘 Cookie、用于作品身份与日程的可选 TMDB 读取令牌或 API Key，以及用于日程补缺的可选 Trakt Client ID。凭据值不进入 Git、命令参数、日志、任务记录或报告；普通配置仅保存凭据来源引用。

## 存放位置

| 凭据 | 来源 | 位置 |
| --- | --- | --- |
| 默认影库的夸克 Cookie | AList 挂载引用，或私有文件 | `config/headless.json` 的 `credential_source`：`{"type": "alist", "storage_id": N}` 运行时读取 AList；`{"type": "file"}` 读取 `quark.json` |
| 其他网盘连接的 Cookie | AList 挂载引用，或私有文件 | `runtime/libraries.json` 记录引用；文件型为 `drives/<网盘ID>.json` |
| TMDB 令牌 | 私有文件 | `tmdb.json`（`read_access_token`，或用 `--api-key` 保存的 `api_key`） |
| Trakt Client ID | 私有文件 | `trakt.json`（`client_id`）；`configure-trakt` 同时把 `config/headless.json` 的 `trakt_schedule_fill` 设为 `true` |

私有凭据默认位于被 Git 忽略的 `runtime/secrets/`，可由 `MEDIA_HELPER_SECRETS_DIR` 指定目录。创建权限为目录 0700、文件 0600；读取拒绝向组或其他用户开放权限、不属于当前用户、符号链接或类型不符的路径。文件采用未加密 JSON，依赖文件权限保护，macOS 与 Linux 使用相同存储规则。

AList 引用模式通过 Apple Container 的 `container exec alist` 只读访问 AList 数据库，Cookie 仅保留在内存，不写入本项目。该模式要求 macOS 及既有 AList 容器，其他环境使用私有文件模式。本项目不修改或重启 AList。

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

# Trakt（日程补缺；只需 Client ID，不需要 Client Secret 或用户登录）
python3 scripts/media.py configure-trakt

# 只读检查
python3 scripts/media.py doctor --check-quark
```

终端采用隐藏输入；无终端环境接受一行标准输入，例如 `printf '%s\n' "$MEDIA_QUARK_COOKIE" | python3 scripts/media.py configure-quark`，变量值须预先由安全方式提供，不在命令中填写凭据。`library connect --cookie` 和 `library credential` 保存前只读列出网盘根目录；已有登记影库时，还须核验相同父目录下的影库 fid，防止切换账号。

默认 `quark.json` 文件模式允许 `QUARK_COOKIE` 临时覆盖；指定其他凭据文件时只读取该文件，不回退到环境变量或默认文件。文件缺少有效 Cookie 时在构造 Provider 前停止。AList 引用模式只读取指定挂载。`current` 是默认网盘保留名，不能注册为文件型新网盘。`TMDB_READ_ACCESS_TOKEN`、`TMDB_API_KEY` 可覆盖对应文件值；`TRAKT_CLIENT_ID` 可覆盖 `trakt.json`，补缺仍需 `trakt_schedule_fill` 为 `true`。Trakt 应用连续 30 天无调用会被 Trakt 自动删除，之后需重建应用并重新录入。定时任务不保证继承交互 shell 环境，常规运行应配置持久凭据。认证失败、验证码或风控按[执行范围](headless-design.md#执行范围)停止，不循环重试。

## 部署到其他机器

1. 复制项目，排除 `runtime/`。
2. 按需设置 `MEDIA_HELPER_SECRETS_DIR`，默认使用 `runtime/secrets/`。
3. 通过 `configure-quark`、`configure-tmdb` 录入凭据；从 `config/headless.example.json` 建立配置并填写影库根目录及分类目录 ID。
4. 运行 `doctor --check-quark` 检查读取能力。

`scripts/manage.py` 按 Apple Container（仅 macOS）、Docker、Podman 的顺序选择运行时，通过 `up`、`status`、`stop` 管理 PanSou。容器名、镜像标签、端口与资源限制跨平台保持一致；可用 `MEDIA_HELPER_CONTAINER_RUNTIME=podman` 或可执行文件路径（如 `/usr/bin/docker`）显式指定运行时。

运行用户须能直接调用所选运行时；Docker 可通过 `docker` 组授权，或使用无 root 的 Podman。Podman 缓存目录使用 `:Z` 挂载以适配 SELinux。`config/images.lock.json` 记录 `linux/arm64` 镜像 digest；x86_64 主机拉取失败时须使用对应平台的 digest。服务固定监听 `127.0.0.1:8888`，与示例配置中的 `pansou_url` 一致。实际部署与验证记录见[验证报告](test-report.md)。
