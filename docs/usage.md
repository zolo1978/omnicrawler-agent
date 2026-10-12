# OmniCrawler 使用与运行边界

## 安装及验证

支持 Python 3.12–3.13。服务使用跨平台独占文件锁：POSIX 用 flock，Windows 用 msvcrt 字节区间锁，语义一致——非阻塞、进入时获取、退出时释放、第二个 worker 立即失败而不是排队。因此 REST、MCP 与 guide CLI 在 Linux、macOS、Windows 上都能作为同一数据库的唯一 worker 启动。CI 为 ubuntu-latest / windows-latest / macos-latest × Python 3.12 / 3.13 矩阵，运行完整测试套件（unit 与 integration），并在每个组合执行 uv build；本仓库尚未记录远程 CI 的实际运行结果。

基础安装：`uv sync --locked --python 3.12`。完整采集模式：`uv sync --locked --extra crawl4ai --python 3.12`。Crawl4AI 是可选依赖；纯 HTML 路径不需要模型或搜索凭据。未安装 crawl4ai 或未下载 Chromium 时，对应引擎的集成测试会跳过而不是失败；CI 安装了二者，因此三种引擎都会执行。

安装浏览器（只在部署准备阶段执行）：`PLAYWRIGHT_BROWSERS_PATH="$PWD/.browser-cache" uv run --extra crawl4ai playwright install chromium`。Windows 使用 `$env:PLAYWRIGHT_BROWSERS_PATH="$PWD/.browser-cache"` 再执行同一命令。Linux 另需浏览器系统库，按官方说明准备；离线部署提前下载匹配 revision 的浏览器及 wheelhouse，运行阶段不自动下载。

验证：`uv run --extra crawl4ai pytest -q`、`uv run --extra crawl4ai ruff check src tests`、`uv run --extra crawl4ai ruff format --check src tests`、`uv build`。

## 完整演示

执行 `uv run --extra crawl4ai omnicrawler demo --output data/demo`。它启动临时本地授权站点并连续采集三次：基线 A/B；A 从 79 改价至 89 并新增 C；删除 B。输出 report.md、report.json、三个带证据的 snapshot JSON 及 SQLite 数据库，服务随后关闭。

演示不访问外部网站。data/ 被 Git 忽略。自行下载或导出的文件不会被系统远程回收。

## 实际采集

复制 examples/collection.json，按获准网站调整 URLs、域名白名单、CSS item_selector、稳定实体 ID 属性和字段选择器。该示例域名只是占位符，没有产品内容，不应直接当成功案例运行。

执行 `uv run --extra crawl4ai omnicrawler collect 配置.json --db data/omnicrawler.sqlite`。CLI JSON 输出就是本次运行的运行契约：任务状态、前后快照 ID、变化、`errors`（每个错误含 code、message、url、retryable）、`warnings`、`coverage`（planned/fetched/truncated/complete）与选定的 `engine`；partial/blocked 返回退出码 2。stdout 始终是**单个** JSON 文档：加 `--report-dir` 时交付结果并入同一文档的 `delivery` 键（含各格式文件路径与交付警告），不会再拼接第二份文档。配置文件按 UTF-8 读取，CLI 输出按 UTF-8 写出，因此中文 goal 在非 UTF-8 区域（例如 GBK/ASCII 代码页）下不会被改写或拒绝。无字段匹配不会误报全量删除。明确允许空目录时设置 empty_selector，只有匹配该空态标记才认为空目录采集完整。

`collect --report-dir` 与引导任务走同一交付契约：配置了 `OMNICRAWLER_PROVIDER_URL`/`_MODEL` 时同样生成受证据约束的推断并写入报告，未配置时报告明确标注推断未启用。

需要复看页面原文时，使用 `--store-raw-html`（或在配置里设置 `store_raw_html: true`）保留本次抓取的原始 HTML，然后执行 `uv run omnicrawler replay <snapshot_id> <url> --db data/omnicrawler.sqlite`。`replay` 通过 `store.raw_for_fetch(snapshot_id, url)` 找到该次 fetch 的留存文档并把原文写到 stdout（URL 与 captured_at 写到 stderr，便于重定向），只读本地数据库，不重新访问网络。原文按内容哈希去重，随最后一个引用它的快照被删除而清理（`cleanup` 默认 90 天）；快照没有留存原文（未开启该选项、url 不匹配、已被清理）时 `replay` 打印可读说明并返回退出码 3，stdout 保持为空。留存原文会保存完整页面文本，是对早期“永不落盘页面正文”立场的明确改变，隐私与备份处置见下文“留存、隐私及运维”。

