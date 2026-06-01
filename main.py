import argparse
import asyncio
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import spotipy
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

load_dotenv()

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

DEFAULT_TIKTOK_DATA_FILE = "user_data_tiktok.json"
DEFAULT_PLAYLIST_NAME = "TikTok Favorites"
TIKTOK_CACHE_FILE = ".tiktok_cache.json"
SPOTIFY_CACHE_FILE = ".spotify_match_cache.json"

SKIP_PATTERNS = re.compile(
    r"original sound|original audio|\boriginal\b|access denied|"
    r"tiktok - make your day|visit tiktok|fyp|foryou",
    re.IGNORECASE,
)


@dataclass
class ResultRow:
    status: str
    source_sound: str
    track: str
    artist: str
    spotify_url: str
    spotify_uri: str
    spotify_id: str
    tiktok_url: str
    reason: str = ""


def load_json_file(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json_file(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def clean_sound_name(sound: str) -> str:
    sound = re.sub(r"\(.*?\)|\[.*?\]", "", sound).strip()
    return re.sub(r"\s+", " ", sound)


def sound_key(sound: str) -> str:
    return clean_sound_name(sound).casefold()


def load_tiktok_links(filepath: str) -> list[str]:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    try:
        entries = data["Likes and Favorites"]["Favorite Sounds"]["FavoriteSoundList"]
    except (KeyError, TypeError):
        print("[!] Could not find Likes and Favorites > Favorite Sounds > FavoriteSoundList.")
        print("    Make sure you exported TikTok data as JSON and selected Likes and Favorites.")
        sys.exit(1)

    links = [entry["Link"] for entry in entries if entry.get("Link")]
    print(f"Found {len(links)} favorited sound link(s).")
    return links


async def fetch_one(context, url: str) -> tuple[str, str | None]:
    page = await context.new_page()
    await Stealth().apply_stealth_async(page)

    try:
        await page.goto(url, timeout=15000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        og = await page.query_selector('meta[property="og:title"]')
        if og:
            content = await og.get_attribute("content")
            if is_usable_tiktok_title(content):
                await page.close()
                return url, clean_sound_name(content or "")

        for selector in ["h1", '[class*="music-title"]', '[class*="song"]']:
            element = await page.query_selector(selector)
            if element:
                text = clean_sound_name(await element.inner_text())
                if is_usable_tiktok_title(text):
                    await page.close()
                    return url, text
    except Exception:
        pass

    await page.close()
    return url, None


def is_usable_tiktok_title(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    blocked_terms = ["tiktok", "visit", "access denied", "make your day"]
    return len(value.strip()) > 2 and not any(term in lowered for term in blocked_terms)


async def fetch_all_async(urls: list[str], cache: dict, concurrency: int) -> dict:
    uncached = [url for url in urls if url not in cache]
    print(f"  {len(urls) - len(uncached)} cached, {len(uncached)} new page(s) to fetch.")

    if not uncached:
        return cache

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        semaphore = asyncio.Semaphore(concurrency)
        completed = 0

        async def bounded(url: str) -> tuple[str, str | None]:
            nonlocal completed
            async with semaphore:
                result = await fetch_one(context, url)
                cache[url] = result[1]
                save_json_file(TIKTOK_CACHE_FILE, cache)
                completed += 1
                title = result[1] or "blocked or unreadable"
                print(f"  [{completed:>3}/{len(uncached)}] {title[:70]}")
                return result

        await asyncio.gather(*(bounded(url) for url in uncached))
        await browser.close()

    return cache


def fetch_tiktok_titles(urls: list[str], concurrency: int) -> dict:
    cache = load_json_file(TIKTOK_CACHE_FILE)
    return asyncio.run(fetch_all_async(urls, cache, concurrency))


def retry_after_seconds(error: spotipy.SpotifyException, attempt: int) -> int:
    headers = getattr(error, "headers", None) or {}
    raw_retry = headers.get("Retry-After") or headers.get("retry-after")
    try:
        wait = int(raw_retry)
    except (TypeError, ValueError):
        wait = 0
    return max(wait, 10 * (attempt + 1))


def spotify_credentials_available() -> bool:
    return bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)


def make_spotify_client(mode: str) -> spotipy.Spotify | None:
    if not spotify_credentials_available():
        return None

    if mode == "playlist":
        auth_manager = SpotifyOAuth(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            redirect_uri=SPOTIFY_REDIRECT_URI,
            scope="playlist-modify-public playlist-modify-private",
            cache_path=".spotify_cache",
        )
    else:
        auth_manager = SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
        )

    return spotipy.Spotify(auth_manager=auth_manager, retries=0)


def parse_title_artist(sound: str) -> tuple[str, str | None]:
    cleaned = clean_sound_name(sound)
    if " - " in cleaned:
        title, artist = cleaned.split(" - ", 1)
        return title.strip(), artist.strip()
    return cleaned, None


def search_spotify(sp: spotipy.Spotify, sound: str, spotify_cache: dict, thorough: bool) -> dict | None:
    key = sound_key(sound)
    if key in spotify_cache:
        cached = spotify_cache[key]
        if cached and "url" not in cached and cached.get("id"):
            cached["url"] = f"https://open.spotify.com/track/{cached['id']}"
            cached["uri"] = cached.get("uri") or f"spotify:track:{cached['id']}"
            spotify_cache[key] = cached
            save_json_file(SPOTIFY_CACHE_FILE, spotify_cache)
        return cached

    sound_clean = clean_sound_name(sound)
    if SKIP_PATTERNS.search(sound_clean):
        spotify_cache[key] = None
        save_json_file(SPOTIFY_CACHE_FILE, spotify_cache)
        return None

    title, artist = parse_title_artist(sound_clean)
    if thorough and artist:
        queries = [
            f"track:{title} artist:{artist}",
            f"track:{artist} artist:{title}",
            sound_clean,
            title,
        ]
    elif artist:
        queries = [f"{title} {artist}"]
    else:
        queries = [title]

    for query in queries:
        max_attempts = 6 if thorough else 3
        for attempt in range(max_attempts):
            try:
                results = sp.search(q=query, type="track", limit=1)
                items = results["tracks"]["items"]
                if items:
                    track = items[0]
                    match = {
                        "id": track["id"],
                        "name": track["name"],
                        "artist": track["artists"][0]["name"],
                        "url": track["external_urls"]["spotify"],
                        "uri": track["uri"],
                    }
                    spotify_cache[key] = match
                    save_json_file(SPOTIFY_CACHE_FILE, spotify_cache)
                    return match
                break
            except spotipy.SpotifyException as e:
                if e.http_status == 429:
                    wait = retry_after_seconds(e, attempt)
                    print(f"  Spotify rate limit hit. Waiting {wait}s...")
                    time.sleep(wait)
                else:
                    break
            except Exception:
                break

    spotify_cache[key] = None
    save_json_file(SPOTIFY_CACHE_FILE, spotify_cache)
    return None


def build_results(
    links: list[str],
    url_to_song: dict,
    sp: spotipy.Spotify | None,
    thorough: bool,
) -> list[ResultRow]:
    spotify_cache = load_json_file(SPOTIFY_CACHE_FILE)
    rows: list[ResultRow] = []
    seen_sounds: set[str] = set()
    seen_track_ids: set[str] = set()

    for index, url in enumerate(links, 1):
        raw_sound = url_to_song.get(url)
        if not raw_sound:
            rows.append(ResultRow("skipped", "", "", "", "", "", "", url, "Could not read TikTok page"))
            print(f"[{index:>3}/{len(links)}] skipped - unreadable")
            continue

        sound = clean_sound_name(raw_sound)
        print(f"[{index:>3}/{len(links)}] {sound[:70]}")

        if SKIP_PATTERNS.search(sound):
            rows.append(ResultRow("skipped", sound, "", "", "", "", "", url, "Original audio or blocked page"))
            print("       skipped")
            continue

        key = sound_key(sound)
        if key in seen_sounds:
            rows.append(ResultRow("duplicate", sound, "", "", "", "", "", url, "Duplicate sound title"))
            print("       duplicate sound")
            continue
        seen_sounds.add(key)

        if not sp:
            title, artist = parse_title_artist(sound)
            rows.append(ResultRow("title_only", sound, title, artist or "", "", "", "", url, "Spotify search disabled"))
            print("       exported title only")
            continue

        match = search_spotify(sp, sound, spotify_cache, thorough)
        if not match:
            title, artist = parse_title_artist(sound)
            rows.append(ResultRow("unmatched", sound, title, artist or "", "", "", "", url, "No Spotify match"))
            print("       not found on Spotify")
            continue

        if match["id"] in seen_track_ids:
            rows.append(
                ResultRow(
                    "duplicate",
                    sound,
                    match["name"],
                    match["artist"],
                    match["url"],
                    match["uri"],
                    match["id"],
                    url,
                    "Duplicate Spotify track",
                )
            )
            print(f"       duplicate track - {match['name']} by {match['artist']}")
            continue

        seen_track_ids.add(match["id"])
        rows.append(
            ResultRow(
                "matched",
                sound,
                match["name"],
                match["artist"],
                match["url"],
                match["uri"],
                match["id"],
                url,
            )
        )
        print(f"       matched - {match['name']} by {match['artist']}")

    return rows


def write_exports(rows: list[ResultRow], output_dir: str) -> None:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    matched = [row for row in rows if row.status == "matched"]
    title_only = [row for row in rows if row.status == "title_only"]
    playlist_candidates = matched + title_only
    unresolved = [row for row in rows if row.status in {"unmatched", "skipped", "duplicate"}]

    with open(output_path / "spotlistr.txt", "w", encoding="utf-8") as f:
        for row in playlist_candidates:
            f.write(format_track_line(row.track, row.artist) + "\n")

    with open(output_path / "spotify_links.txt", "w", encoding="utf-8") as f:
        for row in matched:
            f.write(f"{row.spotify_url}\n")

    with open(output_path / "tunemymusic.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Track", "Artist", "Spotify URL"])
        for row in playlist_candidates:
            writer.writerow([row.track, row.artist, row.spotify_url])

    with open(output_path / "unmatched.txt", "w", encoding="utf-8") as f:
        for row in unresolved:
            label = row.source_sound or row.tiktok_url
            f.write(f"{row.status}: {label} ({row.reason})\n")

    with open(output_path / "results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(ResultRow.__dataclass_fields__.keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)

    summary = {
        "matched": len(matched),
        "title_only": len(title_only),
        "unresolved": len(unresolved),
        "total": len(rows),
        "files": [
            "spotlistr.txt",
            "spotify_links.txt",
            "tunemymusic.csv",
            "unmatched.txt",
            "results.csv",
            "summary.json",
        ],
    }
    with open(output_path / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("\nExport files created:")
    for filename in summary["files"]:
        print(f"  {output_path / filename}")


def format_track_line(track: str, artist: str) -> str:
    if track and artist:
        return f"{track} - {artist}"
    return track or artist


def create_playlist(sp: spotipy.Spotify, rows: list[ResultRow], playlist_name: str) -> None:
    track_ids = [row.spotify_id for row in rows if row.status == "matched" and row.spotify_id]
    if not track_ids:
        print("No matched tracks to add.")
        return

    user = sp.current_user()
    print(f"\nSpotify: logged in as {user['display_name']}")

    playlist = sp._post(
        "me/playlists",
        payload={
            "name": playlist_name,
            "public": False,
            "description": "Imported from TikTok favorited sounds",
        },
    )

    for batch_start in range(0, len(track_ids), 100):
        sp.playlist_add_items(playlist["id"], track_ids[batch_start:batch_start + 100])

    print(f"Created playlist '{playlist_name}' with {len(track_ids)} tracks.")
    print(f"{playlist['external_urls']['spotify']}")


def print_summary(rows: list[ResultRow]) -> None:
    counts = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1

    print("\nSummary")
    print("-" * 40)
    labels = {
        "matched": "Matched",
        "title_only": "Title only",
        "unmatched": "Unmatched",
        "skipped": "Skipped",
        "duplicate": "Duplicate",
    }
    for status in ["matched", "title_only", "unmatched", "skipped", "duplicate"]:
        print(f"{labels[status]:<10}: {counts.get(status, 0)}")
    print(f"{'Total':<10}: {len(rows)}")
    print("-" * 40)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert TikTok favorited sounds into Spotify-ready playlist exports."
    )
    parser.add_argument(
        "--mode",
        choices=["export", "playlist"],
        default="export",
        help="export creates files for Spotlistr/TuneMyMusic. playlist also creates a Spotify playlist.",
    )
    parser.add_argument("--input", default=DEFAULT_TIKTOK_DATA_FILE, help="Path to TikTok JSON export.")
    parser.add_argument("--output", default="exports", help="Folder for generated playlist files.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N sounds.")
    parser.add_argument("--playlist-name", default=DEFAULT_PLAYLIST_NAME, help="Spotify playlist name.")
    parser.add_argument("--concurrency", type=int, default=3, help="TikTok page fetch concurrency.")
    parser.add_argument("--thorough", action="store_true", help="Try more Spotify queries per sound.")
    parser.add_argument(
        "--no-spotify",
        action="store_true",
        help="Skip Spotify search and export title/artist guesses only.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not os.path.exists(args.input):
        print(f"TikTok export file not found: {args.input}")
        sys.exit(1)

    print(f"Loading TikTok sounds from '{args.input}'...")
    all_links = load_tiktok_links(args.input)
    links = all_links if args.limit is None else all_links[: args.limit]
    print(f"Processing {len(links)} sound(s).")

    print("\nFetching TikTok sound titles...")
    url_to_song = fetch_tiktok_titles(links, args.concurrency)

    sp = None
    if not args.no_spotify:
        sp = make_spotify_client(args.mode)
        if not sp:
            print("\nSpotify credentials were not found. Exporting title/artist guesses only.")
            print("Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to .env for Spotify matches.")

    print("\nMatching sounds...")
    rows = build_results(links, url_to_song, sp, args.thorough)
    print_summary(rows)
    write_exports(rows, args.output)

    if args.mode == "playlist":
        if not sp:
            print("Playlist mode requires Spotify credentials.")
            sys.exit(1)
        create_playlist(sp, rows, args.playlist_name)


if __name__ == "__main__":
    main()
