# 后台网盘影库助手

你在对话中说“帮我查下《嫁金钗》，存到我的夸克并重命名”，助手在后台核对影库已有资源、查询预计季集、搜索缺集、转存并规范命名。全程不打开网页，Infuse 继续使用原有 AList 链路。

## 当前能力

- TMDB 精确匹配支持 ID/详情链接、年份、同 ID 别名及公共 ID 交叉核验；TVmaze 原主身份保留，区分预计播出、未来和时间未知。
- 读取真实夸克目录，复用原剧目录和分类，只补明确缺失集。
- 电影按 TMDB 片名+年份或 ID 精确确认，在电影分类核对是否已有，再搜索、转存唯一版本并命名为 `片名 (年份) [标签].扩展名`；同级版本交由对话选择。
- PanSou 后台搜索，实际枚举分享文件后选择来源；按剧保存分辨率、回退、编码/HDR、大小及发布标签策略。同级版本或纯数字集号经对话确认后继续同一任务。
- 作品目录与视频文件共用同一剧名前缀，季集号后可保留分辨率、编码及 HDR 标签，字幕同步命名。来源标签有据可查，不冒充媒体探测。
- 转存提交后立即保存任务 ID；支持只读对账及恢复，不重复提交结果未知的转存。
- 按作品和季订阅追更，复用确认过的分享目录，结合更新日程与真实库存补缺；支持暂停、只读检查与异常阻断。
- 新网盘可先创建影库及五类目录，再选择是否扫描、匹配作品和按保存计划导入；初始化不会自动整理已有资源。
- 只读影库体检，报告命名、季集、多版本、字幕及压缩分卷问题，可选核对元数据预计日程。
- 压缩包全链路均须显式指令：搜索中选定的单文件压缩包可转存到隔离目录，云解压后核验输出，再生成普通入库计划整理进电影/电视剧分类。分卷与带密码的包仍需人工处理。
- 单个作品目录可原地规范命名：视频统一用真实目录名作前缀，字幕跟随；只改名不移动，歧义、重复版本与冲突保留原样。
- 默认不改名或移动旧资源；整理须先生成计划，再经用户明确授权执行该计划。不覆盖、不删除，未知写入结果不自动重发。

当前真实联调状态和未完成项见 [验证报告](docs/test-report.md)。

精确匹配、体检、版本策略及压缩包命令见[影库维护说明](docs/media-maintenance.md)。

## 对话任务入口

以下命令供助手在后台调用，不需要用户打开网页或填写表单：

```bash
python3 scripts/media.py request '帮我查下《嫁金钗》，存到我的夸克网盘并重命名'
python3 scripts/media.py request '只查询《嫁金钗》，不要转存' --plan
python3 scripts/media.py doctor
```

明确请求保存时 `request` 可执行；否定写入、预览或 `--plan` 只做查询和计划。请求中出现“电影”时按电影处理，也可用 `--kind movie|tv` 指定；电影需要年份或 TMDB ID，例如 `request '帮我找电影《沙丘2》 2024，存到夸克'`。作品或目录不确定时返回结构化待确认项，由对话解决，不自动采用第一条搜索结果。

命名示例：`剧名 (2026)/Season 01/剧名 (2026) S01E01 [2160p].mkv`。已有作品优先沿用真实作品目录名；缺少媒体信息时不添加猜测标签。统一命名有助于识别，封面仍取决于 Infuse 的元数据匹配与刷新，不能以云盘改名成功代替播放器验收。

已确认作品也可调用 `sync --title ... --year ... --season ... --series-id ... --metadata-source tvmaze --apply`。影库根目录与凭据来源在 [config/headless.json](config/headless.json)，目录 ID 与 AList 展示路径不混用。

## 新网盘建库

新夸克账号可引用其已有 AList 挂载，在指定位置创建 `影库/电影、电视剧、动漫、纪录片、综艺`。完成后可直接结束，也可选择扫描已有资源并预览整理方案；不会切换当前影库或追更。

