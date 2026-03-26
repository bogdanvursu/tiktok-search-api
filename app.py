"""
TikTok Search API
=================

Backend Flask care caută postări TikTok live cu profil Android.
Folosește Playwright Chromium cu User-Agent Android (Pixel 3) pentru
a face fetch-ul din contextul paginii TikTok (cu cookies și token-uri reale).

Endpoints:
  GET /search?q=<query>[&cursor=<int>][&search_id=<str>][&count=<int>]
  GET /health
  GET /docs
  GET /
"""

import logging
import os
import subprocess
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, request

from scraper import search_tiktok

# ============================================================
# Configurare logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ============================================================
# Flask app
# ============================================================

app = Flask(__name__)
app.json.sort_keys = False

PORT = int(os.environ.get("PORT", 5001))
WORKER_PORT = int(os.environ.get("WORKER_PORT", 8764))
WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), "playwright_worker.py")


def _start_playwright_worker():
    """Pornește playwright_worker.py ca subprocess la startup."""
    result = subprocess.run(
        f"ss -tlnp 2>/dev/null | grep {WORKER_PORT}",
        shell=True, capture_output=True, text=True,
    )
    if str(WORKER_PORT) in result.stdout:
        logger.info(f"[Worker] Deja rulează pe portul {WORKER_PORT}")
        return

    logger.info(f"[Worker] Pornire playwright_worker...")
    subprocess.Popen(
        ["python3", WORKER_SCRIPT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for i in range(25):
        time.sleep(1)
        result = subprocess.run(
            f"ss -tlnp 2>/dev/null | grep {WORKER_PORT}",
            shell=True, capture_output=True, text=True,
        )
        if str(WORKER_PORT) in result.stdout:
            logger.info(f"[Worker] Gata după {i+1}s")
            return

    logger.error("[Worker] Nu a pornit în 25s")


def _worker_status() -> str:
    result = subprocess.run(
        f"ss -tlnp 2>/dev/null | grep {WORKER_PORT}",
        shell=True, capture_output=True, text=True,
    )
    return "running" if str(WORKER_PORT) in result.stdout else "stopped"


# ============================================================
# Endpoints
# ============================================================

@app.route("/search")
def search():
    """
    Caută postări TikTok live.

    Query parameters:
      q         (str, required)  — termenul de căutare
      cursor    (int, optional)  — offset paginare, default 0
      search_id (str, optional)  — ID sesiune din răspunsul anterior
      count     (int, optional)  — rezultate per pagină, default 12, max 50
    """
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Parametrul 'q' este obligatoriu"}), 400

    try:
        cursor = int(request.args.get("cursor", 0))
        count = min(int(request.args.get("count", 12)), 50)
    except ValueError:
        return jsonify({
            "error": "Parametrii 'cursor' și 'count' trebuie să fie numere întregi"
        }), 400

    search_id = request.args.get("search_id") or None
    start_time = time.time()

    try:
        result = search_tiktok(
            query=query,
            cursor=cursor,
            count=count,
            search_id=search_id,
            timeout=40,
        )
    except TimeoutError as e:
        return jsonify({"error": str(e), "hint": "Playwright worker timeout"}), 504
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        logger.error(f"Eroare căutare: {e}", exc_info=True)
        return jsonify({"error": f"Eroare internă: {str(e)}"}), 500

    elapsed_ms = round((time.time() - start_time) * 1000, 2)

    new_search_id = result.get("search_id", "")
    next_cursor = result.get("next_cursor")
    has_more = result.get("has_more", False)

    next_page_url = None
    if has_more and next_cursor is not None:
        base_url = request.base_url
        next_page_url = (
            f"{base_url}?q={query}&cursor={next_cursor}&count={count}"
            + (f"&search_id={new_search_id}" if new_search_id else "")
        )

    return jsonify({
        "query": query,
        "posts": result["posts"],
        "pagination": {
            "has_more": has_more,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "search_id": new_search_id or None,
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
    """Verifică starea serverului și a componentelor."""
    worker = _worker_status()
    status = "ok" if worker == "running" else "degraded"

    return jsonify({
        "status": status,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "components": {
            "flask_api": "running",
            "playwright_worker": worker,
        },
    })


@app.route("/docs")
def docs():
    """Documentație completă API."""
    return jsonify({
        "name": "TikTok Search API",
        "version": "4.0.0",
        "description": (
            "API de căutare TikTok care folosește Playwright Chromium cu User-Agent Android "
            "(Pixel 3, Android 9) pentru a face fetch-uri din contextul paginii TikTok, "
            "cu toate cookies și token-urile generate de JavaScript-ul TikTok."
        ),
        "architecture": {
            "user_agent": "Mozilla/5.0 (Linux; Android 9; Pixel 3 Build/PQ3A.190801.002) ...",
            "method": "fetch() din contextul paginii TikTok (cu msToken, ttwid, cookies reale)",
            "endpoint_tiktok": "https://www.tiktok.com/api/search/general/full/",
            "paginare": "session_id (UUID) care mapează la o pagină Playwright persistentă",
        },
        "endpoints": {
            "GET /search": {
                "description": "Caută postări TikTok live",
                "parameters": {
                    "q": "string (obligatoriu) — termenul de căutare",
                    "cursor": "integer (opțional, default 0) — offset paginare",
                    "search_id": "string (opțional) — ID sesiune din răspunsul anterior",
                    "count": "integer (opțional, default 12, max 50) — rezultate per pagină",
                },
                "example": "/search?q=funny+cats&count=12",
            },
            "GET /health": {"description": "Starea serverului"},
            "GET /docs": {"description": "Această documentație"},
        },
        "pagination": {
            "description": "Folosiți next_page_url din răspuns sau construiți manual URL-ul",
            "example": {
                "step_1": "GET /search?q=funny+cats",
                "step_2": "GET /search?q=funny+cats&cursor=12&search_id=<din_raspuns>",
                "step_3": "Repetați până când has_more = false",
            },
        },
        "response_schema": {
            "query": "string",
            "posts": [{
                "id": "string",
                "description": "string",
                "url": "string",
                "create_time": "integer (unix timestamp)",
                "author": {
                    "unique_id": "string",
                    "nickname": "string",
                    "verified": "boolean",
                    "followers": "integer",
                    "avatar": "string (URL)",
                },
                "stats": {
                    "plays": "integer",
                    "likes": "integer",
                    "comments": "integer",
                    "shares": "integer",
                },
                "video": {
                    "duration": "integer (secunde)",
                    "cover": "string (URL)",
                    "play_url": "string (URL)",
                    "width": "integer",
                    "height": "integer",
                },
                "hashtags": ["string"],
                "music": {
                    "title": "string",
                    "author": "string",
                    "cover": "string (URL)",
                },
            }],
            "pagination": {
                "has_more": "boolean",
                "cursor": "integer",
                "next_cursor": "integer | null",
                "search_id": "string | null",
                "next_page_url": "string | null",
            },
            "meta": {
                "total_found": "integer",
                "took_ms": "float",
                "android_user_agent": "boolean",
            },
        },
    })


@app.route("/")
def index():
    return jsonify({
        "name": "TikTok Search API",
        "version": "4.0.0",
        "endpoints": {
            "search": "/search?q=<query>",
            "pagination": "/search?q=<query>&cursor=<int>&search_id=<str>",
            "health": "/health",
            "docs": "/docs",
        },
    })


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("TikTok Search API v4.0.0")
    logger.info("=" * 60)

    # Pornire playwright worker în background
    threading.Thread(target=_start_playwright_worker, daemon=True).start()

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
