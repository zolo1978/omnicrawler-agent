import json
import subprocess
import sys


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
    import asyncio

    from omnicrawler.cli import run_guide

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