```bash
python3 scripts/media.py library drives
python3 scripts/media.py library connect --drive-id second-quark --alist-storage-id 2
python3 scripts/media.py library init --drive-id second-quark --library-id family --parent-id 0 --name 影库 --version-preset balanced --plan
```

建库时可选影库版本预设：`quality`（品质优先）、`balanced`（兼顾品质与存储）、`storage`（节省存储），之后用 `library preset [影库ID] --set ...` 修改，不带影库ID时作用于当前默认影库。预设只影响之后的待补资源，不替换已存文件。

实际执行加 `--apply`；可选扫描、整理和新影库请求见[建库与导入说明](docs/library-bootstrap.md)。本轮已在当前网盘的隔离测试目录验证创建结构与3集扫描预览，未迁移旧资源。其他网盘写入仍需 Provider 适配。

## 同一任务的确认与恢复

正式入口返回本地 `task_id`，助手用它传递用户确认和继续执行，无需创建单剧脚本：

```bash
python3 scripts/media.py status TASK_ID
python3 scripts/media.py confirm TASK_ID --quality 2160p --numeric-episodes --remember-quality --apply
python3 scripts/media.py reconcile TASK_ID
python3 scripts/media.py resume TASK_ID --apply
python3 scripts/media.py normalize-saved TASK_ID --apply
```

电影任务用 `confirm TASK_ID --selection '{"movie":"候选ID"}' --apply` 或 `--quality 2160p` 选定版本。`confirm` 从本任务已枚举的候选快照选择，执行前重新核验真实文件名、大小和来源。`--numeric-episodes` 只表示本次明确确认的数字集号映射。`reconcile` 只读取任务状态与目录；`resume`、`confirm`、`normalize-saved` 不带 `--apply` 时均不写云盘。命名修正还须同时通过原转存凭据、日志和真实新 fid 的核对。

中文名未被元数据收录时，助手核实来源后用 `bind-series` 保存作品 ID、年份、远端原名、中文别名及 HTTPS 依据；每次复用仍检查远端身份。画质偏好只作用于绑定作品。详见[执行约定](docs/headless-design.md)。

## 压缩包与原地整理

```bash
python3 scripts/media.py archive fetch TASK_ID --candidate-id 候选ID --plan
python3 scripts/media.py archive fetch TASK_ID --candidate-id 候选ID --apply --extract
python3 scripts/media.py archive plan --fid 压缩包FID
python3 scripts/media.py archive organize PLAN_ID --apply
python3 scripts/media.py tidy plan --folder-id 作品目录ID
python3 scripts/media.py tidy apply PLAN_ID --apply
```

每一步不带 `--apply` 时只读。边界与恢复方式见[影库维护说明](docs/media-maintenance.md)。

## 智能追更

订阅从已确认任务导入作品身份、影库目录和来源。用户已授权的首批范围为《白色黑手党时代》第 1 季：每 6 小时检查，优先沿用已选 4K 分享目录，有明确缺集和可用文件时自动转存并命名，无变化不通知。周期唤醒通过 Codex heartbeat 启用；后台服务命令本身不创建调度。

```bash
python3 scripts/media.py watch add --task-id TASK_ID --interval-hours 6 --auto-save
python3 scripts/media.py watch list
python3 scripts/media.py watch status SUBSCRIPTION_ID
python3 scripts/media.py watch run --subscription-id SUBSCRIPTION_ID --force --plan
python3 scripts/media.py watch run --due --apply
python3 scripts/media.py watch pause SUBSCRIPTION_ID
python3 scripts/media.py watch resume SUBSCRIPTION_ID
```

`watch run` 默认只读；云盘写入同时要求订阅已获 `--auto-save` 授权、执行带 `--apply` 且未阻断。`--plan` 始终只读。`resume` 恢复检查不会自动解除异常阻断；`resume --clear-block` 先只读对账，满足恢复条件后才解除，后续执行仍遵循写入权限。

