import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from omnicrawler.models import SearchConfig
from omnicrawler.search import search_candidates, search_from_env, search_sources
from omnicrawler.service import TaskService
from omnicrawler.session import SessionStore


class SearchBackend:
    """A loopback SearXNG-shaped JSON endpoint that records what it was asked."""

    def __init__(self, results, *, status=200, body=None):
        self.results = results
        self.status = status
        self.body = body
        self.requests = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                state.requests.append((self.path, self.headers.get("Authorization")))
                payload = state.body
                if payload is None:
                    payload = json.dumps(
                        {"results": [{"url": url} for url in state.results]}
                    ).encode()
                elif isinstance(payload, str):
                    payload = payload.encode()
                self.send_response(state.status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def backend():
    servers = []

    def build(results, **kwargs):
        server = SearchBackend(results, **kwargs)
        servers.append(server)
        return server

    yield build
    for server in servers:
        server.close()


def test_configured_backend_produces_the_exact_candidate_list(backend):
    server = backend(["http://127.0.0.1:9/a", "http://127.0.0.1:9/b"])
    urls = asyncio.run(
        search_sources(
            goal="价格",
            config=SearchConfig(endpoint=server.origin, token="search-token"),
            allowed_hosts={"127.0.0.1"},
        )
    )
    assert urls == ["http://127.0.0.1:9/a", "http://127.0.0.1:9/b"]
    assert len(server.requests) == 1
    path, authorization = server.requests[0]
    assert urlsplit(path).path == "/search"
    assert parse_qs(urlsplit(path).query) == {"q": ["价格"], "format": ["json"]}
    assert authorization == "Bearer search-token"


def test_query_template_dedupe_and_max_results_bound_the_nomination(backend):
    server = backend(
        [
            "http://127.0.0.1:9/a",
            "http://127.0.0.1:9/a",
            "http://127.0.0.1:9/b",
            "https://evil.example/x",
        ]
    )
    accepted, rejected = asyncio.run(
        search_candidates(
            goal="价格",
            config=SearchConfig(
                endpoint=server.origin,
                query_template="site:shop.example {goal}",
                max_results=1,
            ),
            allowed_hosts={"127.0.0.1"},
        )
    )
    assert accepted == ["http://127.0.0.1:9/a"]
    assert [item.url for item in rejected] == [
        "https://evil.example/x",
        "http://127.0.0.1:9/b",
    ]
    assert [item.code for item in rejected] == [
        "search_candidate_rejected",
        "search_candidate_truncated",
    ]
    query = parse_qs(urlsplit(server.requests[0][0]).query)
    assert query["q"] == ["site:shop.example 价格"]


def test_candidates_with_credentials_or_other_schemes_are_rejected(backend):
    server = backend(
        [
            "file:///etc/passwd",
            "http://user:secret@127.0.0.1/x",
            "/relative",
            "http://127.0.0.1:9/ok",
        ]
    )
    accepted, rejected = asyncio.run(
        search_candidates(
            goal="价格",
            config=SearchConfig(endpoint=server.origin),
            allowed_hosts={"127.0.0.1"},
        )
    )
    assert accepted == ["http://127.0.0.1:9/ok"]
    assert [item.url for item in rejected] == [
        "file:///etc/passwd",
        "http://user:secret@127.0.0.1/x",
        "/relative",
    ]


def test_broken_backends_fail_clearly_instead_of_returning_an_empty_list(backend):
    with pytest.raises(ValueError, match="configured search backend"):
        asyncio.run(search_sources(goal="价格", config=None, allowed_hosts=set()))
    with pytest.raises(ValueError, match="requires an endpoint"):
        asyncio.run(
            search_sources(goal="价格", config=SearchConfig(), allowed_hosts=set())
        )
    refused = backend([], status=500)
    with pytest.raises(ValueError, match="refused or failed"):
        asyncio.run(
            search_sources(
                goal="价格",
                config=SearchConfig(endpoint=refused.origin),
                allowed_hosts={"127.0.0.1"},
            )
        )
    html = backend([], body="<html>not json</html>")
    with pytest.raises(ValueError, match="JSON envelope"):
        asyncio.run(
            search_sources(
                goal="价格",
                config=SearchConfig(endpoint=html.origin),
                allowed_hosts={"127.0.0.1"},
            )
        )
    malformed = backend([], body=json.dumps({"results": [{"title": "no url"}]}))
    with pytest.raises(TypeError, match="missing a URL string"):
        asyncio.run(
            search_sources(
                goal="价格",
                config=SearchConfig(endpoint=malformed.origin),
                allowed_hosts={"127.0.0.1"},
            )
        )
    out_of_scope = backend(["https://evil.example/x"])
    with pytest.raises(ValueError, match="no source inside the authorized allowlist"):
        asyncio.run(
            search_sources(
                goal="价格",
                config=SearchConfig(endpoint=out_of_scope.origin),
                allowed_hosts={"127.0.0.1"},
            )
        )


def test_search_config_from_env_mirrors_the_provider(monkeypatch):
    monkeypatch.delenv("OMNICRAWLER_SEARCH_URL", raising=False)
    monkeypatch.delenv("OMNICRAWLER_SEARCH_TOKEN", raising=False)
    assert search_from_env() is None
    monkeypatch.setenv("OMNICRAWLER_SEARCH_URL", "https://searx.example")
    monkeypatch.setenv("OMNICRAWLER_SEARCH_TOKEN", "token")
    assert search_from_env() == SearchConfig(
        endpoint="https://searx.example", token="token"
    )
    monkeypatch.delenv("OMNICRAWLER_SEARCH_URL")
    with pytest.raises(ValueError, match="OMNICRAWLER_SEARCH_URL"):
        search_from_env()


def test_session_nomination_does_not_fetch_or_authorize_until_accepted(
    site, tmp_path, backend
):
    origin, state = site
    server = backend([origin + "/products", "https://evil.example/x"])
    sessions = SessionStore(
        TaskService(tmp_path / "db.sqlite"),
        allowed_hosts={"127.0.0.1"},
        local_origins={origin},
        search=SearchConfig(endpoint=server.origin),
    )
    sid = sessions.start()["id"]
    value = asyncio.run(
        sessions.nominate(
            sid,
            {
                "task_type": "once",
                "goal": "价格",
                "fields": {"price": ".price"},
            },
        )
    )
    assert value["nominations"]["candidates"] == [origin + "/products"]
    assert value["draft"]["nominations"]["candidates"] == [origin + "/products"]
    assert [item["url"] for item in value["nominations"]["rejected"]] == [
        "https://evil.example/x"
    ]
    assert "source" not in value["answers"]
    assert state["hits"] == []
    with pytest.raises(ValueError):
        sessions.confirm(sid, value["revision"], actor="test")
    assert state["hits"] == []

    value = sessions.answer(sid, {"accept_nomination": True})
    assert value["answers"]["source"] == [origin + "/products"]
    assert "scope" in value["draft"]["unknowns"]
    assert "evil.example" not in json.dumps(value["draft"]["sources"])
    assert "evil.example" not in json.dumps(value["draft"]["allowed_hosts"])
    assert state["hits"] == []

    value = sessions.answer(
        sid,
        {
            "scope": ["127.0.0.1"],
            "item_selector": "article[data-id]",
            "id_attribute": "data-id",
        },
    )
    result = sessions.confirm(sid, value["revision"], actor="test")
    audited = json.dumps(sessions.audit(sid))
    assert "evil.example" not in audited
    assert sessions.audit(sid)[0]["scope"]["urls"] == [origin + "/products"]
    assert state["hits"] == []
    asyncio.run(sessions.manager.run(result["task_id"]))
    assert "/products" in state["hits"]


def test_nominations_survive_restart_for_later_explicit_acceptance(
    site, tmp_path, backend
):
    origin, _ = site
    server = backend([origin + "/products"])
    sessions = SessionStore(
        TaskService(tmp_path / "db.sqlite"),
        allowed_hosts={"127.0.0.1"},
        local_origins={origin},
        search=SearchConfig(endpoint=server.origin),
    )
    sid = sessions.start()["id"]
    asyncio.run(sessions.nominate(sid, {"task_type": "once", "goal": "价格"}))

    resumed = SessionStore(
        TaskService(tmp_path / "db.sqlite"),
        allowed_hosts={"127.0.0.1"},
        local_origins={origin},
    )
    assert resumed.get(sid)["nominations"]["candidates"] == [origin + "/products"]
    assert resumed.answer(sid, {"accept_nomination": True})["answers"]["source"] == [
        origin + "/products"
    ]


def test_nomination_without_a_backend_is_refused_before_any_write(tmp_path):
    sessions = SessionStore(
        TaskService(tmp_path / "db.sqlite"), allowed_hosts={"127.0.0.1"}
    )
    sid = sessions.start()["id"]
    with pytest.raises(ValueError, match="No search backend configured"):
        asyncio.run(sessions.nominate(sid, {"task_type": "once", "goal": "价格"}))
    value = sessions.get(sid)
    assert value["revision"] == 0
    assert value["nominations"] is None
    assert "source" not in value["answers"]
