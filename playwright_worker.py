"""
Playwright Worker — Async API cu asyncio
=========================================

Folosește Playwright async API pentru a evita problemele cu greenlet/threading.
Toate operațiile Playwright rulează în același event loop asyncio.
Comunicare cu Flask prin socket TCP local (port 8764).
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

    async def start(self):
        from playwright.async_api import async_playwright
        logger.info("Pornire Playwright async...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--single-process",
            ],
        )
        logger.info("Playwright browser async pornit cu succes")

    async def _get_or_create_session(self, session_id: Optional[str], query: str):
        """Returnează sesiunea existentă sau creează una nouă."""
        if session_id and session_id in self.sessions:
            session = self.sessions[session_id]
            logger.info(f"Refolosire sesiune {session_id[:8]} pentru '{query}'")
            return session["page"], session_id

        # Creare context nou cu profil Android
        context = await self.browser.new_context(
            user_agent=ANDROID_UA,
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            locale="en-US",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua-mobile": "?1",
            },
        )
        page = await context.new_page()

        logger.info(f"Navigare la TikTok search: '{query}'")
        try:
            await page.goto(
                f"https://www.tiktok.com/search?q={query}",
                timeout=25000,
                wait_until="commit",
            )
        except Exception as e:
            logger.warning(f"Navigare parțială (OK): {type(e).__name__}")

        # Așteptare pentru generarea token-urilor JS
        await page.wait_for_timeout(3000)

        new_sid = str(uuid.uuid4())
        self.sessions[new_sid] = {
            "page": page,
            "context": context,
            "query": query,
            "created_at": time.time(),
        }

        # Curățare sesiuni vechi (> 5 min)
        now = time.time()
        expired = [k for k, v in list(self.sessions.items()) if now - v["created_at"] > 300]
        for k in expired:
            try:
                await self.sessions[k]["context"].close()
            except Exception:
                pass
            del self.sessions[k]

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
            raise RuntimeError(f"Eroare fetch: {e}")

        status = result.get("status", 0)
        body = result.get("body", "")

        if not body:
            raise ValueError(f"Răspuns gol de la TikTok (status={status})")

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON invalid: {e} | body={body[:100]}")

        return {"data": data, "session_id": session_id}

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Procesează un request de la Flask."""
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=40)
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
