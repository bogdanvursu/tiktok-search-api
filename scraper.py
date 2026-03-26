"""
TikTok Scraper — Client pentru Playwright Worker
=================================================

Comunică cu playwright_worker.py prin socket TCP local.
Parsează răspunsul TikTok API și returnează postările structurate.
"""

import json
import logging
import os
import socket
import subprocess
import time
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

WORKER_PORT = int(os.environ.get("WORKER_PORT", 8764))
WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), "playwright_worker.py")


# ============================================================
# Playwright Worker Client
# ============================================================

def _worker_request(payload: Dict, timeout: int = 40) -> Dict:
    """Trimite un request la playwright_worker și returnează răspunsul."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", WORKER_PORT))
        sock.sendall(json.dumps(payload).encode() + b"\n")

        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if chunk.endswith(b"\n"):
                break

        sock.close()
        return json.loads(b"".join(chunks).decode())
    except socket.timeout:
        raise TimeoutError(f"Playwright worker timeout după {timeout}s")
    except ConnectionRefusedError:
        raise RuntimeError("Playwright worker nu rulează — repornire automată...")
    except Exception as e:
        raise RuntimeError(f"Eroare comunicare worker: {e}")


def _ensure_worker_running() -> bool:
    """Verifică dacă worker-ul rulează și îl pornește dacă nu."""
    import subprocess
    result = subprocess.run(
        f"ss -tlnp 2>/dev/null | grep {WORKER_PORT}",
        shell=True, capture_output=True, text=True,
    )
    if str(WORKER_PORT) in result.stdout:
        return True

    logger.info(f"[Worker] Pornire playwright_worker pe portul {WORKER_PORT}...")
    subprocess.Popen(
        ["python3", WORKER_SCRIPT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for i in range(20):
        time.sleep(1)
        result = subprocess.run(
            f"ss -tlnp 2>/dev/null | grep {WORKER_PORT}",
            shell=True, capture_output=True, text=True,
        )
        if str(WORKER_PORT) in result.stdout:
            logger.info(f"[Worker] Gata după {i+1}s")
            return True

    logger.error("[Worker] Nu a pornit în 20s")
    return False


# ============================================================
# Parsare răspuns TikTok
# ============================================================

def _parse_posts(data: Dict, cursor: int, count: int) -> Dict[str, Any]:
    """Parsează răspunsul TikTok API."""
    posts: List[Dict] = []

    raw_items = data.get("data", []) or data.get("item_list", [])

    for raw_item in raw_items:
        # Structura reală: {"type": 1, "item": {...}, "common": {...}}
        if isinstance(raw_item, dict) and "item" in raw_item:
            video_info = raw_item["item"]
        else:
            video_info = raw_item

        if not video_info or not isinstance(video_info, dict):
            continue

        author = video_info.get("author", {})
        stats = video_info.get("stats", {})
        video = video_info.get("video", {})
        music = video_info.get("music", {})

        video_id = str(video_info.get("id", ""))
        author_id = author.get("uniqueId", author.get("unique_id", ""))

        # Hashtag-uri din challenges sau descriere
        hashtags = [
            ch.get("hashtagName", "")
            for ch in video_info.get("challenges", [])
            if ch.get("hashtagName")
        ]
        if not hashtags:
            desc = video_info.get("desc", "")
            hashtags = [w[1:] for w in desc.split() if w.startswith("#")]

        post = {
            "id": video_id,
            "description": video_info.get("desc", ""),
            "url": (
                f"https://www.tiktok.com/@{author_id}/video/{video_id}"
                if video_id and author_id else ""
            ),
            "create_time": video_info.get("createTime", 0),
            "author": {
                "unique_id": author_id,
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

    raw_cursor = data.get("cursor", cursor + count)
    search_id = data.get("search_id") or data.get("backtrace") or ""
    has_more = bool(data.get("has_more", 0))

    return {
        "posts": posts,
        "has_more": has_more,
        "cursor": cursor,
        "next_cursor": int(raw_cursor) if has_more else None,
        "search_id": search_id,
    }


# ============================================================
# Funcție publică principală
# ============================================================

def search_tiktok(
    query: str,
    cursor: int = 0,
    count: int = 12,
    search_id: Optional[str] = None,
    timeout: int = 40,
) -> Dict[str, Any]:
    """
    Caută postări TikTok live cu profil Android complet.

    Args:
        query:     Termenul de căutare
        cursor:    Offset paginare (0 = prima pagină)
        count:     Număr de rezultate dorite (max 50)
        search_id: ID sesiune din răspunsul anterior (pentru paginare)
        timeout:   Timeout total în secunde

    Returns:
        Dict cu: posts, has_more, cursor, next_cursor, search_id, session_id
    """
    logger.info(
        f"[Search] query='{query}' cursor={cursor} count={count} "
        f"search_id={search_id[:20] if search_id else 'None'}"
    )

    # Asigură că worker-ul rulează
    _ensure_worker_running()

    payload = {
        "query": query,
        "cursor": cursor,
        "count": count,
        "search_id": search_id,
    }

    response = _worker_request(payload, timeout=timeout)

    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Eroare necunoscută de la worker"))

    result_data = response["result"]
    raw_data = result_data["data"]
    session_id = result_data["session_id"]

    result = _parse_posts(raw_data, cursor=cursor, count=count)

    # Folosim session_id ca search_id pentru paginare dacă TikTok nu returnează unul
    if not result["search_id"]:
        result["search_id"] = session_id
    result["session_id"] = session_id

    logger.info(
        f"[Search] Rezultate: {len(result['posts'])} postări, "
        f"has_more={result['has_more']}, session={session_id[:8]}"
    )
    return result
