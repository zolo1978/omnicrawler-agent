"""Evidence-scoped reports and offline renderers with explicit format downgrade."""

import html
import json
import os
import re
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnicrawler.governance import sanitize


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = "OmniCrawler 报告"
    generated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    goal: str = ""
    task_type: Literal["once", "monitor"] = "once"
    summary: dict = Field(default_factory=dict)
    records: list[dict] = Field(default_factory=list)
    changes: list[dict] = Field(default_factory=list)
    claims: list[dict] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    schedule_seconds: int | None = None
    facts: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    status: str = "succeeded"
    snapshot_id: str | None = None


def _clean(value):
    if isinstance(value, dict):
        return {
            str(_clean(key)): "[redacted]"
            if str(key).lower()
            in {"password", "cookie", "authorization", "api_key", "token"}
            else _clean(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, str):
        text = re.sub(
            r"(?i)(authorization\s*:\s*bearer\s+|\bbearer\s+)[^\s,;]+",
            r"\1[redacted]",
            value,
        )
        text = re.sub(r"(?im)(\bcookie\s*:)\s*[^\r\n]+", r"\1 [redacted]", text)

        def clean_url(match):
            try:
                parsed = urlsplit(match.group(0))
                query = [
                    (
                        key,
                        "[redacted]"
                        if key.lower().replace("-", "_")
                        in {
                            "api_key",
                            "apikey",
                            "token",
                            "access_token",
                            "password",
                            "secret",
                            "auth",
                            "key",
                        }
                        else item,
                    )
                    for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                ]
                parsed = parsed._replace(query=urlencode(query))
                cleaned = urlunsplit(parsed)
                if parsed.username is not None or parsed.password is not None:
                    cleaned = urlunsplit(
                        parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1])
                    )
                return cleaned
            except ValueError:
                return "[invalid-url]"

        text = re.sub(r"https?://[^\s<>\"']+", clean_url, text, flags=re.IGNORECASE)
        text = sanitize(text)
        return text
    return value


def build_report(
    store,
    result: dict,
    *,
    task_type="once",
    schedule_seconds=None,
    title="OmniCrawler 报告",
) -> Report:
    identifier = result.get("snapshot_id")
    bundle = store.bundle(identifier) if identifier else {}
    records = bundle.get("records", [])
    previous = result.get("previous_snapshot_id")
    old_records = store.bundle(previous).get("records", []) if previous else []
    changes = []
    for change in result.get("changes", []):
        if not isinstance(change, dict):
            from dataclasses import asdict

            change = asdict(change)
        entity = change["entity_id"]
        changes.append(
            {
                **change,
                "evidence": {
                    "before": [r for r in old_records if r["entity_id"] == entity],
                    "after": [r for r in records if r["entity_id"] == entity],
                },
            }
        )
    sources = {fetch["url"] for fetch in bundle.get("fetches", []) if fetch.get("url")}
    for record in records + old_records:
        sources.update(
            proof["url"]
            for proof in record.get("evidence", {}).values()
            if proof.get("url")
        )
    for claim in bundle.get("claims", []):
        sources.update(
            observation.get("evidence", {}).get("url")
            for observation in claim.get("observations", [])
            if observation.get("evidence", {}).get("url")
        )
    summary = {
        "records": len(records),
        **{
            kind: sum(c["kind"] == kind for c in changes)
            for kind in ("added", "modified", "removed")
        },
    }
    facts = [
        f"本次保存 {len(records)} 条记录；状态：{result.get('status', 'unknown')}。"
    ]
    if (
        previous
        and store.get(previous).complete
        and store.get(identifier).complete
        and store.get(previous).coverage == store.get(identifier).coverage
    ):
        facts.append(f"变化来自快照 {previous} → {identifier} 的可比范围比较。")
    if result.get("status") == "partial":
        facts.append("范围不完整；不能推断删除，也不代表完整来源覆盖。")
    disputed = sum(
        claim.get("status") == "disputed" for claim in bundle.get("claims", [])
    )
    if disputed:
        facts.append(f"{disputed} 个字段存在来源冲突；不视为一致事实。")
    return Report(
        **_clean(
            {
                "title": title,
                "goal": bundle.get("goal", ""),
                "task_type": task_type,
                "summary": summary,
                "records": records,
                "changes": changes,
                "claims": bundle.get("claims", []),
                "sources": sorted(sources),
                "schedule_seconds": schedule_seconds,
                "facts": facts,
                "inferences": [],
                "status": result.get("status", "unknown"),
                "snapshot_id": identifier,
            }
        )
    )


