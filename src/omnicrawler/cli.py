"""Local CLI, deterministic demo, REST server and MCP entrypoint."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from omnicrawler.pipeline import CollectionConfig, collect
from omnicrawler.storage import SnapshotStore


def use_utf8_streams():
    """Emit UTF-8 on stdout/stderr regardless of the locale.

    The CLI's data is UTF-8 end to end (configs, reports, JSON output), so a
    locale-encoded pipe would either mangle it or raise on a character the codec
    cannot represent. On a real Windows console Python writes through the console
    API, so this only changes what a pipe or redirect receives.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main():
    use_utf8_streams()
    parser = argparse.ArgumentParser(prog="omnicrawler")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("collect")
    run.add_argument("config", type=Path)
    run.add_argument("--db", type=Path, default=Path("data/omnicrawler.sqlite"))
    run.add_argument("--report-dir", type=Path)
    run.add_argument("--format", action="append", choices=["md", "docx", "pdf", "json"])
    run.add_argument(
        "--store-raw-html",
        action="store_true",
        help="Keep the fetched page source so `omnicrawler replay` can reprint it",
    )
    replay = commands.add_parser(
        "replay", help="Print the stored page source behind one fetch of a snapshot"
    )
    replay.add_argument("snapshot_id")
    replay.add_argument("url")
    replay.add_argument("--db", type=Path, default=Path("data/omnicrawler.sqlite"))
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
        config = CollectionConfig.model_validate_json(
            args.config.read_text(encoding="utf-8")
        )
        if args.store_raw_html:
            # Retention is opt-in per run: the flag overrides the config file so
            # the same plan can be replayed without editing and re-committing it.
            config = config.model_copy(update={"store_raw_html": True})
        result = asyncio.run(collect(config, SnapshotStore(args.db)))
        payload = result.model_dump(mode="json")
        if args.report_dir:
            from omnicrawler.provider import provider_from_env
            from omnicrawler.report import build_report, deliver
            from omnicrawler.synthesis import synthesize

            store = SnapshotStore(args.db)
            # Same delivery contract as a guided run: a configured provider is used
            # here too, so `collect --report-dir` cannot hand back a report whose
            # inference section says "not enabled" while one is configured.
            claims, synthesis_warnings = asyncio.run(
                synthesize(store=store, result=payload, provider=provider_from_env())
            )
            report = build_report(store, payload, inferences=claims)
            manifest = asyncio.run(
                deliver(
                    report,
                    args.report_dir,
                    args.format or ["md", "docx", "pdf", "json"],
                )
            )
            manifest["warnings"].extend(str(warning) for warning in synthesis_warnings)
            # One JSON document on stdout, so a consumer never has to split
            # concatenated documents to find the run result.
            payload["delivery"] = manifest
        print(json.dumps(payload, ensure_ascii=False, indent=2))
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
    elif args.command == "replay":
        store = SnapshotStore(args.db)
        try:
            document = store.raw_for_fetch(args.snapshot_id, args.url)
        except KeyError:
            document = None  # Unknown snapshot and no retained source read the same.
        if document is None:
            print(
                f"无法回放：快照 {args.snapshot_id} 不存在，或没有为 {args.url} 留存原文。"
                "留存原文是显式选项（collect --store-raw-html 或配置 store_raw_html=true）；"
                "结构化字段与证据仍可通过报告和快照读取。",
                file=sys.stderr,
            )
            raise SystemExit(3)
        # Provenance goes to stderr so stdout stays exactly the retained source,
        # and the source goes out as UTF-8 bytes so a non-UTF-8 console locale
        # can neither mangle nor reject the page.
        print(
            f"# {document['url']} captured_at={document['captured_at']}",
            file=sys.stderr,
        )
        sys.stdout.buffer.write((document["html"] + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        print(
            json.dumps({"deleted_snapshots": SnapshotStore(args.db).cleanup(args.days)})
        )


async def run_guide(path, allowed_hosts, session_id=None, *, local_origins=()):
    from omnicrawler.provider import provider_from_env
    from omnicrawler.search import search_from_env
    from omnicrawler.service import TaskService
    from omnicrawler.session import SessionStore

    # Nomination is offered only when the operator configured a search backend;
    # without one the dialogue is exactly the deterministic questionnaire.
    search = search_from_env()
    provider = provider_from_env()
    manager = TaskService(path, provider=provider)
    sessions = SessionStore(
        manager,
        allowed_hosts=set(allowed_hosts),
        local_origins=local_origins,
        provider=provider,
        search=search,
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
                search is not None
                and answers.get("goal")
                and not answers.get("source")
                and input("用已配置的搜索后端提名候选来源？[y/N] ").strip().lower()
                == "y"
            ):
                try:
                    value = await sessions.nominate(value["id"])
                except ValueError as exc:
                    print(f"提名未完成：{exc}")
                    continue
                print(json.dumps(value["nominations"], ensure_ascii=False, indent=2))
                if (
                    input("采纳以上候选来源？仍需最终执行确认。[y/N] ").strip().lower()
                    == "y"
                ):
                    value = sessions.answer(value["id"], {"accept_nomination": True})
                    continue
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
