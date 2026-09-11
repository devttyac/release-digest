#!/usr/bin/env python3
"""Fetch a month's games/movies/TV releases from RAWG and TMDB.

Usage:
    .venv/bin/python fetch_releases.py [YYYY-MM] [--out PATH]

Writes a single JSON file with three ranked lists (games, movies, tv),
each sorted latest-first by release date, with a `top_pick` flag on the
single highest-popularity item per category. Reads TMDB_API_KEY and
RAWG_API_KEY from a .env file next to this script — never pass keys as
CLI arguments, and never print their values.
"""

import argparse
import calendar
import html
import re
import datetime
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

REQUEST_TIMEOUT = 20  # seconds — an unattended monthly run must not hang forever
SCRIPT_DIR = Path(__file__).resolve().parent


def clean_description(text: str) -> str:
    """Normalise a description from either source into plain prose.

    Handles HTML (RAWG's `description` field is HTML), leftover markdown
    artifacts that store-page copy carries into `description_raw`, HTML
    entities, and newline/whitespace noise.
    """
    text = text or ""
    # Turn block boundaries into spaces before dropping tags, so words either
    # side of a <br> or </p> don't get glued together.
    text = re.sub(r"<\s*br\s*/?\s*>|</\s*(p|div|li)\s*>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[#*_`]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^[.\-\s]+", "", text)


