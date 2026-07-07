"""Unit tests for Trend Viewer's parsers and caching logic.

The app is mostly fragile parsing of undocumented upstream payloads, so these
tests pin the expected shapes with small recorded-style fixtures — if an
upstream format drifts and a parser is adjusted, the fixtures document what
the old shape looked like.

Run:  python3 -m unittest discover -s tests -v
"""
import base64
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402


class TestPeriodFilter(unittest.TestCase):
    def test_today_excludes_older(self):
        self.assertTrue(server.within_period("3 hours ago", "day"))
        self.assertFalse(server.within_period("1 day ago", "day"))
        self.assertFalse(server.within_period("2 weeks ago", "day"))

    def test_week_allows_days(self):
        self.assertTrue(server.within_period("6 days ago", "week"))
        self.assertFalse(server.within_period("2 weeks ago", "week"))
        self.assertFalse(server.within_period("1 month ago", "week"))

    def test_month_allows_weeks(self):
        self.assertTrue(server.within_period("3 weeks ago", "month"))
        self.assertFalse(server.within_period("2 months ago", "month"))
        self.assertFalse(server.within_period("1 year ago", "month"))

    def test_missing_text_passes(self):
        self.assertTrue(server.within_period("", "day"))

    def test_localized_words(self):
        self.assertTrue(server.within_period("3시간 전", "day", "ko"))
        self.assertFalse(server.within_period("2일 전", "day", "ko"))
        self.assertFalse(server.within_period("3주 전", "week", "ko"))
        self.assertTrue(server.within_period("6日前", "week", "ja"))
        self.assertFalse(server.within_period("2週間前", "week", "ja"))
        self.assertTrue(server.within_period("vor 6 Tagen", "week", "de"))
        self.assertFalse(server.within_period("vor 2 Wochen", "week", "de"))
        self.assertFalse(server.within_period("il y a 2 ans", "month", "fr"))
        self.assertTrue(server.within_period("il y a 5 heures", "day", "fr"))
        # unshipped language falls back to English words (passes through)
        self.assertTrue(server.within_period("hace 2 semanas", "week", "es"))

    def test_every_localized_hl_has_period_words(self):
        for code, r in server.REGIONS.items():
            if r["hl"] != "en":
                self.assertIn(r["hl"], server.PERIOD_EXCLUDE,
                              f"region {code}: hl {r['hl']} missing PERIOD_EXCLUDE words")
                self.assertIn(r["hl"], server.CATEGORY_QUERIES_L10N,
                              f"region {code}: hl {r['hl']} missing localized queries")


class TestRegion(unittest.TestCase):
    def setUp(self):
        server.SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "test_settings.json")
        if os.path.exists(server.SETTINGS_FILE):
            os.remove(server.SETTINGS_FILE)

    def tearDown(self):
        if os.path.exists(server.SETTINGS_FILE):
            os.remove(server.SETTINGS_FILE)

    def test_default_region(self):
        self.assertEqual(server.current_region(), server.DEFAULT_REGION)

    def test_set_region_persists(self):
        self.assertEqual(server.set_region("KR"), "KR")
        self.assertEqual(server.current_region(), "KR")
        self.assertEqual(server.load_settings()["region"], "KR")

    def test_invalid_region_rejected(self):
        server.set_region("KR")
        self.assertEqual(server.set_region("XX"), "KR")
        self.assertEqual(server.current_region(), "KR")

    def test_locale_follows_region(self):
        server.set_region("JP")
        old_hl, old_gl = os.environ.pop("YT_HL", None), os.environ.pop("YT_GL", None)
        try:
            self.assertEqual(server.yt_locale(), ("ja", "JP"))
        finally:
            if old_hl: os.environ["YT_HL"] = old_hl
            if old_gl: os.environ["YT_GL"] = old_gl

    def test_category_query_localization(self):
        self.assertEqual(server.category_query("Mukbang", "ko"), "먹방")
        self.assertEqual(server.category_query("Mukbang", "en"), "mukbang")
        self.assertEqual(server.category_query("Tech", "de"), "technik test")
        # a label missing from an override table falls back to English
        self.assertEqual(server.category_query("Mukbang", "de"), "mukbang")

    def test_settings_payload_shape(self):
        p = server.settings_payload()
        self.assertIn("region", p)
        codes = [r["code"] for r in p["regions"]]
        self.assertIn("US", codes)
        self.assertIn("KR", codes)


