# 第三方依赖

主项目原创实现以 Apache-2.0 发布。下列为 2026-10-11 本地锁定环境中直接运行依赖的版本与包元数据许可声明；不替代包内 LICENSE/NOTICE，也不表示所有传递依赖或浏览器二进制已完成审查。

| 包 | 版本 | 包声明许可 |
| --- | --- | --- |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT |
| APScheduler | 3.11.3 | MIT |
| beautifulsoup4 | 4.15.0 | MIT |
| FastAPI | 0.143.0 | MIT |
| MCP Python SDK | 1.30.0 | MIT |
| Playwright Python | 1.63.0 | Apache-2.0 |
| python-docx | 1.2.0 | MIT |
| Pydantic | 2.14.0 | MIT |
| Uvicorn | 0.54.0 | BSD-3-Clause |
| Crawl4AI（可选） | 0.9.4 | Apache-2.0 |

依赖由 uv 按 uv.lock 安装，源码和浏览器二进制未纳入本仓库或项目构建产物。重新分发包含依赖的镜像、可执行程序或浏览器时，应保留对应许可及通知，并针对实际组合核查传递依赖。

config/upstreams.json 是研究候选清单，不是已集成依赖清单。Firecrawl 的 AGPL-3.0 源码未纳入当前实现；后续接入或分发须按实际目录和组合重新审查。用户提供的第三方参考原稿、归档副本及采集数据不在公开提交范围内，也不受主项目许可授权。

## 可选的外部适配器（进程外，不随本仓库分发）

`omnicrawler source` 可把外部工具当作取数来源，通过各自公开接口调用，不链接、不再分发其代码；未安装时该子命令不可用，其余功能不受影响。

| 适配器 | 上游 | 上游许可 | 接入方式 | 状态 |
| --- | --- | --- | --- | --- |
| xhs | xpzouying/xiaohongshu-mcp | Apache-2.0 | 进程外 MCP over HTTP（默认 `http://127.0.0.1:18060/mcp`） | 2026-10-12 已对 v2.5.5（windows-amd64，sha256 校验）真实实例验证：自有账号 26 篇笔记枚举、逐篇详情证据、`limit` 截断与字段缺失告警、连续两次运行 0 变化 |

已评估但**未接入**：NanmiCoder/MediaCrawler（66.7k star，许可为自定义的 NON-COMMERCIAL LEARNING LICENSE 1.1，明文禁止商用与大规模采集，与本项目的 Apache-2.0 定位不兼容）、JoeanAmier/XHS-Downloader（GPL-3.0，链接会传染）。两者仅作研究记录，源码与产物均未纳入。

报告测试使用 pypdf 6.20.0（BSD-3-Clause）验证 PDF 中文文本；它只属于开发依赖。Linux PDF 部署需系统 Noto CJK 字体（SIL Open Font License 1.1）；字体不随本项目分发。

## 文档设计参考（非运行时依赖）

2026-10-12 整合 MiniMax minimax-pdf（MIT）、jamditis document-design（MIT）、Anthropic frontend-design（Apache-2.0）的设计模式。原件与许可随仓库保存于 docs/references/document-design，固定提交与校验值见 manifest.json；萃取版使用项目自己的中文 A4 离线实现。没有执行上游脚本或纳入其运行时。
