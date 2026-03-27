#!/usr/bin/env python3
"""Download audio from Nate B. Jones' YouTube channel as podcast episodes."""

import argparse
import json
import os
import sys

import yt_dlp

CHANNEL_URL = "https://www.youtube.com/@natebjones/videos"
EPISODES_FILE = "episodes.json"
AUDIO_DIR = "audio"


def load_episodes():
    """Load existing episodes from JSON file."""
    if os.path.exists(EPISODES_FILE):
        with open(EPISODES_FILE, "r") as f:
            return json.load(f)
    return []


def save_episodes(episodes):
    """Save episodes to JSON file."""
    with open(EPISODES_FILE, "w") as f:
        json.dump(episodes, f, indent=2)


def get_existing_ids(episodes):
    """Get set of already-downloaded video IDs."""
    return {ep["id"] for ep in episodes}


def fetch_video_list(max_episodes):
    """Fetch list of recent videos from the channel."""
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "playlistend": max_episodes * 3,  # fetch extra to account for filtering
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(CHANNEL_URL, download=False)
        if info and "entries" in info:
            return list(info["entries"])
    return []


def download_audio(video_id):
    """Download audio for a single video. Returns metadata dict or None on failure."""
    os.makedirs(AUDIO_DIR, exist_ok=True)

    output_template = os.path.join(AUDIO_DIR, "%(id)s.%(ext)s")
    metadata = {}

    def progress_hook(d):
        if d["status"] == "finished":
            print(f"  Download complete, converting...")

    ydl_opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }
        ],
        "postprocessor_args": {"FFmpegExtractAudio": ["-ac", "1"]},
        "outtmpl": output_template,
        "quiet": False,
        "no_warnings": False,
        "progress_hooks": [progress_hook],
    }

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                mp3_path = os.path.join(AUDIO_DIR, f"{video_id}.mp3")
                filesize = os.path.getsize(mp3_path) if os.path.exists(mp3_path) else 0

                metadata = {
                    "id": video_id,
                    "title": info.get("title", "Unknown Title"),
                    "description": info.get("description", ""),
                    "upload_date": info.get("upload_date", ""),
                    "duration": info.get("duration", 0),
                    "filename": f"{video_id}.mp3",
                    "filesize": filesize,
                    "published": False,
                }
                return metadata
    except Exception as e:
        print(f"  Error downloading {video_id}: {e}", file=sys.stderr)
    return None


def main():
    parser = argparse.ArgumentParser(description="Download YouTube audio as podcast episodes")
    parser.add_argument(
        "--max-episodes",
        type=int,
        default=5,
        help="Maximum number of new episodes to download (default: 5)",
    )
    args = parser.parse_args()

    print(f"Loading existing episodes from {EPISODES_FILE}...")
    episodes = load_episodes()
    existing_ids = get_existing_ids(episodes)
    print(f"Found {len(episodes)} existing episodes.")

    print(f"Fetching video list from {CHANNEL_URL}...")
    videos = fetch_video_list(args.max_episodes)
    print(f"Found {len(videos)} videos on channel.")

    # Filter out already-downloaded videos
    new_videos = [v for v in videos if v.get("id") and v["id"] not in existing_ids]
    new_videos = new_videos[: args.max_episodes]
    print(f"{len(new_videos)} new videos to download.")

    if not new_videos:
        print("No new episodes to download. Done!")
        return

    downloaded = 0
    for i, video in enumerate(new_videos, 1):
        video_id = video["id"]
        title = video.get("title", video_id)
        print(f"\n[{i}/{len(new_videos)}] Downloading: {title}")

        metadata = download_audio(video_id)
        if metadata:
            episodes.append(metadata)
            downloaded += 1
            print(f"  Saved: {metadata['filename']} ({metadata['filesize'] / 1024 / 1024:.1f} MB)")
        else:
            print(f"  Failed to download {video_id}")

    # Sort episodes by upload date (newest first)
    episodes.sort(key=lambda x: x.get("upload_date", ""), reverse=True)

    save_episodes(episodes)
    print(f"\nDone! Downloaded {downloaded} new episodes. Total: {len(episodes)} episodes.")


if __name__ == "__main__":
    main()
