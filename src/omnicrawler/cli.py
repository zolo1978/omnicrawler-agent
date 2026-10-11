"""Local CLI, deterministic demo, REST server and MCP entrypoint."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from omnicrawler.pipeline import CollectionConfig, collect
from omnicrawler.storage import SnapshotStore


def main():
    parser = argparse.ArgumentParser(prog="omnicrawler")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("collect")
    run.add_argument("config", type=Path)
    run.add_argument("--db", type=Path, default=Path("data/omnicrawler.sqlite"))
    demo = commands.add_parser("demo")
    demo.add_argument("--output", type=Path, default=Path("data/demo"))
    serve = commands.add_parser("serve")
    serve.add_argument("--db", type=Path, default=Path("data/omnicrawler.sqlite"))
    serve.add_argument("--allow-host", action="append", default=[])
    serve.add_argument("--port", type=int, default=8000)
    mcp = commands.add_parser("mcp")
    mcp.add_argument("--db", type=Path, default=Path("data/omnicrawler.sqlite"))
    mcp.add_argument("--allow-host", action="append", default=[])
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--db", type=Path, default=Path("data/omnicrawler.sqlite"))
    cleanup.add_argument("--days", type=int, default=90)
    args = parser.parse_args()
    if args.command == "collect":
        config = CollectionConfig.model_validate_json(args.config.read_text())
        result = asyncio.run(collect(config, SnapshotStore(args.db)))
        print(result.model_dump_json(indent=2))
        if result.status != "succeeded":
            raise SystemExit(2)
    elif args.command == "demo":
        from omnicrawler.demo import run_demo

        print(
            json.dumps(asyncio.run(run_demo(args.output)), ensure_ascii=False, indent=2)
        )
    elif args.command == "serve":
        import uvicorn

        from omnicrawler.api import create_app

        key = os.environ.get("OMNICRAWLER_API_KEY")
        if not key:
            parser.error("Set OMNICRAWLER_API_KEY before serving")
        uvicorn.run(
            create_app(args.db, key, allowed_hosts=args.allow_host),
            host="127.0.0.1",
            port=args.port,
            workers=1,
        )
    elif args.command == "mcp":
        from omnicrawler.mcp_server import build_server

        build_server(args.db, set(args.allow_host)).run(transport="stdio")
    else:
        print(
            json.dumps({"deleted_snapshots": SnapshotStore(args.db).cleanup(args.days)})
        )
