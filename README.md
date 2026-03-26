# TikTok Search API

Backend Flask care caută postări TikTok live cu profil Android, cu suport pentru paginare.

## Arhitectura

```
Flask API (port 5001)
    └── playwright_worker.py  ← proces dedicat Playwright async
            Chromium headless cu User-Agent Android (Pixel 3, Android 9)
            Navighează la tiktok.com/search?q=...
            Face fetch() din contextul paginii (cu cookies TikTok reale)
            Sesiuni persistente per query (paginare reală)
```

## Endpoints

### `GET /search`

| Parametru | Tip | Default | Descriere |
|---|---|---|---|
| `q` | string | **obligatoriu** | Termenul de căutare |
| `count` | integer | `12` | Rezultate per pagină (max 50) |
| `cursor` | integer | `0` | Offset paginare |
| `search_id` | string | — | ID sesiune din răspunsul anterior |

### Exemplu răspuns

```json
{
  "query": "funny cats",
  "posts": [
    {
      "id": "7621245573584162070",
      "description": "😂 Funny cats compilation #funnycat",
      "url": "https://www.tiktok.com/@user/video/7621245573584162070",
      "create_time": 1774459517,
      "author": {
        "unique_id": "funny_cats_daily",
        "nickname": "Funny Cats",
        "verified": false,
        "followers": 125000,
        "avatar": "https://..."
      },
      "stats": {
        "plays": 428900,
        "likes": 24030,
        "comments": 370,
        "shares": 990
      },
      "video": {
        "duration": 30,
        "cover": "https://...",
        "play_url": "https://...",
        "width": 576,
        "height": 1024
      },
      "hashtags": ["funnycat", "cats", "fyp"],
      "music": {
        "title": "original sound",
        "author": "funny_cats_daily",
        "cover": "https://..."
      }
    }
  ],
  "pagination": {
    "has_more": true,
    "cursor": 0,
    "next_cursor": 12,
    "search_id": "f798cd75-21b8-424c-8abc-123456789abc",
    "next_page_url": "/search?q=funny+cats&cursor=12&count=12&search_id=f798cd75-..."
  },
  "meta": {
    "total_found": 12,
    "took_ms": 8234.5,
    "android_user_agent": true
  }
}
```

### Paginare

```bash
# Pagina 1
GET /search?q=funny+cats&count=12

# Pagina 2 (folosind search_id și next_cursor din răspunsul anterior)
GET /search?q=funny+cats&count=12&cursor=12&search_id=<search_id_din_raspuns>

# Repetă până când has_more = false
```

### Alte endpoints

| Endpoint | Descriere |
|---|---|
| `GET /health` | Status server și componente |
| `GET /docs` | Documentație completă JSON |
| `GET /` | Index |

## Deployment local

```bash
pip install -r requirements.txt
playwright install chromium
python3 app.py
```

## Deployment Railway

```bash
# Instalare Railway CLI
npm install -g @railway/cli

# Login și deploy
railway login
railway init
railway up
```

## Deployment Docker

```bash
docker build -t tiktok-search-api .
docker run -p 5001:5001 tiktok-search-api
```
