#!/usr/bin/env python3
"""Trend Viewer local server — trending YouTube/Shorts/Reels/X/Threads/TikTok
plus AI-video model/news feeds, with no API keys required.

Standard library only. Run:  python3 server.py  →  http://localhost:8778

Configuration (environment variables, all optional):
  PORT              HTTP port                            (default 8778)
  REGION            Initial region until one is picked
                    in the UI (header selector; persisted
                    to settings.json)                    (default US)
  YT_HL / YT_GL     Advanced: override the region's
                    YouTube language/country             (unset)
  CACHE_TTL         Cache lifetime in seconds            (default 3600)
  REFRESH_INTERVAL  Background refresh in seconds, 0=off (default 3600)
"""
import base64
import email.utils
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, urlencode

PORT = int(os.environ.get("PORT", "8778"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_TTL = int(os.environ.get("CACHE_TTL", "3600"))
STALE_RETRY = 300  # after a failed refresh, retry upstream at most every 5 min
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "3600"))  # 0 disables
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Cache entries: key -> {"data": ..., "fetched": ts of the data itself,
#                        "checked": ts of the last refresh attempt, "stale": bool}
_cache = {}
_cache_lock = threading.Lock()
# Thumbnail proxy in-memory cache (url -> (content_type, bytes))
_img_cache = {}
_img_lock = threading.Lock()
IMG_CACHE_MAX = 600

# ---------------------------------------------------------------- Region
# One region drives everything: YouTube search language/country, TikTok's
# trending feed, and localized category queries. Picked in the UI (header
# selector), persisted to settings.json; the REGION env var is only the
# initial default. hl stays "en" except where we have localized
# published-time words (see PERIOD_EXCLUDE) — country targeting comes
# from gl, which works with any hl.
REGIONS = {
    # "Global" keeps the raw worldwide views-sort; every real region fetches
    # relevance-sorted (region-targeted) results and ranks by views locally,
    # because a global views sort drowns every region in the biggest upload
    # markets no matter what gl says.
    "GLOBAL": {"label": "Global (most viewed)", "hl": "en", "gl": "US",
               "tiktok": "US", "views_sort": True},
    "US": {"label": "United States", "hl": "en", "gl": "US"},
    "KR": {"label": "South Korea", "hl": "ko", "gl": "KR"},
    "JP": {"label": "Japan", "hl": "ja", "gl": "JP"},
    "TW": {"label": "Taiwan", "hl": "zh-TW", "gl": "TW"},
    "GB": {"label": "United Kingdom", "hl": "en", "gl": "GB"},
    "DE": {"label": "Germany", "hl": "en", "gl": "DE"},
    "FR": {"label": "France", "hl": "en", "gl": "FR"},
    "IN": {"label": "India", "hl": "en", "gl": "IN"},
    "BR": {"label": "Brazil", "hl": "en", "gl": "BR"},
    "ID": {"label": "Indonesia", "hl": "en", "gl": "ID"},
    "VN": {"label": "Vietnam", "hl": "en", "gl": "VN"},
}
_env_region = os.environ.get("REGION", "US").upper()
DEFAULT_REGION = _env_region if _env_region in REGIONS else "US"
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
_settings_lock = threading.Lock()


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
            if isinstance(s, dict):
                return s
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_settings(s):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def current_region() -> str:
    code = str(load_settings().get("region", "")).upper()
    return code if code in REGIONS else DEFAULT_REGION


def set_region(code: str) -> str:
    code = str(code or "").upper()
    if code not in REGIONS:
        return current_region()
    with _settings_lock:
        s = load_settings()
        s["region"] = code
        save_settings(s)
    return code


def yt_locale():
    r = REGIONS[current_region()]
    return (os.environ.get("YT_HL") or r["hl"],
            os.environ.get("YT_GL") or r["gl"])


# ---------------------------------------------------------------- YouTube
# Category label -> YouTube search query
CATEGORIES = {
    "Mukbang": "mukbang",
    "Beauty/Fashion": "beauty makeup fashion",
    "Vlog": "vlog",
    "Comedy": "funny comedy",
    "Movies/TV": "movie drama review",
    "Tech": "tech review",
    "Education": "explained documentary",
    "Travel": "travel",
    "Animals": "dog cat",
}
# The "All" tab merges these categories, re-sorted by views
ALL_MERGE = ["Mukbang", "Vlog", "Comedy", "Beauty/Fashion", "Movies/TV", "Travel"]

# Localized query overrides per YouTube hl (labels stay English in the UI;
# only the search terms change). Missing labels fall back to English.
CATEGORY_QUERIES_L10N = {
    "ko": {
        "Mukbang": "먹방", "Beauty/Fashion": "뷰티 메이크업 패션", "Vlog": "브이로그",
        "Comedy": "예능 웃긴 영상", "Movies/TV": "영화 드라마 리뷰", "Tech": "테크 리뷰",
        "Education": "지식 교양", "Travel": "여행", "Animals": "강아지 고양이",
    },
    "ja": {
        "Mukbang": "モッパン 大食い", "Beauty/Fashion": "美容 メイク", "Vlog": "vlog 日常",
        "Comedy": "お笑い 面白い動画", "Movies/TV": "映画 ドラマ レビュー", "Tech": "ガジェット レビュー",
        "Education": "解説 教養", "Travel": "旅行", "Animals": "犬 猫",
    },
    "zh-TW": {
        "Mukbang": "吃播", "Beauty/Fashion": "美妝 時尚", "Vlog": "vlog 日常",
        "Comedy": "搞笑 綜藝", "Movies/TV": "電影 戲劇 影評", "Tech": "科技 開箱",
        "Education": "知識 科普", "Travel": "旅遊", "Animals": "狗 貓",
    },
}
AI_QUERIES_L10N = {
    "ko": ["AI 영상 제작", "AI 영상 생성", "sora ai video", "runway kling veo"],
    "ja": ["AI 動画 生成", "AI 動画 作り方", "sora ai video", "runway kling veo"],
    "zh-TW": ["AI 影片 生成", "AI 影像 生成", "sora ai video", "runway kling veo"],
}


def category_query(label: str, hl: str) -> str:
    return CATEGORY_QUERIES_L10N.get(hl, {}).get(label) or CATEGORIES.get(label, label)


# Search-filter protobuf: upload date (2=today, 3=this week, 4=this month)
PERIOD_CODE = {"day": 2, "week": 3, "month": 4}

# Recommendation shelves mixed into search results can bypass the upload-date
# filter; drop them by their published-time text ("N days ago" style). The
# words are localized per hl because YouTube localizes that text; unknown
# languages fall back to English (regions we ship keep hl in this table).
PERIOD_EXCLUDE = {
    "en": {
        "day": ("day", "week", "month", "year"),
        "week": ("week", "month", "year"),
        "month": ("month", "year"),
    },
    "ko": {
        "day": ("일 전", "주 전", "개월 전", "년 전"),
        "week": ("주 전", "개월 전", "년 전"),
        "month": ("개월 전", "년 전"),
    },
    "ja": {
        "day": ("日前", "週間前", "か月前", "年前"),
        "week": ("週間前", "か月前", "年前"),
        "month": ("か月前", "年前"),
    },
    "zh-TW": {
        "day": ("天前", "週前", "个月前", "個月前", "年前"),
        "week": ("週前", "个月前", "個月前", "年前"),
        "month": ("个月前", "個月前", "年前"),
    },
}

# ---------------------------------------------------------------- Instagram Reels
IG_APP_ID = "936619743392459"  # public app id used by the instagram.com web client
ACCOUNTS_FILE = os.path.join(BASE_DIR, "reels_accounts.json")
DEFAULT_IG_ACCOUNTS = [
    "openai", "runwayapp", "pika_labs", "lumalabsai", "midjourney",
    "klingai_official", "heygen_official", "higgsfield.ai", "googledeepmind",
]

# ---------------------------------------------------------------- X (Twitter)
X_ACCOUNTS_FILE = os.path.join(BASE_DIR, "x_accounts.json")
DEFAULT_X_ACCOUNTS = [
    "OpenAI", "runwayml", "Kling_ai", "GoogleDeepMind", "midjourney",
    "LumaLabsAI", "pika_labs", "heygen_com", "elevenlabsio", "AIatMeta",
]

# ---------------------------------------------------------------- Threads
THREADS_ACCOUNTS_FILE = os.path.join(BASE_DIR, "threads_accounts.json")
DEFAULT_THREADS_ACCOUNTS = [
    "openai", "runway", "google", "meta.ai", "zuck",
]
IG_APP_ID_THREADS = "238260118697367"  # public app id used by the threads.com web client

# ---------------------------------------------------------------- TikTok
# The free tikwm API handles TikTok's request signing (X-Bogus/msToken) and
# returns views/likes/comments without authentication.
TIKTOK_ACCOUNTS_FILE = os.path.join(BASE_DIR, "tiktok_accounts.json")
DEFAULT_TIKTOK_ACCOUNTS = [
    "openai", "runwayapp", "krea.ai", "elevenlabs", "sora",
    "zachking", "khaby.lame", "google",
]
TIKWM_BASE = "https://www.tikwm.com/api"

# ---------------------------------------------------------------- AI videos tab
AI_YT_QUERIES = ["AI video generation", "sora ai video", "runway kling veo", "AI filmmaking"]
NEWS_FEEDS = [
    ("AI video", "https://news.google.com/rss/search?q=" +
     quote('"AI video" model OR Sora OR Runway OR Kling OR Veo') + "&hl=en-US&gl=US&ceid=US:en"),
    ("GenAI", "https://news.google.com/rss/search?q=" +
     quote('"generative AI" video OR "text-to-video"') + "&hl=en-US&gl=US&ceid=US:en"),
]
HF_PIPELINES = ["text-to-video", "image-to-video"]
# Hosts the image proxy may fetch from (to bypass CDN hotlink blocking)
IMG_PROXY_ALLOW = (".cdninstagram.com", ".fbcdn.net", ".ytimg.com",
                   ".googleusercontent.com", ".twimg.com",
                   ".tiktokcdn.com", ".tiktokcdn-eu.com", ".tiktokcdn-us.com")

# ---------------------------------------------------------------- Favorites
FAVORITES_FILE = os.path.join(BASE_DIR, "favorites.json")
FAV_FIELDS = ("platform", "id", "title", "account", "url", "thumbnail",
              "views", "likes", "savedAt")
_fav_lock = threading.Lock()

# ---------------------------------------------------------------- Trend history
DB_FILE = os.path.join(BASE_DIR, "trends.db")
HISTORY_DAYS = 14
_db_lock = threading.Lock()

FAVICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
               '<text y="26" font-size="26">\U0001F4C8</text></svg>').encode()


