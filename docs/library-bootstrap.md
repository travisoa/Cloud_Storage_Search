# 影库初始化与资源导入

入口为 `scripts/media.py library`。先连接网盘并在指定父目录建立影库，再按需要扫描和整理已有资源。初始化仅创建目录，不自动扫描、迁移文件或切换默认影库。

## 接入与建目录

夸克支持 AList 中已启用的 Cookie 类型挂载引用，或通过 `library connect --drive-id X --cookie` 配置独立凭据，存储与平台要求见[凭据配置](credentials.md)。连接前只读列出网盘根目录。其他网盘须实现相应 Provider，AList 挂载本身不提供本项目的写入能力。

```bash
python3 scripts/media.py library drives
python3 scripts/media.py library connect --drive-id second-quark --alist-storage-id 2
python3 scripts/media.py library init --drive-id second-quark --library-id family --parent-id 0 --name 影库 --version-preset balanced --plan
python3 scripts/media.py library init --drive-id second-quark --library-id family --parent-id 0 --name 影库 --version-preset balanced --apply
python3 scripts/media.py library status family
```

挂载 ID 和父目录由用户指定，`0` 表示所选网盘根目录。示例 ID 须替换为实际值。默认目录结构：

```text
影库/
├── 电影/
├── 电视剧/
├── 动漫/
├── 纪录片/
└── 综艺/
```

作品及季目录仅在资源已明确时创建。唯一同名目录可复用；同名文件、重复目录或身份变化会停止执行。目录创建按[恢复与审计](headless-design.md#恢复与审计)持久化意图与真实 fid。原 `init` 命令附加 `--reconcile` 可只读对账；未返回 fid 的未知结果不得仅凭同名目录认领或重复创建。

网盘与影库登记保存在私有 `runtime/libraries.json`。同名连接不得切换账号，同名影库不得变更目录。初始化不修改默认 `config/headless.json` 或既有追更订阅。

## 版本预设

`--version-preset` 设置影库默认版本规则；未指定时，`--plan` 列出可选预设。后续可用 `library preset family --set storage` 设置，或用 `--clear` 清除。大小按单个电影文件或单集文件计算，媒体依据见[命名与媒体信息](headless-design.md#命名与媒体信息)。

| 预设 | 分辨率顺序 | 单文件大小（电影 / 单集） | 同档版本 |
| --- | --- | --- | --- |
| `quality` 品质优先 | 2160p → 1080p | ≥3 GB / ≥0.5 GB，不设上限 | 偏好 HDR，体积降序 |
| `balanced` 兼顾品质与存储 | 2160p → 1080p | 1.5–25 GB / 0.3–5 GB | 偏好 HEVC 和 HDR，限额内体积降序 |
| `storage` 节省存储 | 1080p → 720p → 2160p | 0.7–8 GB / 0.15–2 GB | 偏好 HEVC/AV1，体积升序 |

优先级为：作品级 `policy set` 策略 > 任务中 `confirm` 指定的画质或候选 > 影库预设。预设之外的版本仍列入候选，明确选择后可覆盖该任务的影库预设。预设仅影响待补资源，不替换、升级或删除已存文件，不扩大追更授权范围。作品级策略见[版本策略](media-maintenance.md#按剧设置版本策略)。

## 可选扫描与整理

```bash
python3 scripts/media.py library scan family --source-id 0
python3 scripts/media.py library plan family SCAN_ID
python3 scripts/media.py library organize family PLAN_ID --plan
# 已授权计划的移动与重命名
python3 scripts/media.py library organize family PLAN_ID --apply
```

扫描只读并排除目标影库，分页不完整、循环或触及数量/深度上限时不生成可执行的完整计划。预览和计划 ID 绑定账号、源快照、影库及分类目录，不跨网盘或影库复用。

作品须具备明确年份，并通过唯一精确标题或已确认别名及实时元数据核验。电视剧还须有明确季集号，且能在对应季表中确认，不按文件顺序或纯数字名推断。电影使用 TMDB 电影接口，电视剧使用 TMDB/TVmaze 及已确认身份，详见[元数据来源](metadata-assessment.md)。自动整理覆盖电影和电视剧；动漫、纪录片、综艺仅创建分类目录，未启用专用整理规则。

目标结构为 `电影/片名 (年份)/视频文件` 或 `电视剧/剧名 (年份)/Season 01/视频文件`，视频及字幕使用[统一命名规则](headless-design.md#命名与媒体信息)。字幕须在同目录唯一关联视频。压缩包、未知大小、身份或季集歧义、重复目标均保留原位并列入待处理，不自动解压、删除或覆盖。

仅执行已明确授权的已保存计划。每次目录创建、移动或改名前复核账号、完整目标目录链和源 fid、名称、大小；移动后在目标读取相同 fid，并确认源目录已无该文件。原路径与操作意图写入独立日志。移动或改名结果未知时，仅对账并停止该轮写入，不重复提交。接口不提供条件写入或跨客户端原子事务，整理期间须避免其他客户端修改同一范围。

## 使用新影库与隔离测试

初始化后的非测试影库可显式承接媒体请求，保留默认影库设置：

```bash
python3 scripts/media.py library request family '转存《剧名》2026年第1季' --plan
python3 scripts/media.py reconcile TASK_ID --library-id family
python3 scripts/media.py resume TASK_ID --library-id family --apply
```

`library request` 支持电视剧和电影，电影可指定 `--kind movie`。该入口未接入新影库追更订阅；`--plan` 优先于写入，初始化不创建订阅。

默认连接 `current` 受既有影库范围保护，测试初始化须带 `--test`，仅可在该影库内创建隔离目录。测试影库只允许扫描与整理预览，不承接转存或旧资源迁入。模拟验证、真实操作验收及已知限制见[验证报告](test-report.md)。

接口参考：[TMDB 电影搜索](https://developer.themoviedb.org/reference/search-movie)、[电影详情](https://developer.themoviedb.org/reference/movie-details)、[AList Quark 驱动源码](https://github.com/AlistGo/alist/blob/main/drivers/quark_uc/driver.go)。执行器直接调用网盘接口，AList 仅提供已授权挂载的凭据引用。
