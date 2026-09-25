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
        self.original = (main.DATA_DIR, main.CONFIG_PATH, main.PROGRESS_PATH)
        main.DATA_DIR = Path(self.directory.name)
        main.CONFIG_PATH = main.DATA_DIR / "config.json"
        main.PROGRESS_PATH = main.DATA_DIR / "reading-progress.json"
        main.write_config({"session_secret": "test-session-secret", "password_hash": "configured"})

    def tearDown(self):
        main.DATA_DIR, main.CONFIG_PATH, main.PROGRESS_PATH = self.original
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


if __name__ == "__main__":
    unittest.main()