class TestSearchParams(unittest.TestCase):
    def test_relevance_sort_is_default(self):
        # No sort field: relevance ranking (regionalized by gl), filters only
        raw = base64.urlsafe_b64decode(server.build_search_params("week", shorts=False))
        self.assertEqual(raw, bytes([0x12, 0x04, 0x08, 0x03, 0x10, 0x01]))

    def test_views_sort_opt_in(self):
        raw = base64.urlsafe_b64decode(server.build_search_params("week", shorts=False, by_views=True))
        self.assertEqual(raw, bytes([0x08, 0x03, 0x12, 0x04, 0x08, 0x03, 0x10, 0x01]))

    def test_shorts_adds_length_filter(self):
        raw = base64.urlsafe_b64decode(server.build_search_params("day", shorts=True))
        self.assertEqual(raw, bytes([0x12, 0x06, 0x08, 0x02, 0x10, 0x01, 0x18, 0x01]))

    def test_global_region_uses_views_sort(self):
        self.assertTrue(server.REGIONS["GLOBAL"]["views_sort"])
        self.assertFalse(server.REGIONS["US"].get("views_sort", False))


class TestNumberParsing(unittest.TestCase):
    def test_view_count_text(self):
        self.assertEqual(server.parse_view_count("1,234,567 views"), 1234567)
        self.assertEqual(server.parse_view_count(""), 0)

    def test_compact_numbers(self):
        self.assertEqual(server.parse_compact_number("1.2M"), 1_200_000)
        self.assertEqual(server.parse_compact_number("12K"), 12_000)
        self.assertEqual(server.parse_compact_number("3.4B"), 3_400_000_000)
        self.assertEqual(server.parse_compact_number("1,234"), 1234)
        self.assertEqual(server.parse_compact_number("garbage"), 0)


class TestYouTubeParsers(unittest.TestCase):
    def test_extract_videos(self):
        fixture = {"contents": [{"videoRenderer": {
            "videoId": "abc123",
            "title": {"runs": [{"text": "Test "}, {"text": "video"}]},
            "ownerText": {"runs": [{"text": "Channel"}]},
            "viewCountText": {"simpleText": "1,234 views"},
            "lengthText": {"simpleText": "3:21"},
            "publishedTimeText": {"simpleText": "2 days ago"},
            "thumbnail": {"thumbnails": [{"url": "small"}, {"url": "large"}]},
        }}]}
        out = []
        server.extract_videos(fixture, out)
        self.assertEqual(len(out), 1)
        v = out[0]
        self.assertEqual(v["id"], "abc123")
        self.assertEqual(v["title"], "Test video")
        self.assertEqual(v["channel"], "Channel")
        self.assertEqual(v["views"], 1234)
        self.assertEqual(v["thumbnail"], "large")

    def test_like_count_from_entity(self):
        fixture = {"frameworkUpdates": {"mutations": [{"payload": {"likeCountEntity": {
            "likeCountIfIndifferent": {"content": "1.2K"},
            "expandedLikeCountIfIndifferent": {"content": "1,234"},
        }}}]}}
        self.assertEqual(server.parse_like_count(fixture), 1234)

    def test_like_count_fallback_string(self):
        fixture = {"accessibility": "liked along with 4,567 other people"}
        self.assertEqual(server.parse_like_count(fixture), 4568)

    def test_like_count_absent(self):
        self.assertEqual(server.parse_like_count({"nothing": []}), 0)


