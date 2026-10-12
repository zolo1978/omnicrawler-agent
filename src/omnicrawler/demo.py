"""Local authorized fixture demonstrating baseline, additions and deletion."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from omnicrawler.pipeline import CollectionConfig, collect
from omnicrawler.storage import SnapshotStore


async def run_demo(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    state = {"products": [("A", "79"), ("B", "20")]}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                body = b"User-agent: *\nAllow: /"
            else:
                body = (
                    "<html><body>"
                    + "".join(
                        f'<article data-id="{key}"><span class="price">{price}</span><span class="currency">USD</span></article>'
                        for key, price in state["products"]
                    )
                    + "</body></html>"
                ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    config = CollectionConfig(
        goal="监控产品价格及新品",
        urls=[origin + "/products"],
        allowed_hosts=["127.0.0.1"],
        local_origins=[origin],
        item_selector="article",
        id_attribute="data-id",
        fields={"price": ".price", "currency": ".currency"},
        delay=0,
    )
    store = SnapshotStore(output / "demo.sqlite")
    runs = []
    try:
        for products in [
            [("A", "79"), ("B", "20")],
            [("A", "89"), ("B", "20"), ("C", "129")],
            [("A", "89"), ("C", "129")],
        ]:
            state["products"] = products
            runs.append((await collect(config, store)).model_dump(mode="json"))
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join()
    report = {
        "verified": all(run["status"] == "succeeded" for run in runs)
        and [len(run["changes"]) for run in runs] == [0, 2, 1],
        "runs": runs,
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for index, run in enumerate(runs, 1):
        from omnicrawler.report import build_report, deliver, render_markdown

        canonical = build_report(
            store, run, task_type="monitor", title=f"OmniCrawler 演示：采集 {index}"
        )
        await deliver(
            canonical, output / "reports" / f"run-{index}", formats=["md", "json"]
        )
        (output / f"snapshot-{index}.json").write_text(
            json.dumps(store.bundle(run["snapshot_id"]), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
    canonical.facts.append(
        "本地授权演示连续采集三次，变化数量为 0 / 2 / 1；机器执行记录见 report.json，逐次规范报告见 reports/run-1 至 run-3。"
    )
    (output / "report.md").write_text(render_markdown(canonical), encoding="utf-8")
    return report
