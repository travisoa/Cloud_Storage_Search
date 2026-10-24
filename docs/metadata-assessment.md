# 元数据来源与预计日程

元数据查询使用 TMDB 或 TVmaze。新任务自动选源时，有 TMDB 凭据则优先使用 TMDB，否则使用 TVmaze；已确认任务和订阅保留主来源及作品 ID，不因凭据新增或接口失败切换身份。以 TMDB 为主来源的订阅可由 Trakt 补齐单集日期与时刻，Trakt 不作为身份来源。

| 来源 | 用途 | 接入方式 |
| --- | --- | --- |
| [TMDB](https://developer.themoviedb.org/reference/tv-season-details) | 标题/原名、年份、作品 ID、季集、预计日期及外部 ID | catalog.py / updates.py；需要 API 凭据 |
| [TVmaze](https://www.tvmaze.com/api) | 别名、节目与季、单集预计日期/时刻及外部 ID | catalog.py / updates.py；公开只读接口 |
| [Trakt](https://developer.trakt.tv/) | TMDB 缺少的单集日期与时刻、作品常规播出时刻 | catalog.py / schedules.py；需要 Client ID；仅补缺 |

作品通过精确标题或已确认别名及年份核验，搜索排序不作为身份依据。身份绑定记录来源与作品 ID；别名检查不完整或候选有歧义时不确认唯一匹配。

订阅可配置补充来源。关联须至少有一个相同的 IMDb 或 TVDB 外部 ID，且所有共同存在的外部 ID 一致；标题相近不构成关联依据。关联后按同一季集号核对，日程、季集集合或总数冲突会阻断自动转存，并保留双方证据。补充来源普通网络故障可降级使用主来源；该来源或交叉核对本身出现过冲突时除外，须该来源重新读取成功（无论是否关联）后才恢复，期间订阅保持待确认；仅主来源自身的数据问题（如重复集号）修正后即可恢复。认证失败或限流停止自动执行。

Trakt 补缺与上述补充来源分开：运行 `configure-trakt` 录入 Client ID 后（同时把配置 `trakt_schedule_fill` 设为 `true`），自动作用于 TMDB 主来源的订阅。关联用 TMDB 作品 ID 查找（`/search/tmdb/{id}`），结果须唯一且 Trakt 记录的 TMDB ID 相同；两边都有 IMDb 或 TVDB ID 时须一致，否则该订阅不使用 Trakt。合并以 TMDB 为准：日期相同时采用 Trakt 时刻；TMDB 无日期时采用 Trakt 日期与时刻；两边日期不同时取较早者。日期或集数差异只记入证据，不阻断自动转存，因为日程只决定何时搜索。Trakt 的 UTC 时刻按其作品时区换算为当地日期，时区缺失时使用配置 `timezone`。Trakt 网络错误、认证失败或限流只影响补缺，限流期间按 `Retry-After` 暂停请求。

订阅日程缓存于 `runtime/schedules/<订阅 ID>.json`，TMDB 字段与 Trakt 字段分开保存，不含凭据或完整接口响应。首次检查拉取并保存，之后每天最多刷新一次；作品已完结且各集日期均已过去时不再自动刷新，`watch run --refresh-schedule` 可立即刷新。主来源、作品 ID、季或实际生效的补充来源变化时缓存作废。刷新时普通网络失败继续使用旧缓存；认证、限流或元数据冲突仍按原规则阻断，且缓存在下次成功读取前不再使用，`watch resume --clear-block` 的只读核对也总是重新读取日程。仅在仍有未播出或日期未知的集时请求 Trakt。关闭 `trakt_schedule_fill` 或删除凭据后，缓存中的 Trakt 数据在下一轮检查时清除。下次检查时间与唤醒计划见[订阅与周期检查](headless-design.md#订阅与周期检查)。

`air_date` 仅有日期时记录为预计日期，不补造时刻；可靠 `airstamp` 保留时区，并转换至 `Asia/Shanghai` 对照。同日有效时刻可细化日期，但不消除日期冲突。未知日期保持未知，不计为已播缺集。

三类证据分别记录：元数据给出预计日程，分享目录枚举证明资源可用，影库真实目录证明已有库存。预计时间到达仅触发搜索，不证明已经播出或网盘已有文件；转存计划须以实际枚举并匹配的缺集为依据。

订阅授权范围、执行条件与通知规则见[订阅与周期检查](headless-design.md#订阅与周期检查)。Codex heartbeat 使用 `watch run --subscription-id ID --force --apply` 执行指定订阅，批量到期检查使用 `watch run --due --apply`。具体关联与真实验收记录见[验证报告](test-report.md)。

TVmaze 数据须按[API 许可说明](https://www.tvmaze.com/api#licensing)署名。TMDB 凭据及署名要求见[官方认证](https://developer.themoviedb.org/docs/authentication-application)和[FAQ](https://developer.themoviedb.org/docs/faq)，本项目录入方式见[凭据配置](credentials.md)。Trakt 数据按其 [API 使用政策](https://developer.trakt.tv/docs/api-use-policy)使用：仅读公开数据、按订阅缓存；本项目使用 Trakt，但未获 Trakt 认可或认证。Trakt 应用连续 30 天无调用会被自动删除，届时需重建应用并重新运行 `configure-trakt`。[TMDB Changes](https://developer.themoviedb.org/docs/tracking-content-changes)反映元数据字段变化，不代表网盘新增资源。

特别篇、综艺上下期、绝对集号、多集容器、延播及源间冲突须单独核对，不提供通用自动映射。Infuse 负责播放端刮削，本项目不维护海报库。
