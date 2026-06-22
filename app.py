#!/usr/bin/env python3
"""
hlsrestream: Sportsurge scraper + HLS proxy for VLC playback.
"""

import urllib.parse
import requests
from flask import Flask, request, Response, render_template_string, redirect, url_for
from sportsurge_links import SportsurgeScraper

app = Flask(__name__)

# Headers that the target expects
TARGET_HEADERS = {
    "Origin": "https://gooz.aapmains.net",
    "Referer": "https://gooz.aapmains.net/",
}


def fetch_with_headers(url: str, **kwargs):
    """Make a GET request with the required headers."""
    headers = {**TARGET_HEADERS, **kwargs.pop("headers", {})}
    return requests.get(url, headers=headers, timeout=15, **kwargs)


def rewrite_playlist(content: str, base_url: str, proxy_root: str) -> str:
    """
    Rewrite lines in an M3U playlist so that media URLs go through our proxy.
    """
    lines = content.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        # Media URL or playlist URL
        absolute = urllib.parse.urljoin(base_url, stripped)
        # Encode for our proxy
        encoded = urllib.parse.quote(absolute, safe='')
        proxied = f"{proxy_root}?url={encoded}"
        out.append(proxied)
    return "\n".join(out) + ("\n" if content.endswith("\n") else "")


@app.route("/")
def index():
    scraper = SportsurgeScraper()
    try:
        html, _ = scraper.fetch("https://sportsurge.ws/")
        events = scraper.get_homepage_events(html)
    except Exception as e:
        app.logger.error(f"Failed to fetch homepage: {e}")
        events = []
    # Simple template
    template = """
    <!doctype html>
    <title>HLS Restream – Live Sports</title>
    <h1>Live Sports Events</h1>
    <ul>
    {% for ev in events %}
        <li>
            <strong>{{ ev.title }}</strong> ({% if ev.category %}{{ ev.category }}{% else %}Unknown{% endif %})
            – {{ ev.status }}
            [<a href="{{ url_for('event_page', watch_url=ev.url|urlencode) }}">Streams</a>]
        </li>
    {% endfor %}
    </ul>
    """
    return render_template_string(template, events=events)


@app.route("/event")
def event_page():
    watch_url = request.args.get("watch_url")
    if not watch_url:
        return "Missing watch_url parameter", 400
    watch_url = urllib.parse.unquote(watch_url)
    scraper = SportsurgeScraper()
    try:
        entries = scraper.get_embed_urls(watch_url)
    except Exception as e:
        app.logger.error(f"Failed to get embed URLs: {e}")
        return f"Error fetching streams: {e}", 500
    template = """
    <!doctype html>
    <title>Streams for {{ watch_url }}</title>
    <h1>Available Streams</h1>
    <ul>
    {% for e in entries %}
        <li>
            <strong>{{ e.label }}</strong> (ID {{ e.stream_id }})
            {% if e.stream_url %}
                → <a href="{{ url_for('watch_page', stream_url=e.stream_url|urlencode) }}">Play</a>
            {% else %}
                → Failed: {{ e.stream_error }}
            {% endif %}
        </li>
    {% endfor %}
    </ul>
    """
    return render_template_string(template, watch_url=watch_url, entries=entries)


@app.route("/watch")
def watch_page():
    stream_url = request.args.get("stream_url")
    if not stream_url:
        return "Missing stream_url parameter", 400
    stream_url = urllib.parse.unquote(stream_url)
    # Build a proxied URL for the master playlist
    proxy_base = request.host_url.rstrip('/')
    proxied_master = f"{proxy_base}/proxy?url={urllib.parse.quote(stream_url, safe='')}"
    # Provide a VLC deeplink (vlc:// scheme) and a plain HTTP link
    vlc_link = f"vlc://{proxied_master}"
    template = """
    <!doctype html>
    <title>Watch Stream</title>
    <h1>Watch Stream</h1>
    <p>Master playlist: <code>{{ stream_url }}</code></p>
    <p>
        <a href="{{ vlc_link }}">Open in VLC (iOS/Android)</a><br>
        <a href="{{ proxied_master }}">Direct proxy link (for testing)</a>
    </p>
    <p>If VLC does not open, copy the proxy link into VLC's network stream dialog.</p>
    """
    return render_template_string(template, stream_url=stream_url, vlc_link=vlc_link, proxied_master=proxied_master)


@app.route("/proxy")
def proxy():
    target_url = request.args.get("url")
    if not target_url:
        return "Missing url parameter", 400
    target_url = urllib.parse.unquote(target_url)
    try:
        resp = fetch_with_headers(target_url, stream=True)
        resp.raise_for_status()
    except Exception as e:
        app.logger.error(f"Proxy fetch failed: {e}")
        return f"Failed to fetch target: {e}", 502

    content_type = resp.headers.get("Content-Type", "")
    # If it's a playlist, rewrite
    if "application/vnd.apple.mpegurl" in content_type or "application/x-mpegurl" in content_type or target_url.lower().endswith(".m3u8"):
        # Read whole content (playlists are small)
        content = resp.content.decode("utf-8", errors="ignore")
        rewritten = rewrite_playlist(content, target_url, request.host_url.rstrip('/') + "/proxy")
        return Response(rewritten, content_type=content_type)
    else:
        # Stream raw bytes
        def generate():
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        return Response(generate(), content_type=content_type)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)