_LABELS = {
    "records": "记录数",
    "added": "新增",
    "modified": "变化",
    "removed": "下架",
    "entity_id": "对象",
    "fields": "字段",
    "evidence": "来源依据",
    "url": "来源",
    "locator": "页面位置",
    "captured_at": "采集时间",
    "excerpt": "摘录",
    "price": "价格",
    "currency": "币种",
    "name": "名称",
    "category": "品类",
    "player": "球员或角色",
    "year": "年份",
    "set": "系列",
    "grade": "评级",
    "price_type": "价格口径",
    "before": "变更前",
    "after": "变更后",
    "kind": "变更类型",
    "status": "状态",
    "observations": "来源观测",
    "value": "值",
    "topic": "主题",
    "date": "日期",
    "summary": "摘要",
    "metric_scope": "统计口径",
    "confidence": "可信度",
    "method": "核查方式",
    "primary_sources": "原始来源数",
    "simulated_cards": "模拟卡片数",
    "pages": "页面数",
    "simulation_verified": "模拟验证",
    "real_transaction_samples": "真实成交样本数",
}
DETAIL_LIMIT = 20


def _readable(value, depth=0):
    if isinstance(value, dict):
        return "；".join(
            f"{_LABELS.get(key, key)}：{_readable(item, depth + 1)}"
            for key, item in value.items()
        )
    if isinstance(value, list):
        return " / ".join(_readable(item, depth + 1) for item in value)
    if value is None:
        return "未提供"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _lines(report: Report) -> list[tuple[str, str]]:
    data = _clean(report.model_dump(mode="json"))
    lines = [
        ("title", data["title"]),
        ("text", f"目标：{data['goal']}"),
        ("text", f"生成时间：{data['generated_at']}"),
        (
            "text",
            f"任务类型：{data['task_type']}；状态：{data['status']}；周期：{data['schedule_seconds']}",
        ),
        ("heading", "汇总"),
        ("text", _readable(data["summary"])),
        ("heading", "事实"),
    ]
    lines.extend(("text", fact) for fact in data["facts"])
    lines.append(("heading", "推断"))
    lines.extend(("text", inference) for inference in data["inferences"])
    if not data["inferences"]:
        lines.append(("text", "无推断；来源独立性未验证。"))
    lines.append(("heading", "记录与证据"))
    if not data["records"]:
        lines.append(("text", "无记录。"))
    for record in data["records"][:DETAIL_LIMIT]:
        lines.append(("text", _readable(record)))
    if len(data["records"]) > DETAIL_LIMIT:
        lines.append(
            (
                "text",
                f"正文展示前 {DETAIL_LIMIT} 条，共 {len(data['records'])} 条；完整记录与证据见 JSON 数据文件。",
            )
        )
    lines.append(("heading", "变化：before → after 与证据"))
    for change in data["changes"][:DETAIL_LIMIT]:
        visible = {key: value for key, value in change.items() if key != "evidence"}
        lines.append(("text", _readable(visible)))
    if data["changes"]:
        lines.append(
            (
                "text",
                f"共 {len(data['changes'])} 条变化；完整前后记录与逐字段证据见 JSON 数据文件。",
            )
        )
    if not data["changes"]:
        lines.append(("text", "无变化事件；首个快照或不完整范围不能推断删除。"))
    lines.append(("heading", "字段观测与冲突证据"))
    for claim in data["claims"][:DETAIL_LIMIT]:
        lines.append(("text", _readable(claim)))
    if len(data["claims"]) > DETAIL_LIMIT:
        lines.append(("text", "其余来源观测与冲突证据见 JSON 数据文件。"))
    lines.append(("heading", "来源"))
    lines.extend(("text", source) for source in data["sources"])
    return lines


def render_markdown(report: Report) -> str:
    def escape(text):
        return re.sub(r"([\\`*_{}\[\]()#|>])", r"\\\1", html.escape(text, quote=False))

    return (
        "\n\n".join(
            ("# " if kind == "title" else "## " if kind == "heading" else "")
            + escape(text)
            for kind, text in _lines(report)
        )
        + "\n"
    )