def _db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS snapshots ("
        " platform TEXT, item_id TEXT, day TEXT,"
        " views INTEGER, likes INTEGER, comments INTEGER,"
        " PRIMARY KEY (platform, item_id, day))")
    return conn


def record_snapshot(platform, items, id_field="id"):
    """Store today's per-item metrics so tomorrow's load can show deltas."""
    if not items:
        return
    day = time.strftime("%Y-%m-%d")
    rows = []
    for it in items:
        item_id = str(it.get(id_field) or it.get("url") or "")
        if not item_id:
            continue
        rows.append((platform, item_id, day,
                     int(it.get("views") or 0), int(it.get("likes") or 0),
                     int(it.get("comments") or 0)))
    if not rows:
        return
    try:
        with _db_lock, closing(_db()) as conn:
            conn.executemany("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)", rows)
            cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
            conn.execute("DELETE FROM snapshots WHERE day < ?", (cutoff,))
            conn.commit()
    except sqlite3.Error:
        pass


def attach_history(platform, items, id_field="id", metric="views"):
    """Add `delta` (vs the most recent earlier day) and `history` (daily series)."""
    if not items:
        return items
    today = time.strftime("%Y-%m-%d")
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - HISTORY_DAYS * 86400))
    col = metric if metric in ("views", "likes", "comments") else "views"
    hist = {}
    try:
        with _db_lock, closing(_db()) as conn:
            rows = conn.execute(
                "SELECT item_id, day, %s FROM snapshots"
                " WHERE platform = ? AND day >= ? ORDER BY day" % col,
                (platform, cutoff)).fetchall()
    except sqlite3.Error:
        return items
    for item_id, day, value in rows:
        hist.setdefault(item_id, []).append((day, value))
    for it in items:
        item_id = str(it.get(id_field) or it.get("url") or "")
        series = hist.get(item_id)
        if not series:
            continue
        current = int(it.get(metric) or 0)
        prior = [v for d, v in series if d < today]
        if prior:
            it["delta"] = current - prior[-1]
        points = prior + [current]
        if len(points) >= 3:
            it["history"] = points[-HISTORY_DAYS:]
    return items


