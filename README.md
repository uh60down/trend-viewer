# Daily Trend Viewer

A local dashboard for checking what's trending — YouTube videos & Shorts,
Instagram Reels, X posts, Threads, TikTok, and AI-video model/news feeds —
ranked by views/likes, per category and time period.

No API keys, no external packages: the server uses only the Python standard
library and calls the platforms' public unauthenticated endpoints.

## Run

```bash
python3 server.py
```

Then open **http://localhost:8778**.

Optional configuration via environment variables:

| Variable           | Default | Meaning                                            |
|--------------------|---------|----------------------------------------------------|
| `PORT`             | `8778`  | HTTP port                                          |
| `REGION`           | `US`    | Initial region until one is picked in the UI       |
| `CACHE_TTL`        | `3600`  | Cache lifetime in seconds                          |
| `REFRESH_INTERVAL` | `3600`  | Background refresh in seconds (0 = off)            |
| `YT_HL` / `YT_GL`  | unset   | Advanced: override the region's YouTube locale     |

**Region** is normally picked in the UI: the 🌐 selector in the header drives
YouTube search language/country, the TikTok trending feed, and localized
category search terms (Korean, Japanese, and Traditional Chinese queries ship
built in). The choice is saved to `settings.json` next to the server, so the
background scheduler follows it too.

For real regions, YouTube results are fetched **relevance-ranked** (which
YouTube regionalizes properly) and then sorted by view count locally — asking
YouTube for a raw global views sort floods every category with the largest
upload markets regardless of country settings. Pick **🌍 Global (most
viewed)** if you want that raw worldwide ranking anyway.

## Features

- **YouTube / Shorts tabs** — category chips (All · **AI** · Mukbang · Beauty ·
  Vlog · Comedy · Movies/TV · Tech · Education · Travel · Animals) × period
  (today / this week / this month), collapsible **sort menu (views / likes)**
  — like counts are enriched per video server-side — and a popup player.
- **🤖 AI Videos tab** — newly released and trending video-generation models
  (Hugging Face, live) plus AI-video news (Google News RSS). Popular AI
  *videos* live under the YouTube tab's "AI" category.
- **🔍 Search** — on the video tabs it searches YouTube by any keyword; on the
  Reels/X/Threads/TikTok tabs it live-filters the already-loaded items.
- **📸 Reels tab** — latest reels from subscribed accounts via Instagram's
  web-internal API (no auth), sortable by views / likes / comments and
  filterable by period. Accounts are managed in the UI
  (`reels_accounts.json`).
- **𝕏 Twitter tab** — recent tweets with engagement via Twitter's public
  embed (syndication) API, sortable by likes / replies / retweets / views
  (`x_accounts.json`).
- **🧵 Threads tab** — anonymous live lookup against Meta's GraphQL endpoint,
  including **automatic discovery of the rotating `doc_id`** from the
  threads.com JS bundles; falls back to account shortcuts when Meta blocks
  anonymous reads (`threads_accounts.json`).
- **🎵 TikTok tab** — live trending feed + subscribed accounts via the free
  tikwm API, with views / likes / comments and period filtering
  (`tiktok_accounts.json`).
- **⭐ Saved tab** — star any card on any tab to collect it in
  `favorites.json`.
- **📈 Trend history** — every successful fetch snapshots per-item metrics
  into a local SQLite file (`trends.db`); cards then show a **daily delta
  badge** (▲/▼ vs the previous day) and a mini sparkline once three days of
  data exist. A **background scheduler** (hourly by default) keeps snapshots
  accumulating even when no tab is open, and every sort menu has a
  **🔥 Hot (daily Δ)** option that ranks by growth instead of absolute size.
- **📥 Digest** — one click downloads (and copies) a Markdown summary of the
  current top 10 per platform, with links and daily deltas.
- **ⓘ Status panel** — per-source health at a glance: last successful fetch,
  item counts, stale/failing indicators, accounts in cooldown, and the
  scheduler's next run. No more guessing why a tab is empty.
- **Remembered UI** — the active tab, category, period, and every sort choice
  persist across restarts (browser `localStorage`).
- **Resilient caching** — results are cached (1 h by default) and a failed
  refresh **never blanks a tab**: the last good data is served with a
  "cached data" warning while the server retries in the background, and
  accounts that keep failing are cooled down for 10 minutes instead of being
  hammered.

## Notes on the data sources

- **YouTube** data comes from the internal search API (InnerTube) with a
  views-sorted filter; the public Trending feed was retired in 2025, so the
  rankings are search-based. Like counts are read from the structured
  `likeCountEntity` (locale-independent), with a text fallback.
- **Instagram Reels** uses the same `web_profile_info` endpoint the
  instagram.com web client calls (with its public app-id header). It returns
  each account's ~12 most recent posts; heavy use can be temporarily
  rate-limited, which the cache and backoff absorb. Thumbnails are served
  through the local image proxy (`/api/img`) to bypass CDN hotlink blocks.
- **X (Twitter)** uses the embed-widget syndication API
  (`syndication.twitter.com/srv/timeline-profile/...`), which includes
  likes/replies/retweets without auth. Impressions are absent for some posts;
  those are excluded when sorting by views.
- **Threads** answers GraphQL without login, but the profile-posts query's
  `doc_id` rotates. The server tries a freshly discovered id first (scraped
  daily from the public JS bundles), then known fallbacks; if all fail it
  shows account shortcuts until Meta allows access again.
- **TikTok**'s own APIs require request signing (X-Bogus/msToken) and TLS
  fingerprinting, so the free **tikwm** API is used instead — it proxies the
  signing and returns views/likes/comments/thumbnails/video URLs. Its free
  tier throttles heavy concurrency, so the server fetches with low
  parallelism and caches for an hour.

## Development

```bash
python3 -m unittest discover -s tests -v   # parser & caching tests
node tests/smoke.mjs                       # browser smoke test (needs playwright + a running server)
```

CI (GitHub Actions) runs the unit tests plus a headless-browser smoke test —
every tab is clicked and any page/console error fails the build — on every
push.

Files created at runtime — `*_accounts.json`, `favorites.json`, `trends.db` —
are personal state and gitignored.
