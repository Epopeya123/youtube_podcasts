#!/usr/bin/env python3
"""Download audio from YouTube videos as podcast episodes.

Speed notes (why this file looks the way it does):
  * One metadata extraction per video, not two. Extraction is the slow part on a
    phone (it fetches the player JS and runs a JS challenge through Node), so
    doing it twice roughly doubled the wait.
  * Audio is kept in its original AAC/m4a stream by default. Re-encoding to MP3
    means a full decode+encode pass over the whole episode on the phone CPU,
    which is the single most expensive step. Pass --audio-format mp3 if you
    actually need MP3.
  * Sockets have timeouts and downloads are chunked, so a stalled connection
    fails fast and retries instead of hanging forever.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

import yt_dlp

CHANNEL_URL = "https://www.youtube.com/@natebjones/videos"
EPISODES_FILE = "episodes.json"
MANIFEST_NAME = ".episodes.json"
AUDIO_DIR = "audio"
SHORTS_SUBDIR = "Shorts"
YOUTUBE_RSS_CACHE = ".channel_id"
SHORTS_MAX_DURATION = 180  # Videos under 3 minutes go to Shorts folder

# Containers we may end up with, in the order we prefer to find them on disk.
AUDIO_EXTS = (".m4a", ".mp3", ".opus", ".ogg", ".webm", ".aac", ".flac", ".wav", ".mka")
THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")

AUDIO_FORMAT_CHOICES = ("m4a", "mp3", "keep")
# m4a keeps YouTube's own AAC stream byte for byte. MP3 is not something
# YouTube serves, so asking for it means decoding their audio and re-encoding
# it into an older, weaker codec - worse quality and a slow extra pass.
DEFAULT_AUDIO_FORMAT = "m4a"

# Ranged requests. YouTube throttles long single-connection reads hard; asking
# for the file in chunks keeps the transfer at full speed.
HTTP_CHUNK_SIZE = 10 * 1024 * 1024
SOCKET_TIMEOUT = 30

_JS_RUNTIME_CACHE = None


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


def detect_js_runtimes():
    """Find JavaScript runtimes yt-dlp can use for YouTube's challenges.

    yt-dlp only enables Deno by default. Termux ships Node, so unless we hand it
    over explicitly yt-dlp reports "No supported JavaScript runtime could be
    found", falls back to weaker player clients and loses formats.
    """
    global _JS_RUNTIME_CACHE
    if _JS_RUNTIME_CACHE is not None:
        return _JS_RUNTIME_CACHE

    try:
        from yt_dlp.globals import supported_js_runtimes

        known = set(supported_js_runtimes.value.keys())
    except Exception:
        # Older yt-dlp without pluggable runtimes; nothing to configure.
        _JS_RUNTIME_CACHE = {}
        return _JS_RUNTIME_CACHE

    found = {}
    for name in ("deno", "node", "bun", "quickjs"):
        if name not in known:
            continue
        path = shutil.which(name)
        if path:
            found[name] = {"path": path}

    if "node" in found and len(found) == 1 and not _node_version_ok(found["node"]["path"]):
        print(
            "WARNING: Node is older than v22, which yt-dlp cannot use for YouTube.\n"
            "         Run 'pkg upgrade nodejs-lts' in Termux or downloads will be slow.",
            file=sys.stderr,
        )

    _JS_RUNTIME_CACHE = found
    return found


def _node_version_ok(path, minimum=22):
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
        match = re.search(r"v(\d+)", out)
        if not match:
            return True  # Can't tell; let yt-dlp decide.
        return int(match.group(1)) >= minimum
    except Exception:
        return True  # Can't tell; let yt-dlp decide.


# --------------------------------------------------------------------------
# Episode index
# --------------------------------------------------------------------------


def resolve_episodes_file(explicit, output_dir):
    """Pick where to keep the episode index.

    When the audio lands outside the checkout (the phone writes to
    ~/storage/shared/Podcasts) the index lives next to the audio. That keeps the
    app's manifest with the files it describes, and stops us writing to a
    tracked file in the repo, which used to make the next 'git pull' fail.
    """
    if explicit:
        return explicit
    env = os.environ.get("YTP_EPISODES_FILE")
    if env:
        return env

    out_abs = os.path.abspath(output_dir)
    cwd_abs = os.path.abspath(os.getcwd())
    if out_abs == cwd_abs or out_abs.startswith(cwd_abs + os.sep):
        return EPISODES_FILE
    return os.path.join(out_abs, MANIFEST_NAME)


def load_episodes(path=EPISODES_FILE):
    """Load existing episodes from JSON file."""
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (ValueError, OSError):
            return []
    return []


def save_episodes(episodes, path=EPISODES_FILE):
    """Save episodes to JSON file, atomically so a crash can't truncate it."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(episodes, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        # Don't leave a half-written temp file lying next to the audio.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def get_existing_ids(episodes):
    """Get set of already-downloaded video IDs."""
    return {ep["id"] for ep in episodes if ep.get("id")}


