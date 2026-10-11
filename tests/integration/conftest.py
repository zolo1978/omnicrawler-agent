import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def site():
    state = {"products": [("A", "79"), ("B", "20")], "hits": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"].append(self.path)
            if self.path == "/robots.txt":
                body = b"User-agent: *\nDisallow: /private"
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/private")
                self.end_headers()
                return
            elif self.path == "/deniedbig":
                self.send_response(403)
                self.send_header("Content-Length", "99999999")
                self.end_headers()
                return
            elif self.path == "/denied":
                self.send_response(403)
                self.end_headers()
                return
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
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