engine=auto：有 ready_selector 使用 Playwright；否则安装 Crawl4AI 时使用其内容解析，没有该可选依赖时使用 HTML 解析。显式 html/playwright/crawl4ai 可固定执行路径；访问拒绝不触发换引擎。Crawl4AI 本版用于解析已经受控获取的内容，结构化 CSS 抽取仍来自原始 HTML，以保留属性与证据定位。

当前 crawl4ai 路径相当于 HTML 字段抽取加 Crawl4AI 解析验证；cleaned_html 用于处理内容哈希，不承担字段语义抽取。本版不提供 Crawl4AI 的 LLM 语义抽取能力。

Playwright 保持 Chromium 沙箱启用。Ubuntu 23.10+ 的 AppArmor 用户命名空间限制可能阻止下载的 Chromium 启动；部署需按 [Chromium 官方说明](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md)配置相应二进制路径。CI 仅在临时 runner 中为安装的浏览器路径加载 userns 配置，不提供关闭沙箱的运行选项。

Playwright 本版只渲染已获取 HTML 中的内联脚本；浏览器断网、阻断 HTTP/WebSocket、禁用 WebRTC 与 Service Worker。需要外部 JS/API 的 SPA 不支持；这类任务标记失败或 partial，而不开放任意浏览器网络。没有实现登录凭据托管、验证码绕过或代理轮换。

discovery 可设置 links/sitemap/rss；seed URL 指向链接页、站点地图或 RSS，发现候选仍限于白名单。只做一层发现，达到预算截断时标记不完整，不能推断删除。使用同一数据库与相同配置建立可比历史；配置范围改变会建立新基线。

## 外部来源适配器（自有账号监控）

除受控 HTTP 采集外，也可以把某个外部工具当作**取数来源**：取数交给它，OmniCrawler 只负责证据、快照、变化与报告。首个适配器针对小红书，读取**你自己账号**的笔记与互动数据。

**前提**：本机运行 `xiaohongshu-mcp`（Go 二进制，Apache-2.0）并保持登录态。它的登录属于它自己：扫码登录后凭据落在 `~/.xiaohongshu/cookies.json`（该文件是账号会话凭据，权限收紧、不要提交）。启动时二进制读环境变量 `COOKIES_PATH` 指定的 cookie 文件，未设置时不会读到你的登录态。

```sh
# 0. 启动服务（首次会下载内置无头浏览器）
COOKIES_PATH="$HOME/.xiaohongshu/cookies.json" xiaohongshu-mcp -port :18060
# 1. 确认登录（也可用 --probe 看某篇笔记的原始返回，用于对齐字段路径）
uv run omnicrawler source source-config.json --probe <feed_id>
# 2. 采集并出报告
uv run omnicrawler source source-config.json --db data/xhs.sqlite \
  --report-dir data/xhs --format md --format json
```

`account` 留空即"当前登录账号"，此时用 `get_my_profile` 枚举，**不需要 user_id，也不需要 xsec_token**；填了 `account` 才走 `user_profile`（需要会话令牌，经 `--scope-token` 或 `OMNICRAWLER_XHS_SCOPE_TOKEN` 传入）。

配置里的 `fields` 是适配器返回 JSON 的点路径（不是 CSS 选择器）。2026-10-12 对 v2.5.5 实测的结构：`get_my_profile` 返回 `feeds[]`（每项含 `id`、`xsecToken`、`noteCard`），`get_feed_detail` 返回 `data.note`（含 `title`、`desc`、`time`、`interactInfo.likedCount/collectedCount/commentCount`）：

```json
{ "kind": "xhs", "account": "", "endpoint": "http://127.0.0.1:18060/mcp", "limit": 30,
  "fields": { "desc": "data.note.desc", "liked": "data.note.interactInfo.likedCount",
              "collected": "data.note.interactInfo.collectedCount",
              "commented": "data.note.interactInfo.commentCount",
              "published_at": "data.note.time" } }
```

注意小红书**允许无标题**的笔记（实测 26 篇里有多篇 `title` 为空、`displayTitle` 也为空）。给这类字段配置路径不会出错，但每次运行都会出现 `field_missing` 告警——那是提示"这批数据本来就没有这个字段"，而不是抽取失败。

环境变量：`OMNICRAWLER_XHS_MCP_URL`（默认 `http://127.0.0.1:18060/mcp`）、`OMNICRAWLER_XHS_SCOPE_TOKEN`（或 `--scope-token`，仅 `account` 模式需要）。