# --------------------------------------------------------------------------
# Channel listing
# --------------------------------------------------------------------------


def _load_channel_id_cache():
    """Cached channel IDs, keyed by channel URL."""
    try:
        with open(YOUTUBE_RSS_CACHE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        # Older versions stored a single bare ID here with no idea which channel
        # it belonged to. Ignore it rather than risk answering for the wrong one.
        return {}


def _store_channel_id(channel_url, channel_id):
    cache = _load_channel_id_cache()
    cache[channel_url] = channel_id
    try:
        with open(YOUTUBE_RSS_CACHE, "w") as f:
            json.dump(cache, f, indent=2)
    except OSError:
        pass


def discover_channel_id(channel_url=CHANNEL_URL):
    """Use yt-dlp to discover the channel ID from the handle."""
    cached = _load_channel_id_cache().get(channel_url)
    if cached:
        return cached

    print("Discovering channel ID...")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": 1,
        "socket_timeout": SOCKET_TIMEOUT,
    }
    runtimes = detect_js_runtimes()
    if runtimes:
        ydl_opts["js_runtimes"] = runtimes
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if info:
                channel_id = info.get("channel_id") or info.get("id", "")
                if channel_id and channel_id.startswith("UC"):
                    print(f"Found channel ID: {channel_id}")
                    _store_channel_id(channel_url, channel_id)
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
        with urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT) as response:
            xml_data = response.read()
        root = ET.fromstring(xml_data)
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "yt": "http://www.youtube.com/xml/schemas/2015",
        }
        entries = root.findall("atom:entry", ns)
        videos = []
        for entry in entries:
            video_id_el = entry.find("yt:videoId", ns)
            title_el = entry.find("atom:title", ns)
            title = title_el.text if title_el is not None else ""
            if video_id_el is not None:
                videos.append({"id": video_id_el.text, "title": title})
        print(f"Found {len(videos)} videos from RSS feed.")
        return videos
    except Exception as e:
        print(f"RSS feed fetch failed: {e}")
        return []


def fetch_videos_from_ytdlp(max_episodes, channel_url=CHANNEL_URL):
    """Fetch list of recent videos using yt-dlp (fallback)."""
    print("Trying yt-dlp to fetch video list...")
    ydl_opts = {
        "extract_flat": True,
        "quiet": False,
        "no_warnings": False,
        "playlistend": max_episodes * 3,
        "socket_timeout": SOCKET_TIMEOUT,
    }
    runtimes = detect_js_runtimes()
    if runtimes:
        ydl_opts["js_runtimes"] = runtimes
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if info and "entries" in info:
                videos = [e for e in info["entries"] if e]
                print(f"Found {len(videos)} videos via yt-dlp.")
                return videos
    except Exception as e:
        print(f"yt-dlp fetch failed: {e}")
    return []


