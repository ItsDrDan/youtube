#!/usr/bin/env python3
"""
Step 5: ItsDrDan video library + automatic searchable captions

No YouTube Data API key or OAuth token is required.

The script:
1. Reads the public @itsdrdan/videos channel page with yt-dlp.
2. Rebuilds videos.json safely.
3. Downloads a limited batch of missing English subtitle tracks.
4. Saves each chosen caption permanently as captions/VIDEO_ID.vtt.
5. Adds a caption URL to videos.json for every saved transcript.

Existing VIDEO_ID.vtt files are never overwritten.
"""

from pathlib import Path
from datetime import datetime, timezone
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

CHANNEL_URL = "https://www.youtube.com/@itsdrdan/videos"
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "videos.json"
CAPTIONS = ROOT / "captions"

MIN_EXPECTED_VIDEOS = 10

# Backfill a manageable number per run to reduce the chance of rate limiting.
# Set CAPTION_LIMIT=0 to try all missing captions in one run.
CAPTION_LIMIT = int(os.environ.get("CAPTION_LIMIT", "20"))


def run(cmd, check=True):
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"Command failed with exit code {result.returncode}")
    return result


def load_existing():
    if not OUTPUT.exists():
        return {}
    try:
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
        videos = data if isinstance(data, list) else data.get("videos", [])
        return {str(v.get("id")): v for v in videos if v.get("id")}
    except Exception:
        return {}


def get_channel_entries():
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--ignore-errors",
        "--no-warnings",
        CHANNEL_URL,
    ]
    result = run(cmd)
    data = json.loads(result.stdout)
    entries = [x for x in (data.get("entries") or []) if x and x.get("id")]

    seen = set()
    unique = []
    for item in entries:
        video_id = str(item.get("id", "")).strip()
        if video_id and video_id not in seen:
            seen.add(video_id)
            unique.append(item)

    if len(unique) < MIN_EXPECTED_VIDEOS:
        raise RuntimeError(
            f"Safety stop: only {len(unique)} videos were returned; "
            f"expected at least {MIN_EXPECTED_VIDEOS}. Existing videos.json was not replaced."
        )

    return unique


def duration_fields(value):
    try:
        seconds = int(float(value or 0))
    except (TypeError, ValueError):
        seconds = 0

    if seconds <= 0:
        return 0, ""

    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    display = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return seconds, display


def candidate_priority(path, video_id):
    name = path.name.lower()
    expected = [
        f"{video_id.lower()}.en.vtt",
        f"{video_id.lower()}.en-orig.vtt",
        f"{video_id.lower()}.en-us.vtt",
        f"{video_id.lower()}.en-gb.vtt",
    ]
    for rank, candidate in enumerate(expected):
        if name == candidate:
            return rank
    if ".en" in name and name.endswith(".vtt"):
        return 20
    return 100


def fetch_one_caption(video_id):
    final = CAPTIONS / f"{video_id}.vtt"

    # Preserve automatic downloads and any later hand-corrections.
    if final.exists() and final.stat().st_size > 20:
        return "existing"

    url = f"https://www.youtube.com/watch?v={video_id}"

    with tempfile.TemporaryDirectory(prefix=f"itsdrdan-{video_id}-") as temp:
        tempdir = Path(temp)

        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", "en,en-orig,en-US,en-GB",
            "--sub-format", "vtt/best",
            "--ignore-errors",
            "--no-warnings",
            "--retries", "4",
            "--extractor-retries", "3",
            "--sleep-requests", "1",
            "--sleep-subtitles", "1",
            "-o", str(tempdir / "%(id)s.%(ext)s"),
            url,
        ]

        result = run(cmd, check=False)

        candidates = sorted(
            tempdir.glob(f"{video_id}*.vtt"),
            key=lambda p: (candidate_priority(p, video_id), p.name.lower())
        )

        if not candidates:
            detail = ""
            if result.stderr.strip():
                detail = result.stderr.strip().splitlines()[-1]
            return f"missing:{detail}"

        shutil.copy2(candidates[0], final)
        return f"downloaded:{candidates[0].name}"


def build_video_records(entries, previous):
    records = []

    for order, item in enumerate(entries):
        video_id = str(item["id"]).strip()
        old = previous.get(video_id, {})

        seconds, duration = duration_fields(
            item.get("duration") or old.get("durationSeconds")
        )

        caption_file = CAPTIONS / f"{video_id}.vtt"

        records.append({
            "id": video_id,
            "title": item.get("title") or old.get("title") or f"YouTube video {video_id}",
            "description": item.get("description") or old.get("description") or "",
            "keywords": old.get("keywords") or "",
            "course": old.get("course") or "",
            "thumbnail": item.get("thumbnail") or old.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "duration": duration or old.get("duration", ""),
            "durationSeconds": seconds or old.get("durationSeconds", 0),
            "uploadDate": item.get("upload_date") or old.get("uploadDate") or "",
            "order": order,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "caption": f"./captions/{video_id}.vtt" if caption_file.exists() else None,
        })

    return records


def write_library(records):
    payload = {
        "channel": "https://www.youtube.com/@itsdrdan",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "videoCount": len(records),
        "captionCount": sum(1 for v in records if v["caption"]),
        "videos": records,
    }

    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=ROOT,
        suffix=".json",
    ) as tmp:
        json.dump(payload, tmp, indent=2, ensure_ascii=False)
        tmp.write("\n")
        temp_path = Path(tmp.name)

    temp_path.replace(OUTPUT)


def main():
    CAPTIONS.mkdir(parents=True, exist_ok=True)

    previous = load_existing()

    print("Reading public ItsDrDan channel...")
    entries = get_channel_entries()
    print(f"Found {len(entries)} videos.")

    # First make sure videos.json stays current even if caption downloading later hits a limit.
    records = build_video_records(entries, previous)
    write_library(records)

    missing = [
        v for v in records
        if not (CAPTIONS / f"{v['id']}.vtt").exists()
    ]

    if CAPTION_LIMIT > 0:
        targets = missing[:CAPTION_LIMIT]
    else:
        targets = missing

    print(f"Missing captions: {len(missing)}")
    print(f"Attempting this run: {len(targets)}")

    new_count = 0
    missing_count = 0

    for n, video in enumerate(targets, start=1):
        print(f"[{n}/{len(targets)}] {video['title']}")
        status = fetch_one_caption(video["id"])

        if status.startswith("downloaded"):
            new_count += 1
            print(f"  ✓ {status}")
        elif status == "existing":
            print("  ✓ existing")
        else:
            missing_count += 1
            print(f"  – {status}")

        # Gentle pacing between separate video subtitle requests.
        time.sleep(1)

    # Rebuild so newly downloaded captions are immediately referenced.
    records = build_video_records(entries, previous)
    write_library(records)

    total_captions = sum(
        1 for v in records
        if (CAPTIONS / f"{v['id']}.vtt").exists()
    )

    print()
    print(f"Videos:                 {len(records)}")
    print(f"Saved searchable VTTs:  {total_captions}")
    print(f"New VTTs this run:      {new_count}")
    print(f"No VTT this run:        {missing_count}")
    print(f"Wrote:                  {OUTPUT}")


if __name__ == "__main__":
    main()
