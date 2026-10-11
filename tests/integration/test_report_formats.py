import asyncio
import unicodedata
from datetime import UTC, datetime
from io import BytesIO

from docx import Document
from pypdf import PdfReader

from omnicrawler import report as reporting


def sample():
    proof = {
        "url": "https://example.com/item",
        "locator": ".price",
        "captured_at": "2026-10-11T10:00:00+00:00",
        "excerpt": "商品价格证据",
    }
    return reporting.Report(
        title="中文采集报告",
        generated_at=datetime.now(UTC),
        goal="价格监控",
        task_type="monitor",
        summary={"modified": 1},
        records=[
            {"entity_id": "A", "fields": {"price": "20"}, "evidence": {"price": proof}}
        ],
        changes=[
            {
                "kind": "modified",
                "before": {"price": "10"},
                "after": {"price": "20"},
                "evidence": {"before": proof, "after": proof},
            }
        ],
        sources=["https://example.com/item"],
        schedule_seconds=60,
        facts=["确认价格变化"],
        inferences=["可能调整定价"],
    )


def test_word_contains_chinese_change_and_evidence():
    document = Document(BytesIO(reporting.render_docx(sample())))
    text = "\n".join(p.text for p in document.paragraphs)
    text = unicodedata.normalize("NFKC", text)
    for target in [
        "中文采集报告",
        "事实",
        "推断",
        "before",
        "after",
        "10",
        "20",
        ".price",
        "https://example.com/item",
        "2026-10-11T10:00:00+00:00",
        "可能调整定价",
    ]:
        assert target in text


def test_pdf_actual_chinese_text_and_evidence():
    data = asyncio.run(reporting.render_pdf(sample()))
    text = unicodedata.normalize(
        "NFKC",
        "\n".join(page.extract_text() for page in PdfReader(BytesIO(data)).pages),
    )
    for target in [
        "中文采集报告",
        "价格监控",
        "事实",
        "推断",
        ".price",
        "https://example.com/item",
        "before",
        "after",
    ]:
        assert target in text


def test_missing_font_downgrades_without_losing_other_files(tmp_path, monkeypatch):
    monkeypatch.setattr(reporting, "cjk_font_family", lambda: None)
    result = asyncio.run(reporting.deliver(sample(), tmp_path))
    assert set(result["files"]) == {"md", "docx", "json"}
    assert any("CJK" in warning for warning in result["warnings"])
    assert not (tmp_path / "report.pdf").exists()


def test_missing_font_removes_previous_artifact(tmp_path, monkeypatch):
    (tmp_path / "report.pdf").write_bytes(b"old private artifact")
    monkeypatch.setattr(reporting, "cjk_font_family", lambda: None)
    result = asyncio.run(reporting.deliver(sample(), tmp_path, formats=("md", "pdf")))
    assert set(result["files"]) == {"md"}
    assert not (tmp_path / "report.pdf").exists()


def test_missing_word_dependency_preserves_markdown_json(tmp_path, monkeypatch):
    import builtins

    original_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "docx":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    result = asyncio.run(
        reporting.deliver(sample(), tmp_path, formats=("md", "docx", "json"))
    )
    assert set(result["files"]) == {"md", "json"}
    assert any("python-docx" in warning for warning in result["warnings"])


def test_word_tables_and_all_formats_keep_disputed_proof(tmp_path):
    report = sample().model_copy(
        update={
            "claims": [
                {
                    "status": "disputed",
                    "observations": [
                        {
                            "value": "999",
                            "evidence": {
                                "url": "https://conflict.com/",
                                "locator": ".disputed",
                                "captured_at": "2026-10-11T11:00:00+00:00",
                            },
                        }
                    ],
                }
            ]
        }
    )
    document = Document(BytesIO(reporting.render_docx(report)))
    assert document.tables
    text = "\n".join(p.text for p in document.paragraphs)
    assert ".disputed" in text
    pdf = asyncio.run(reporting.render_pdf(report))
    assert ".disputed" in "\n".join(
        p.extract_text() for p in PdfReader(BytesIO(pdf)).pages
    )
    result = asyncio.run(reporting.deliver(report, tmp_path, formats=("md", "json")))
    for path in result["files"].values():
        assert ".disputed" in __import__("pathlib").Path(path).read_text()


def test_all_formats_redact_embedded_url_and_cookie(tmp_path):
    report = sample().model_copy(
        update={
            "goal": "监控 https://example.com/?token=TOP_SECRET&category=public\nCookie: session=COOKIE_SECRET"
        }
    )
    result = asyncio.run(reporting.deliver(report, tmp_path))
    assert set(result["files"]) == {"md", "docx", "pdf", "json"}
    from pathlib import Path

    texts = [Path(result["files"][key]).read_text() for key in ("md", "json")]
    document = Document(result["files"]["docx"])
    texts.append("\n".join(p.text for p in document.paragraphs))
    texts.append(
        "\n".join(p.extract_text() for p in PdfReader(result["files"]["pdf"]).pages)
    )
    for text in texts:
        assert "TOP_SECRET" not in text.replace("\\", "")
        assert "COOKIE_SECRET" not in text.replace("\\", "")


def test_existing_directory_does_not_block_other_deliverables(tmp_path):
    occupied = tmp_path / "report.docx"
    occupied.mkdir()
    protected = occupied / "keep.txt"
    protected.write_text("user content")
    result = asyncio.run(
        reporting.deliver(sample(), tmp_path, formats=("md", "docx", "json"))
    )
    assert set(result["files"]) == {"md", "json"}
    assert result["warnings"]
    assert protected.read_text() == "user content"


def test_cleanup_failure_is_warning_and_does_not_abort(tmp_path, monkeypatch):
    from pathlib import Path

    original_unlink = Path.unlink

    def cannot_delete(path, *args, **kwargs):
        if path.name == "report.pdf":
            raise PermissionError("cannot delete")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(reporting, "cjk_font_family", lambda: None)
    monkeypatch.setattr(Path, "unlink", cannot_delete)
    result = asyncio.run(reporting.deliver(sample(), tmp_path, formats=("pdf", "json")))
    assert set(result["files"]) == {"json"}
    assert any("清理" in warning for warning in result["warnings"])


def test_word_has_readable_body_and_prominent_headings():
    document = Document(BytesIO(reporting.render_docx(sample())))
    assert document.styles["Normal"].font.size.pt >= 12
    assert document.styles["Title"].font.size.pt >= 28
    assert document.styles["Heading 1"].font.size.pt >= 18
    assert document.sections[0].header.paragraphs[0].text
