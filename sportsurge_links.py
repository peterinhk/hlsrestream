#!/usr/bin/env python3
"""
sportsurge_links.py
Retrieve all server embed URLs for a Sportsurge watch page.

Usage:
    python sportsurge_links.py <watch_url> [options]

Examples:
    python sportsurge_links.py https://sportsurge.ws/watch/.../363496200
    python sportsurge_links.py https://sportsurge.ws/watch/.../363496200 --format json
    python sportsurge_links.py https://sportsurge.ws/watch/.../363496200 --format csv -v
"""

import sys
import re
import json
import csv
import io
import random
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENTS = [
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.3 Safari/605.1.15"
    ),
    (
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
]

TIMEOUT = 15  # seconds

# Embed URL patterns to try in order
IFRAME_PATTERNS = [
    re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE),  # src="..."
    re.compile(r'<iframe[^>]+data-src=["\']([^"\']+)["\']', re.IGNORECASE),  # data-src lazy
    re.compile(r'embedUrl\s*=\s*["\']([^"\']+)["\']'),  # JS var embedUrl = '...'
    re.compile(r'src:\s*["\']([^"\']+embed[^"\']+)["\']'),  # JS object src: '...embed...'
]

# Server button patterns – captures stream ID and full inner label text
SERVER_PATTERN = re.compile(
    r'onclick=["\']window\.changeStream\((\d+)\)["\'][^>]*>(.*?)<',
    re.DOTALL,
)

# Boilerplate suffixes Sportsurge appends to /event/ slugs that aren't part
# of the actual event name (e.g. "...-live-streaming-links")
_SLUG_JUNK_SUFFIXES = ("-live-streaming-links", "-streaming-links", "-live-stream")

def _slug_to_title(slug: str) -> str:
    """Convert a URL slug into a readable title, stripping known boilerplate suffixes."""
    for suffix in _SLUG_JUNK_SUFFIXES:
        if slug.endswith(suffix):
            slug = slug[: -len(suffix)]
            break
    return slug.replace("-", " ").title()

def _title_slug_from_href(href: str) -> str:
    """
    Pick the most descriptive path segment for the title fallback.
    /watch/.../<teams-slug>/<numeric-id>  -> the teams slug is second-to-last
    /event/<sport>/<descriptive-slug>     -> the descriptive slug is last (no numeric id)
    """
    parts = [p for p in href.split("/") if p]
    if not parts:
        return ""
    if parts[-1].isdigit() and len(parts) >= 2:
        return parts[-2]
    return parts[-1]

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ServerEntry:
    label: str
    stream_id: str
    url: str
    is_default: bool = field(default=False)
    stream_url: Optional[str] = field(default=None)
    stream_error: Optional[str] = field(default=None)

# ---------------------------------------------------------------------------
# Scraper class
# ---------------------------------------------------------------------------

