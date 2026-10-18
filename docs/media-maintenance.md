# 影库维护

入口为 `scripts/media.py`。匹配、策略设置、体检、压缩包检查及 `--plan` 预览不写云盘；维护命令的转存、解压、入库和改名须在已授权范围内使用 `--apply`。通用执行与恢复约定见[执行约定](headless-design.md)。

## TMDB 精确匹配

```bash
python3 scripts/media.py match --kind tv --title '片名' --year 2026
python3 scripts/media.py match --kind movie --tmdb-id https://www.themoviedb.org/movie/作品ID
python3 scripts/media.py match --task-id TASK_ID --tmdb-id https://www.themoviedb.org/tv/作品ID --bind
```

支持 TMDB 数字 ID、同类作品的 HTTPS 详情链接及 `--imdb-id` / `--tvdb-id`。标题搜索须提供年份；完整获取有界搜索结果后，核验同一 ID 的原名、别名、译名和外部 ID，搜索排序与模糊相似度不作为确认依据。同名候选、年份或外部 ID 冲突、结果不完整均返回待处理状态。

`--task-id` 同时核验已确认任务的身份。`--bind` 仅保存通过实时交叉核验的 TMDB 关联，保留原主身份、任务及订阅 ID。追更使用关联补充预计日程，仍须核对两源公共 ID 与季集冲突，详见[元数据来源](metadata-assessment.md)。结果保存在 `runtime/reports/match-latest.json`，凭据通过[配置命令](credentials.md#配置命令)录入。

## 影库体检

```bash
python3 scripts/media.py health
python3 scripts/media.py health --folder-id FOLDER_ID --metadata
python3 scripts/media.py health --library-id LIBRARY_ID
```

默认扫描所选影库的目录、文件名、大小及季集，检查前缀差异、未知编号、同集多版本、孤立字幕、空目录、零字节和压缩分卷缺号等。年份或名称告警仅表示规范化问题，不证明文件损坏或 Infuse 识别失败；数字片名不视为发行年份。

`--metadata` 使用已配置元数据核对唯一作品和预计季集；元数据不可用或身份不唯一时保留未完成状态。预计缺集、未来日程与真实库存分别记录，不代表资源已发布。体检仅读取目录属性与可选元数据，不检查媒体字节完整性或 Infuse 播放。

默认读取上限为 10,000 个条目、12 层；扫描不完整时不报告健康。子目录检查先按目录列表定位并核验影库归属（优先打开分类及地区、年份分组目录），再只扫描该目录；隔离测试影库自动排除。终端默认最多展示 20 条问题，`--details` 显示完整结果；JSON 和 Markdown 报告分别保存至 `runtime/reports/health-latest.json`、`runtime/reports/health-latest.md`。体检不自动修复文件。

## 按剧设置版本策略

影库预设见[版本预设](library-bootstrap.md#版本预设)，作品级策略优先于影库预设。`--prefer-size smaller|larger` 在同分辨率版本之间按体积排序。

```bash
python3 scripts/media.py policy show --task-id TASK_ID
python3 scripts/media.py policy set --task-id TASK_ID --resolution 2160p --no-fallback --archives manual
python3 scripts/media.py policy set --task-id TASK_ID --resolution 2160p --resolution 1080p --allow-fallback --codec HEVC --prefer-codec --hdr prefer --max-size-gb 15
python3 scripts/media.py policy reset --task-id TASK_ID
```

`--profile-key` 可指定已确认作品，新影库任务须同时指定 `--library-id`。策略按稳定作品 ID 和年份隔离，后续请求、任务恢复及追更均读取最新设置。重置后恢复原已确认画质偏好，不从旧快照恢复已删除策略。

分辨率按重复 `--resolution` 的顺序选择，仅 `--allow-fallback` 允许回退至其他已列分辨率。编码可设为偏好或由 `--require-codec` 限定；HDR 支持 `any / prefer / exclude / require / dolby-vision`，并可附加发布组关键词及文件大小约束。先比较分辨率；追更在同分辨率下优先已选来源，再比较次要偏好，等价结果保持待确认。

策略使用[有来源依据的媒体标签](headless-design.md#命名与媒体信息)，缺失标签不满足特定编码或 HDR 要求。策略仅影响待补资源，不替换、删除或升级已存文件，不扩大追更的作品、季或数字集号授权；已有剧集替换须使用下述专用参数。

## 压缩包检查与隔离解压

```bash
python3 scripts/media.py archive inspect --folder-id FOLDER_ID
python3 scripts/media.py archive extract --fid ARCHIVE_FID --parent-id SOURCE_PARENT --plan
python3 scripts/media.py archive extract --fid ARCHIVE_FID --parent-id SOURCE_PARENT --apply
python3 scripts/media.py archive extract --fid ARCHIVE_FID --parent-id SOURCE_PARENT --reconcile
python3 scripts/media.py archive cleanup --fid ARCHIVE_FID --plan
python3 scripts/media.py archive cleanup --fid ARCHIVE_FID --apply
```

搜索优先选择直接视频。压缩包仅记录候选、格式及分卷线索；`skip` 跳过，`manual` 保留显式选择提示，两者均不自动转存或解压分享中的压缩包。包名不作为包内季集与画质的证据。

对影库内明确选定且已获解压授权的单包，提交前核对源 fid、名称、大小及目录范围，仅写入该任务新建的空目录 `解压待整理_…`。记录任务 ID，核验返回的输出与该目录实际内容一致，并递归读取该目录；不遍历影库其他目录。解压完成仅表示输出核验通过，入库仍按扫描、身份匹配和导入计划执行。

分卷（含 ZIP + Z01）、缺卷、加密或密码、未知格式及会员要求保留具体待处理原因，不自动输入密码、本地解压或推断分卷完整性。认证和风控按[执行范围](headless-design.md#执行范围)处理。无任务 ID 的未知提交不重发；有任务 ID 时通过 `--reconcile` 查询状态与实际输出，不因更换目录或嵌套影库重复提交。隔离测试影库仅允许检查和预览。

解压残留按 `config/headless.json` 的 `archive_cleanup` 处理，回收均指移入夸克回收站，不清空回收站：

| 取值 | 解压核验通过后 | `organize` 完成后 |
| --- | --- | --- |
| `keep`（未设置时的默认值） | 全部保留 | 全部保留 |
| `recycle` | 回收原压缩包；`fetch` 创建的 `压缩包待解压_…` 随后为空时一并回收 | `解压待整理_…` 已无文件时回收 |
| `ask` | 全部保留，报告中 `cleanup.source.state` 为 `awaiting_choice` | 有可回收项时报告 `cleanup.choices` |

`ask` 模式下 `choices.recycle` 给出的 `archive cleanup` 命令须向用户确认后再执行。`--remove-archive` / `--keep-archive` 仅覆盖本次解压。`archive cleanup` 按原包、转存暂存目录、解压目录的顺序处理已核验解压任务的残留，不要求输出仍在原处：原包的 fid、名称、大小与目录须与解压记录一致；目录须仍为该任务创建（名称与 fid 一致）；暂存目录须在原包确认已回收或不存在后为空，原包结果未知时保留，解压目录不能有文件（可留空子目录），否则保留并说明原因。已被手动删除的项目报告为 `not_found` / `absent`。每项回收意图在请求前落盘，结果未知时只核对真实目录，不重发请求。

执行记录保存在私有 `runtime/archive-jobs.sqlite3`，最新报告为 `runtime/reports/archive-latest.json`。模拟验证与真实操作验收范围见[验证报告](test-report.md)；实际可用格式及账号权益以所选源包的服务端结果为依据。

## 替换已有剧集的版本

```bash
python3 scripts/media.py sync --title 剧名 --year 2026 --season 1 --series-id ID --metadata-source tvmaze \
  --replace-episode S01E02 --replace-episode S01E03 --plan
python3 scripts/media.py sync --title 剧名 --year 2026 --season 1 --series-id ID --metadata-source tvmaze \
  --replace-episode S01E02 --replace-episode S01E03 --remove-replaced --apply
```

普通请求仅补缺集；`--replace-episode` 为指定的已有剧集重新选源。每集在目标季目录中须恰有一个旧文件，新版本须有分辨率标签，选择遵循作品级策略或影库预设，不与旧文件比较画质。替换任务仅处理指定剧集，其他缺集列入 `missing_not_in_scope`。新文件使用[统一命名规则](headless-design.md#命名与媒体信息)，旧文件默认保留。

显式授权 `--remove-replaced` 后，仅在新版本转存、改名及读回核验完成时，将记录中的旧 fid 移入夸克回收站，不清空回收站。回收时遇到认证或风控立即停止全部写入；回收结果未知时仅核对真实目录，不重发请求。重复执行同一请求仅恢复未完成回收，不重新替换已转存并核验的新版本；转存前中止、未保存任何新版本的请求会重新规划。部分剧集未保存时返回 `replacement_partially_completed_rerun_pending_episodes` 及待处理剧集，需另行只指定这些剧集。

## 从搜索结果转存压缩包并入库

搜索仅返回压缩包时，`archive_candidates` 列出候选 ID。选定候选后使用：

```bash
python3 scripts/media.py archive fetch TASK_ID --candidate-id 候选ID --plan
python3 scripts/media.py archive fetch TASK_ID --candidate-id 候选ID --apply --extract
python3 scripts/media.py archive plan --fid 压缩包FID
python3 scripts/media.py archive organize PLAN_ID --plan
python3 scripts/media.py archive organize PLAN_ID --apply
```

`fetch` 仅接受单文件、受支持格式且分享无提取码的候选；带密码的分享不进入任务快照。执行前重新枚举分享并核对 fid、名称及大小，在影库根目录下新建空目录 `压缩包待解压_…`，单次提交转存并持久化任务 ID。结果未知时通过 `--reconcile` 按任务 ID 与实际文件对账，不重复提交。`--extract` 在转存核验通过后执行隔离解压，原包处理同上，也接受 `--remove-archive` / `--keep-archive`。

`archive plan` 仅读取已核验完成（`EXTRACTED`）的解压输出，按[可选扫描与整理](library-bootstrap.md#可选扫描与整理)识别身份并生成计划。`organize` 执行已授权计划，将视频和关联字幕移动至对应分类并命名；未识别文件保留原位，原压缩包及暂存目录按 `archive_cleanup` 处理。

## 单个作品目录原地规范命名

```bash
python3 scripts/media.py tidy plan --folder-id 作品目录ID [--kind tv|movie]
python3 scripts/media.py tidy apply PLAN_ID          # 只读复核
python3 scripts/media.py tidy apply PLAN_ID --apply  # 用户授权后执行
```

用于处理体检报告中的文件与目录前缀差异，按[统一命名规则](headless-design.md#命名与媒体信息)生成计划，不改作品目录名。电视剧使用文件名季集号，季目录仅补充缺失信息，冲突保留待处理；电影要求作品目录顶层仅有一个正片。字幕仅在同目录唯一关联视频时同步命名。未知编号、同集多版本、目标重名、花絮及季目录外视频均保留原位并列入待处理；分类目录、地区子分类及包含多个带年份作品的目录不接受整理。

执行时按计划记录的目录链逐级复核，并复核文件 fid、名称、大小及重名冲突；每次改名前持久化意图，改名后读回核验。结果未知时，下一次运行仅对账：已为目标名则记录完成，仍为原名则记录未生效，后续 `--apply` 才继续执行。计划保存在私有 `runtime/tidy-plans/`。目录缺少年份等元数据问题不在此命令范围内。
