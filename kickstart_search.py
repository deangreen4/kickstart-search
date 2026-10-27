#!/usr/bin/env python3
"""
Kick Start Search — a tiny local search engine for the DJ mix archive at
https://sets.dfx.at/Kick Start/ (600+ episodes, each with an MP3 and a
plain-text tracklist).

Zero third-party dependencies: Python 3.9+ standard library only, with
SQLite FTS5 powering full-text search.

Usage:
    python3 kickstart_search.py crawl     # build / incrementally update the index
    python3 kickstart_search.py serve     # search UI on http://127.0.0.1:8720/
    python3 kickstart_search.py stats

The crawler never downloads MP3s. It reads directory listings and the small
text tracklists only, and stores direct MP3 URLs so the UI can stream from
the origin server.
"""

import argparse
import hashlib
import json
import re
import socket
import sqlite3
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

BASE_URL = "https://sets.dfx.at/Kick%20Start/"
DEFAULT_PORT = 8720
USER_AGENT = "kickstart-search/1.0 (local personal index; contact: local user)"

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = SCRIPT_DIR / "kickstart.db"
STATIC_DIR = SCRIPT_DIR / "static"

SET_DIR_RE = re.compile(r"^Kick Start (\d{1,4})$")
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)  # same splitting the FTS tokenizer does
TRACK_NUM_RE = re.compile(r"^\s*(\d{1,3})\s*[.)]\s*(\S.*?)\s*$")
TRACK_NUM_DASH_RE = re.compile(r"^\s*(\d{1,3})\s+[-\u2013\u2014]\s+(\S.*?)\s*$")
ARTIST_TITLE_RE = re.compile(r"^(.{2,}?)\s+[-\u2013\u2014]\s+(.+)$")
PAREN_RE = re.compile(r"\(([^()]*)\)")
REMIX_WORDS_RE = re.compile(
    r"\b(remix|remixes|remastered|remode|mix|mixes|edit|dub|version|bootleg|"
    r"rework|refix|re-rub|rerub|vip|radio|cover|tribute|instrumental|acoustic)\b",
    re.I,
)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

_SSL_CTX = None
_SSL_WARNED = False