def within_period(published: str, period: str, hl: str = "en") -> bool:
    if not published:
        return True  # no published-time text (live streams etc.) passes through
    words = PERIOD_EXCLUDE.get(hl, PERIOD_EXCLUDE["en"]).get(period, ())
    return not any(word in published for word in words)


def build_search_params(period: str, shorts: bool = False, by_views: bool = False) -> str:
    """Build the search-filter protobuf (upload date, video type, length) as base64.

    by_views adds sort=view count (field 1 = 3). Left off by default: a global
    views sort ignores regional relevance entirely, so regional fetches use
    YouTube's relevance ranking (which honors gl) and re-rank by views locally.
    """
    filters = bytes([0x08, PERIOD_CODE.get(period, 3), 0x10, 0x01])
    if shorts:
        filters += bytes([0x18, 0x01])  # duration: under 4 minutes
    raw = (bytes([0x08, 0x03]) if by_views else b"") + bytes([0x12, len(filters)]) + filters
    return base64.urlsafe_b64encode(raw).decode()


def http_get(url: str, payload=None, headers=None, timeout=15):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", UA)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.headers.get("Content-Type", ""), resp.read()


def http_json(url: str, payload=None, headers=None, timeout=15):
    _, body = http_get(url, payload, headers, timeout)
    return json.loads(body.decode())


