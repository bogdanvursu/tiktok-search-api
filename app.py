"""
TikTok Search API v7.0
======================
Single-process architecture: Flask + Playwright asyncio in background thread.

Proxy support (v7.0):
- Set PROXY_SERVER env var to route all browser traffic through a proxy.
- Formats supported:
    http://host:port
    http://user:pass@host:port
    socks5://host:port
    socks5://user:pass@host:port
- If PROXY_SERVER is not set, no proxy is used (direct connection).

How it works:
1. Visit TikTok homepage to obtain real ttwid + msToken cookies.
2. Navigate to search page — TikTok's JS SDK initialises and generates X-Bogus.
3. Intercept the /api/search/general/full/ response via page.route() for
   buffered body access (works reliably even on cloud/datacenter IPs with proxy).
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

from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", 5001))

# ---------------------------------------------------------------------------
# Proxy configuration
# Set PROXY_SERVER to e.g. "http://user:pass@1.2.3.4:8080" or
# "socks5://1.2.3.4:1080" to route Playwright through a proxy.
# Leave unset (or empty) for direct connection.
# ---------------------------------------------------------------------------
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip()

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _build_proxy_config():
    """
    Build the Playwright proxy dict from PROXY_SERVER env var.
    Playwright proxy format:
        {"server": "http://host:port"}
        {"server": "http://host:port", "username": "user", "password": "pass"}
    """
    if not PROXY_SERVER:
        return None

    parsed = urllib.parse.urlparse(PROXY_SERVER)
    proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        proxy["username"] = urllib.parse.unquote(parsed.username)
    if parsed.password:
        proxy["password"] = urllib.parse.unquote(parsed.password)
    return proxy


PROXY_CONFIG = _build_proxy_config()

if PROXY_CONFIG:
    logger.info(f"[Config] Proxy enabled: {PROXY_CONFIG['server']}")
else:
    logger.info("[Config] No proxy configured — using direct connection.")


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

        launch_kwargs = dict(
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
                "--js-flags=--max-old-space-size=256",
            ],
        )
        if PROXY_CONFIG:
            launch_kwargs["proxy"] = PROXY_CONFIG

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        logger.info("[PW] Chromium launched successfully!")

    def _run_coro(self, coro, timeout=90):
        if not self._loop:
            raise RuntimeError("Event loop not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def search(self, query, cursor, count, search_id, timeout=80):
        return self._run_coro(
            self._async_search(query, cursor, count, search_id),
            timeout=timeout,
        )

    async def _ensure_browser(self):
        if not self._browser or not self._browser.is_connected():
            logger.warning("[PW] Browser disconnected, restarting...")
            await self._init()

    async def _async_search(self, query, cursor, count, search_id):
        """
        1. Visit TikTok homepage → get real ttwid + msToken cookies.
        2. Navigate to search page → TikTok JS SDK initialises, X-Bogus generated.
        3. Trigger fetch() → intercept response via page.route() (buffered body).
        """
        await self._ensure_browser()

        context = await self._browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

        try:
            # ----------------------------------------------------------------
            # Step 1 — bootstrap cookies via homepage visit
            # ----------------------------------------------------------------
            logger.info("[PW] Visiting TikTok homepage to obtain cookies...")
            homepage = await context.new_page()
            try:
                await homepage.goto(
                    "https://www.tiktok.com/",
                    timeout=20000,
                    wait_until="commit",
                )
                await homepage.wait_for_timeout(2000)
            except Exception as e:
                logger.warning(f"[PW] Homepage partial load (OK): {type(e).__name__}")
            finally:
                await homepage.close()

            cookies = await context.cookies("https://www.tiktok.com")
            cookie_names = [c["name"] for c in cookies]
            logger.info(f"[PW] Cookies after homepage: {cookie_names}")

            # ----------------------------------------------------------------
            # Step 2 — navigate to search and intercept API response
            # ----------------------------------------------------------------
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
                                "mt_search_general_user_live_card": 1,
                            }
                        },
                        "search_server": {},
                    }
                }),
            }
            if search_id:
                params["search_id"] = search_id

            api_url_prefix = "/api/search/general/full/"

            async def handle_route(route):
                if api_url_prefix in route.request.url and not intercept_event.is_set():
                    try:
                        response = await route.fetch()
                        body = await response.body()
                        intercepted_data["body"] = body
                        intercepted_data["status"] = response.status
                        has_xbogus = "X-Bogus" in route.request.url
                        logger.info(
                            f"[PW] Intercepted API: status={response.status}, "
                            f"len={len(body)}, X-Bogus={has_xbogus}"
                        )
                        intercept_event.set()
                        await route.fulfill(response=response)
                    except Exception as e:
                        logger.error(f"[PW] Route error: {e}")
                        intercepted_data["error"] = str(e)
                        intercept_event.set()
                        await route.continue_()
                elif any(
                    ext in route.request.url
                    for ext in [
                        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
                        ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".webm",
                    ]
                ):
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**", handle_route)

            logger.info(f"[PW] Navigating to TikTok search: {query!r}")
            try:
                await page.goto(
                    "https://www.tiktok.com/search?q=" + urllib.parse.quote(query),
                    timeout=25000,
                    wait_until="commit",
                )
                logger.info(f"[PW] Search page loaded: {page.url}")
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
                params,
            )

            try:
                await asyncio.wait_for(intercept_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                raise ValueError(
                    f"Timeout waiting for TikTok API response (query={query!r})"
                )

            if intercepted_data["error"]:
                raise ValueError(
                    f"Error intercepting response: {intercepted_data['error']}"
                )

            body = intercepted_data["body"]
            status = intercepted_data["status"]

            if not body:
                raise ValueError(f"Empty response from TikTok (status={status})")

            try:
                data = json.loads(body)
            except json.JSONDecodeError as e:
                body_str = (
                    body.decode("utf-8", errors="replace")
                    if isinstance(body, bytes)
                    else body
                )
                raise ValueError(f"Invalid JSON: {e} | body={body_str[:200]}")

            return {"data": data, "session_id": session_id}

        finally:
            try:
                await context.close()
            except Exception:
                pass

    def is_ready(self):
        return self._browser is not None and self._browser.is_connected()

    def proxy_info(self):
        if PROXY_CONFIG:
            return PROXY_CONFIG.get("server", "configured")
        return None


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
                "url": (
                    "https://www.tiktok.com/@"
                    + author.get("uniqueId", "")
                    + "/video/"
                    + item_data.get("id", "")
                ),
                "create_time": item_data.get("createTime", 0),
                "author": {
                    "id": author.get("id", ""),
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
    next_cursor = raw_data.get("cursor", cursor + len(posts))
    search_id = (
        raw_data.get("search_id")
        or raw_data.get("extra", {}).get("search_id")
    )

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
        raw = pw_manager.search(
            query=query, cursor=cursor, count=count, search_id=search_id
        )
    except Exception as e:
        logger.error(f"[Search] Error: {e}")
        return jsonify({"error": str(e)}), 500

    result = _parse_posts(raw["data"], cursor=cursor, count=count)
    if not result["search_id"]:
        result["search_id"] = raw.get("session_id")

    elapsed_ms = round((time.time() - start_time) * 1000, 1)
    next_cursor = result["next_cursor"]
    next_page_url = (
        "/search?q="
        + urllib.parse.quote(query)
        + "&cursor="
        + str(next_cursor)
        + "&search_id="
        + str(result["search_id"])
        if result["has_more"] and next_cursor
        else None
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
        "proxy": pw_manager.proxy_info(),
    })


@app.route("/docs")
def docs():
    return jsonify({
        "name": "TikTok Search API",
        "version": "7.0.0",
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
            "GET /health": "Server status + proxy info",
            "GET /docs": "This documentation",
        },
        "proxy": {
            "env_var": "PROXY_SERVER",
            "formats": [
                "http://host:port",
                "http://user:pass@host:port",
                "socks5://host:port",
                "socks5://user:pass@host:port",
            ],
            "example": "export PROXY_SERVER=http://user:pass@1.2.3.4:8080",
        },
    })


@app.route("/")
def index():
    return jsonify({
        "name": "TikTok Search API",
        "version": "7.0.0",
        "endpoints": {
            "search": "/search?q=<query>",
            "pagination": "/search?q=<query>&cursor=<int>&search_id=<str>",
            "health": "/health",
            "docs": "/docs",
        },
    })


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("TikTok Search API v7.0.0")
    logger.info(f"Proxy: {PROXY_CONFIG['server'] if PROXY_CONFIG else 'none (direct)'}")
    logger.info("=" * 60)
    pw_manager.start_background()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
