# Setting up the ClipStream server

This guide takes you from nothing to Live TV and a release calendar in the ClipStream app. It covers Dispatcharr (where
the channels come from), the ClipStream server itself, the reverse proxy that lets the app find it, and the app side.

**You don't need this server to use ClipStream.** Without it the app plays your Jellyfin library as normal, and Live TV
falls back to Jellyfin's own Live TV (if you've set up a tuner in Jellyfin). The server adds:

- **Live TV** built from Dispatcharr: your own categories (News, Baseball, Soccer...), what's on now and next, the guide,
  and event channels (a game or fight slot) shown as upcoming, live or ended, with favorite teams first.
- **A release calendar**: upcoming episodes from Sonarr and theater, digital and Blu-ray dates from Radarr.

Nobody who watches needs a Dispatcharr, Sonarr or Radarr account. The app sends the Jellyfin sign-in it already has; the
server checks it with Jellyfin and keeps every other password and API key to itself.

## Contents

1. [What you need](#1-what-you-need)
2. [Set up Dispatcharr](#2-set-up-dispatcharr)
3. [Run the ClipStream server](#3-run-the-clipstream-server)
4. [Put it behind your Jellyfin address](#4-put-it-behind-your-jellyfin-address)
5. [Check it's working](#5-check-its-working)
6. [Use it in the app](#6-use-it-in-the-app)
7. [Choose your categories](#7-choose-your-categories)
8. [Event channels](#8-event-channels)
9. [The calendar](#9-the-calendar)
10. [Troubleshooting](#10-troubleshooting)

## 1. What you need

- A **Jellyfin** server your viewers already sign in to.
- A machine that runs **Docker** (Unraid, a NAS, a Linux box...). The server is small: one container, no database.
- For Live TV: **Dispatcharr** and an IPTV source it can read (an M3U playlist or an Xtream Codes login), plus a guide
  (XMLTV) if your source doesn't include one.
- For the calendar (optional): **Sonarr** and/or **Radarr**.
- For watching away from home: an **HTTPS address** for Jellyfin through a reverse proxy (Nginx Proxy Manager, SWAG,
  Caddy, Traefik...). At home, plain `http://` addresses work too.

## 2. Set up Dispatcharr

Dispatcharr turns an IPTV source into a tidy channel list with a guide, and serves the streams. ClipStream reads the
channel list and guide from it; TVs play the streams straight from it.

### 2.1 Install it

Follow the [Dispatcharr install instructions](https://github.com/Dispatcharr/Dispatcharr). With Docker it's one container
(web UI on port `9191` by default) with a volume for its data. Open the web UI and create your admin account.

### 2.2 Add your source and guide

1. In **M3U & EPG Manager**, add your IPTV source: an M3U URL, or an Xtream Codes server, username and password. Let it
   load. Streams are sorted into the groups your provider uses.
2. Add a guide (EPG) source if your provider's guide isn't included in the M3U/Xtream login. XMLTV URLs work.
3. Set a refresh interval on both, so new streams and guide data keep coming in.

### 2.3 Make channels

A provider can have tens of thousands of streams; you only want the ones worth watching. Channels are what ClipStream
shows, so this step decides what your viewers see.

- **By hand:** in **Channels**, create channels from streams and put them in groups.
- **Automatically (recommended):** for a provider group, turn on **auto channel sync** with a **name filter** (a regular
  expression) and a **starting channel number**, and send the matches to a group of your own (for example `Baseball`).
  Dispatcharr re-runs this on every refresh, so channels that the provider renames or adds (tonight's game slots, for
  example) show up without you doing anything.

Tips:

- Give your **channel groups** the names you'll use as categories in the app (`News & TV`, `Baseball`, `Soccer`...). The
  server maps channels into categories by group (step 7), and a channel can appear in several categories.
- Filter out duplicates you don't want (backup feeds, West-coast copies, other languages) with the exclude filter.
- Leave a gap between number ranges (100s for news, 300s for baseball...), so each category keeps its own block.
- Match channels to guide data (Dispatcharr can auto-match by the channel's TVG id). Unmatched channels still play but
  show no "now / next".

### 2.4 Create a user for ClipStream

In Dispatcharr's user settings, add a user just for the ClipStream server, for example `clipstream`, with the
**Standard** level and a strong password. It only reads the channel list, groups, logos and guide; it never changes
anything (unless you turn on backup streams, step 2.6). (The **Streamer** level can't use the API, so it won't work.) If you've limited which channels a user can see,
make sure this one can see all the channels you want in the app.

### 2.5 Make streams reachable

TVs play streams from Dispatcharr directly, so Dispatcharr must be reachable wherever your viewers are:

- **At home only:** its LAN address (`http://192.168.1.10:9191`) is enough.
- **Away from home too:** give Dispatcharr its own HTTPS address through your reverse proxy (for example
  `https://dispatch.example.com`).

If your domain is on Cloudflare, set that one host to **DNS only** (grey cloud). Cloudflare's proxy isn't meant for
streaming video and tends to stall or block it.

Dispatcharr's stream links (`/proxy/ts/stream/<id>`) play without a password, for anyone who has one, forever. To
close that, use **signed links** instead:

1. Give this server a DNS-only HTTPS address of its own (for example `https://live.example.com`, grey cloud on
   Cloudflare), pointing at the server's host port, and set `STREAM_PUBLIC_URL` to it.
2. Channel lists then hand out links to this server, signed and good for 12 hours; the server checks the signature and
   relays the stream from Dispatcharr over your LAN (a few milliseconds; the stream takes the same route as before).
3. Block `/proxy/` on Dispatcharr's public address (its admin pages can stay), so old links stop working. Keep the LAN
   address open: anything at home that plays from Dispatcharr directly (Jellyfin's own Live TV tuner) uses that.

No app update is needed: the apps play whatever link the channel list gives them.

### 2.6 Channel upkeep: backup streams, duplicates, numbers (optional)

With more than one IPTV source (or several logins to one provider), the server can add the same channel from each
other source to every auto-synced channel. Dispatcharr then plays the first stream whose source has a free
connection, and moves on when a stream fails, so viewers don't notice which login they're on.

1. In **M3U & EPG Manager**, set each source's **max streams** to the number of connections your provider allows.
   Dispatcharr only skips a full source when it knows the limit.
2. Set each source's **priority**: higher numbers go first.
3. Give the ClipStream user the **Admin** level (it now changes channels) and set
   `DISPATCHARR_MANAGE_CHANNELS=true` (step 3).

A stream counts as the same channel when its name matches once the country prefix, quality tags and symbols are
dropped (`US - ESPN HD` and `US| ESPN FHD`). The server checks new and changed channels every few minutes, and all of
them after a source refreshes, so renamed game slots get matching backups again. `/api/health` shows the last check
(step 5).

The same setting does three more things:

- **Duplicates.** Some providers list a channel twice, so auto-sync makes two channels with the same name. The server
  keeps the first, hides the other and adds its stream to the first as one more backup.
- **Channel numbers.** Dispatcharr's auto-sync renumbers its channels whenever a source refreshes, except channels
  whose number you've pinned (edit the channel and change its number: that's saved as an override). Once a group has
  pinned numbers, the server pins channels that show up later after the group's highest number, so the order you
  set stays. Groups with no pinned numbers keep Dispatcharr's numbering.
- **Guides.** A channel with no guide assigned gets a matching guide entry from your EPG sources: the same US call
  sign (`PIX11 (WPIX)`), or the same name in the same country. Channels that still have no listings show their event
  (for game and fight slots) or their own name in the app's guide.

## 3. Run the ClipStream server

The image is `ghcr.io/jcarrnah/clipstream-server:latest`. The server listens on port **8787** inside the container.

### Settings

| Variable | Needed for | What to put |
|---|---|---|
| `JELLYFIN_URL` | everything | Jellyfin as the container can reach it, e.g. `http://192.168.1.10:8096` |
| `DISPATCHARR_URL` | Live TV | Dispatcharr as the container can reach it, e.g. `http://192.168.1.10:9191` |
| `DISPATCHARR_PUBLIC_URL` | Live TV | The address TVs use to play streams (step 2.5). Defaults to `DISPATCHARR_URL`. |
| `DISPATCHARR_USERNAME` / `DISPATCHARR_PASSWORD` | Live TV | The user from step 2.4 |
| `STREAM_PUBLIC_URL` | optional | Signed stream links through this server (step 2.5), e.g. `https://live.example.com`. Unset: Dispatcharr's own links. |
| `STREAM_SECRET` | optional | Key for signing those links. Default: derived from `DISPATCHARR_PASSWORD` (changing that password ends old links). |
| `DISPATCHARR_MANAGE_CHANNELS` | optional | `true` for backup streams from your other IPTV sources, hidden duplicates and kept channel numbers (step 2.6). Off by default. |
| `EVENT_PREGAME_MINUTES` / `EVENT_POSTGAME_MINUTES` | optional | Game slots show from this long before the real start (ESPN's schedule for NFL, NBA, NHL, MLB, WNBA, college football/basketball, major soccer) until this long after the final, so "starting soon" pictures and leftover slots stay out of the list. Default 30 / 30. Other events use the provider's times. `SCOREBOARD=false` turns the schedule check off. |
| `LOCAL_TIMEZONE` | Live TV | Your time zone, e.g. `America/Chicago`. Event times without a zone ("7pm Cubs at Reds") are read in it. Default `America/New_York`. |
| `CATEGORIES_FILE` | Live TV | Path (inside the container) to your categories file, see step 7 |
| `SONARR_URL` / `SONARR_API_KEY` | Calendar | Sonarr's address and API key (Sonarr → Settings → General) |
| `RADARR_URL` / `RADARR_API_KEY` | Calendar | Radarr's address and API key (Radarr → Settings → General) |
| `DISCORD_REPORT_WEBHOOK` | Reports | A Discord channel webhook (channel → Edit → Integrations → Webhooks → New, Copy URL). Viewers' "report a problem" messages are posted there. Without it reports are only kept and logged (`GET /api/reports`, admins). |
| `DISCORD_REPORT_MENTION` | optional | Who to ping with each report, e.g. `<@123456789012345678>` (Discord: Developer Mode on, right-click yourself → Copy User ID). |
| `JELLYFIN_PUBLIC_URL` | optional | Jellyfin's public address, so a report links to the item, e.g. `https://watch.example.com`. |
| `REPORTS_FILE` | optional | Keep reports across restarts, e.g. `/data/reports.jsonl` on a mapped volume. |
| `SEERR_URL` / `SEERR_API_KEY` | Requests | Jellyseerr's (or Seerr's) address and API key (Settings → General). The server makes requests as each signed-in Jellyfin user, so Jellyseerr's own permissions and auto-approval apply; a user who never opened Jellyseerr is imported on first use. |
| `CHANNEL_REFRESH_SECONDS` | optional | How often the channel list is re-read. Default `120`. |
| `GUIDE_REFRESH_SECONDS` / `GUIDE_HOURS_AHEAD` | optional | Guide refresh (default `300` s) and how far ahead it reaches (default `24` h) |

Live TV turns on when the three `DISPATCHARR_*` login settings are present; the calendar when at least one of Sonarr or
Radarr has both an address and a key; requests when both `SEERR_*` are set. Leave out what you don't use.

### Docker Compose

Copy [`docker-compose.example.yml`](../docker-compose.example.yml) and [`.env.example`](../.env.example) next to each
other as `docker-compose.yml` and `.env`, fill in `.env`, then:

```
docker compose up -d
```

To use a categories file, uncomment the `volumes:` lines, put `categories.json` in `./config`, and set
`CATEGORIES_FILE=/config/categories.json`.

### Unraid

1. **Docker → Add Container**.
2. **Repository:** `ghcr.io/jcarrnah/clipstream-server:latest`.
3. **Port:** add a port mapping, container `8787` → any free host port (for example `8787`).
4. **Variables:** add each setting from the table above as a variable.
5. For a categories file: add a path mapping, container `/config` → a folder on your array, and set
   `CATEGORIES_FILE=/config/categories.json`.
6. Apply. To update later, use **Check for updates** on the Docker page.

## 4. Put it behind your Jellyfin address

The app looks for the server at **`<your Jellyfin address>/clipstream`**. Add that path to the reverse proxy site that
serves Jellyfin, pointing at the server's host port. The server works whether or not the proxy strips the `/clipstream`
prefix.

**Nginx Proxy Manager:** edit the Jellyfin proxy host → **Custom locations** → add location `/clipstream`, scheme
`http`, forward hostname/IP = the Docker host, forward port = the server's host port. Save.

**Nginx / SWAG** (inside the Jellyfin `server { }` block):

```nginx
location /clipstream {
    proxy_pass http://192.168.1.10:8787;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

**Caddy** (inside the Jellyfin site):

```
handle /clipstream* {
    reverse_proxy 192.168.1.10:8787
}
```

**Traefik:** add a router on the Jellyfin host with the rule ``PathPrefix(`/clipstream`)`` pointing at the server's port.

No reverse proxy? The app can use the server's address directly (step 6).

## 5. Check it's working

Open `https://<your Jellyfin address>/clipstream/api/health` in a browser. It needs no sign-in and should look like:

```json
{
  "name": "clipstream-server",
  "features": {"livetv": true, "calendar": true},
  "livetv": {"channels": 312, "channelsAt": "...", "guideAt": "..."},
  "calendar": {"sonarr": "ok (41 items)", "radarr": "ok (12 items)"}
}
```

- `features` shows what's switched on (step 3).
- `livetv.channels` is how many channels it read from Dispatcharr. `0` means Dispatcharr has no channels yet (step
  2.3), or the user can't see them (step 2.4).
- `calendar` shows each source's status, with the error if one can't be reached.

## 6. Use it in the app

- Sign in to Jellyfin in ClipStream as usual. The app checks `<Jellyfin address>/clipstream` and, when the server
  answers, **Live TV** and **Calendar** appear in the menu.
- **Settings → ClipStream server** shows the address it found. Set it by hand when the automatic check can't reach it:
  when you sign in with a local address and the server isn't under that address, or when you skipped the reverse proxy
  (then enter `http://<docker host>:<port>`). Clear it to go back to automatic.
- Everyone who can sign in to your Jellyfin gets Live TV and the calendar. There's nothing to set up per viewer.

## 7. Choose your categories

Categories are the rows of the Live TV screen. Each one collects channels **by Dispatcharr group**, **by channel name**,
or both, so one channel can sit in several (a regional sports network under both Basketball and Hockey, for example).
A channel that matches no category doesn't appear in any category.

The built-in categories are an example; set up your own with a JSON file (`CATEGORIES_FILE`, step 3):

```json
[
  {"id": "news", "name": "News & TV", "groups": ["News & TV"]},
  {"id": "baseball", "name": "Baseball", "groups": ["Baseball"],
   "names": ["MLB NETWORK", "BALLY SPORTS MIDWEST"], "teams": ["Cardinals"]},
  {"id": "basketball", "name": "Basketball", "groups": ["Basketball"],
   "names": ["BALLY SPORTS MIDWEST"], "teams": ["Celtics", "Lakers"]},
  {"id": "soccer", "name": "Soccer", "groups": ["Soccer"], "exclude": ["PRESS CONFERENCE"]},
  {"id": "events", "name": "Events", "groups": ["Events"]}
]
```

| Field | |
|---|---|
| `id` | Short unique id (letters, digits, dashes) |
| `name` | What the app shows |
| `groups` | Dispatcharr channel groups whose channels belong here (exact names) |
| `names` | Regular expressions matched against channel names (case-insensitive) |
| `exclude` | Regular expressions; a channel matching one is left out even if it matched above |
| `teams` | Favorite teams: games with one of these names sort to the top and are highlighted |

Categories show in the order of the file. Restart the container after changing the file.

## 8. Event channels

Many sources have numbered slots that carry a different game or fight each day, with the matchup and time in the
channel name. The server reads those names and shows each one as an event with a start time and a status (upcoming,
live, ended). Ended events drop out on their own, and new ones appear as Dispatcharr's auto sync picks up the renamed
slots.

It understands the common naming styles, for example:

- `MLB 08 : Rays x Yankees start:2026-09-25 00:05:00 stop:2026-09-25 03:05:00`
- `NFL | 07 - 1pm Titans at Giants`
- `UFC 332: Main Card (2026-10-03 20:00:00)`
- `NEXT | SINGAPORE: RACE | Sun 11 Oct 06:50 EDT`

Times without a zone use `LOCAL_TIMEZONE`. A slot whose time can't be read still shows, just without a time. Put these
channels in groups that map to your sports categories (or an `Events` category) like any other channel.

## 9. The calendar

With Sonarr and/or Radarr set, the app's **Calendar** shows the coming month: new episodes of the shows Sonarr monitors,
and the theater, digital and Blu-ray release dates of the movies Radarr monitors. Use addresses the container can
reach (usually the LAN address and port), and each app's API key from **Settings → General**.

## 10. Troubleshooting

**Live TV or Calendar doesn't show in the app.** Open `/clipstream/api/health` (step 5). If it doesn't load, the reverse
proxy location is wrong (step 4). If it loads but the feature is `false`, a setting is missing (step 3). If you sign in
with a local address, set the server address in the app (step 6).

**`livetv.channels` is 0.** Dispatcharr has no channels yet (step 2.3), or the ClipStream user can't log in or see them
(step 2.4). Check `DISPATCHARR_URL` from inside the container: it must be an address the container can reach, not
`localhost`.

**Live TV or Calendar missing in the web version of ClipStream.** Update the container: web browsers need the
server's CORS headers (added in September 2026).

**Channels load but won't play away from home.** `DISPATCHARR_PUBLIC_URL` must be an address that works from outside
your network (step 2.5), and on Cloudflare that host must be DNS only.

**A category is empty.** Its `groups` must match your Dispatcharr group names exactly, or its `names` must match some
channel names (step 7).

**Event times are off by some hours.** Set `LOCAL_TIMEZONE` to the zone your source writes its times in.

**The calendar shows an error for Sonarr or Radarr.** Check the address from inside the container and the API key.
