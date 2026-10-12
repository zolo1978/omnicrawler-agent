# 产品级优化验证记录（2026-10-12）

分支 `product-upgrade/all-recommendations`，基线 `bae2fbb`。决策与范围见 [产品级优化方案](../plans/2026-10-12-product-upgrade.md)。

## 结论摘要

| 指标 | 基线 `bae2fbb` | 本次 |
| --- | --- | --- |
| 全量测试（Windows / Python 3.12 / 含 crawl4ai 与 Chromium） | 100 passed，14 failed | **201 passed，0 failed，0 skipped** |
| 测试用例数 | 114 | 201 |
| Windows 可用性 | REST/MCP/guide 因 `fcntl` 整体不可用 | 全入口可用（跨平台 worker 锁） |
| PDF 交付 | Windows 下必降级（只找 macOS/Linux 字体根） | 正常交付（补 Windows 字体根与家族） |
| 报告"推断"分区 | 恒为空数组，章节标题照常渲染 | 无后端时显式标注未启用；有后端时均为带证据引用 |
| 真实页面抽取验证 | 无（全部为 loopback 固定页） | 12 个真实页面（5 域名）+ 8 个脏数据变体离线重放 |

全量命令与结果：

```sh
uv sync --locked --extra crawl4ai --python 3.12
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browser-cache" uv run --extra crawl4ai playwright install chromium
uv run --extra crawl4ai pytest -q     # 201 passed in 81.56s
uv run --extra crawl4ai ruff check src tests tools          # All checks passed
uv run --extra crawl4ai ruff format --check src tests tools  # 46 files already formatted
uv build                                                     # sdist + wheel 均成功
```

## 逐项验收

| 编号 | 交付 | 验收证据 |
| --- | --- | --- |
| AS01 答案综合 | `synthesis.py`；`provider.py` 第三 stage；`Report.inferences: list[Claim]`；`Report.coverage/errors/warnings` | 18 项报告/综合测试，含"伪造定位""伪造 URL""零引用""非法类型""重复文本"全部丢弃、"一条引用不可证则整条丢弃"、"重复引用同一证据不判失败"、四种 provider 失败模式仍可交付 md+json |
| DS01 来源提名 | `search.py`；`session.nominate`；REST `POST /sessions/{id}/nominate`；MCP `nominate_sources`；CLI 提示 | 白名单外候选返回 `search_candidate_rejected` 且不进入 scope；采纳前零抓取（`site.hits == []`）；目标+白名单即可构造（无需伪造 seed）；候选集变化时 coverage 随之变化，不会被误认作同一基线 |
| RE01 抽取韧性 | 字段选择器备选链（含元素自身文本）；字段级偏差降级为 warning；分页；`Coverage` 口径；结构化 `CollectError` | 16 项韧性测试：单字段失效仍可比、已记录实体不再解析 → `entity_unresolved` 且不做删除推断、分页进入同一快照、预算截断阻断删除推断、HTTP 500 报 `http_status` 且带 url |
| RE02 真实语料 | `tools/record_corpus.py`；`tests/fixtures/corpus/`（12 真实页 + 8 脏数据变体）；23 项离线重放测试 | 录制遵守 robots（被禁页面跳过且退出 1）；`--check` 校验 manifest 与文件一致；重录幂等 |
| EV01 原文回放 | `store_raw_html`（内容哈希去重、随快照清理）；`omnicrawler replay` | 手工端到端：`collect --store-raw-html` 后 `replay` 打印留存原文（exit 0）；未留存时 exit 3 并说明原因；删除快照后原文被回收（引用计数） |
| OP01 跨平台/CI | 跨平台 worker 锁；Windows 字体根；CI 矩阵 ubuntu/windows/macos × 3.12/3.13 + `uv build` | 6 个原 `fcntl` 失败用例全部通过；锁有独立进程互斥测试；3 个 PDF 用例在本机实际渲染通过 |
| OP02 引擎固化 | 引擎按 coverage 固化到 `collection_engines`；coverage 不再包含引擎 | 固化后即使 `select_engine` 改判也沿用原引擎；固定引擎不可用时 `engine_unavailable` + blocked，而不是静默换引擎 |
| P2 韧性与独立性 | 429/5xx 有界重试 + `Retry-After`；`independence_verified` 由真实主机数得出 | 12 项传输测试（429 重试后成功、403 从不重试、重试耗尽仍结构化报错、超时重试、每次重试重跑 robots 与 DNS 校验）；治理测试覆盖 2 主机独立 / 单主机不独立 |

