# 面经自动整理流水线

本地采集公开面经，使用 **DeepSeek** 抽取实际面试问题、公司、岗位、轮次和追问链，输出每个岗位目录一份持续更新的 Markdown。主要面向 Java 后端与 Agent/AI 应用开发。不生成参考答案。

## 快速开始

需要 Python 3.10+。在项目目录中执行；Windows PowerShell 示例：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml  # 首次安装；已有配置不要覆盖
```

Key 可通过通用环境变量 `LLM_API_KEY` 提供，优先于 `DEEPSEEK_API_KEY` 和配置文件中的 key。OpenAI 兼容服务可在本地 `config.yaml` 设置 `llm.base_url`、`llm.model` 与可选的 `llm.reasoning_effort`；API Key 不要写入源码或提交的配置。默认值仍是 DeepSeek 的 `https://api.deepseek.com/v1` 与 `deepseek-chat`。

例如使用带推理强度参数的兼容服务时，在被 Git 忽略的本地 `config.yaml` 中设置：

```yaml
llm:
  base_url: "https://your-provider.example/v1"
  model: "your-model"
  reasoning_effort: "high"
  api_key: ""
```

然后在启动流水线的进程环境中设置 `LLM_API_KEY`。实际接口路径和模型名称以服务商文档为准。

```powershell
# 本示例将采集和高频榜窗口同时临时覆盖为近 7 个自然日（含今天）
.\.venv\Scripts\python.exe -m pipeline run --days 7

# 只运行牛客；--limit 仅限制本次抽取的帖子数
.\.venv\Scripts\python.exe -m pipeline run --sources nowcoder --days 7 --limit 3

# 各阶段独立运行
.\.venv\Scripts\python.exe -m pipeline collect --days 7
.\.venv\Scripts\python.exe -m pipeline extract
.\.venv\Scripts\python.exe -m pipeline render
.\.venv\Scripts\python.exe -m pipeline status
```

`python -m pipeline.collect / pipeline.extract / pipeline.render` 也可使用。所有入口共用同一个参数解析器、日志、运行记录和进程锁。自定义配置使用 `--config path/to/config.yaml`，**相对数据/模板路径以配置文件所在目录为基准**。

`config.yaml` 是本机运行配置，由 `config.example.yaml` 首次复制生成，包含来源、60 天采集窗口、过滤词、模型和输出路径；它被 Git 忽略。`DEEPSEEK_API_KEY` 环境变量优先于配置中的 key。`data/interviews.db` 是内部 SQLite 状态库，用于 URL 去重、抽取状态、失败恢复、题目频次和运行记录，不是最终交付内容。原文快照在 `raw/`，运行日志在 `logs/pipeline.log`，可读结果在 `output/` Markdown 文件中。

已有 `config.yaml` 不会被示例配置覆盖；升级时需将其中的 `collect.days` 和 `output.recency_days` 改为 `60`，或在命令行使用 `--days 60` 同时设置本次采集与高频榜窗口。

## 数据来源与访问边界

| 来源 | 发现/读取方式 | 默认配置 |
|---|---|---|
| 牛客 | 公开讨论列表 → 公开 SSR 文章正文 | 启用，优先运行，每日最多 150 帖 |
| CSDN | 公开博客列表或作者 RSS/主页 → 公开正文 | 启用，每日最多 25 帖 |
| 稀土掘金 | 官方文章站点地图最新分片 → 公开文章正文 | 启用，每日最多 25 帖 |
| GitHub | 官方 REST 仓库搜索、Contents、Commits API → 公开 Markdown | 默认关闭，启用后每日最多 10 篇 |

每天所有来源合计最多 200 帖。限额是上限，不保证一定能发现足够的新面经。列表推荐和站点地图可能包含无关文章；进入 LLM 前必须同时命中面试词和岗位词。

