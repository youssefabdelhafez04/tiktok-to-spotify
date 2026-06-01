import asyncio
import json
import os
import re
import sys
import time
import spotipy
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "YOUR_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "YOUR_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI  = "http://127.0.0.1:8888/callback"

TIKTOK_DATA_FILE = "user_data_tiktok.json"
PLAYLIST_NAME    = "TikTok Favorites"
MAX_SOUNDS       = None
CACHE_FILE       = ".tiktok_cache.json"   # saves progress so crashes don't lose work
SPOTIFY_CACHE_FILE = ".spotify_match_cache.json"
FAST_MODE = True
SPOTIFY_SEARCH_DELAY = 0
# ─────────────────────────────────────────────────────────────────────────────

SKIP_PATTERNS = re.compile(
    r"original sound|original audio|\boriginal\b|access denied|"
    r"tiktok - make your day|visit tiktok|fyp|foryou",
    re.IGNORECASE,
)


def load_tiktok_links(filepath: str) -> list[str]:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    try:
        entries = data["Likes and Favorites"]["Favorite Sounds"]["FavoriteSoundList"]
    except (KeyError, TypeError):
        print("[!] Could not find the sounds list in your export.")
        sys.exit(1)
    links = [e["Link"] for e in entries if e.get("Link")]
    print(f"Found {len(links)} favorited sound link(s).")
    return links


