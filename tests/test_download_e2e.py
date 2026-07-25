"""A real download, over the real network, with the real yt-dlp and ffmpeg.

Everything else in this suite fakes ``yt_dlp.YoutubeDL``, which means it proves
the options we *pass* are right and nothing about what actually comes back.
That gap is how EmbedThumbnail deleting the artwork sidecar, and the episode
index being written into the git checkout, both shipped past a green suite.

The source is archive.org rather than YouTube for two reasons: YouTube blocks
datacenter IPs with a bot check, and hammering it from CI is how you get an
account or an IP banned.  Everything these tests exercise -- format selection,
the ffmpeg postprocessor chain, thumbnail handling, filename sanitising, the
manifest -- is site-agnostic.  The YouTube-only part (the player JS challenge)
cannot be covered here at all; that is what the phone is for.

Skipped unless YTP_E2E=1, so the ordinary suite stays offline and fast:

    YTP_E2E=1 python3 -m pytest tests/test_download_e2e.py -v
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

pytestmark = pytest.mark.skipif(
    os.environ.get("YTP_E2E") != "1",
    reason="network test; set YTP_E2E=1 to run",
)

# A small public-domain file on archive.org.  Deliberately tiny: this runs over
# a real network and should not move megabytes to prove a code path.
SOURCE_URL = "https://archive.org/details/testmp3testfile"


@pytest.fixture(scope="module")
def downloader():
    import download_audio

    return download_audio


@pytest.fixture(scope="module")
def fetched(downloader, tmp_path_factory):
    """Run the app's own yt-dlp options against a real URL, once."""
    out = tmp_path_factory.mktemp("e2e")
    opts = downloader.build_ydl_opts(
        str(out / "%(id)s.%(ext)s"),
        downloader.DEFAULT_AUDIO_FORMAT,
        True,   # embed_thumbnail
        False,  # polite
    )
    import yt_dlp

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(SOURCE_URL, download=True)
    if info.get("_type") == "playlist":
        info = info["entries"][0]
    return out, info


def test_ffmpeg_is_actually_available(downloader):
    """Every postprocessor silently degrades without it."""
    location = downloader.build_ydl_opts("x", "m4a", True, False).get("ffmpeg_location")
    binary = location or shutil.which("ffmpeg")
    assert binary, "no ffmpeg: conversion and thumbnail embedding cannot work"
    result = subprocess.run(
        [binary if os.path.isfile(str(binary)) else "ffmpeg", "-version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_a_real_audio_file_lands_on_disk(fetched):
    out, _info = fetched
    audio = [p for p in out.iterdir() if p.suffix.lower() in (".m4a", ".mp3", ".webm", ".opus")]
    assert audio, f"nothing downloaded; got {[p.name for p in out.iterdir()]}"
    assert audio[0].stat().st_size > 1024, "the file is too small to be real audio"


def test_the_file_is_playable_audio_not_an_error_page(fetched):
    """A 403 saved to disk is still a file. Make ffprobe confirm it decodes."""
    out, _info = fetched
    audio = next(p for p in out.iterdir() if p.suffix.lower() in (".m4a", ".mp3", ".webm", ".opus"))
    probe = shutil.which("ffprobe")
    if not probe:
        pytest.skip("ffprobe not installed")
    result = subprocess.run(
        [probe, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_type,duration", "-of", "json", str(audio)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"ffprobe rejected the file: {result.stderr}"
    streams = json.loads(result.stdout).get("streams", [])
    assert streams and streams[0]["codec_type"] == "audio"


def test_extraction_reports_a_duration(fetched):
    """The manifest and the Downloads list both depend on this."""
    _out, info = fetched
    assert info.get("duration"), "no duration: episode rows would show a blank"


def test_the_thumbnail_sidecar_survives_embedding(fetched):
    """EmbedThumbnail deletes the image unless already_have_thumbnail is set.

    That exact bug shipped: episodes had cover art inside the file but no .jpg
    beside it, so the app's Downloads tab had nothing to show.
    """
    out, _info = fetched
    opts = None
    import download_audio

    opts = download_audio.build_ydl_opts("x", "m4a", True, False)
    embed = [p for p in opts["postprocessors"] if p["key"] == "EmbedThumbnail"]
    assert embed, "thumbnail embedding is not configured at all"
    assert embed[0].get("already_have_thumbnail") is True, (
        "EmbedThumbnail will delete the sidecar image the app needs"
    )
    # archive.org items may genuinely have no thumbnail; only assert the
    # sidecar survived when one was fetched at all.
    images = [p for p in out.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    if images:
        assert images[0].stat().st_size > 0


def test_the_episode_index_never_lands_in_the_checkout(downloader, tmp_path):
    """Writing episodes.json into the repo makes the next git pull fail."""
    target = downloader.resolve_episodes_file(None, str(tmp_path))
    assert Path(target).parent == tmp_path
    assert REPO_ROOT not in Path(target).parents, (
        "the episode index would be written into the git checkout"
    )


def test_a_saved_manifest_has_what_the_app_reads(downloader, fetched, tmp_path):
    """The app's Downloads tab is built entirely from these fields."""
    out, info = fetched
    episodes_file = downloader.resolve_episodes_file(None, str(out))
    entry = {
        "id": info.get("id", "e2e"),
        "title": info.get("title", "Untitled"),
        "duration": info.get("duration", 0),
        "filename": "x.m4a",
        "relpath": "x.m4a",
        "downloaded_at": "2026-07-25T00:00:00",
    }
    downloader.save_episodes([entry], episodes_file)

    saved = json.loads(Path(episodes_file).read_text())
    assert isinstance(saved, list) and saved
    for field in ("id", "title", "duration", "relpath", "downloaded_at"):
        assert field in saved[0], f"the app reads {field!r} and it is missing"
