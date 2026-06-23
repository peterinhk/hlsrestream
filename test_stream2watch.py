import re
import random
import requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import cloudscraper

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

TIMEOUT = 15

def _make_session():
    sess = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    sess.mount("https://", HTTPAdapter(max_retries=retry))
    sess.mount("http://", HTTPAdapter(max_retries=retry))
    return sess

def _get(url, referer=None):
    """Fetch a URL with a random UA and optional Referer."""
    sess = cloudscraper.create_scraper()
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    r = sess.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text, sess

class Stream2WatchScraper:
    BASE = "https://www.stream2watch.me/en/"

    def __init__(self, verbose=False):
        self.verbose = verbose

    def get_homepage_events(self):
        html, _ = _get(self.BASE)
        soup = BeautifulSoup(html, "html.parser")
        events = []

        for sport_head in soup.find_all("h2"):
            sport = sport_head.get_text(strip=True)
            ul = sport_head.find_next_sibling("ul")
            if not ul:
                continue
            for li in ul.find_all("li", class_="event"):
                title_div = li.find("div", class_="title")
                title = title_div.get_text(strip=True) if title_div else "Unknown"
                a = li.find("a", class_="watch-btn")
                if not a or not a.has_attr("data-url"):
                    continue
                watch_url = urljoin(self.BASE, a["data-url"])
                events.append({
                    "title": title,
                    "category": sport,
                    "status": "LIVE",
                    "url": watch_url,
                })
        return events

    def get_embed_urls(self, watch_url):
        html, sess = _get(watch_url, referer=self.BASE)
        soup = BeautifulSoup(html, "html.parser")
        iframe = soup.find("iframe", {"id": "playeriframe"}) \
                  or soup.find("iframe", src=re.compile(r"/embed/"))
        if not iframe or not iframe.has_attr("src"):
            return []
        embed_url = iframe["src"]
        m = re.search(r"/embed/(\d+)", embed_url)
        stream_id = m.group(1) if m else None

        embed_html, _ = _get(embed_url, referer=watch_url)

        playlist_url = None
        if stream_id:
            pat = re.compile(r'["\'](https?://[^"\']+/' + re.escape(stream_id) + r'/load-?playlist[^"\']*)["\']')
            m = pat.search(embed_html)
            if m:
                playlist_url = m.group(1)
        if not playlist_url:
            m = re.search(r'["\'](https?://[^"\']+/load-?playlist[^"\']*)["\']', embed_html, re.I)
            if m:
                playlist_url = m.group(1)

        if not playlist_url:
            return []

        playlist_txt, _ = _get(playlist_url, referer=embed_url)

        stream_url = None
        for line in reversed(playlist_txt.splitlines()):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            stream_url = line
            break

        if not stream_url:
            return []

        # Return a simple dict for testing
        return [{
            "label": "Stream2Watch",
            "stream_id": stream_id or "unknown",
            "url": watch_url,
            "is_default": True,
            "stream_url": stream_url,
            "stream_error": None,
        }]

if __name__ == "__main__":
    scraper = Stream2WatchScraper()
    print("Fetching homepage events...")
    events = scraper.get_homepage_events()
    print(f"Found {len(events)} events.")
    for ev in events[:5]:  # show first 5
        print(f"- {ev['title']} ({ev['category']}) - {ev['url']}")
    if events:
        # Try to resolve the first event's stream
        first = events[0]
        print(f"\nResolving stream for: {first['title']}")
        streams = scraper.get_embed_urls(first['url'])
        if streams:
            s = streams[0]
            print(f"  Stream URL: {s['stream_url']}")
        else:
            print("  No stream URL found.")
    else:
        print("No live events found.")