class TestXParser(unittest.TestCase):
    HTML = ('<html><script id="__NEXT_DATA__" type="application/json">%s</script></html>'
            % json.dumps({"props": {"pageProps": {"timeline": {"entries": [
                {"content": {"tweet": {
                    "id_str": "111", "full_text": "hello world",
                    "favorite_count": 10, "reply_count": 2, "retweet_count": 3,
                    "views": {"count": "500"},
                    "user": {"name": "Test User"},
                    "mediaDetails": [{"media_url_https": "https://pbs.twimg.com/x.jpg"}],
                    "created_at": "Mon Jul 06 00:00:00 +0000 2026",
                }}},
                {"content": {"other": {}}},
            ]}}}}))

    def test_parse_posts(self):
        posts = server.parse_x_html(self.HTML, "tester")
        self.assertEqual(len(posts), 1)
        p = posts[0]
        self.assertEqual(p["text"], "hello world")
        self.assertEqual(p["likes"], 10)
        self.assertEqual(p["views"], 500)
        self.assertEqual(p["media"], "https://pbs.twimg.com/x.jpg")
        self.assertEqual(p["url"], "https://x.com/tester/status/111")

    def test_no_next_data(self):
        self.assertEqual(server.parse_x_html("<html></html>", "tester"), [])


class TestThreadsParsers(unittest.TestCase):
    def test_parse_threads_posts(self):
        fixture = {"data": {"edges": [{"node": {"post": {
            "caption": {"text": "a thread post"},
            "like_count": 42,
            "text_post_app_info": {"direct_reply_count": 5, "repost_count": 7},
            "image_versions2": {"candidates": [{"url": "https://cdn/img.jpg"}]},
            "code": "XYZ",
            "taken_at": 1700000000,
        }}}]}}
        posts = server._parse_threads(fixture, "tester")
        self.assertEqual(len(posts), 1)
        p = posts[0]
        self.assertEqual(p["likes"], 42)
        self.assertEqual(p["replies"], 5)
        self.assertEqual(p["reposts"], 7)
        self.assertEqual(p["url"], "https://www.threads.com/@tester/post/XYZ")

    def test_doc_id_discovery_regex(self):
        bundle = ('x={kind:"PreloadableConcreteRequest",params:{id:"12345678901234567",'
                  'metadata:{},name:"BarcelonaProfileThreadsTabQuery"}}')
        self.assertEqual(server.find_doc_id_in_bundle(bundle), "12345678901234567")
        reverse = 'q={name:"BarcelonaProfileThreadsTabQuery",id:"76543210987654321"}'
        self.assertEqual(server.find_doc_id_in_bundle(reverse), "76543210987654321")
        self.assertIsNone(server.find_doc_id_in_bundle("no ids here"))


class TestStaleCache(unittest.TestCase):
    def setUp(self):
        server._cache.clear()

    def test_failure_keeps_last_good_data(self):
        key = ("test", "stale")
        data, _, stale = server.cached(key, False, lambda: ["good"])
        self.assertEqual(data, ["good"])
        self.assertFalse(stale)
        # A forced refresh that fails must serve the old data, marked stale
        data, _, stale = server.cached(key, True, lambda: [])
        self.assertEqual(data, ["good"])
        self.assertTrue(stale)
        # And an exception behaves the same
        def boom():
            raise RuntimeError("upstream down")
        data, _, stale = server.cached(key, True, boom)
        self.assertEqual(data, ["good"])
        self.assertTrue(stale)
        # A later successful refresh clears the stale flag
        data, _, stale = server.cached(key, True, lambda: ["fresh"])
        self.assertEqual(data, ["fresh"])
        self.assertFalse(stale)

    def test_first_failure_serves_empty_not_error(self):
        data, _, stale = server.cached(("test", "empty"), False, lambda: [])
        self.assertEqual(data, [])
        self.assertFalse(stale)

    def test_stale_entry_retries_sooner(self):
        key = ("test", "retry")
        server.cached(key, False, lambda: ["good"])
        server.cached(key, True, lambda: [])          # now stale
        entry = server._cache[key]
        entry["checked"] = time.time() - server.STALE_RETRY - 1
        calls = []
        server.cached(key, False, lambda: calls.append(1) or ["fresh"])
        self.assertEqual(calls, [1])  # refresh attempted before CACHE_TTL elapsed


class TestBackoff(unittest.TestCase):
    def setUp(self):
        server._fail.clear()

    def test_cooldown_after_repeated_failures(self):
        key = ("test", "acct")
        calls = []

        def failing():
            calls.append(1)
            return []
        for _ in range(server.FAIL_LIMIT):
            server.with_backoff(key, failing)
        self.assertEqual(len(calls), server.FAIL_LIMIT)
        server.with_backoff(key, failing)  # inside cooldown: skipped
        self.assertEqual(len(calls), server.FAIL_LIMIT)

    def test_success_resets(self):
        key = ("test", "acct2")
        server.with_backoff(key, lambda: [])
        self.assertIn(key, server._fail)
        server.with_backoff(key, lambda: ["ok"])
        self.assertNotIn(key, server._fail)


