# 元数据来源与预计日程

元数据查询使用 TMDB 或 TVmaze。新任务自动选源时，有 TMDB 凭据则优先使用 TMDB，否则使用 TVmaze；已确认任务和订阅保留主来源及作品 ID，不因凭据新增或接口失败切换身份。

| 来源 | 用途 | 接入方式 |
| --- | --- | --- |
| [TMDB](https://developer.themoviedb.org/reference/tv-season-details) | 标题/原名、年份、作品 ID、季集、预计日期及外部 ID | catalog.py / updates.py；需要 API 凭据 |
| [TVmaze](https://www.tvmaze.com/api) | 别名、节目与季、单集预计日期/时刻及外部 ID | catalog.py / updates.py；公开只读接口 |
| [Trakt](https://developer.trakt.tv/docs/about-calendars) | 可选日历与观看进度 | 未接入 |

作品通过精确标题或已确认别名及年份核验，搜索排序不作为身份依据。身份绑定记录来源与作品 ID；别名检查不完整或候选有歧义时不确认唯一匹配。

订阅可配置补充来源。关联须至少有一个相同的 IMDb 或 TVDB 外部 ID，且所有共同存在的外部 ID 一致；标题相近不构成关联依据。关联后按同一季集号核对，日程、季集集合或总数冲突会阻断自动转存，并保留双方证据。补充来源普通网络故障可降级使用主来源；认证失败或限流停止自动执行。

`air_date` 仅有日期时记录为预计日期，不补造时刻；可靠 `airstamp` 保留时区，并转换至 `Asia/Shanghai` 对照。同日有效时刻可细化日期，但不消除日期冲突。未知日期保持未知，不计为已播缺集。

三类证据分别记录：元数据给出预计日程，分享目录枚举证明资源可用，影库真实目录证明已有库存。预计时间到达仅触发搜索，不证明已经播出或网盘已有文件；转存计划须以实际枚举并匹配的缺集为依据。

订阅授权范围、执行条件与通知规则见[订阅与周期检查](headless-design.md#订阅与周期检查)。Codex heartbeat 使用 `watch run --subscription-id ID --force --apply` 执行指定订阅，批量到期检查使用 `watch run --due --apply`。具体关联与真实验收记录见[验证报告](test-report.md)。

TVmaze 数据须按[API 许可说明](https://www.tvmaze.com/api#licensing)署名。TMDB 凭据及署名要求见[官方认证](https://developer.themoviedb.org/docs/authentication-application)和[FAQ](https://developer.themoviedb.org/docs/faq)，本项目录入方式见[凭据配置](credentials.md)。[TMDB Changes](https://developer.themoviedb.org/docs/tracking-content-changes)反映元数据字段变化，不代表网盘新增资源。

特别篇、综艺上下期、绝对集号、多集容器、延播及源间冲突须单独核对，不提供通用自动映射。Infuse 负责播放端刮削，本项目不维护海报库。
