"""Validated evidence and task contracts."""

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidence(Contract):
    url: str
    captured_at: AwareDatetime
    locator: str = Field(min_length=1)
    excerpt: str = Field(min_length=1, max_length=2000)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Evidence URL must be credential-free HTTP(S)")
        if any(char.isspace() for char in value):
            raise ValueError("Evidence URL contains whitespace")
        _ = parsed.port
        return value


class Record(Contract):
    entity_id: str = Field(min_length=1)
    fields: dict[str, Any]
    evidence: dict[str, Evidence]

    @model_validator(mode="after")
    def fields_have_evidence(self):
        if not self.fields or self.fields.keys() != self.evidence.keys():
            raise ValueError("Every field needs evidence; orphan evidence is invalid")
        return self


class Claim(Contract):
    kind: Literal["fact", "inference"]
    text: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)


Status = Literal[
    "queued", "running", "succeeded", "partial", "failed", "cancelled", "blocked"
]


class Task(Contract):
    id: str = Field(min_length=1)
    status: Status = "queued"

    def transition(self, status: Status) -> "Task":
        allowed = {
            "queued": {"running", "cancelled", "blocked"},
            "running": {"succeeded", "partial", "failed", "cancelled", "blocked"},
        }
        if status not in allowed.get(self.status, set()):
            raise ValueError(f"Invalid transition {self.status} -> {status}")
        return Task(id=self.id, status=status)
