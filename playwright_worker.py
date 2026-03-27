"""
Playwright Worker — Async API cu asyncio
=========================================
Folosește Playwright async API pentru a evita problemele cu greenlet/threading.
Toate operațiile Playwright rulează în același event loop asyncio.
Comunicare cu Flask prin socket TCP local (port 8764).

Optimizat pentru medii cu memorie limitată (Render free tier: 512MB).
"""
import asyncio
import json
import logging
import os
import time
import uuid
from typing import Dict, Optional, Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [PW-WORKER] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

WORKER_PORT = int(os.environ.get("WORKER_PORT", 8764))
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
        from_page: 'search',
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
    if (searchId) params.set('search_id', searchId);
    const resp = await fetch(
        '/api/search/general/full/?' + params.toString(),
        {
            credentials: 'include',
            headers: {
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'en-US,en;q=0.9',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-origin',
                'X-Requested-With': 'com.zhiliaoapp.musically'
            }
        }
    );
    const text = await resp.text();
    return { status: resp.status, body: text };
}
"""


class AsyncPlaywrightWorker:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.sessions: Dict[str, Any] = {}
        self._browser_lock = asyncio.Lock()

    async def _launch_browser(self):
        """Lansează sau relansează browserul."""
        from playwright.async_api import async_playwright

        # Închide browser-ul existent dacă există
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None

        if self.playwright is None:
            logger.info("Pornire Playwright async...")
            self.playwright = await async_playwright().start()

        logger.info("Lansare Chromium...")
        self.browser = await self.playwright.chromium.launch(
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
                "--disable-plugins",
                "--disable-images",
                "--blink-settings=imagesEnabled=false",
                "--js-flags=--max-old-space-size=128",
            ],
        )
        logger.info("Chromium pornit cu succes")

    async def start(self):
        await self._launch_browser()

    async def _get_or_create_session(self, session_id: Optional[str], query: str):
        """Returnează sesiunea existentă sau creează una nouă."""
        # Verifică dacă browser-ul este activ
        if not self.browser or not self.browser.is_connected():
            logger.warning("Browser-ul nu este conectat, relansare...")
            async with self._browser_lock:
                if not self.browser or not self.browser.is_connected():
                    await self._launch_browser()
                    self.sessions = {}

        if session_id and session_id in self.sessions:
            session = self.sessions[session_id]
            try:
                if not session["page"].is_closed():
                    logger.info(f"Refolosire sesiune {session_id[:8]} pentru '{query}'")
                    return session["page"], session_id
            except Exception:
                pass
            del self.sessions[session_id]

        # Curățare sesiuni vechi (> 3 min)
        now = time.time()
        expired = [k for k, v in list(self.sessions.items()) if now - v["created_at"] > 180]
        for k in expired:
            try:
                await self.sessions[k]["context"].close()
            except Exception:
                pass
            del self.sessions[k]

        # Maxim 2 sesiuni simultane
        if len(self.sessions) >= 2:
            oldest_key = min(self.sessions, key=lambda k: self.sessions[k]["created_at"])
            try:
                await self.sessions[oldest_key]["context"].close()
            except Exception:
                pass
            del self.sessions[oldest_key]

        # Creare context nou cu profil Android
        context = await self.browser.new_context(
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

        # Blochează resursele inutile
        await context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,mp4,webm}",
            lambda route: route.abort()
        )

        page = await context.new_page()
        logger.info(f"Navigare la TikTok search: '{query}'")
        try:
            await page.goto(
                f"https://www.tiktok.com/search?q={query}",
                timeout=30000,
                wait_until="commit",
            )
        except Exception as e:
            logger.warning(f"Navigare parțială (OK): {type(e).__name__}")

        # Așteptare pentru generarea token-urilor JS
        await page.wait_for_timeout(4000)

        new_sid = str(uuid.uuid4())
        self.sessions[new_sid] = {
            "page": page,
            "context": context,
            "query": query,
            "created_at": time.time(),
        }
        logger.info(f"Sesiune nouă creată: {new_sid[:8]}")
        return page, new_sid

    async def search(self, query: str, cursor: int, count: int,
                     search_id: Optional[str]) -> Dict:
        """Face o căutare TikTok și returnează rezultatele brute."""
        page, session_id = await self._get_or_create_session(search_id, query)
        logger.info(f"Fetch API: query='{query}' cursor={cursor} count={count}")
        try:
            result = await page.evaluate(
                FETCH_JS,
                {"keyword": query, "cursor": cursor, "count": count, "searchId": search_id or ""}
            )
        except Exception as e:
            # Dacă pagina s-a închis, încearcă cu o sesiune nouă
            if "closed" in str(e).lower() or "target" in str(e).lower():
                logger.warning(f"Pagina s-a închis, creare sesiune nouă: {e}")
                if search_id and search_id in self.sessions:
                    del self.sessions[search_id]
                page, session_id = await self._get_or_create_session(None, query)
                result = await page.evaluate(
                    FETCH_JS,
                    {"keyword": query, "cursor": cursor, "count": count, "searchId": ""}
                )
            else:
                raise RuntimeError(f"Eroare fetch: {e}")

        status = result.get("status", 0)
        body = result.get("body", "")
        if not body:
            raise ValueError(f"Răspuns gol de la TikTok (status={status})")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON invalid: {e} | body={body[:200]}")
        return {"data": data, "session_id": session_id}

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Procesează un request de la Flask."""
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=60)
            if not raw:
                return
            req = json.loads(raw.decode())
            query = req["query"]
            cursor = req.get("cursor", 0)
            count = req.get("count", 12)
            search_id = req.get("search_id")
            result = await self.search(query, cursor, count, search_id)
            response = {"ok": True, "result": result}
        except asyncio.TimeoutError:
            response = {"ok": False, "error": "Timeout căutare TikTok"}
        except Exception as e:
            logger.error(f"Eroare request: {e}")
            response = {"ok": False, "error": str(e)}
        try:
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except Exception as e:
            logger.error(f"Eroare trimitere răspuns: {e}")
        finally:
            writer.close()

    async def run_server(self):
        """Pornește serverul TCP asyncio."""
        server = await asyncio.start_server(
            self.handle_client,
            "127.0.0.1",
            WORKER_PORT,
        )
        logger.info(f"Worker ascultă pe portul {WORKER_PORT}")
        async with server:
            await server.serve_forever()


async def main():
    worker = AsyncPlaywrightWorker()
    await worker.start()
    await worker.run_server()


if __name__ == "__main__":
    asyncio.run(main())
