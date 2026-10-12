# 引导采集与多格式报告

## 安装与首次运行

```sh
uv sync --locked --extra crawl4ai --python 3.12
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browser-cache" uv run --extra crawl4ai playwright install chromium
uv run --extra crawl4ai omnicrawler guide --allow-host example.com
```

Linux PDF 需要 `fonts-noto-cjk`（Debian/Ubuntu：`sudo apt-get install fonts-noto-cjk`），Chromium 沙箱部署要求见 usage.md。macOS 优先使用可用的 STHeiti，再使用 PingFang。字体不存在时不输出缺字 PDF，而交付其他格式并返回 warning。

CLI 白名单由 `--allow-host` 指定，不从任意用户输入自动扩大。引导顺序：once/monitor → 目标 → 种子 URL（配置搜索后端时可用目标提名，见「来源提名」）→ 范围 → 字段 → 采样或手工定位 → 草案确认。字段名用逗号分隔；CSS 选择器可由预览提案补充。`exit`、EOF 或 Ctrl-C 保存草案；用相同 `--db` 和 `--session-id` 恢复。拒绝最终确认不会创建任务。

monitor 需要至少 10 秒的周期和稳定 ID。确认同时创建立即执行的基线任务与持久计划；CLI 返回后，需启动同一数据库、同一白名单的 `serve` 或 `mcp` 才持续检查。禁止多个进程同时执行同一数据库，guide/serve/mcp 使用单 worker 锁。

## REST 五步

所有会话、采样、确认和报告路径都要求 `X-API-Key`。服务启动白名单不可由请求扩大。启动示例：

```sh
# 在本地配置 OMNICRAWLER_API_KEY，勿将真实值提交
uv run --extra crawl4ai omnicrawler serve --allow-host example.com
```

1. `POST /sessions` → `id`、首问、revision=0。
2. `POST /sessions/{id}/answer`，请求体 `{"answers": {...}}`；可部分回答。
3. `POST /sessions/{id}/preview` 在来源范围明确确认后采样一个种子，返回候选 item/id/字段及至多三条样本证据。
4. `{"answers":{"accept_preview":true}}` 主动接受提案；`GET /sessions/{id}/draft` 回显计划。`POST /sessions/{id}/confirm` 请求 `{"revision":当前版本}`。修改后的旧版本、未知项或越权范围不能确认；重复确认同一版本返回相同 ID。
5. `GET /tasks/{task_id}` 轮询终态；`GET /tasks/{task_id}/report?format=md|docx|pdf|json` 获取报告。

一次性回答示例（源地址为占位，需替换为授权实际页面）：

```json
{
  "answers": {
    "task_type": "once",
    "goal": "整理产品价格",
    "source": ["https://example.com/products"],
    "fields": {"price": ""},
    "scope": ["example.com"],
    "deliverable_formats": ["md", "docx", "pdf", "json"]
  }
}
```

长期任务使用 `task_type=monitor` 与 `frequency=3600`。完整手工配置可追加 `item_selector`、`id_attribute`、非空字段选择器。`budget` 默认 100、最大 1000。一次性任务也需实体 ID，因为复用现有采集契约；预览无法识别稳定 ID 时会明确留下未知项。

`GET /sessions/{id}` 可恢复完整状态；审计在 SQLite 保存确认 actor、时间、版本及来源/字段/预算/频率。单租户 API actor 是 api-key-owner，未保存 API Key；MCP 为 local-mcp-client，CLI 为 local-cli-user，不提供多用户身份认证。

未就绪报告返回 409；请求不存在的格式返回 422；字体/依赖缺失导致指定格式无法交付时返回 503，并给出安全 warning。采集任务仍保留其原成功/partial/blocked 状态。

## 来源提名（可选搜索后端）

配置 `OMNICRAWLER_SEARCH_URL`（SearXNG 兼容 JSON 端点）启用来源提名，可选 `OMNICRAWLER_SEARCH_TOKEN` 作为 `Authorization: Bearer`；两者都不设置时行为与今天完全一致。启用后 `source` 一问可以直接用 goal 提名候选：REST 为 `POST /sessions/{id}/nominate`，MCP 为 `nominate_sources`，CLI 仅在配置了后端时提供提名提示。会话执行一次 `GET {endpoint}/search?q=<goal>&format=json`（`query_template` 可改写查询，`max_results` 限定候选数），并把结果持久化到会话视图的 `nominations`（含 `goal`、`candidates`、`rejected`、`captured_at`；`draft.nominations` 同样可见），用 `--session-id` 恢复后仍是同一批候选。

