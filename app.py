"""
TikTok Search API v6.0
======================
Single-process architecture: Flask + Playwright asyncio in background thread.
Uses network response interception instead of JS fetch - works on cloud IPs too.
"""
import asyncio
import json
import logging
import os
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from typing import Dict, Optional

from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", 5001))

ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 9; Pixel 3 Build/PQ3A.190801.002) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/91.0.4472.120 Mobile Safari/537.36"
)


class PlaywrightManager:
    def __init__(self):
        self._loop = None
        self._playwright = None
        self._browser = None
        self._ready = threading.Event()
        self._error = None

    def start_background(self):
        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._init())
                self._ready.set()
                self._loop.run_forever()
            except Exception as e:
                logger.error(f"[PW] Fatal error: {e}")
                self._error = str(e)
                self._ready.set()

        t = threading.Thread(target=run, daemon=True, name="playwright-loop")
        t.start()
        logger.info("[PW] Background thread started, waiting for Playwright init...")
        self._ready.wait(timeout=60)
        if self._error:
            logger.error(f"[PW] Init failed: {self._error}")
        else:
            logger.info("[PW] Playwright ready!")

    async def _init(self):
        from playwright.async_api import async_playwright
        logger.info("[PW] Starting Playwright...")
        self._playwright = await async_playwright().start()
        logger.info("[PW] Launching Chromium...")
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--disable-images",
                "--blink-settings=imagesEnabled=false",
                "--js-flags=--max-old-space-size=128",
            ],
        )
        logger.info("[PW] Chromium launched successfully!")

    def _run_coro(self, coro, timeout=90):
        if not self._loop:
            raise RuntimeError("Event loop not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def search(self, query, cursor, count, search_id, timeout=80):
        return self._run_coro(
            self._async_search(query, cursor, count, search_id),
            timeout=timeout
        )

    async def _ensure_browser(self):
        if not self._browser or not self._browser.is_connected():
            logger.warning("[PW] Browser disconnected, restarting...")
            await self._init()

    async def _async_search(self, query, cursor, count, search_id):
        """
        Search using network response interception.
        This works even when TikTok does not set cookies for cloud IPs.
        """
        await self._ensure_browser()

        context = await self._browser.new_context(
            user_agent=ANDROID_UA,
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            locale="en-US",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua-mobile": "?1",
            },
        )

        await context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,mp4,webm}",
            lambda route: route.abort()
        )

        page = await context.new_page()
        session_id = str(uuid.uuid4())
        intercepted_data = {"body": None, "status": None, "error": None}
        intercept_event = asyncio.Event()

        params = {
            "keyword": query,
            "offset": str(cursor),
            "count": str(count),
            "from_page": "search",
            "web_search_code": json.dumps({
                "tiktok": {
                    "client_params_x": {
                        "search_engine": {
                            "ies_mt_user_live_video_card_use_libra": 1,
                            "mt_search_general_user_live_card": 1
                        }
                    },
                    "search_server": {}
                }
            })
        }
        if search_id:
            params["search_id"] = search_id

        api_url_prefix = "/api/search/general/full/"

        async def handle_response(response):
            if api_url_prefix in response.url and not intercept_event.is_set():
                try:
                    body = await response.body()
                    intercepted_data["body"] = body
                    intercepted_data["status"] = response.status
                    logger.info(f"[PW] Intercepted API: status={response.status}, len={len(body)}")
                    intercept_event.set()
                except Exception as e:
                    intercepted_data["error"] = str(e)
                    intercept_event.set()

        page.on("response", handle_response)

        try:
            logger.info(f"[PW] Navigating to TikTok search: {query!r}")
            try:
                await page.goto(
                    "https://www.tiktok.com/search?q=" + urllib.parse.quote(query),
                    timeout=25000,
                    wait_until="commit",
                )
                logger.info(f"[PW] Page loaded: {page.url}")
            except Exception as e:
                logger.warning(f"[PW] Partial navigation (OK): {type(e).__name__}")

            await page.wait_for_timeout(3000)

            logger.info(f"[PW] Triggering fetch: query={query!r} cursor={cursor}")
            await page.evaluate(
                """
                async (params) => {
                    const p = new URLSearchParams(params);
                    fetch('/api/search/general/full/?' + p.toString(), {
                        credentials: 'include',
                        headers: {
                            'Accept': 'application/json, text/plain, */*',
                            'Accept-Language': 'en-US,en;q=0.9',
                            'sec-fetch-dest': 'empty',
                            'sec-fetch-mode': 'cors',
                            'sec-fetch-site': 'same-origin',
                        }
                    });
                }
                """,
                params
            )

            try:
                await asyncio.wait_for(intercept_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                raise ValueError(f"Timeout waiting for TikTok API response (query={query!r})")

            if intercepted_data["error"]:
                raise ValueError(f"Error intercepting response: {intercepted_data['error']}")

            body = intercepted_data["body"]
            status = intercepted_data["status"]

            if not body:
                raise ValueError(f"Empty response from TikTok (status={status})")

            try:
                data = json.loads(body)
            except json.JSONDecodeError as e:
                body_str = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
                raise ValueError(f"Invalid JSON: {e} | body={body_str[:200]}")

            return {"data": data, "session_id": session_id}

        finally:
            try:
                await context.close()
            except Exception:
                pass

    def is_ready(self):
        return self._browser is not None and self._browser.is_connected()


pw_manager = PlaywrightManager()
app = Flask(__name__)
app.json.sort_keys = False


def _parse_posts(raw_data, cursor, count):
    posts = []
    items = raw_data.get("data", [])
    if not items:
        items = raw_data.get("item_list", [])

    for item in items:
        try:
            if item.get("type") != 1:
                continue
            item_data = item.get("item", {})
            if not item_data:
                item_data = item

            author = item_data.get("author", {})
            stats = item_data.get("stats", {})
            video = item_data.get("video", {})
            music = item_data.get("music", {})
            desc = item_data.get("desc", "")
            hashtags = [
                c["hashtagName"]
                for c in item_data.get("textExtra", [])
                if c.get("hashtagName")
            ]

            post = {
                "id": item_data.get("id", ""),
                "description": desc,
                "url": "https://www.tiktok.com/@" + author.get("uniqueId", "") + "/video/" + item_data.get("id", ""),
                "create_time": item_data.get("createTime", 0),
                "author": {
                    "unique_id": author.get("uniqueId", ""),
                    "nickname": author.get("nickname", ""),
                    "verified": author.get("verified", False),
                    "followers": author.get("followerCount", 0),
                    "avatar": author.get("avatarThumb", ""),
                },
                "stats": {
                    "plays": stats.get("playCount", 0),
                    "likes": stats.get("diggCount", 0),
                    "comments": stats.get("commentCount", 0),
                    "shares": stats.get("shareCount", 0),
                },
                "video": {
                    "duration": video.get("duration", 0),
                    "cover": video.get("cover", ""),
                    "play_url": video.get("playAddr", ""),
                    "width": video.get("width", 0),
                    "height": video.get("height", 0),
                },
                "hashtags": hashtags,
                "music": {
                    "title": music.get("title", ""),
                    "author": music.get("authorName", ""),
                    "cover": music.get("coverThumb", ""),
                },
            }
            posts.append(post)
        except Exception as e:
            logger.warning(f"[Parse] Error: {e}")
            continue

    has_more = bool(raw_data.get("has_more", 0))
    next_cursor = raw_data.get("cursor", cursor + len(posts)) if has_more else None
    search_id = raw_data.get("search_id") or raw_data.get("extra", {}).get("search_id")

    return {
        "posts": posts,
        "has_more": has_more,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "search_id": search_id,
    }


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Parameter q is required"}), 400

    try:
        cursor = int(request.args.get("cursor", 0))
        count = min(int(request.args.get("count", 12)), 50)
    except ValueError:
        return jsonify({"error": "Parameters cursor and count must be integers"}), 400

    search_id = request.args.get("search_id") or None
    start_time = time.time()

    try:
        raw = pw_manager.search(query=query, cursor=cursor, count=count, search_id=search_id)
    except Exception as e:
        logger.error(f"[Search] Error: {e}")
        return jsonify({"error": str(e)}), 500

    result = _parse_posts(raw["data"], cursor=cursor, count=count)
    if not result["search_id"]:
        result["search_id"] = raw.get("session_id")

    elapsed_ms = round((time.time() - start_time) * 1000, 1)
    next_cursor = result["next_cursor"]
    next_page_url = (
        "/search?q=" + query + "&cursor=" + str(next_cursor) + "&search_id=" + str(result["search_id"])
        if result["has_more"] and next_cursor else None
    )

    return jsonify({
        "query": query,
        "posts": result["posts"],
        "pagination": {
            "has_more": result["has_more"],
            "cursor": cursor,
            "next_cursor": next_cursor,
            "search_id": result["search_id"],
            "next_page_url": next_page_url,
        },
        "meta": {
            "total_found": len(result["posts"]),
            "took_ms": elapsed_ms,
            "android_user_agent": True,
        },
    })


@app.route("/health")
def health():
    ready = pw_manager.is_ready()
    return jsonify({
        "status": "ok" if ready else "degraded",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "components": {
            "flask_api": "running",
            "playwright": "ready" if ready else "initializing",
        },
    })


@app.route("/docs")
def docs():
    return jsonify({
        "name": "TikTok Search API",
        "version": "6.0.0",
        "endpoints": {
            "GET /search": {
                "parameters": {
                    "q": "string (required)",
                    "cursor": "integer (optional, default 0)",
                    "search_id": "string (optional, for pagination)",
                    "count": "integer (optional, default 12, max 50)",
                },
                "example": "/search?q=funny+cats",
            },
            "GET /health": "Server status",
            "GET /docs": "This documentation",
        },
    })


@app.route("/")
def index():
    return jsonify({
        "name": "TikTok Search API",
        "version": "6.0.0",
        "endpoints": {
            "search": "/search?q=<query>",
            "pagination": "/search?q=<query>&cursor=<int>&search_id=<str>",
            "health": "/health",
            "docs": "/docs",
        },
    })


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("TikTok Search API v6.0.0 (Network Interception)")
    logger.info("=" * 60)
    pw_manager.start_background()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
