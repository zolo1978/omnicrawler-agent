"""Own-account monitoring through an external adapter, against a mock MCP service.

The mock speaks the same Streamable HTTP/JSON-RPC handshake as xiaohongshu-mcp
(initialize -> session id -> initialized -> tools/call) so the adapter's protocol
handling is exercised, not stubbed.
"""

import asyncio
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from omnicrawler import source, xhs
from omnicrawler.report import build_report, render_markdown
from omnicrawler.storage import SnapshotStore


@pytest.fixture
def mcp():
    state = {
        "notes": {
            "n1": {"title": "第一篇", "liked": 10, "token": "tok-1"},
            "n2": {"title": "第二篇", "liked": 3, "token": "tok-2"},
        },
        "profile_error": None,
        "sse": False,
        "calls": [],
        "arguments": {},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            message = json.loads(self.rfile.read(length) or b"{}")
            method = message.get("method")
            params = message.get("params") or {}
            state["calls"].append((method, params.get("name")))
            if method == "initialize":
                self.reply(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "serverInfo": {"name": "mock-xhs", "version": "0"},
                        },
                    },
                    session="test-session",
                )
                return
            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if params.get("name") in {"user_profile", "get_my_profile"}:
                state["arguments"][params["name"]] = params.get("arguments")
                if state["profile_error"]:
                    self.reply(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "error": {
                                "code": -32000,
                                "message": state["profile_error"],
                            },
                        }
                    )
                    return
                notes = [
                    {
                        "id": key,
                        "xsecToken": note["token"],
                        "noteCard": {"displayTitle": note["title"]},
                    }
                    for key, note in state["notes"].items()
                ]
                payload = (
                    {"data": {"notes": notes}}
                    if params.get("name") == "user_profile"
                    else {"userBasicInfo": {"nickname": "自用账号"}, "feeds": notes}
                )
            else:
                state["arguments"]["get_feed_detail"] = params.get("arguments")
                feed_id = (params.get("arguments") or {}).get("feed_id")
                note = state["notes"].get(feed_id)
                if note is None:
                    self.reply(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {
                                "isError": True,
                                "content": [{"type": "text", "text": "笔记不存在"}],
                            },
                        }
                    )
                    return
                payload = {
                    "note": {"title": note["title"], "desc": note.get("desc", "")},
                    "interactInfo": {"likedCount": str(note["liked"])},
                    "tags": ["标签A", "标签B"],
                }
            self.reply(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(payload, ensure_ascii=False),
                            }
                        ]
                    },
                }
            )

        def reply(self, body, session=None):
            raw = json.dumps(body, ensure_ascii=False).encode()
            if state["sse"]:
                raw = b"event: message\ndata: " + raw + b"\n\n"
                content_type = "text/event-stream"
            else:
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()
    thread.join()


def configuration(origin, **overrides):
    base = {
        "kind": "xhs",
        "account": "my-account",
        "endpoint": origin + "/mcp",
        "limit": 10,
        "fields": {
            "title": "note.title",
            "liked": "interactInfo.likedCount",
            "tags": "tags[]",
        },
    }
    return source.SourceConfig(**{**base, **overrides})


def run_once(origin, tmp_path, store=None, config=None, **overrides):
    config = config or configuration(origin, **overrides)
    store = store or SnapshotStore(tmp_path / "xhs.sqlite")
    adapter = xhs.adapter_for(config, scope_token="session-token")
    result = asyncio.run(source.collect_from_source(config, store, adapter))
    return result, store, config


def test_own_account_run_records_evidence_and_detects_changes(mcp, tmp_path):
    origin, state = mcp
    first, store, _config = run_once(origin, tmp_path)
    assert first.status == "succeeded"
    assert first.errors == []
    records = store.bundle(first.snapshot_id)["records"]
    assert sorted(record["entity_id"] for record in records) == ["n1", "n2"]
    evidence = records[0]["evidence"]
    assert evidence["title"]["url"] == "https://www.xiaohongshu.com/explore/n1"
    assert evidence["title"]["locator"] == "xhs:note/n1#title"
    assert evidence["tags"]["excerpt"] == "标签A；标签B"

    state["notes"]["n1"]["liked"] = 99
    second, _store, _config = run_once(origin, tmp_path, store=store)
    assert second.previous_snapshot_id == first.snapshot_id
    assert [(change.kind, change.entity_id) for change in second.changes] == [
        ("modified", "n1")
    ]

    state["notes"]["n3"] = {"title": "第三篇", "liked": 1, "token": "tok-3"}
    third, _store, _config = run_once(origin, tmp_path, store=store)
    assert [(change.kind, change.entity_id) for change in third.changes] == [
        ("added", "n3")
    ]


def test_session_token_never_reaches_the_database_or_the_report(mcp, tmp_path):
    origin, _state = mcp
    result, store, _config = run_once(origin, tmp_path)
    stored = json.dumps(store.bundle(result.snapshot_id), ensure_ascii=False)
    assert "tok-1" not in stored and "tok-2" not in stored
    assert "session-token" not in stored


def test_external_retrieval_is_disclosed(mcp, tmp_path):
    origin, _state = mcp
    result, store, _config = run_once(origin, tmp_path)
    assert [warning.code for warning in result.warnings] == ["external_retrieval"]
    assert result.engine == "source:xhs"
    bundle = store.bundle(result.snapshot_id)
    assert "network policy not applied" in bundle["route_reason"]
    markdown = render_markdown(build_report(store, result.model_dump(mode="json")))
    # Markdown escapes underscores inside the code token.
    assert "external_retrieval" in markdown.replace("\\", "")