def fetch_video_list(max_episodes, channel_url=CHANNEL_URL):
    """Fetch list of recent videos, trying RSS first then yt-dlp."""
    channel_id = discover_channel_id(channel_url)
    if channel_id:
        videos = fetch_videos_from_rss(channel_id)
        if videos:
            return videos

    return fetch_videos_from_ytdlp(max_episodes, channel_url)


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------


def sanitize_filename(title):
    """Create a safe filename from a video title.

    Strip after truncating too: shared storage is FAT/exFAT, which silently
    drops trailing spaces and dots, and that would break the path we record in
    the episode index.
    """
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in title)
    return safe.strip()[:80].strip(" .") or "episode"


def is_short_video(duration, title=""):
    """Determine if a video is a Short based on duration and title."""
    if duration and duration <= SHORTS_MAX_DURATION:
        return True
    title_lower = title.lower()
    hashtag_count = title_lower.count("#")
    if hashtag_count >= 2 and (not duration or duration <= SHORTS_MAX_DURATION):
        return True
    return False


def build_ydl_opts(output_template, audio_format, embed_thumbnail, polite):
    """Assemble yt-dlp options tuned for downloading on a phone."""
    opts = {
        "outtmpl": output_template,
        "quiet": False,
        "no_warnings": False,
        "writethumbnail": True,
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": 10,
        "fragment_retries": 10,
        "http_chunk_size": HTTP_CHUNK_SIZE,
        "concurrent_fragment_downloads": 4,
        "noplaylist": True,
    }

    runtimes = detect_js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes

    # Only throttle ourselves when walking a whole channel. For a single video
    # the sleep was pure waiting.
    if polite:
        opts["sleep_interval"] = 1
        opts["max_sleep_interval"] = 3

    postprocessors = []
    if audio_format == "mp3":
        # Any source codec, then a full re-encode. Slowest option.
        opts["format"] = "bestaudio/best"
        postprocessors.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        )
    elif audio_format == "m4a":
        # Prefer YouTube's AAC stream. yt-dlp sees the file is already m4a and
        # skips ffmpeg entirely, so there is no transcode at all.
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        postprocessors.append({"key": "FFmpegExtractAudio", "preferredcodec": "m4a"})
    else:  # "keep" - whatever container YouTube served, no audio processing.
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"

    # Convert the thumbnail to jpg so both the tag and the sidecar are readable
    # by Android. already_have_thumbnail keeps the sidecar file on disk; without
    # it yt-dlp deletes the image after embedding and the app has no artwork.
    postprocessors.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})
    if embed_thumbnail:
        postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": True})

    opts["postprocessors"] = postprocessors
    return opts


class _Progress:
    """Compact, throttled progress output that never looks stalled."""

    def __init__(self, interval=1.5):
        self.interval = interval
        self._last = 0.0

    def hook(self, d):
        status = d.get("status")
        if status == "downloading":
            now = time.monotonic()
            if now - self._last < self.interval:
                return
            self._last = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            speed = (d.get("speed") or 0) / 1024.0
            if total:
                print(
                    f"  {done * 100.0 / total:5.1f}%  "
                    f"{done / 1048576:.1f}/{total / 1048576:.1f} MB  {speed:.0f} KB/s"
                )
            else:
                print(f"  {done / 1048576:.1f} MB  {speed:.0f} KB/s")
        elif status == "finished":
            print("  Download finished.")

    def pp_hook(self, d):
        if d.get("status") == "started":
            name = d.get("postprocessor", "")
            if name == "FFmpegExtractAudio":
                print("  Converting audio (this is the slow part)...")
            elif name == "EmbedThumbnail":
                print("  Adding cover art...")


def _find_downloaded_file(info, search_dir, video_id):
    """Locate the final audio file yt-dlp produced."""
    for entry in info.get("requested_downloads") or []:
        path = entry.get("filepath")
        if path and os.path.exists(path):
            return path

    try:
        names = os.listdir(search_dir)
    except OSError:
        return None
    for ext in AUDIO_EXTS:
        for name in names:
            if name.startswith(video_id) and name.lower().endswith(ext):
                return os.path.join(search_dir, name)
    return None


