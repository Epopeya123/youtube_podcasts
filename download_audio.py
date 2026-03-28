#!/usr/bin/env python3
"""Download audio from Nate B. Jones' YouTube channel as podcast episodes."""

import argparse
import json
import os
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET

import yt_dlp

CHANNEL_URL = "https://www.youtube.com/@natebjones/videos"
EPISODES_FILE = "episodes.json"
AUDIO_DIR = "audio"
YOUTUBE_RSS_CACHE = ".channel_id"


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


def discover_channel_id():
    """Use yt-dlp to discover the channel ID from the handle."""
    if os.path.exists(YOUTUBE_RSS_CACHE):
        with open(YOUTUBE_RSS_CACHE, "r") as f:
            channel_id = f.read().strip()
            if channel_id:
                return channel_id

    print("Discovering channel ID...")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": 1,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(CHANNEL_URL, download=False)
            if info:
                channel_id = info.get("channel_id") or info.get("id", "")
                if channel_id and channel_id.startswith("UC"):
                    print(f"Found channel ID: {channel_id}")
                    with open(YOUTUBE_RSS_CACHE, "w") as f:
                        f.write(channel_id)
                    return channel_id
    except Exception as e:
        print(f"Could not discover channel ID: {e}")
    return None


def fetch_videos_from_rss(channel_id):
    """Fetch recent videos from YouTube's public RSS feed (works from any IP)."""
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    print("Fetching videos from YouTube RSS feed...")
    try:
        req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as response:
            xml_data = response.read()
        root = ET.fromstring(xml_data)
        ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        entries = root.findall("atom:entry", ns)
        videos = []
        for entry in entries:
            video_id_el = entry.find("yt:videoId", ns)
            title_el = entry.find("atom:title", ns)
            title = title_el.text if title_el is not None else ""
            # Skip YouTube Shorts (typically very short, have hashtag-heavy titles)
            if video_id_el is not None:
                videos.append({
                    "id": video_id_el.text,
                    "title": title,
                })
        print(f"Found {len(videos)} videos from RSS feed.")
        return videos
    except Exception as e:
        print(f"RSS feed fetch failed: {e}")
        return []


def fetch_videos_from_ytdlp(max_episodes):
    """Fetch list of recent videos using yt-dlp (fallback)."""
    print("Trying yt-dlp to fetch video list...")
    ydl_opts = {
        "extract_flat": True,
        "quiet": False,
        "no_warnings": False,
        "playlistend": max_episodes * 3,
        "extractor_args": {"youtube": {"player_client": ["web"]}},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(CHANNEL_URL, download=False)
            if info and "entries" in info:
                videos = [e for e in info["entries"] if e]
                print(f"Found {len(videos)} videos via yt-dlp.")
                return videos
    except Exception as e:
        print(f"yt-dlp fetch failed: {e}")
    return []


def fetch_video_list(max_episodes):
    """Fetch list of recent videos, trying RSS first then yt-dlp."""
    channel_id = discover_channel_id()
    if channel_id:
        videos = fetch_videos_from_rss(channel_id)
        if videos:
            return videos

    return fetch_videos_from_ytdlp(max_episodes)


def sanitize_filename(title):
    """Create a safe filename from a video title."""
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in title)
    return safe.strip()[:80] or "episode"


def download_audio(video_id, output_dir):
    """Download audio for a single video. Returns metadata dict or None on failure."""
    os.makedirs(output_dir, exist_ok=True)

    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")

    def progress_hook(d):
        if d["status"] == "finished":
            print("  Download complete, converting...")

    ydl_opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }
        ],
        "postprocessor_args": ["-ac", "1"],
        "outtmpl": output_template,
        "quiet": False,
        "no_warnings": False,
        "progress_hooks": [progress_hook],
        "extractor_args": {"youtube": {"player_client": ["web"]}},
        "sleep_interval": 2,
        "max_sleep_interval": 5,
    }

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                mp3_path = os.path.join(output_dir, f"{video_id}.mp3")
                filesize = os.path.getsize(mp3_path) if os.path.exists(mp3_path) else 0
                title = info.get("title", "Unknown Title")

                # Rename file to a readable name
                safe_name = f"{sanitize_filename(title)}.mp3"
                readable_path = os.path.join(output_dir, safe_name)
                if os.path.exists(mp3_path) and not os.path.exists(readable_path):
                    shutil.move(mp3_path, readable_path)

                metadata = {
                    "id": video_id,
                    "title": title,
                    "description": info.get("description", ""),
                    "upload_date": info.get("upload_date", ""),
                    "duration": info.get("duration", 0),
                    "filename": safe_name,
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
    parser.add_argument(
        "--output-dir",
        type=str,
        default=AUDIO_DIR,
        help="Directory to save MP3 files (default: audio/)",
    )
    args = parser.parse_args()

    print(f"Loading existing episodes from {EPISODES_FILE}...")
    episodes = load_episodes()
    existing_ids = get_existing_ids(episodes)
    print(f"Found {len(episodes)} existing episodes.")

    print("Fetching video list...")
    videos = fetch_video_list(args.max_episodes)

    if not videos:
        print("WARNING: Could not fetch any videos. YouTube may be blocking this IP.")
        print("Try running locally or check the logs for details.")
        return

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

        metadata = download_audio(video_id, args.output_dir)
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