def parse_view_count(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


def parse_compact_number(text: str) -> int:
    """'1.2M' / '12K' / '3.4B' / '1,234' -> int."""
    m = re.match(r"\s*([\d.,]+)\s*([KMB]?)", str(text or ""), re.I)
    if not m:
        return 0
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[m.group(2).upper()]
    return int(num * mult)


# Per-source fetch status for the UI's status panel (/api/status)
_status = {}
_status_lock = threading.Lock()


def _source_of_key(key):
    kind = key[0] if isinstance(key, tuple) and key else str(key)
    if kind == "yt":
        return "shorts" if key[3] else "youtube"
    return kind


def _count_items(data):
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return sum(_count_items(v) for v in data.values() if isinstance(v, (list, dict)))
    return 0


def cached(key, force, fetch_fn, is_good=bool):
    """Cache with stale fallback.

    A refresh that raises or returns something `is_good` rejects (empty list,
    None) does NOT overwrite the last good result — the previous data is served
    with stale=True, and the next upstream retry happens after STALE_RETRY
    seconds instead of a full CACHE_TTL. This prevents one transient upstream
    block from blanking a tab for an hour.

    Returns (data, fetched_at, stale).
    """
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not force:
            ttl = STALE_RETRY if hit["stale"] else CACHE_TTL
            if now - hit["checked"] < ttl:
                return hit["data"], hit["fetched"], hit["stale"]
    try:
        result = fetch_fn()
    except Exception:
        result = None
    now = time.time()
    with _cache_lock:
        prev = _cache.get(key)
        if result is not None and is_good(result):
            _cache[key] = {"data": result, "fetched": now, "checked": now, "stale": False}
        elif prev:
            prev["checked"] = now
            prev["stale"] = True
        else:
            empty = result if result is not None else []
            _cache[key] = {"data": empty, "fetched": now, "checked": now, "stale": False}
        hit = _cache[key]
        data, fetched, stale = hit["data"], hit["fetched"], hit["stale"]
    with _status_lock:
        _status[_source_of_key(key)] = {
            "lastAttempt": now, "lastSuccess": fetched,
            "stale": stale, "items": _count_items(data),
        }
    return data, fetched, stale


# ---------------------------------------------------------------- backoff
# After FAIL_LIMIT consecutive empty/failed fetches for one source+account,
# skip that account for COOLDOWN seconds instead of hammering a blocking host.
_fail = {}
_fail_lock = threading.Lock()
FAIL_LIMIT = 3
COOLDOWN = 600


def with_backoff(key, fn):
    now = time.time()
    with _fail_lock:
        f = _fail.get(key)
        if f and f["count"] >= FAIL_LIMIT and now < f["until"]:
            return []
    try:
        out = fn()
    except Exception:
        out = []
    with _fail_lock:
        if out:
            _fail.pop(key, None)
        else:
            f = _fail.setdefault(key, {"count": 0, "until": 0})
            f["count"] += 1
            if f["count"] >= FAIL_LIMIT:
                f["until"] = time.time() + COOLDOWN
    return out


# ================================================================ YouTube
def extract_videos(node, out):
    """Walk the response tree collecting videoRenderer entries."""
    if isinstance(node, dict):
        if "videoRenderer" in node:
            v = node["videoRenderer"]
            title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", []))
            views_text = v.get("viewCountText", {}).get("simpleText", "")
            thumbs = v.get("thumbnail", {}).get("thumbnails", [])
            out.append({
                "id": v.get("videoId", ""),
                "title": title,
                "channel": "".join(r.get("text", "") for r in v.get("ownerText", {}).get("runs", [])),
                "views": parse_view_count(views_text),
                "viewsText": views_text,
                "length": v.get("lengthText", {}).get("simpleText", ""),
                "published": v.get("publishedTimeText", {}).get("simpleText", ""),
                "thumbnail": thumbs[-1]["url"] if thumbs else "",
            })
        for value in node.values():
            extract_videos(value, out)
    elif isinstance(node, list):
        for item in node:
            extract_videos(item, out)


def yt_client_context():
    hl, gl = yt_locale()
    return {"context": {"client": {
        "clientName": "WEB",
        "clientVersion": "2.20250624.01.00",
        "hl": hl, "gl": gl,
    }}}


def yt_search(query: str, period: str, shorts: bool):
    hl, _ = yt_locale()
    by_views = REGIONS[current_region()].get("views_sort", False)
    payload = dict(yt_client_context(),
                   query=query, params=build_search_params(period, shorts, by_views))
    try:
        data = http_json("https://www.youtube.com/youtubei/v1/search", payload)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    videos = []
    extract_videos(data, videos)
    seen, unique = set(), []
    for v in videos:
        if v["id"] and v["id"] not in seen and within_period(v["published"], period, hl):
            seen.add(v["id"])
            unique.append(v)
    return unique


def parse_like_count(data) -> int:
    """Extract the like count from a youtubei/v1/next response.

    Prefers the structured likeCountEntity (locale-independent); falls back to
    the localized accessibility strings only if the entity is missing.
    """
    found = []

    def walk(node):
        if isinstance(node, dict):
            e = node.get("likeCountEntity")
            if isinstance(e, dict):
                found.append(e)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(data)
    for e in found:
        for k in ("expandedLikeCountIfIndifferent", "likeCountIfIndifferent",
                  "likeCountIfLiked"):
            content = (e.get(k) or {}).get("content")
            if content:
                n = parse_compact_number(content)
                if n:
                    return n
    # Fallback: localized "N others liked" strings in the raw payload
    s = json.dumps(data, ensure_ascii=False)
    m = (re.search(r"along with ([0-9,]+) other", s)
         or re.search(r"다른 사용자 ([0-9,]+)명", s))
    return int(m.group(1).replace(",", "")) + 1 if m else 0


def yt_like_count(video_id: str):
    """Fetch a single video's like count via youtubei/v1/next (absent from search)."""
    payload = dict(yt_client_context(), videoId=video_id)
    try:
        data = http_json("https://www.youtube.com/youtubei/v1/next", payload, timeout=10)
        return parse_like_count(data)
    except Exception:
        return 0


def enrich_likes(videos, limit=45):
    """Fill in likes for the top videos in parallel; skips already-filled items."""
    todo = [v for v in videos[:limit] if not v.get("likes")]
    if not todo:
        return videos
    with ThreadPoolExecutor(max_workers=12) as pool:
        counts = pool.map(lambda v: yt_like_count(v["id"]), todo)
    for v, c in zip(todo, counts):
        v["likes"] = c
    return videos


def merge_yt_searches(queries, period, shorts):
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = pool.map(lambda q: yt_search(q, period, shorts), queries)
    merged, seen = [], set()
    for chunk in results:
        for v in chunk:
            if v["id"] not in seen:
                seen.add(v["id"])
                merged.append(v)
    merged.sort(key=lambda v: v["views"], reverse=True)
    return merged


def get_videos(category: str, period: str, shorts: bool, force: bool,
               enrich: bool = False, query: str = ""):
    platform = "shorts" if shorts else "youtube"
    hl, _ = yt_locale()
    region = current_region()  # cache per region code: GLOBAL and US share a locale

    def fetch():
        if query:
            queries = [query]
        elif category == "All":
            queries = [category_query(c, hl) for c in ALL_MERGE]
        elif category == "AI":
            queries = AI_QUERIES_L10N.get(hl, AI_YT_QUERIES)
        else:
            queries = [category_query(category, hl)]
        vids = merge_yt_searches(queries, period, shorts)
        if enrich:
            enrich_likes(vids)
        record_snapshot(platform, vids)
        attach_history(platform, vids)
        return vids
    return cached(("yt", query or category, period, shorts, enrich, region), force, fetch)


# ================================================================ Instagram Reels
def load_accounts(path, defaults):
    try:
        with open(path) as f:
            accounts = json.load(f)
            if isinstance(accounts, list) and accounts:
                return accounts
    except (OSError, json.JSONDecodeError):
        pass
    return list(defaults)


def save_accounts(path, accounts):
    with open(path, "w") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)


# Per-source account-list configuration (file path, default accounts)
ACCOUNT_SOURCES = {
    "reels": (ACCOUNTS_FILE, DEFAULT_IG_ACCOUNTS),
    "x": (X_ACCOUNTS_FILE, DEFAULT_X_ACCOUNTS),
    "threads": (THREADS_ACCOUNTS_FILE, DEFAULT_THREADS_ACCOUNTS),
    "tiktok": (TIKTOK_ACCOUNTS_FILE, DEFAULT_TIKTOK_ACCOUNTS),
}


def fetch_ig_reels(username: str):
    """Fetch an account's recent reels via Instagram's web-internal API (no auth)."""
    url = ("https://www.instagram.com/api/v1/users/web_profile_info/?username="
           + quote(username))
    try:
        data = http_json(url, headers={"x-ig-app-id": IG_APP_ID}, timeout=12)
    except Exception:
        return []
    user = (data.get("data") or {}).get("user") or {}
    reels = []
    for edge in (user.get("edge_owner_to_timeline_media") or {}).get("edges", []):
        n = edge.get("node", {})
        if not n.get("is_video"):
            continue
        caps = (n.get("edge_media_to_caption") or {}).get("edges") or []
        title = caps[0]["node"]["text"].split("\n")[0][:120] if caps else ""
        reels.append({
            "account": username,
            "title": title or "(no caption)",
            "views": n.get("video_view_count") or 0,
            "likes": (n.get("edge_liked_by") or {}).get("count", 0),
            "comments": (n.get("edge_media_to_comment") or {}).get("count", 0),
            "thumbnail": n.get("thumbnail_src") or "",
            "url": "https://www.instagram.com/reel/%s/" % n.get("shortcode", ""),
            "id": n.get("shortcode", ""),
            "takenAt": n.get("taken_at_timestamp") or 0,
        })
    return reels


