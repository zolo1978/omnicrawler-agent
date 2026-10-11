"""Transform prefetched content without browser access to unvalidated networks.

Dynamic rendering supports inline JS. Network-loaded SPAs require a future
policy proxy; we deliberately do not let the browser fetch arbitrary resources.
"""

import asyncio
import os
from pathlib import Path


async def transform(engine: str, html: str, url: str, ready_selector: str | None):
    if engine == "crawl4ai":
        from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy

        result = await asyncio.to_thread(
            LXMLWebScrapingStrategy().scrap, url, html, word_count_threshold=0
        )
        return result.cleaned_html
    if engine != "playwright":
        raise ValueError("Unsupported content engine")
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(Path(".browser-cache").resolve())
    )
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            chromium_sandbox=True,
            args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
        )
        try:
            context = await browser.new_context(offline=True, service_workers="block")
            await context.route("**/*", lambda route: route.abort())
            await context.route_web_socket("**/*", lambda socket: socket.close())
            deny_peer_connections = """() => {
                for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection']) {
                    Object.defineProperty(globalThis, name, {value: undefined, configurable: false, writable: false});
                }
            }"""
            await context.add_init_script(f"({deny_peer_connections})();")
            page = await context.new_page()
            await page.evaluate(deny_peer_connections)
            await page.set_content(html, wait_until="domcontentloaded", timeout=10000)
            if ready_selector:
                await page.wait_for_selector(
                    ready_selector, state="attached", timeout=10000
                )
            return await page.content()
        finally:
            await browser.close()
