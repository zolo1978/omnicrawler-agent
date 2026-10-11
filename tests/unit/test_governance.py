from datetime import UTC, datetime

from omnicrawler import governance
from omnicrawler.models import Evidence, Record


def record(url, price):
    return Record(
        entity_id="A",
        fields={"price": price},
        evidence={
            "price": Evidence(
                url=url, captured_at=datetime.now(UTC), locator=".price", excerpt=price
            )
        },
    )


def test_cross_validation_keeps_conflicting_observations():
    result = governance.assess(
        [record("https://one.example/p", "79"), record("https://two.example/p", "89")]
    )
    assert result[0]["status"] == "disputed"
    assert {item["value"] for item in result[0]["observations"]} == {"79", "89"}
    assert result[0]["kind"] == "fact"


def test_agreement_does_not_prove_source_independence():
    result = governance.assess(
        [record("https://one.example/p", "79"), record("https://two.example/p", "79")]
    )
    assert result[0]["status"] == "agreement"
    assert not result[0]["independence_verified"]
    assert len(result[0]["observations"]) == 2


def test_personal_email_is_filtered_before_storage():
    assert (
        governance.sanitize("Contact alice@example.com") == "Contact [redacted-email]"
    )