## 语料发现并修复的真实缺陷

| # | 现象 | 处置 |
| --- | --- | --- |
| F1 | `select_one` 只能匹配后代元素，Sphinx 这类"标签文本在锚点自身"的列表被静默丢弃：docs.python.org 列表页 279 条中 40 条（14.3%）不进入实体集，而运行仍报 `succeeded` 且 `complete=True` | 备选链最后一项允许命中元素自身；该页记录数 239 → 279，语料 floor 断言 270 |
| F2 | 未声明字符集的响应按 `utf-8 + replace` 解码，GBK 页面被替换为 U+FFFD 后当作字段值与证据存入，且不报错 | 改为按 header → BOM → `<meta charset>` → UTF-8 顺序解码；仍失败时 `text_decoding_lossy` 警告。原先记录该缺陷的语料测试按注释约定**翻转**为断言正确解码 |
| F3 | 语料工具 `--refresh` 实为空操作（幂等守卫先返回） | 由语料 agent 修复并验证重录幂等 |
| F4 | 新发现：`chromium_available()` 检查 Playwright 默认缓存，而采集器默认用仓库内 `.browser-cache`，导致按 README 安装浏览器后浏览器用例仍被静默跳过 | 检测与采集器路径对齐；本机浏览器用例由"跳过"变为真实执行并通过 |
| F5 | 真实站点使用中发现：`cli.py` 用**区域编码**（GBK/ASCII 代码页）读取配置与写出 demo 产物。中文 goal 的 UTF-8 配置在非 UTF-8 区域下会报 `UnicodeDecodeError` 或静默变成乱码；CLI 自身 stdout 同样受区域编码限制 | 配置文件按 UTF-8 读取、demo 产物按 UTF-8 写出、CLI stdout/stderr 显式 UTF-8；新增"敌意区域设置"子进程回归测试（`PYTHONUTF8=0 LC_ALL=C`），断言中文 goal 原样进入报告 |
| F6 | 真实站点使用中发现：`collect --report-dir` 不调用综合层，因此**配置了 provider 也会得到"推断未启用"的报告**（只有引导/服务路径会综合），属于典型的静默降级 | 该路径改为与引导任务同一交付契约，并补文档说明 |
| F7 | 真实站点使用中发现：`collect --report-dir` 把运行结果与交付清单**两个 JSON 文档拼接**输出，任何机器消费者 `json.loads(stdout)` 直接失败 | 合并为单个文档（交付清单并入 `delivery` 键），文档同步更新 |

## 行为与契约变化（升级须知）

1. `CollectionResult.errors` 由 `list[str]` 改为 `list[CollectError]`（`code/message/url/retryable`），新增 `warnings`、`coverage`、`engine`。原先是裸类名字符串（实测 `["ValueError"]`）。
2. 快照覆盖率不再包含引擎，引擎改为按 coverage 固化；因此**换机器不再重置基线**，但固定引擎缺失时会 blocked 而非降级。
3. 偏差分级：影响实体集合的偏差（身份缺失、同 id 冲突、选择器零命中、已记录实体不再解析、预算截断）→ error 且快照不完整，不做删除推断；字段级偏差 → warning，仍保持可比。
4. `Report.inferences` 由 `list[str]` 改为 `list[models.Claim]`，并新增 `coverage/errors/warnings` 字段与"采集范围与运行告警"章节。
5. 推断的引用校验是严格的：任何一条引用无法与本次运行已存证据精确匹配（url + 定位），整条推断被丢弃，不做修剪。
6. 文本解码不再静默丢字符；损失性解码会出现在 warnings 中。
7. 429/5xx 与超时改为有界重试（默认 2 次）；401/403 仍然"拒绝即停"。`timeout`、`max_bytes`、`retries` 现可在配置中调整。
8. 原文留存是显式选项，**改变了原先"不持久化网页原文"的口径**：`--store-raw-html` 或 `store_raw_html=true` 才留存，且随快照/来源删除一并清理。
9. `planner.plan_with_provider` 删除（零调用点、一次性产出完整配置的路径不可核验）；模型能力收敛到"对已抓取页面提选择器提案"与"证据约束推断"两处。
10. 服务端不再限制在 macOS/Linux。
11. CLI 文件 I/O 固定 UTF-8：配置按 UTF-8 读取、demo 产物按 UTF-8 写出、stdout/stderr 显式 UTF-8。此前依赖区域编码，在 GBK/ASCII 代码页下会报错或静默乱码。
12. `collect --report-dir` 现在与引导任务走同一交付契约（配置了 provider 就生成受证据约束的推断），并只向 stdout 输出**单个** JSON 文档（交付清单并入 `delivery` 键）。

