"""Release years from music databases, asked before any AI service.

MusicBrainz (no key needed) and Discogs (needs a personal token, or an app's
consumer key and secret: see `radioserver install_discogs`) are both searched, and the earliest year either one
has for a matching recording wins: reissues and compilations only ever make a
song look newer than it is. Only when neither database knows the song do the
AI services get asked.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

USER_AGENT = "radioserver/1.0 ( https://github.com/priestc/radioserver )"
TIMEOUT = 6               # seconds per request
MB_MIN_INTERVAL = 1.1     # MusicBrainz allows one request per second
DISCOGS_CHECK = 4         # how many of the earliest Discogs candidates to open and check
DISCOGS_RETRIES = 3       # Discogs allows 60 requests a minute; a 429 means wait and retry
EARLIEST_YEAR = 1888
ARTIST_CACHE_TTL = 24 * 3600   # an artist's MusicBrainz ID and catalogue are reused for a day

# Shared by every gunicorn worker and management command, so lookups running side by side
# still keep to MusicBrainz's rate limit and reuse each other's artist data.
STATE_DIR = Path.home() / ".cache" / "radioserver" / "lookups"

# Requests go through the service delegator's logging proxy, which records how
# long each one takes: https://musicbrainz.org/ws/2/x becomes
# <delegator>/proxy/radioserver/musicbrainz/x.
_PROXIED = {
    "https://musicbrainz.org/ws/2/": "musicbrainz",
    "https://api.discogs.com/": "discogs",
}


def _proxied(url: str) -> str:
    for base, service in _PROXIED.items():
        if url.startswith(base):
            return f"{settings.SERVICE_DELEGATOR_URL.rstrip('/')}/proxy/radioserver/{service}/{url[len(base):]}"
    return url


class LookupError_(Exception):
    """A database couldn't be asked (network, HTTP error, bad response)."""


