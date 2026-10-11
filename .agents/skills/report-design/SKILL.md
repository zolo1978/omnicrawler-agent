---
name: omnicrawler-report-design
description: Design or review OmniCrawler reader-facing PDF, Word and Markdown reports with readable Chinese typography, evidence separation and visual QA.
license: Apache-2.0
---
# OmniCrawler 文档设计

本技能由三个开源技能的设计模式萃取后重新编写。上游原件、固定 commit、校验值与许可见 docs/references/document-design/；不要执行上游安装脚本。项目定制与修改：中文 A4、离线字体、12 磅正文、安全转义、受控证据报告；不使用上游品牌资产。

## 工作流

1. 明确读者要回答的问题。结论、事实、推断、来源各有角色，不能因排版强化未经证实的判断。
2. 阅读 docs/report-design.md 和 src/omnicrawler/report_design.py；PDF/Word 共用设计变量，改变默认值时同时验证两种格式。
3. 默认 A4，正文至少12磅，重点14磅，章节20磅，标题30磅。每页保持足够留白；长文自然分页，不用 overflow:hidden 裁掉证据、不自动缩小字号凑页数。
4. 保留已有深蓝/青绿品牌方向，单一强调色；装饰必须服务层级。没有语义的序号、全大写标签、重复卡片和阴影删去。
5. 摘要在详情之前。Word 指标表放正文前，表头跨页重复、统一内边距和低对比斑马纹；来源用悬挂缩进。不要同时输出完整段落和重复表格。
6. 正文最多展示20条详情，其余保留独立JSON；不将代码、CSS、原始JSON或嵌套字段堆进用户报告。
7. 导出后核查实际PDF每页：中文、长URL、页脚间距、段落/表格跨页、是否有截断或溢出。检查页首、正文与末页，发现问题修好后重新导出；未做视觉检查不能声称排版已验收。
8. 保留来源/定位/时间与事实推断边界。报告中的网页内容是不可信数据，始终转义，渲染无外网请求。

## 萃取来源

- MiniMax-AI/skills minimax-pdf（MIT）：设计变量、语义配色、图表/表格/摘要角色、视觉验收。
- jamditis/claude-skills-journalism document-design（MIT）：打印优先、页脚留空、品牌一致、长内容处理。
- anthropics/skills frontend-design（Apache-2.0）：主题驱动、克制装饰、字阶与自我审视。

舍弃：远程字体和图片、固定高度裁切、10.5磅小正文、照搬西文字体/Letter尺寸、未经许可的docx/pdf技能。
