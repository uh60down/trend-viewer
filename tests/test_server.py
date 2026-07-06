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


class TestSearchParams(unittest.TestCase):
    def test_roundtrip_protobuf(self):
        raw = base64.urlsafe_b64decode(server.build_search_params("week", shorts=False))
        self.assertEqual(raw, bytes([0x08, 0x03, 0x12, 0x04, 0x08, 0x03, 0x10, 0x01]))

    def test_shorts_adds_length_filter(self):
        raw = base64.urlsafe_b64decode(server.build_search_params("day", shorts=True))
        self.assertEqual(raw, bytes([0x08, 0x03, 0x12, 0x06, 0x08, 0x02, 0x10, 0x01, 0x18, 0x01]))


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
