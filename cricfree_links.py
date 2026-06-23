# -*- coding: utf-8 -*-
"""
cricfree_links.py
A minimal scraper for cricfree.sc that mimics the interface of
SportsurgeScraper (fetch homepage → list of events; get_embed_urls → ServerEntry).

NOTE: Because the host blocks direct access from this sandbox,
      you may need to adjust the CSS/regex selectors after testing
      on a machine that can reach the site.
"""

import re
import random
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# Re‑use the ServerEntry dataclass from the Sportsurge scraper
from sportsurge_links import ServerEntry

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
BASE_URL = "https://cricfree.sc/"          # change if the site uses a mirror
USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
]
TIMEOUT = 15  # seconds

# ----------------------------------------------------------------------
# Helper: a requests.Session with retry logic and a random UA
# ----------------------------------------------------------------------
def _make_session():
    sess = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


def _get(url: str, referer: str | None = None):
    """Perform a GET request with a random UA and optional Referer."""
    sess = _make_session()
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    # Many free‑sports sites have expired or self‑signed certs; disable verification.
    # **Only do this on a trusted network / VPS you control.**
    resp = sess.get(url, headers=headers, timeout=TIMEOUT, verify=False)
    resp.raise_for_status()
    return resp.text, sess


# ----------------------------------------------------------------------
# Scraper class
# ----------------------------------------------------------------------
class CricfreeScraper:
    """
    Scrapes cricfree.sc for live sports events and resolves the HLS stream.
    The public API mirrors SportsurgeScraper:
        - get_homepage_events() -> list[dict]  (title, category, status, url)
        - get_embed_urls(watch_url) -> list[ServerEntry]
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.BASE_URL = BASE_URL
    # ------------------------------------------------------------------
    # 1️⃣  Homepage → list of live events
    # ------------------------------------------------------------------
    def get_homepage_events(self):
        """
        Returns a list of dicts:
            {
                "title": str,          # e.g. "India vs Pakistan"
                "category": str,       # e.g. "Cricket"
                "status": str,         # usually "LIVE"
                "url": str             # absolute URL to the watch page
            }
        """
        html, _ = _get(self.BASE_URL)
        events = []

        soup = BeautifulSoup(html, "html.parser")

        # --- Primary attempt: look for blocks with class "match" or "event" ---
        blocks = soup.find_all(class_=re.compile(r"(match|event|game)", re.I))
        for block in blocks:
            # Title is often inside a <div class="teams"> or <span class="title">
            title_el = block.find(class_=re.compile(r"title|teams", re.I))
            title = title_el.get_text(strip=True) if title_el else "Unknown"

            # Category/sport may be in a preceding heading; we try to fetch the
            # closest preceding <h2>/<h3> that is not generic.
            sport = "Unknown"
            for prev in block.find_previous_siblings():
                if prev.name in ("h2", "h3"):
                    txt = prev.get_text(strip=True)
                    if txt and len(txt) < 30:  # sport names are short
                        sport = txt
                        break

            # Watch URL: many sites use a button with data‑url or an <a> with href
            watch_el = block.find("a", href=True) or block.find(
                "button", {"data-url": True}
            )
            if watch_el:
                href = watch_el.get("href") or watch_el.get("data-url")
                watch_url = urljoin(self.BASE_URL, href)
            else:
                continue

            events.append(
                {
                    "title": title,
                    "category": sport,
                    "status": "LIVE",  # cricfree only shows live events on the main page
                    "url": watch_url,
                }
            )

        # --- Fallback: simple regex search for /watch/ or /event/ links ---
        if not events:
            # Pattern: <a href="/watch/12345">... (sometimes with extra path)
            watch_links = re.findall(
                r'href=["\'](/watch/[^"\']+)[\'"]', html, re.I
            )
            for href in watch_links:
                watch_url = urljoin(self.BASE_URL, href)
                # Try to guess title from surrounding text (very rough)
                title_match = re.search(
                    rf'{re.escape(href)}[^>]*>([^<]+)<', html, re.I | re.S
                )
                title = title_match.group(1).strip() if title_match else "Unknown"
                events.append(
                    {
                        "title": title,
                        "category": "Unknown",
                        "status": "LIVE",
                        "url": watch_url,
                    }
                )

        if self.verbose:
            print(f"[Cricfree] Found {len(events)} live events")
        return events

    # ------------------------------------------------------------------
    # 2️⃣  Watch page → ServerEntry (resolve the HLS chain)
    # ------------------------------------------------------------------
    def get_embed_urls(self, watch_url: str):
        """
        Given a cricfree watch‑page URL, return a list containing a single
        ServerEntry (most cricfree pages expose only one stream per event).
        If anything fails, an empty list is returned.
        """
        try:
            html, sess = _get(watch_url, referer=self.BASE_URL)
        except Exception as e:  # pragma: no cover
            if self.verbose:
                print(f"[Cricfree] Failed to fetch watch page: {e}")
            return []

        soup = BeautifulSoup(html, "html.parser")

        # ------------------------------------------------------------------
        # Find the iframe that holds the player.
        # Typical pattern: <iframe id="playerframe" src="https://gooz.aapmains.net/embed/52362"></iframe>
        # ------------------------------------------------------------------
        iframe = None
        for tag in ("iframe", "frame"):
            cand = soup.find(
                tag,
                src=re.compile(r"(gooz\.aapmains\.net|chatgpt\.hereisman\.net|grok3\.hereisman\.net)/embed/", re.I),
            )
            if cand:
                iframe = cand
                break

        if not iframe or not iframe.has_attr("src"):
            if self.verbose:
                print("[Cricfree] No player iframe found")
            return []

        embed_url = iframe["src"]
        # Extract the numeric stream id from the embed URL (e.g. …/embed/52362)
        m = re.search(r"/embed/(\d+)", embed_url)
        stream_id = m.group(1) if m else None

        # ------------------------------------------------------------------
        # Step 2: fetch the embed page and locate the playlist URL
        # ------------------------------------------------------------------
        try:
            embed_html, _ = _get(embed_url, referer=watch_url)
        except Exception as e:  # pragma: no cover
            if self.verbose:
                print(f"[Cricfree] Embed fetch failed: {e}")
            return []

        embed_soup = BeautifulSoup(embed_html, "html.parser")
        playlist_url = None
        # Look for any URL that contains /<stream_id>/load-playlist
        if stream_id:
            pat = re.compile(
                r'["\'](https?://[^"\']+/' + re.escape(stream_id) + r'/load-?playlist[^"\']*)["\']',
                re.I,
            )
            m = pat.search(embed_html)
            if m:
                playlist_url = m.group(1)

        # Fallback: any URL that ends with load‑playlist
        if not playlist_url:
            m = re.search(
                r'["\'](https?://[^"\']+/load-?playlist[^"\']*)["\']',
                embed_html,
                re.I,
            )
            if m:
                playlist_url = m.group(1)

        if not playlist_url:
            if self.verbose:
                print("[Cricfree] Could not locate playlist URL in embed page")
            return []

        # ------------------------------------------------------------------
        # Step 3: fetch the playlist (it's an m3u8 master) and grab the .caxi line
        # ------------------------------------------------------------------
        try:
            playlist_txt, _ = _get(playlist_url, referer=embed_url)
        except Exception as e:  # pragma: no cover
            if self.verbose:
                print(f"[Cricfree] Playlist fetch failed: {e}")
            return []

        stream_url = None
        for line in reversed(playlist_txt.splitlines()):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            stream_url = line
            break

        if not stream_url:
            if self.verbose:
                print("[Cricfree] No stream URL found in playlist")
            return []

        # ------------------------------------------------------------------
        # Build the ServerEntry (the same class used by Sportsurge)
        # ------------------------------------------------------------------
        entry = ServerEntry(
            label="Cricfree",
            stream_id=stream_id or "unknown",
            url=watch_url,
            is_default=True,
            stream_url=stream_url,
            stream_error=None,
        )
        if self.verbose:
            print(f"[Cricfree] Resolved {entry.label} → {entry.stream_url}")
        return [entry]


# ----------------------------------------------------------------------
# Convenience function so app.py can treat all scrapers uniformly
# ----------------------------------------------------------------------
def get_scraper():
    """Factory – returns an instance ready to be used."""
    return CricfreeScraper()