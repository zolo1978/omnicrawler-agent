"""Durable, revisioned guidance with explicit scope and execution consent."""

import json
import re
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import soupsieve
from bs4 import BeautifulSoup
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnicrawler.core import make_plan
from omnicrawler.governance import sanitize
from omnicrawler.models import Evidence
from omnicrawler.pipeline import CollectionConfig
from omnicrawler.transport import SafeFetcher

FORMATS = {"md", "docx", "pdf", "json"}
SENSITIVE = {"email", "phone", "username", "password", "cookie", "address"}


class Question(BaseModel):
    key: str
    prompt: str
    kind: Literal["choice", "text", "list", "url"]
    required: bool = True
    options: list[str] | None = None


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_type: Literal["once", "monitor"] | None = None
    goal: str | None = None
    source: list[str] | None = None
    fields: dict[str, str] | None = None
    frequency: int | None = Field(default=None, ge=10)
    scope: list[str] | None = None
    item_selector: str | None = None
    id_attribute: str | None = None
    budget: int = Field(default=100, ge=1, le=1000)
    deliverable_formats: list[Literal["md", "docx", "pdf", "json"]] = Field(
        default_factory=lambda: ["md", "docx", "pdf", "json"], min_length=1
    )
    engine: Literal["auto", "html", "playwright", "crawl4ai"] = "auto"
    ready_selector: str | None = None

    @model_validator(mode="after")
    def valid_answers(self):
        if self.goal is not None and not self.goal.strip():
            raise ValueError("Goal must not be empty")
        if self.source is not None:
            make_plan(self.source, ["preview"], self.budget)
        if self.fields is not None:
            if not self.fields or any(not name.isidentifier() for name in self.fields):
                raise ValueError("Field names must be identifiers")
            if set(self.fields) & SENSITIVE:
                raise ValueError("Personal or credential fields are disabled")
            for selector in self.fields.values():
                if selector:
                    soupsieve.compile(selector)
        if self.item_selector is not None:
            if not self.item_selector.strip():
                raise ValueError("Item selector must not be empty")
            soupsieve.compile(self.item_selector)
        if self.id_attribute is not None and not re.fullmatch(
            r"[A-Za-z_][\w:.-]*", self.id_attribute
        ):
            raise ValueError("Invalid identity attribute")
        return self


class PreviewResult(BaseModel):
    captured_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    source_url: str
    item_selector: str | None
    id_attribute: str | None
    fields: dict[str, str]
    samples: list[dict]
    unknowns: list[str]
    candidate_sources: list[str] = Field(default_factory=list)
    provider_used: bool = False
    warnings: list[str] = Field(default_factory=list)


class PlanDraft(BaseModel):
    goal: str
    task_type: Literal["once", "monitor"]
    sources: list[str]
    allowed_hosts: list[str]
    proposed_fields: dict[str, str]
    item_selector: str | None
    id_attribute: str | None
    schedule_seconds: int | None
    budget: int
    unknowns: list[str]
    preview: PreviewResult | None = None
    deliverable_formats: list[str]
    status: Literal["draft", "confirmed"] = "draft"


def hosts_for(urls):
    return sorted(
        {urlsplit(url).hostname.lower().encode("idna").decode() for url in urls}
    )


def draft_for(answers: Answers, preview=None, confirmed=False):
    if answers.task_type is None:
        return None
    unknowns = []
    for key in ("goal", "source", "fields", "item_selector", "id_attribute"):
        if not getattr(answers, key):
            unknowns.append(key)
    for name, selector in (answers.fields or {}).items():
        if not selector:
            unknowns.append(f"fields.{name}")
    hosts = hosts_for(answers.source or [])
    if not hosts or set(answers.scope or []) != set(hosts):
        unknowns.append("scope")
    if answers.task_type == "monitor" and answers.frequency is None:
        unknowns.append("frequency")
    return PlanDraft(
        goal=answers.goal or "",
        task_type=answers.task_type,
        sources=answers.source or [],
        allowed_hosts=hosts,
        proposed_fields=answers.fields or {},
        item_selector=answers.item_selector,
        id_attribute=answers.id_attribute,
        schedule_seconds=answers.frequency if answers.task_type == "monitor" else None,
        budget=answers.budget,
        unknowns=unknowns,
        preview=preview,
        deliverable_formats=answers.deliverable_formats,
        status="confirmed" if confirmed else "draft",
    )


