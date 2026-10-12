"""Replay the recorded page corpus through the real pipeline, offline.

Why: every other integration target is a two-item loopback fixture, so nothing
measured whether extraction survives real markup.  ``tools/record_corpus.py``
captured real, robots-permitted listing pages into ``tests/fixtures/corpus``;
this module serves those exact bytes back over loopback and asserts that the
production extraction path still turns them into records whose every field
carries evidence pointing at the run's own stored bundle.

The synthetic fixtures cover what real pages cannot supply on demand: a GBK
page, a deeply nested table, missing/duplicate identity, a field class renamed
mid-listing, a pagination cycle, and a 200-item page for the report row cap.
"""

import asyncio
import gzip
import hashlib
import json
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnicrawler import pipeline, report, storage

CORPUS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "corpus"
RECORDER = Path(__file__).resolve().parents[2] / "tools" / "record_corpus.py"
MANIFEST = json.loads((CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
MANIFEST_PAGES: dict = MANIFEST["pages"]
GBK_FIXTURE = "synthetic_gbk_listing.html"


@dataclass(frozen=True)
class Listing:
    """The caller's authorized extraction scope for one recorded page."""

    item_selector: str
    id_attribute: str
    fields: dict[str, tuple[str, ...]]
    # Records observed when the page was recorded; the assertion is a floor so a
    # legitimate extraction improvement does not require re-recording the corpus,
    # while a regression that drops items fails loudly.
    min_records: int
    # Field-level deviations the page is known to cause today.  Kept explicit so
    # real-world degradation is asserted rather than tolerated by accident.
    warnings: tuple[str, ...] = ()


# Fallback chains are deliberate: real markup nests the same value under
# different wrappers (a link text, a <code> span, a heading span).
RECORDED: dict[str, Listing] = {
    "docs-python-org-3-py-modindex-html": Listing(
        "table.modindextable a", "href", {"module": ("code.xref", "code")}, 310
    ),
    # Sphinx cross-reference anchors whose label is the anchor's own text (no nested
    # <span>): the last chain entry matches the item element itself, so all 279
    # entries resolve instead of 40 silently leaving the entity universe.
    "docs-python-org-3-library-index-html": Listing(
        "a.reference.internal",
        "href",
        {"title": ("span.std-ref", "span", "code", "a.reference.internal")},
        270,
    ),
    "developer-mozilla-org-en-us-docs-web-http-status": Listing(
        "dt[id]", "id", {"status": ("a code", "code", "a")}, 60
    ),
    "developer-mozilla-org-en-us-docs-web-http-methods": Listing(
        "dt[id]", "id", {"method": ("a code", "code", "a")}, 10
    ),
    "developer-mozilla-org-en-us-docs-web-accessibility-aria-attributes": Listing(
        "dt[id]", "id", {"attribute": ("a code", "code", "a")}, 50
    ),
    "developer-mozilla-org-en-us-docs-web-css-css-selectors": Listing(
        "dt[id]", "id", {"selector": ("a code", "code", "a")}, 8
    ),
    "en-wikipedia-org-wiki-list-of-http-status-codes": Listing(
        "section[id]", "id", {"heading": ("div.mw-heading", "h2", "h3", "div")}, 25
    ),
    "en-wikipedia-org-wiki-list-of-http-header-fields": Listing(
        "section[id]", "id", {"heading": ("div.mw-heading", "h2", "h3", "div")}, 120
    ),
    "en-wikipedia-org-wiki-list-of-dns-record-types": Listing(
        "section[id]", "id", {"heading": ("div.mw-heading", "h2", "h3", "div")}, 6
    ),
    "postgresql-org-docs-release-16-0": Listing(
        "div[id]", "id", {"heading": ("h2", "h3", "li", "a", "p")}, 32
    ),
    "postgresql-org-docs-release-15-0": Listing(
        "div[id]", "id", {"heading": ("h2", "h3", "li", "a", "p")}, 35
    ),
    "pkg-go-dev-std": Listing("tr[data-id]", "data-id", {"package": ("a", "td")}, 310),
}

# Synthetic fixtures are served with the header the fixture is about: the GBK
# page declares its encoding in <meta> only, which is exactly the case the
# transport does not read today.
_SYNTHETIC_TYPES = {GBK_FIXTURE: "text/html"}


@pytest.fixture(scope="module")
def corpus_site():
    """Serve the stored corpus bytes (and synthetic fixtures) over loopback."""
    routes: dict[str, tuple[bytes, str]] = {}
    for slug, page in MANIFEST_PAGES.items():
        routes[f"/{slug}.html"] = (
            gzip.decompress((CORPUS_DIR / page["path"]).read_bytes()),
            page["content_type"] or "text/html",
        )
    for fixture in sorted(CORPUS_DIR.glob("synthetic_*.html")):
        routes[f"/{fixture.name}"] = (
            fixture.read_bytes(),
            _SYNTHETIC_TYPES.get(fixture.name, "text/html; charset=utf-8"),
        )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                body = b"User-agent: *\nAllow: /\n"
            else:
                route = routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body, content_type = route
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def collect_page(
    origin, tmp_path, path, *, item_selector, id_attribute, fields, **overrides
):
    """Collect one local page with the real pipeline. Returns (url, result, store).

    The engine is pinned to the deterministic HTML path: with the optional
    crawl4ai extra installed, ``engine="auto"`` would resolve differently on
    different machines, and this corpus exists to test extraction, not routing.
    """
    url = origin + path
    config = pipeline.CollectionConfig(
        urls=[url],
        allowed_hosts=["127.0.0.1"],
        local_origins=[origin],
        item_selector=item_selector,
        id_attribute=id_attribute,
        fields=fields,
        delay=0,
        engine="html",
        **overrides,
    )
    store = storage.SnapshotStore(tmp_path / "corpus.sqlite")
    result = asyncio.run(pipeline.collect(config, store))
    return url, result, store


def collect_recorded(origin, tmp_path, slug, **overrides):
    listing = RECORDED[slug]
    return collect_page(
        origin,
        tmp_path,
        f"/{slug}.html",
        item_selector=listing.item_selector,
        id_attribute=listing.id_attribute,
        fields={name: list(chain) for name, chain in listing.fields.items()},
        **overrides,
    )


def test_every_manifest_page_is_exercised_by_a_listing_config():
    """A recorded page nobody queries would silently rot in the corpus."""
    assert set(RECORDED) == set(MANIFEST_PAGES)


def test_manifest_matches_stored_files():
    """The manifest is the corpus's contract: size, hash and diversity."""
    stored_bytes = 0
    hosts = set()
    for slug, page in MANIFEST_PAGES.items():
        path = CORPUS_DIR / page["path"]
        assert path.exists(), f"{slug} is missing {page['path']}"
        blob = path.read_bytes()
        stored_bytes += len(blob)
        assert len(blob) == page["gzip_bytes"]
        body = gzip.decompress(blob)
        assert len(body) == page["bytes"]
        assert hashlib.sha256(body).hexdigest() == page["sha256"]
        assert page["content_type"].startswith("text/html")
        hosts.add(page["url"].split("/")[2])

    on_disk = {path.name for path in CORPUS_DIR.glob("*.html.gz")}
    assert on_disk == {page["path"] for page in MANIFEST_PAGES.values()}
    assert len(MANIFEST_PAGES) >= 10
    assert len(hosts) >= 3

    synthetic = [path for path in CORPUS_DIR.glob("synthetic_*.html")]
    assert len(synthetic) >= 8
    stored_bytes += sum(path.stat().st_size for path in synthetic)
    assert stored_bytes < 2_000_000, "corpus is meant to stay replayable in one session"


def test_recorder_check_mode_validates_the_stored_corpus():
    """The tool that produced the corpus must still be able to vouch for it."""
    completed = subprocess.run(
        [sys.executable, str(RECORDER), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"{len(MANIFEST_PAGES)} recorded page(s)" in completed.stdout


@pytest.mark.parametrize("slug", sorted(RECORDED))
def test_recorded_listing_extracts_records_with_evidence(corpus_site, tmp_path, slug):
    url, result, store = collect_recorded(corpus_site, tmp_path, slug)

    assert result.status == "succeeded", result.errors
    assert result.errors == []
    assert [warning.code for warning in result.warnings] == list(
        RECORDED[slug].warnings
    )
    assert result.coverage.fetched_pages == 1
    assert result.coverage.complete is True

    bundle = store.bundle(result.snapshot_id)
    records = bundle["records"]
    assert len(records) >= RECORDED[slug].min_records
    assert {name for record in records for name in record["fields"]} == set(
        RECORDED[slug].fields
    )
    for record in records:
        assert record["entity_id"]
        assert record["fields"]
        assert set(record["fields"]) == set(record["evidence"])
        for name, evidence in record["evidence"].items():
            value = record["fields"][name]
            assert value, (slug, name)
            assert evidence["url"] == url
            assert record["entity_id"] in evidence["locator"]
            assert evidence["excerpt"] == value
            assert "\ufffd" not in value, (
                "recorded pages are UTF-8; no silent corruption"
            )

    fetch = bundle["fetches"][0]
    assert fetch["url"] == url and fetch["requested_url"] == url
    assert len(fetch["hash"]) == 64 and fetch["extracted_hash"] == fetch["hash"]
    assert bundle["claims"], "field observations must be recorded for corroboration"
    assert bundle["engine"] == "html"


def test_evidence_points_back_into_the_stored_bundle(corpus_site, tmp_path):
    """The recorded bytes themselves must be retrievable from the run's bundle."""
    slug = "docs-python-org-3-py-modindex-html"
    url, result, store = collect_recorded(
        corpus_site, tmp_path, slug, store_raw_html=True
    )

    raw = store.raw_for_fetch(result.snapshot_id, url)
    assert raw is not None and raw["url"] == url
    served = gzip.decompress((CORPUS_DIR / MANIFEST_PAGES[slug]["path"]).read_bytes())
    assert (
        hashlib.sha256(raw["html"].encode()).hexdigest()
        == MANIFEST_PAGES[slug]["sha256"]
    )
    assert raw["html"].encode() == served

    for record in store.bundle(result.snapshot_id)["records"]:
        for value in record["fields"].values():
            assert value in raw["html"], (
                "every excerpt must exist in the stored document"
            )


def test_nested_table_listing_finds_every_record(corpus_site, tmp_path):
    _, result, store = collect_page(
        corpus_site,
        tmp_path,
        "/synthetic_nested_table.html",
        item_selector="table.listing article",
        id_attribute="data-id",
        fields={"name": ["span.name"], "price": ["span.price"]},
    )
    assert result.status == "succeeded" and result.errors == []
    records = store.bundle(result.snapshot_id)["records"]
    assert [record["entity_id"] for record in records] == ["N1", "N2", "N3", "N4", "N5"]
    assert records[0]["fields"] == {"name": "Alpha", "price": "10"}


def test_missing_identity_is_an_entity_level_error(corpus_site, tmp_path):
    """An anonymous row makes the entity universe incomplete, not merely partial."""
    _, result, store = collect_page(
        corpus_site,
        tmp_path,
        "/synthetic_missing_identity.html",
        item_selector="article",
        id_attribute="data-id",
        fields={"name": ["span.name"]},
    )
    assert [error.code for error in result.errors] == ["entity_identity_missing"]
    assert result.status == "partial"
    assert result.coverage.complete is False
    assert sorted(
        record["entity_id"] for record in store.bundle(result.snapshot_id)["records"]
    ) == ["M1", "M3"]


def test_duplicate_entity_ids_with_different_values_are_conflicting(
    corpus_site, tmp_path
):
    _, result, store = collect_page(
        corpus_site,
        tmp_path,
        "/synthetic_duplicate_ids.html",
        item_selector="article",
        id_attribute="data-id",
        fields={"name": ["span.name"], "price": ["span.price"]},
    )
    assert [error.code for error in result.errors] == ["conflicting_entity_values"]
    assert result.coverage.complete is False
    bundle = store.bundle(result.snapshot_id)
    assert len(bundle["records"]) == 2
    assert bundle["claims"][1]["entity_id"] == "D1"
    assert bundle["claims"][1]["status"] == "disputed"


def test_renamed_field_class_warns_but_keeps_other_fields(corpus_site, tmp_path):
    """One renamed class must not zero the page: field loss is a warning."""
    _, result, store = collect_page(
        corpus_site,
        tmp_path,
        "/synthetic_renamed_class.html",
        item_selector="article",
        id_attribute="data-id",
        fields={"name": ["span.name"], "price": ["span.price"]},
    )
    assert result.errors == []
    assert [warning.code for warning in result.warnings] == ["field_selector_no_match"]
    assert "price" in result.warnings[0].message
    assert result.status == "succeeded"
    assert result.coverage.complete is True

    records = {
        record["entity_id"]: record["fields"]
        for record in store.bundle(result.snapshot_id)["records"]
    }
    assert set(records) == {"R1", "R2", "R3", "R4"}
    assert all("name" in fields for fields in records.values())
    assert [name for name, fields in sorted(records.items()) if "price" in fields] == [
        "R1",
        "R2",
    ]


def test_looping_pagination_terminates_on_the_visited_set(corpus_site, tmp_path):
    """A → B → A must stop on the visited set, not on the page budget."""
    origin = corpus_site
    _, result, store = collect_page(
        origin,
        tmp_path,
        "/synthetic_looping_next.html",
        item_selector="article",
        id_attribute="data-id",
        fields={"name": ["span.name"]},
        pagination=pipeline.Pagination(next_selector="a.next", max_pages=5),
        max_pages=20,
    )
    assert result.errors == []
    assert result.status == "succeeded"
    assert result.coverage.fetched_pages == 2
    assert result.coverage.truncated is False
    bundle = store.bundle(result.snapshot_id)
    assert [fetch["url"] for fetch in bundle["fetches"]] == [
        origin + "/synthetic_looping_next.html",
        origin + "/synthetic_looping_next_b.html",
    ]
    assert sorted(record["entity_id"] for record in bundle["records"]) == [
        "LA1",
        "LA2",
        "LB1",
        "LB2",
    ]


def test_two_hundred_items_keep_every_record_while_the_report_caps_rows(
    corpus_site, tmp_path
):
    _, result, store = collect_page(
        corpus_site,
        tmp_path,
        "/synthetic_many_items.html",
        item_selector="article",
        id_attribute="data-id",
        fields={"name": ["span.name"], "price": ["span.price"]},
    )
    assert result.status == "succeeded" and result.errors == []
    assert len(store.bundle(result.snapshot_id)["records"]) == 200

    built = report.build_report(
        store, result.model_dump(), task_type="once", title="Corpus"
    )
    assert len(built.records) == 200
    markdown = report.render_markdown(built)
    assert markdown.count("来源依据") == report.DETAIL_LIMIT == 20
    assert "共 200 条" in markdown


def test_gbk_page_is_decoded_from_its_declared_charset(corpus_site, tmp_path):
    """The fixture declares ``<meta charset="gbk">`` and is really GBK; the response
    header carries no charset, so the document's own declaration must win.

    This test previously recorded the silent-corruption defect verbatim (every
    non-ASCII byte became U+FFFD and the run still reported success). The transport
    now honours header/BOM/in-document charsets and reports a lossy decode, so the
    assertions flipped to the corrected behaviour instead of being deleted.
    """
    _, result, store = collect_page(
        corpus_site,
        tmp_path,
        f"/{GBK_FIXTURE}",
        item_selector="article",
        id_attribute="data-id",
        fields={"name": ["span.name"], "price": ["span.price"]},
    )
    records = {
        record["entity_id"]: record["fields"]
        for record in store.bundle(result.snapshot_id)["records"]
    }
    assert set(records) == {"G1", "G2", "G3"}
    assert records["G1"]["price"] == "10"
    names = [fields["name"] for fields in records.values()]

    # The bytes really are GBK and the page really does say so …
    declared = (CORPUS_DIR / GBK_FIXTURE).read_bytes().decode("gbk")
    assert "中文名称一" in declared and 'charset="gbk"' in declared
    assert "\ufffd" not in declared
    # … and the stored field values are the page's actual text, not replacement chars.
    assert set(names) == {"中文名称一", "中文名称二", "中文名称三"}, names
    assert result.status == "succeeded"
    assert result.errors == [] and result.warnings == []
