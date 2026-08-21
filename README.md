# Chronicle Scrobbler

[![Latest Release](https://img.shields.io/github/v/release/thegoddamnbeckster/Chronicle_Scrobbler?label=Chronicle_Scrobbler&color=1a5276)](https://github.com/thegoddamnbeckster/Chronicle_Scrobbler/releases/latest)

A Kodi addon that scrobbles playback to your self-hosted [Chronicle](https://github.com/thegoddamnbeckster/Chronicle)
server, and syncs ratings, artwork, and watch counts both ways. Modeled directly on
[SIMKL_Scrobbler](https://github.com/thegoddamnbeckster/SIMKLScrobbler) — same QR
device-auth flow, same live-scrobbling engine — retargeted at Chronicle instead of SIMKL,
plus a full bidirectional data-parity sync SIMKL's own addon doesn't have a Chronicle
equivalent for.

**Addon ID:** `service.chronicle.scrobbler`
**Version:** 2.0.16
**Kodi:** 19 "Matrix" and later (JSON-RPC API v12+)
**Auth:** QR code device authentication (no password typed into Kodi)

---

## Table of Contents

- [Overview](#overview)
- [What syncs, and in which direction](#what-syncs-and-in-which-direction)
- [Architecture](#architecture)
- [Setup](#setup)
- [Settings](#settings)
- [Data Model](#data-model)
- [Known Limitations (Phase 1)](#known-limitations-phase-1)
- [Repository Structure](#repository-structure)
- [Installation](#installation)
- [Roadmap](#roadmap)

---

## Overview

Chronicle is a self-hosted, universal media tracker — the same role Trakt or SIMKL play,
but locally hosted and generic across any media type. This addon is Chronicle's
counterpart to SIMKL's own official Kodi scrobbler: it runs inside Kodi, reports what
you're watching in real time, and keeps Kodi's own library metadata (ratings, artwork,
play counts) in sync with what Chronicle knows — including from *other* sources Chronicle
tracks (Trakt imports, Letterboxd, manual entries, other devices).

## What syncs, and in which direction

Two different rules, by field type — this is a deliberate design decision, not an
oversight:

| Field | Direction | Rule |
|---|---|---|
| **Rating** | Chronicle → Kodi | Chronicle is the source of truth. Unconditional overwrite — whatever Chronicle has replaces Kodi's local rating, always. |
| **Artwork** (poster, fanart, banner, clearlogo, clearart, discart) | Chronicle → Kodi | Same — unconditional overwrite. |
| **Play count / last played** | **Both ways** | Genuine reconciliation. Whichever side's last-played timestamp is more recent is "ahead"; the other side's count is brought up to match. A watch Chronicle doesn't know about (Kodi watched it before this addon existed, or offline) flows *up*; a watch Kodi doesn't know about (tracked via another Chronicle source) flows *down*. |
| **Live playback progress** | Kodi → Chronicle | Real-time scrobbling as you watch — this is the one thing that's always Kodi → Chronicle, since Chronicle can't know what's playing right now any other way. |

There's no bulk "export my Kodi ratings/collection to Chronicle" — that would contradict
Chronicle being the source of truth for those fields. Kodi only feeds Chronicle through
live scrobble *events* (new watches are additive facts) and the play-count reconciliation
above (which only pushes *upward* when Kodi is legitimately ahead).

## Architecture

```
Kodi (this addon)
    │
    ├── service.py → lib/monitor.py        ← background: live playback scrobbling
    │       └─ POST /api/v1/scrobble        (resolves/creates the Chronicle item
    │                                        automatically if not matched yet)
    │
    └── default.py → menu
            ├── "Sync Watch History & Ratings Now" → lib/sync_engine.py
            │       ├─ GET /api/v1/library?status=…     (every status, paginated)
            │       ├─ lib/kodi_matcher.py                (movie/show/episode/set lookup)
            │       ├─ VideoLibrary.Set*Details            (rating + art, unconditional)
            │       ├─ GET /api/v1/scrobble/summary/{id}   (per-item watch reconciliation)
            │       └─ VideoLibrary.Set*Details / POST /api/v1/scrobble  (whichever side wins)
            │
            ├── "Connect to Chronicle" → lib/device_auth.py + lib/qr_dialog.py
            │       (QR code + PIN, same UX as SIMKL's own device-auth flow)
            │
            ├── "Sync Lists to Kodi" → lib/playlist_sync.py
            │       (Chronicle lists → Kodi .m3u playlists, matched via kodi_matcher.py)
            │
            ├── "Reset TV Show / Movie Progress" → lib/reset_manager.py
            │       (Kodi-local only — Chronicle keeps full history regardless)
            │
            └── "Test Connection"
```

### Covers every hierarchy level with data at it

- **Movies** — full rating + art + play-count reconciliation.
- **Movie collections** — Chronicle models a collection as a plain parent item with no
  watch/rating concept of its own; matched against Kodi's native Movie Set (art + overview
  only — Kodi Movie Sets have no `userrating`/`playcount`).
- **TV shows** — rating + art (no play-count reconciliation at the show level; Kodi tracks
  watch state per episode, not as an independent show-level count).
- **TV episodes** — full rating + art + play-count reconciliation, matched via the show's
  own match plus season/episode number (season number is resolved from the season's own
  Chronicle item, since Chronicle's `AncestorDto` only carries id+name).

## Setup

1. Install this addon in Kodi (see [Installation](#installation)).
2. Open the addon → **Open Settings** → **Connection** → enter your Chronicle server's
   URL (e.g. `http://192.168.1.50:7979` or your domain — whatever address Kodi can
   actually reach on your network; there's no default filled in on purpose).
3. Open the addon → **Connect to Chronicle** → a QR code and a short display code appear.
   Scan it (or open the shown URL) on your phone/computer, log into Chronicle, and
   approve the device. The addon polls automatically and saves the API key once approved
   — same flow as SIMKL's own PIN/QR signup.
4. Optionally run **Sync Watch History & Ratings Now** to do an initial bulk push.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Chronicle URL | _(empty)_ | Required — your server's host/IP and port |
| API Key | _(empty, hidden)_ | Set automatically by the QR device-auth flow |
| Watched threshold (%) | 80 | Progress percentage that counts as "watched" |
| Poll interval (seconds) | 30 | How often a scrobble update is sent during playback |
| Scrobble movies / TV / music | true / true / false | Per-type scrobbling toggle |
| Sync ratings | true | Chronicle → Kodi rating push |
| Sync artwork | true | Chronicle → Kodi art push |
| Sync play counts / last played | true | Bidirectional reconciliation |

## Data Model

**Scrobble payload** (`POST /api/v1/scrobble`) — matches Chronicle.API's real
`ScrobbleRequestDto` exactly:
```json
{
  "mediaItemId": null,
  "title": "Movie Title",
  "year": 2026,
  "mediaType": "movie",
  "externalIds": {"imdb": "tt1234567", "tmdb": "12345"},
  "progressPercent": 87.5,
  "deviceName": "Kodi"
}
```
`mediaItemId` is omitted for anything Chronicle hasn't matched yet — Chronicle resolves
or creates the item from `title`/`externalIds`/`mediaType` server-side.

**Watch-count reconciliation** uses `GET /api/v1/scrobble/summary/{mediaItemId}` —
`{lastWatchedAt, watchedCount}` — added specifically for this addon so a sync client
doesn't need to page through full scrobble history just to compare counts.

**Art mapping** (Chronicle's `ResolvedMetadataDto` → Kodi's `art` dict):

| Chronicle field | Kodi key (movies/shows/sets) | Kodi key (episodes) |
|---|---|---|
| `posterUrl` | `poster` | `thumb` |
| `backdropUrl` | `fanart` | _(not applicable)_ |
| `logoUrl` | `clearlogo` | _(not applicable)_ |
| `bannerUrl` | `banner` | _(not applicable)_ |
| `clearartUrl` | `clearart` | _(not applicable)_ |
| `discUrl` | `discart` | _(not applicable)_ |

Episodes use a different Kodi art model — confirmed live against a real Kodi 21+
library: an episode's own art is just `thumb` (its still/screenshot); `poster`/`fanart`
only exist as `tvshow.poster`/`tvshow.fanart` (inherited from the show, not independently
overwritable per-episode).

## Since 2.0.0

A run of stability fixes on top of the Phase 1 release: the device-auth QR flow now
actually renders reliably (several fixes to VFS path handling, PNG color type, and
per-attempt cache-busting filenames), the connect dialog got a real backdrop and
styled buttons, device names sent to Chronicle prefer the machine's real hostname
over Kodi's own arbitrary FriendlyName, and `VideoLibrary.Clean` now runs
automatically after a scan — Kodi's "Set Content" re-scan toggle alone doesn't clear
stale (moved/renamed/deleted) library entries. See `addon.xml`'s own `<news>` block
for the full per-version changelog.

## Known Limitations (Phase 1)

- **`ResolvedMetadataDto` gained 4 new fields** (`logoUrl`, `bannerUrl`, `clearartUrl`,
  `discUrl`) as part of this addon's build — Chronicle already computed them internally
  but didn't expose them over the API before now.
- **Movie collection matching is by title only** — Chronicle collections have no external
  ID of their own, and Kodi Movie Sets don't carry one either. An exact-title match is
  the best available signal; a title that differs between the two (e.g. "Alien" vs.
  "Alien Collection") won't match. Not deeply tested against real collection data.
- **Not yet built** (see [Roadmap](#roadmap)): a branded rating-dialog UI prompting after
  the watched threshold, the three standalone context-menu addons (rate / toggle-watched
  / manual-sync) SIMKL_Scrobbler has, exclusion-path settings, and incremental
  activity-based sync (today's bulk sync re-checks everything every run).

## Repository Structure

```
Chronicle_Scrobbler/
├── addon.xml
├── default.py                    # Menu entry point
├── service.py                    # Background service entry point
├── icon.png                      # Chronicle's own "C" icon
├── LICENSE
├── lib/
│   ├── logger.py                 # xbmc.log wrapper
│   ├── chronicle_client.py       # HTTP client for Chronicle's REST API
│   ├── media_info.py             # Kodi JSON-RPC wrapper + playback snapshot
│   ├── monitor.py                # Playback lifecycle → scrobble triggers
│   ├── progress_tracker.py       # Scrobble-timing decision rules
│   ├── device_auth.py            # QR device-auth flow orchestration
│   ├── qr_dialog.py              # QR code + PIN display UI
│   ├── reset_manager.py          # Kodi-local watch-progress reset
│   ├── playlist_sync.py          # Chronicle Lists → Kodi .m3u playlists
│   ├── kodi_matcher.py           # Movie/show/episode/collection lookup (shared)
│   └── sync_engine.py            # Bidirectional rating/art/play-count sync
└── resources/
    ├── settings.xml
    └── language/resource.language.en_gb/strings.po
```

## Installation

Zip this directory (or clone it directly into Kodi's addons folder as
`service.chronicle.scrobbler`), then install via Kodi's "Install from zip file" option, or
copy the folder directly into Kodi's `addons/` directory and restart Kodi.

```
<Kodi userdata>/addons/service.chronicle.scrobbler/
```

## Roadmap

Mirrors SIMKL_Scrobbler's own development phases, adapted:

1. **Auth, live scrobbling, bidirectional watch-history/rating/art sync** — this release.
2. Branded rating-dialog UI (prompt after watched threshold).
3. Context-menu addons (`context.chronicle.rate`, `context.chronicle.watched`,
   `context.chronicle.sync`) — Kodi requires these as standalone addons.
4. Exclusion settings (Live TV, HTTP streams, plugin sources, custom paths).
5. Incremental sync via activity timestamps; scheduled auto-sync interval.

---

*This project supersedes and consolidates two earlier, near-identical skeleton
repositories (`service.chronicle.scrobbler` and `Chronicle.Service.Scrobbler.Kodi`) —
their working `lib/` scaffold (auth, scrobbling, playlist sync, reset manager) is reused
here; both are retired.*