def trim_overview(text: str, limit: int = 220) -> str:
    """Trim to a word boundary near `limit` chars.

    Stores a generous slice — the renderer clamps it further for display, so
    presentation length is a layout decision, not baked into the data. Never
    split on '. ': abbreviations like 'U.S.' or 'Dr.' produce garbage fragments
    ('When a U.S', '...and Dr').
    """
    text = clean_description(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


class BadMonth(ValueError):
    """A malformed month argument, reported as a message rather than a traceback."""


def target_month(arg: str | None) -> tuple[str, str]:
    """Return (first_day, last_day) as YYYY-MM-DD for the target month.

    The month can arrive from DIGEST_MONTH in a container environment, where a
    typo would otherwise surface as a bare traceback in the logs.
    """
    if arg:
        try:
            year, month = (int(part) for part in arg.split("-", 1))
            first_day = datetime.date(year, month, 1)
        except ValueError as exc:
            raise BadMonth(f"invalid month {arg!r} — expected YYYY-MM ({exc})") from exc
    else:
        today = datetime.date.today()
        year, month = today.year, today.month
        first_day = datetime.date(year, month, 1)
    last_day = datetime.date(year, month, calendar.monthrange(year, month)[1])
    return first_day.isoformat(), last_day.isoformat()


ANIMATION_GENRE = 16


def hentai_keyword_id(api_key: str) -> int | None:
    """Resolve TMDB's 'hentai' keyword id, for excluding explicit titles.

    Looked up rather than hardcoded so it can't silently rot if the id changes.
    Only 'hentai' is blocked — deliberately not 'ecchi', which denotes
    fanservice rather than explicit content and is applied to many mainstream
    titles; blocking it would remove legitimate releases invisibly.

    include_adult=false already runs on every query, but TMDB only sets that
    flag on outright pornographic entries, so it misses most hentai series.
    """
    try:
        resp = requests.get(
            "https://api.themoviedb.org/3/search/keyword",
            params={"api_key": api_key, "query": "hentai"},
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not resp.ok:
        return None
    for k in data.get("results", []):
        if k.get("name", "").lower() == "hentai":
            return k.get("id")
    return None


def fetch_tmdb(kind: str, api_key: str, first_day: str, last_day: str,
               anime: bool = False, exclude_keyword: int | None = None) -> tuple[list[dict], str | None]:
    """Fetch TMDB /discover/movie or /discover/tv. Returns (items, error).

    With anime=True, restricts to Japanese-language animation. The two filters
    are ANDed by TMDB, so live-action Japanese titles cannot match — only
    genre 16 does. That heuristic trades false negatives for precision: it
    misses anime that isn't Japanese-language (co-productions, English-original
    anime-style shows) rather than polluting the section with everything
    animated.
    """
    date_field = "primary_release_date" if kind == "movie" else "first_air_date"
    params = {
        "api_key": api_key,
        "sort_by": "popularity.desc",
        "include_adult": "false",
        f"{date_field}.gte": first_day,
        f"{date_field}.lte": last_day,
    }
    if anime:
        params["with_genres"] = ANIMATION_GENRE
        params["with_original_language"] = "ja"
    if exclude_keyword:
        params["without_keywords"] = exclude_keyword
    if kind == "movie" and not anime:
        # Anime films rarely get a wide US theatrical run, so applying the
        # region/release-type filter to them would exclude nearly all of them.
        params["region"] = "US"
        params["with_release_type"] = "2|3"  # theatrical (limited + wide)

    try:
        resp = requests.get(
            f"https://api.themoviedb.org/3/discover/{kind}",
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return [], f"TMDB {kind} request failed: {exc}"

    try:
        data = resp.json()
    except ValueError:
        return [], f"TMDB {kind} returned non-JSON response (HTTP {resp.status_code})"

    # TMDB returns HTTP 200 with success:false on auth/param errors — this is NOT
    # the same as a genuinely quiet month (empty results with success omitted/true).
    if data.get("success") is False:
        return [], f"TMDB {kind} API error: {data.get('status_message', 'unknown error')}"
    if not resp.ok:
        return [], f"TMDB {kind} HTTP {resp.status_code}: {data.get('status_message', resp.text[:200])}"

    items = []
    for r in data.get("results", []):
        title = r.get("title") or r.get("name")
        release_date = r.get(date_field) or r.get("release_date") or r.get("first_air_date")
        if not title or not release_date:
            continue
        poster_path = r.get("poster_path")
        tmdb_id = r.get("id")
        items.append({
            "title": title,
            "release_date": release_date,
            "popularity": r.get("popularity", 0),
            # Links straight to the title's page on the source site.
            "url": f"https://www.themoviedb.org/{kind}/{tmdb_id}" if tmdb_id else None,
            "tmdb_id": tmdb_id,
            "kind": kind,
            # The anime section merges films and series, so each card says which.
            "format": "Film" if kind == "movie" else "Series",
            "original_language": r.get("original_language"),
            "overview": trim_overview(r.get("overview")),
            # w185 — posters display at 64x96, so this is already 2x for retina.
            # Larger sizes bloat the email badly once images are embedded inline.
            "image": f"https://image.tmdb.org/t/p/w185{poster_path}" if poster_path else None,
        })
    return items, None


def rawg_thumb(url: str | None) -> str | None:
    """Ask RAWG's CDN for a 320px-wide variant instead of the full-size artwork.

    RAWG serves originals that can run several hundred KB each — fine as a
    remote <img>, far too heavy once every poster is embedded in the message.
    Their media CDN resizes via a `/media/resize/<width>/-/` path segment.

    Only certain widths exist: 200, 420, 640 and 1280 are served; 320 redirects
    to a dead URL. 420 is ~20KB against ~168KB for the original, and still well
    above the 96px the thumbnail renders at.
    """
    if not url or "/media/resize/" in url:
        return url
    return url.replace("/media/", "/media/resize/420/-/", 1)


def fetch_rawg(api_key: str, first_day: str, last_day: str) -> tuple[list[dict], str | None]:
    """Fetch RAWG /games. Returns (items, error)."""
    params = {
        "key": api_key,
        "dates": f"{first_day},{last_day}",
        "ordering": "-added",
        "page_size": 20,
    }
    try:
        resp = requests.get("https://api.rawg.io/api/games", params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        return [], f"RAWG request failed: {exc}"

    try:
        data = resp.json()
    except ValueError:
        return [], f"RAWG returned non-JSON response (HTTP {resp.status_code})"

    if not resp.ok:
        return [], f"RAWG HTTP {resp.status_code}: {data.get('detail') or data.get('error') or resp.text[:200]}"
    if "error" in data or "detail" in data:
        return [], f"RAWG API error: {data.get('detail') or data.get('error')}"

    items = []
    for r in data.get("results", []):
        name = r.get("name")
        released = r.get("released")
        if not name or not released:
            continue
        platforms = [p["platform"]["name"] for p in (r.get("platforms") or []) if p.get("platform", {}).get("name")]
        slug = r.get("slug")
        items.append({
            "id": r.get("id"),
            "title": name,
            "release_date": released,
            "popularity": r.get("added", 0),
            "url": f"https://rawg.io/games/{slug}" if slug else None,
            "image": rawg_thumb(r.get("background_image")),
            "platforms": platforms,
            "overview": "",  # RAWG's list endpoint has no description; filled in by enrich_game_descriptions
        })
    return items, None


def enrich_tmdb_overviews(items: list[dict], api_key: str) -> None:
    """Fill in `overview` for TMDB items whose English synopsis is empty.

    TMDB's discover response returns the English overview, which is routinely
    blank for foreign-language titles even though the site shows a synopsis —
    the text exists only as a translation. Falls back to the title's original
    language, then to whatever translation has one. Showing a French synopsis
    beats showing an empty card.

    Only called for the final, already-capped list, so this costs one request
    per genuinely-missing description rather than one per candidate.
    """
    for item in items:
        if item.get("overview") or not item.get("tmdb_id"):
            continue
        # Coerce rather than interpolate raw API values into a URL path — ids
        # are integers, and anything else has no business shaping the request.
        try:
            tmdb_id = int(item["tmdb_id"])
        except (TypeError, ValueError):
            continue
        kind = "tv" if item.get("kind") == "tv" else "movie"
        try:
            resp = requests.get(
                f"https://api.themoviedb.org/3/{kind}/{tmdb_id}/translations",
                params={"api_key": api_key},
                timeout=REQUEST_TIMEOUT,
            )
            data = resp.json()
        except (requests.RequestException, ValueError):
            continue  # an enhancement, not core data — skip quietly
        if not resp.ok:
            continue

        # `or ""` rather than a default arg: the key is often present with a
        # null value, and .strip() on None would raise.
        by_lang = {
            t.get("iso_639_1"): ((t.get("data") or {}).get("overview") or "").strip()
            for t in data.get("translations", [])
        }
        by_lang = {k: v for k, v in by_lang.items() if v}
        if not by_lang:
            continue

        original = item.get("original_language")
        for lang in ("en", original, *by_lang.keys()):
            if lang and by_lang.get(lang):
                item["overview"] = trim_overview(by_lang[lang])
                break


def enrich_game_descriptions(games: list[dict], api_key: str) -> None:
    """Fill in `overview` for each game via RAWG's per-game detail endpoint.

    The /games list endpoint has no description field at all — only /games/{id}
    does. Only called on the final, already-capped list (not all raw candidates)
    to keep this to a handful of extra requests, not one per candidate.
    """
    for game in games:
        if not game.get("id"):
            continue
        try:
            game_id = int(game["id"])
        except (TypeError, ValueError):
            continue
        try:
            resp = requests.get(
                f"https://api.rawg.io/api/games/{game_id}",
                params={"key": api_key},
                timeout=REQUEST_TIMEOUT,
            )
            data = resp.json()
        except (requests.RequestException, ValueError):
            continue  # description is an enhancement, not core data — skip quietly on failure
        if resp.ok:
            # description_raw is often empty even when the site shows an About
            # section — the text is only in `description`, which is HTML.
            # clean_description strips the markup.
            game["overview"] = trim_overview(data.get("description_raw") or data.get("description"))


def clip_to_range(items: list[dict], first_day: str, last_day: str) -> list[dict]:
    """Drop items whose actual release_date falls outside [first_day, last_day].

    TMDB's primary_release_date.gte/.lte filter matches a title's *global* primary
    release date, which can diverge from the region-specific date shown in the
    response (e.g. an earlier festival/digital date elsewhere puts a title inside
    the filter window while its displayed US date is a different month). The
    server-side filter alone is not trustworthy for "was this released this
    month" — re-check locally against the date we're actually going to display.
    """
    return [i for i in items if first_day <= i["release_date"] <= last_day]


def dedupe(items: list[dict]) -> list[dict]:
    """Collapse near-duplicate titles (same normalized name + date), keep the highest-popularity copy.

    RAWG in particular sometimes returns the same game twice under slightly
    different name strings (e.g. with/without punctuation) for different
    platform or regional listings.
    """
    def norm(title: str) -> str:
        return "".join(ch.lower() for ch in title if ch.isalnum())

    best: dict[tuple[str, str], dict] = {}
    for item in items:
        key = (norm(item["title"]), item["release_date"])
        current = best.get(key)
        if current is None or item["popularity"] > current["popularity"]:
            best[key] = item
    return list(best.values())


def rank(items: list[dict], cap: int = 10) -> list[dict]:
    """Sort latest-first by date; flag the single highest-popularity item as top_pick.

    top_pick is chosen from the FULL list before capping, then guaranteed a slot in
    the returned (capped) list even if its release date would otherwise put it
    outside the date-sorted window — capping by date must never silently drop the
    one item the whole ranking exists to surface.
    """
    if not items:
        return []
    top = max(items, key=lambda i: i["popularity"])
    capped = sorted(items, key=lambda i: i["release_date"], reverse=True)[:cap]
    if top not in capped:
        capped = capped[: cap - 1] + [top]
        capped.sort(key=lambda i: i["release_date"], reverse=True)
    for item in capped:
        item["top_pick"] = item is top

    # Rank within the category, most popular first. The cards are ordered by
    # date, so this is what lets a reader spot the notable ones at a glance.
    # Deliberately a rank and not the raw score: RAWG's `added` is a user count
    # while TMDB's `popularity` is an opaque daily-changing float, so the two
    # are meaningless to compare directly.
    for position, item in enumerate(sorted(capped, key=lambda i: i["popularity"], reverse=True), start=1):
        item["popularity_rank"] = position
    return capped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("month", nargs="?", default=None, help="Target month as YYYY-MM (default: current month)")
    parser.add_argument("--out", default="/tmp/release-digest-data.json", help="Output JSON path")
    args = parser.parse_args()

    load_dotenv(SCRIPT_DIR / ".env")
    tmdb_key = os.environ.get("TMDB_API_KEY")
    rawg_key = os.environ.get("RAWG_API_KEY")

    missing = [name for name, val in (("TMDB_API_KEY", tmdb_key), ("RAWG_API_KEY", rawg_key)) if not val or val.startswith("your_")]
    if missing:
        print(f"Missing/unset: {', '.join(missing)} — edit .env in {SCRIPT_DIR}", file=sys.stderr)
        return 2

    try:
        first_day, last_day = target_month(args.month)
    except BadMonth as exc:
        print(str(exc), file=sys.stderr)
        return 2

    errors = []
    block = hentai_keyword_id(tmdb_key)
    if block is None:
        errors.append("TMDB hentai keyword lookup failed — explicit titles filtered by include_adult only")

    movies, err = fetch_tmdb("movie", tmdb_key, first_day, last_day, exclude_keyword=block)
    if err:
        errors.append(err)
    tv, err = fetch_tmdb("tv", tmdb_key, first_day, last_day, exclude_keyword=block)
    if err:
        errors.append(err)
    games, err = fetch_rawg(rawg_key, first_day, last_day)
    if err:
        errors.append(err)

    # Anime spans both endpoints, so it needs two queries merged into one list.
    anime = []
    for kind in ("movie", "tv"):
        got, err = fetch_tmdb(kind, tmdb_key, first_day, last_day, anime=True, exclude_keyword=block)
        if err:
            errors.append(f"anime/{kind}: {err}")
        anime.extend(got)

    if not movies and not tv and not games:
        print("All three sources returned nothing or errored:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    def clean(cat_items: list[dict]) -> list[dict]:
        return rank(dedupe(clip_to_range(cat_items, first_day, last_day)))

    # Enrichment runs only on the final capped lists, so the extra requests are
    # a handful rather than one per raw candidate.
    games_final = clean(games)
    enrich_game_descriptions(games_final, rawg_key)

    # Anime gets its own section, so remove those titles from Movies and TV —
    # otherwise a popular anime film would occupy a slot in both places.
    anime_final = clean(anime)
    anime_ids = {i["tmdb_id"] for i in anime_final if i.get("tmdb_id")}
    movies_final = clean([m for m in movies if m.get("tmdb_id") not in anime_ids])
    tv_final = clean([t for t in tv if t.get("tmdb_id") not in anime_ids])

    for group in (movies_final, tv_final, anime_final):
        enrich_tmdb_overviews(group, tmdb_key)

    output = {
        # Always the canonical YYYY-MM. Taking args.month verbatim would let
        # `2026-9` through, which then names the archive files inconsistently.
        "month": first_day[:7],
        "first_day": first_day,
        "last_day": last_day,
        "games": games_final,
        "movies": movies_final,
        "tv": tv_final,
        "anime": anime_final,
        "errors": errors,
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(output, indent=2))
    print(f"Wrote {out_path} — games={len(output['games'])} movies={len(output['movies'])} "
          f"tv={len(output['tv'])} anime={len(output['anime'])}", file=sys.stderr)
    if errors:
        print("Partial failures:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
