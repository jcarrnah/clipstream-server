# clipstream-server

The companion service for the [ClipStream](https://github.com/jcarrnah) Android TV app. It gives everyone who can
sign in to your Jellyfin server:

- **Live TV** from [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr): categories, channels with what's on
  now and next, the guide, and event channels (game and fight slots) with parsed start times and live/upcoming
  status. Favorite teams sort to the top.
- **A release calendar** that merges Sonarr episodes and Radarr theater, digital and Blu-ray dates.

Nobody needs a Dispatcharr, Sonarr or Radarr account. The app sends the Jellyfin sign-in it already has, and
this service asks Jellyfin who it belongs to. The service account and API keys stay on the server.
Streams play straight from Dispatcharr, so your provider only ever sees your server.

## Setup

**[docs/SETUP.md](docs/SETUP.md)** walks through everything: Dispatcharr (source, guide, channels, the service
account), running this container (Docker Compose or Unraid), the reverse proxy path the app looks for, checking it
works, categories, event channels and the calendar.

Short version: run `ghcr.io/jcarrnah/clipstream-server:latest` (port 8787) with the settings from
[`.env.example`](.env.example), route `/clipstream` on your Jellyfin site to it, and open
`https://<your-jellyfin>/clipstream/api/health`.

## API

All endpoints except `/api/health` and logos need a Jellyfin token: an `Authorization: MediaBrowser Token="..."`
header, an `X-Emby-Token` header, or `?api_key=`.

| Endpoint | |
|---|---|
| `GET /api/health` | Features and status (no auth) |
| `GET /api/livetv/categories` | Categories with channel and live-event counts |
| `GET /api/livetv/channels?category=baseball` | Channels in a category, best first; ended events are dropped |
| `GET /api/library/upgrades` | Movies (TMDB ids) and episodes (TVDB ids) whose recent Sonarr/Radarr imports were only quality upgrades, so the app keeps them out of Recently Added |
| `GET /api/livetv/guide?channels=1,2&hours=6` | Guide programs per channel id; channels without listings get filler (an event slot's event, or hourly blocks with the channel name, `placeholder: true`) |
| `GET /api/livetv/logo/{id}` | Channel logo (cached) |
| `GET /api/calendar?days_back=1&days_ahead=30` | Upcoming episodes and movie releases |
| `GET /api/requests/mine` | The caller's Jellyseerr requests with their status (pending, approved, downloading, partial, available, declined, failed) |
| `GET /api/requests/discover/{trending,movies,tv,upcoming}`, `GET /api/requests/search?q=` | Titles to request, each with what the library already has |
| `GET /api/requests/{movie,tv}/{tmdbId}` | One title; a show lists its seasons and each one's state |
| `POST /api/requests/{movie,tv}/{tmdbId}` | Request it as the caller (`{"seasons": [1, 2]}` for some of a show's seasons) |

## Categories

The defaults are in [`app/categories.py`](app/categories.py). A channel appears in every category whose rule
matches: its Dispatcharr group, or a name pattern. That's how MSG lands in both Basketball and Hockey, and ESPN
in News & TV. Each category can list favorite `teams`. To customize, copy the list to a JSON file and set
`CATEGORIES_FILE`.

## Dispatcharr notes

Dispatcharr quirks found along the way:
- One auto-sync rule per provider group, so this service does the multi-category mapping.
- Several rules feeding one group need "next available" numbering; fixed ranges leave channels unnumbered.
- `/proxy/ts/stream/<uuid>` plays without authentication. With `STREAM_PUBLIC_URL` set, apps get signed, expiring
  links to this server, which relays the stream (`app/streams.py`), and `/proxy/` can be closed to the internet.

## Development

```
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt pytest
.venv/Scripts/python -m pytest -q
JELLYFIN_URL=... DISPATCHARR_URL=... .venv/Scripts/python -m uvicorn app.main:app --port 8787
```

License: GPL-3.0, same as the app.