def ssl_context():
    """Build an SSLContext, tolerating corporate TLS-inspection proxies.

    Python's OpenSSL doesn't read the macOS Keychain trust store, so behind
    TLS-inspecting gateways (e.g. Cato Networks) the default context fails.
    Ladder: default CAs -> macOS bundle -> certifi -> macOS Keychain export
    -> last-resort unverified (with a loud warning).
    """
    global _SSL_CTX, _SSL_WARNED
    if _SSL_CTX is not None:
        return _SSL_CTX

    candidates = []
    if Path("/etc/ssl/cert.pem").exists():
        candidates.append(("macOS /etc/ssl/cert.pem", ssl.create_default_context(cafile="/etc/ssl/cert.pem")))
    try:
        import certifi
        candidates.append(("certifi", ssl.create_default_context(cafile=certifi.where())))
    except ImportError:
        pass
    try:
        import subprocess, tempfile
        pem = subprocess.run(
            ["security", "find-certificate", "-a", "-p"],
            capture_output=True, timeout=15,
        ).stdout
        if b"BEGIN CERTIFICATE" in pem:
            f = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
            f.write(pem)
            f.close()
            candidates.append(("macOS Keychain roots", ssl.create_default_context(cafile=f.name)))
    except Exception:
        pass

    for label, ctx in candidates:
        try:
            with socket.create_connection((urlparse(BASE_URL).hostname, 443), timeout=10) as sock:
                ctx.wrap_socket(sock, server_hostname=urlparse(BASE_URL).hostname)
            _SSL_CTX = ctx
            return _SSL_CTX
        except Exception:
            continue

    _SSL_WARNED = True
    print(
        "WARNING: no working TLS verification strategy found; proceeding WITHOUT "
        "certificate verification (--insecure behavior).",
        file=sys.stderr, flush=True,
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    _SSL_CTX = ctx
    return _SSL_CTX


def fetch(url, etag=None, last_modified=None, timeout=30, retries=3):
    """GET with optional conditional-request headers. Returns (status, headers, body)."""
    last_err = None
    for attempt in range(retries):
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
        if etag:
            req.add_header("If-None-Match", etag)
        if last_modified:
            req.add_header("If-Modified-Since", last_modified)
        try:
            with urlopen(req, timeout=timeout, context=ssl_context()) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except HTTPError as e:
            if e.code == 304:
                return 304, {}, b""
            if e.code in (403, 404, 410):
                return e.code, {}, b""
            last_err = e
        except (URLError, TimeoutError, OSError) as e:
            last_err = e
        time.sleep(min(2 ** attempt, 5))
    raise RuntimeError(f"failed after {retries} tries: {url} ({last_err})")


def decode_text(data):
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def parse_listing(body):
    """Return [(raw_href, decoded_name)] for an autoindex page, skipping sort links."""
    html = body.decode("utf-8", "replace")
    out = []
    for href in re.findall(r'href="([^"]+)"', html):
        if href.startswith("?") or href == "../":
            continue
        out.append((href, unquote(href)))
    return out


# --------------------------------------------------------------------------- #
# Tracklist parsing
# --------------------------------------------------------------------------- #

def parse_track_line(line):
    """Parse one tracklist line -> (label, artist, title, remix) or None.

    Handles 'NN. Artist - Title (Some Remix)' plus the quirks found on the
    site: numbering that restarts mid-file, missing separators, trailing
    annotations like [!], en/em-dashes.
    """
    m = TRACK_NUM_RE.match(line) or TRACK_NUM_DASH_RE.match(line)
    if not m:
        return None
    label, rest = m.group(1), m.group(2)

    artist, title = "", rest
    m2 = ARTIST_TITLE_RE.match(rest)
    if m2:
        artist, title = m2.group(1).strip(), m2.group(2).strip()

    remix = ""
    for pm in reversed(list(PAREN_RE.finditer(title))):
        inner = pm.group(1)
        if REMIX_WORDS_RE.search(inner):
            remix = inner.strip()
            title = (title[: pm.start()] + title[pm.end():]).strip()
            title = re.sub(r"\s{2,}", " ", title)
            break
    return label, artist, title, remix


def parse_tracklist(text):
    """Parse a whole tracklist file into ordered track dicts.

    Position comes from line order, not the numeric label — some files use a
    broken formatter whose numbers restart (01..09, 00, 01..09, 00, ...).
    """
    tracks, pos = [], 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parsed = parse_track_line(line)
        if parsed:
            pos += 1
            label, artist, title, remix = parsed
            tracks.append(
                {"position": pos, "label": label, "artist": artist, "title": title, "remix": remix}
            )
    return tracks


def pick_txt(entries, num):
    """Choose the best .txt in a folder (names sometimes don't match the set number)."""
    cands = [(href, name) for href, name in entries if name.lower().endswith(".txt")]
    if not cands:
        return None

    def score(item):
        low = item[1].lower()
        s = 0
        if "tracklist" in low:
            s -= 4
        if f"kick start {num:03d}" in low:
            s -= 2
        elif f"kick start {num}" in low:
            s -= 1
        return s

    return sorted(cands, key=score)[0][0]


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

def connect_db(path):
    con = sqlite3.connect(str(path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def create_schema(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS sets (
            number      INTEGER PRIMARY KEY,
            title       TEXT NOT NULL,
            url         TEXT NOT NULL,
            mp3_url     TEXT,
            mp3_name    TEXT,
            txt_url     TEXT,
            txt_name    TEXT,
            listing_lm  TEXT,
            listing_sha TEXT,
            listing_sha TEXT,
            txt_etag    TEXT,
            txt_lm      TEXT,
            raw_text    TEXT,
            track_count INTEGER NOT NULL DEFAULT 0,
            status      TEXT NOT NULL DEFAULT 'ok',
            note        TEXT,
            crawled_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tracks (
            id         INTEGER PRIMARY KEY,
            set_number INTEGER NOT NULL REFERENCES sets(number),
            position   INTEGER NOT NULL,
            label      TEXT,
            artist     TEXT NOT NULL DEFAULT '',
            title      TEXT NOT NULL,
            remix      TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_tracks_set ON tracks(set_number, position);
        CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
            artist, title, remix, set_title
        );
        """
    )
    # migration for indexes created before listing_sha existed
    cols = {r[1] for r in con.execute("PRAGMA table_info(sets)")}
    if "listing_sha" not in cols:
        con.execute("ALTER TABLE sets ADD COLUMN listing_sha TEXT")


def rebuild_fts(con):
    con.execute("DELETE FROM tracks_fts")
    con.execute(
        """
        INSERT INTO tracks_fts(rowid, artist, title, remix, set_title)
        SELECT t.id, t.artist, t.title, t.remix, s.title
        FROM tracks t JOIN sets s ON s.number = t.set_number
        """
    )


def save_set(con, res):
    tracks = res["tracks"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with con:
        con.execute(
            """
            INSERT INTO sets(number, title, url, mp3_url, mp3_name, txt_url, txt_name,
                             listing_lm, listing_sha, txt_etag, txt_lm, raw_text, track_count,
                             status, note, crawled_at)
            VALUES(:number, :title, :url, :mp3_url, :mp3_name, :txt_url, :txt_name,
                   :listing_lm, :listing_sha, :txt_etag, :txt_lm, :raw_text, :track_count,
                   :status, :note, :crawled_at)
            ON CONFLICT(number) DO UPDATE SET
                title=excluded.title, url=excluded.url, mp3_url=excluded.mp3_url,
                mp3_name=excluded.mp3_name, txt_url=excluded.txt_url,
                txt_name=excluded.txt_name, listing_lm=excluded.listing_lm,
                listing_sha=excluded.listing_sha,
                txt_etag=excluded.txt_etag, txt_lm=excluded.txt_lm,
                raw_text=excluded.raw_text, track_count=excluded.track_count,
                status=excluded.status, note=excluded.note,
                crawled_at=excluded.crawled_at
            """,
            {
                "number": res["number"],
                "title": f"Kick Start {res['number']:03d}",
                "url": res["url"],
                "mp3_url": res["mp3_url"],
                "mp3_name": res["mp3_name"],
                "txt_url": res["txt_url"],
                "txt_name": res["txt_name"],
                "listing_lm": res["listing_lm"],
                "listing_sha": res["listing_sha"],
                "txt_etag": res["txt_etag"],
                "txt_lm": res["txt_lm"],
                "raw_text": res["raw_text"],
                "track_count": len(tracks),
                "status": res["status"],
                "note": res["note"],
                "crawled_at": now,
            },
        )
        con.execute("DELETE FROM tracks WHERE set_number=?", (res["number"],))
        con.executemany(
            "INSERT INTO tracks(set_number, position, label, artist, title, remix)"
            " VALUES(?,?,?,?,?,?)",
            [
                (res["number"], t["position"], t["label"], t["artist"], t["title"], t["remix"])
                for t in tracks
            ],
        )


# --------------------------------------------------------------------------- #
# Crawler
# --------------------------------------------------------------------------- #

def crawl_set(num, href, base, prev, delay, force=False):
    """Fetch one set folder. Returns a result dict; never raises (errors -> status)."""
    folder_url = base.rstrip("/") + "/" + href
    try:
        status, headers, body = fetch(folder_url)
    except RuntimeError as e:
        return {
            "number": num, "url": folder_url, "mp3_url": None, "mp3_name": None,
            "txt_url": None, "txt_name": None, "listing_lm": None,
            "listing_sha": None, "txt_etag": None, "txt_lm": None,
            "raw_text": None, "tracks": [], "status": "error", "note": str(e)[:200],
        }
    if status == 404:
        return {
            "number": num, "url": folder_url, "mp3_url": None, "mp3_name": None,
            "txt_url": None, "txt_name": None, "listing_lm": None,
            "listing_sha": None, "txt_etag": None, "txt_lm": None,
            "raw_text": None, "tracks": [], "status": "missing", "note": "404 — folder listed but gone",
        }

    listing_lm = headers.get("last-modified")
    listing_sha = hashlib.sha256(body).hexdigest()

    # Skip sets whose directory listing is byte-identical to the last crawl
    # (nginx autoindex sends no Last-Modified/ETag, so compare content).
    if (
        not force
        and prev
        and prev["listing_sha"]
        and prev["listing_sha"] == listing_sha
        and prev["status"] in ("ok", "empty", "no_txt", "missing")
    ):
        return {"number": num, "skip": True, "track_count": prev["track_count"]}

    time.sleep(delay)
    entries = parse_listing(body)
    mp3_href = next(
        (h for h, n in sorted(entries, key=lambda x: x[1]) if n.lower().endswith(".mp3")), None
    )
    txt_href = pick_txt(entries, num)

    mp3_url = folder_url + mp3_href if mp3_href else None
    txt_url = folder_url + txt_href if txt_href else None
    mp3_name = unquote(mp3_href) if mp3_href else None
    txt_name = unquote(txt_href) if txt_href else None

    raw_text, txt_etag, txt_lm, note = None, None, None, None
    if txt_url:
        time.sleep(delay)
        try:
            tstatus, theaders, tbody = fetch(
                txt_url, etag=prev["txt_etag"] if prev else None,
                last_modified=prev["txt_lm"] if prev else None,
            )
            if tstatus == 304:
                raw_text, txt_etag, txt_lm = prev["raw_text"], prev["txt_etag"], prev["txt_lm"]
            elif tstatus in (404,):
                note = "tracklist txt 404"
            else:
                raw_text = decode_text(tbody)
                txt_etag = theaders.get("etag")
                txt_lm = theaders.get("last-modified")
        except RuntimeError as e:
            note = f"txt fetch failed: {e}"[:200]

    tracks = parse_tracklist(raw_text) if raw_text else []
    if tracks:
        tstatus = "ok"
    elif raw_text:
        tstatus = "empty"
        note = note or "txt present but no numbered tracks parsed"
    elif txt_url:
        tstatus = "no_txt"
        note = note or "txt unreachable"
    else:
        tstatus = "no_txt"
        note = note or "no txt file in folder"

    return {
        "number": num, "url": folder_url, "mp3_url": mp3_url, "mp3_name": mp3_name,
        "txt_url": txt_url, "txt_name": txt_name, "listing_lm": listing_lm,
        "listing_sha": listing_sha, "txt_etag": txt_etag, "txt_lm": txt_lm,
        "raw_text": raw_text, "tracks": tracks, "status": tstatus, "note": note,
    }


def cmd_crawl(args):
    db_path = Path(args.db)
    if args.reset and db_path.exists():
        db_path.unlink()
    con = connect_db(db_path)
    create_schema(con)

    print(f"Index: {db_path}", flush=True)
    print(f"Fetching directory index: {args.base}", flush=True)
    status, _, body = fetch(args.base)
    entries = parse_listing(body)
    folders = []
    for href, name in entries:
        m = SET_DIR_RE.match(name[:-1] if name.endswith("/") else name)
        if m and href.endswith("/"):
            folders.append((int(m.group(1)), href))
    folders.sort()
    print(f"Found {len(folders)} Kick Start folders", flush=True)
    if args.limit:
        folders = folders[: args.limit]
        print(f"Limited to first {args.limit} folders (--limit)", flush=True)

    prev_by_num = {}
    for row in con.execute("SELECT number, listing_lm, listing_sha, txt_etag, txt_lm, raw_text, status, track_count FROM sets"):
        prev_by_num[row["number"]] = dict(row)

    t0 = time.time()
    done = skipped = 0
    counts = {"ok": 0, "empty": 0, "no_txt": 0, "missing": 0, "error": 0}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                crawl_set, num, href, args.base,
                prev_by_num.get(num), args.delay, args.force,
            ): num
            for num, href in folders
        }
        for fut in as_completed(futures):
            num = futures[fut]
            try:
                res = fut.result()
            except Exception as e:  # defensive: workers should not raise
                print(f"  ! set {num:03d}: unexpected error {e}", flush=True)
                continue
            if res.get("skip"):
                skipped += 1
            else:
                save_set(con, res)
                counts[res["status"]] = counts.get(res["status"], 0) + 1
            done += 1
            if done % 25 == 0 or done == len(folders):
                print(
                    f"  {done}/{len(folders)} folders "
                    f"({done - skipped} fetched, {skipped} unchanged)",
                    flush=True,
                )

    print("Rebuilding full-text index...", flush=True)
    with con:
        rebuild_fts(con)
    con.execute("PRAGMA optimize")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    n_sets = con.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    n_tracks = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    print(
        f"Done in {time.time() - t0:.0f}s: {n_sets} sets, {n_tracks} tracks indexed "
        f"(this run: {counts['ok']} ok, {counts['no_txt']} without tracklist, "
        f"{counts['missing']} missing, {counts['error']} errors, {skipped} unchanged)",
        flush=True,
    )


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def fts_query(q):
    """Build a safe FTS5 MATCH expression: quoted phrases exact, other words prefixed."""
    parts = []
    for phrase in re.findall(r'"([^"]*)"', q):
        toks = TOKEN_RE.findall(phrase)
        if toks:
            parts.append('"' + " ".join(toks) + '"')
    remaining = re.sub(r'"[^"]*"', " ", q)
    for tok in TOKEN_RE.findall(remaining):
        parts.append(f'"{tok}"*')
    return " ".join(parts)


def search(con, q, limit=400):
    expr = fts_query(q)
    tokens = TOKEN_RE.findall(q)
    result = {"q": q, "expr": expr, "tokens": tokens, "total_hits": 0,
              "set_count": 0, "sets": [], "tracks": [], "elapsed_ms": None}
    if not expr:
        return result

    t0 = time.time()
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT t.id, t.set_number, s.title AS set_title, s.url AS set_url, s.mp3_url,
               t.position, t.label, t.artist, t.title, t.remix,
               bm25(tracks_fts) AS rank
        FROM tracks_fts
        JOIN tracks t ON t.id = tracks_fts.rowid
        JOIN sets   s ON s.number = t.set_number
        WHERE tracks_fts MATCH ?
        ORDER BY rank, t.set_number, t.position
        LIMIT ?
        """,
        (expr, limit),
    ).fetchall()

    counts = cur.execute(
        """
        WITH m AS MATERIALIZED (
            SELECT t.set_number AS set_number, bm25(tracks_fts) AS rank
            FROM tracks_fts
            JOIN tracks t ON t.id = tracks_fts.rowid
            WHERE tracks_fts MATCH ?
        )
        SELECT set_number, COUNT(*) AS hits, MIN(rank) AS best
        FROM m
        GROUP BY set_number
        ORDER BY best
        LIMIT 100
        """,
        (expr,),
    ).fetchall()

    set_rows = {r["set_number"]: r for r in counts}
    if set_rows:
        placeholders = ",".join("?" * len(set_rows))
        meta = con.execute(
            f"SELECT number, title, url, mp3_url, track_count FROM sets WHERE number IN ({placeholders})"
            " ORDER BY number",
            tuple(set_rows),
        ).fetchall()
        for m in meta:
            c = set_rows[m["number"]]
            result["sets"].append({
                "number": m["number"], "title": m["title"], "url": m["url"],
                "mp3_url": m["mp3_url"], "track_count": m["track_count"], "hits": c["hits"],
            })

    result["tracks"] = [
        {
            "id": r["id"], "set_number": r["set_number"], "set_title": r["set_title"],
            "set_url": r["set_url"], "position": r["position"], "label": r["label"],
            "artist": r["artist"], "title": r["title"], "remix": r["remix"],
        }
        for r in rows
    ]
    result["total_hits"] = cur.execute(
        """
        SELECT COUNT(*) FROM tracks_fts
        JOIN tracks t ON t.id = tracks_fts.rowid
        WHERE tracks_fts MATCH ?
        """,
        (expr,),
    ).fetchone()[0]
    result["set_count"] = len(result["sets"])
    result["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
    return result


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #

VERBOSE = False


class Handler(BaseHTTPRequestHandler):
    server_version = "KickStartSearch/1.0"
    protocol_version = "HTTP/1.1"
    db = None  # set in cmd_serve
    db_path = None  # set in cmd_serve

    def log_message(self, fmt, *args):
        if VERBOSE:
            super().log_message(fmt, *args)

    # -- helpers ------------------------------------------------------------
    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, ctype):
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        try:
            self._route()
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass

    def _route(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self.send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/api/search":
            q = (parse_qs(parsed.query).get("q", [""])[0] or "").strip()
            try:
                limit = max(1, min(1000, int(parse_qs(parsed.query).get("limit", ["400"])[0])))
            except ValueError:
                limit = 400
            res = search(self.db, q, limit)
            if re.fullmatch(r"\d{1,3}", q):
                num = int(q)
                row = self.db.execute("SELECT * FROM sets WHERE number=?", (num,)).fetchone()
                if row:
                    tracks = self.db.execute(
                        "SELECT position, label, artist, title, remix FROM tracks"
                        " WHERE set_number=? ORDER BY position",
                        (num,),
                    ).fetchall()
                    res["set_direct"] = {
                        "number": row["number"], "title": row["title"], "url": row["url"],
                        "mp3_url": row["mp3_url"], "track_count": row["track_count"],
                        "tracks": [dict(t) for t in tracks],
                    }
            self.send_json(res)
        elif (m := re.fullmatch(r"/api/set/(\d+)", path)):
            num = int(m.group(1))
            row = self.db.execute("SELECT * FROM sets WHERE number=?", (num,)).fetchone()
            if not row:
                self.send_json({"error": f"set {num} not indexed"}, 404)
                return
            tracks = self.db.execute(
                "SELECT position, label, artist, title, remix FROM tracks"
                " WHERE set_number=? ORDER BY position",
                (num,),
            ).fetchall()
            self.send_json({
                "number": row["number"], "title": row["title"], "url": row["url"],
                "mp3_url": row["mp3_url"], "txt_url": row["txt_url"],
                "track_count": row["track_count"], "status": row["status"],
                "tracks": [dict(t) for t in tracks],
            })
        elif path == "/api/sets":
            rows = self.db.execute(
                "SELECT number, title, url, mp3_url, track_count, status FROM sets ORDER BY number"
            ).fetchall()
            self.send_json({"sets": [dict(r) for r in rows]})
        elif path == "/api/stats":
            one = self.db.execute(
                "SELECT COUNT(*) AS n, MAX(crawled_at) AS crawled FROM sets"
            ).fetchone()
            two = self.db.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()
            self.send_json({
                "sets": one["n"], "tracks": two["n"], "crawled_at": one["crawled"],
                "db": str(self.db_path),
            })
        else:
            self.send_json({"error": "not found"}, 404)


def cmd_serve(args):
    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"No index at {db_path}\nRun first:  python3 {Path(__file__).name} crawl")
    global VERBOSE
    VERBOSE = args.verbose
    Handler.db = connect_db(db_path)
    Handler.db_path = db_path
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        sys.exit(f"Cannot bind {args.host}:{args.port} — {e}")
    httpd.daemon_threads = True
    n_tracks = Handler.db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    n_sets = Handler.db.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    print(f"Serving {n_sets} sets / {n_tracks} tracks", flush=True)
    print(f"Open http://{args.host}:{args.port}/   (Ctrl-C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")


def cmd_stats(args):
    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"No index at {db_path}\nRun first:  python3 {Path(__file__).name} crawl")
    con = connect_db(db_path)
    n_sets = con.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    n_tracks = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    crawled = con.execute("SELECT MAX(crawled_at) FROM sets").fetchone()[0]
    artists = con.execute("SELECT COUNT(DISTINCT artist) FROM tracks WHERE artist != ''").fetchone()[0]
    print(f"db       : {db_path}")
    print(f"sets     : {n_sets}")
    print(f"tracks   : {n_tracks}")
    print(f"artists  : {artists}")
    print(f"crawled  : {crawled}")


# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Tiny local search engine for sets.dfx.at/Kick Start")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("crawl", help="build or incrementally update the search index")
    pc.add_argument("--db", default=str(DEFAULT_DB), help=f"sqlite db path (default {DEFAULT_DB})")
    pc.add_argument("--base", default=BASE_URL, help="archive root URL")
    pc.add_argument("--limit", type=int, default=None, help="only crawl first N folders (testing)")
    pc.add_argument("--workers", type=int, default=6, help="parallel fetchers (default 6)")
    pc.add_argument("--delay", type=float, default=0.05, help="politeness delay per request, seconds")
    pc.add_argument("--force", action="store_true", help="refetch tracklists even when folder listings look unchanged")
    pc.add_argument("--reset", action="store_true", help="delete the index and crawl from scratch")

    ps = sub.add_parser("serve", help="run the local search web UI")
    ps.add_argument("--db", default=str(DEFAULT_DB), help=f"sqlite db path (default {DEFAULT_DB})")
    ps.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ps.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    ps.add_argument("--verbose", action="store_true", help="log every request")

    pt = sub.add_parser("stats", help="show index statistics")
    pt.add_argument("--db", default=str(DEFAULT_DB), help=f"sqlite db path (default {DEFAULT_DB})")

    args = ap.parse_args()
    {"crawl": cmd_crawl, "serve": cmd_serve, "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    main()
