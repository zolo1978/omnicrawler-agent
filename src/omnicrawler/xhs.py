"""Xiaohongshu adapter over the xiaohongshu-mcp service (Apache-2.0).

Reads the operator's own account: ``user_profile`` enumerates the authorized
identities, ``get_feed_detail`` reports what each note says now. Retrieval happens
inside that service — it signs requests and drives a logged-in browser — so this
module never touches the collection transport, and every run it produces is
marked as externally retrieved.

Two deliberate choices:

* The session token (``xsec_token``) is request data, never evidence. It is not
  written into a snapshot, a locator or a report, because it is a session
  credential that expires and is not what a reader needs to check a value.
* Field paths are configuration, not hardcoded guesses about the service's JSON.
  ``omnicrawler source --probe`` prints one note's raw payload so an operator can
  confirm the paths against their own deployment.
"""

import json
import os

import aiohttp

from omnicrawler.source import SourceConfig

DEFAULT_ENDPOINT = "http://127.0.0.1:18060/mcp"
NOTE_URL = "https://www.xiaohongshu.com/explore/{feed_id}"
PROTOCOL_VERSION = "2024-11-05"
IDENTITY_KEYS = ("id", "noteId", "note_id", "feedId", "feed_id")
TOKEN_KEYS = ("xsecToken", "xsec_token")
NOTES_CONTAINERS = ("notes", "noteList", "note_list", "items", "feeds")


def endpoint_from_env() -> str:
    return os.environ.get("OMNICRAWLER_XHS_MCP_URL", DEFAULT_ENDPOINT)


def scope_token_from_env() -> str | None:
    return os.environ.get("OMNICRAWLER_XHS_SCOPE_TOKEN") or None


