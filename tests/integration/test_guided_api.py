import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient

from omnicrawler import api
from omnicrawler.service import TaskService
from omnicrawler.session import SessionStore


def answers(origin, kind="once"):
    return {
        "task_type": kind,
        "goal": "价格采集",
        "source": [origin + "/products"],
        "scope": ["127.0.0.1"],
        "fields": {"price": ""},
        "frequency": 60 if kind == "monitor" else None,
        "deliverable_formats": ["md", "json", "docx"],
    }


def wait_task(client, identifier, headers):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        task = client.get(f"/tasks/{identifier}", headers=headers).json()
        if task["status"] not in {"queued", "running"}:
            return task
        time.sleep(0.02)
    raise AssertionError("Task did not finish")


def test_rest_guidance_to_report_and_source_deletion(site, tmp_path):
    origin, _ = site
    headers = {"X-API-Key": "test-key"}
    with TestClient(
        api.create_app(
            tmp_path / "db.sqlite",
            "test-key",
            allowed_hosts={"127.0.0.1"},
            local_origins={origin},
        )
    ) as client:
        assert client.post("/sessions").status_code == 401
        sid = client.post("/sessions", headers=headers).json()["id"]
        response = client.post(
            f"/sessions/{sid}/answer",
            headers=headers,
            json={"answers": answers(origin)},
        )
        assert response.status_code == 200, response.text
        assert (
            client.post(
                f"/sessions/{sid}/confirm",
                headers=headers,
                json={"revision": response.json()["revision"]},
            ).status_code
            == 409
        )
        preview = client.post(f"/sessions/{sid}/preview", headers=headers).json()
        assert preview["draft"]["preview"]["samples"]
        draft = client.post(
            f"/sessions/{sid}/answer",
            headers=headers,
            json={"answers": {"accept_preview": True}},
        ).json()
        assert (
            client.get(f"/sessions/{sid}/draft", headers=headers).json()["unknowns"]
            == []
        )
        response = client.post(
            f"/sessions/{sid}/confirm",
            headers=headers,
            json={"revision": draft["revision"]},
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["task_id"]
        task = wait_task(client, task_id, headers)
        assert task["status"] == "succeeded", task
        assert Path(task["result"]["delivery"]["files"]["docx"]).is_file()
        assert client.get(f"/tasks/{task_id}/report?format=json").status_code == 401
        report = client.get(f"/tasks/{task_id}/report?format=json", headers=headers)
        assert report.status_code == 200
        assert report.json()["task_type"] == "once"
        assert report.json()["summary"]["records"] == 2
        assert (
            client.get(
                f"/tasks/{task_id}/report?format=md", headers=headers
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/tasks/{task_id}/report?format=exe", headers=headers
            ).status_code
            == 422
        )
        assert (tmp_path / "reports" / task_id / "report.docx").exists()
        client.post(
            "/data/delete", headers=headers, json={"source_url": origin + "/products"}
        )
        assert not (tmp_path / "reports" / task_id).exists()
        assert (
            client.get(
                f"/tasks/{task_id}/report?format=json", headers=headers
            ).status_code
            == 409
        )


def test_monitor_periodic_runs_inherit_delivery_contract(site, tmp_path):
    origin, _ = site
    manager = TaskService(tmp_path / "db.sqlite")
    sessions = SessionStore(
        manager, allowed_hosts={"127.0.0.1"}, local_origins={origin}
    )
    value = answers(origin, "monitor")
    value.update(
        fields={"price": ".price"}, item_selector="article", id_attribute="data-id"
    )
    sid = sessions.start()["id"]
    draft = sessions.answer(sid, value)
    confirmed = sessions.confirm(sid, draft["revision"], actor="test")
    asyncio.run(manager.run(confirmed["task_id"]))
    first = manager.get(confirmed["task_id"])
    assert first["result"]["delivery"]["files"]["md"]
    manager.trigger_schedule(confirmed["schedule_id"])
    task_id = manager.list_tasks()[0]["id"]
    assert task_id != confirmed["task_id"]
    asyncio.run(manager.run(task_id))
    report = asyncio.run(manager.generate_report(task_id, formats=["json"]))
    import json
    from pathlib import Path

    data = json.loads(Path(report["files"]["json"]).read_text())
    assert data["task_type"] == "monitor"
    assert data["schedule_seconds"] == 60


def test_scheduled_task_and_delivery_are_published_atomically(
    site, tmp_path, monkeypatch
):
    origin, _ = site
    manager = TaskService(tmp_path / "db.sqlite")
    sessions = SessionStore(
        manager, allowed_hosts={"127.0.0.1"}, local_origins={origin}
    )
    value = answers(origin, "monitor")
    value.update(
        fields={"price": ".price"}, item_selector="article", id_attribute="data-id"
    )
    sid = sessions.start()["id"]
    draft = sessions.answer(sid, value)
    result = sessions.confirm(sid, draft["revision"], actor="test")
    manager.cancel(result["task_id"])
    original = manager.enqueue
    observed = []

    def observe_enqueue(*args, **kwargs):
        task_id = original(*args, **kwargs)
        with manager.store._connection() as connection:
            task = connection.execute(
                "SELECT id FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            contract = connection.execute(
                "SELECT task_id FROM task_deliveries WHERE task_id=?", (task_id,)
            ).fetchone()
        observed.append(bool(task) and not bool(contract))
        return task_id

    monkeypatch.setattr(manager, "enqueue", observe_enqueue)
    manager.trigger_schedule(result["schedule_id"])
    assert observed == [False], (
        "Worker could observe queued task without report contract"
    )


def test_source_deletion_also_removes_session_preview_samples(site, tmp_path):
    origin, _ = site
    manager = TaskService(tmp_path / "db.sqlite")
    sessions = SessionStore(
        manager, allowed_hosts={"127.0.0.1"}, local_origins={origin}
    )
    sid = sessions.start()["id"]
    sessions.answer(sid, answers(origin))
    asyncio.run(sessions.preview(sid))
    assert sessions.get(sid)["draft"]["preview"]["samples"]
    manager.store.delete_scope(source_url=origin + "/products")
    assert sessions.get(sid)["draft"]["preview"] is None


def test_failed_report_file_deletion_keeps_snapshot_retryable(site, tmp_path):
    import os

    import pytest

    origin, _ = site
    manager = TaskService(tmp_path / "db.sqlite")
    sessions = SessionStore(
        manager, allowed_hosts={"127.0.0.1"}, local_origins={origin}
    )
    sid = sessions.start()["id"]
    value = answers(origin)
    value.update(
        fields={"price": ".price"}, item_selector="article", id_attribute="data-id"
    )
    draft = sessions.answer(sid, value)
    confirmed = sessions.confirm(sid, draft["revision"], actor="test")
    asyncio.run(manager.run(confirmed["task_id"]))
    result = manager.get(confirmed["task_id"])["result"]
    directory = manager.report_root / confirmed["task_id"]
    os.chmod(directory, 0o555)
    try:
        with pytest.raises(OSError):
            manager.store.delete(result["snapshot_id"])
        assert manager.store.bundle(result["snapshot_id"])
        assert manager.get(confirmed["task_id"])["result"]
    finally:
        os.chmod(directory, 0o755)
    manager.store.delete(result["snapshot_id"])
    assert not directory.exists()


def test_preview_is_subject_to_default_retention(site, tmp_path):
    import json

    origin, _ = site
    manager = TaskService(tmp_path / "db.sqlite")
    sessions = SessionStore(
        manager, allowed_hosts={"127.0.0.1"}, local_origins={origin}
    )
    sid = sessions.start()["id"]
    sessions.answer(sid, answers(origin))
    asyncio.run(sessions.preview(sid))
    with manager.store._connection() as conn:
        raw = conn.execute(
            "SELECT preview FROM guidance_sessions WHERE id=?", (sid,)
        ).fetchone()[0]
        data = json.loads(raw)
        data["captured_at"] = "2020-01-01T00:00:00+00:00"
        conn.execute(
            "UPDATE guidance_sessions SET preview=? WHERE id=?", (json.dumps(data), sid)
        )
    manager.cleanup()
    assert sessions.get(sid)["draft"]["preview"] is None


def test_report_output_failure_does_not_reverse_collection(site, tmp_path):
    origin, _ = site
    manager = TaskService(tmp_path / "db.sqlite")
    sessions = SessionStore(
        manager, allowed_hosts={"127.0.0.1"}, local_origins={origin}
    )
    sid = sessions.start()["id"]
    value = answers(origin)
    value.update(
        fields={"price": ".price"}, item_selector="article", id_attribute="data-id"
    )
    draft = sessions.answer(sid, value)
    confirmed = sessions.confirm(sid, draft["revision"], actor="test")
    manager.report_root.write_text("occupied")
    asyncio.run(manager.run(confirmed["task_id"]))
    task = manager.get(confirmed["task_id"])
    assert task["status"] == "succeeded", task
    assert task["result"]["delivery"]["warnings"]
    assert manager.store.bundle(task["result"]["snapshot_id"])["records"]
