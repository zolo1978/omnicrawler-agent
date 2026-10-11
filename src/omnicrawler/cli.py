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
    run.add_argument("--report-dir", type=Path)
    run.add_argument("--format", action="append", choices=["md", "docx", "pdf", "json"])
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
    guide = commands.add_parser("guide")
    guide.add_argument("--db", type=Path, default=Path("data/omnicrawler.sqlite"))
    guide.add_argument("--allow-host", action="append", default=[])
    guide.add_argument("--session-id")
    args = parser.parse_args()
    if args.command == "collect":
        config = CollectionConfig.model_validate_json(args.config.read_text())
        result = asyncio.run(collect(config, SnapshotStore(args.db)))
        print(result.model_dump_json(indent=2))
        if args.report_dir:
            from omnicrawler.report import build_report, deliver

            report = build_report(
                SnapshotStore(args.db), result.model_dump(mode="json")
            )
            print(
                json.dumps(
                    asyncio.run(
                        deliver(
                            report,
                            args.report_dir,
                            args.format or ["md", "docx", "pdf", "json"],
                        )
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
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
    elif args.command == "guide":
        from omnicrawler.service import worker_lock

        args.db.parent.mkdir(parents=True, exist_ok=True)
        with worker_lock(args.db):
            asyncio.run(run_guide(args.db, args.allow_host, args.session_id))
    else:
        print(
            json.dumps({"deleted_snapshots": SnapshotStore(args.db).cleanup(args.days)})
        )


async def run_guide(path, allowed_hosts, session_id=None, *, local_origins=()):
    from omnicrawler.provider import provider_from_env
    from omnicrawler.service import TaskService
    from omnicrawler.session import SessionStore

    manager = TaskService(path)
    sessions = SessionStore(
        manager,
        allowed_hosts=set(allowed_hosts),
        local_origins=local_origins,
        provider=provider_from_env(),
    )
    value = sessions.get(session_id) if session_id else sessions.start()
    print(f"会话 ID：{value['id']}（可用 --session-id 恢复；exit 退出并保存）")
    try:
        while not value["confirmation"]:
            if not value["questions"]:
                print(json.dumps(value["draft"], ensure_ascii=False, indent=2))
                consent = (
                    input("确认上述来源、字段、预算、频率和交付格式并执行？[y/N] ")
                    .strip()
                    .lower()
                )
                if consent != "y":
                    print("草案已保存，尚未执行。")
                    return value
                confirmation = sessions.confirm(
                    value["id"], value["revision"], actor="local-cli-user"
                )
                await manager.run(confirmation["task_id"])
                print(
                    json.dumps(
                        manager.get(confirmation["task_id"]),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                if confirmation["schedule_id"]:
                    print(
                        "监控计划已持久化。持续监控需运行 serve 或 mcp，并使用相同数据库与域名白名单。"
                    )
                return sessions.get(value["id"])
            answers = value["answers"]
            unknowns = value["draft"]["unknowns"] if value["draft"] else []
            if (
                answers.get("source")
                and answers.get("fields")
                and "scope" not in unknowns
                and any(
                    key in unknowns
                    for key in ["item_selector", "id_attribute"]
                    + ["fields." + name for name in answers["fields"]]
                )
                and input("尝试受控采样并给出选择器提案？[y/N] ").strip().lower() == "y"
            ):
                value = await sessions.preview(value["id"])
                print(
                    json.dumps(value["draft"]["preview"], ensure_ascii=False, indent=2)
                )
                if (
                    input("接受以上选择器提案？仍需最终执行确认。[y/N] ")
                    .strip()
                    .lower()
                    == "y"
                ):
                    value = sessions.answer(value["id"], {"accept_preview": True})
                    continue
            question = (
                next(
                    (q for q in value["questions"] if q["key"] == "scope"),
                    value["questions"][0],
                )
                if answers.get("source")
                else value["questions"][0]
            )
            raw = input(question["prompt"] + " ").strip()
            if raw.lower() == "exit":
                return value
            key = question["key"]
            if key == "fields":
                update = {
                    key: {name.strip(): "" for name in raw.split(",") if name.strip()}
                }
            elif key in {"source", "scope"}:
                update = {
                    key: [part.strip() for part in raw.split(",") if part.strip()]
                }
            elif key == "frequency":
                try:
                    update = {key: int(raw)}
                except ValueError:
                    print("请输入至少 10 的整数秒数。")
                    continue
            elif key.startswith("fields."):
                update = {"fields": {**answers["fields"], key.split(".", 1)[1]: raw}}
            else:
                update = {key: raw}
            try:
                value = sessions.answer(value["id"], update)
            except ValueError:
                print("回答不符合约束，请核对字段、格式及启动白名单。")
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return value
    except (EOFError, KeyboardInterrupt):
        print("会话已保存。")
        return value
