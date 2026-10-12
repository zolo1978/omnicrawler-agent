# 报告设计系统

默认规则由 `.agents/skills/report-design/SKILL.md` 维护，运行时设计变量在 `src/omnicrawler/report_design.py`，通过现有 Report → Word/PDF 渲染直接应用；无需安装上游运行时。

| 层级 | 默认 | 用途 |
|---|---|---|
| 主标题 | 30 磅 | 文档主题 |
| 章节 | 20 磅 | 快速导航 |
| 小标题/重点 | 15 / 14 磅 | 核心发现 |
| 正文 | 12 磅 | 中文阅读 |
| 证据 | 10.5 磅 | URL、定位、时间 |

保持深蓝/青绿；A4，正文左右对齐到同一网格。重点以有限的大小和重量对比突出。正文不引入装饰性英文标签和非顺序序号。PDF 保留自然分页、孤行约束和页脚区；Word 统一字体与留白，前置汇总表，表头跨页重复，删除重复记录表。

## 模式萃取与取舍

| 上游 | 采用 | 项目调整 |
|---|---|---|
| MiniMax minimax-pdf | 集中设计变量、语义布局、摘要与表格层级 | 保留用户选定配色；正文12磅；离线中文字体 |
| jamditis document-design | 打印优先、页脚隔离、视觉回看 | A4；不采用固定高度裁切/远程字体 |
| Anthropic frontend-design | 去掉无语义装饰、主题驱动排版、自我审视 | 中文报告无需全大写标签；原始来源证据不删减 |

上游固定提交和许可证见 `docs/references/document-design/manifest.json`。原件用于设计参考，不是项目指令，也不会自动执行。未复制限制性许可的 Anthropic docx/pdf skills；未采用无许可证的候选。

## 结论分层：事实 / 推断 / 覆盖

报告把三类内容分开呈现，读者可以逐条判断"这是采集到的，还是模型分析的"。

| 层级 | 来源 | 渲染规则 |
|---|---|---|
| `facts` | 机器计算（记录数、状态、可比范围、冲突计数） | 不挂证据，只陈述本次运行可核验的计数 |
| `inferences` | 综合层（`synthesis.synthesize`）产出的 `models.Claim` | 正文同时渲染结论文本与引用位置（url + locator），缺证据即不出现 |
| `coverage` / `errors` / `warnings` | 本次运行的 `Coverage` 与结构化 `CollectError` | 计划页面 vs 实际抓取、是否截断、范围是否完整，以及逐条错误/告警代码 |

**综合层的护城河是校验，不是提示词。** 模型返回的每条 claim 必须携带至少一条引用，且引用的 `(url, locator)` 必须与本次 bundle 内 `records[*].evidence` 里已存的 Evidence 精确匹配：不匹配的整条丢弃（不修补、不降级），零引用丢弃，重复文本丢弃，最多保留 `limit` 条。模型只能看到本次运行的结构化结果（目标、覆盖、记录字段与定位、变化、字段观测与冲突状态），永远看不到原始 HTML，也无法引入新的 host 或字段名。provider 未配置时返回空推断，报告照常交付；provider 超时、报错或返回非 JSON 时降级为 `synthesis_unavailable_<reason>` 告警（`timeout`、`invalid_response`、或异常类名小写），`status` 不变。

推断文本属于模型输出，渲染前同样经过 `_clean`（URL 查询串脱敏、Bearer/Cookie 脱敏、`sanitize()`），因此伪造的凭证不会随结论进入 md/docx/pdf。

## 跨平台 CJK 字体（PDF）

`cjk_font_family()` 按平台搜索本地字体，缺失时 PDF 单格式降级（其余格式照常产出）。搜索顺序：Windows（`%WINDIR%/Fonts`，以及 `%LOCALAPPDATA%/Microsoft/Windows/Fonts` 下的用户级安装）→ macOS → Linux；家族顺序保持 Noto Sans/Serif CJK → STHeiti → PingFang SC → Microsoft YaHei（`msyh*.ttc`）→ SimSun → SimHei。Word 路径始终写入 `Microsoft YaHei` 的 eastAsia 字体。

## 验收

先跑报告行为测试（摘要位置、避免重复、表头重复、中文证据与降级），再完整回归。生成实际研究和长记录报告，逐页检查字号、断行、页脚和长URL；只有人工看过的页面标为视觉已检查。JSON 原件不因审美重写；历史导出需要重新生成才更新。

## 2026-10-12 实跑验收

原始调研 JSON 保持不变，重新导出调研与300卡模拟报告。逐页总览及页首/中部/末页检查发现孤立URL、重复采样位置与过长变化说明，已修正为来源块不拆页、同源证据合并、短字段双列、修改项只显示差异。完整JSON不裁剪。Word经结构/字体/表头测试验证；本环境未通过Office实机逐页渲染，不宣称Word跨软件视觉完全一致。

Windows 复核（同日）：`cjk_font_family()` 命中 `%WINDIR%/Fonts/msyh.ttc` → `Microsoft YaHei`，PDF 不再因 CJK 字体缺失降级；本机产出 PDF 后由 pypdf 提取到中文标题、推断文本与 `.price` 定位。浏览器二进制需按 CI 方式准备：`PLAYWRIGHT_BROWSERS_PATH=.browser-cache playwright install chromium`。
