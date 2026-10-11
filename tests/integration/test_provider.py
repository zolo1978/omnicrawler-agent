import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from omnicrawler.provider import JsonPlanningProvider, two_step_proposal


def test_custom_provider_uses_real_http_two_steps_and_cannot_expand_scope():
    requests = []
    responses = [
        {"sources": ["https://example.com/products"], "field_names": ["price"]},
        {
            "item_selector": "article",
            "id_attribute": "data-id",
            "fields": {"price": ".price"},
        },
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((payload, self.headers.get("Authorization")))
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    responses[min(len(requests) - 1, 1)]
                                )
                            }
                        }
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = JsonPlanningProvider(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            "local-model",
            token="test-token",
        )
        proposal = asyncio.run(
            two_step_proposal(
                goal="价格",
                sources=["https://example.com/products"],
                fields={"price": ""},
                preview={
                    "item_selector": "article",
                    "id_attribute": "data-id",
                    "fields": {"price": ".price"},
                },
                provider=provider,
                allowed_hosts={"example.com"},
            )
        )
        assert proposal["fields"] == {"price": ".price"}
        assert len(requests) == 2
        assert requests[0][1] == "Bearer test-token"
        assert requests[0][0]["response_format"] == {"type": "json_object"}
        responses[0] = {"sources": ["https://evil.com"], "field_names": ["price"]}
        requests.clear()
        with pytest.raises(ValueError):
            asyncio.run(
                two_step_proposal(
                    goal="价格",
                    sources=["https://example.com/products"],
                    fields={"price": ""},
                    preview={},
                    provider=provider,
                    allowed_hosts={"example.com"},
                )
            )
        assert len(requests) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_provider_candidate_fields_and_schema_cannot_expand():
    async def provider(stage, payload):
        return {"sources": payload["sources"], "field_names": ["password"]}

    with pytest.raises(ValueError):
        asyncio.run(
            two_step_proposal(
                goal="prices",
                sources=["https://example.com"],
                fields={"price": ""},
                preview={},
                provider=provider,
                allowed_hosts={"example.com"},
            )
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://remote.example/v1",
        "file:///secret",
        "https://user:pass@example.com",
        "https://example.com/#fragment",
    ],
)
def test_provider_configuration_rejects_untrusted_transport(endpoint):
    with pytest.raises(ValueError):
        JsonPlanningProvider(endpoint, "model")