def stringify(value) -> str:
    """A stable text rendering for one field value, whatever the service returned."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def resolve_path(payload, path: str):
    """Read one dotted path out of a tool payload.

    ``a.b`` walks object keys. A trailing ``[]`` maps over a list and joins the
    scalars ("tags[]"), which is as much structure as a report field needs.
    """
    current = payload
    for segment in str(path).split("."):
        if not segment:
            return None
        map_over = segment.endswith("[]")
        key = segment[:-2] if map_over else segment
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if map_over:
            if not isinstance(current, list):
                return None
            joined = "；".join(
                text for text in (stringify(item) for item in current) if text.strip()
            )
            return joined or None
    if current is None or isinstance(current, str):
        return current
    return stringify(current)


def first_path(payload, paths) -> str | None:
    """First declared path that yields a non-empty value."""
    for path in [paths] if isinstance(paths, str) else list(paths):
        value = resolve_path(payload, path)
        if value is not None and str(value).strip():
            return str(value)
    return None


def collect_identities(payload) -> tuple[list[dict], bool]:
    """Every node carrying both a note id and its token, plus a "proved empty" flag.

    The service's nesting is its own business; matching on the id/token pair keeps
    this working across releases instead of pinning one layout. The flag separates
    "the account really has no notes" (an explicit empty notes container) from
    "the payload did not look like a note list at all", which must not be read as
    an empty account.
    """
    found: dict[str, dict] = {}
    proved_empty = False

    def walk(node):
        nonlocal proved_empty
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in NOTES_CONTAINERS and isinstance(value, list) and not value:
                proved_empty = True
        identifier = next(
            (
                node[key]
                for key in IDENTITY_KEYS
                if isinstance(node.get(key), str) and node[key].strip()
            ),
            None,
        )
        token = next(
            (
                node[key]
                for key in TOKEN_KEYS
                if isinstance(node.get(key), str) and node[key].strip()
            ),
            None,
        )
        if identifier and token and identifier not in found:
            found[identifier] = {"identity": identifier, "token": token}
        for value in node.values():
            walk(value)

    walk(payload)
    return list(found.values()), proved_empty


class XhsMcpAdapter:
    """MCP client for xiaohongshu-mcp (Streamable HTTP, JSON-RPC 2.0)."""

    name = "xhs"

    def __init__(
        self,
        config: SourceConfig,
        *,
        scope_token: str | None = None,
        timeout: float = 180,
        max_bytes: int = 4_000_000,
    ):
        self.config = config
        self.endpoint = config.endpoint or DEFAULT_ENDPOINT
        self.account = config.account
        self.scope_token = scope_token
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._session: str | None = None

    async def _post(self, message: dict) -> object:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session and message.get("method") != "initialize":
            headers["Mcp-Session-Id"] = self._session
        async with (
            aiohttp.ClientSession(
                trust_env=False, timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as session,
            session.post(
                self.endpoint, json=message, headers=headers, allow_redirects=False
            ) as response,
        ):
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self._session = session_id
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > self.max_bytes:
                    raise ValueError("MCP response exceeds budget")
            content_type = response.headers.get("Content-Type", "")
            if response.status >= 400 and response.status not in {202, 204}:
                detail = raw.decode("utf-8", errors="replace")[:200]
                raise ValueError(
                    f"MCP endpoint returned HTTP {response.status}: {detail}"
                )
        return _decode(raw, content_type)

    async def _open(self) -> None:
        if self._session:
            return
        await self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "omnicrawler", "version": "0.1"},
                },
            }
        )
        if not self._session:
            raise ValueError(
                "MCP endpoint did not return a session id; is xiaohongshu-mcp running "
                f"at {self.endpoint}?"
            )
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def call_tool(self, name: str, arguments: dict) -> object:
        await self._open()
        envelope = await self._post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        if not isinstance(envelope, dict):
            raise TypeError(f"Tool {name!r} returned no usable envelope")
        if envelope.get("error"):
            error = envelope["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ValueError(f"{name}: {message}")
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise TypeError(f"Tool {name!r} returned no result")
        if result.get("isError"):
            raise ValueError(f"{name}: {_text_of(result) or 'tool reported an error'}")
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        text = _text_of(result)
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ValueError(
                f"Tool {name!r} did not return JSON: {text[:200]}"
            ) from exc

    async def identities(self) -> list[dict]:
        arguments = {"user_id": self.account}
        if self.scope_token:
            arguments["xsec_token"] = self.scope_token
        payload = await self.call_tool("user_profile", arguments)
        entries, proved_empty = collect_identities(payload)
        if not entries and not proved_empty:
            raise ValueError(
                "user_profile returned no note carrying an id and token pair; run "
                "`omnicrawler source --probe <feed_id>` against this deployment to "
                "see the payload shape"
            )
        return entries

    async def observe(self, entry: dict) -> dict:
        feed_id = str(entry.get("identity") or "")
        arguments = {"feed_id": feed_id, "load_all_comments": False}
        if entry.get("token"):
            arguments["xsec_token"] = entry["token"]
        payload = await self.call_tool("get_feed_detail", arguments)
        url = NOTE_URL.format(feed_id=feed_id)
        values, provenance = {}, {}
        for name, declared in self.config.fields.items():
            value = first_path(payload, declared)
            values[name] = value
            # The locator names the note and the field, never the session token.
            provenance[name] = {"url": url, "locator": f"xhs:note/{feed_id}#{name}"}
        return {
            "observed_at": None,
            "url": url,
            "values": values,
            "provenance": provenance,
        }

    async def probe(self, feed_id: str, token: str | None = None) -> object:
        """Raw tool payload for one note, so field paths can be confirmed."""
        arguments = {"feed_id": feed_id, "load_all_comments": False}
        if token:
            arguments["xsec_token"] = token
        return await self.call_tool("get_feed_detail", arguments)


def _text_of(result: dict) -> str:
    parts = result.get("content")
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    ).strip()


def _decode(raw: bytearray, content_type: str):
    """JSON, or the last JSON object of a Server-Sent-Events stream."""
    text = raw.decode("utf-8", errors="replace").strip()
    if "text/event-stream" in content_type.lower():
        payloads = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                payloads.append(json.loads(data))
            except ValueError:
                continue
        return payloads[-1] if payloads else None
    if not text:
        return None
    return json.loads(text)


def adapter_for(config: SourceConfig, *, scope_token: str | None = None):
    """The adapter a config asks for, or a clear error naming the ones that exist.

    Endpoint validation lives on :class:`SourceConfig`; this only routes, so adding
    an adapter is one branch plus one module.
    """
    if config.kind != "xhs":
        raise ValueError(
            f"Unknown source adapter {config.kind!r}; available adapters: xhs"
        )
    return XhsMcpAdapter(config, scope_token=scope_token)
