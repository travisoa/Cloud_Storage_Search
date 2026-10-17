# 新网盘影库初始化与可选导入

先接入网盘，在指定父目录建立影库，再选择是否盘点和整理已有资源。初始化完成即可结束，不会自动扫描、迁移文件或切换现有影库。入口均为 `scripts/media.py library`，不需要网页。

## 接入与建目录

夸克网盘可引用 AList 中已启用的 Cookie 类型挂载（只保存挂载引用），或用 `library connect --drive-id X --cookie` 把 Cookie 存为私有凭据文件 `drives/X.json`（不依赖 AList，适合 Linux 部署）。连接前都会只读列出网盘根目录确认可用。其他网盘需要实现相应 Provider，不能因 AList 已挂载就声称已支持写入。

```bash
python3 scripts/media.py library drives
python3 scripts/media.py library connect --drive-id second-quark --alist-storage-id 2
python3 scripts/media.py library init --drive-id second-quark --library-id family --parent-id 0 --name 影库 --version-preset balanced --plan
python3 scripts/media.py library init --drive-id second-quark --library-id family --parent-id 0 --name 影库 --version-preset balanced --apply
python3 scripts/media.py library status family
```

挂载 ID 和父目录须来自用户选择；`0` 是该连接下的网盘根目录。命令中的新挂载仅为示例。默认结构：

```text
影库/
├── 电影/
├── 电视剧/
├── 动漫/
├── 纪录片/
└── 综艺/
```

单部作品及季目录在有明确资源时才创建。唯一同名目录可以复用；同名文件、重复目录和身份变化停止执行。每次 mkdir 先保存意图，返回真实 fid 后立即保存并最多3次只读核验；列表延迟时暂停，不重复创建。继续同一任务前可在原 `init` 命令使用 `--reconcile`，它始终只读。没有返回 fid 的未知结果不能凭同名目录认领。

网盘与影库登记存于私有的 `runtime/libraries.json`，同名连接不能换账号，同名影库不能换目录。初始化完成不会修改默认 `config/headless.json` 或现有追更订阅。

## 版本预设

建库时用 `--version-preset` 选择影库默认版本规则；未选择时 `--plan` 结果会列出三个选项，可之后用 `library preset family --set storage` 设置或 `--clear` 清除。大小按单个电影文件或单集文件计，来自文件列表大小与名称标签，不探测码流。

| 预设 | 分辨率顺序 | 单文件大小（电影 / 单集） | 同档版本 |
| --- | --- | --- | --- |
| `quality` 品质优先 | 2160p → 1080p | ≥3GB / ≥0.5GB，不设上限 | 偏好HDR，选更大的版本 |
| `balanced` 兼顾品质与存储 | 2160p → 1080p | 1.5–25GB / 0.3–5GB | 偏好HEVC和HDR，限额内选更大的版本 |
| `storage` 节省存储 | 1080p → 720p → 2160p | 0.7–8GB / 0.15–2GB | 偏好HEVC/AV1，选更小的版本 |

优先级为：单剧 `policy set` 规则 > 本次 `confirm` 指定的画质或候选 > 影库预设。超出预设的版本仍会出现在候选里，对话中明确选择即可按本次选择执行。预设只影响之后的待补资源，不替换、升级或删除已存文件，也不扩大追更授权范围。

## 可选扫描与整理

```bash
python3 scripts/media.py library scan family --source-id 0
python3 scripts/media.py library plan family SCAN_ID
python3 scripts/media.py library organize family PLAN_ID --plan
# 用户明确选择此计划并授权移动及重命名后：
python3 scripts/media.py library organize family PLAN_ID --apply
```

扫描只读，排除目标影库，遇分页不完整、循环或数量/深度上限不生成可执行的完整计划。预览和计划 ID 绑定账号、源快照、影库及分类目录，不能跨网盘或影库复用。

作品必须由明确年份、唯一精确标题或已确认别名及实时元数据核验。电视剧还须有明确季集号，并能在该作品的季表中确认；不按文件顺序或纯数字名猜测。电影使用 TMDB 电影接口，电视剧可使用 TMDB/TVmaze 和已确认的作品身份。当前自动整理规则覆盖电影和电视剧；动漫、纪录片、综艺目录先建立，细分类规则尚未自动启用。

整理形式为 `电影/片名 (年份)/片名 (年份) [媒体标签].扩展名`，或 `电视剧/剧名 (年份)/Season 01/剧名 (年份) S01E01 [媒体标签].扩展名`。媒体标签只来自文件或目录，不冒充码流探测。字幕仅在同目录唯一关联视频时跟随；压缩包、未知大小、身份/季集歧义和重复目标保留原位，列入待处理项，不自动解压、不删除、不覆盖。

只有明确授权的已保存计划才能改动旧文件。每次目录创建、移动或改名前重核账号、完整目标目录链和源 fid/名称/大小；移动后还须在目标读取相同 fid，并确认源目录已无该文件。原路径及操作意图进入独立日志。未知移动/改名先只读对账，本次不继续写入，也不盲目重发。当前调用方式不具备条件写入，整理同一范围时应避免其他客户端同时修改文件；逐步复核不能提供跨客户端原子事务。

## 使用新影库与隔离测试

初始化的新影库可显式承接已有电视剧助手请求，不切换当前默认影库：

```bash
python3 scripts/media.py library request family '帮我查下《剧名》，存到我的网盘' --plan
python3 scripts/media.py reconcile TASK_ID --library-id family
python3 scripts/media.py resume TASK_ID --library-id family --apply
```

`library request` 沿用电视剧与电影的转存能力（电影可加 `--kind movie`）；新影库的追更订阅尚未接入。`--plan` 始终优先于写入；初始化本身不创建任何新订阅。

当前连接 `current` 及其已有影库受原范围保护。测试初始化须带 `--test`，只能在现有影库内创建隔离目录；测试影库不能成为转存目标，也不能执行旧资源迁入，只允许扫描与整理预览。本轮真实测试状态见[验证报告](test-report.md)。

元数据接口参考 [TMDB 电影搜索](https://developer.themoviedb.org/reference/search-movie)、[电影详情](https://developer.themoviedb.org/reference/movie-details)；夸克移动请求参考 [AList Quark 驱动源码](https://github.com/AlistGo/alist/blob/main/drivers/quark_uc/driver.go)。后台仍直接调用网盘接口，AList 仅提供已授权的登录引用。
