# 精确匹配、体检、版本策略、压缩包与原地整理

这些功能通过 `scripts/media.py` 在后台使用。匹配、策略设置、体检、压缩包检查及各类 `--plan` 预览不写云盘；转存、解压、入库和改名都要求对应命令带 `--apply`。

## TMDB 精确匹配

```bash
python3 scripts/media.py match --kind tv --title '片名' --year 2026
python3 scripts/media.py match --kind movie --tmdb-id https://www.themoviedb.org/movie/作品ID
python3 scripts/media.py match --task-id TASK_ID --tmdb-id https://www.themoviedb.org/tv/作品ID --bind
```

可按 TMDB 数字 ID、同类作品的 HTTPS 详情链接，或 `--imdb-id` / `--tvdb-id` 查找。标题搜索须提供年份；先取完整的有界搜索结果，再核同一 ID 的原名、别名、译名和外部 ID，不按第一条或模糊相似度自动确认。多部同名、年份冲突、外部 ID 矛盾或结果未完整取得时返回待处理状态。

`--task-id` 额外核验已确认任务的身份。`--bind` 只保存通过实时交叉核验的 TMDB 关联，不改变 TVmaze 等原主身份、任务或订阅 ID。追更使用该关联补充预计日程，仍须检查两源公共 ID 及季集冲突。结果保存在 `runtime/reports/match-latest.json`。TMDB 凭据继续通过 `configure-tmdb` 隐藏输入，不放在命令或聊天中。

## 影库体检

```bash
python3 scripts/media.py health
python3 scripts/media.py health --folder-id FOLDER_ID --metadata
python3 scripts/media.py health --library-id LIBRARY_ID
```

默认扫描当前影库的目录、文件名、大小及季集，检查目录和文件前缀、无法识别的编号、多版本同集、孤立字幕、空目录、零字节、压缩分卷缺号等。年份或名称告警是规范化线索，不能单凭告警断言文件损坏或 Infuse 无法识别。数字片名不会被当作发行年份。

`--metadata` 可进一步使用已配置元数据核对唯一作品和预计季集；元数据不可用或身份不唯一会保留未完成状态。预计缺集、未来日程与真实库存分别记录，不表示资源已发布。未执行媒体下载、码流完整性或 Infuse 播放验证。

读取限额默认 10,000 个条目、12 层，扫描不完整不会报告健康。检查子目录也先证明其位于影库内，隔离测试影库自动排除。终端最多展示20条问题，`--details` 可查看完整输出；完整 JSON 和 Markdown 始终写入 `runtime/reports/health-latest.json`、`runtime/reports/health-latest.md`，不自动修复旧文件。

## 按剧设置版本策略

