import pytest

from omnicrawler.service import TaskService
from omnicrawler.session import SessionStore


def complete(kind="once"):
    return {
        "task_type": kind,
        "goal": "监控价格",
        "source": ["https://example.com/products"],
        "fields": {"price": ".price"},
        "item_selector": "article",
        "id_attribute": "data-id",
        "scope": ["example.com"],
        "frequency": 60 if kind == "monitor" else None,
        "deliverable_formats": ["md", "json"],
    }


def manager(tmp_path, hosts=("example.com",)):
    service = TaskService(tmp_path / "db.sqlite")
    return SessionStore(service, allowed_hosts=set(hosts)), service


def test_first_question_and_partial_answers_survive_restart(tmp_path):
    sessions, service = manager(tmp_path)
    session = sessions.start()
    assert session["state"] == "eliciting"
    assert session["questions"][0]["key"] == "task_type"
    value = sessions.answer(session["id"], {"task_type": "monitor", "goal": "监控价格"})
    assert "frequency" in [q["key"] for q in value["questions"]]
    recovered = SessionStore(service, allowed_hosts={"example.com"}).get(session["id"])
    assert recovered["answers"] == value["answers"]


@pytest.mark.parametrize(
    "answer",
    [
        {"task_type": "wrong"},
        {"frequency": 5},
        {"source": ["file:///etc/passwd"]},
        {"source": ["https://user:secret@example.com"]},
        {"goal": ""},
        {"bogus": True},
        {"fields": {"password": ".password"}},
    ],
)
def test_invalid_answers_are_rejected_without_mutating_session(tmp_path, answer):
    sessions, _ = manager(tmp_path)
    session = sessions.start()
    with pytest.raises(ValueError):
        sessions.answer(session["id"], answer)
    assert sessions.get(session["id"])["revision"] == 0


def test_unknowns_prevent_confirmation_and_new_answers_invalidate_draft(tmp_path):
    sessions, _ = manager(tmp_path)
    session = sessions.start()
    value = sessions.answer(
        session["id"],
        {
            "task_type": "once",
            "goal": "价格",
            "source": ["https://example.com"],
            "fields": {"price": ""},
        },
    )
    assert value["draft"]["unknowns"]
    with pytest.raises(ValueError):
        sessions.confirm(session["id"], value["revision"], actor="test")
    value = sessions.answer(session["id"], complete())
    old_revision = value["revision"]
    value = sessions.answer(session["id"], {"goal": "更新价格"})
    with pytest.raises(ValueError):
        sessions.confirm(session["id"], old_revision, actor="test")
    assert value["draft"]["goal"] == "更新价格"


@pytest.mark.parametrize("kind", ["once", "monitor"])
def test_confirmation_is_atomic_idempotent_and_auditable(tmp_path, kind):
    from concurrent.futures import ThreadPoolExecutor

    sessions, service = manager(tmp_path)
    session = sessions.start()
    value = sessions.answer(session["id"], complete(kind))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: sessions.confirm(
                    session["id"], value["revision"], actor="test-user"
                ),
                range(4),
            )
        )
    assert all(item == results[0] for item in results)
    assert len(service.list_tasks()) == 1
    assert len(service.schedules()) == (kind == "monitor")
    assert results[0]["task_id"]
    assert bool(results[0]["schedule_id"]) == (kind == "monitor")
    assert sessions.audit(session["id"])[0]["actor"] == "test-user"
    assert sessions.audit(session["id"])[0]["scope"]["allowed_hosts"] == ["example.com"]
    with pytest.raises(ValueError):
        sessions.answer(session["id"], {"goal": "alter after consent"})


def test_source_policy_cannot_expand_even_before_preview(tmp_path):
    sessions, _ = manager(tmp_path)
    session = sessions.start()
    with pytest.raises(ValueError):
        sessions.answer(session["id"], {"source": ["https://evil.com"]})


def test_task_type_change_clears_monitor_frequency_and_scope_change_needs_reapproval(
    tmp_path,
):
    sessions, _ = manager(tmp_path, ("example.com", "other.com"))
    session = sessions.start()
    value = sessions.answer(session["id"], complete("monitor"))
    value = sessions.answer(
        session["id"], {"task_type": "once", "source": ["https://other.com"]}
    )
    assert value["draft"]["schedule_seconds"] is None
    assert "scope" in value["draft"]["unknowns"]
