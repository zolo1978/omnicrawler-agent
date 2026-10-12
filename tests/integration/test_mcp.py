import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from omnicrawler import mcp_server


def tool_json(result):
    """FastMCP answers with content blocks, or with blocks plus structured data."""
    blocks = result[0] if isinstance(result, tuple) else result
    text = "".join(getattr(block, "text", "") for block in blocks)
    return json.loads(text) if text.strip() else result[1]


def test_mcp_exposes_tools_and_enforces_source_policy(tmp_path):
    async def run():
        server = mcp_server.build_server(tmp_path / "db.sqlite", {"example.com"})
        names = {tool.name for tool in await server.list_tools()}
        assert {
            "create_task",
            "task_status",
            "task_result",
            "cancel_task",
            "list_changes",
            "nominate_sources",
        } <= names
        with pytest.raises(ToolError):
            await server.call_tool(
                "create_task",
                {
                    "config": {
                        "urls": ["https://evil.com"],
                        "allowed_hosts": ["evil.com"],
                        "item_selector": "article",
                        "id_attribute": "data-id",
                        "fields": {"price": ".price"},
                    }
                },
            )

    asyncio.run(run())


def test_task_status_carries_the_run_contract(tmp_path):
    async def run():
        server = mcp_server.build_server(tmp_path / "db.sqlite", {"example.com"})
        created = tool_json(
            await server.call_tool(
                "create_task",
                {
                    "config": {
                        "urls": ["https://example.com/products"],
                        "allowed_hosts": ["example.com"],
                        "item_selector": "article",
                        "id_attribute": "data-id",
                        "fields": {"price": ".price"},
                    }
                },
            )
        )
        status = tool_json(
            await server.call_tool("task_status", {"task_id": created["id"]})
        )
        assert status["status"] == "queued"
        # The run contract is present before a run finishes, so a caller never
        # has to guess whether a key is missing or simply empty.
        assert status["errors"] == []
        assert status["warnings"] == []
        assert status["coverage"] is None
        assert status["engine"] is None

    asyncio.run(run())


def test_nominate_sources_requires_a_configured_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNICRAWLER_SEARCH_URL", raising=False)
    monkeypatch.delenv("OMNICRAWLER_SEARCH_TOKEN", raising=False)

    async def run():
        server = mcp_server.build_server(tmp_path / "db.sqlite", {"example.com"})
        session = tool_json(await server.call_tool("start_session", {}))
        with pytest.raises(ToolError) as failure:
            await server.call_tool("nominate_sources", {"session_id": session["id"]})
        assert "No search backend configured" in str(failure.value)

    asyncio.run(run())