def questions_for(answers, draft):
    if answers.task_type is None:
        return [
            Question(
                key="task_type",
                prompt="一次性采集，还是长期监控？",
                kind="choice",
                options=["once", "monitor"],
            )
        ]
    prompts = {
        "goal": ("你想得到什么？", "text"),
        "source": ("提供已授权的种子 URL 列表；搜索主题尚需补充 URL。", "url"),
        "fields": (
            "关注哪些字段？提交字段名到 CSS 选择器的映射，未知选择器留空。",
            "list",
        ),
        "item_selector": (
            "实体容器的 CSS 选择器是什么？可先确认范围并请求采样建议。",
            "text",
        ),
        "id_attribute": ("哪个属性是稳定实体 ID？长期监控必须使用稳定 ID。", "text"),
        "frequency": ("多少秒检查一次？至少 10 秒。", "text"),
        "scope": (
            "确认采样与采集仅限这些域名：" + ", ".join(draft.allowed_hosts),
            "list",
        ),
    }
    return [
        Question(
            key=key,
            prompt=prompts.get(key, ("补充字段选择器：" + key, "text"))[0],
            kind=prompts.get(key, ("", "text"))[1],
        )
        for key in draft.unknowns
    ]


class SessionStore:
    def __init__(self, manager, *, allowed_hosts, local_origins=(), provider=None):
        self.manager = manager
        self.store = manager.store
        self.allowed_hosts = {h.lower().encode("idna").decode() for h in allowed_hosts}
        self.local_origins = set(local_origins)
        self.provider = provider
        with self.store._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS guidance_sessions (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, revision INTEGER NOT NULL,
                    answers TEXT NOT NULL, preview TEXT, confirmation TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS guidance_audit (
                    session_id TEXT PRIMARY KEY REFERENCES guidance_sessions(id),
                    actor TEXT NOT NULL, confirmed_at TEXT NOT NULL, revision INTEGER NOT NULL, scope TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_deliveries (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
                    task_type TEXT NOT NULL, seconds INTEGER, formats TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedule_deliveries (
                    schedule_id TEXT PRIMARY KEY REFERENCES schedules(id) ON DELETE CASCADE,
                    formats TEXT NOT NULL
                );
            """)

    def start(self):
        identifier = str(uuid4())
        with self.store._connection() as conn:
            conn.execute(
                "INSERT INTO guidance_sessions VALUES (?, 'eliciting', 0, ?, NULL, NULL, ?)",
                (
                    identifier,
                    Answers().model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return self.get(identifier)

    def _row(self, conn, identifier):
        row = conn.execute(
            "SELECT state, revision, answers, preview, confirmation FROM guidance_sessions WHERE id=?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise KeyError(identifier)
        return row

    def get(self, identifier):
        with self.store._connection() as conn:
            state, revision, payload, preview, confirmation = self._row(
                conn, identifier
            )
        answers = Answers.model_validate_json(payload)
        result = json.loads(confirmation) if confirmation else None
        if result and state == "executing":
            task = self.manager.get(result["task_id"])
            if task["status"] not in {"queued", "running"}:
                state = "done"
                with self.store._connection() as conn:
                    conn.execute(
                        "UPDATE guidance_sessions SET state='done' WHERE id=?",
                        (identifier,),
                    )
        draft = draft_for(
            answers,
            PreviewResult.model_validate_json(preview) if preview else None,
            bool(result),
        )
        return {
            "id": identifier,
            "state": state,
            "revision": revision,
            "answers": answers.model_dump(exclude_none=True),
            "draft": draft.model_dump(mode="json") if draft else None,
            "questions": [q.model_dump() for q in questions_for(answers, draft)]
            if not result
            else [],
            "confirmation": result,
        }

    def _authorize(self, answers):
        if not set(hosts_for(answers.source or [])) <= self.allowed_hosts:
            raise ValueError("Source policy exceeds server authorization")
        if answers.scope and not set(answers.scope) <= self.allowed_hosts:
            raise ValueError("Source policy exceeds server authorization")

    def answer(self, identifier, updates):
        updates = dict(updates)
        accept = updates.pop("accept_preview", False)
        if type(accept) is not bool:
            raise ValueError("accept_preview must be a boolean")
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state, revision, payload, preview, confirmation = self._row(
                conn, identifier
            )
            if confirmation or state not in {"eliciting", "drafting", "confirming"}:
                raise ValueError(
                    "Confirmed session cannot be edited; start a new session"
                )
            current = json.loads(payload)
            if any(
                key in updates and updates[key] != current.get(key)
                for key in (
                    "source",
                    "fields",
                    "scope",
                    "item_selector",
                    "id_attribute",
                )
            ):
                preview = None
            if "source" in updates and updates["source"] != current.get("source"):
                current["scope"] = None
            if updates.get("task_type") == "once":
                current["frequency"] = None
            current.update(updates)
            if accept:
                if not preview:
                    raise ValueError("No current preview to accept")
                proposal = PreviewResult.model_validate_json(preview)
                current.update(
                    item_selector=proposal.item_selector,
                    id_attribute=proposal.id_attribute,
                    fields={
                        name: proposal.fields.get(name, "")
                        for name in (current.get("fields") or {})
                    },
                )
            answers = Answers.model_validate(current)
            self._authorize(answers)
            draft = draft_for(answers)
            state = "confirming" if draft and not draft.unknowns else "eliciting"
            conn.execute(
                "UPDATE guidance_sessions SET state=?, revision=?, answers=?, preview=? WHERE id=?",
                (state, revision + 1, answers.model_dump_json(), preview, identifier),
            )
        return self.get(identifier)

    def _config(self, answers):
        draft = draft_for(answers)
        if draft is None or draft.unknowns:
            raise ValueError("Draft has unresolved items")
        self._authorize(answers)
        origins = {
            f"{urlsplit(url).scheme}://{urlsplit(url).netloc}" for url in answers.source
        }
        return CollectionConfig(
            goal=answers.goal,
            urls=answers.source,
            allowed_hosts=draft.allowed_hosts,
            local_origins=sorted(origins & self.local_origins),
            fields=answers.fields,
            item_selector=answers.item_selector,
            id_attribute=answers.id_attribute,
            max_pages=answers.budget,
            engine=answers.engine,
            ready_selector=answers.ready_selector,
        )

    def confirm(self, identifier, revision, *, actor):
        if not actor.strip() or type(revision) is not int:
            raise ValueError("Actor and integer draft revision are required")
        schedule_id = None
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state, current_revision, payload, _, confirmation = self._row(
                conn, identifier
            )
            if revision != current_revision:
                raise ValueError("Draft revision changed; review again")
            if confirmation:
                return json.loads(confirmation)
            if state != "confirming":
                raise ValueError("Draft is not ready for confirmation")
            answers = Answers.model_validate_json(payload)
            config = self._config(answers)
            if answers.task_type == "monitor":
                schedule_id = self.manager.schedule(
                    config, answers.frequency, connection=conn
                )
            task_id = self.manager.enqueue(
                config,
                f"schedule:{schedule_id}:baseline"
                if schedule_id
                else f"guidance:{identifier}",
                connection=conn,
            )
            result = {
                "session_id": identifier,
                "revision": revision,
                "task_id": task_id,
                "schedule_id": schedule_id,
            }
            conn.execute(
                "INSERT INTO task_deliveries VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    answers.task_type,
                    answers.frequency if schedule_id else None,
                    json.dumps(answers.deliverable_formats),
                ),
            )
            if schedule_id:
                conn.execute(
                    "INSERT INTO schedule_deliveries VALUES (?, ?)",
                    (schedule_id, json.dumps(answers.deliverable_formats)),
                )
            conn.execute(
                "INSERT INTO guidance_audit VALUES (?, ?, ?, ?, ?)",
                (
                    identifier,
                    actor,
                    datetime.now(UTC).isoformat(),
                    revision,
                    json.dumps(config.model_dump()),
                ),
            )
            conn.execute(
                "UPDATE guidance_sessions SET state='executing', confirmation=? WHERE id=?",
                (json.dumps(result), identifier),
            )
        if schedule_id and self.manager.scheduler:
            self.manager.register_schedule(schedule_id, answers.frequency)
        return result

    def audit(self, identifier):
        with self.store._connection() as conn:
            return [
                {
                    "actor": row[0],
                    "confirmed_at": row[1],
                    "revision": row[2],
                    "scope": json.loads(row[3]),
                }
                for row in conn.execute(
                    "SELECT actor, confirmed_at, revision, scope FROM guidance_audit WHERE session_id=?",
                    (identifier,),
                )
            ]

    async def preview(self, identifier):
        value = self.get(identifier)
        if value["confirmation"]:
            raise ValueError("Confirmed session cannot be previewed")
        answers = Answers.model_validate(value["answers"])
        self._authorize(answers)
        if not answers.source or set(answers.scope or []) != set(
            hosts_for(answers.source)
        ):
            raise ValueError("Explicit source scope required before sampling")
        origin = f"{urlsplit(answers.source[0]).scheme}://{urlsplit(answers.source[0]).netloc}"
        fetcher = SafeFetcher(
            hosts_for(answers.source), local_origins={origin} & self.local_origins
        )
        page = await fetcher.fetch(answers.source[0])
        soup = BeautifulSoup(page.html, "html.parser")
        selector, identity, items = None, None, []
        for attribute in ("data-id", "data-key", "id"):
            for tag in ("article", "li", "tr", "div"):
                candidates = soup.select(f"{tag}[{attribute}]")[:10]
                keys = [item.get(attribute) for item in candidates]
                if candidates and len(keys) == len(set(keys)) and all(keys):
                    selector, identity, items = (
                        f"{tag}[{attribute}]",
                        attribute,
                        candidates,
                    )
                    break
            if items:
                break
        fields = {}
        for name, requested in (answers.fields or {}).items():
            candidates = (
                [requested]
                if requested
                else ["." + soupsieve.escape(name), f'[itemprop="{name}"]']
            )
            for proposed in candidates:
                if (
                    all(
                        item.select_one(proposed)
                        and item.select_one(proposed).get_text(strip=True)
                        for item in items
                    )
                    and items
                ):
                    fields[name] = proposed
                    break
        captured = datetime.now(UTC)
        candidate_sources, provider_used, warnings = [], False, []
        if self.provider is not None:
            from omnicrawler.provider import two_step_proposal

            try:
                offered = await two_step_proposal(
                    goal=answers.goal or "",
                    sources=answers.source,
                    fields=answers.fields or {},
                    preview={
                        "item_selector": selector,
                        "id_attribute": identity,
                        "fields": fields,
                    },
                    provider=self.provider,
                    allowed_hosts=set(hosts_for(answers.source)),
                )
                # Model output remains a proposal and is checked against sampled
                # content before being offered; no URLs are fetched from it.
                sampled = soup.select(offered["item_selector"])[:10]
                keys = [item.get(offered["id_attribute"]) for item in sampled]
                if (
                    not sampled
                    or not all(isinstance(key, str) and key.strip() for key in keys)
                    or len(set(keys)) != len(keys)
                ):
                    raise ValueError("Provider identity proposal did not match sample")
                if not all(
                    item.select_one(css) and item.select_one(css).get_text(strip=True)
                    for item in sampled
                    for css in offered["fields"].values()
                ):
                    raise ValueError("Provider fields did not match sample")
                selector, identity, fields, items = (
                    offered["item_selector"],
                    offered["id_attribute"],
                    offered["fields"],
                    sampled,
                )
                candidate_sources, provider_used = offered["candidate_sources"], True
            except Exception as exc:  # noqa: BLE001 — untrusted optional provider cannot interrupt deterministic guidance
                warnings.append(
                    f"provider_unavailable_or_invalid: {type(exc).__name__}; deterministic proposal retained"
                )
        samples = []
        for item in items[:3]:
            values = {
                name: sanitize(item.select_one(css).get_text(" ", strip=True))
                for name, css in fields.items()
            }
            evidence = {
                name: Evidence(
                    url=page.url,
                    captured_at=captured,
                    locator=f"{selector}[{identity}={item.get(identity)}] {fields[name]}",
                    excerpt=text[:2000],
                ).model_dump(mode="json")
                for name, text in values.items()
            }
            samples.append(
                {
                    "entity_id": item.get(identity),
                    "fields": values,
                    "evidence": evidence,
                }
            )
        unknowns = ([] if selector else ["item_selector", "id_attribute"]) + [
            f"fields.{name}" for name in (answers.fields or {}) if name not in fields
        ]
        proposal = PreviewResult(
            source_url=page.url,
            item_selector=selector,
            id_attribute=identity,
            fields=fields,
            samples=samples,
            unknowns=unknowns,
            candidate_sources=candidate_sources,
            provider_used=provider_used,
            warnings=warnings,
        )
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _state, revision, _, _, confirmation = self._row(conn, identifier)
            if confirmation or revision != value["revision"]:
                raise ValueError("Session changed during preview; retry")
            conn.execute(
                "UPDATE guidance_sessions SET revision=?, preview=? WHERE id=?",
                (revision + 1, proposal.model_dump_json(), identifier),
            )
        return self.get(identifier)