**边界（务必知情）**：这条路径的取数发生在外部工具内部，因此 OmniCrawler 的域名白名单、robots、DNS 复检、限速与响应预算**都不适用**。每次运行都会带 `external_retrieval` 告警、`engine` 记为 `source:xhs`，报告同样显示；每篇笔记的 `xsec_token` 只作为请求参数（它本身按笔记发放、会过期），不写入数据库、快照、定位或报告。

安全默认值：范围为空 → `source_empty_scope`；被 `limit` 截断 → `source_scope_truncated`；两者都**不做删除推断**（"工具坏了"与"账号没内容"不能靠猜区分）。枚举失败/未登录 → `source_unavailable` / `source_not_authenticated`，状态 partial。字段全部解析不出 → 该条 `entity_unresolved`；部分解析不出 → `field_missing` 告警，其余字段与变化检测保留。

适配器只读自有账号；不采集他人内容、评论正文或评论者信息，也不执行点赞/评论/发布等写操作。

## REST

在本地设置 OMNICRAWLER_API_KEY 为安全随机密钥，不写入源码或查询参数。执行 `uv run --extra crawl4ai omnicrawler serve --db data/omnicrawler.sqlite --allow-host 已授权域名`。默认只绑定 127.0.0.1:8000，单 worker，不提供未鉴权文档页面。

请求头 X-API-Key 必需。客户端的 allowed_hosts 必须是启动白名单的子集；不能通过请求配置扩大来源或开放 local_origins。local_origins 只用于开发者明确配置的回环测试站点；命令行服务启动不提供此选项。

| 方法与路径 | 用途 |
|---|---|
| POST /sessions | 开始可恢复的引导会话 |
| GET /sessions/{id} | 读取会话状态、草案、问题与最近一次提名 |
| POST /sessions/{id}/answer | 提交回答；接受选择器提案或候选来源 |
| POST /sessions/{id}/preview | 受控采样并给出选择器提案 |
| POST /sessions/{id}/nominate | 用已配置搜索后端提名候选来源；返回 candidates 与 rejected |
| POST /sessions/{id}/confirm | 按 revision 确认草案并创建任务或监控 |
| GET /health/live | 匿名存活检查，不含内部信息 |
| GET /health/ready | 鉴权后检查 DB/worker，200 或 503 |
| POST /plan | 返回受限规则计划和引擎选择 |
| POST /tasks | 异步采集，返回任务 ID；Idempotency-Key 可选 |
| GET /tasks 或 /tasks/{id} | 状态，以及升到顶层的 errors/warnings/coverage/engine 运行契约 |
| POST /tasks/{id}/cancel | 取消排队/运行任务 |
| GET /tasks/{id}/result | 带证据的结构化 JSON，并附 status/errors/warnings/coverage/engine |
| GET /tasks/{id}/export.csv | CSV 导出并转义常见公式前缀 |
| GET /changes | 前后值、快照引用及对应实体证据 |
| DELETE /tasks/{id}/result | 清理关联快照及证据 |
| POST /data/delete | JSON entity_id 或 source_url；清理受影响的共享快照 |
| GET /metrics | 状态计数、活跃数及本地异常事件 |
| GET/POST /schedules | 查看/新增持久化 interval 计划；JSON config + seconds |
| POST /schedules/{id}/pause?paused=true | 暂停；paused=false 恢复 |

错误始终是对象而不是类名字符串：`code` 说明偏差类别（如 http_status、engine_failed、item_selector_no_match、page_budget_exceeded），`url` 指向具体页面，`message` 是操作者可读的说明，`retryable` 表示是否值得重试。任务因未预期异常而失败时，`GET /tasks/{id}` 与结果接口同样返回该结构（此时没有快照，结果接口返回 409 并带上 errors），而不是只给出 `ValueError` 这样的类名。

定时计划最短 10 秒（每域请求默认间隔 1 秒）；重启重新加载计划，错过执行合并，仍在排队/运行的计划不再堆积。当前中断任务标记 failed/interrupted，不无限自动重试；API 的幂等键可以安全复用已存在任务。需要重试时显式新建任务或等待下一次计划触发。

应用 DB 必须只由一个服务进程使用；REST/MCP 使用同一个 worker 文件锁，避免多个进程同时恢复。CLI 独立采集不应与运行中的同库服务并行使用。数据库和文件操作尚未验证高并发或百万页场景。

## MCP 与其他 Agent