2026-09-14 检查发现，牛客网关的 [robots.txt](https://gw-c.nowcoder.com/robots.txt) 禁止 `/api`，[牛客主站](https://www.nowcoder.com/robots.txt)与[掘金](https://juejin.cn/robots.txt)禁止搜索页。因此新版**不再使用旧的牛客搜索 API，也不使用站点搜索页**。

网页采集每次运行读取 robots.txt，未知/不可读规则按暂停处理；逐跳检查重定向，禁止转入登录、验证或支付页面。默认间隔至少 5 秒，按 robots 的更长间隔执行。网络/服务器失败最多重试 3 次；401/403/407/412/429/451 等限制立即停止该来源；521 与其他 5xx 视为临时服务器错误，按既定有限次数重试，耗尽后停止，其他来源继续。

`collect.max_requests_per_source` 是每来源每次运行的默认 HTTP 请求上限，并非站点官方限额；`sources.<名称>.max_requests_per_run` 可单独覆盖。牛客暂设 320，其他来源沿用 40；robots 请求和重试也计入上限。320 是按每轮最多 80 个候选、正文复查和发现页预留测得的运行上限，不代表站点官方额度。

牛客每解析完一页，就将该页候选 URL 和公开的下一页地址写入 SQLite；若本次请求上限在发现或抓取阶段用完，下次运行会从未处理候选及分页继续。候选队列与每日抓取次数分开保存；`candidate_queue_before/after` 区分待处理、可处理、今日次数用尽、冷却中、当日暂停和已完成。单轮最多处理 `max_candidates_per_source` 个候选，超过此数量但已发现的牛客 URL 仍保留在队列。此前版本只记录未处理数量，无法从运行摘要还原那些未保存的 URL。

付费、VIP、订阅、登录才能阅读全文、验证或缺失公开正文的文章整篇跳过。不执行页面验证脚本，不调用隐藏正文接口，不用搜索摘要冒充全文，不更换身份或绕过限制。旧配置中的 `collect.cookie` 已停用；不需要 cookies.json，也不会要求通过刷新登录态获取受限内容。

GitHub 只使用其明确向公共数据开放的 [REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)，不访问网页搜索、私有仓库或提供令牌提高限额。触发 API 额度限制会停止该来源。

来源能否访问还取决于站点当时的服务规则与访问状态。2026-09-24 真实采集中，CSDN 的 HTTP 521 经有限重试消耗较多额度，牛客与 CSDN 达到请求上限，掘金遇到验证后停止。三个网页来源采到的原文仍须经过粗筛、模型及证据校验，原文数不代表有效面经数。

## 配置补充来源

`sources.<source>.discovery_urls` 支持允许访问的 HTML 列表、RSS/Atom、XML sitemap；`seed_urls` 可填明确的公开文章地址。请保持主机名在对应 `hosts` 中。例：

```yaml
sources:
  csdn:
    enabled: true
    discovery_urls: []
    seed_urls:
      - "https://blog.csdn.net/作者/article/details/文章ID"
  juejin:
    enabled: true
    discovery_urls:
      - "https://juejin.cn/sitemap/posts/index1.xml"
    seed_urls: []
  github:
    enabled: false
    repositories: []  # 可填 owner/repo；为空时按 search_query 搜索近期仓库
    search_query: "面经"
```

示例中的作者/ID 是占位符，运行前替换。配置中添加链接不会跳过 robots、付费检查或时间窗。

掘金 Java 标签页在本次请求中没有公开文章链接，因此默认改用官方站点地图。一个分片约 8 MB；下载有 12 MB 上限，只扫描一个分片并截取候选上限，不遍历全站。可用 `seed_urls` 或更有针对性的公开 RSS 降低无关请求。

## 抽取质量、费用与复核

只收录软件开发/研发岗位面经，排除测试、测试开发、测开、QA、SDET及其他非开发岗位；按实际面试岗位判断，测开面试出现Agent/Java题也不收录。开发岗位自身问到的具体测试技术问题可以保留。只收录面试官实际问出的具体专业知识、算法或技术场景问题。“简单问了Agent项目”、泛泛的项目背景/亮点/难点、薪资、入职时间、实习地点、是否用过某技术等不收录；泛泛开场下如有具体技术追问，只保留具体追问。原帖必须提供技术对象或条件，不补写原帖没有给出的问题。全文无合格问题则标记 discarded，保留 raw。提示词和确定性复查共同约束；历史已抽取结果可运行 revalidate 应用当前规则（操作前保留备份）。

- 模型必须返回完整 JSON Schema；失败时附校验错误重试，首次加两次重试。仍失败标记 `extract_failed`，不会每次自动无限重试。
- 原文作为待分析数据，不执行其中的指令。每题必须带连续原文片段；无法定位的结果进入人工复核。寒暄与自我介绍被过滤。
- `0811` 等不含年份的信息不推测年份；缺失面试日期保留 null。分块传递前文分场标题，不把“面经01”擅自当作“一面”。
- `confidence < 0.6`、不同块的公司归属冲突或证据不足会进入 `output/pending/`。合集可能需要人工拆分，不能只调高置信度就当成正确结果。
- 每次调用前预留输入估算和 `max_tokens` 的预算；用实际 Token 用量结算。超预算前暂停。调用中断/超时且用量未知时保留预留额，避免当作免费重试。下一天自动切换账本。
- `data/usage.json` 保存当日各次调用的帖子 ID、块号、重试号、预留/实际用量；历史账本为 `usage-YYYY-MM-DD.json`。这是配置对应工作目录的预算，不是全账户跨项目预算。
- 分块结果保存在 `data/checkpoints/`。同一内容与抽取配置续跑复用已成功的块；改变模型、Schema 或提示词会重新计算缓存键。

参考 [DeepSeek JSON Output 文档](https://api-docs.deepseek.com/guides/json_mode/)；仍进行本地 Schema 校验，不将 JSON mode 等同于语义正确。

人工复核流程：

1. 在 `output/pending/<fingerprint>.json` 中对照 `post_url` 与本地 raw 修改 `result`。保留 `post_fingerprint`；修正题目、原文证据、日期、归属和 confidence。非面经设 `is_interview_post: false`。
2. 提交复核：

```powershell
python -m pipeline review --review-file "output/pending/<fingerprint>.json"
```

程序再次校验，再事务入库并渲染，生成 `.reviewed.json` 审计副本。已审核帖子不会再次累计计数；pending 目录保留历史文件，当前待处理状态以 `python -m pipeline status` 和数据库为准。

## 去重、日期与输出

牛客发现器会跟进公开 HTML 的下一页链接，默认每轮最多 3 页（含入口页），仍受牛客每轮 320 次 HTTP 总请求上限约束。空壳列表页冷却后最多复查一次。`max_discovery_pages` 是上限；持续空页面、访问限制或没有下一页时会提前停止，并记录停止原因。近 60 天是收录时间窗，不保证推荐列表覆盖全部 60 天帖子。

正文暂缺、过短或缺少可靠发帖时间的文章进入 `retryable` 队列。本机 `collect.page_attempts_per_day: 1`，每帖每天只复查一次，次日继续；`page_retry_cooldown: 30` 仍控制同次运行内的发现页复查。队列与次数保存在 SQLite，重启不会重置当天次数。牛客优先处理队列中尚未尝试的候选；仅剩失败重试项时允许继续发现新页面，避免一个长期解析失败的帖子阻塞每日新帖发现。每轮候选先安排未尝试项，再按上次尝试时间由早到晚安排到期重试，仍受候选数量和 HTTP 请求总上限约束。成功、付费、robots 禁止等情况按各自原因处理。

牛客原帖日期读取 URL 对应的 `prefetchData[*].contentId` 和 `ssrCommonData.contentData.id/uuid` 记录中的 `createdAt/createTime`，不采用评论、推荐帖、编辑时间或面试发生时间。旧牛客记录升级后会排入日期复查队列，未复查前不进入抽取和输出，历史 raw 与抽取内容保留。已有历史记录的日期复核不受新帖采集时间窗下限限制：日期确认不变时恢复原抽取结果，日期纠正时保留旧快照并将新快照交给正常抽取流程；未来日期仍不收录。

原帖唯一指纹为 URL + 发布时间。正文保存在 `raw/{source}/{采集日期}/{post_id}.json`，同一记录永不覆盖；同一日的不同发布时间版本使用指纹后缀。

`questions` 存规范题目，`occurrences` 存每个来源中的公司、轮次、日期和追问。频次按不同原帖 URL 计数，同帖不同轮次、重新抽取或同 URL 版本不会虚增。完整帖子入库、出现记录重建和最终状态在同一事务内完成。

输出：

每个岗位目录只生成一份 `面经汇总.md`，包含已收录的全部历史记录，按公司、原帖、轮次组织。同一篇面经只标一次来源和原帖发布日期，每个轮次单独标明面试日期，原文未明确完整日期则写“未明确”。旧月度文件迁移至 `data/output-history/时间/岗位/` 保留，不再在岗位目录内按月生成。

```text
output/
├── Java后端/面经汇总.md
├── Agent开发/面经汇总.md
├── 其他/面经汇总.md
├── 高频题/近60天高频题Top50.md
└── pending/<fingerprint>.json
```

默认采集发布日期为含今天在内的滚动 60 个自然日，可用 `--days N` 覆盖；牛客只按文章页面的发帖时间判断是否在窗口内，无法从正文或结构化元数据确认发帖时间的帖子会跳过。汇总保留历史已收录记录，高频榜只按文章发布日期判断近 60 天范围；模型抽取的面试发生日期仅作为可选信息，不参与时效筛选。高频榜要求至少 3 个不同原帖，并按每个来源的 `2^(-距今天数/14)` 求和。无符合条件的题目就输出空榜，不以一次题填满 Top50。岗位汇总和高频榜都保留原帖链接。

发现页的标题只用于候选排序，不再单独决定是否抓取；抓取后的完整公开正文仍须通过面试词与岗位词粗筛才会进入模型。粗筛把标题和完整正文合并后做忽略空格、大小写的子串匹配，至少命中一个面试词（如“面试”“一面”“offer”）和一个岗位/技术词（如“Java”“Redis”“Agent”“大模型”）才进入模型；缺任一类会记录原因并标记为 filtered。站点地图/RSS 的时间字段只用于候选排序；对牛客，采集窗口始终使用文章正文页或结构化数据中的发帖时间。

`logs/pipeline.log` 记录运行命令、采集窗口、各来源发现页与候选数量、文章抓取尝试数/实际页面请求数/底层 HTTP 请求数、已采集/已见/超时窗/跳过/失败数量，以及粗筛、抽取、待复核和渲染汇总。网络 5xx（如 502/503/504）按配置最多重试 3 次并逐次退避；每次尝试仍计入来源请求上限，达到上限或重试耗尽就停止该来源，不会死循环。日志不记录 API key、文章正文或完整模型提示词；每次运行的结构化摘要也保存在 SQLite 的 `runs.detail` 中。抓取尝试表保留每个 URL 最近一次结果及跳过原因，便于排查来源问题。

模板独立位于 `templates/`，内容通过 Jinja2 渲染。输出使用原子替换；原始文章不可变。

## 旧版本升级、恢复与运行状态

第一次打开旧数据库时，会创建 `data/interviews.pre-v2-时间.db` 备份，保留 `legacy_posts/legacy_questions`，将旧成功抽取/待复核帖子排队重新抽取。旧频率、猜测日期不直接迁移为可信数据。本次升级前的输出另存于 `data/backups/output-pre-v2-时间/`。

v2 状态库升级到 v3 前会额外创建 `data/interviews.pre-v3-时间.db` 备份，新增页面尝试次数、冷却时间和发布日期复查标记。`occurrences.event_date` 为兼容旧字段名而保留，值统一为原帖发布日期；轮次日期单独保存在 `round_date`，不参与收录和榜单判断。

模型日志包含耗时、`finish_reason`、响应字符数、prompt/completion/reasoning token 用量、JSON 错误位置及 Schema 校验路径。失败最多重试两次，`llm.retry_backoff` 默认 5 秒（两次等待分别 5、10 秒）；鉴权/参数等明确的非暂时错误不重复发送。Schema 日志不含模型返回的字段值，防止正文进入日志。运行摘要区分发现失败、文章失败、临时解析失败、冷却/限次、未处理候选和粗筛原因。

```powershell
# 恢复“raw 已完整写入、数据库尚未提交”时中断的帖子
python -m pipeline recover

# 只重试明确失败的帖子（继续复用已成功的分块），然后更新输出
python -m pipeline retry-failed
python -m pipeline render

# 因预算暂停的帖子直接继续 extract；不要删除 usage.json 绕过预算
python -m pipeline extract

# 修改排除词/日期规则后，免费重新检查已保存结果并更新输出
python -m pipeline revalidate
```

OS 进程锁避免同一个数据库的定时任务重叠。进程结束后锁自动释放，磁盘上的 lock 文件可保留。`runs` 表记录开始、完成、partial、failed 和 interrupted；日志在控制台和 `logs/pipeline.log`，自动轮转。

退出码：0 为本次命令完成；1 为失败；2 为部分完成（例如来源访问受限、达到请求预算或仍有待抽取帖子）。待人工复核会打印提示，不会阻止其他帖子处理。退出码 0 不代表连续三天稳定性或人工准确率已经验收。

模型响应允许一个前置且闭合的 `<think>…</think>` 块及完整 JSON 代码围栏；移除包装后必须整体通过 JSON、Schema 和原文证据检查。不会从任意说明文字里寻找 JSON。连接失败日志记录异常因果链类型与数字错误码，不打印异常消息、密钥或响应正文。请求上限单独记为 `request_limit_reached`，不计入服务器/文章失败；渲染摘要记录汇总来源数、高频窗口内题数、未达到 3 个不同原帖的题数和最终榜单条数。

## Windows 定时与 cron 示例

若已配置定时任务，请勿重复创建。现有任务只应运行、检查和汇报，源码、配置及筛选规则的变更需单独审核。下面是可选部署示例。

`run_daily.bat` 自动创建日志目录，优先使用 `.venv\Scripts\python.exe`，保留 Python 退出码。手动双击会暂停，计划任务使用 `scheduled` 参数。

可在 Windows“任务计划程序”创建每天 09:00 的任务，操作为：

- 程序：`C:\Windows\System32\cmd.exe`
- 参数：`/d /c ""D:\你的项目目录\run_daily.bat" scheduled"`
- 起始于：`D:\你的项目目录`
- 如果任务已运行，选择“不启动新实例”。

也可在 PowerShell 中创建（请替换路径；本项目不会自行注册任务）：

```powershell
$projectPath = (Get-Location).Path
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" -Argument ('/d /c ""{0}\run_daily.bat" scheduled"' -f $projectPath) -WorkingDirectory $projectPath
$trigger = New-ScheduledTaskTrigger -Daily -At '09:00'
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'MianjingPipeline' -Action $action -Trigger $trigger -Settings $settings
```

Linux/macOS cron 示例（先手动验证解释器和配置路径）：

```cron
0 9 * * * cd /absolute/project && /absolute/project/.venv/bin/python -m pipeline run --config config.yaml
```

## 测试与验收

```powershell
python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts/run_tests.py
```

测试只使用临时数据库与模拟 HTTP/LLM，不读取真实 key，不调用外部服务。覆盖 CLI、迁移备份、事务回滚、不同来源计数、60 天采集与高频榜窗口、跨月汇总与历史月文件迁移、预算与断点、访问限制、付费检测和复核。

更早的验收记录保留在 `docs/` 中。连续三天运行需要自然时间，不能由单次离线测试替代；使用 `runs` 和日志核对。

## 个人学习与署名

内容版权归原作者所有，仅供个人学习，不作商用或未经许可的再次分发。所有输出带来源链接和声明。请遵守各站服务条款；robots 允许不等于任何用途都得到授权。

`config.yaml`、`.env`、cookies、raw、data、logs、output 和本地验证目录均被 Git 忽略。项目没有内置真实凭据。采集/解析设计参考 InterviewRadar、nowcoder-interview-digest，Markdown 输出参考 interview-experience；新版已替换受限制的旧采集路径。
