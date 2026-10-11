# 文档设计整合验收（2026-10-12）

- 选中三份开放许可 skills：MiniMax minimax-pdf、jamditis document-design、Anthropic frontend-design。固定 commit、SHA-256 与许可副本保存在 docs/references/document-design。
- 萃取为项目技能 `.agents/skills/report-design/SKILL.md`，由 AGENTS.md 触发；共享设计变量实际应用于 Word/PDF，不只保存提示文本。
- Word 前置汇总表、重复表头、单份记录正文；PDF 去装饰序号、统一边距、短字段双列、网址来源成组、前后变化只展示差异字段、争议信息单列。
- 技能结构验证通过；全部上游文件校验值一致；wheel 保留三份上游许可证；sdist/wheel 构建通过。
- 报告专项测试21项通过，覆盖中文文本、脱敏、证据、字体、前置表格、删除失败、格式降级和差异精简。
- 调研/模拟原始JSON逐字节保持不变，重导出无格式降级告警。模拟报告13页、调研报告10页，大字号优先，不按页数缩字。
- 已看过两份报告全部页面的布局总览，并放大检查代表性页和末页，修正孤立URL和重复证据。Word验证限于结构与字体属性，未做Office跨软件渲染验收。

最终完整回归：114 passed（34.33s）；Ruff 检查与39个Python文件格式检查通过。独立只读复核通过，非阻塞建议（上下边距与英文装饰）已处理。