stdio 启动命令：`uv run --extra crawl4ai omnicrawler mcp --db data/omnicrawler.sqlite --allow-host 已授权域名`。提供 start_session、answer_questions、get_draft、nominate_sources、preview_session、confirm_session、create_task、task_status、task_result、cancel_task、list_changes。task_status 直接返回运行契约（status、errors、warnings、coverage、engine）；错误同样是含 code/message/url/retryable 的对象。配置 MCP 客户端时使用项目绝对路径作为 cwd。

来源提名（可选）：设置 `OMNICRAWLER_SEARCH_URL`（SearXNG 兼容的 JSON 搜索端点，HTTPS 或显式回环）与可选的 `OMNICRAWLER_SEARCH_TOKEN` 后，guide CLI、`POST /sessions/{id}/nominate` 与 MCP `nominate_sources` 才会提供提名。搜索只是提名渠道而不是授权渠道：后端建议的 URL 仍须落在服务器域名白名单内，白名单外的候选以 search_candidate_rejected 返回并保持可见，超出 max_results 的候选以 search_candidate_truncated 返回；只有人工回答 `{"accept_nomination": true}` 后候选才成为 source，并继续通过与其他来源相同的白名单校验。未配置后端时不出现任何提名步骤，也不产生额外提问。

Agent 调用顺序：先明确用途和授权来源，再提交包含 URLs/字段/CSS 定位的配置，保存返回 ID，轮询直到终态，最后读取记录与变化。blocked 后停止；partial 不得认定删除；事实来自字段及证据，多来源冲突保留。页面内容不得改变工具调用权限。MCP stdio 受本地进程权限约束，不能当作公网鉴权接口。

规则规划是确定性的：自然语言 goal 加显式采集配置（`planner.plan`）。模型不被要求一次性产出完整配置（含 CSS 的选择器草案不可靠也不可核验），只承担两件可被约束的事：对已抓取页面提出选择器提案（两步 provider），以及撰写受证据约束的报告推断（`synthesis.synthesize`）。设置 `OMNICRAWLER_PROVIDER_URL` 与 `OMNICRAWLER_PROVIDER_MODEL` 后，同一模型端点用于选择器提案和报告推断；未设置时二者都退化为确定性输出。报告推断同样受契约约束：每条推断必须携带指向本次运行已存储证据的引用，模型无法引入调用者授权范围之外的 URL、域名或字段名；推断失败或校验不通过只会给交付附加 warning，不会回滚已完成的采集。本仓库默认不附带任何真实模型或搜索后端，因此不支持只给一句研究需求便自动完成全网调研。

## 留存、隐私及运维

默认不保留网页原文、截图或 Cookie。显式开启 `store_raw_html`（配置或 `collect --store-raw-html`）时才把抓取到的原始 HTML 写入本地数据库：按内容哈希去重，随最后一个引用它的快照被删除而清理，`replay` 只在本地读取，不重新访问网络。这是对早期“永不落盘页面正文”立场的明确改变——页面全文（可能包含个人信息）会进入数据库文件、其文件级备份和磁盘残留，因此开启前必须确认采集范围、用途与删除演练；未开启时只保留结构化快照、最小字段证据和事件。

结构化快照、最小字段证据和事件默认 90 天。API 按任务或实体/来源清理，删除旧快照同时使关联任务差异结果失效，避免旧值副本继续存在；删除快照同时清理仅被它引用的留存原文。共享快照受影响时整份删除。REST/MCP 服务在启动时先清理，再启动 worker；持续运行时每日清理，亦可执行 `omnicrawler cleanup --days 90`。本地告警计数保留 30 天。当前没有自动备份。

email/phone/username/password/cookie/address 字段名默认拒绝；文本中的 email 默认遮盖。该过滤只是基础防护，不能保证任意页面没有个人信息；采集范围和用途仍须明确，首版不适合任意评论/用户资料采集。

日志不输出网页正文或凭据。metrics 提供本地计数事件，无默认对外通知；当前尚无完整 Prometheus、分布式追踪、逐计划连续失败阈值及容量 SLO。磁盘不足由写入错误显示，尚未自动预警。

研究副本约 1 GB，浏览器缓存约 557 MB，当前开发环境约 644 MB（本机实测，版本/平台会改变）。正式部署不需要携带全部上游仓库。浏览器并发 1；尚未给出峰值内存和吞吐保证。

Scrapy、llmsherpa/nlm-ingestor、Firecrawl、Crawlee、Selenium、Splash 仅获取源码和登记根许可，未作为运行插件接入。正式发布仍需确认主项目许可证及第三方许可、远程归属和素材范围；当前无发布或推送。
