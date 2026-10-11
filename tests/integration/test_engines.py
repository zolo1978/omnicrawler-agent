import asyncio

from bs4 import BeautifulSoup

from omnicrawler import engines


def test_playwright_renders_inline_javascript_without_network():
    html = "<html><body><div id='result'></div><script>fetch('http://127.0.0.1:1/private').catch(()=>{}); document.getElementById('result').innerText='rendered';</script></body></html>"
    output = asyncio.run(
        engines.transform("playwright", html, "https://example.com", "#result")
    )
    assert BeautifulSoup(output, "html.parser").select_one("#result").text == "rendered"


def test_crawl4ai_parses_prefetched_html():
    html = "<html><body><article><h1>Product A</h1><p>Price USD 79 for this smart bracelet today.</p></article></body></html>"
    output = asyncio.run(
        engines.transform("crawl4ai", html, "https://example.com", None)
    )
    assert "Product A" in output and "79" in output


def test_browser_cannot_send_requests_to_authorized_fixture(site):
    origin, state = site
    html = f"<html><body><div id='done'>done</div><img src='{origin}/leak'><script>fetch('{origin}/leak').catch(()=>{{}}); new WebSocket('ws://127.0.0.1:1/leak');</script></body></html>"
    asyncio.run(engines.transform("playwright", html, "https://example.com", "#done"))
    assert state["hits"] == []
