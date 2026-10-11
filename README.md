# OmniCrawler Agent

可本地运行的数据采集、来源证据和变化监控首版。支持受控 HTTP 获取、Crawl4AI 内容解析、Playwright 内联脚本渲染、HTML 字段抽取、SQLite 快照及差异、定时计划、REST 与 MCP stdio。

```sh
uv sync --locked --extra crawl4ai --python 3.12
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browser-cache" uv run --extra crawl4ai playwright install chromium
uv run --extra crawl4ai omnicrawler demo --output data/demo
uv run --extra crawl4ai pytest -q
```

演示连续采集本地授权站点，验证新增、改价与删除，输出带来源证据的快照和报告。实际网站必须明确授权范围、URL 和字段定位；本版未接入真实 LLM/搜索服务，不能只凭一句话完成全网调研。

- [运行文档与能力边界](docs/usage.md)
- [示例配置](examples/collection.json)
- [项目规划](docs/plans/2026-10-11-omnicrawler-roadmap.md)
- [执行记录](docs/plans/2026-10-11-foundation-execution.md)
- [许可证决策](docs/adr/0001-licensing.md)
- [参考素材说明](docs/references/README.md)
- [贡献流程](CONTRIBUTING.md)

GitHub 开源仓库：[zolo1978/omnicrawler-agent](https://github.com/zolo1978/omnicrawler-agent)。原创代码和文档采用 [Apache-2.0](LICENSE)，版权声明见 [NOTICE](NOTICE)，第三方依赖说明见 [依赖清单](docs/third-party-dependencies.md)。上游组件及参考材料保留各自许可或权利。源码研究副本位于被忽略的 external/upstream/，版本记录见 config/upstreams.json。第三方参考原稿、归档副本、采集数据及本地工具状态不纳入 Git 提交，构建包不包含这些副本。
