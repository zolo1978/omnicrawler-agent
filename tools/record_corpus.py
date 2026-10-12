"""Record real public pages into an offline extraction corpus.

Why this exists: every fetch target in the test suite is a two-item loopback
fixture, so nothing measures whether extraction survives real markup.  This
script captures real responses once, gzipped, next to a manifest, so the
integration tests can replay genuine bytes without network access.

Design choices, deliberately conservative:

* stdlib only (``urllib``), not ``omnicrawler.transport``.  Recording is a
  one-shot maintenance action that must run even when the package or its
  optional extras are not installed, and it must not silently inherit whatever
  the library's fetch policy happens to be doing this week.
* robots.txt is consulted per origin before any page request and a disallowed
  path is skipped, reported, and never stored.
* one request per URL per run, ``Accept-Encoding: identity`` so the stored
  bytes are the origin's markup rather than a transport encoding.
* idempotent: a URL already recorded under the same slug is left untouched
  unless ``--force`` is passed, and the manifest is rewritten in sorted order.

Run ``--check`` to prove the manifest still describes the stored files.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

USER_AGENT = (
    "OmniCrawlerCorpus/0.1 (+local test-fixture recorder; "
    "single-request archival of public documentation pages)"
)
CORPUS_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "corpus"
MANIFEST_NAME = "manifest.json"
# Listing pages top out around half a megabyte of markup; the cap keeps any
# accidentally huge page (or an error page that balloons) out of the corpus,
# which must stay replayable inside a single test session.
DEFAULT_MAX_BYTES = 500_000


def slug_for(url: str) -> str:
    """Stable, human-readable file name derived from the URL alone."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").removeprefix("www.")
    path = parsed.path.rstrip("/")
    slug = re.sub(r"[^a-z0-9]+", "-", f"{host}{path}".lower()).strip("-")
    return (slug or "page")[:90]


