#!/usr/bin/env python3
"""
ItsDrDan YouTube library + caption updater
===========================================

No YouTube API key, OAuth token, or user-created secret is required.

What it does:
1. Reads the public video list from https://www.youtube.com/@itsdrdan/videos
2. For videos that do not already have captions/VIDEO_ID.vtt:
   - tries creator-provided English subtitles first
   - also allows YouTube's automatic English captions as a fallback
   - downloads subtitles only (never the video)
   - normalizes the chosen file to captions/VIDEO_ID.vtt
3. Rebuilds videos.json so the GitHub Pages search engine sees the videos/captions.

Requirements:
    python -m pip install -U yt-dlp

Run from the repository root:
    python scripts/build_videos.py
"""

from pathlib import Path
from datetime import datetime, timezone
import json
import shutil
import subprocess
import sys
import tempfile
import time

CHANNEL_URL = "https://www.youtube.com/@itsdrdan/videos"
ROOT = Path(__file__).resolve().parents[1]
CAPTIONS = ROOT / "captions"
OUTPUT = ROOT / "videos.json"

# Keep previously downloaded/manual VIDEO_ID.vtt files.
# This protects hand-corrected captions and means later runs only fetch missing files.
KEEP_EXISTING_CAPTIONS = True

def run(cmd, *, check=True):
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout)
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)
        raise subprocess.CalledProcessError(
            completed.returncode, cmd, completed.stdout, completed.stderr
        )
    return completed

def get_channel_entries():
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
        "--ignore-errors",
        CHANNEL_URL,
    ]
    result = run(cmd)
    data = json.loads(result.stdout)
    return [x for x in (data.get("entries") or []) if x and x.get("id")]

def format_duration(seconds):
    try:
        seconds = int(float(seconds or 0))
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def candidate_priority(path, video_id):
    """
    Prefer a normal English track. yt-dlp generally gives creator subtitles
    precedence when both --write-subs and --write-auto-subs are requested.
    """
    name = path.name.lower()
    exact_order = [
        f"{video_id.lower()}.en.vtt",
        f"{video_id.lower()}.en-orig.vtt",
        f"{video_id.lower()}.en-us.vtt",
        f"{video_id.lower()}.en-gb.vtt",
    ]
    for rank, expected in enumerate(exact_order):
        if name == expected:
            return rank
    # Other English variants after the common ones.
    if ".en" in name and name.endswith(".vtt"):
        return 10
    return 50

def download_caption(video_id):
    CAPTIONS.mkdir(parents=True, exist_ok=True)
    final_path = CAPTIONS / f"{video_id}.vtt"

    if KEEP_EXISTING_CAPTIONS and final_path.exists() and final_path.stat().st_size > 20:
        return "existing"

    video_url = f"https://www.youtube.com/watch?v={video_id}"

    with tempfile.TemporaryDirectory(prefix=f"itsdrdan-{video_id}-") as tmp:
        tmpdir = Path(tmp)

        # --skip-download means media is never downloaded.
        # --write-subs requests uploader/creator subtitle tracks.
        # --write-auto-subs allows automatic captions when needed.
        # en.* covers common English variants such as en, en-US, en-orig.
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", "en.*",
            "--sub-format", "vtt",
            "--ignore-errors",
            "--no-warnings",
            "--sleep-requests", "0.75",
            "--sleep-subtitles", "1",
            "--retries", "5",
            "--extractor-retries", "3",
            "-o", str(tmpdir / "%(id)s.%(ext)s"),
            video_url,
        ]

        result = run(cmd, check=False)

        candidates = sorted(
            tmpdir.glob(f"{video_id}*.vtt"),
            key=lambda p: (candidate_priority(p, video_id), p.name.lower())
        )

        if not candidates:
            if result.stderr.strip():
                print(f"  No caption saved for {video_id}: {result.stderr.strip().splitlines()[-1]}")
            return "missing"

        chosen = candidates[0]
        shutil.copy2(chosen, final_path)
        return f"downloaded:{chosen.name}"

def main():
    CAPTIONS.mkdir(parents=True, exist_ok=True)

    print("Reading public ItsDrDan video list...")
    entries = get_channel_entries()
    print(f"Found {len(entries)} public channel entries.")

    videos = []
    downloaded = 0
    existing = 0
    missing = 0

    for order, item in enumerate(entries, start=1):
        video_id = str(item.get("id")).strip()
        title = item.get("title") or f"YouTube video {video_id}"

        caption_path = CAPTIONS / f"{video_id}.vtt"
        if caption_path.exists() and KEEP_EXISTING_CAPTIONS:
            status = "existing"
        else:
            print(f"[{order}/{len(entries)}] Captions: {title}")
            status = download_caption(video_id)

        if status == "existing":
            existing += 1
        elif status.startswith("downloaded"):
            downloaded += 1
            print(f"  ✓ {status}")
        else:
            missing += 1
            print("  – no English VTT available right now")

        duration_seconds = item.get("duration") or 0
        try:
            duration_seconds = int(float(duration_seconds or 0))
        except (TypeError, ValueError):
            duration_seconds = 0

        videos.append({
            "id": video_id,
            "title": title,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "order": order - 1,
            "description": item.get("description") or "",
            "duration": format_duration(duration_seconds),
            "durationSeconds": duration_seconds,
            "tags": [],
            "course": "",
            "caption": f"./captions/{video_id}.vtt" if (CAPTIONS / f"{video_id}.vtt").exists() else None,
        })

    payload = {
        "channel": "https://www.youtube.com/@itsdrdan",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "videoCount": len(videos),
        "captionCount": sum(1 for v in videos if v["caption"]),
        "videos": videos,
    }

    OUTPUT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print()
    print("Finished.")
    print(f"Videos in library:       {len(videos)}")
    print(f"Existing captions kept:  {existing}")
    print(f"New captions downloaded: {downloaded}")
    print(f"No caption available:    {missing}")
    print(f"Wrote: {OUTPUT}")

if __name__ == "__main__":
    main()