def test_failed_enumeration_never_reports_a_mass_deletion(mcp, tmp_path):
    origin, state = mcp
    first, store, config = run_once(origin, tmp_path)
    state["profile_error"] = "未登录，请先扫码登录"
    second, _store, _config = run_once(origin, tmp_path, store=store, config=config)

    assert second.status == "partial"
    assert [error.code for error in second.errors] == ["source_not_authenticated"]
    assert second.changes == []
    assert second.coverage.complete is False
    assert store.latest(source.source_coverage(config)).id == first.snapshot_id


def test_empty_scope_is_not_read_as_a_deletion(mcp, tmp_path):
    origin, state = mcp
    first, store, config = run_once(origin, tmp_path)
    state["notes"] = {}
    second, _store, _config = run_once(origin, tmp_path, store=store, config=config)

    assert [error.code for error in second.errors] == ["source_empty_scope"]
    assert second.changes == []
    assert store.latest(source.source_coverage(config)).id == first.snapshot_id


def test_truncated_scope_blocks_deletion_inference(mcp, tmp_path):
    origin, state = mcp
    state["notes"]["n3"] = {"title": "第三篇", "liked": 1, "token": "tok-3"}
    result, _store, _config = run_once(origin, tmp_path, limit=2)

    assert result.status == "partial"
    assert [error.code for error in result.errors] == ["source_scope_truncated"]
    assert result.coverage.truncated is True
    assert result.coverage.planned_pages == 3
    assert result.coverage.fetched_pages == 2
    assert result.changes == []


def test_unresolved_field_warns_without_losing_the_run(mcp, tmp_path):
    origin, _state = mcp
    result, store, _config = run_once(
        origin,
        tmp_path,
        fields={"title": "note.title", "desc": "note.desc"},
    )

    assert result.status == "succeeded"
    assert [warning.code for warning in result.warnings] == [
        "external_retrieval",
        "field_missing",
    ]
    records = store.bundle(result.snapshot_id)["records"]
    assert all(set(record["fields"]) == {"title"} for record in records)


def test_sse_framed_responses_are_parsed(mcp, tmp_path):
    origin, state = mcp
    state["sse"] = True
    result, store, _config = run_once(origin, tmp_path)

    assert result.status == "succeeded"
    assert len(store.bundle(result.snapshot_id)["records"]) == 2


def test_cli_source_command_delivers_a_report(mcp, tmp_path):
    origin, _state = mcp
    config_path = tmp_path / "source.json"
    config_path.write_text(
        json.dumps(
            {
                "kind": "xhs",
                "account": "my-account",
                "endpoint": origin + "/mcp",
                "limit": 10,
                "fields": {"title": "note.title", "liked": "interactInfo.likedCount"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report_dir = tmp_path / "reports"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnicrawler",
            "source",
            str(config_path),
            "--db",
            str(tmp_path / "cli.sqlite"),
            "--report-dir",
            str(report_dir),
            "--format",
            "md",
            "--format",
            "json",
            "--scope-token",
            "session-token",
        ],
        check=False,
        capture_output=True,
        timeout=120,
    )

    assert process.returncode == 0, process.stderr.decode("utf-8", "replace")
    payload = json.loads(process.stdout.decode("utf-8"))
    assert payload["status"] == "succeeded"
    assert payload["engine"] == "source:xhs"
    assert sorted(payload["delivery"]["files"]) == ["json", "md"]
    report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["records"] == 2
    text = (report_dir / "report.md").read_text(encoding="utf-8").replace("\\", "")
    assert "external_retrieval" in text


def test_own_account_mode_needs_no_user_id_or_token(mcp, tmp_path):
    """Omitting the account reads the logged-in user's own profile."""
    origin, state = mcp
    result, store, _config = run_once(origin, tmp_path, account="")

    assert result.status == "succeeded"
    records = store.bundle(result.snapshot_id)["records"]
    assert sorted(record["entity_id"] for record in records) == ["n1", "n2"]
    assert state["arguments"]["get_my_profile"] == {}
    assert ("tools/call", "get_my_profile") in state["calls"]


def test_explicit_account_uses_user_profile_with_the_scope_token(mcp, tmp_path):
    origin, state = mcp
    result, _store, _config = run_once(origin, tmp_path)

    assert result.status == "succeeded"
    assert state["arguments"]["user_profile"] == {
        "user_id": "my-account",
        "xsec_token": "session-token",
    }
    assert ("tools/call", "user_profile") in state["calls"]


def test_probe_looks_up_the_per_note_token_itself(mcp, tmp_path):
    """Tokens are per note on this platform, so the operator never handles them."""
    origin, state = mcp
    config = configuration(origin, account="")
    adapter = xhs.adapter_for(config)

    payload = asyncio.run(adapter.probe("n2"))

    assert payload["note"]["title"] == "第二篇"
    assert state["arguments"]["get_feed_detail"] == {
        "feed_id": "n2",
        "xsec_token": "tok-2",
        "load_all_comments": False,
    }


def test_own_account_mode_never_calls_user_profile(mcp, tmp_path):
    """Own-account mode has no user id and no scope token to send."""
    origin, state = mcp
    run_once(origin, tmp_path, account="")
    called = [name for _method, name in state["calls"]]

    assert "get_my_profile" in called
    assert "user_profile" not in called


def test_probe_returns_the_raw_tool_payload(mcp, tmp_path):
    origin, _state = mcp
    config = configuration(origin)
    adapter = xhs.adapter_for(config, scope_token="session-token")
    payload = asyncio.run(adapter.probe("n1", "tok-1"))

    assert payload["note"]["title"] == "第一篇"
    assert payload["interactInfo"]["likedCount"] == "10"
