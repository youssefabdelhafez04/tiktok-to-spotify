import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from flask import Flask, render_template, request, send_file
from werkzeug.utils import secure_filename

from main import (
    build_results,
    fetch_tiktok_titles,
    load_tiktok_links,
    make_spotify_client,
    print_summary,
    write_exports,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

BASE_DIR = Path(__file__).resolve().parent
WEB_EXPORTS_DIR = BASE_DIR / "web_exports"
ALLOWED_SUFFIXES = {".json"}


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/convert")
def convert():
    uploaded_file = request.files.get("tiktok_file")
    if not uploaded_file or uploaded_file.filename == "":
        return render_template("index.html", error="Upload your TikTok JSON export first."), 400

    filename = secure_filename(uploaded_file.filename)
    if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
        return render_template("index.html", error="Please upload a JSON file."), 400

    job_id = uuid.uuid4().hex[:12]
    job_dir = WEB_EXPORTS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    temp_dir = Path(tempfile.mkdtemp(prefix="tiktok_to_spotify_"))
    input_path = temp_dir / filename
    uploaded_file.save(input_path)

    try:
        limit_raw = request.form.get("limit", "").strip()
        limit = int(limit_raw) if limit_raw else None
        use_spotify = request.form.get("use_spotify") == "on"
        thorough = request.form.get("thorough") == "on"

        links = load_tiktok_links(str(input_path))
        links = links if limit is None else links[:limit]

        url_to_song = fetch_tiktok_titles(links, concurrency=3)
        spotify_client = make_spotify_client("export") if use_spotify else None
        rows = build_results(links, url_to_song, spotify_client, thorough=thorough)
        print_summary(rows)
        write_exports(rows, str(job_dir))

        zip_path = job_dir / "tiktok-to-spotify-exports.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for output_file in job_dir.iterdir():
                if output_file == zip_path or not output_file.is_file():
                    continue
                zip_file.write(output_file, output_file.name)

        return render_template(
            "result.html",
            job_id=job_id,
            matched=sum(1 for row in rows if row.status == "matched"),
            title_only=sum(1 for row in rows if row.status == "title_only"),
            unresolved=sum(1 for row in rows if row.status in {"unmatched", "skipped", "duplicate"}),
            total=len(rows),
            used_spotify=use_spotify and spotify_client is not None,
        )
    except SystemExit:
        return render_template("index.html", error="That JSON does not look like a TikTok Likes and Favorites export."), 400
    except Exception as exc:
        return render_template("index.html", error=f"Conversion failed: {exc}"), 500
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.get("/download/<job_id>")
def download(job_id: str):
    safe_job_id = secure_filename(job_id)
    zip_path = WEB_EXPORTS_DIR / safe_job_id / "tiktok-to-spotify-exports.zip"
    if not zip_path.exists():
        return "Export not found.", 404
    return send_file(zip_path, as_attachment=True, download_name="tiktok-to-spotify-exports.zip")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
