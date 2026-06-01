# TikTok to Spotify

Turn TikTok favorited sounds into Spotify-ready playlist files.

This tool reads a TikTok data export, finds the sound names, matches real songs on Spotify, and creates export files you can use with Spotlistr, TuneMyMusic, or Spotify Desktop.

## What it creates

After running, check the `exports/` folder:

- `spotlistr.txt` - one song per line for Spotlistr
- `tunemymusic.csv` - CSV import for TuneMyMusic
- `spotify_links.txt` - direct Spotify track links
- `unmatched.txt` - sounds that were skipped or not found
- `results.csv` - full results table
- `summary.json` - match counts

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
playwright install chromium
```

Download your TikTok data:

1. Open TikTok settings.
2. Go to privacy/data download.
3. Select `Likes and Favorites`.
4. Choose JSON format.
5. Download the file and place it in this folder as `user_data_tiktok.json`.

Optional Spotify matching:

1. Create a Spotify developer app at https://developer.spotify.com/dashboard.
2. Copy `.env.example` to `.env`.
3. Add your Spotify Client ID and Client Secret.

```bash
cp .env.example .env
```

## Run

Web app:

```bash
python web_app.py
```

Then open:

```text
http://127.0.0.1:5000
```

Export playlist files:

```bash
python main.py
```

Process only the first 50 sounds:

```bash
python main.py --limit 50
```

Skip Spotify matching and only export TikTok title guesses:

```bash
python main.py --no-spotify
```

Create a Spotify playlist directly:

```bash
python main.py --mode playlist --playlist-name "TikTok Favorites"
```

## Import to Spotify

Fastest options:

1. Paste `exports/spotlistr.txt` into Spotlistr.
2. Upload `exports/tunemymusic.csv` to TuneMyMusic.
3. Copy links from `exports/spotify_links.txt` into Spotify Desktop.

## Privacy

Your TikTok export and Spotify credentials stay local. These files are ignored by Git:

- `.env`
- `user_data_tiktok.json`
- `.spotify_cache`
- `.tiktok_cache.json`
- `.spotify_match_cache.json`
- `exports/`

## Notes

TikTok original audios, deleted sounds, blocked pages, and remixes may not match Spotify cleanly. The tool skips obvious original sounds and writes misses to `unmatched.txt`.
