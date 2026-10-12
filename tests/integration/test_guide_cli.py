import asyncio
import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from omnicrawler.cli import run_guide


@contextmanager
def search_backend(results):
    """A minimal SearXNG-shaped backend so the nomination path stays offline."""
    payload = json.dumps({"results": [{"url": url} for url in results]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_cli_guidance_interactive_resume_and_confirmation(site, tmp_path):
    origin, _ = site
    # A private fixture can only be enabled by local configuration in a saved
    # session; the public CLI deliberately has no allow-private switch.
    from omnicrawler.service import TaskService
    from omnicrawler.session import SessionStore

    manager = TaskService(tmp_path / "db.sqlite")
    sessions = SessionStore(
        manager, allowed_hosts={"127.0.0.1"}, local_origins={origin}
    )
    sid = sessions.start()["id"]
    sessions.answer(
        sid,
        {
            "task_type": "once",
            "goal": "价格",
            "source": [origin + "/products"],
            "fields": {"price": ".price"},
            "scope": ["127.0.0.1"],
            "item_selector": "article",
            "id_attribute": "data-id",
            "deliverable_formats": ["md", "json"],
        },
    )
    # Declining execution is a real CLI path and needs no private transport.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnicrawler",
            "guide",
            "--db",
            str(tmp_path / "db.sqlite"),
            "--session-id",
            sid,
            "--allow-host",
            "127.0.0.1",
        ],
        input="n\n",
        text=True,
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "price" in result.stdout
    assert "127.0.0.1" in result.stdout
    assert not manager.list_tasks()
    assert sessions.get(sid)["state"] == "confirming"


def test_collect_can_deliver_report_formats(site, tmp_path):
    origin, _ = site
    config = {
        "goal": "价格",
        "urls": [origin + "/products"],
        "allowed_hosts": ["127.0.0.1"],
        "local_origins": [origin],
        "item_selector": "article",
        "id_attribute": "data-id",
        "fields": {"price": ".price"},
        "delay": 0,
        "engine": "html",
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnicrawler",
            "collect",
            str(path),
            "--db",
            str(tmp_path / "db.sqlite"),
            "--report-dir",
            str(tmp_path / "deliverables"),
            "--format",
            "md",
            "--format",
            "json",
        ],
        text=True,
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "deliverables" / "report.md").exists()
    report = json.loads((tmp_path / "deliverables" / "report.json").read_text())
    assert report["summary"]["records"] == 2


def test_full_cli_dialogue_executes_only_after_confirmation(
    site, tmp_path, monkeypatch
):
    origin, _ = site
    replies = iter(
        ["once", "价格", origin + "/products", "127.0.0.1", "price", "y", "y", "y"]
    )
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))
    value = asyncio.run(
        run_guide(tmp_path / "db.sqlite", {"127.0.0.1"}, local_origins={origin})
    )
    assert value["state"] == "done"
    identifier = value["confirmation"]["task_id"]
    assert (tmp_path / "reports" / identifier / "report.md").exists()


def test_guide_cli_nominates_sources_only_with_a_backend(tmp_path, monkeypatch):
    candidate = "http://127.0.0.1:9/product"
    with search_backend([candidate, "https://evil.example/x"]) as backend:
        monkeypatch.setenv("OMNICRAWLER_SEARCH_URL", backend)
        replies = iter(["once", "价格", "y", "y", "127.0.0.1", "exit"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(replies))
        value = asyncio.run(run_guide(tmp_path / "db.sqlite", {"127.0.0.1"}))

    # Accepting a nomination is what makes the candidate a source; the rejected
    # candidate stays visible instead of quietly disappearing.
    assert value["answers"]["source"] == [candidate]
    assert value["nominations"]["candidates"] == [candidate]
    assert value["nominations"]["goal"] == "价格"
    assert value["nominations"]["rejected"][0]["code"] == "search_candidate_rejected"


def test_guide_cli_asks_nothing_extra_without_a_search_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNICRAWLER_SEARCH_URL", raising=False)
    monkeypatch.delenv("OMNICRAWLER_SEARCH_TOKEN", raising=False)
    prompts = []

    def reply(prompt):
        prompts.append(prompt)
        return "exit"

    monkeypatch.setattr("builtins.input", reply)
    value = asyncio.run(run_guide(tmp_path / "db.sqlite", {"127.0.0.1"}))

    assert value["nominations"] is None
    assert not any("提名" in prompt for prompt in prompts)
