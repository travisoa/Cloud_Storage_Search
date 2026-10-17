# 季集更新来源

当前后台实现使用 TMDB 或 TVmaze，不依赖 QASX 的日历或网页。新任务在自动选择来源时，有 TMDB 凭据则优先使用，否则可用 TVmaze；已确认的任务和订阅继续保留主来源及作品 ID，不因后来配置 TMDB 或接口失败切换身份。

| 来源 | 用途 | 当前接入 |
| --- | --- | --- |
| [TMDB](https://developer.themoviedb.org/reference/tv-season-details) | 标题/原名、年份、作品 ID、季集、预计日期及外部 ID | catalog.py / updates.py；需要本机 API 凭据 |
| [TVmaze](https://www.tvmaze.com/api) | 别名、节目与季、单集预计日期/时刻及外部 ID | catalog.py / updates.py；公开只读接口 |
| [Trakt](https://developer.trakt.tv/docs/about-calendars) | 可选的日历/观看进度 | 未接入，不增加当前依赖 |

作品通过标题/别名与年份确认，候选有歧义不采用第一条。身份绑定保存来源与作品ID；未知或未完整检查的别名不能伪装成唯一匹配。

订阅可配置第二来源。补充来源须与主来源至少有一个相同 IMDb 或 TVDB 外部 ID，且共同存在的外部 ID 不得互相矛盾；标题相近不构成关联证明。两源关联后按同一季集号核对，日程、季集集合或季集总数冲突会阻断自动转存，报告保留双方依据。补充来源普通网络不可用时可带降级状态保留主来源；认证失败或限流停止自动执行。

`air_date` 只有日期时，记录为预计日期，不拼造精确时刻；有可靠 `airstamp` 时保留时区，转换到 Asia/Shanghai 进行对照。同一天的有效时刻可细化日期信息，不能据此忽略日期冲突。日程到点只产生可能需搜索的集数，不证明已经播出，更不证明网盘有文件。只有实际枚举分享文件后才可规划转存，已有资源以真实目录文件为依据；未知日期保留未知，不作为已播缺集。

首批《白色黑手党时代》S01 订阅沿用 TVmaze 主身份，并以经外部 ID 核验的 TMDB 身份补充更新信息。用户已授权每 6 小时检查，沿用已选 4K 来源，明确缺集与版本后自动补存及命名。Codex heartbeat 每次指定该订阅调用 `watch run --subscription-id ID --force --apply`；`--force` 只提前检查，不绕过暂停、写入授权或阻断。批量到期检查也可用 `watch run --due --apply`，订阅之外的作品不自动纳入。无变化不通知，身份或日程冲突由对话处理，详见[订阅执行约定](headless-design.md#订阅与周期检查)。

TVmaze数据需要按其[API许可说明](https://www.tvmaze.com/api#licensing)署名；TMDB凭据与署名要求见[官方认证](https://developer.themoviedb.org/docs/authentication-application)和[FAQ](https://developer.themoviedb.org/docs/faq)。TMDB的[Changes](https://developer.themoviedb.org/docs/tracking-content-changes)记录元数据字段变化，不能当作网盘新资源通知。

特别篇、综艺上下期、绝对集号、多集容器、延播及源间冲突需要单独核对；当前不承诺通用自动映射。Infuse继续负责播放端刮削，本项目不重建海报库。