def get_reels(force: bool):
    accounts = load_accounts(ACCOUNTS_FILE, DEFAULT_IG_ACCOUNTS)

    def fetch():
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = pool.map(
                lambda a: with_backoff(("reels", a), lambda: fetch_ig_reels(a)), accounts)
        merged = [r for chunk in results for r in chunk]
        merged.sort(key=lambda r: r["views"], reverse=True)
        record_snapshot("reels", merged)
        attach_history("reels", merged)
        return merged
    reels, fetched_at, stale = cached(("reels", tuple(accounts)), force, fetch)
    return reels, accounts, fetched_at, stale


# ================================================================ X (Twitter)
def _find_timeline_entries(node):
    """Locate the timeline entries list inside syndication __NEXT_DATA__."""
    if isinstance(node, dict):
        tl = node.get("timeline")
        if isinstance(tl, dict) and isinstance(tl.get("entries"), list):
            return tl["entries"]
        for v in node.values():
            r = _find_timeline_entries(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_timeline_entries(v)
            if r:
                return r
    return None


def parse_x_html(html: str, username: str):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    entries = _find_timeline_entries(data) or []
    posts = []
    for e in entries:
        content = e.get("content", {}) if isinstance(e, dict) else {}
        t = content.get("tweet")
        if not isinstance(t, dict):
            tr = content.get("tweetResult") or {}
            t = tr.get("result") if isinstance(tr, dict) else None
        if not isinstance(t, dict) or t.get("favorite_count") is None:
            continue
        user = t.get("user", {}) if isinstance(t.get("user"), dict) else {}
        media = ""
        for mm in (t.get("mediaDetails") or []):
            if mm.get("media_url_https"):
                media = mm["media_url_https"]
                break
        posts.append({
            "account": username,
            "name": user.get("name", username),
            "text": (t.get("full_text") or t.get("text") or "").strip(),
            "likes": t.get("favorite_count") or 0,
            "replies": t.get("reply_count") or 0,
            "retweets": t.get("retweet_count") or 0,
            "views": int(t.get("views", {}).get("count", 0)) if isinstance(t.get("views"), dict) else 0,
            "media": media,
            "url": "https://x.com/%s/status/%s" % (username, t.get("id_str", "")),
            "id": t.get("id_str", ""),
            "createdAt": t.get("created_at", ""),
        })
    return posts


def fetch_x_posts(username: str):
    """Fetch recent tweets + engagement via Twitter's embed (syndication) API, no auth."""
    url = "https://syndication.twitter.com/srv/timeline-profile/screen-name/" + quote(username)
    try:
        _, body = http_get(url, headers={"Accept": "text/html"}, timeout=12)
    except Exception:
        return []
    return parse_x_html(body.decode("utf-8", "ignore"), username)


def get_x_posts(force: bool):
    accounts = load_accounts(X_ACCOUNTS_FILE, DEFAULT_X_ACCOUNTS)

    def fetch():
        # The syndication endpoint returns empty pages under load; keep concurrency low.
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = pool.map(
                lambda a: with_backoff(("x", a), lambda: fetch_x_posts(a)), accounts)
        posts = [p for chunk in results for p in chunk]
        record_snapshot("x", posts)
        attach_history("x", posts, metric="likes")
        return posts
    posts, fetched_at, stale = cached(("x", tuple(accounts)), force, fetch)
    return posts, accounts, fetched_at, stale


# ================================================================ Threads
def _threads_lsd_and_userid(username: str):
    """Read the LSD token from the Threads profile page and the user id from Instagram."""
    lsd = None
    try:
        _, body = http_get("https://www.threads.com/@" + quote(username), timeout=12)
        m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', body.decode("utf-8", "ignore"))
        lsd = m.group(1) if m else None
    except Exception:
        pass
    user_id = None
    try:
        info = http_json(
            "https://www.instagram.com/api/v1/users/web_profile_info/?username=" + quote(username),
            headers={"x-ig-app-id": IG_APP_ID}, timeout=12)
        user_id = (info.get("data") or {}).get("user", {}).get("id")
    except Exception:
        pass
    return lsd, user_id


# The profile-tab GraphQL doc_id rotates frequently. Known candidates are tried
# in order; discover_threads_doc_id() scrapes a current one from the threads.com
# JS bundles and tries it first when found.
THREADS_DOC_IDS = [
    "25073444226023094", "7451607104958938", "23996318550159868",
    "9925907010825989", "26286467210919721",
]
_threads_doc = {"id": None, "checked": 0}
_threads_doc_lock = threading.Lock()
THREADS_DOC_TTL = 86400  # re-discover at most once a day
_DOC_ID_RE = re.compile(
    r'id:\s*"(\d{10,20})"[\s\S]{0,300}?name:\s*"BarcelonaProfileThreadsTabQuery"')
_DOC_ID_RE_REV = re.compile(
    r'name:\s*"BarcelonaProfileThreadsTabQuery"[\s\S]{0,300}?id:\s*"(\d{10,20})"')


def find_doc_id_in_bundle(text: str):
    m = _DOC_ID_RE.search(text) or _DOC_ID_RE_REV.search(text)
    return m.group(1) if m else None


def discover_threads_doc_id():
    """Best effort: pull a current BarcelonaProfileThreadsTabQuery doc_id out of
    the JS bundles referenced by a threads.com profile page. Cached for a day."""
    now = time.time()
    with _threads_doc_lock:
        if now - _threads_doc["checked"] < THREADS_DOC_TTL:
            return _threads_doc["id"]
        _threads_doc["checked"] = now
    found = None
    try:
        _, body = http_get("https://www.threads.com/@meta", timeout=12)
        html = body.decode("utf-8", "ignore")
        bundles = re.findall(
            r'"(https://static\.cdninstagram\.com/rsrc\.php/[^"]+?\.js[^"]*)"', html)
        seen = set()
        for url in bundles[:40]:
            if url in seen:
                continue
            seen.add(url)
            try:
                _, js = http_get(url, timeout=10)
            except Exception:
                continue
            text = js.decode("utf-8", "ignore")
            if "BarcelonaProfileThreadsTabQuery" not in text:
                continue
            found = find_doc_id_in_bundle(text)
            if found:
                break
    except Exception:
        pass
    with _threads_doc_lock:
        _threads_doc["id"] = found
    return found


def fetch_threads_posts(username: str):
    lsd, user_id = _threads_lsd_and_userid(username)
    if not lsd or not user_id:
        return []
    headers = {
        "X-FB-LSD": lsd, "X-IG-App-ID": IG_APP_ID_THREADS,
        "Sec-Fetch-Site": "same-origin",
        "X-FB-Friendly-Name": "BarcelonaProfileThreadsTabQuery",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    doc_ids = list(THREADS_DOC_IDS)
    discovered = discover_threads_doc_id()
    if discovered and discovered not in doc_ids:
        doc_ids.insert(0, discovered)
    for doc_id in doc_ids:
        payload = urlencode({
            "lsd": lsd, "doc_id": doc_id,
            "variables": json.dumps({
                "userID": str(user_id),
                "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": False}),
        }).encode()
        req = urllib.request.Request("https://www.threads.com/api/graphql", data=payload)
        req.add_header("User-Agent", UA)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            continue
        if data.get("errors"):
            continue
        posts = _parse_threads(data, username)
        if posts:
            return posts
    return []


def _parse_threads(data, username):
    posts = []

    def walk(o):
        if isinstance(o, dict):
            if "post" in o and isinstance(o["post"], dict) and o["post"].get("caption") is not None:
                p = o["post"]
                caption = (p.get("caption") or {}).get("text", "") if isinstance(p.get("caption"), dict) else ""
                info = p.get("text_post_app_info", {}) or {}
                imgs = (p.get("image_versions2") or {}).get("candidates") or []
                posts.append({
                    "account": username,
                    "text": caption[:280],
                    "likes": p.get("like_count") or 0,
                    "replies": info.get("direct_reply_count") or 0,
                    "reposts": info.get("repost_count") or 0,
                    "views": 0,
                    "media": imgs[0]["url"] if imgs else "",
                    "url": "https://www.threads.com/@%s/post/%s" % (username, p.get("code", "")),
                    "id": p.get("code", ""),
                    "createdAt": p.get("taken_at") or 0,
                })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return posts


def get_threads_posts(force: bool):
    accounts = load_accounts(THREADS_ACCOUNTS_FILE, DEFAULT_THREADS_ACCOUNTS)

    def fetch():
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = pool.map(
                lambda a: with_backoff(("threads", a), lambda: fetch_threads_posts(a)), accounts)
        posts = [p for chunk in results for p in chunk]
        record_snapshot("threads", posts)
        attach_history("threads", posts, metric="likes")
        return posts
    posts, fetched_at, stale = cached(("threads", tuple(accounts)), force, fetch)
    return posts, accounts, fetched_at, stale


# ================================================================ TikTok
def _tiktok_item(v):
    author = v.get("author", {}) if isinstance(v.get("author"), dict) else {}
    handle = author.get("unique_id", "")
    vid = v.get("video_id", "")
    return {
        "account": handle,
        "name": author.get("nickname", handle),
        "title": (v.get("title") or "").strip() or "(no caption)",
        "views": v.get("play_count") or 0,
        "likes": v.get("digg_count") or 0,
        "comments": v.get("comment_count") or 0,
        "shares": v.get("share_count") or 0,
        "thumbnail": v.get("cover") or v.get("origin_cover") or "",
        "url": "https://www.tiktok.com/@%s/video/%s" % (handle, vid),
        "id": vid,
        "createdAt": v.get("create_time") or 0,
    }


def fetch_tiktok_user(handle: str):
    url = "%s/user/posts?unique_id=%s&count=12" % (TIKWM_BASE, quote(handle))
    try:
        d = http_json(url, timeout=15)
    except Exception:
        return []
    vids = (d.get("data") or {}).get("videos") or []
    return [_tiktok_item(v) for v in vids]


def fetch_tiktok_trending(region: str):
    url = "%s/feed/list?region=%s&count=20" % (TIKWM_BASE, region)
    try:
        d = http_json(url, timeout=15)
    except Exception:
        return []
    vids = d.get("data") or []
    return [_tiktok_item(v) for v in vids]


def get_tiktok(force: bool):
    accounts = load_accounts(TIKTOK_ACCOUNTS_FILE, DEFAULT_TIKTOK_ACCOUNTS)
    code = current_region()
    region = REGIONS[code].get("tiktok", code)  # tikwm needs a real country code

    def fetch():
        # Merge the trending feed with subscribed accounts' latest videos, deduped.
        # tikwm's free tier rate-limits aggressive concurrency, so keep it low.
        posts = with_backoff(("tiktok", "_trending"), lambda: fetch_tiktok_trending(region))
        with ThreadPoolExecutor(max_workers=3) as pool:
            for chunk in pool.map(
                    lambda a: with_backoff(("tiktok", a), lambda: fetch_tiktok_user(a)), accounts):
                posts.extend(chunk)
        seen, unique = set(), []
        for p in posts:
            if p["id"] and p["id"] not in seen:
                seen.add(p["id"])
                unique.append(p)
        record_snapshot("tiktok", unique)
        attach_history("tiktok", unique)
        return unique
    posts, fetched_at, stale = cached(("tiktok", tuple(accounts), region), force, fetch)
    return posts, accounts, fetched_at, stale


# ================================================================ AI videos tab
def fetch_news():
    def one(feed):
        label, url = feed
        try:
            _, body = http_get(url, timeout=12)
            root = ET.fromstring(body)
        except Exception:
            return []
        items = []
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            source = item.findtext("source") or ""
            pub = item.findtext("pubDate") or ""
            try:
                ts = email.utils.parsedate_to_datetime(pub).timestamp()
            except (TypeError, ValueError):
                ts = 0
            items.append({"region": label, "title": title, "source": source,
                          "link": item.findtext("link") or "", "ts": ts})
        return items[:25]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = pool.map(one, NEWS_FEEDS)
    merged = [n for chunk in results for n in chunk]
    seen, unique = set(), []
    for n in merged:
        if n["title"] not in seen:
            seen.add(n["title"])
            unique.append(n)
    unique.sort(key=lambda n: n["ts"], reverse=True)
    return unique[:40]


def fetch_hf_models():
    def one(args):
        pipeline, sort = args
        url = ("https://huggingface.co/api/models?pipeline_tag=%s&sort=%s"
               "&direction=-1&limit=12" % (pipeline, sort))
        try:
            data = http_json(url, timeout=12)
        except Exception:
            return []
        return [{"id": m.get("id", ""), "likes": m.get("likes", 0),
                 "downloads": m.get("downloads", 0), "pipeline": pipeline,
                 "createdAt": m.get("createdAt", "")} for m in data]

    jobs = [(p, s) for p in HF_PIPELINES for s in ("createdAt", "trendingScore")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, jobs))

    def dedupe(lists):
        seen, out = set(), []
        for chunk in lists:
            for m in chunk:
                if m["id"] not in seen:
                    seen.add(m["id"])
                    out.append(m)
        return out
    latest = dedupe(results[0::2])
    latest.sort(key=lambda m: m["createdAt"], reverse=True)
    trending = dedupe(results[1::2])
    return {"latest": latest[:12], "trending": trending[:12]}


def get_ai_data(force: bool):
    # The AI tab serves text content (models + news); popular AI videos live
    # under the YouTube tab's "AI" category.
    def fetch():
        with ThreadPoolExecutor(max_workers=2) as pool:
            news_f = pool.submit(fetch_news)
            models_f = pool.submit(fetch_hf_models)
            return {"news": news_f.result(), "models": models_f.result()}

    def is_good(d):
        return bool(d.get("news") or d.get("models", {}).get("latest")
                    or d.get("models", {}).get("trending"))
    return cached(("ai",), force, fetch, is_good)


# ================================================================ Favorites
def load_favorites():
    try:
        with open(FAVORITES_FILE) as f:
            items = json.load(f)
            if isinstance(items, list):
                return items
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_favorites(items):
    with open(FAVORITES_FILE, "w") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def update_favorites(action, item):
    url = (item or {}).get("url", "")
    if not url:
        return load_favorites()
    with _fav_lock:
        items = load_favorites()
        items = [x for x in items if x.get("url") != url]
        if action == "add":
            clean = {k: item.get(k) for k in FAV_FIELDS if item.get(k) is not None}
            clean["savedAt"] = int(time.time())
            items.insert(0, clean)
        save_favorites(items)
    return items


# ================================================================ status / digest / scheduler
def compact(n):
    n = n or 0
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return ("%.1f" % (n / div)).rstrip("0").rstrip(".") + suffix
    return str(int(n))


def get_status():
    now = time.time()
    with _fail_lock:
        cooldowns = [
            {"source": k[0], "account": k[1], "failures": f["count"], "retryAt": f["until"]}
            for k, f in _fail.items()
            if f["count"] >= FAIL_LIMIT and f["until"] > now
        ]
    with _status_lock:
        sources = {k: dict(v) for k, v in _status.items()}
    return {"sources": sources, "cooldowns": cooldowns,
            "scheduler": dict(_scheduler), "region": current_region(), "now": now}


def settings_payload():
    return {"region": current_region(),
            "regions": [{"code": c, "label": r["label"]} for c, r in REGIONS.items()]}


def build_digest():
    """Markdown digest of the current top items per platform (from caches)."""
    lines = ["# Daily Trend Digest — " + time.strftime("%Y-%m-%d"), ""]

    def section(title, items, metric, limit=10):
        if not items:
            return
        lines.append("## " + title)
        lines.append("")
        unit = "likes" if metric == "likes" else "views"
        for i, it in enumerate(items[:limit], 1):
            name = (it.get("title") or it.get("text") or "(untitled)")
            name = " ".join(name.split()).replace("[", "(").replace("]", ")")[:100]
            who = it.get("account") or it.get("channel") or ""
            d = it.get("delta") or 0
            delta = " · ▲ %s today" % compact(d) if d > 0 else (
                " · ▼ %s today" % compact(-d) if d < 0 else "")
            lines.append("%d. [%s](%s) — %s · %s %s%s"
                         % (i, name, it.get("url", ""), who,
                            compact(it.get(metric) or 0), unit, delta))
        lines.append("")

    videos, _, _ = get_videos("All", "week", False, False)
    videos = [dict(v, url="https://www.youtube.com/watch?v=" + v["id"]) for v in videos]
    section("▶ YouTube", videos, "views")
    shorts, _, _ = get_videos("All", "week", True, False)
    shorts = [dict(v, url="https://www.youtube.com/watch?v=" + v["id"]) for v in shorts]
    section("⚡ Shorts", shorts, "views")
    reels, _, _, _ = get_reels(False)
    section("📸 Reels", reels, "views")
    tiktok, _, _, _ = get_tiktok(False)
    section("🎵 TikTok", sorted(tiktok, key=lambda p: p.get("views") or 0, reverse=True), "views")
    x_posts, _, _, _ = get_x_posts(False)
    section("𝕏 Twitter", sorted(x_posts, key=lambda p: p.get("likes") or 0, reverse=True), "likes")
    threads, _, _, _ = get_threads_posts(False)
    section("🧵 Threads", sorted(threads, key=lambda p: p.get("likes") or 0, reverse=True), "likes")

    if len(lines) <= 2:
        lines.append("_No data cached yet — open a few tabs or wait for the background refresh._")
    return "\n".join(lines)


# Background scheduler: keeps caches warm and — more importantly — keeps the
# daily trend-history snapshots accumulating even when no tab is open.
_scheduler = {"enabled": False, "interval": REFRESH_INTERVAL, "lastRun": 0, "nextRun": 0}


def refresh_all():
    tasks = (
        lambda: get_videos("All", "week", False, False),
        lambda: get_videos("All", "week", True, False),
        lambda: get_reels(False),
        lambda: get_x_posts(False),
        lambda: get_threads_posts(False),
        lambda: get_tiktok(False),
        lambda: get_ai_data(False),
    )
    for task in tasks:
        try:
            task()
        except Exception:
            pass


def _scheduler_loop():
    while True:
        _scheduler["lastRun"] = time.time()
        _scheduler["nextRun"] = _scheduler["lastRun"] + REFRESH_INTERVAL
        refresh_all()
        time.sleep(max(60, _scheduler["nextRun"] - time.time()))


def start_scheduler():
    if REFRESH_INTERVAL <= 0:
        return False
    _scheduler["enabled"] = True
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    return True


# ================================================================ misc
def fetch_oembed(url: str):
    """oEmbed metadata for TikTok/YouTube URLs (CORS-bypass proxy)."""
    host = urlparse(url).netloc.lower()
    if "tiktok.com" in host:
        endpoint = "https://www.tiktok.com/oembed?url=" + quote(url, safe="")
    elif "youtube.com" in host or "youtu.be" in host:
        endpoint = "https://www.youtube.com/oembed?format=json&url=" + quote(url, safe="")
    else:
        return {"ok": False, "reason": "unsupported"}
    try:
        data = http_json(endpoint, timeout=10)
        return {"ok": True, "title": data.get("title", ""),
                "author": data.get("author_name", ""),
                "thumbnail": data.get("thumbnail_url", "")}
    except Exception:
        return {"ok": False, "reason": "fetch_failed"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        force = qs.get("force", ["0"])[0] == "1"

        if parsed.path in ("/", "/index.html"):
            with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return

        if parsed.path == "/favicon.ico":
            self._send(200, FAVICON_SVG, "image/svg+xml")
            return

        if parsed.path == "/api/videos":
            category = qs.get("category", ["All"])[0]
            period = qs.get("period", ["week"])[0]
            shorts = qs.get("shorts", ["0"])[0] == "1"
            enrich = qs.get("enrich", ["0"])[0] == "1"
            query = qs.get("q", [""])[0].strip()
            if not query and category not in ("All", "AI") and category not in CATEGORIES:
                self._send(400, {"error": "unknown category"})
                return
            videos, fetched_at, stale = get_videos(category, period, shorts, force, enrich, query)
            self._send(200, {"videos": videos[:60], "fetchedAt": fetched_at, "stale": stale})
            return

        if parsed.path == "/api/categories":
            self._send(200, {"categories": ["All", "AI"] + list(CATEGORIES.keys())})
            return

        if parsed.path == "/api/reels":
            reels, accounts, fetched_at, stale = get_reels(force)
            self._send(200, {"reels": reels[:80], "accounts": accounts,
                             "fetchedAt": fetched_at, "stale": stale})
            return

        if parsed.path == "/api/x":
            posts, accounts, fetched_at, stale = get_x_posts(force)
            self._send(200, {"posts": posts, "accounts": accounts,
                             "fetchedAt": fetched_at, "stale": stale})
            return

        if parsed.path == "/api/threads":
            posts, accounts, fetched_at, stale = get_threads_posts(force)
            self._send(200, {"posts": posts, "accounts": accounts,
                             "fetchedAt": fetched_at, "stale": stale})
            return

        if parsed.path == "/api/tiktok":
            posts, accounts, fetched_at, stale = get_tiktok(force)
            self._send(200, {"posts": posts[:100], "accounts": accounts,
                             "fetchedAt": fetched_at, "stale": stale})
            return

        if parsed.path == "/api/ai":
            data, fetched_at, stale = get_ai_data(force)
            self._send(200, {**data, "fetchedAt": fetched_at, "stale": stale})
            return

        if parsed.path == "/api/favorites":
            self._send(200, {"items": load_favorites()})
            return

        if parsed.path == "/api/status":
            self._send(200, get_status())
            return

        if parsed.path == "/api/settings":
            self._send(200, settings_payload())
            return

        if parsed.path == "/api/digest":
            self._send(200, build_digest().encode(), "text/markdown; charset=utf-8")
            return

        if parsed.path == "/api/oembed":
            self._send(200, fetch_oembed(qs.get("url", [""])[0]))
            return

        if parsed.path == "/api/img":
            # Fetch hotlink-blocked thumbnails (Instagram/TikTok CDNs) server-side.
            url = qs.get("u", [""])[0]
            host = urlparse(url).netloc.lower()
            if not url.startswith("https://") or not host.endswith(IMG_PROXY_ALLOW):
                self._send(400, {"error": "host not allowed"})
                return
            hit = _img_cache.get(url)
            if hit:
                self._send(200, hit[1], hit[0])
                return
            try:
                ctype, body = http_get(url, timeout=12)
                ctype = ctype or "image/jpeg"
                with _img_lock:
                    if len(_img_cache) > IMG_CACHE_MAX:
                        _img_cache.clear()
                    _img_cache[url] = (ctype, body)
                self._send(200, body, ctype)
            except Exception:
                self._send(502, {"error": "fetch failed"})
            return

        self._send(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length).decode()) if length else {}
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return

        # /api/{reels|x|threads|tiktok}/accounts — add/remove subscribed accounts
        m = re.match(r"^/api/(reels|x|threads|tiktok)/accounts$", parsed.path)
        if m:
            source = m.group(1)
            path, defaults = ACCOUNT_SOURCES[source]
            action = req.get("action")
            raw = (req.get("username") or "").strip().lstrip("@")
            # X is case-preserving; Instagram/Threads/TikTok handles are lowercase
            username = raw if source == "x" else raw.lower()
            accounts = load_accounts(path, defaults)
            if action == "add" and username and username not in accounts:
                accounts.append(username)
            elif action == "remove" and username in accounts:
                accounts.remove(username)
            save_accounts(path, accounts)
            self._send(200, {"accounts": accounts})
            return

        if parsed.path == "/api/favorites":
            action = req.get("action")
            if action not in ("add", "remove"):
                self._send(400, {"error": "unknown action"})
                return
            items = update_favorites(action, req.get("item") or {})
            self._send(200, {"items": items})
            return

        if parsed.path == "/api/settings":
            if "region" in req:
                set_region(req.get("region"))
            self._send(200, settings_payload())
            return

        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    if start_scheduler():
        print(f"Background refresh every {REFRESH_INTERVAL}s (REFRESH_INTERVAL=0 to disable)")
    print(f"Trend Viewer running at http://localhost:{PORT}")
    server.serve_forever()
