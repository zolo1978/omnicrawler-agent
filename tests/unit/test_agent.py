import pytest

from omnicrawler import discovery, planner


def test_discovery_extracts_same_domain_links_rss_and_sitemap():
    assert discovery.extract_sources(
        "<a href='/p'>p</a><a href='https://evil.com'>bad</a>",
        "https://example.com",
        "links",
        {"example.com"},
    ) == ["https://example.com/p"]
    assert discovery.extract_sources(
        "<urlset><url><loc>https://example.com/p</loc></url></urlset>",
        "https://example.com",
        "sitemap",
        {"example.com"},
    ) == ["https://example.com/p"]
    assert discovery.extract_sources(
        "<rss><channel><item><link>https://example.com/p</link></item></channel></rss>",
        "https://example.com",
        "rss",
        {"example.com"},
    ) == ["https://example.com/p"]


def test_planner_retains_goal_and_requires_actionable_seed_scope():
    config = planner.plan(
        "监控竞品价格",
        urls=["https://example.com/products"],
        allowed_hosts=["example.com"],
        item_selector="article",
        id_attribute="data-id",
        fields={"price": ".price"},
    )
    assert config.goal == "监控竞品价格"
    assert config.urls == ["https://example.com/products"]
    with pytest.raises(ValueError):
        planner.plan(
            "research anything",
            urls=[],
            allowed_hosts=[],
            item_selector="article",
            id_attribute="data-id",
            fields={"price": ".price"},
        )


def test_auto_routing_selects_browser_for_inline_dynamic_content():
    from omnicrawler.pipeline import CollectionConfig, select_engine

    config = CollectionConfig(
        urls=["https://example.com"],
        allowed_hosts=["example.com"],
        item_selector="article",
        id_attribute="data-id",
        fields={"price": ".price"},
        engine="auto",
        ready_selector=".price",
    )
    assert select_engine(config) == "playwright"