class SportsurgeScraper:
    """Fetch and parse a Sportsurge watch page for stream embed URLs."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._setup_logging()
        self.session = self._build_session()

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _setup_logging(self) -> None:
        level = logging.DEBUG if self.verbose else logging.WARNING
        logging.basicConfig(
            format="[%(levelname)s] %(message)s",
            level=level,
            stream=sys.stderr,
        )
        self.log = logging.getLogger("sportsurge")

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _make_headers(self, referer: Optional[str] = None) -> dict:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    # ------------------------------------------------------------------
    # Core fetch
    # ------------------------------------------------------------------

    def fetch(self, url: str) -> str:
        """Download the raw HTML of the watch page, following redirects."""
        self.log.debug("GET %s", url)
        resp = self.session.get(
            url,
            headers=self._make_headers(),
            timeout=TIMEOUT,
            allow_redirects=True,
        )
        resp.raise_for_status()
        final_url = resp.url
        if final_url != url:
            self.log.debug("Redirected → %s", final_url)
        self.log.debug("Response: %d bytes, status %d", len(resp.content), resp.status_code)
        return resp.text, final_url

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_servers(self, html: str) -> list[tuple[str, str]]:
        """
        Return (label, stream_id) pairs in document order.
        Captures full inner label text (e.g. 'Server1', 'HD1', 'Backup').
        """
        matches = SERVER_PATTERN.findall(html)
        results = []
        for sid, raw_label in matches:
            label = re.sub(r'<[^>]+>', '', raw_label).strip()  # strip any nested tags
            if label:
                results.append((label, sid))
        self.log.debug("Found %d server buttons", len(results))
        return results

    def parse_base_url(self, html: str) -> Optional[str]:
        """
        Try multiple patterns to find the iframe/embed base URL.
        Returns the base (with trailing stream ID stripped) or None.
        """
        for pattern in IFRAME_PATTERNS:
            m = pattern.search(html)
            if m:
                src = m.group(1)
                self.log.debug("Embed URL found via pattern %r: %s", pattern.pattern[:40], src)
                # Strip trailing numeric segment to get reusable base
                base = re.sub(r'\d+$', '', src)
                return base
        self.log.debug("No embed URL found in HTML")
        return None

    def parse_default_id(self, html: str) -> Optional[str]:
        """Extract the stream ID currently loaded in the iframe src."""
        for pattern in IFRAME_PATTERNS:
            m = pattern.search(html)
            if m:
                src = m.group(1)
                dm = re.search(r'(\d+)$', src)
                return dm.group(1) if dm else None
        return None

    # ------------------------------------------------------------------
    # Embedded stream resolution (embed page -> playlist -> stream URL)
    # ------------------------------------------------------------------

    def _extract_playlist_endpoint(self, embed_html: str, stream_id: str) -> Optional[str]:
        """
        Locate the playlist URL inside an embed page. Embed frames typically
        contain a JS line like:
            const source = "https://<host>/playlist/<stream_id>/load-playlist";
        We accept any URL that contains ``/<stream_id>/load-playlist``.
        """
        # Look for a fully formed absolute URL first.
        for url in re.findall(
            r'["\'](https?://[^"\']+/' + re.escape(stream_id) + r'/(?:load-?playlist|playlist)[^"\']*)["\']',
            embed_html,
            re.IGNORECASE,
        ):
            return url
        # Fallback: rebuild from a captured host.
        m = re.search(
            r'["\'](https?://[^"\'/]+)/playlist/[^"\']*' + re.escape(stream_id) + r'[^"\']*["\']',
            embed_html,
            re.IGNORECASE,
        )
        if m:
            # We have a host but lose the path -- synthesise the conventional one.
            return f"{m.group(1)}/playlist/{stream_id}/load-playlist"
        return None

    def _extract_stream_url_from_playlist(self, playlist_body: str) -> Optional[str]:
        """
        Given an m3u/m3u8 master playlist, return the first usable media URL.
        These embeds typically serve a master whose final entry ends in ``/caxi``
        (e.g. ``https://pl.kamfir5.space/playlist/52316/example.com/caxi``).
        """
        # Pick the last URL line in the body (master playlists list variants last;
        # the ``/caxi`` variant is the target here).
        candidates = [ln.strip() for ln in playlist_body.splitlines() if ln.strip()]
        for line in reversed(candidates):
            if line.startswith("#"):
                continue
            return line
        return None

    def _resolve_stream_url(self, embed_url: str, stream_id: str) -> tuple[Optional[str], Optional[str]]:
        """
        Follow embed -> playlist -> embedded stream URL.

        Returns ``(stream_url, error_msg)`` exactly one of which is non-None.
        Never raises — failures are reported via the error string so a single
        bad server cannot abort the whole scrape.
        """
        try:
            embed_resp = self.session.get(
                embed_url,
                headers=self._make_headers(referer="https://sportsurge.ws/"),
                timeout=TIMEOUT,
            )
            embed_resp.raise_for_status()
        except Exception as e:
            return None, f"embed fetch failed: {e}"

        playlist_url = self._extract_playlist_endpoint(embed_resp.text, stream_id)
        if not playlist_url:
            return None, "could not locate playlist endpoint inside embed page"

        try:
            playlist_resp = self.session.get(
                playlist_url,
                headers=self._make_headers(referer=embed_url),
                timeout=TIMEOUT,
            )
            playlist_resp.raise_for_status()
        except Exception as e:
            return None, f"playlist fetch failed: {e}"

        stream_url = self._extract_stream_url_from_playlist(playlist_resp.text)
        if not stream_url:
            return None, "no stream URL found in playlist body"

        return stream_url, None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_embed_urls(self, watch_url: str) -> list[ServerEntry]:
        """
        Fetch the watch page and return a list of ServerEntry objects.
        Raises RuntimeError if the page cannot be parsed.
        """
        html, final_url = self.fetch(watch_url)

        base_url = self.parse_base_url(html)
        default_id = self.parse_default_id(html)
        servers = self.parse_servers(html)

        if not servers:
            if not base_url or not default_id:
                # Check if it looks like we got a valid page at all
                if len(html) < 500:
                    raise RuntimeError(
                        "Page response is suspiciously small — may be blocked or redirected to an error page."
                    )
                raise RuntimeError(
                    "No server entries found and no iframe/embed URL could be located. "
                    "The page may be JS-rendered (content injected after load) or the URL may be invalid."
                )
            # Some pages (e.g. single-fight /event/ cards) only have one embedded
            # iframe and no alternate-server buttons — treat that iframe as the
            # sole server rather than erroring out.
            self.log.debug("No server buttons found; using the single embedded iframe as Server1.")
            servers = [("Server1", default_id)]

        if not base_url:
            raise RuntimeError(
                "Could not locate an iframe/embed URL in the page source. "
                "The site may use JS-injected embeds not visible in raw HTML."
            )

        self.log.debug("Default stream ID: %s", default_id)

        entries = []
        for label, sid in servers:
            url = f"{base_url}{sid}"
            entries.append(ServerEntry(
                label=label,
                stream_id=sid,
                url=url,
                is_default=(sid == default_id),
            ))

        # Resolve the embedded stream URL (the URL that ends in ``/caxi``) for
        # every server in parallel — avoids serialising a per-server 2-hop fetch
        # which would 5x the latency on a 5-server page.
        try:
            with ThreadPoolExecutor(max_workers=min(4, max(1, len(entries)))) as _ex:
                futures = {
                    _ex.submit(self._resolve_stream_url, e.url, e.stream_id): e
                    for e in entries
                }
                for fut in as_completed(futures):
                    entry = futures[fut]
                    stream_url, err = fut.result()
                    entry.stream_url = stream_url
                    entry.stream_error = err
                    if stream_url:
                        self.log.debug("Resolved stream for %s → %s", entry.label, stream_url)
                    else:
                        self.log.debug("Stream resolution failed for %s: %s", entry.label, err)
        except Exception as e:
            # Thread pool failure is itself non-fatal — entries already populated.
            self.log.debug("Stream resolution pool error: %s", e)
            for entry in entries:
                if entry.stream_url is None and entry.stream_error is None:
                    entry.stream_error = f"resolution skipped: {e}"

        return entries

    def get_homepage_events(self, html: str) -> list[dict]:
        """Parse available sporting events from the homepage HTML."""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            events = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/watch/" not in href and "/event/" not in href:
                    continue

                full_url = urljoin("https://sportsurge.ws", href)

                # Extract teams
                team_rows = a.find_all(class_="team-name-event-row")
                teams = []
                for row in team_rows:
                    img = row.find("img", alt=True)
                    if img:
                        teams.append(img["alt"].strip())
                    else:
                        span = row.find("span")
                        if span:
                            teams.append(span.get_text().strip())
                        else:
                            teams.append(row.get_text().strip())

                # Extract category and status
                list_divs = a.find_all(class_="ListelemeDuzen")
                category = ""
                status = ""
                for div in list_divs:
                    text = div.get_text().strip()
                    if not text:
                        continue
                    if div.find("img"):
                        continue
                    if not category:
                        category = text
                    else:
                        status = text

                # Fallback/clean title
                event_title = " vs ".join(teams) if teams else ""
                chevron_img = a.find("img", alt=True)
                if chevron_img and chevron_img["alt"].startswith("Watch "):
                    alt_val = chevron_img["alt"]
                    if not category and ":" in alt_val:
                        category = alt_val.split(":", 1)[0].replace("Watch", "").strip()
                    if not event_title and ":" in alt_val:
                        event_title = alt_val.split(":", 1)[1].strip()

                if not event_title:
                    slug = _title_slug_from_href(href)
                    if slug:
                        event_title = _slug_to_title(slug)

                if not category:
                    link_text = a.get_text(" ", strip=True)
                    for sport in ["MLB", "WNBA", "NBA", "NFL", "NHL", "Boxing", "MMA",
                                  "FIFA World Cup", "UFC", "WWE", "NCAA"]:
                        if sport in link_text:
                            category = sport
                            break

                events.append({
                    "title": event_title,
                    "category": category or "Unknown Sport",
                    "status": status or "Scheduled",
                    "url": full_url
                })
            return events
        except Exception as e:
            self.log.debug("BeautifulSoup parsing failed or not available, falling back to regex: %s", e)
            return self._parse_homepage_events_regex(html)

    def _parse_homepage_events_regex(self, html: str) -> list[dict]:
        """Regex-based fallback parser for homepage events."""
        a_pattern = re.compile(
            r'<a[^>]+href=[\"\'](https://sportsurge\.ws/(?:watch|event)/[^\'\"]+)[\"\'][^>]*>(.*?)</a>',
            re.DOTALL
        )
        img_alt_pattern = re.compile(r'alt=[\"\']([^\"\']+)[\"\']')

        events = []
        for href, inner in a_pattern.findall(html):
            alts = img_alt_pattern.findall(inner)
            watch_alt = None
            for alt in alts:
                if alt.startswith("Watch "):
                    watch_alt = alt
                    break

            category = "Unknown Sport"
            title = ""
            if watch_alt:
                content = watch_alt[6:].strip()
                if ":" in content:
                    category, title = [part.strip() for part in content.split(":", 1)]
                else:
                    title = content

            if not title:
                teams = [alt for alt in alts if not alt.startswith("Watch") and "chevron" not in alt.lower()]
                if teams:
                    title = " vs ".join(teams)

            if not title:
                slug = _title_slug_from_href(href)
                if slug:
                    title = _slug_to_title(slug)

            text_content = re.sub(r"<[^>]+>", " ", inner)
            text_content = re.sub(r"\s+", " ", text_content).strip()

            status = "Scheduled"
            if "LIVE" in text_content:
                status = "LIVE"
            else:
                time_match = re.search(r"(\d+\s+(?:minute|hour|day)s?\s+from\s+now)", text_content, re.IGNORECASE)
                if time_match:
                    status = time_match.group(1)

            if category == "Unknown Sport":
                for sport in ["MLB", "WNBA", "NBA", "NFL", "NHL", "Boxing", "MMA",
                              "FIFA World Cup", "UFC", "WWE", "NCAA"]:
                    if sport in text_content:
                        category = sport
                        break

            events.append({
                "title": title,
                "category": category,
                "status": status,
                "url": href
            })
        return events


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def _style(text: str, *codes: str, stream=sys.stdout) -> str:
    """Wrap text in ANSI codes if the target stream is a TTY."""
    if not stream.isatty():
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"

def _colorize(text: str, code: str) -> str:
    """Single-code colorizer for stdout (used by the table formatter)."""
    return _style(text, code, stream=sys.stdout)

def _err(text: str, *codes: str) -> str:
    """Colorizer for stderr (status/log messages)."""
    return _style(text, *codes, stream=sys.stderr)

def _status(msg: str) -> None:
    print(_err(f"➤ {msg}", "36"), file=sys.stderr)

def _success(msg: str) -> None:
    print(_err(f"✓ {msg}", "1", "32"), file=sys.stderr)

def _error(msg: str) -> None:
    print(_err(f"✗ {msg}", "1", "31"), file=sys.stderr)

def fmt_table(entries: list[ServerEntry]) -> str:
    """Box-drawn, colorized table with a title banner, sized to actual content."""
    COL_DEFAULT = "Default"
    TICK = "✓"

    def _display_stream(entry: ServerEntry) -> str:
        return entry.stream_url or entry.stream_error or "(unresolved)"

    # Compute column widths from real data (no hardcoded padding)
    w_label     = max(len("Server"), max(len(e.label) for e in entries))
    w_url       = max(len("Embed URL"), max(len(e.url) for e in entries))
    w_stream    = max(len("Stream URL"), max(len(_display_stream(e)) for e in entries))
    w_def       = max(len(COL_DEFAULT), len(TICK))

    def cell(text: str, width: int, center: bool = False) -> str:
        return f" {text:^{width}} " if center else f" {text:<{width}} "

    top    = f"┌{'─' * (w_label + 2)}┬{'─' * (w_url + 2)}┬{'─' * (w_stream + 2)}┬{'─' * (w_def + 2)}┐"
    mid    = f"├{'─' * (w_label + 2)}┼{'─' * (w_url + 2)}┼{'─' * (w_stream + 2)}┼{'─' * (w_def + 2)}┤"
    bottom = f"└{'─' * (w_label + 2)}┴{'─' * (w_url + 2)}┴{'─' * (w_stream + 2)}┴{'─' * (w_def + 2)}┘"
    inner_width = len(top) - 2

    banner = (
        f"╭{'─' * inner_width}╮\n"
        + _colorize(f"│{' Sportsurge Stream Servers '.center(inner_width)}│", "1;36") + "\n"
        + f"╰{'─' * inner_width}╯"
    )

    header = (
        _colorize(f"│{cell('Server', w_label)}", "1;36") +
        _colorize(f"│{cell('Embed URL', w_url)}", "1;36") +
        _colorize(f"│{cell('Stream URL', w_stream)}", "1;36") +
        _colorize(f"│{cell(COL_DEFAULT, w_def, center=True)}│", "1;36")
    )

    rows = [banner, top, header, mid]
    for e in entries:
        label_text   = cell(e.label, w_label)
        url_text     = cell(e.url, w_url)
        stream_text  = cell(_display_stream(e), w_stream)
        def_text     = cell(TICK if e.is_default else "", w_def, center=True)

        if e.is_default:
            label_text  = _colorize(label_text, "1;36")
            url_text    = _colorize(url_text, "33")
            stream_text = _colorize(stream_text, "33")
            def_text    = _colorize(def_text, "1;32")
        else:
            url_text = _colorize(url_text, "2")
            if e.stream_error:
                stream_text = _colorize(stream_text, "31")  # red for errors
            else:
                stream_text = _colorize(stream_text, "2")

        rows.append(f"│{label_text}│{url_text}│{stream_text}│{def_text}│")

    rows.append(bottom)
    return "\n".join(rows)

def fmt_json(entries: list[ServerEntry]) -> str:
    return json.dumps(
        [
            {
                "label": e.label,
                "stream_id": e.stream_id,
                "url": e.url,
                "stream_url": e.stream_url,
                "default": e.is_default,
                **({"stream_error": e.stream_error} if e.stream_error else {}),
            }
            for e in entries
        ],
        indent=2,
    )

def fmt_csv(entries: list[ServerEntry]) -> str:
    buf = io.StringIO()
    fieldnames = ["label", "stream_id", "url", "stream_url", "default", "stream_error"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for e in entries:
        writer.writerow({
            "label": e.label,
            "stream_id": e.stream_id,
            "url": e.url,
            "stream_url": e.stream_url or "",
            "default": e.is_default,
            "stream_error": e.stream_error or "",
        })
    return buf.getvalue().rstrip()

FORMATTERS = {
    "table": fmt_table,
    "json": fmt_json,
    "csv": fmt_csv,
}

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Retrieve all server embed URLs for a Sportsurge watch page.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python sportsurge_links.py\n"
            "  python sportsurge_links.py https://sportsurge.ws/watch/.../363496200\n"
            "  python sportsurge_links.py <url> --format json\n"
            "  python sportsurge_links.py <url> --format csv -v\n"
        ),
    )
    p.add_argument(
        "watch_url",
        nargs="?",
        default=None,
        help="Full Sportsurge /watch/ URL (optional, starts interactive selection if omitted)"
    )
    p.add_argument(
        "--format", "-f",
        choices=["table", "json", "csv"],
        default="table",
        help="Output format (default: table)",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print debug info (final URL, response size, parsed data) to stderr",
    )
    p.add_argument(
        "--ios-server",
        type=int,
        nargs="?",
        const=0,
        default=None,
        metavar="N",
        help=(
            "Print only the iOS-VLC-ready stream URL for server N (1-based, "
            "or the default server when no N is given). Combine with "
            "`--ios-deeplink` to emit a clickable vlc:// URL."
        ),
    )
    p.add_argument(
        "--ios-deeplink",
        action="store_true",
        help=(
            "With --ios-server (or default), print a vlc:// deeplink the user "
            "can tap on iOS to open VLC."
        ),
    )
    return p


def _print_event_menu(events: list[dict]) -> None:
    """Render homepage events grouped by category, numbered for selection."""
    print(_err(f"\n── Available Sporting Events ({len(events)}) ──", "1;36"), file=sys.stderr)

    groups: dict[str, list[tuple[int, dict]]] = {}
    order: list[str] = []
    for idx, ev in enumerate(events, 1):
        cat = ev["category"]
        if cat not in groups:
            groups[cat] = []
            order.append(cat)
        groups[cat].append((idx, ev))

    for cat in order:
        print(_err(f"\n  {cat}", "1;35"), file=sys.stderr)
        for idx, ev in groups[cat]:
            status = ev["status"]
            if status.strip().upper() == "LIVE":
                badge = _err("● LIVE", "1;31")
            else:
                badge = _err(f"⏱ {status}", "33")
            num = _err(f"[{idx:>2}]", "2")
            print(f"    {num} {ev['title']:<40} {badge}", file=sys.stderr)


def select_event_interactively(scraper: SportsurgeScraper) -> str:
    """Fetch homepage, display sporting events, and prompt user to choose one."""
    homepage_url = "https://sportsurge.ws/"
    _status(f"Fetching {homepage_url} for active events…")
    try:
        html, _ = scraper.fetch(homepage_url)
    except Exception as e:
        _error(f"Error fetching homepage: {e}")
        sys.exit(1)

    events = scraper.get_homepage_events(html)
    if not events:
        _error("No active sporting events found on the homepage.")
        sys.exit(1)

    _print_event_menu(events)

    while True:
        try:
            prompt = _err(f"\nSelect an event (1-{len(events)}) or press Enter to exit: ", "1")
            sys.stderr.write(prompt)
            sys.stderr.flush()
            choice = sys.stdin.readline().strip()
            if not choice:
                _status("Exit.")
                sys.exit(0)

            idx = int(choice)
            if 1 <= idx <= len(events):
                selected = events[idx - 1]
                _success(f"Selected: {selected['title']}")
                print(file=sys.stderr)
                return selected["url"]
            else:
                _error(f"Please enter a number between 1 and {len(events)}.")
        except ValueError:
            _error("Invalid input. Please enter a valid number.")
        except (KeyboardInterrupt, EOFError):
            print(file=sys.stderr)
            _status("Exit.")
            sys.exit(0)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    scraper = SportsurgeScraper(verbose=args.verbose)

    watch_url = args.watch_url
    if not watch_url:
        watch_url = select_event_interactively(scraper)

    _status(f"Fetching {watch_url}")
    try:
        entries = scraper.get_embed_urls(watch_url)
    except requests.HTTPError as e:
        _error(f"HTTP error fetching page: {e}")
        sys.exit(1)
    except requests.ConnectionError as e:
        _error(f"Connection error: {e}")
        sys.exit(1)
    except requests.Timeout:
        _error(f"Request timed out after {TIMEOUT}s.")
        sys.exit(1)
    except RuntimeError as e:
        _error(f"Parse error: {e}")
        sys.exit(1)

    default_entry = next((e for e in entries if e.is_default), None)
    summary = f"Found {len(entries)} server{'s' if len(entries) != 1 else ''}"
    if default_entry:
        summary += f" — default: {default_entry.label}"
    _success(summary)

    # --ios-server [N] / --ios-deeplink shortcut paths.
    if args.ios_server is not None or args.ios_deeplink:
        # args.ios_server: None     → flag absent
        #                      0      → flag present, no N → use default
        #                      >=1    → specific server
        if args.ios_server is None or args.ios_server == 0:
            chosen = default_entry
        else:
            idx = args.ios_server - 1
            if idx < 0 or idx >= len(entries):
                _error(f"--ios-server N must be between 1 and {len(entries)}")
                sys.exit(1)
            chosen = entries[idx]
        if chosen is None:
            _error("No matching server (no default found and --ios-server omitted).")
            sys.exit(1)
        if not chosen.stream_url:
            _error(f"{chosen.label} has no resolvable stream URL: {chosen.stream_error or 'unknown error'}")
            sys.exit(1)
        target = chosen.stream_url

        if args.ios_deeplink:
            from urllib.parse import quote
            print(f"vlc://{quote(target, safe='')}")
        else:
            print(target)
        return

    formatter = FORMATTERS[args.format]
    print(formatter(entries))


if __name__ == "__main__":
    main()
