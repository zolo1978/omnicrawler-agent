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
| Pydantic | 2.14.0 | MIT |
| Uvicorn | 0.54.0 | BSD-3-Clause |
| Crawl4AI（可选） | 0.9.4 | Apache-2.0 |

依赖由 uv 按 uv.lock 安装，源码和浏览器二进制未纳入本仓库或项目构建产物。重新分发包含依赖的镜像、可执行程序或浏览器时，应保留对应许可及通知，并针对实际组合核查传递依赖。

config/upstreams.json 是研究候选清单，不是已集成依赖清单。Firecrawl 的 AGPL-3.0 源码未纳入当前实现；后续接入或分发须按实际目录和组合重新审查。用户提供的第三方参考原稿、归档副本及采集数据不在公开提交范围内，也不受主项目许可授权。