def _get(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(_proxied(url), headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                         "X-Task": "song-year", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise LookupError_(f"HTTP {e.code} {e.reason}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LookupError_(f"couldn't connect ({getattr(e, 'reason', e)})")
    except ValueError:
        raise LookupError_("unreadable response")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("&", "and"))


def _title_key(s: str) -> str:
    """Compare titles without bracketed extras: "I Need Money (Keep Your Alibis)" ~ "I Need Money"."""
    return _norm(re.sub(r"\([^)]*\)|\[[^\]]*\]", "", s)) or _norm(s)


def _main_artist(artist: str) -> str:
    """The lead act: "Moon Mullican & His Blue Ridge Playboys" -> "Moon Mullican"."""
    return re.split(r"\s+(?:&|and|with|feat\.?|featuring|w/)\s+|,|\(", artist, flags=re.I)[0].strip() or artist


def _artist_key(artist: str) -> str:
    """The most distinctive word of the lead act, which any credit for it should contain."""
    words = [w for w in (_norm(x) for x in _main_artist(artist).split()) if w and w != "the"]
    return max(words, key=len) if words else _norm(artist)


def _year(s) -> int | None:
    m = re.match(r"(\d{4})", str(s or ""))
    y = int(m.group(1)) if m else None
    return y if y and EARLIEST_YEAR <= y <= time.localtime().tm_year else None


def _lucene(s: str) -> str:
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s)


MB_MAX_RELEASE_GROUPS = 500   # per artist; enough for anyone but the most prolific


def _locked_file(name: str):
    """An exclusively locked file in STATE_DIR (released when closed). Locks threads and processes alike."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(STATE_DIR / name, "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def _mb_wait_turn() -> None:
    """Wait until MB_MIN_INTERVAL has passed since the last MusicBrainz request from any process."""
    with _locked_file("musicbrainz.throttle") as f:
        f.seek(0)
        try:
            last = float(f.read() or 0)
        except ValueError:
            last = 0.0
        wait = MB_MIN_INTERVAL - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        f.seek(0)
        f.truncate()
        f.write(str(time.time()))


def _mb(path: str, **params) -> dict:
    """One MusicBrainz request, keeping to its one-request-per-second limit."""
    url = f"https://musicbrainz.org/ws/2/{path}?" + urllib.parse.urlencode({**params, "fmt": "json"})
    for attempt in range(3):
        _mb_wait_turn()
        try:
            return _get(url)
        except LookupError_ as e:
            if "HTTP 503" not in str(e) or attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))     # MusicBrainz's "slow down"


def _cached(key: str, fetch):
    """fetch()'s result, reused for ARTIST_CACHE_TTL. Lookups wanting the same key wait for one fetch."""
    name = hashlib.sha1(key.encode()).hexdigest()
    with _locked_file(name + ".lock"):
        path = STATE_DIR / (name + ".json")
        try:
            if time.time() - path.stat().st_mtime < ARTIST_CACHE_TTL:
                return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        value = fetch()     # errors aren't cached: the next lookup tries again
        path.write_text(json.dumps(value))
        return value


def _original_issue(rg: dict) -> bool:
    """A single, EP or album in its own right: not a compilation, live album, soundtrack, etc."""
    return rg.get("primary-type") in ("Single", "EP", "Album") and not rg.get("secondary-types")


def _mb_artist_ids(artist: str) -> list[str]:
    """MusicBrainz IDs for the artist as credited, or for its lead act."""
    def fetch():
        names = {_norm(artist), _norm(_main_artist(artist))}
        found = _mb("artist", query=f'artist:"{_lucene(_main_artist(artist))}"', limit=10).get("artists", [])
        return [a["id"] for a in found
                if _norm(a.get("name", "")) in names or any(_norm(x.get("name", "")) in names for x in a.get("aliases", []))][:2]
    return _cached(f"mb-artist:{_norm(artist)}", fetch)


def _mb_original_issues(aid: str) -> list[tuple[str, str]]:
    """(title, first release date) of each of an artist's own singles, EPs and albums."""
    def fetch():
        out, offset = [], 0
        while offset < MB_MAX_RELEASE_GROUPS:
            page = _mb("release-group", artist=aid, type="single|album|ep", limit=100, offset=offset)
            out += [(rg.get("title", ""), rg.get("first-release-date") or "")
                    for rg in page.get("release-groups", []) if _original_issue(rg)]
            offset += 100
            if offset >= page.get("release-group-count", 0):
                break
        return out
    return _cached(f"mb-release-groups:{aid}", fetch)


def musicbrainz_year(title: str, artist: str) -> int | None:
    """Year of the earliest original (non-compilation) release by this artist that has this song.

    1. The artist's own singles and albums (listed in full, so the answer doesn't depend on
       search ranking) with the song as the title, or as the A- or B-side ("A / B").
    2. The artist's recordings of the song, counting only non-compilation releases (this finds
       album tracks, and albums that came out before the single).
    Compilations are ignored throughout: their dates are when they were compiled, not when
    the song came out.
    """
    want = _title_key(title)
    ids = _mb_artist_ids(artist)
    if not ids:
        return None
    years = []
    for aid in ids:
        for rg_title, date in _mb_original_issues(aid):
            if want in [_title_key(x) for x in rg_title.split(" / ")] and (y := _year(date)):
                years.append(y)
    arids = " OR ".join(f"arid:{i}" for i in ids)
    for rec in _mb("recording", query=f'recording:"{_lucene(title)}" AND ({arids})', limit=100).get("recordings", []):
        if _title_key(rec.get("title", "")) != want:
            continue
        for rel in rec.get("releases", []):
            if _original_issue(rel.get("release-group", {})) and (y := _year(rel.get("date"))):
                years.append(y)
    return min(years) if years else None


def _discogs_auth() -> dict | None:
    """Discogs auth header: a personal token, or an app's consumer key and secret."""
    token = getattr(settings, "DISCOGS_TOKEN", "")
    if token:
        return {"Authorization": f"Discogs token={token}"}
    key, secret = getattr(settings, "DISCOGS_KEY", ""), getattr(settings, "DISCOGS_SECRET", "")
    if key and secret:
        return {"Authorization": f"Discogs key={key}, secret={secret}"}
    return None


def _discogs(url: str, auth: dict) -> dict:
    """One Discogs request, waiting and retrying when it says we've made too many."""
    for attempt in range(DISCOGS_RETRIES):
        try:
            return _get(url, auth)
        except LookupError_ as e:
            if "HTTP 429" not in str(e) or attempt == DISCOGS_RETRIES - 1:
                raise
            time.sleep(3 * (attempt + 1))


def discogs_year(title: str, artist: str) -> int | None:
    """Earliest year of a Discogs master (or release) by this artist whose tracklist has this song.
    Compilations are skipped, as with MusicBrainz."""
    auth = _discogs_auth()
    if not auth:
        return None
    want_title, want_artist = _title_key(title), _artist_key(artist)
    for kind in ("master", "release"):
        url = "https://api.discogs.com/database/search?" + urllib.parse.urlencode(
            {"type": kind, "artist": _main_artist(artist), "track": title, "per_page": 50})
        results = _discogs(url, auth).get("results", [])
        by_artist = [r for r in results if want_artist in _norm(r.get("title", "").split(" - ")[0])]
        comp_years = [y for r in by_artist if "Compilation" in (r.get("format") or []) and (y := _year(r.get("year")))]
        cands = sorted(
            ((y, r) for r in results
             if (y := _year(r.get("year"))) and want_artist in _norm(r.get("title", "").split(" - ")[0])
             and "Compilation" not in (r.get("format") or [])),   # a compilation's year isn't the song's
            key=lambda c: c[0],
        )
        for y, r in cands[:DISCOGS_CHECK]:
            detail = _discogs(r["resource_url"], auth)
            if any(_title_key(t.get("title", "")) == want_title for t in detail.get("tracklist", [])):
                # Compilations with the song before this "original" mean it's really a reissue,
                # and the true original isn't listed: don't guess.
                return None if comp_years and min(comp_years) < y else y
    return None


def database_year(title: str, artist: str) -> tuple[int | None, str, list[str]]:
    """Ask MusicBrainz and Discogs. Returns (earliest year found, which database(s) said so, notes)."""
    found, notes = {}, []
    sources = [("MusicBrainz", musicbrainz_year)]
    if _discogs_auth():
        sources.append(("Discogs", discogs_year))
    else:
        notes.append("Discogs: not set up (radioserver install_discogs)")

    def run(fn):
        """One database's lookup on its own thread: the year, None, or the error."""
        try:
            return fn(title, artist)
        except LookupError_ as e:
            return e

    # Both databases at once: a track costs the slower of the two, not both added together.
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        futures = [(name, pool.submit(run, fn)) for name, fn in sources]
    for name, future in futures:
        y = future.result()
        failed = isinstance(y, LookupError_)
        if failed:
            notes.append(f"{name}: {y}")
            continue
        if y:
            found[name] = y
        else:
            notes.append(f"{name}: not found")
    if not found:
        return None, "", notes
    year = min(found.values())
    agree = [n for n, y in found.items() if y == year]
    others = [f"{n} has {y}" for n, y in found.items() if y != year]
    return year, " + ".join(agree) + (f" (earliest; {', '.join(others)})" if others else ""), notes


def is_show_talk(track) -> bool:
    """Host talk on a radio-show album (Theme Time Radio Hour): it gets the air year, not a release year."""
    album = track.album
    if not album or not album.title.startswith("Theme Time Radio Hour"):
        return False
    names = list(track.artists.values_list("name", flat=True))
    return names == [album.artist.name]


def find_release_year(title: str, artist: str, preferred_backend: str = "") -> tuple[int | None, str, list[str]]:
    """Release year from the music databases, falling back to AI services that check each other.

    Same return shape as library.ai.lookup_year_with_fallback: (year or None, source, notes).
    """
    from library.ai import LOOKUP_BUDGET, lookup_year_with_fallback

    started = time.monotonic()
    year, source, notes = database_year(title, artist)
    if year:
        return year, source, notes
    budget = LOOKUP_BUDGET - (time.monotonic() - started)
    year, source, ai_notes = lookup_year_with_fallback(title, artist, preferred_backend=preferred_backend, budget=budget)
    return year, source, notes + ai_notes