def load_json_file(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_json_file(path: str, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def clean_sound_name(sound: str) -> str:
    sound = re.sub(r"\(.*?\)|\[.*?\]", "", sound).strip()
    sound = re.sub(r"\s+", " ", sound)
    return sound


def sound_key(sound: str) -> str:
    return clean_sound_name(sound).casefold()


async def fetch_one(context, url: str) -> tuple[str, str | None]:
    page = await context.new_page()
    await Stealth().apply_stealth_async(page)
    try:
        await page.goto(url, timeout=15000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        og = await page.query_selector('meta[property="og:title"]')
        if og:
            content = await og.get_attribute("content")
            if content and "tiktok" not in content.lower() and "visit" not in content.lower() and "access denied" not in content.lower():
                await page.close()
                return url, content.strip()

        for selector in ["h1", '[class*="music-title"]', '[class*="song"]']:
            el = await page.query_selector(selector)
            if el:
                text = (await el.inner_text()).strip()
                if text and len(text) > 2 and "access denied" not in text.lower():
                    await page.close()
                    return url, text

    except Exception:
        pass
    await page.close()
    return url, None


async def fetch_all_async(urls: list[str], cache: dict, concurrency: int = 3) -> dict:
    uncached = [u for u in urls if u not in cache]
    print(f"  {len(urls) - len(uncached)} already cached, fetching {len(uncached)} new pages...\n")

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
        sem = asyncio.Semaphore(concurrency)

        completed = 0

        async def bounded(url):
            nonlocal completed
            async with sem:
                result = await fetch_one(context, url)
                cache[url] = result[1]
                save_json_file(CACHE_FILE, cache)
                completed += 1
                song = result[1] or "✗ blocked"
                print(f"  [{completed:>3}/{len(uncached)}] {song[:60]}")
                return result

        await asyncio.gather(*[bounded(u) for u in uncached])
        await browser.close()

    return cache


def get_songs_from_tiktok_urls(urls: list[str], cache: dict) -> dict:
    return asyncio.run(fetch_all_async(urls, cache))


def retry_after_seconds(error: spotipy.SpotifyException, attempt: int) -> int:
    headers = getattr(error, "headers", None) or {}
    raw_retry = headers.get("Retry-After") or headers.get("retry-after")
    try:
        wait = int(raw_retry)
    except (TypeError, ValueError):
        wait = 0
    return max(wait, 10 * (attempt + 1))


def search_spotify(sp: spotipy.Spotify, sound: str, spotify_cache: dict):
    key = sound_key(sound)
    if key in spotify_cache:
        return spotify_cache[key]

    sound_clean = clean_sound_name(sound)
    if SKIP_PATTERNS.search(sound_clean):
        spotify_cache[key] = None
        save_json_file(SPOTIFY_CACHE_FILE, spotify_cache)
        return None

    if " - " in sound_clean:
        parts = sound_clean.split(" - ", 1)
        title, artist = parts[0].strip(), parts[1].strip()
    else:
        title, artist = sound_clean, None

    queries = []
    if FAST_MODE:
        queries.append(f"{title} {artist}" if artist else title)
    elif artist:
        queries.append(f"track:{title} artist:{artist}")
        queries.append(f"track:{artist} artist:{title}")
        queries.append(sound_clean)
        queries.append(title)
    else:
        queries.append(sound_clean)
        queries.append(title)

    for q in queries:
        for attempt in range(3 if FAST_MODE else 10):
            try:
                results = sp.search(q=q, type="track", limit=1)
                items = results["tracks"]["items"]
                if items:
                    track = items[0]
                    match = {
                        "id": track["id"],
                        "name": track["name"],
                        "artist": track["artists"][0]["name"],
                    }
                    spotify_cache[key] = match
                    save_json_file(SPOTIFY_CACHE_FILE, spotify_cache)
                    time.sleep(SPOTIFY_SEARCH_DELAY)
                    return match
                break
            except spotipy.SpotifyException as e:
                if e.http_status == 429:
                    wait = retry_after_seconds(e, attempt)
                    print(f"\n  Rate limited — waiting {wait}s...")
                    time.sleep(wait)
                else:
                    time.sleep(2)
                    break
            except Exception:
                time.sleep(2)
                break

    spotify_cache[key] = None
    save_json_file(SPOTIFY_CACHE_FILE, spotify_cache)
    time.sleep(SPOTIFY_SEARCH_DELAY)
    return None


def main():
    if "YOUR_CLIENT" in SPOTIFY_CLIENT_ID:
        print("Fill in your Spotify credentials in the .env file first.")
        sys.exit(1)

    if not os.path.exists(TIKTOK_DATA_FILE):
        print(f"TikTok export file not found: {TIKTOK_DATA_FILE}")
        sys.exit(1)

    sp = spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            redirect_uri=SPOTIFY_REDIRECT_URI,
            scope="playlist-modify-public playlist-modify-private",
            cache_path=".spotify_cache",
        ),
        retries=0,
    )

    user = sp.current_user()
    print(f"Spotify: logged in as {user['display_name']}\n")

    print(f"Loading TikTok sounds from '{TIKTOK_DATA_FILE}'...")
    all_links = load_tiktok_links(TIKTOK_DATA_FILE)
    links_to_process = all_links if MAX_SOUNDS is None else all_links[:MAX_SOUNDS]
    print(f"Processing {len(links_to_process)} sound(s).")

    cache = load_json_file(CACHE_FILE)
    print("Fetching TikTok pages (runs in background, saves progress as it goes)...")
    url_to_song = get_songs_from_tiktok_urls(links_to_process, cache)
    spotify_cache = load_json_file(SPOTIFY_CACHE_FILE)

    found:     list[dict] = []
    not_found: list[str]  = []
    skipped:   list[str]  = []
    seen_sounds: set[str] = set()
    seen_track_ids: set[str] = set()

    for i, url in enumerate(links_to_process, 1):
        sound = url_to_song.get(url)

        if not sound:
            print(f"[{i:>3}/{len(links_to_process)}] ✗ could not read page")
            skipped.append(url)
            continue

        sound = clean_sound_name(sound)
        print(f"[{i:>3}/{len(links_to_process)}] {sound[:60]}")

        if SKIP_PATTERNS.search(sound):
            print("       → skipped")
            skipped.append(sound)
            continue

        key = sound_key(sound)
        if key in seen_sounds:
            print("       → duplicate sound, skipped")
            skipped.append(sound)
            continue
        seen_sounds.add(key)

        track = search_spotify(sp, sound, spotify_cache)

        if track:
            if track["id"] in seen_track_ids:
                print(f"       → duplicate track: {track['name']} — {track['artist']}")
                skipped.append(sound)
                continue
            seen_track_ids.add(track["id"])
            print(f"       ✓ {track['name']} — {track['artist']}")
            found.append(track)
        else:
            print("       ✗ not found on Spotify")
            not_found.append(sound)

    print(f"\n{'─'*50}")
    print(f"  Matched  : {len(found)}")
    print(f"  Not found: {len(not_found)}")
    print(f"  Skipped  : {len(skipped)}")
    print(f"{'─'*50}\n")

    if not found:
        print("Nothing matched on Spotify. Exiting.")
        return

    playlist = sp._post("me/playlists", payload={
        "name": PLAYLIST_NAME,
        "public": False,
        "description": "Imported from TikTok favorited sounds",
    })

    track_ids = [t["id"] for t in found]
    for batch_start in range(0, len(track_ids), 100):
        sp.playlist_add_items(playlist["id"], track_ids[batch_start:batch_start + 100])

    print(f"✓ Created playlist '{PLAYLIST_NAME}' with {len(found)} tracks.")
    print(f"  → {playlist['external_urls']['spotify']}\n")

    if not_found:
        print("Sounds that couldn't be matched on Spotify:")
        for s in not_found:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