def render_docx(report: Report) -> bytes:
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError("Word 未交付：需要安装 python-docx。") from exc
    document = Document()
    for kind, text in _lines(report):
        if kind in {"title", "heading"}:
            document.add_heading(text, level=0 if kind == "title" else 1)
        else:
            document.add_paragraph(text)
    summary = document.add_table(rows=1, cols=2)
    summary.style = "Table Grid"
    summary.rows[0].cells[0].text = "汇总指标"
    summary.rows[0].cells[1].text = "值"
    cleaned = _clean(report.model_dump(mode="json"))
    for key, value in cleaned["summary"].items():
        cells = summary.add_row().cells
        cells[0].text, cells[1].text = _LABELS.get(key, key), _readable(value)
    if cleaned["records"]:
        records = document.add_table(rows=1, cols=3)
        records.style = "Table Grid"
        for cell, label in zip(
            records.rows[0].cells,
            ["实体", "字段和值", "证据（URL / 定位 / 时间）"],
            strict=True,
        ):
            cell.text = label
        for record in cleaned["records"][:DETAIL_LIMIT]:
            cells = records.add_row().cells
            cells[0].text = str(record.get("entity_id", ""))
            cells[1].text = _readable(record.get("fields", {}))
            cells[2].text = _readable(record.get("evidence", {}))
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def cjk_font_family() -> str | None:
    roots = [
        Path("/System/Library/Fonts"),
        Path("/System/Library/AssetsV2"),
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".local/share/fonts",
    ]
    for pattern, family in [
        ("NotoSansCJK*", "Noto Sans CJK SC"),
        ("NotoSerifCJK*", "Noto Serif CJK SC"),
        ("STHeiti Medium.ttc", "STHeiti"),
        ("PingFang*", "PingFang SC"),
    ]:
        for root in roots:
            if root.exists() and next(root.rglob(pattern), None) is not None:
                return family
    return None


async def render_pdf(report: Report) -> bytes:
    family = cjk_font_family()
    if not family:
        raise RuntimeError("PDF 未交付：缺少 CJK 字体，请安装 Noto CJK；保留其他格式。")
    from playwright.async_api import async_playwright

    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(Path(".browser-cache").resolve())
    )
    body = "".join(
        f"<{('h1' if kind == 'title' else 'h2' if kind == 'heading' else 'p')}>{html.escape(text)}</{('h1' if kind == 'title' else 'h2' if kind == 'heading' else 'p')}>"
        for kind, text in _lines(report)
    )
    content = f'<!doctype html><html lang="zh"><meta charset="utf-8"><style>body {{font-family: "{family}", sans-serif; font-size: 11px}} p {{line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere}} h2 {{break-after: avoid}}</style><body>{body}</body></html>'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(chromium_sandbox=True)
        try:
            context = await browser.new_context(
                java_script_enabled=False, offline=True, service_workers="block"
            )
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            await page.set_content(content)
            await page.evaluate("document.fonts.ready")
            return await page.pdf(
                format="A4",
                print_background=True,
                margin={
                    "top": "16mm",
                    "bottom": "16mm",
                    "left": "15mm",
                    "right": "15mm",
                },
            )
        finally:
            await browser.close()


async def deliver(
    report: Report, output: Path, formats=("md", "docx", "pdf", "json")
) -> dict:
    formats = tuple(dict.fromkeys(formats))
    if set(formats) - {"md", "docx", "pdf", "json"}:
        raise ValueError("Unknown report format")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    files, warnings = {}, []
    for format in formats:
        path = output / f"report.{format}"
        try:
            if format == "pdf":
                content = await render_pdf(report)
            elif format == "docx":
                content = render_docx(report)
            elif format == "md":
                content = render_markdown(report).encode()
            else:
                content = json.dumps(
                    _clean(report.model_dump(mode="json")), ensure_ascii=False, indent=2
                ).encode()
            path = output / f"report.{format}"
            path.write_bytes(content)
            files[format] = str(path)
        except Exception as exc:  # noqa: BLE001 — format boundary intentionally downgrades
            try:
                path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                warnings.append(
                    f"{format}: 清理旧产物失败（{type(cleanup_error).__name__}）；未删除目录或其中内容。"
                )
            # A requested auxiliary format never reverses a completed collection.
            message = (
                _clean(str(exc))
                if isinstance(exc, RuntimeError)
                else "渲染或写入失败；请检查本地依赖和输出目录。"
            )
            warnings.append(f"{format}: {type(exc).__name__}: {message}")
    return {"files": files, "warnings": warnings}