## 未验证与限制

- **真实搜索后端**：SearXNG 兼容接口只对回环 mock 验证过，未连真实实例；`OMNICRAWLER_SEARCH_URL/_TOKEN` 的正向路径属此类。
- **真实模型**：未配置任何真实模型账户，选择器提案与推断均以回环 mock 与假 provider 验证其契约（引用校验、失败降级），未评估提案与推断的质量。
- **Retry-After 的 HTTP-date 形式**只对解析器单测，没有端到端服务端发日期头的用例。
- **并发与规模**：仍为串行抓取；未验证高并发、百万页、多进程同库。
- **平台**：本机只实测 Windows + Python 3.12；macOS/Linux 与 3.13 由 CI 矩阵承担，本地未执行。
- **PDF**：依赖系统 CJK 字体；Linux 需 `fonts-noto-cjk`。Office 跨软件渲染未验收（仅结构与字体验证）。
- **语料是冻结快照**：`tools/record_corpus.py --refresh` 重录会改变 `fetched_at/sha256`，需同步调整 `test_corpus.py` 的 floor 与期望。

## 指标前 → 后

| 指标 | 基线 | 本次 |
| --- | --- | --- |
| 结论可回溯率 | 推断分区恒为空 | 每条推断都带命中已存证据的引用，否则整条不出现 |
| 抽取完整率 | 无此概念 | 报告给出 计划/实际抓取 与截断状态 |
| 站点改版存活率 | 单字段换类名 → 该页 0 记录 | 字段失效降级为 warning，其余字段与变更检测保留 |
| 真实页面覆盖 | 0 个真实页面 | 12 个真实页面 + 8 个脏数据变体，离线可复现 |
| 首次提出来源 | 必须自带 URL | 可提名（白名单 + 人工采纳后生效） |
| 跨平台 | Windows 14 项失败、服务不可用 | 201 项全通过 |

## 真实站点使用验证（非 fixture）

用真实授权公开页面走一遍产品路径，验证"能不能正常用"：

1. `collect` 抓 `https://docs.python.org/3/py-modindex.html`：状态 `succeeded`，**319 条记录**，每条含 url + 定位 + 片段 + 时间；报告写入 md/json，覆盖率显示 计划 1 / 实际 1 / 完整。
2. 同一配置第二次运行：走同一 coverage 的基线比较，`changes` 为空（页面未变），`previous_snapshot_id` 指向上一次。
3. `replay` 回放该快照的留存原文：exit 0，原文写 stdout、`url captured_at` 写 stderr；未留存的 URL exit 3 并给出原因。
4. 端到端综合验证（本地回环 mock 的 OpenAI 兼容端点）：模型收到 50 条记录 + 覆盖率摘要 + 已存在的 (url, 定位) 白名单；返回两条推断，一条引用真实定位、一条引用虚构定位。报告只渲染真实那条，并附完整来源依据；**虚构那条在报告中不存在**。这条链路此前在真实数据上从未跑过。

## 后续建议

1. 文档解析（PDF/DOCX 作为来源）与更多采样建议。
2. 并发抓取与告警通知（当前串行、仅本地计数）。
3. 为选择器提案与推断补评估集（真实模型账户），把"提案接受率""推断可用率"变成可回归指标。
4. 若要发布：确认主项目与第三方许可、远程归属与素材范围（`docs/third-party-dependencies.md` 已有登记）。
