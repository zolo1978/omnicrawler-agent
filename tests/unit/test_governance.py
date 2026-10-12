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


def test_agreement_across_two_hosts_verifies_independence():
    result = governance.assess(
        [record("https://one.example/p", "79"), record("https://two.example/p", "79")]
    )
    assert result[0]["status"] == "agreement"
    assert result[0]["independence_verified"] is True
    assert result[0]["independent_sources"] == 2
    assert len(result[0]["observations"]) == 2


def test_repeated_observations_on_one_host_are_not_independent():
    result = governance.assess(
        [
            record("https://one.example/a", "79"),
            record("https://one.example/b", "79"),
            record("https://one.example:8443/c", "79"),
        ]
    )
    assert result[0]["status"] == "agreement"
    assert result[0]["independence_verified"] is False
    assert result[0]["independent_sources"] == 1


def test_single_observation_is_neither_agreement_nor_independent():
    result = governance.assess([record("https://one.example/p", "79")])
    assert result[0]["status"] == "single_source"
    assert result[0]["independence_verified"] is False
    assert result[0]["independent_sources"] == 1


def test_personal_email_is_filtered_before_storage():
    assert (
        governance.sanitize("Contact alice@example.com") == "Contact [redacted-email]"
    )
