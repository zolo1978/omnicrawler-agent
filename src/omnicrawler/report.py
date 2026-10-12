"""Evidence-scoped reports and offline renderers with explicit format downgrade."""

import html
import json
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnicrawler import models
from omnicrawler.governance import sanitize
from omnicrawler.report_design import THEME


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
    # Model conclusions, each carrying the stored Evidence that proves it. The
    # contract, not the prompt, is what keeps an unsupported sentence out.
    inferences: list[models.Claim] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)
    errors: list[models.CollectError] = Field(default_factory=list)
    warnings: list[models.CollectError] = Field(default_factory=list)
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


def _as_dict(value):
    """Accept stored JSON, dataclasses, or pydantic models for one concept."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return asdict(value) if is_dataclass(value) else dict(value)


def build_report(
    store,
    result: dict,
    *,
    task_type="once",
    schedule_seconds=None,
    title="OmniCrawler 报告",
    inferences=None,
) -> Report:
    identifier = result.get("snapshot_id")
    bundle = store.bundle(identifier) if identifier else {}
    records = bundle.get("records", [])
    previous = result.get("previous_snapshot_id")
    old_records = store.bundle(previous).get("records", []) if previous else []
    changes = []
    for change in result.get("changes", []):
        change = _as_dict(change)
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
                # Normalise before redaction: inference text is model-authored.
                "inferences": [
                    models.Claim.model_validate(claim).model_dump(mode="json")
                    for claim in (inferences or [])
                ],
                "coverage": bundle.get("coverage_detail") or {},
                "errors": [
                    _as_dict(error)
                    for error in (bundle.get("errors") or result.get("errors") or [])
                ],
                "warnings": [
                    _as_dict(warning)
                    for warning in (
                        bundle.get("warnings") or result.get("warnings") or []
                    )
                ],
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


def _evidence_summary(value):
    groups = {}
    notes = []

    def visit(item):
        if isinstance(item, dict):
            if item.get("url"):
                key = (str(item["url"]), str(item.get("captured_at", "")))
                groups.setdefault(key, set())
                if item.get("locator"):
                    groups[key].add(str(item["locator"]))
                if item.get("method"):
                    notes.append(str(item["method"]))
            else:
                for child in item.values():
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    lines = [
        f"来源：{url}"
        + (f"；采集时间：{time}" if time else "")
        + (f"；页面位置：{' / '.join(sorted(locators)[:3])}" if locators else "")
        for (url, time), locators in groups.items()
    ]
    if any(len(locators) > 3 for locators in groups.values()):
        lines.append("其余字段定位见完整 JSON 证据文件")
    return "；".join(lines + list(dict.fromkeys(notes))) or _readable(value)


def _reader_record(record):
    return (
        _readable({key: value for key, value in record.items() if key != "evidence"})
        + "；来源依据："
        + _evidence_summary(record.get("evidence", {}))
    )


def _reader_claim(claim):
    header = _readable(
        {key: value for key, value in claim.items() if key != "observations"}
    )
    observations = []
    for observation in claim.get("observations", []):
        observations.append(
            "观测值："
            + _readable(observation.get("value"))
            + "；"
            + _evidence_summary(observation.get("evidence", {}))
        )
    return header + "；" + " / ".join(observations)


def _reader_change(change):
    visible = {key: value for key, value in change.items() if key != "evidence"}
    before, after = change.get("before"), change.get("after")
    if (
        change.get("kind") == "modified"
        and isinstance(before, dict)
        and isinstance(after, dict)
    ):
        changed = {
            key
            for key in before.keys() | after.keys()
            if before.get(key) != after.get(key)
        }
        visible["before"] = {
            key: value for key, value in before.items() if key in changed
        }
        visible["after"] = {
            key: value for key, value in after.items() if key in changed
        }
    return _readable(visible)


def _reader_inference(claim):
    """A model conclusion is only readable next to the proof it cites."""
    return (
        str(claim.get("text", ""))
        + "；来源依据："
        + _evidence_summary(claim.get("evidence", []))
    )


def _coverage_line(coverage):
    planned = coverage.get("planned_pages", 0)
    fetched = coverage.get("fetched_pages", 0)
    parts = [f"计划页面：{planned}；实际抓取：{fetched}"]
    if coverage.get("truncated"):
        parts.append("分页被预算截断")
    parts.append("范围完整：" + ("是" if coverage.get("complete") else "否"))
    return "；".join(parts)


def _reader_deviation(deviation, prefix="错误"):
    """Operators need the stable code, not a prose apology, to triage a run."""
    text = f"{prefix}：{deviation.get('code')} · {deviation.get('message')}"
    if deviation.get("url"):
        text += f" · {deviation['url']}"
    if deviation.get("retryable"):
        text += " · 可重试"
    return text


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
    ]
    if data["coverage"] or data["errors"] or data["warnings"]:
        lines.append(("heading", "采集范围与运行告警"))
        if data["coverage"]:
            lines.append(("text", _coverage_line(data["coverage"])))
        lines.extend(("text", _reader_deviation(error)) for error in data["errors"])
        lines.extend(
            ("text", _reader_deviation(warning, prefix="告警"))
            for warning in data["warnings"]
        )
    lines.append(("heading", "事实"))
    lines.extend(("text", fact) for fact in data["facts"])
    lines.append(("heading", "推断"))
    lines.extend(("text", _reader_inference(claim)) for claim in data["inferences"])
    if not data["inferences"]:
        lines.append(("text", "无推断；来源独立性未验证。"))
    lines.append(("heading", "记录与证据"))
    if not data["records"]:
        lines.append(("text", "无记录。"))
    for record in data["records"][:DETAIL_LIMIT]:
        lines.append(("text", _reader_record(record)))
    if len(data["records"]) > DETAIL_LIMIT:
        lines.append(
            (
                "text",
                f"正文展示前 {DETAIL_LIMIT} 条，共 {len(data['records'])} 条；完整记录与证据见 JSON 数据文件。",
            )
        )
    lines.append(("heading", "变化：before → after 与证据"))
    for change in data["changes"][:DETAIL_LIMIT]:
        lines.append(("text", _reader_change(change)))
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
    for claim in [c for c in data["claims"] if c.get("status") == "disputed"][
        :DETAIL_LIMIT
    ]:
        lines.append(("text", _reader_claim(claim)))
    if data["claims"]:
        lines.append(
            (
                "text",
                f"共 {len(data['claims'])} 个字段观测；正文突出争议项，完整观测与冲突证据见 JSON 数据文件。",
            )
        )
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
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(THEME.margin_mm / 10)
    section.left_margin = section.right_margin = Cm(THEME.margin_mm / 10)
    for name, size, color in [
        ("Normal", THEME.body_pt, THEME.text[1:]),
        ("Title", THEME.title_pt, THEME.ink[1:]),
        ("Heading 1", THEME.section_pt, THEME.ink[1:]),
        ("Heading 2", THEME.subheading_pt, THEME.ink[1:]),
    ]:
        style = document.styles[name]
        style.font.name = "Arial"
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor.from_string(color)
        style.element.get_or_add_rPr().get_or_add_rFonts().set(
            qn("w:eastAsia"), "Microsoft YaHei"
        )
        style.paragraph_format.space_after = Pt(10)
        style.paragraph_format.line_spacing = THEME.line_height
    document.styles["Heading 1"].paragraph_format.space_before = Pt(20)
    header = section.header.paragraphs[0]
    header.text = "OmniCrawler · 调研与证据"
    header.style = document.styles["Caption"]
    footer = section.footer.paragraphs[0]
    footer.text = "OmniCrawler · 来源可追溯   |   "
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    cleaned = _clean(report.model_dump(mode="json"))
    summary_text = _readable(cleaned["summary"])
    for kind, text in _lines(report):
        if kind in {"title", "heading"}:
            document.add_heading(text, level=0 if kind == "title" else 1)
            if text == "汇总" and cleaned["summary"]:
                table = document.add_table(rows=1, cols=2)
                table.style = "Light Shading Accent 1"
                table.rows[0].cells[0].text = "汇总指标"
                table.rows[0].cells[1].text = "值"
                repeat = OxmlElement("w:tblHeader")
                table.rows[0]._tr.get_or_add_trPr().append(repeat)
                for key, value in cleaned["summary"].items():
                    cells = table.add_row().cells
                    cells[0].text, cells[1].text = (
                        _LABELS.get(key, key),
                        _readable(value),
                    )
                for index, row in enumerate(table.rows):
                    for cell in row.cells:
                        shade = OxmlElement("w:shd")
                        shade.set(
                            qn("w:fill"),
                            THEME.accent[1:]
                            if index == 0
                            else (THEME.tint[1:] if index % 2 else "FFFFFF"),
                        )
                        cell._tc.get_or_add_tcPr().append(shade)
                        margins = OxmlElement("w:tcMar")
                        for side in ["top", "bottom", "left", "right"]:
                            edge = OxmlElement("w:" + side)
                            edge.set(qn("w:w"), "100")
                            edge.set(qn("w:type"), "dxa")
                            margins.append(edge)
                        cell._tc.get_or_add_tcPr().append(margins)
                        for paragraph in cell.paragraphs:
                            paragraph.paragraph_format.space_after = Pt(4)
                            for run in paragraph.runs:
                                run.bold = index == 0
                                if index == 0:
                                    run.font.color.rgb = RGBColor.from_string("FFFFFF")
        elif text != summary_text or not cleaned["summary"]:
            paragraph = document.add_paragraph(text)
            paragraph.paragraph_format.widow_control = True
            if text in cleaned["facts"][:3]:
                for run in paragraph.runs:
                    run.bold = True
                    run.font.size = Pt(THEME.emphasis_pt)
                    run.font.color.rgb = RGBColor.from_string(THEME.ink[1:])
            if text in cleaned["sources"]:
                paragraph.paragraph_format.left_indent = Cm(0.6)
                paragraph.paragraph_format.first_line_indent = Cm(-0.6)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def _cjk_font_roots() -> list[Path]:
    """Font roots, most specific platform first.

    Windows roots are prepended because ``%WINDIR%/Fonts`` does not exist on
    macOS/Linux and vice versa, so the pre-existing fallback order is unchanged
    on the platforms it was written for.
    """
    roots = []
    windir = os.environ.get("WINDIR")
    if windir:
        roots.append(Path(windir) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        # Per-user installs land here and are absent from %WINDIR%/Fonts.
        roots.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    roots += [
        Path("/System/Library/Fonts"),
        Path("/System/Library/AssetsV2"),
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".local/share/fonts",
    ]
    return roots


def cjk_font_family() -> str | None:
    for pattern, family in [
        ("NotoSansCJK*", "Noto Sans CJK SC"),
        ("NotoSerifCJK*", "Noto Serif CJK SC"),
        ("STHeiti Medium.ttc", "STHeiti"),
        ("PingFang*", "PingFang SC"),
        # Windows ships no Noto; these are the families the docx path also names.
        ("msyh*.ttc", "Microsoft YaHei"),
        ("simsun*.ttf", "SimSun"),
        ("simhei*.ttf", "SimHei"),
    ]:
        for root in _cjk_font_roots():
            if root.exists() and next(root.rglob(pattern), None) is not None:
                return family
    return None


def _pdf_html(report: Report, family: str) -> str:
    data = _clean(report.model_dump(mode="json"))
    esc = lambda value: html.escape(str(value))
    metrics = "".join(
        f'<div class="metric"><div class="number">{esc(_readable(value))}</div><div class="label">{esc(_LABELS.get(key, key))}</div></div>'
        for key, value in data["summary"].items()
    )
    sections = []
    if data["coverage"] or data["errors"] or data["warnings"]:
        deviations = [
            f'<p class="deviation">{esc(_reader_deviation(error))}</p>'
            for error in data["errors"]
        ] + [
            f'<p class="deviation">{esc(_reader_deviation(warning, prefix="告警"))}</p>'
            for warning in data["warnings"]
        ]
        sections.append(
            "<section><h2>采集范围与运行告警</h2>"
            + (
                f"<p>{esc(_coverage_line(data['coverage']))}</p>"
                if data["coverage"]
                else ""
            )
            + "".join(deviations)
            + "</section>"
        )
    for title, findings in [
        (
            "核心发现 · 事实",
            [esc(fact) for fact in data["facts"]],
        ),
        (
            "分析与行动 · 推断",
            [esc(_reader_inference(claim)) for claim in data["inferences"]],
        ),
    ]:
        content = (
            "".join(
                f'<article class="finding"><p class="{("lead" if i <= 3 else "")}">{text}</p></article>'
                for i, text in enumerate(findings, 1)
            )
            or "<p>暂无内容。</p>"
        )
        sections.append(f"<section><h2>{title}</h2>{content}</section>")
    records = []
    for record in data["records"][:DETAIL_LIMIT]:
        fields = record.get("fields", {})
        rows = "".join(
            f'<div class="field"><b>{esc(_LABELS.get(key, key))}</b><span>{esc(_readable(value))}</span></div>'
            for key, value in fields.items()
        )
        compact = (
            "compact"
            if fields and all(len(str(value)) < 80 for value in fields.values())
            else ""
        )
        records.append(
            f'<article class="record"><h3>{esc(record.get("entity_id", "记录"))} · {esc(fields.get("topic", fields.get("name", "")))}</h3><div class="{compact}">{rows}</div><p class="evidence">{esc(_evidence_summary(record.get("evidence", {})))}</p></article>'
        )
    sections.append(
        "<section><h2>记录与来源依据</h2>"
        + "".join(records)
        + f'<p class="note">共 {len(data["records"])} 条记录，正文最多展示 {DETAIL_LIMIT} 条。完整数据与证据见独立 JSON 文件。</p></section>'
    )
    if data["changes"]:
        rows = "".join(
            "<p>" + esc(_reader_change(change)) + "</p>"
            for change in data["changes"][:DETAIL_LIMIT]
        )
        sections.append(
            "<section><h2>变化 · before → after</h2>"
            + rows
            + f'<p class="note">共 {len(data["changes"])} 条变化；完整前后证据见 JSON 文件。</p></section>'
        )
    disputed = [claim for claim in data["claims"] if claim.get("status") == "disputed"]
    if disputed:
        sections.append(
            "<section><h2>字段冲突与观测</h2>"
            + "".join(
                "<p>" + esc(_reader_claim(claim)) + "</p>"
                for claim in disputed[:DETAIL_LIMIT]
            )
            + '<p class="note">完整争议与观测见 JSON 文件。</p></section>'
        )
    sources = "".join(
        f'<p class="source"><b>{i:02}</b> {esc(url)}</p>'
        for i, url in enumerate(data["sources"], 1)
    )
    css = """
    body {font-size:12pt; color:#243447; line-height:1.75; margin:0}
    .cover {border-top:8px solid #087f8c; padding:24px 0 20px; margin-bottom:24px; border-bottom:1px solid #d9e3eb}
    .eyebrow {font-size:10pt; letter-spacing:2px; color:#087f8c; font-weight:700}
    h1 {font-size:30pt; line-height:1.35; color:#102c46; margin:14px 0 20px; letter-spacing:-.5px}
    .goal {font-size:14pt; line-height:1.7} .meta {font-size:10.5pt; color:#5b6f82}
    h2 {font-size:20pt; line-height:1.4; margin:30px 0 18px; color:#102c46; border-left:5px solid #087f8c; padding-left:12px; break-after:avoid}
    h3 {font-size:15pt; color:#102c46; line-height:1.5; margin:0 0 14px; break-after:avoid}
    .metrics {display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:24px 0; break-inside:avoid}
    .metric {background:#edf5f6; border-radius:3px; padding:14px 16px} .number {font-size:24pt; font-weight:700; color:#087f8c; line-height:1.2} .label {font-size:11pt; margin-top:8px}
    p {margin:0 0 14px; overflow-wrap:anywhere; orphans:3; widows:3}
    .finding {margin-bottom:20px; break-inside:avoid}
    .index {position:absolute; left:0; top:3px; color:#087f8c; font-weight:700; font-size:13pt}
    .lead {font-size:14pt; font-weight:600; color:#102c46}
    .record {break-inside:avoid; border-top:1px solid #d9e3eb; padding:18px 0; margin:12px 0}
    .field {display:grid; grid-template-columns:90px 1fr; gap:12px; margin:8px 0; break-inside:avoid}
    .change-section {break-inside:avoid}
    .source-group {break-inside:avoid}
    .compact {display:grid; grid-template-columns:1fr 1fr; gap:0 20px}
    .compact .field {grid-template-columns:70px 1fr; margin:4px 0}
    .field b {color:#5b6f82; font-size:11pt} .evidence,.note,.source,.deviation {font-size:10.5pt; color:#5b6f82; line-height:1.7}
    .evidence {background:#f3f6f9; padding:12px; border-radius:3px; margin-top:14px} .source {break-inside:avoid; padding-bottom:10px; border-bottom:1px solid #e6edf2}
    """
    for original, replacement in {
        "12pt": f"{THEME.body_pt}pt",
        "30pt": f"{THEME.title_pt}pt",
        "20pt": f"{THEME.section_pt}pt",
        "15pt": f"{THEME.subheading_pt}pt",
        "14pt": f"{THEME.emphasis_pt}pt",
        "10.5pt": f"{THEME.evidence_pt}pt",
        "#243447": THEME.text,
        "#102c46": THEME.ink,
        "#087f8c": THEME.accent,
        "#5b6f82": THEME.muted,
        "#edf5f6": THEME.tint,
        "#d9e3eb": THEME.rule,
    }.items():
        css = css.replace(original, replacement)
    css += f"body {{line-height:{THEME.line_height}}} @media print {{* {{print-color-adjust:exact}}}}"
    return (
        f'<!doctype html><html lang="zh"><meta charset="utf-8"><style>body {{font-family:"{esc(family)}",sans-serif}}{css}</style><body><header class="cover"><div class="eyebrow">OmniCrawler 调研报告</div><h1>{esc(data["title"])}</h1><p class="goal">{esc(data["goal"])}</p><p class="meta">{esc(data["generated_at"][:10])} · {esc({"succeeded": "已完成", "partial": "部分完成", "blocked": "访问受限", "failed": "执行失败"}.get(data["status"], data["status"]))} · 来源与结论分层呈现</p><div class="metrics">{metrics}</div></header>'
        + "".join(sections)
        + f'<section class="{("source-group" if len(data["sources"]) <= 8 else "")}"><h2>来源索引</h2>{sources}</section></body></html>'
    )


async def render_pdf(report: Report) -> bytes:
    family = cjk_font_family()
    if not family:
        raise RuntimeError("PDF 未交付：缺少 CJK 字体，请安装 Noto CJK；保留其他格式。")
    from playwright.async_api import async_playwright

    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(Path(".browser-cache").resolve())
    )
    content = _pdf_html(report, family)
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
                display_header_footer=True,
                header_template="<span></span>",
                footer_template='<div style="font-size:9px;color:#64748b;width:100%;margin:0 15mm;display:flex;justify-content:space-between"><span>OmniCrawler · 调研与证据</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>',
                margin={
                    "top": f"{THEME.margin_mm}mm",
                    "bottom": f"{THEME.margin_mm}mm",
                    "left": f"{THEME.margin_mm}mm",
                    "right": f"{THEME.margin_mm}mm",
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