搜索只能**提名**，不能**授权**：

- 每个候选都要通过与服务端白名单相同的规则（绝对 http(s)、host 在白名单内、无凭据、去重、不超过 `max_results`）；越界候选进入 `nominations.rejected` 而不是被静默丢弃，若没有任何候选通过白名单则明确报错，不返回空列表。
- 候选不会自动写入 `answers.source`：人工回复 `{"accept_nomination": true}` 才生效（与 `accept_preview` 同一套 revision/确认语义），接受后范围（scope）必须重新确认，并照常经过采样与最终执行确认。
- 接受前不会向任何采集目标发起请求；搜索端点未配置、不可用、返回非 JSON 或全部候选越界时都是明确错误，而不是空候选。
- 与 provider 相互独立：未配置 provider 时提名仍可用，未配置搜索后端时 provider 行为不变。

## MCP

新增 `start_session`、`answer_questions`、`get_draft`、`nominate_sources`、`preview_session`、`confirm_session`、`task_report`。调用方 Agent 管理对话，OmniCrawler 提供受约束原语。`get_draft` 返回会话视图，含 revision/draft/未知项与已提名的 `nominations`；confirm 必须提交看到的版本。stdio 启动白名单保持固定。

## 报告阅读体验

设计规则由项目技能 `.agents/skills/report-design/SKILL.md` 维护；详见 [设计系统](report-design.md)。共享设计变量直接应用于 Word/PDF，汇总表前置、表头跨页重复，避免重复输出记录。

PDF / Word 使用 12 磅正文、约 30 磅主标题与 20 磅章节标题；关键发现放大，PDF 汇总指标使用数字卡片，配页码、留白与分块来源说明。

Markdown、Word、PDF 使用可读标签与正文，每类记录/变化/冲突最多展示前 20 条，并注明总量；不在正文嵌入原始 JSON。完整记录、前后证据及机器字段保留在独立 JSON 数据文件中。来源与采样证据仍可在正文中核查。

## 文件与留存

引导任务及其周期任务按确认格式自动写入数据库目录下 `reports/{task_id}/report.*`（默认 data/reports）。任务结果的 `delivery` 包含文件和 warning；直接任务默认仍按需生成 MD/JSON。

服务报告依赖当前及前驱快照，删除任一依赖会删除相关文件并使任务结果失效。文件删除失败时数据库回滚，调用失败且可修复权限后重试，不能把残留文件视为已删除。按来源/实体删除也清除会话预览；预览样本同样保留 90 天。授权记录和问卷配置用于恢复/审计，不自动认定为网页采样内容。

按需请求单个格式会保留该任务的确认格式集合；即使 Word/PDF 降级也保留 MD/JSON（若它们在确认格式中）。新采样不自动执行，拒绝访问不切换引擎。

独立命令也可导出：

```sh
uv run --extra crawl4ai omnicrawler collect examples/collection.json --report-dir data/exports/sample --format md --format json
```

这种显式导出及 demo 文件是用户自行管理的副本，不受服务报告关联清理；请自行删除导出/备份。demo 的 report.json 保留 verified/runs 机器执行记录，规范报告在 reports/run-1～run-3/report.json，report.md 使用统一 Report 模型。

## 自配 provider（可选）

未配置时不请求模型，使用确定性 CSS 采样提案。设置 `OMNICRAWLER_PROVIDER_URL`（完整 chat-completions JSON 端点）、`OMNICRAWLER_PROVIDER_MODEL`，可选 `OMNICRAWLER_PROVIDER_TOKEN`（仅本地环境保存），启用用户自配 JSON 服务。远程端点要求 HTTPS；本地开发允许明确配置的 HTTP loopback。服务凭据和端点不会写入会话、报告或 Git。

两阶段：从已提供且已授权的种子中提出候选 → 基于受控采样的确定性提案提出字段定位。请求发送目标、已选 URL/字段及候选选择器，不发送原始 HTML或完整网页。调用可能产生所配置服务的费用，由该服务账户管理；未配置时无模型调用。适配器验证 JSON schema、种子范围、字段集合及采样匹配，不跟随 provider 重定向，不读取环境代理；失败或越权仅安全告警并保留确定性提案。

模型提案仍需主动接受并最终确认。它不能添加 host/字段、改动频率或预算，也不参与来源发现：provider 只在已授权种子内定位字段。goal-only 的种子澄清走独立的搜索提名路径（见「来源提名」），未配置搜索后端时仍会询问 URL。provider 这段实现与受约束闭环，尚未使用外部真实模型账户评估提案质量。