影库整体的三档预设见[建库与导入说明](library-bootstrap.md#版本预设)；这里的单剧规则优先于预设。`--prefer-size smaller|larger` 可在同档版本之间按体积取舍。

```bash
python3 scripts/media.py policy show --task-id TASK_ID
python3 scripts/media.py policy set --task-id TASK_ID --resolution 2160p --no-fallback --archives manual
python3 scripts/media.py policy set --task-id TASK_ID --resolution 2160p --resolution 1080p --allow-fallback --codec HEVC --prefer-codec --hdr prefer --max-size-gb 15
python3 scripts/media.py policy reset --task-id TASK_ID
```

也可用 `--profile-key` 选择已确认的作品；新影库任务需同时指定 `--library-id`。策略按稳定作品 ID 和年份隔离，后续请求、任务恢复和追更每次读取最新设置。重置后回到原已确认的画质偏好，不从旧快照恢复已删除的策略。

分辨率按重复 `--resolution` 的顺序选择；只有 `--allow-fallback` 才允许其他已列分辨率。编码可偏好或用 `--require-codec` 限定；HDR 可选 `any / prefer / exclude / require / dolby-vision`，还可用发布组关键字和文件大小约束。选择先比较分辨率；同分辨率追更优先原已选来源，再比较次要偏好，等价结果保留待确认。

所有媒体信息都保留文件名或父目录的标签依据，未探测真实码流。缺少 HDR 标签不证明 SDR，要求特定编码或 HDR 而没有依据时不通过。设置只影响待补集，不替换、删除或升级已存文件，也不扩大自动追更的作品、季和数字编号授权。

## 压缩包检查与隔离解压

```bash
python3 scripts/media.py archive inspect --folder-id FOLDER_ID
python3 scripts/media.py archive extract --fid ARCHIVE_FID --parent-id SOURCE_PARENT --plan
python3 scripts/media.py archive extract --fid ARCHIVE_FID --parent-id SOURCE_PARENT --apply
python3 scripts/media.py archive extract --fid ARCHIVE_FID --parent-id SOURCE_PARENT --reconcile
```

搜索优先直接视频。遇到压缩包会记录候选、格式及分卷线索；策略 `skip` 跳过，`manual` 保留显式选择提示。两者均不自动转存分享中的压缩包或解压，不能用包名推断包内季集及画质。

对影库内已经存在且明确选中的单包，可预览后在一次独立请求中执行夸克云解压。提交前核对源 fid、名称、大小和真实目录范围，只写入本次创建的空目录 `解压待整理_…`。保留原包，记录任务 ID，递归核验输出都属于该目录且不是影库旧 fid。完成仅表示待整理输出已核验，之后仍通过扫描、身份匹配和导入计划决定入库。

分卷（含 ZIP + Z01）、缺卷、加密/密码、未知格式或会员要求保留明确待处理原因；当前不自动输入密码、下载本地解压或猜测分卷完整性。认证和风控立即停止。无任务 ID 的未知提交不重发；已有任务 ID 时 `--reconcile` 只查询状态和真实输出。更改目录或切换嵌套影库不能绕过这条保护。隔离测试影库仅允许检查和预览。

运行记录在私有 `runtime/archive-jobs.sqlite3`，最新结果为 `runtime/reports/archive-latest.json`。当前云解压端点及恢复路径通过模拟服务验证，尚未执行真实压缩包解压；实际可用格式、账号权益与服务端行为仍需对用户选定包验收。

## 从搜索结果转存压缩包并入库

搜索只找到压缩包时，任务报告的 `archive_candidates` 列出候选 ID。用户选定后：

```bash
python3 scripts/media.py archive fetch TASK_ID --candidate-id 候选ID --plan
python3 scripts/media.py archive fetch TASK_ID --candidate-id 候选ID --apply --extract
python3 scripts/media.py archive plan --fid 压缩包FID
python3 scripts/media.py archive organize PLAN_ID --plan
python3 scripts/media.py archive organize PLAN_ID --apply
```

`fetch` 只接受单文件、受支持格式的候选，且分享须无提取码（带密码的分享不进入任务快照）。执行前重新枚举分享并核对 fid、名称和大小，在影库根目录下新建空的 `压缩包待解压_…`，单次提交转存；任务 ID 先落盘，结果未知时 `--reconcile` 只按任务 ID 和真实文件对账，不重发。`--extract` 在核验转存后接着执行上节的隔离解压。

`archive plan` 只读取已核验完成（EXTRACTED）的解压输出，按“可选扫描与整理”的同一规则识别电影/电视剧身份并生成计划；`organize` 执行该计划，把视频和关联字幕移动并命名进电影或电视剧分类。原压缩包、暂存目录和未识别文件保留原位，不删除。

## 单个作品目录原地规范命名

```bash
python3 scripts/media.py tidy plan --folder-id 作品目录ID [--kind tv|movie]
python3 scripts/media.py tidy apply PLAN_ID          # 只读复核
python3 scripts/media.py tidy apply PLAN_ID --apply  # 用户授权后执行
```

用于体检报告中的“文件与目录前缀差异”。前缀取真实作品目录名，不改目录名；电视剧按文件名季集号（季目录号仅作补充，冲突则保留），电影要求目录顶层只有一个正片。字幕仅在同目录唯一关联视频时跟随改名。无法识别的编号、同集多版本、目标重名、花絮及季目录以外的视频都保留原样并列入待处理。分类目录、地区子分类或含多个带年份作品的目录会被拒绝。

执行时先重核目录链、文件 fid、名称、大小与目标重名，每次改名前写意图，改名后读回核验。结果未知时下一次运行只读观察：已是新名记为完成，仍是原名记为未生效，之后的 `--apply` 才会继续。计划保存在私有 `runtime/tidy-plans/`。目录缺少年份等需要元数据的问题不在此命令范围内。
