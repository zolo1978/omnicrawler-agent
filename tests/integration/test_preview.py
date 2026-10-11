import asyncio

import pytest

from omnicrawler.service import TaskService
from omnicrawler.session import SessionStore


def test_preview_is_safe_proposal_and_requires_explicit_acceptance(site, tmp_path):
    origin, state = site
    sessions = SessionStore(
        TaskService(tmp_path / "db.sqlite"),
        allowed_hosts={"127.0.0.1"},
        local_origins={origin},
    )
    session = sessions.start()
    value = sessions.answer(
        session["id"],
        {
            "task_type": "monitor",
            "goal": "价格变化",
            "source": [origin + "/products"],
            "scope": ["127.0.0.1"],
            "fields": {"price": ""},
            "frequency": 60,
        },
    )
    assert "item_selector" in value["draft"]["unknowns"]
    preview = asyncio.run(sessions.preview(session["id"]))
    assert preview["draft"]["preview"]["item_selector"] == "article[data-id]"
    assert preview["draft"]["preview"]["fields"] == {"price": ".price"}
    assert (
        preview["draft"]["preview"]["samples"][0]["evidence"]["price"]["url"]
        == origin + "/products"
    )
    assert preview["draft"]["unknowns"]
    value = sessions.answer(session["id"], {"accept_preview": True})
    assert value["draft"]["unknowns"] == []
    result = sessions.confirm(session["id"], value["revision"], actor="test")
    assert result["schedule_id"]
    assert state["hits"] == ["/robots.txt", "/products"]


def test_preview_requires_scope_and_respects_robots(site, tmp_path):
    origin, state = site
    sessions = SessionStore(
        TaskService(tmp_path / "db.sqlite"),
        allowed_hosts={"127.0.0.1"},
        local_origins={origin},
    )
    session = sessions.start()
    value = {
        "task_type": "once",
        "goal": "数据",
        "source": [origin + "/private"],
        "fields": {"price": ""},
    }
    sessions.answer(session["id"], value)
    with pytest.raises(ValueError):
        asyncio.run(sessions.preview(session["id"]))
    assert not state["hits"]
    sessions.answer(session["id"], {"scope": ["127.0.0.1"]})
    with pytest.raises(ValueError):
        asyncio.run(sessions.preview(session["id"]))
    assert state["hits"] == ["/robots.txt"]


def test_preview_selector_keeps_identity_filter_for_mixed_containers(site, tmp_path):
    origin, state = site
    state["extra_html"] = "<article><span class='price'>not a product</span></article>"
    sessions = SessionStore(
        TaskService(tmp_path / "db.sqlite"),
        allowed_hosts={"127.0.0.1"},
        local_origins={origin},
    )
    sid = sessions.start()["id"]
    sessions.answer(
        sid,
        {
            "task_type": "once",
            "goal": "价格",
            "source": [origin + "/products"],
            "scope": ["127.0.0.1"],
            "fields": {"price": ""},
            "deliverable_formats": ["md", "json"],
        },
    )
    preview = asyncio.run(sessions.preview(sid))
    assert preview["draft"]["preview"]["item_selector"] == "article[data-id]"
    value = sessions.answer(sid, {"accept_preview": True})
    result = sessions.confirm(sid, value["revision"], actor="test")
    asyncio.run(sessions.manager.run(result["task_id"]))
    assert sessions.manager.get(result["task_id"])["status"] == "succeeded"
