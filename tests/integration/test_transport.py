"""Bounded resilience: transient failures retry, refusals stay final."""

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from omnicrawler import pipeline, transport
from omnicrawler.policy import AccessDenied
from omnicrawler.storage import SnapshotStore

PRODUCTS = (
    '<html><body><article data-id="A">'
    '<span class="price">79</span></article></body></html>'
)


@pytest.fixture
def scripted_site():
    """Loopback server whose replies are scripted per path and per hit.

    ``script[path]`` is a list of ``(status, body, headers)`` consumed one entry per
    hit with the last entry repeating, so a test can make a path recover after a
    transient failure. ``slow_once[path]`` delays only the first hit, which provokes
    a real client timeout without leaving an endpoint permanently slow.
    """
    state = {"hits": [], "script": {}, "slow_once": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"].append(self.path)
            delay = state["slow_once"].pop(self.path, 0)
            if delay:
                time.sleep(delay)
            plan = state["script"].get(self.path) or [(200, "<html></html>", {})]
            status, body, headers = plan[0] if len(plan) == 1 else plan.pop(0)
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                # The client may have timed out and closed the socket already.
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()
    thread.join()


def fetcher(origin, **overrides):
    return transport.SafeFetcher(
        {"127.0.0.1"}, local_origins={origin}, delay=0, **overrides
    )


def configuration(origin, path, **overrides):
    base = {
        "urls": [origin + path],
        "allowed_hosts": ["127.0.0.1"],
        "local_origins": [origin],
        "item_selector": "article",
        "id_attribute": "data-id",
        "fields": {"price": ".price"},
        "delay": 0,
    }
    return pipeline.CollectionConfig(**{**base, **overrides})


def test_rate_limit_with_retry_after_is_retried_then_succeeds(scripted_site):
    origin, state = scripted_site
    state["script"]["/rate"] = [
        (429, "<html></html>", {"Retry-After": "0"}),
        (200, PRODUCTS, {}),
    ]

    page = asyncio.run(fetcher(origin).fetch(origin + "/rate"))

    assert page.status == 200
    assert "data-id" in page.html
    # Two hits on the same path prove the 429 was retried rather than being final.
    assert state["hits"].count("/rate") == 2


def test_exhausted_rate_limit_becomes_a_refusal(scripted_site):
    origin, state = scripted_site
    state["script"]["/rate"] = [(429, "<html></html>", {"Retry-After": "0"})]

    with pytest.raises(AccessDenied):
        asyncio.run(fetcher(origin, retries=1).fetch(origin + "/rate"))

    # Initial attempt plus exactly one retry, then the rate limit is a refusal.
    assert state["hits"].count("/rate") == 2


def test_exhausted_rate_limit_blocks_the_run(scripted_site, tmp_path):
    origin, state = scripted_site
    state["script"]["/rate"] = [(429, "<html></html>", {"Retry-After": "0"})]

    result = asyncio.run(
        pipeline.collect(
            configuration(origin, "/rate", retries=1),
            SnapshotStore(tmp_path / "db.sqlite"),
        )
    )

    # A sustained rate limit is a refusal once the retry budget is gone, and a refusal
    # stops the task rather than being silently worked around.
    assert result.status == "blocked"
    assert [error.code for error in result.errors] == ["access_denied"]
    assert state["hits"].count("/rate") == 2


def test_refusal_is_never_retried_and_stops_the_run(scripted_site, tmp_path):
    origin, state = scripted_site
    state["script"]["/locked"] = [(403, "<html></html>", {})]

    result = asyncio.run(
        pipeline.collect(
            configuration(origin, "/locked", retries=2),
            SnapshotStore(tmp_path / "db.sqlite"),
        )
    )

    assert result.status == "blocked"
    assert [error.code for error in result.errors] == ["access_denied"]
    # A 403 answers the question; three identical refusals would still mean nothing new.
    assert state["hits"].count("/locked") == 1


def test_exhausted_server_error_is_structured_and_bounded(
    scripted_site, tmp_path, monkeypatch
):
    monkeypatch.setattr(transport, "RETRY_BASE_DELAY", 0.0)
    origin, state = scripted_site
    state["script"]["/broken"] = [(503, "<html></html>", {})]

    result = asyncio.run(
        pipeline.collect(
            configuration(origin, "/broken", retries=1),
            SnapshotStore(tmp_path / "db.sqlite"),
        )
    )

    assert result.status == "partial"
    assert [error.code for error in result.errors] == ["http_status"]
    assert result.errors[0].url == origin + "/broken"
    assert "503" in result.errors[0].message
    # Initial attempt plus exactly the one configured retry, then a structured error.
    assert state["hits"].count("/broken") == 2
    # Each attempt re-ran the robots policy instead of bypassing it.
    assert state["hits"].count("/robots.txt") == 2


def test_transport_timeout_is_retried_within_budget(scripted_site, monkeypatch):
    monkeypatch.setattr(transport, "RETRY_BASE_DELAY", 0.0)
    origin, state = scripted_site
    state["script"]["/slow"] = [(200, PRODUCTS, {})]
    state["slow_once"]["/slow"] = 1.0

    page = asyncio.run(fetcher(origin, timeout=0.4).fetch(origin + "/slow"))

    assert page.status == 200
    # The first attempt timed out mid-response, the retry got the page.
    assert state["hits"].count("/slow") == 2


def test_configured_timeout_reaches_the_transport(scripted_site, tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "RETRY_BASE_DELAY", 0.0)
    origin, state = scripted_site
    state["script"]["/products"] = [(200, PRODUCTS, {})]
    state["slow_once"]["/products"] = 1.0

    result = asyncio.run(
        pipeline.collect(
            configuration(origin, "/products", timeout=0.4),
            SnapshotStore(tmp_path / "db.sqlite"),
        )
    )

    assert result.status == "succeeded"
    assert result.coverage.fetched_pages == 1
    assert state["hits"].count("/products") == 2


def test_oversized_response_is_rejected_rather_than_truncated(scripted_site):
    origin, state = scripted_site
    state["script"]["/big"] = [(200, "x" * 5000, {})]

    with pytest.raises(ValueError, match="byte budget"):
        asyncio.run(fetcher(origin, max_bytes=1024).fetch(origin + "/big"))

    # Size is not a transient fault, so the budget is never silently paid by retrying.
    assert state["hits"].count("/big") == 1


def test_configured_byte_budget_reaches_the_transport(scripted_site, tmp_path):
    origin, state = scripted_site
    state["script"]["/products"] = [(200, PRODUCTS * 20, {})]

    result = asyncio.run(
        pipeline.collect(
            configuration(origin, "/products", max_bytes=200),
            SnapshotStore(tmp_path / "db.sqlite"),
        )
    )

    assert result.status == "partial"
    assert "byte budget" in result.errors[0].message


def test_retry_after_delta_seconds_and_http_date_are_honored():
    client = fetcher("http://127.0.0.1:1")

    assert client._retry_delay(0, "0") == 0
    # A stated delay outranks backoff for the attempt it accompanies.
    assert client._retry_delay(3, "2") == 2
    # A date already past means "retry now"; a far-future one is capped so a bogus
    # header cannot stall a run indefinitely.
    assert client._retry_delay(0, "Wed, 21 Oct 2015 07:28:00 GMT") == 0
    assert (
        client._retry_delay(0, "Wed, 21 Oct 2099 07:28:00 GMT")
        == transport.RETRY_MAX_DELAY
    )
    # An unparseable value falls back to jittered backoff instead of failing the fetch.
    assert 0 <= client._retry_delay(1, "soon") <= transport.RETRY_BASE_DELAY * 2
