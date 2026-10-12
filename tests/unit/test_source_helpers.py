"""Pure helpers behind the external source adapter layer."""

import pytest

from omnicrawler import source, xhs

ENDPOINT = "http://127.0.0.1:18060/mcp"


def base_config(**overrides):
    return source.SourceConfig(
        **{
            "kind": "xhs",
            "account": "me",
            "endpoint": ENDPOINT,
            "fields": {"a": "note.title"},
            **overrides,
        }
    )


def test_resolve_path_walks_keys_and_maps_over_lists():
    payload = {
        "note": {"title": "标题"},
        "interactInfo": {"likedCount": "12"},
        "tags": ["A", "B"],
        "flag": False,
        "nested": {"deep": {"x": 1}},
        "empty": None,
    }
    assert xhs.resolve_path(payload, "note.title") == "标题"
    assert xhs.resolve_path(payload, "interactInfo.likedCount") == "12"
    assert xhs.resolve_path(payload, "tags[]") == "A；B"
    assert xhs.resolve_path(payload, "flag") == "false"
    assert xhs.resolve_path(payload, "note") == '{"title": "标题"}'
    assert xhs.resolve_path(payload, "missing.path") is None
    assert xhs.resolve_path(payload, "note.title.deeper") is None
    assert xhs.resolve_path(payload, "empty") is None


def test_first_path_takes_the_first_non_empty_candidate():
    payload = {"a": "", "b": "值", "c": "   "}

    assert xhs.first_path(payload, ["a", "b"]) == "值"
    assert xhs.first_path(payload, "b") == "值"
    assert xhs.first_path(payload, ["a", "c"]) is None
    assert xhs.first_path(payload, ["missing"]) is None


def test_collect_identities_only_accepts_id_and_token_pairs():
    payload = {
        "data": {
            "notes": [
                {"id": "n1", "xsecToken": "t1"},
                {"id": "n2"},
                {"xsecToken": "t3"},
                {"noteId": "n4", "xsec_token": "t4"},
                "not a note",
            ]
        }
    }
    entries, proved_empty = xhs.collect_identities(payload)

    assert entries == [
        {"identity": "n1", "token": "t1"},
        {"identity": "n4", "token": "t4"},
    ]
    assert proved_empty is False


def test_collect_identities_separates_proved_empty_from_unparsable():
    assert xhs.collect_identities({"data": {"notes": []}}) == ([], True)
    assert xhs.collect_identities({"data": {"unexpected": "shape"}}) == ([], False)
    assert xhs.collect_identities({"notes": [], "data": {}}) == ([], True)


def test_source_config_rejects_remote_plaintext_and_empty_paths():
    for endpoint in [
        "http://xhs.example.com/mcp",
        "https://user:secret@xhs.example.com/mcp",
        "https://xhs.example.com/mcp?q=1",
    ]:
        with pytest.raises(ValueError):
            base_config(endpoint=endpoint)
    with pytest.raises(ValueError):
        base_config(fields={"a": ""})
    with pytest.raises(ValueError):
        base_config(fields={"a": []})
    with pytest.raises(ValueError):
        base_config(fields={"  ": "a"})
    assert base_config().limit == 20


def test_adapter_for_names_the_available_adapters():
    with pytest.raises(ValueError, match="available adapters: xhs"):
        xhs.adapter_for(base_config(kind="unknown"))


def test_source_coverage_covers_scope_not_data():
    """A newly published note is data, so it must not fork the baseline."""
    assert source.source_coverage(base_config()) == source.source_coverage(
        base_config()
    )
    assert source.source_coverage(base_config()) != source.source_coverage(
        base_config(account="someone-else")
    )
    assert source.source_coverage(base_config()) != source.source_coverage(
        base_config(limit=5)
    )
    assert source.source_coverage(base_config()) != source.source_coverage(
        base_config(fields={"a": "note.desc"})
    )