def _find_thumbnail(search_dir, stem):
    for ext in THUMB_EXTS:
        candidate = os.path.join(search_dir, stem + ext)
        if os.path.exists(candidate):
            return candidate
    return None


def _move_unique(src, dst):
    """Move src to dst, returning the path actually used."""
    if os.path.abspath(src) == os.path.abspath(dst):
        return dst
    if os.path.exists(dst):
        return src
    shutil.move(src, dst)
    return dst


def download_audio(
    video_id,
    output_dir,
    audio_format=DEFAULT_AUDIO_FORMAT,
    embed_thumbnail=True,
    polite=False,
):
    """Download audio for one video. Returns a metadata dict, or None on failure.

    Everything comes from a single yt-dlp extraction. Shorts are sorted into a
    subfolder afterwards, using the duration that extraction already gave us.
    """
    os.makedirs(output_dir, exist_ok=True)
    started = time.monotonic()

    progress = _Progress()
    ydl_opts = build_ydl_opts(
        os.path.join(output_dir, "%(id)s.%(ext)s"),
        audio_format,
        embed_thumbnail,
        polite,
    )
    ydl_opts["progress_hooks"] = [progress.hook]
    ydl_opts["postprocessor_hooks"] = [progress.pp_hook]

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        print(f"  Error downloading {video_id}: {e}", file=sys.stderr)
        return None

    if not info:
        return None

    title = info.get("title", "Unknown Title")
    duration = info.get("duration", 0) or 0
    audio_path = _find_downloaded_file(info, output_dir, video_id)
    if not audio_path:
        print(f"  Downloaded {video_id} but could not find the audio file.", file=sys.stderr)
        return None

    thumb_path = _find_thumbnail(output_dir, video_id)

    # Sort into Shorts/ now that we know how long it is.
    is_short = is_short_video(duration, title)
    target_dir = os.path.join(output_dir, SHORTS_SUBDIR) if is_short else output_dir
    if is_short:
        os.makedirs(target_dir, exist_ok=True)
        print(f"  -> Short ({duration}s), filing under {SHORTS_SUBDIR}/")
    elif duration:
        print(f"  -> Regular video ({duration}s)")

    # Give both files a human-readable name that matches the episode title.
    safe_stem = sanitize_filename(title)
    audio_ext = os.path.splitext(audio_path)[1]
    audio_path = _move_unique(audio_path, os.path.join(target_dir, safe_stem + audio_ext))

    if thumb_path:
        # Name the artwork after where the audio actually landed. If the title
        # collided and the audio kept its video-id name, a thumbnail named after
        # the title would no longer match it and the app would show no artwork.
        final_stem = os.path.splitext(os.path.basename(audio_path))[0]
        thumb_ext = os.path.splitext(thumb_path)[1]
        thumb_path = _move_unique(
            thumb_path,
            os.path.join(os.path.dirname(audio_path), final_stem + thumb_ext),
        )

    filesize = os.path.getsize(audio_path) if os.path.exists(audio_path) else 0
    index_dir = os.path.abspath(output_dir)

    def _rel(path):
        if not path:
            return None
        try:
            return os.path.relpath(os.path.abspath(path), index_dir).replace(os.sep, "/")
        except ValueError:
            return os.path.basename(path)

    elapsed = time.monotonic() - started
    print(f"  Took {elapsed:.0f}s")

    return {
        "id": video_id,
        "title": title,
        "description": info.get("description", ""),
        "upload_date": info.get("upload_date", ""),
        "duration": duration,
        # filename stays a bare name so the RSS feed's release URL keeps working
        "filename": os.path.basename(audio_path),
        # relpath is what the Android app uses to find the file on disk
        "relpath": _rel(audio_path),
        "thumbnail": os.path.basename(thumb_path) if thumb_path else None,
        "thumbnail_relpath": _rel(thumb_path),
        "filesize": filesize,
        "is_short": is_short,
        "audio_format": audio_ext.lstrip(".").lower(),
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "published": False,
    }


