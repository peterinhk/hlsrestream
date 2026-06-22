# hlsrestream

A Python project that scrapes Sportsurge for live sports streams, follows the HLS chain with required headers, and provides a proxied HLS URL that can be opened in VLC (including iOS/Android) via a deeplink.

## Features

- Scrapes Sportsurge homepage for live events.
- For each event, retrieves available server streams and resolves the final `.caxi` HLS master playlist.
- Proxy service that rewrites HLS URLs to route through the server, injecting the required `Origin` and `Referer` headers.
- Provides a clickable VLC deeplink (`vlc://<proxy_url>`) for easy opening on mobile devices.

## Requirements

- Python 3.9+
- [uv](https://github.com/astral-sh/uv) (for fast package management)
- Packages: `flask`, `requests` (installed via `uv sync`)

## Setup

```bash
# Clone the repository (or copy the folder)
git clone <repo-url> hlsrestream
cd hlsrestream

# Create virtual environment and install dependencies
uv sync

# Run the web server
uv run python app.py
```

The server will be available at `http://localhost:5000`.

## Usage

1. Open `http://localhost:5000` in a browser.
2. Browse the list of live events.
3. Click **Streams** on an event to see available servers.
4. Click **Play** on a server to get a VLC deeplink.
5. On iOS/Android, tap the **Open in VLC** link (or copy the direct proxy link into VLC’s network stream dialog).

## How it works

- The Sportsurge scraper (`sportsurge_links.py`) is used unchanged to fetch watch pages and resolve stream URLs.
- The HLS proxy (`/proxy` endpoint) forwards segment and playlist requests with the headers:
    - `Origin: https://gooz.aapmains.net`
    - `Referer: https://gooz.aapmains.net/`
  and rewrites URLs in `.m3u8` playlists so that all segment requests also go through the proxy.

## Notes

- The stream URLs are time‑signed; the proxy must be used soon after generation.
- This project is for educational purposes only. Respect the stream providers’ terms of service.