元数据只表示预计更新；分享目录真实存在的视频才可转存。已绑定的 TVmaze 身份保留为主来源，TMDB 经外部作品 ID 核验后补充；季集或日期冲突暂停自动执行。已确认来源的纯数字集号可通过 `--numeric-source` 固定到同一分享目录、季和画质；新来源或版本歧义仍交由对话确认。配置与完整执行边界见[当前设计](docs/headless-design.md)。

## 凭据

两种来源，按网盘连接分别选择：

- **引用 AList 挂载**：`configure-alist`（默认影库）或 `library connect --alist-storage-id N`。运行时从 AList 数据库读出 Cookie，只在内存中使用，AList 里更新后自动跟上。依赖本机 Apple Container 中的 `alist` 容器。
- **私有凭据文件**（macOS 与 Linux 通用）：`configure-quark`（默认影库）或 `library connect --drive-id X --cookie`；过期后用 `library credential --drive-id X` 替换，替换前核对仍能看到该网盘已登记的影库，防止换成其他账号。TMDB 密钥用 `configure-tmdb`。

```bash
python3 scripts/media.py configure-quark
python3 scripts/media.py library connect --drive-id family --cookie
printf '%s\n' "$QUARK_COOKIE" | python3 scripts/media.py library credential --drive-id family
python3 scripts/media.py doctor --check-quark
```

在终端运行时隐藏输入；无终端的服务器可从标准输入传一行。凭据不接受命令行参数，不打印，也不发送到聊天或 Git。文件默认在被忽略的 `runtime/secrets/`，可用 `MEDIA_HELPER_SECRETS_DIR` 改到其他私有目录。存放位置、权限要求、环境变量覆盖和部署步骤见[密钥存储与配置](docs/credentials.md)。

## 后台服务

```bash
python3 scripts/manage.py up
python3 scripts/manage.py status
python3 scripts/manage.py stop
```

本机沿用 Apple Container；`manage.py` 只管理 PanSou API，不启停 AList 或旧网页容器。追更调度与服务启停独立；首批6小时 Codex heartbeat 已启用，只检查指定且启用的订阅，不自动扩大范围。本机及 Codex 需可运行，首次定时触发仍待实际观察。

## 验证与来源

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
python3 scripts/smoke.py
```

后台模块只用 Python 标准库。旧网页验证记录在 `docs/archive/`，不作为当前后台执行验收。

[当前设计](docs/headless-design.md) · [更新来源](docs/metadata-assessment.md) · [验证状态](docs/test-report.md)

## 致谢

本项目的设计和接口核对参考了以下开源项目与公开资料，感谢各位作者的工作：

- [PanSou](https://github.com/fish2018/pansou)（fish2018）：网盘资源搜索服务，本项目以独立容器方式调用其 API 作为后台搜索来源。
- [Quark Auto Save X](https://github.com/x1ao4/quark-auto-save-x)（x1ao4）及其上游 [quark-auto-save](https://github.com/Cp0204/quark-auto-save)（Cp0204）：夸克分享读取、转存、任务查询与云解压等接口的行为参考。
- [AList](https://github.com/AlistGo/alist)：夸克驱动中的文件移动接口参考；本项目也可引用本机 AList 挂载中的 Cookie。
- [TMDB](https://developer.themoviedb.org/) 与 [TVmaze](https://www.tvmaze.com/api)：作品身份、别名与播出日程数据。
- [Infuse Metadata 101](https://support.firecore.com/hc/en-us/articles/215090947-Metadata-101)（Firecore）：影视目录与文件命名规范。

后台执行器在 `src/media_helper/` 中独立实现，只用 Python 标准库，不包含或导入上述项目的代码；它们的源码与镜像仍遵循各自的许可证。
