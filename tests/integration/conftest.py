import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def installed(module):
    """Whether an optional dependency is importable in this environment."""
    return importlib.util.find_spec(module) is not None


def chromium_available():
    """Whether a real browser download exists; the package alone cannot render.

    Resolves the same cache the collector uses: ``PLAYWRIGHT_BROWSERS_PATH`` when
    the operator set it, otherwise the repo-local ``.browser-cache`` (see
    ``engines.py``). Without this, a checkout that followed the README install
    step would silently skip every browser test.
    """
    import os

    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(Path(".browser-cache").resolve())
    )
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).is_file()
    except Exception:  # noqa: BLE001 — an unusable browser set skips, never fails
        return False


@pytest.fixture
def site():
    state = {"products": [("A", "79"), ("B", "20")], "hits": [], "routes": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"].append(self.path)
            if self.path == "/robots.txt":
                body = b"User-agent: *\nDisallow: /private"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/private")
                self.end_headers()
                return
            if self.path == "/deniedbig":
                self.send_response(403)
                self.send_header("Content-Length", "99999999")
                self.end_headers()
                return
            if self.path == "/denied":
                self.send_response(403)
                self.end_headers()
                return
            status = 200
            headers = {}
            custom = state["routes"].get(self.path)
            if custom is not None:
                status, body, headers = (*custom, {})[:3]
                if isinstance(body, str):
                    body = body.encode()
            else:
                body = (
                    "<html><body>"
                    + "".join(
                        f'<article data-id="{key}"><span class="price">{price}</span></article>'
                        for key, price in state.get("by_path", {}).get(
                            self.path, state["products"]
                        )
                    )
                    + "</body></html>"
                ).encode()
                if state.get("extra_html"):
                    body = body.replace(
                        b"</body>", state["extra_html"].encode() + b"</body>"
                    )
            self.send_response(status)
            self.send_header(
                "Content-Type", headers.get("Content-Type", "text/html; charset=utf-8")
            )
            for name, value in headers.items():
                if name.lower() != "content-type":
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    yield origin, state
    server.shutdown()
    server.server_close()
    thread.join()
