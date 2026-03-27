"""
TikTok Search API v5.0
======================
Single-process architecture: Flask + Playwright asyncio in background thread.
No subprocess needed - more reliable on cloud platforms.
"""
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

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

FETCH_JS = """
async (args) => {
    const { keyword, cursor, count, searchId } = args;
    const params = new URLSearchParams({
        keyword: keyword,
        offset: String(cursor),
        count: String(count),
        from_page: "search",
        web_search_code: JSON.stringify({
            tiktok: {
                client_params_x: {
                    search_engine: {
                        ies_mt_user_live_video_card_use_libra: 1,
                        mt_search_general_user_live_card: 1
                    }
                },
                search_server: {}
            }
        })
    });
    if (searchId) params.set("search_id", searchId);
    try {
        const resp = await fetch(
            "/api/search/general/full/?" + params.toString(),
            {
                credentials: "include",
                headers: {
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "same-origin",
                    "X-Requested-With": "com.zhiliaoapp.musically"
                }
            }
        );
        const text = await resp.text();
        return { ok: true, status: resp.status, body: text, pageUrl: window.location.href };
    } catch(e) {
        return { ok: false, error: e.toString(), pageUrl: window.location.href };
    }
}
"""


class PlaywrightManager:
    def __init__(self):
        self._loop = None
        self._playwright = None
        self._browser = None
        self._sessions = {}
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
            self._sessions = {}

    async def _get_or_create_session(self, session_id, query):
        await self._ensure_browser()

        if session_id and session_id in self._sessions:
            session = self._sessions[session_id]
            try:
                if not session["page"].is_closed():
                    logger.info(f"[PW] Reusing session {session_id[:8]} for '{query}'")
                    return session["page"], session_id
            except Exception:
                pass
            del self._sessions[session_id]

        now = time.time()
        expired = [k for k, v in list(self._sessions.items()) if now - v["created_at"] > 180]
        for k in expired:
            try:
                await self._sessions[k]["context"].close()
            except Exception:
                pass
            del self._sessions[k]

        if len(self._sessions) >= 2:
            oldest = min(self._sessions, key=lambda k: self._sessions[k]["created_at"])
            try:
                await self._sessions[oldest]["context"].close()
            except Exception:
                pass
            del self._sessions[oldest]

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
        logger.info(f"[PW] Navigating to TikTok search: '{query}'")
        try:
            await page.goto(
                f"https://www.tiktok.com/search?q={query}",
                timeout=30000,
                wait_until="commit",
            )
            logger.info(f"[PW] Page loaded: {page.url}")
        except Exception as e:
            logger.warning(f"[PW] Partial navigation (OK): {type(e).__name__}: {e}")

        await page.wait_for_timeout(6000)

        new_sid = str(uuid.uuid4())
        self._sessions[new_sid] = {
            "page": page,
            "context": context,
            "query": query,
            "created_at": time.time(),
        }
        logger.info(f"[PW] New session: {new_sid[:8]}, page: {page.url}")
        return page, new_sid

    async def _async_search(self, query, cursor, count, search_id):
        page, session_id = await self._get_or_create_session(search_id, query)
        logger.info(f"[PW] Fetch API: query='{query}' cursor={cursor} count={count}")

        try:
            result = await page.evaluate(
                FETCH_JS,
                {"keyword": query, "cursor": cursor, "count": count,
                 "searchId": search_id or ""}
            )
        except Exception as e:
            if "closed" in str(e).lower() or "target" in str(e).lower():
                logger.warning(f"[PW] Page closed, new session: {e}")
                if search_id and search_id in self._sessions:
                    del self._sessions[search_id]
                page, session_id = await self._get_or_create_session(None, query)
                result = await page.evaluate(
                    FETCH_JS,
                    {"keyword": query, "cursor": cursor, "count": count, "searchId": ""}
                )
            else:
                raise RuntimeError(f"Fetch error: {e}")

        if not result.get("ok", True):
            raise RuntimeError(f"JS fetch error: {result.get('error', 'unknown')}")

        status = result.get("status", 0)
        body = result.get("body", "")
        page_url = result.get("pageUrl", "")

        logger.info(f"[PW] TikTok response: status={status}, body_len={len(body)}, pageUrl={page_url[:80]}")

        if not body:
            raise ValueError(f"Empty response from TikTok (status={status}, pageUrl={page_url})")

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e} | body={body[:200]}")

        return {"data": data, "session_id": session_id}


    async def _debug_info(self):
        """Get debug info about browser state."""
        if not self._browser or not self._browser.is_connected():
            return {"browser": "not connected"}
        
        context = await self._browser.new_context(
            user_agent=ANDROID_UA, is_mobile=True, has_touch=True
        )
        page = await context.new_page()
        try:
            await page.goto("https://www.tiktok.com/search?q=test", timeout=20000, wait_until="commit")
            await page.wait_for_timeout(5000)
            
            result = await page.evaluate("""
                async () => {
                    const cookies = document.cookie;
                    const params = new URLSearchParams({keyword: 'test', offset: '0', count: '5', from_page: 'search'});
                    const resp = await fetch('/api/search/general/full/?' + params.toString(), {credentials: 'include'});
                    const text = await resp.text();
                    return {
                        pageUrl: window.location.href,
                        cookieCount: cookies.split(';').length,
                        cookieNames: cookies.split(';').map(c => c.trim().split('=')[0]),
                        apiStatus: resp.status,
                        apiBodyLen: text.length,
                        apiBodyStart: text.substring(0, 200)
                    };
                }
            """)
            return result
        finally:
            await context.close()

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
        f"/search?q={query}&cursor={next_cursor}&search_id={result['search_id']}"
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
        "version": "5.0.0",
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
        "version": "5.0.0",
        "endpoints": {
            "search": "/search?q=<query>",
            "pagination": "/search?q=<query>&cursor=<int>&search_id=<str>",
            "health": "/health",
            "docs": "/docs",
        },
    })



@app.route("/debug")
def debug():
    """Debug endpoint to check Playwright state."""
    try:
        raw = pw_manager._run_coro(pw_manager._debug_info(), timeout=60)
        return jsonify(raw)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("TikTok Search API v5.0.0 (Single-Process)")
    logger.info("=" * 60)
    pw_manager.start_background()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
