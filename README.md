# Kick Start Search

A tiny local search engine for the DJ mix archive at
[sets.dfx.at/Kick Start](https://sets.dfx.at/Kick%20Start/) — 600+ episodes,
each with one MP3 and a plain-text tracklist. The site itself has no search;
this indexes every tracklist into SQLite (FTS5) and serves a fast local
search UI.

**Zero dependencies** — Python 3.9+ standard library only (SQLite FTS5 is
bundled with Python's `sqlite3` on macOS and most builds).

## Quick start

```bash
cd kickstart-search

# 1. Build the index (first run: a few minutes; later runs are incremental)
python3 kickstart_search.py crawl

# 2. Start the service
python3 kickstart_search.py serve

# 3. Open http://127.0.0.1:8720/
```

Search examples:

- `sean tyas` — all words must match (prefix matching, so `tyas*` is implied)
- `"lost emotions"` — quoted exact phrase
- `244` — jumps straight to set 244
- `bryan kearney remix` — search remixers too

The UI groups results by set, shows the matched track positions, can expand
the full tracklist of any set, copy it to the clipboard, and stream the MP3
directly from the origin server (no MP3s are ever downloaded by the crawler).

The **re-crawl to update** link in the header re-indexes the archive in the
background (via the running server) with a live progress bar — no need to
touch the terminal for routine updates.

## Commands

| command | purpose |
|---|---|
| `crawl` | fetch directory listings + tracklists, parse, index. Re-run any time: folders whose directory listing is unchanged (compared by content hash — the server sends no validators for listings) are skipped, tracklists use conditional `ETag`/`If-Modified-Since`, and new episodes are picked up automatically. `--force` ignores caches |
| `serve` | run the web UI + JSON API on `127.0.0.1:8720` (`--host`/`--port` to change) |
| `stats` | print index counts |

Useful crawl options: `--reset` (rebuild from scratch), `--limit N` (testing),
`--workers N`, `--delay S` (politeness throttle), `--force` (refetch everything).

## HTTP API

```
GET  /api/search?q=sean+tyas&limit=400   -> ranked tracks + per-set hit counts
GET  /api/set/244                        -> full tracklist of one set
GET  /api/sets                           -> all indexed sets
GET  /api/stats                          -> index statistics
POST /api/recrawl                        -> start a background re-crawl (409 if one is running)
GET  /api/crawl/status                   -> live crawl progress (phase, done/total, unchanged)
```

## How it works

- The crawler reads the Apache-style directory index at `Kick Start/`,
  follows only `Kick Start NNN/` folders (631 exist between 001–682; gaps
  like 500/550/600 don't exist), and in each folder grabs the `.mp3` URL
  and the `.txt` tracklist (the name varies — `tracklist.txt` or
  `dfx - Kick Start NNN.txt` — so it prefers matching names).
- Tracklist lines are parsed as `NN. Artist - Title (Remix)`. Some files
  use a broken formatter whose numbers restart mid-file (e.g. set 024:
  `01..09, 00, 01..09, 00, ...`), so the stored position comes from line
  order; the original label is kept too.
- Everything lands in `kickstart.db` (SQLite, WAL mode): `sets`, `tracks`,
  and an FTS5 index over artist / title / remix / set title.

## Files

- `kickstart_search.py` — crawler + parser + HTTP server (single file)
- `static/index.html` — the search UI (vanilla JS, no build step)
- `kickstart.db` — created by `crawl` (gitignore it if you fork this)