class TestCompactAndDigest(unittest.TestCase):
    def setUp(self):
        server._cache.clear()

    def tearDown(self):
        server._cache.clear()

    def test_compact(self):
        self.assertEqual(server.compact(2_400_000), "2.4M")
        self.assertEqual(server.compact(12_000), "12K")
        self.assertEqual(server.compact(1_500_000_000), "1.5B")
        self.assertEqual(server.compact(999), "999")
        self.assertEqual(server.compact(None), "0")

    def _seed(self, key, data):
        now = time.time()
        server._cache[key] = {"data": data, "fetched": now, "checked": now, "stale": False}

    def test_digest_uses_cached_data(self):
        region = server.current_region()
        self._seed(("yt", "All", "week", False, False, region),
                   [{"id": "v1", "title": "Big [viral] video", "channel": "Chan",
                     "views": 2_400_000, "delta": 350_000}])
        self._seed(("yt", "All", "week", True, False, region), [])
        for kind, accounts in (("reels", server.DEFAULT_IG_ACCOUNTS),
                               ("x", server.DEFAULT_X_ACCOUNTS),
                               ("threads", server.DEFAULT_THREADS_ACCOUNTS)):
            self._seed((kind, tuple(accounts)), [])
        tt_region = server.REGIONS[region].get("tiktok", region)
        self._seed(("tiktok", tuple(server.DEFAULT_TIKTOK_ACCOUNTS), tt_region), [])
        text = server.build_digest()
        self.assertIn("## ▶ YouTube", text)
        self.assertIn("Big (viral) video", text)          # brackets sanitized
        self.assertIn("https://www.youtube.com/watch?v=v1", text)
        self.assertIn("2.4M views", text)
        self.assertIn("▲ 350K today", text)
        self.assertNotIn("## 📸 Reels", text)             # empty sections skipped

    def test_status_reports_sources_and_cooldowns(self):
        server._status.clear()
        server._fail.clear()
        self._seed(("reels", ("a",)), [{"id": "r1"}])
        server.cached(("reels", ("a",)), False, lambda: None)  # cache hit, no status yet
        server.cached(("tiktok", ("b",)), False, lambda: [{"id": "t1"}])
        server._fail[("x", "someacct")] = {"count": server.FAIL_LIMIT,
                                           "until": time.time() + 100}
        s = server.get_status()
        self.assertIn("tiktok", s["sources"])
        self.assertEqual(s["sources"]["tiktok"]["items"], 1)
        self.assertEqual(len(s["cooldowns"]), 1)
        self.assertEqual(s["cooldowns"][0]["account"], "someacct")
        self.assertIn("scheduler", s)

    def test_source_of_key(self):
        self.assertEqual(server._source_of_key(("yt", "All", "week", True, False)), "shorts")
        self.assertEqual(server._source_of_key(("yt", "All", "week", False, False)), "youtube")
        self.assertEqual(server._source_of_key(("threads", ("a",))), "threads")


class TestHistory(unittest.TestCase):
    def setUp(self):
        server.DB_FILE = os.path.join(os.path.dirname(__file__), "test_trends.db")
        if os.path.exists(server.DB_FILE):
            os.remove(server.DB_FILE)

    def tearDown(self):
        if os.path.exists(server.DB_FILE):
            os.remove(server.DB_FILE)

    def test_delta_against_previous_day(self):
        yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        with server.closing(server._db()) as conn:
            conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                         ("youtube", "vid1", yesterday, 1000, 10, 1))
            conn.commit()
        items = [{"id": "vid1", "views": 1500, "likes": 20, "comments": 2}]
        server.record_snapshot("youtube", items)
        server.attach_history("youtube", items)
        self.assertEqual(items[0]["delta"], 500)

    def test_no_history_no_delta(self):
        items = [{"id": "new", "views": 100}]
        server.attach_history("youtube", items)
        self.assertNotIn("delta", items[0])


if __name__ == "__main__":
    unittest.main()
