import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from app import main


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.original = (main.DATA_DIR, main.CONFIG_PATH, main.PROGRESS_PATH, main.CACHE_PATH, main.MANUAL_ARTICLES_PATH)
        main.DATA_DIR = Path(self.directory.name)
        main.CONFIG_PATH = main.DATA_DIR / "config.json"
        main.PROGRESS_PATH = main.DATA_DIR / "reading-progress.json"
        main.CACHE_PATH = main.DATA_DIR / "sync-cache.json"
        main.MANUAL_ARTICLES_PATH = main.DATA_DIR / "manual-articles.json"
        main.write_config({"session_secret": "test-session-secret", "password_hash": "configured"})

    def tearDown(self):
        main.DATA_DIR, main.CONFIG_PATH, main.PROGRESS_PATH, main.CACHE_PATH, main.MANUAL_ARTICLES_PATH = self.original
        self.directory.cleanup()

    def request(self, authorized=True, progress_header=True):
        headers = []
        if authorized:
            token = main.session_token("test-session-secret")
            headers.append((b"cookie", (main.SESSION_COOKIE + "=" + token).encode()))
        if progress_header:
            headers.append((b"x-reader-progress", b"1"))
        return Request({"type": "http", "method": "POST", "path": "/api/progress/123",
                        "query_string": b"", "headers": headers})

    def test_progress_is_saved_separately_for_each_article(self):
        first = asyncio.run(main.save_reading_progress(self.request(), "123", 3, 12, 560, 710))
        second = asyncio.run(main.save_reading_progress(self.request(), "456", 1, 5, 560, 710))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        saved = main.read_progress()
        self.assertEqual(saved["123"]["page"], 3)
        self.assertEqual(saved["456"]["page"], 1)
        self.assertEqual(json.loads(first.body)["total"], 12)

    def test_progress_requires_session_and_same_origin_header(self):
        unauthenticated = asyncio.run(main.save_reading_progress(self.request(authorized=False), "123", 0, 1, 560, 710))
        missing_header = asyncio.run(main.save_reading_progress(self.request(progress_header=False), "123", 0, 1, 560, 710))
        invalid = asyncio.run(main.save_reading_progress(self.request(), "123", 9, 4, 560, 710))
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(missing_header.status_code, 403)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(main.read_progress(), {})

    def test_article_media_stays_readable_and_safe(self):
        cleaned = main.clean_article_html(
            "<script>alert('bad')</script><p>Hello</p><img src='/picture.jpg' alt='Map'>"
            "<iframe src='https://example.org/embed'></iframe>",
            "https://sample.substack.com/p/a-story",
        )
        self.assertIn("<p>Hello</p>", cleaned)
        self.assertIn("https://sample.substack.com/picture.jpg", cleaned)
        self.assertIn("Open it in the original article.", cleaned)
        self.assertNotIn("alert", cleaned)
        self.assertNotIn("iframe", cleaned)

    def test_manual_saved_article_survives_repeated_syncs_and_renders(self):
        manual = {"id": "external-example", "title": "A manually saved article",
                  "author": "Example Author", "publication": "example.org",
                  "date": "2026-10-28T21:00:00-05:00", "url": "https://example.org/article",
                  "body_html": "<p>Complete article body.</p>"}
        main.MANUAL_ARTICLES_PATH.write_text(json.dumps({manual["id"]: manual}))

        async def scenario():
            client = main.SubstackClient()
            for identifier in (101, 202):
                post = {"id": identifier, "title": "Synced article"}
                await client.store_feed({"posts": [post]}, {"items": [post]})
                saved, _ = await client.list_posts("saved")
                self.assertEqual([item["id"] for item in saved], [manual["id"], str(identifier)])
                inbox, _ = await client.list_posts("inbox")
                self.assertEqual([item["id"] for item in inbox], [str(identifier)])
            await client.store_feed({}, {})
            saved, _ = await client.list_posts("saved")
            self.assertEqual(saved, [manual])
            self.assertEqual(await client.article(manual["id"]), manual)
            # Exercise the actual authenticated handlers using only fixture credentials.
            listing = await main.saved(self.request())
            rendered = await main.article(self.request(), manual["id"])
            self.assertEqual(listing.status_code, 200)
            self.assertIn(b"/article/external-example?from=saved", listing.body)
            self.assertIn(b"2026-10-28", listing.body)
            self.assertEqual(rendered.status_code, 200)
            self.assertIn(b"Complete article body.", rendered.body)
            self.assertIn(b"reader-next", rendered.body)
        asyncio.run(scenario())

    def test_manual_article_is_deduplicated_and_overrides_stale_body(self):
        manual = {"id": "123", "title": "Manual", "date": "2026-10-28",
                  "body_html": "<p>Full body</p>"}
        main.MANUAL_ARTICLES_PATH.write_text(json.dumps({"123": manual}))
        main.write_sync_cache({"sections": {"saved": [{"id": "123", "title": "Stale"}]},
                               "articles": {"123": {"id": "123", "body_html": "Stale"}}})
        client = main.SubstackClient()
        self.assertEqual(asyncio.run(client.list_posts("saved"))[0], [manual])
        self.assertEqual(asyncio.run(client.article("123")), manual)

    def test_newer_synced_article_sorts_above_manual_article_after_sync(self):
        manual = {"id": "external-example", "title": "Added October 28",
                  "date": "2026-10-28T21:00:00-05:00", "body_html": "<p>Full body</p>"}
        main.MANUAL_ARTICLES_PATH.write_text(json.dumps({manual["id"]: manual}))

        async def scenario():
            client = main.SubstackClient()
            newer = {"id": 29, "title": "October 29 article", "post_date": "2026-10-29T12:00:00Z"}
            older = {"id": 27, "title": "October 27 article", "post_date": "2026-10-27T12:00:00Z"}
            for _ in range(2):
                await client.store_feed({}, {"items": [older, newer]})
                saved, _ = await client.list_posts("saved")
                self.assertEqual([item["id"] for item in saved], ["29", manual["id"], "27"])
                self.assertEqual(saved[1]["date"], manual["date"])
                self.assertEqual((await client.article(manual["id"]))["body_html"], manual["body_html"])
        asyncio.run(scenario())

    def test_normal_saved_feed_works_without_manual_articles(self):
        item = {"id": "123", "title": "Normal saved article"}
        main.write_sync_cache({"sections": {"saved": [item]}})
        client = main.SubstackClient()
        self.assertEqual(asyncio.run(client.list_posts("saved"))[0], [item])
        main.MANUAL_ARTICLES_PATH.write_text("[]")
        self.assertEqual(asyncio.run(client.list_posts("saved"))[0], [item])


if __name__ == "__main__":
    unittest.main()