def _fetch(url: str, proxy: str | None, timeout: float = 30.0, attempts: int = 4):
    """One GET, with bounded retries that never reach the origin twice.

    ``attempts`` only covers failures to establish a connection (TLS handshake
    aborted, DNS hiccup, proxy dropping the CONNECT).  Such an attempt never
    produced a request at the origin, so retrying keeps the "one request per
    URL" promise while making recording usable behind a flaky network path.
    A response of any status is returned as-is and never retried.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "identity",
        },
    )
    handlers = (
        [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    )
    opener = urllib.request.build_opener(*handlers)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with opener.open(request, timeout=timeout) as response:
                return (
                    response.status,
                    response.geturl(),
                    response.headers.get("Content-Type", ""),
                    response.read(),
                )
        except urllib.error.HTTPError:
            raise  # A status was received: that is the origin's answer.
        except Exception as exc:  # noqa: BLE001 — transport-level failure, retry
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise last_error if last_error else RuntimeError(f"no response from {url}")


def robots_rules(
    origin_root: str, proxy: str | None
) -> tuple[RobotFileParser | None, str]:
    """Per-origin rules. ``(None, reason)`` means "cannot verify" -> fail closed."""
    rules_url = origin_root + "/robots.txt"
    try:
        _, _, _, body = _fetch(rules_url, proxy)
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 410}:
            parser = RobotFileParser()
            parser.parse([])  # No robots.txt means no declared restrictions.
            return parser, rules_url
        return None, f"{rules_url} -> HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 — any transport failure must fail closed
        return None, f"{rules_url} -> {type(exc).__name__}: {exc}"
    parser = RobotFileParser()
    parser.parse(body.decode("utf-8", errors="replace").splitlines())
    return parser, rules_url


def record(entry_url: str, *, proxy, force, max_bytes, corpus_dir, manifest) -> str:
    """Fetch and store one URL. Returns a one-line human status."""
    slug = slug_for(entry_url)
    origin_root = "{0.scheme}://{0.netloc}".format(urlsplit(entry_url))
    existing = manifest["pages"].get(slug)
    target = corpus_dir / f"{slug}.html.gz"
    if existing and existing["url"] == entry_url and target.exists() and not force:
        return f"unchanged  {slug}"

    parser, note = robots_rules(origin_root, proxy)
    if parser is None:
        return f"skip       {slug}: robots.txt unverifiable ({note})"
    if not parser.can_fetch(USER_AGENT, entry_url):
        return f"skip       {slug}: robots.txt disallows {entry_url}"
    try:
        status, final_url, content_type, body = _fetch(entry_url, proxy)
    except Exception as exc:  # noqa: BLE001 — report the failure, keep the corpus intact
        return f"skip       {slug}: {type(exc).__name__}: {exc}"
    if status != 200:
        return f"skip       {slug}: HTTP {status}"
    if final_url != entry_url and not parser.can_fetch(USER_AGENT, final_url):
        return f"skip       {slug}: redirect target disallowed by robots.txt"
    if len(body) > max_bytes:
        return f"skip       {slug}: {len(body)} bytes exceeds --max-bytes {max_bytes}"

    payload = gzip.compress(body, mtime=0)  # mtime=0 keeps re-runs byte-identical
    target.write_bytes(payload)
    manifest["pages"][slug] = {
        "slug": slug,
        "url": entry_url,
        "final_url": final_url,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "content_type": content_type,
        "http_status": status,
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "gzip_bytes": len(payload),
        "path": target.name,
    }
    return f"recorded   {slug} ({len(body)} bytes -> {len(payload)} gzipped)"


def write_manifest(corpus_dir: Path, manifest: dict) -> None:
    manifest["pages"] = dict(sorted(manifest["pages"].items()))
    (corpus_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def load_manifest(corpus_dir: Path) -> dict:
    path = corpus_dir / MANIFEST_NAME
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"user_agent": USER_AGENT, "pages": {}}


def check(corpus_dir: Path) -> int:
    """Prove the manifest still describes the stored files. Returns exit code."""
    manifest = load_manifest(corpus_dir)
    failures = []
    stored = set()
    total = 0
    for slug, page in sorted(manifest["pages"].items()):
        path = corpus_dir / page["path"]
        stored.add(page["path"])
        if not path.exists():
            failures.append(f"{slug}: missing {page['path']}")
            continue
        body = gzip.decompress(path.read_bytes())
        total += len(body)
        if hashlib.sha256(body).hexdigest() != page["sha256"]:
            failures.append(f"{slug}: sha256 mismatch")
        if len(body) != page["bytes"]:
            failures.append(f"{slug}: byte length {len(body)} != {page['bytes']}")
    for path in sorted(corpus_dir.glob("*.html.gz")):
        if path.name not in stored:
            failures.append(f"{path.name}: stored file absent from manifest")
    print(f"{len(manifest['pages'])} recorded page(s), {total / 1024:.1f} KiB raw")
    for failure in failures:
        print(f"  FAIL {failure}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Record public pages into tests/fixtures/corpus so extraction quality "
            "is testable without network access."
        )
    )
    parser.add_argument("urls", nargs="*", help="page URLs to record")
    parser.add_argument(
        "--proxy",
        default=None,
        help="HTTP(S) proxy URL, e.g. http://127.0.0.1:7897 (defaults to env proxies)",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-fetch URLs that are already recorded"
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"skip responses larger than this many raw bytes (default {DEFAULT_MAX_BYTES})",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-fetch every URL already in the manifest (idempotent top-up)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the manifest against the stored files and exit",
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=CORPUS_DIR,
        help="corpus directory (defaults to tests/fixtures/corpus)",
    )
    args = parser.parse_args(argv)

    corpus_dir: Path = args.corpus_dir
    corpus_dir.mkdir(parents=True, exist_ok=True)
    if args.check:
        return check(corpus_dir)

    manifest = load_manifest(corpus_dir)
    manifest["user_agent"] = USER_AGENT
    targets = list(args.urls)
    if args.refresh:
        targets.extend(page["url"] for page in manifest["pages"].values())
    if not targets:
        parser.error("provide at least one URL, or --refresh, or --check")

    exit_code = 0
    # ``--refresh`` re-fetches what is already recorded, so it must bypass the
    # idempotency guard; without this it would only re-print "unchanged".
    force = args.force or args.refresh
    for url in dict.fromkeys(targets):
        status = record(
            url,
            proxy=args.proxy,
            force=force,
            max_bytes=args.max_bytes,
            corpus_dir=corpus_dir,
            manifest=manifest,
        )
        print(status)
        if status.startswith("skip"):
            exit_code = 1
    write_manifest(corpus_dir, manifest)
    print(f"manifest: {len(manifest['pages'])} page(s) in {corpus_dir / MANIFEST_NAME}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
