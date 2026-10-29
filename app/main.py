from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import bleach
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


BASE_URL = "https://substack.com"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_PATH = DATA_DIR / "config.json"
CACHE_PATH = DATA_DIR / "sync-cache.json"
PROGRESS_PATH = DATA_DIR / "reading-progress.json"
MANUAL_ARTICLES_PATH = DATA_DIR / "manual-articles.json"
SESSION_COOKIE = "reader_session"
MAX_ITEMS = 60

ALLOWED_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "div",
    "em",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
    "img": ["src", "alt", "title"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_config(config: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n")
    temporary.replace(CONFIG_PATH)


def read_sync_cache() -> dict[str, Any]:
    try:
        return json.loads(CACHE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_sync_cache(cache: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(cache, ensure_ascii=False))
    temporary.replace(CACHE_PATH)


def read_progress() -> dict[str, Any]:
    try:
        value = json.loads(PROGRESS_PATH.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def read_manual_articles() -> dict[str, dict[str, Any]]:
    try:
        articles = json.loads(MANUAL_ARTICLES_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(articles, dict):
        return {}
    return {
        str(identifier): article
        for identifier, article in articles.items()
        if isinstance(article, dict) and str(article.get("id")) == str(identifier)
    }


def write_progress(progress: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = PROGRESS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(progress, separators=(",", ":")))
    temporary.replace(PROGRESS_PATH)


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        candidate = password_hash(password, bytes.fromhex(salt_hex)).split("$", 2)[2]
        return hmac.compare_digest(candidate, digest_hex)
    except (ValueError, TypeError):
        return False


def session_token(secret: str) -> str:
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), timestamp.encode(), hashlib.sha256).hexdigest()
    return f"{timestamp}.{signature}"


def valid_session(token: str | None, secret: str | None) -> bool:
    if not token or not secret:
        return False
    try:
        timestamp, signature = token.split(".", 1)
        if abs(int(time.time()) - int(timestamp)) > 60 * 60 * 24 * 30:
            return False
        expected = hmac.new(secret.encode(), timestamp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def is_logged_in(request: Request) -> bool:
    config = read_config()
    return valid_session(request.cookies.get(SESSION_COOKIE), config.get("session_secret"))


def page(title: str, body: str, *, authenticated: bool = False, reader_mode: bool = False, status_code: int = 200) -> HTMLResponse:
    nav = ""
    if authenticated and not reader_mode:
        nav = """
        <nav>
          <a href='/reader'>All</a>
          <a href='/saved'>Saved</a>
          <a href='/setup'>Setup</a>
          <form method='post' action='/logout'><button type='submit'>Log out</button></form>
        </nav>
        """
    reader_assets = "<link rel='stylesheet' href='/static/reader.css?v=2'>" if reader_mode else ""
    reader_class = " class='reader-page'" if reader_mode else ""
    document = f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{html.escape(title)} · Substack reader</title>
  {reader_assets}
  <style>
    :root {{ color-scheme: dark; font-family: Georgia, 'Times New Roman', serif; }}
    html {{ background: #141414; }}
    body {{ margin: 0 auto; max-width: 52rem; padding: 1.1rem 1rem 4rem; color: #eceae5; background: #141414; font-size: 1.1rem; line-height: 1.62; }}
    a {{ color: #c6d8ff; }}
    nav {{ display: flex; flex-wrap: wrap; align-items: center; gap: .55rem; border-bottom: 1px solid #505050; padding-bottom: .8rem; margin-bottom: 1.35rem; font-family: sans-serif; font-size: .95rem; }}
    nav a, button, .button {{ display: inline-block; border: 1px solid #666; border-radius: .25rem; padding: .35rem .65rem; color: #eceae5; background: #292d30; text-decoration: none; }}
    nav a:first-child {{ font-weight: bold; }}
    nav form {{ margin: 0; }}
    h1, h2, h3 {{ line-height: 1.2; }}
    h1 {{ font-size: 1.85rem; }}
    h2 {{ font-size: 1.4rem; }}
    h3 {{ font-size: 1.15rem; margin-bottom: .25rem; }}
    .muted {{ color: #b9b6ae; font-family: sans-serif; font-size: .9rem; }}
    .item {{ border-bottom: 1px solid #505050; padding: .85rem 0 1rem; }}
    .item a {{ color: #eceae5; text-decoration: none; }}
    .item a:hover, .item a:focus {{ text-decoration: underline; }}
    .subtitle {{ margin: .2rem 0 0; }}
    label {{ display: block; margin: .8rem 0 .25rem; font-family: sans-serif; font-size: .95rem; }}
    input {{ box-sizing: border-box; width: 100%; max-width: 30rem; padding: .6rem; border: 1px solid #777; border-radius: .25rem; color: #eceae5; background: #202326; font: inherit; }}
    input:focus, button:focus, nav a:focus, .button:focus {{ outline: 2px solid #a8c6ff; outline-offset: 2px; }}
    button {{ font: inherit; cursor: pointer; }}
    button:disabled {{ color: #888; cursor: default; }}
    form.stack {{ max-width: 34rem; margin: 1rem 0 2rem; }}
    .notice {{ border-left: .35rem solid #777; padding: .45rem .75rem; background: #202326; }}
    article {{ max-width: 46rem; font-size: calc(1.1rem + 4px); }}
    article img {{ display: block; max-width: 100%; height: auto; margin: 1rem auto; }}
    article figure {{ margin: 1rem 0; }}
    article figcaption {{ color: #b9b6ae; font-family: sans-serif; font-size: .9rem; }}
    article pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #202326; padding: .7rem; }}
    article blockquote {{ margin-left: .5rem; padding-left: 1rem; border-left: .25rem solid #666; }}
    article a {{ overflow-wrap: anywhere; }}
    .back {{ font-family: sans-serif; font-size: .95rem; }}
  </style>
</head>
<body{reader_class}>{nav}{body}</body>
</html>"""
    return HTMLResponse(document, status_code=status_code)


def redirect_to(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def auth_redirect(request: Request) -> RedirectResponse | None:
    config = read_config()
    if not config.get("password_hash"):
        return redirect_to("/claim")
    if not is_logged_in(request):
        return redirect_to("/login")
    return None


def clean_article_html(raw_html: str | None, original_url: str | None = None) -> str:
    if not raw_html:
        return "<p>No article body was returned by Substack.</p>"
    source = BeautifulSoup(raw_html, "html.parser")
    for tag in source.find_all(["script", "style", "form", "input", "button"]):
        tag.decompose()
    for tag in source.find_all(["iframe", "video", "audio", "object", "embed"]):
        if not tag.parent:
            continue
        replacement = source.new_tag("p")
        replacement.string = "This article contains media. "
        if original_url and original_url.startswith(("https://", "http://")):
            link = source.new_tag("a", href=original_url)
            link.string = "Open it in the original article."
            replacement.append(link)
        tag.replace_with(replacement)
    cleaned = bleach.clean(
        str(source),
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=["http", "https", "mailto"],
        strip=True,
        strip_comments=True,
    )
    soup = BeautifulSoup(cleaned, "html.parser")
    for tag in soup.find_all(["script", "style", "iframe", "object", "embed", "form", "input", "button"]):
        tag.decompose()
    for image in soup.find_all("img"):
        source = image.get("src", "")
        if source.startswith("//"):
            image["src"] = "https:" + source
        elif source and not source.startswith(("https://", "http://")):
            image["src"] = urljoin(original_url or BASE_URL, source)
        image["referrerpolicy"] = "no-referrer"
    for link in soup.find_all("a"):
        if link.get("href", "").startswith("/"):
            link["href"] = BASE_URL + link["href"]
        link["rel"] = "noreferrer"
    return str(soup)


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def result_payload(value: Any) -> dict[str, Any]:
    value = as_dict(value)
    result = value.get("result")
    return as_dict(result) if result is not None else value


def post_id(value: Any) -> str | None:
    data = as_dict(value)
    for key in ("post_id", "postId", "id"):
        candidate = data.get(key)
        if candidate is not None and str(candidate).isdigit():
            return str(candidate)
    post = as_dict(data.get("post"))
    candidate = post.get("id")
    return str(candidate) if candidate is not None else None


def normalize_post(post: dict[str, Any], inbox_item: dict[str, Any] | None = None) -> dict[str, Any]:
    inbox_item = inbox_item or {}
    publication = as_dict(post.get("publication"))
    publication_name = (
        publication.get("name")
        or post.get("publication_name")
        or inbox_item.get("publication_name")
        or "Substack"
    )
    title = post.get("title") or inbox_item.get("title") or "Untitled"
    subtitle = post.get("subtitle") or post.get("description") or inbox_item.get("subtitle") or ""
    date_value = post.get("post_date") or post.get("postDate") or inbox_item.get("content_date")
    bylines = (
        post.get("publishedBylines")
        or post.get("published_bylines")
        or inbox_item.get("published_bylines")
        or inbox_item.get("authors")
        or []
    )
    if isinstance(bylines, list):
        author = ", ".join(
            str(as_dict(byline).get("name") or as_dict(byline).get("author_name"))
            for byline in bylines
            if as_dict(byline).get("name") or as_dict(byline).get("author_name")
        )
    elif isinstance(bylines, dict):
        author = str(bylines.get("name") or bylines.get("author_name") or "")
    else:
        author = str(bylines or "")
    return {
        "id": post_id(post) or post_id(inbox_item),
        "title": str(title),
        "subtitle": str(subtitle),
        "publication": str(publication_name),
        "date": str(date_value or ""),
        "url": post.get("canonical_url") or post.get("canonicalUrl") or post.get("url") or "",
        "author": author,
        "body_html": post.get("body_html") or post.get("bodyHtml") or "",
    }


def is_locked_paid_preview(post: dict[str, Any], inbox_item: dict[str, Any] | None = None) -> bool:
    post = as_dict(post)
    inbox_item = as_dict(inbox_item)
    nested_inbox_item = as_dict(post.get("inboxItem"))
    audiences = {
        str(value).lower()
        for value in (
            post.get("audience"),
            inbox_item.get("audience"),
            nested_inbox_item.get("audience"),
        )
        if value is not None
    }
    if "only_paid" not in audiences:
        return False
    preview_flags = (
        post.get("is_preview"),
        inbox_item.get("is_preview"),
        nested_inbox_item.get("is_preview"),
        post.get("free_unlock_required"),
        inbox_item.get("free_unlock_required"),
    )
    return any(value is True or value == 1 or value == "true" for value in preview_flags)


def normalize_feed(payload: Any) -> tuple[list[dict[str, Any]], str | None]:
    result = result_payload(payload)
    raw_posts = result.get("posts") or []
    if isinstance(raw_posts, dict):
        raw_posts = list(raw_posts.values())
    posts_by_id = {post_id(item): as_dict(item) for item in raw_posts if post_id(item)}
    raw_items = result.get("inboxItems") or result.get("inbox_items") or []
    if isinstance(raw_items, dict):
        raw_items = list(raw_items.values())
    saved_items = result.get("items") or []
    if isinstance(saved_items, dict):
        saved_items = list(saved_items.values())
    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        item_data = as_dict(item)
        identifier = post_id(item_data)
        post = posts_by_id.get(identifier, item_data)
        if is_locked_paid_preview(post, item_data):
            continue
        normalized_post = normalize_post(post, item_data)
        if normalized_post["id"]:
            normalized.append(normalized_post)
    for item in saved_items:
        item_data = as_dict(item)
        post = as_dict(item_data.get("post")) or item_data
        if is_locked_paid_preview(post, item_data):
            continue
        normalized_post = normalize_post(post, item_data)
        if normalized_post["id"]:
            normalized.append(normalized_post)
    if not normalized:
        for item in raw_posts:
            item_data = as_dict(item)
            if is_locked_paid_preview(item_data):
                continue
            normalized_post = normalize_post(item_data)
            if normalized_post["id"]:
                normalized.append(normalized_post)
    next_cursor = result.get("cursor") or result.get("nextCursor") or result.get("next_cursor")
    return normalized, str(next_cursor) if next_cursor else None


def normalize_article_payload(payload: Any) -> dict[str, Any]:
    result = result_payload(payload)
    post = as_dict(result.get("post")) or as_dict(payload.get("post"))
    if not post and result.get("id"):
        post = result
    return normalize_post(post)


def sync_bookmarklet(app_origin: str, sync_token: str) -> str:
    app_literal = json.dumps(app_origin, separators=(",", ":"))
    token_literal = json.dumps(sync_token, separators=(",", ":"))
    script = (
        "javascript:(()=>{const A="
        + app_literal
        + ",T="
        + token_literal
        + ",S='https://substack.com',J=async u=>{const r=await fetch(u,{credentials:'include'});if(!r.ok)throw Error('Substack '+new URL(u).pathname+' HTTP '+r.status);return r.json()},R=x=>x&&x.result||x||{},P=async(p,b,k,c,a)=>{let o=Object.fromEntries(k.map(z=>[z,[]])),q={...b};for(let n=0;n<25;n++){const x=R(await J(S+p+'?'+new URLSearchParams(q)));for(const z of k){const v=x[z];if(Array.isArray(v))o[z].push(...v)}const m=x[c];if(!m||x.more===false||n===24)break;q.cursor=m;if(a){const v=x[k[k.length-1]];if(Array.isArray(v)&&v.length&&v[v.length-1].content_date)q[a]=v[v.length-1].content_date}}return o},L=x=>{const p=x?.post??x,i=x?.inboxItem??p?.inboxItem??{},a=p?.audience??x?.audience??i?.audience,v=p?.is_preview??x?.is_preview??i?.is_preview,f=p?.free_unlock_required??x?.free_unlock_required;return a==='only_paid'&&(v===true||f===true||i?.is_preview===true)},V=x=>{const a=[];for(const k of ['inboxItems','inbox_items','posts','items']){const v=x[k];if(Array.isArray(v))a.push(...v.filter(x=>!L(x)));else if(v&&typeof v==='object')a.push(...Object.values(v).filter(x=>!L(x)))}return a},I=x=>String(x?.post_id??x?.postId??x?.id??x?.post?.id??'');(async()=>{"
        "try{const[i,s]=await Promise.all([P('/api/v1/inbox/top',{inboxType:'inbox',surface:'inbox_all',limit:'20'},['posts','inboxItems'],'cursor','after'),P('/api/v1/reader/saved',{filter:'all'},['items'],'nextCursor','')]);"
        "let r=await fetch(A+'/api/sync/feed',{method:'POST',headers:{'Content-Type':'application/json','X-Sync-Token':T},body:JSON.stringify({inbox:i,saved:s})});if(!r.ok)throw Error('Reader HTTP '+r.status);"
        "const ids=[...new Set([...V(R(i)),...V(R(s))].map(I).filter(Boolean))];let done=0;"
        "for(let n=0;n<ids.length;n+=4){const out=await Promise.all(ids.slice(n,n+4).map(async id=>{try{return[id,await J(S+'/api/v1/posts/by-id/'+encodeURIComponent(id)+'?allowMeteredUnlock=true')]}catch(e){return null}}));const a=Object.fromEntries(out.filter(Boolean));"
        "if(Object.keys(a).length){r=await fetch(A+'/api/sync/articles',{method:'POST',headers:{'Content-Type':'application/json','X-Sync-Token':T},body:JSON.stringify({articles:a})});if(!r.ok)throw Error('Article upload HTTP '+r.status);done+=Object.keys(a).length}}"
        "alert('Sync complete: '+done+' article bodies transferred. Return to the Kindle reader and reload.')}catch(e){alert('Sync failed: '+e.message)}})()})()"
    )
    return script


class SubstackClient:
    def __init__(self) -> None:
        self.playwright = None
        self.context: BrowserContext | None = None
        self.login_page: Page | None = None
        self.lock = asyncio.Lock()
        self.sync_lock = asyncio.Lock()

    async def ensure_context(self) -> BrowserContext:
        if self.context:
            return self.context
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(DATA_DIR / "browser"),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        return self.context

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        self.context = None
        self.playwright = None

    async def request_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        context = await self.ensure_context()
        response = await context.request.get(BASE_URL + path, params=params or {})
        try:
            payload = await response.json()
        except Exception:
            payload = {"errors": [{"msg": f"Substack returned HTTP {response.status}"}]}
        if not isinstance(payload, dict):
            return {"result": payload}
        return payload

    async def status(self) -> dict[str, Any]:
        cache = read_sync_cache()
        updated_at = cache.get("updated_at")
        sections = as_dict(cache.get("sections"))
        if updated_at and (isinstance(sections.get("inbox"), list) or isinstance(sections.get("saved"), list)):
            return {
                "signed_in": True,
                "message": f"Last desktop sync: {updated_at}",
            }
        return {"signed_in": False, "message": "No desktop sync has been completed yet."}

    async def store_feed(self, inbox_payload: Any, saved_payload: Any) -> None:
        inbox, _ = normalize_feed(inbox_payload)
        saved, _ = normalize_feed(saved_payload)
        async with self.sync_lock:
            cache = read_sync_cache()
            cache["sections"] = {"inbox": inbox, "saved": saved}
            cache.setdefault("articles", {})
            cache["updated_at"] = now_iso()
            write_sync_cache(cache)

    async def store_articles(self, articles: Any) -> int:
        if not isinstance(articles, dict):
            raise ValueError("The sync payload did not contain an article map.")
        stored = 0
        async with self.sync_lock:
            cache = read_sync_cache()
            article_cache = cache.setdefault("articles", {})
            for identifier, payload in articles.items():
                normalized = normalize_article_payload(payload)
                if not normalized["id"]:
                    normalized["id"] = str(identifier)
                if normalized["id"]:
                    article_cache[str(normalized["id"])] = normalized
                    stored += 1
            cache["updated_at"] = now_iso()
            write_sync_cache(cache)
        return stored

    async def _status_unlocked(self) -> dict[str, Any]:
        payload = await self.request_json(
            "/api/v1/reader/posts",
            {"inboxType": "inbox", "limit": "1"},
        )
        errors = payload.get("errors") or []
        signed_in = not any("sign in" in str(as_dict(error).get("msg", "")).lower() for error in errors)
        return {"signed_in": signed_in, "message": as_dict(errors[0]).get("msg", "") if errors else ""}

    async def start_login(self, email: str) -> str:
        async with self.lock:
            context = await self.ensure_context()
            if self.login_page and not self.login_page.is_closed():
                await self.login_page.close()
            page = await context.new_page()
            self.login_page = page
            page.set_default_timeout(15000)
            page.set_default_navigation_timeout(15000)
            try:
                await page.goto(BASE_URL + "/sign-in", wait_until="commit", timeout=15000)
            except PlaywrightTimeoutError:
                # Substack can keep background navigation open after the sign-in
                # document has committed. The form can still be used safely.
                pass
            field = page.locator("input[name='email'], input[type='email']").first
            try:
                await field.wait_for(state="visible", timeout=15000)
            except PlaywrightTimeoutError:
                await page.evaluate("window.stop()")
                await field.wait_for(state="visible", timeout=5000)
            await field.fill(email, timeout=10000)
            await page.get_by_role("button", name=re.compile("continue", re.I)).click(
                timeout=15000,
                no_wait_after=True,
            )
            try:
                await page.locator(
                    "input[autocomplete='one-time-code'], input[inputmode='numeric'], input[type='tel']"
                ).first.wait_for(state="visible", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            return (await page.locator("body").inner_text(timeout=5000))[-1200:]

    async def finish_login(self, code: str) -> dict[str, Any]:
        async with self.lock:
            if not self.login_page or self.login_page.is_closed():
                return {"ok": False, "message": "Start the Substack sign-in flow first."}
            page = self.login_page
            page.set_default_timeout(15000)
            page.set_default_navigation_timeout(15000)
            digits = re.sub(r"\D", "", code)
            one_time = page.locator(
                "input[autocomplete='one-time-code'], input[inputmode='numeric'], input[type='tel']"
            ).first
            try:
                await one_time.wait_for(state="visible", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            if await one_time.count() and await one_time.is_visible():
                await one_time.fill(digits or code, timeout=10000)
            else:
                candidates = page.locator("input[type='text'], input[type='tel'], input[inputmode='numeric']")
                count = await candidates.count()
                if count >= len(digits) >= 4:
                    for index, digit in enumerate(digits):
                        await candidates.nth(index).fill(digit)
                elif count:
                    await candidates.first.fill(code)
                else:
                    return {"ok": False, "message": "Could not find Substack's verification-code field."}
            buttons = page.get_by_role("button", name=re.compile("continue|verify|sign in", re.I))
            if await buttons.count():
                await buttons.last.click(timeout=15000, no_wait_after=True)
            await asyncio.sleep(1)
            state = await self._status_unlocked()
            return {"ok": state["signed_in"], "message": state["message"] or ("Signed in." if state["signed_in"] else "Substack did not accept the code yet.")}

    async def list_posts(self, inbox_type: str, cursor: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        if cursor:
            return [], None
        cache = read_sync_cache()
        sections = as_dict(cache.get("sections"))
        items = sections.get(inbox_type)
        items = items if isinstance(items, list) else []
        if inbox_type == "saved":
            manual = read_manual_articles()
            added = sorted(manual.values(), key=lambda item: item.get("date", ""), reverse=True)
            items = added + [item for item in items if str(item.get("id")) not in manual]
        return items, None

    async def article(self, identifier: str) -> dict[str, Any]:
        manual = read_manual_articles().get(str(identifier))
        if manual:
            return manual
        cache = read_sync_cache()
        articles = as_dict(cache.get("articles"))
        cached = as_dict(articles.get(str(identifier)))
        if cached:
            return cached
        sections = as_dict(cache.get("sections"))
        for section in (sections.get("inbox"), sections.get("saved")):
            if isinstance(section, list):
                for item in section:
                    if str(as_dict(item).get("id")) == str(identifier) and as_dict(item).get("body_html"):
                        return as_dict(item)
        raise RuntimeError("This article has not been transferred yet. Run the desktop Substack sync again.")


client = SubstackClient()


@asynccontextmanager
async def lifespan(_: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    yield
    await client.close()


app = FastAPI(title="Substack for Kindle", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
progress_lock = asyncio.Lock()


SYNC_ORIGINS = {"https://substack.com", "https://www.substack.com"}


def ensure_sync_token() -> str:
    config = read_config()
    token = config.get("sync_token")
    if not token:
        token = secrets.token_urlsafe(32)
        config["sync_token"] = token
        write_config(config)
    return str(token)


def sync_headers(request: Request) -> dict[str, str]:
    origin = request.headers.get("origin", "")
    headers = {
        "Vary": "Origin",
        "Access-Control-Allow-Headers": "Content-Type, X-Sync-Token",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
    }
    if origin in SYNC_ORIGINS:
        headers["Access-Control-Allow-Origin"] = origin
    return headers


def sync_token_matches(request: Request) -> bool:
    expected = read_config().get("sync_token")
    supplied = request.headers.get("x-sync-token", "")
    return bool(expected and supplied and hmac.compare_digest(str(expected), supplied))


@app.options("/api/sync/{kind}")
async def sync_preflight(request: Request, kind: str) -> Response:
    return Response(status_code=204, headers=sync_headers(request))


@app.post("/api/sync/{kind}")
async def sync_push(request: Request, kind: str) -> Response:
    headers = sync_headers(request)
    if request.headers.get("origin") not in SYNC_ORIGINS:
        return JSONResponse({"error": "Sync requests must originate from Substack."}, status_code=403, headers=headers)
    if not sync_token_matches(request):
        return JSONResponse({"error": "Invalid sync token."}, status_code=403, headers=headers)
    try:
        payload = await request.json()
        if kind == "feed":
            await client.store_feed(payload.get("inbox"), payload.get("saved"))
            result = {"ok": True, "kind": "feed"}
        elif kind == "articles":
            result = {"ok": True, "kind": "articles", "stored": await client.store_articles(payload.get("articles"))}
        else:
            return JSONResponse({"error": "Unknown sync payload."}, status_code=404, headers=headers)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400, headers=headers)
    return JSONResponse(result, headers=headers)


@app.get("/healthz")
async def healthz() -> HTMLResponse:
    return HTMLResponse("ok")


@app.get("/", response_class=HTMLResponse)
async def root(request: Request) -> RedirectResponse:
    if not read_config().get("password_hash"):
        return redirect_to("/claim")
    return redirect_to("/reader" if is_logged_in(request) else "/login")


@app.get("/claim", response_class=HTMLResponse)
async def claim_page() -> Response:
    if read_config().get("password_hash"):
        return redirect_to("/login")
    body = """
    <h1>Set up your private reader</h1>
    <p>This is the one-time local password for the reader. Use something you can type on a Kindle.</p>
    <form class='stack' method='post' action='/claim'>
      <label for='password'>Reader password</label>
      <input id='password' name='password' type='password' minlength='8' required autofocus>
      <label for='confirm'>Repeat password</label>
      <input id='confirm' name='confirm' type='password' minlength='8' required>
      <p><button type='submit'>Save password</button></p>
    </form>
    """
    return page("Set up", body)


@app.post("/claim", response_class=HTMLResponse)
async def claim(password: str = Form(...), confirm: str = Form(...)) -> Response:
    if read_config().get("password_hash"):
        return redirect_to("/login")
    if len(password) < 8 or password != confirm:
        return page("Set up", "<h1>Passwords do not match</h1><p><a href='/claim'>Try again.</a></p>", status_code=400)
    config = {
        "password_hash": password_hash(password),
        "session_secret": secrets.token_urlsafe(32),
        "sync_token": secrets.token_urlsafe(32),
        "created_at": now_iso(),
    }
    write_config(config)
    response = redirect_to("/setup")
    response.set_cookie(SESSION_COOKIE, session_token(config["session_secret"]), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> Response:
    if not read_config().get("password_hash"):
        return redirect_to("/claim")
    body = """
    <h1>Substack reader</h1>
    <p>Enter the reader password to continue.</p>
    <form class='stack' method='post' action='/login'>
      <label for='password'>Password</label>
      <input id='password' name='password' type='password' required autofocus>
      <p><button type='submit'>Open reader</button></p>
    </form>
    """
    return page("Log in", body)


@app.post("/login", response_class=HTMLResponse)
async def login(password: str = Form(...)) -> Response:
    config = read_config()
    if not config.get("password_hash"):
        return redirect_to("/claim")
    if not password_matches(password, config["password_hash"]):
        return page("Log in", "<h1>That password was not accepted</h1><p><a href='/login'>Try again.</a></p>", status_code=401)
    response = redirect_to("/reader")
    response.set_cookie(SESSION_COOKIE, session_token(config["session_secret"]), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return response


@app.post("/logout")
async def logout() -> RedirectResponse:
    response = redirect_to("/login")
    response.delete_cookie(SESSION_COOKIE)
    return response


def render_items(items: list[dict[str, Any]], section_name: str, next_cursor: str | None, inbox_type: str) -> str:
    chunks = [f"<h1>{html.escape(section_name)}</h1>"]
    if not items:
        chunks.append("<p>No articles were returned. Use Setup to check the Substack session.</p>")
    for item in items:
        identifier = quote(str(item["id"]), safe="")
        date = html.escape(item.get("date", "")[:10])
        publication = html.escape(item.get("publication", "Substack"))
        author = html.escape(item.get("author", ""))
        subtitle = html.escape(item.get("subtitle", ""))
        metadata = " · ".join(part for part in (author, publication, date) if part)
        article_path = f"/article/{identifier}?from=saved" if inbox_type == "saved" else f"/article/{identifier}"
        chunks.append(
            f"<section class='item'><h2><a href='{article_path}'>{html.escape(item.get('title', 'Untitled'))}</a></h2>"
            f"<div class='muted'>{metadata}</div>"
            f"<p class='subtitle'>{subtitle}</p></section>"
        )
    if next_cursor:
        chunks.append(f"<p><a class='button' href='?cursor={quote(next_cursor, safe='')}'>Older articles</a></p>")
    return "".join(chunks)


async def section(request: Request, inbox_type: str, section_name: str) -> Response:
    redirect = auth_redirect(request)
    if redirect:
        return redirect
    cursor = request.query_params.get("cursor")
    try:
        items, next_cursor = await client.list_posts(inbox_type, cursor)
    except Exception as error:
        body = f"<h1>{html.escape(section_name)}</h1><p class='notice'>Could not load Substack: {html.escape(str(error))}</p>"
        return page(section_name, body, authenticated=True, status_code=502)
    return page(section_name, render_items(items, section_name, next_cursor, inbox_type), authenticated=True)


@app.get("/reader", response_class=HTMLResponse)
async def reader(request: Request) -> Response:
    return await section(request, "inbox", "All subscriptions")


@app.get("/saved", response_class=HTMLResponse)
async def saved(request: Request) -> Response:
    return await section(request, "saved", "Saved articles")


@app.get("/article/{identifier}", response_class=HTMLResponse)
async def article(request: Request, identifier: str) -> Response:
    redirect = auth_redirect(request)
    if redirect:
        return redirect
    try:
        item = await client.article(identifier)
    except Exception as error:
        return page("Article", f"<p class='notice'>Could not load this article: {html.escape(str(error))}</p>", authenticated=True, status_code=502)
    title = html.escape(item.get("title", "Untitled"))
    publication = html.escape(item.get("publication", "Substack"))
    author = html.escape(item.get("author", ""))
    date = html.escape(item.get("date", "")[:10])
    original = item.get("url")
    source_link = f"<a href='{html.escape(original, quote=True)}'>Original</a>" if original and original.startswith(("https://", "http://")) else ""
    back_path = "/saved" if request.query_params.get("from") == "saved" else "/reader"
    back_label = "Saved" if back_path == "/saved" else "All"
    metadata = " · ".join(part for part in (author, publication, date) if part)
    saved = html.escape(json.dumps(read_progress().get(identifier, {}), separators=(",", ":")), quote=True)
    body = f"""
    <div id='reader-shell' data-article-id='{html.escape(identifier, quote=True)}' data-progress='{saved}'>
      <header id='reader-topbar'>
        <a href='{back_path}' class='reader-back'>← {back_label}</a>
        <span class='reader-top-title'>{title}</span>
        {source_link}
      </header>
      <div id='reader-frame'>
        <div id='reader-viewport'>
          <article id='reader-content'>
            <h1>{title}</h1><p class='muted reader-byline'>{metadata}</p>
            {clean_article_html(item.get('body_html'), original)}
          </article>
        </div>
      </div>
      <footer id='reader-footer'>
        <button id='reader-prev' type='button' aria-label='Previous page'>←</button>
        <span id='reader-status' aria-live='polite'>Scroll to read</span>
        <button id='reader-next' type='button' aria-label='Next page'>→</button>
      </footer>
    </div>
    <script src='/static/reader.js?v=1'></script>
    """
    return page(item.get("title", "Article"), body, reader_mode=True)


@app.post("/api/progress/{identifier}")
async def save_reading_progress(request: Request, identifier: str, page_number: int = Form(...), total: int = Form(...), width: int = Form(...), height: int = Form(...)) -> Response:
    if not is_logged_in(request):
        return JSONResponse({"error": "Sign in again to save reading progress."}, status_code=401)
    if request.headers.get("x-reader-progress") != "1":
        return JSONResponse({"error": "Invalid progress request."}, status_code=403)
    if not identifier.isdigit() or len(identifier) > 30 or not (0 <= page_number < total <= 10000) or not (100 <= width <= 5000 and 100 <= height <= 5000):
        return JSONResponse({"error": "Invalid reading position."}, status_code=400)
    record = {"page": page_number, "total": total, "width": width, "height": height, "updated_at": now_iso()}
    async with progress_lock:
        progress = read_progress()
        progress[identifier] = record
        write_progress(progress)
    return JSONResponse(record, headers={"Cache-Control": "no-store"})


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, message: str = "") -> Response:
    redirect = auth_redirect(request)
    if redirect:
        return redirect
    state = await client.status()
    cache = read_sync_cache()
    sections = as_dict(cache.get("sections"))
    updated_at = cache.get("updated_at")
    if updated_at:
        status = f"ready; last sync {html.escape(str(updated_at))}"
        counts = f"{len(sections.get('inbox', []))} All articles and {len(sections.get('saved', []))} Saved articles"
    else:
        status = "waiting for the first desktop sync"
        counts = "No articles have been synced yet"
    app_origin = str(request.base_url).rstrip("/")
    if app_origin.startswith("http://"):
        app_origin = "https://" + app_origin[len("http://") :]
    bookmarklet = html.escape(sync_bookmarklet(app_origin, ensure_sync_token()), quote=True)
    notice = f"<p class='notice'>{html.escape(message)}</p>" if message else ""
    body = f"""
    <h1>Setup</h1>
    {notice}
    <p>Sync status: <strong>{status}</strong>.</p>
    <p class='muted'>{counts}. The reader stores the synced copy in its private persistent storage.</p>
    <h2>Sync from your normal desktop browser</h2>
    <p>Substack blocks hosted headless browsers with a security check, so sign in to Substack normally on your desktop and use this one-click bookmarklet there.</p>
    <ol>
      <li>Drag the link below to your desktop browser's bookmarks bar.</li>
      <li>Open Substack while signed in, then click the bookmark.</li>
      <li>Wait for the sync confirmation, then reload this reader.</li>
    </ol>
    <p><a class='button' href='{bookmarklet}'>Sync Substack to Kindle reader</a></p>
    <p class='muted'>The bookmarklet reads only your All and Saved feeds and their article bodies in your signed-in browser, then sends them to this reader over HTTPS.</p>
    """
    return page("Setup", body, authenticated=True)


@app.post("/setup/substack/start", response_class=HTMLResponse)
async def start_substack_login(request: Request, email: str = Form(...)) -> Response:
    redirect = auth_redirect(request)
    if redirect:
        return redirect
    message = "Use the desktop sync bookmarklet above. Substack blocks sign-in from hosted headless browsers."
    return redirect_to("/setup?message=" + quote(message))


@app.post("/setup/substack/verify", response_class=HTMLResponse)
async def finish_substack_login(request: Request, code: str = Form(...)) -> Response:
    redirect = auth_redirect(request)
    if redirect:
        return redirect
    message = "Use the desktop sync bookmarklet above. Substack blocks sign-in from hosted headless browsers."
    return redirect_to("/setup?message=" + quote(message))
