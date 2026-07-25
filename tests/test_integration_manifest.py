"""The downloader writes the episode index; the app reads it.

Both sides have their own tests, but those would keep passing if the two
schemas drifted apart - the downloader would write a manifest the app quietly
ignores, and the Downloads tab would go empty. These tests run the real
download_audio.py (with a fake yt-dlp) and then hand the result to the real
app scanner, so a schema change on either side fails here.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from _ytp_download_audio_fakes import DownloadPlan, FakeYoutubeDLFactory  # noqa: E402

import download_audio as da  # noqa: E402


def _app_module():
    """Import app/main.py the way the smoke harness does.

    Kivy needs a display to create a Window even with the mock GL backend, so
    skip rather than fail when pytest is run bare instead of through
    tests/run_all.sh (which wraps everything in xvfb).
    """
    harness = pytest.importorskip("harness", reason="app harness not available")
    try:
        return harness.load_app_module()
    except (Exception, SystemExit) as exc:
        # Kivy calls sys.exit() when it cannot get a Window, so SystemExit
        # has to be caught too - it is not an Exception subclass.
        pytest.skip(f"cannot load the app module here ({exc}); use tests/run_all.sh")


def _scan_with_app(app_module, base_dir, folder_name):
    """Run the app's folder scanner without instantiating the whole app."""
    app = app_module.YouTubePodcastApp()
    return app._scan_folder(os.path.join(base_dir, folder_name), folder_name)


def _download(tmp_path, plan, audio_format="m4a"):
    out = tmp_path / "Podcasts" / "General"
    out.mkdir(parents=True)
    factory = FakeYoutubeDLFactory(plan)
    original = da.yt_dlp.YoutubeDL
    da.yt_dlp.YoutubeDL = factory
    try:
        meta = da.download_audio(plan.video_id, str(out), audio_format=audio_format)
    finally:
        da.yt_dlp.YoutubeDL = original
    assert meta is not None
    index = os.path.join(str(out), da.MANIFEST_NAME)
    da.save_episodes([meta], index)
    return out, meta


class TestManifestRoundTrip:
    def test_app_sees_the_episode_the_downloader_wrote(self, tmp_path):
        app_module = _app_module()
        plan = DownloadPlan(
            video_id="A1M5hduoj3E",
            title="You're Not Good at Predicting Things",
            duration=947,
            audio_ext=".m4a",
            thumb_ext=".jpg",
        )
        out, meta = _download(tmp_path, plan)

        found = _scan_with_app(app_module, str(out.parent), "General")
        assert len(found) == 1, "the app found no episode for a manifest we just wrote"

        episode = found[0]
        assert episode["title"] == meta["title"]
        assert episode["id"] == meta["id"]
        assert episode["duration"] == meta["duration"]
        assert episode["filesize"] == meta["filesize"]
        assert os.path.exists(episode["path"])
        assert episode["thumb"] and os.path.exists(episode["thumb"])

    def test_artwork_is_present_for_the_now_playing_view(self, tmp_path):
        """The cover art must survive the download - EmbedThumbnail deletes it
        unless already_have_thumbnail is set."""
        app_module = _app_module()
        plan = DownloadPlan(video_id="ART00000001", title="Has Artwork", duration=600)
        out, _meta = _download(tmp_path, plan)

        episode = _scan_with_app(app_module, str(out.parent), "General")[0]
        assert episode["thumb"] is not None
        assert episode["thumb"].lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        # Same stem as the audio, which is how the app finds it for old episodes.
        assert os.path.splitext(os.path.basename(episode["thumb"]))[0] == os.path.splitext(
            os.path.basename(episode["path"])
        )[0]

    def test_short_filed_in_subfolder_is_still_found(self, tmp_path):
        app_module = _app_module()
        plan = DownloadPlan(video_id="SHORT000001", title="Quick Take", duration=45)
        out, meta = _download(tmp_path, plan)

        assert meta["is_short"] is True
        assert meta["relpath"].startswith("Shorts/"), meta["relpath"]

        found = _scan_with_app(app_module, str(out.parent), "General")
        titles = [e["title"] for e in found]
        assert titles.count("Quick Take") == 1, (
            "a Short must be listed exactly once - the manifest entry and the "
            f"loose-file fallback both matched: {titles}"
        )

    @pytest.mark.parametrize("audio_format", ["m4a", "mp3", "keep"])
    def test_every_audio_format_produces_a_readable_episode(self, tmp_path, audio_format):
        app_module = _app_module()
        ext = {"m4a": ".m4a", "mp3": ".mp3", "keep": ".webm"}[audio_format]
        plan = DownloadPlan(
            video_id="FMT00000001", title="Format Check", duration=900, audio_ext=ext
        )
        out, meta = _download(tmp_path, plan, audio_format=audio_format)

        found = _scan_with_app(app_module, str(out.parent), "General")
        assert len(found) == 1
        assert found[0]["path"].endswith(ext)
        # The app knows how to hand this extension to another app.
        assert os.path.splitext(meta["filename"])[1].lower() in app_module.MIME_BY_EXT

    def test_deleting_in_the_app_removes_the_downloaders_entry(self, tmp_path):
        app_module = _app_module()
        plan = DownloadPlan(video_id="DEL00000001", title="Delete Me", duration=700)
        out, _meta = _download(tmp_path, plan)

        app = app_module.YouTubePodcastApp()
        episode = _scan_with_app(app_module, str(out.parent), "General")[0]
        audio, thumb, manifest = episode["path"], episode["thumb"], episode["manifest"]

        app.delete_episode(episode)

        assert not os.path.exists(audio)
        assert not os.path.exists(thumb)
        with open(manifest) as f:
            assert json.load(f) == [], "manifest entry should be gone"
        assert _scan_with_app(app_module, str(out.parent), "General") == []