def extract_video_id(url):
    """Extract video ID from a YouTube URL."""
    patterns = [
        r"(?:v=|/v/|youtu\.be/|/shorts/|/live/|/embed/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",  # bare video ID
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Download YouTube audio as podcast episodes"
    )
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
        help="Directory to save audio files (default: audio/)",
    )
    parser.add_argument(
        "--url",
        type=str,
        default=None,
        help="Download a specific YouTube video URL instead of checking the channel",
    )
    parser.add_argument(
        "--channel-url",
        type=str,
        default=CHANNEL_URL,
        help="Channel to check in channel mode",
    )
    parser.add_argument(
        "--audio-format",
        choices=AUDIO_FORMAT_CHOICES,
        default=DEFAULT_AUDIO_FORMAT,
        help=(
            "m4a: keep YouTube's AAC stream, no re-encoding (default, fastest). "
            "mp3: re-encode to MP3, much slower on a phone. "
            "keep: whatever container YouTube served."
        ),
    )
    parser.add_argument(
        "--no-embed-thumbnail",
        action="store_true",
        help="Skip writing cover art into the audio file",
    )
    parser.add_argument(
        "--episodes-file",
        type=str,
        default=None,
        help="Where to keep the episode index (default: next to the audio)",
    )
    args = parser.parse_args()

    episodes_file = resolve_episodes_file(args.episodes_file, args.output_dir)
    embed_thumbnail = not args.no_embed_thumbnail

    print(f"Loading existing episodes from {episodes_file}...")
    episodes = load_episodes(episodes_file)
    existing_ids = get_existing_ids(episodes)
    print(f"Found {len(episodes)} existing episodes.")

    runtimes = detect_js_runtimes()
    if runtimes:
        print(f"JavaScript runtime: {', '.join(sorted(runtimes))}")

    started = time.monotonic()

    # Single video mode
    if args.url:
        video_id = extract_video_id(args.url)
        if not video_id:
            print(f"ERROR: Could not extract video ID from: {args.url}")
            sys.exit(1)
        if video_id in existing_ids:
            print(f"Already downloaded: {video_id}")
            return
        print(f"Downloading single video: {args.url}")
        metadata = download_audio(
            video_id,
            args.output_dir,
            audio_format=args.audio_format,
            embed_thumbnail=embed_thumbnail,
            polite=False,
        )
        if metadata:
            episodes.append(metadata)
            save_episodes(episodes, episodes_file)
            print(
                f"Saved: {metadata['filename']} "
                f"({metadata['filesize'] / 1024 / 1024:.1f} MB) "
                f"in {time.monotonic() - started:.0f}s"
            )
        else:
            print("Download failed.")
            sys.exit(1)
        return

    # Channel mode
    print("Fetching video list...")
    videos = fetch_video_list(args.max_episodes, args.channel_url)

    if not videos:
        print("WARNING: Could not fetch any videos. YouTube may be blocking this IP.")
        print("Try running locally or check the logs for details.")
        return

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

        metadata = download_audio(
            video_id,
            args.output_dir,
            audio_format=args.audio_format,
            embed_thumbnail=embed_thumbnail,
            polite=len(new_videos) > 1,
        )
        if metadata:
            episodes.append(metadata)
            downloaded += 1
            save_episodes(episodes, episodes_file)
            print(
                f"  Saved: {metadata['filename']} "
                f"({metadata['filesize'] / 1024 / 1024:.1f} MB)"
            )
        else:
            print(f"  Failed to download {video_id}")

    episodes.sort(key=lambda x: x.get("upload_date", ""), reverse=True)
    save_episodes(episodes, episodes_file)
    print(
        f"\nDone! Downloaded {downloaded} new episodes in "
        f"{time.monotonic() - started:.0f}s. Total: {len(episodes)} episodes."
    )


if __name__ == "__main__":
    main()
