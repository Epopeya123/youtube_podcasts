"""Tests for download_audio.py.

No network: YouTube blocks this container (media fetches 403), so every download
goes through `FakeYoutubeDLFactory` from `_ytp_download_audio_fakes`, which
writes the files a real yt-dlp run would have written and returns a realistic
info dict.

The tests that look oddly specific (single extraction per video, no
FFmpegExtractAudio for "keep", `already_have_thumbnail: True`) are regression
guards for the slow-download / missing-artwork bugs this module was rewritten to
fix. Please do not relax them without reading the docstring at the top of
download_audio.py.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import types

import pytest

# Work no matter how the rest of tests/ is laid out (package or not, whatever
# import mode pytest picks).
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from _ytp_download_audio_fakes import (  # noqa: E402  (needs the path bootstrap above)
    REPO_ROOT,
    DownloadPlan,
    FakeYoutubeDLFactory,
)

import download_audio as da  # noqa: E402
import yt_dlp  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_module_state(monkeypatch):
    """Keep global state and the ambient environment out of the tests.

    detect_js_runtimes() memoises into a module global and may shell out to
    `node --version`; default it to "already detected, nothing found" so the
    other tests are deterministic and never spawn a subprocess. Tests that care
    about detection override this themselves.
    """
    monkeypatch.setattr(da, "_JS_RUNTIME_CACHE", {}, raising=False)
    monkeypatch.setattr(da, "_LAST_DOWNLOAD_BLOCKED", False, raising=False)
    monkeypatch.setattr(da, "_LAST_DOWNLOAD_BOT_CHECKED", False, raising=False)
    monkeypatch.setattr(da, "_LAST_LISTING_BOT_CHECKED", False, raising=False)
    # Keep a developer's real cookie file out of the tests.
    monkeypatch.setattr(
        da, "DEFAULT_COOKIES_FILE", "/nonexistent/ytp-test-cookies.txt", raising=False
    )
    monkeypatch.delenv("YTP_EPISODES_FILE", raising=False)
    monkeypatch.delenv("YTP_COOKIES_FILE", raising=False)
    monkeypatch.delenv("YTP_PLAYER_CLIENTS", raising=False)


@pytest.fixture
def outdir(tmp_path):
    d = tmp_path / "audio"
    d.mkdir()
    return d


def _install_fake_ytdlp(monkeypatch, plan=None):
    factory = FakeYoutubeDLFactory(plan)
    monkeypatch.setattr(da.yt_dlp, "YoutubeDL", factory)
    return factory


def _pp_keys(opts):
    return [pp["key"] for pp in opts["postprocessors"]]


def _pp(opts, key):
    matches = [pp for pp in opts["postprocessors"] if pp["key"] == key]
    return matches[0] if matches else None


def _make_stub_exe(directory, name, body="#!/bin/sh\necho v22.11.0\n"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ---------------------------------------------------------------------------
# extract_video_id
# ---------------------------------------------------------------------------


class TestExtractVideoId:
    @pytest.mark.parametrize(
        "url,expected",
        [
            # Canonical watch URLs
            ("https://www.youtube.com/watch?v=A1M5hduoj3E", "A1M5hduoj3E"),
            ("http://youtube.com/watch?v=A1M5hduoj3E", "A1M5hduoj3E"),
            ("https://m.youtube.com/watch?v=A1M5hduoj3E", "A1M5hduoj3E"),
            # Extra query params after the id (the YouTube app's share format)
            (
                "https://www.youtube.com/watch?v=A1M5hduoj3E&pp=ugUEEgJlbg%3D%3D",
                "A1M5hduoj3E",
            ),
            ("https://www.youtube.com/watch?v=A1M5hduoj3E&t=42s", "A1M5hduoj3E"),
            # Params *before* the id
            ("https://www.youtube.com/watch?app=desktop&v=A1M5hduoj3E", "A1M5hduoj3E"),
            # Playlist context
            (
                "https://www.youtube.com/watch?v=A1M5hduoj3E&list=PL123&index=2",
                "A1M5hduoj3E",
            ),
            # Short links
            ("https://youtu.be/A1M5hduoj3E", "A1M5hduoj3E"),
            ("https://youtu.be/A1M5hduoj3E?si=Xy_9-abcDEF", "A1M5hduoj3E"),
            # Shorts / live / embed / old /v/
            ("https://www.youtube.com/shorts/A1M5hduoj3E", "A1M5hduoj3E"),
            ("https://www.youtube.com/shorts/A1M5hduoj3E?feature=share", "A1M5hduoj3E"),
            ("https://www.youtube.com/live/A1M5hduoj3E", "A1M5hduoj3E"),
            ("https://www.youtube.com/embed/A1M5hduoj3E", "A1M5hduoj3E"),
            ("https://www.youtube.com/v/A1M5hduoj3E", "A1M5hduoj3E"),
            # Bare ids, including the awkward characters
            ("A1M5hduoj3E", "A1M5hduoj3E"),
            ("_-aBcDeF123", "_-aBcDeF123"),
            ("-A1M5hduoj3", "-A1M5hduoj3"),
        ],
    )
    def test_recognised_forms(self, url, expected):
        assert da.extract_video_id(url) == expected

    @pytest.mark.parametrize(
        "shared",
        [
            "Check this out https://www.youtube.com/watch?v=A1M5hduoj3E",
            "Check this out https://youtu.be/A1M5hduoj3E?si=abc — great episode",
            "https://www.youtube.com/shorts/A1M5hduoj3E\nshared via YouTube",
            "watch this later: https://www.youtube.com/watch?v=A1M5hduoj3E&pp=ugUEEgJlbg%3D%3D",
        ],
    )
    def test_url_embedded_in_shared_text(self, shared):
        """Android's share sheet hands us a sentence, not a bare URL."""
        assert da.extract_video_id(shared) == "A1M5hduoj3E"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "not a url",
            "https://example.com/",
            "https://example.com/watch",
            "https://vimeo.com/123456789",
            "https://www.youtube.com/@natebjones",
            "https://www.youtube.com/@natebjones/videos",
            "abc",  # too short for a bare id
            "abcdefghijkl",  # 12 chars: not a bare id either
            "https://www.youtube.com/watch?v=short",  # truncated id
        ],
    )
    def test_unrecognised_input_returns_none(self, value):
        assert da.extract_video_id(value) is None

    def test_bare_id_must_be_exactly_eleven_chars(self):
        assert da.extract_video_id("a" * 11) == "a" * 11
        assert da.extract_video_id("a" * 10) is None
        assert da.extract_video_id("a" * 12) is None


# ---------------------------------------------------------------------------
# resolve_episodes_file
# ---------------------------------------------------------------------------


class TestResolveEpisodesFile:
    def test_explicit_argument_wins_over_everything(self, tmp_path, monkeypatch):
        monkeypatch.setenv("YTP_EPISODES_FILE", str(tmp_path / "from_env.json"))
        explicit = str(tmp_path / "explicit.json")
        assert da.resolve_episodes_file(explicit, str(tmp_path / "elsewhere")) == explicit

    def test_env_var_used_when_no_explicit_argument(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        env_path = str(tmp_path / "from_env.json")
        monkeypatch.setenv("YTP_EPISODES_FILE", env_path)
        # Even for an output dir inside the cwd, the env var still wins.
        assert da.resolve_episodes_file(None, "audio") == env_path

    def test_empty_env_var_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YTP_EPISODES_FILE", "")
        assert da.resolve_episodes_file(None, "audio") == da.EPISODES_FILE

    @pytest.mark.parametrize("relative", ["audio", "./audio", "audio/subdir", "."])
    def test_output_dir_inside_cwd_uses_repo_episodes_json(
        self, tmp_path, monkeypatch, relative
    ):
        monkeypatch.chdir(tmp_path)
        assert da.resolve_episodes_file(None, relative) == da.EPISODES_FILE

    def test_output_dir_equal_to_cwd_uses_repo_episodes_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert da.resolve_episodes_file(None, os.getcwd()) == da.EPISODES_FILE

    def test_output_dir_outside_cwd_uses_hidden_manifest_next_to_audio(
        self, tmp_path, monkeypatch
    ):
        """The phone writes to ~/storage/shared/Podcasts.

        Writing the repo-tracked episodes.json there used to leave the checkout
        dirty and break the next `git pull`, so the index has to live with the
        audio instead.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        podcasts = tmp_path / "storage" / "Podcasts"
        podcasts.mkdir(parents=True)
        monkeypatch.chdir(repo)

        result = da.resolve_episodes_file(None, str(podcasts))

        assert result == os.path.join(os.path.abspath(str(podcasts)), da.MANIFEST_NAME)
        assert os.path.basename(result) == ".episodes.json"
        assert result != da.EPISODES_FILE
        assert os.path.isabs(result)

    def test_sibling_dir_with_shared_prefix_is_outside_cwd(self, tmp_path, monkeypatch):
        """`/x/repo-audio` must not be mistaken for a child of `/x/repo`."""
        repo = tmp_path / "repo"
        repo.mkdir()
        sibling = tmp_path / "repo-audio"
        sibling.mkdir()
        monkeypatch.chdir(repo)

        result = da.resolve_episodes_file(None, str(sibling))

        assert result == os.path.join(os.path.abspath(str(sibling)), da.MANIFEST_NAME)

    def test_parent_of_cwd_is_outside_cwd(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        result = da.resolve_episodes_file(None, "..")

        assert result.endswith(da.MANIFEST_NAME)
        assert result == os.path.join(os.path.abspath(".."), da.MANIFEST_NAME)


# ---------------------------------------------------------------------------
# build_ydl_opts
# ---------------------------------------------------------------------------


class TestBuildYdlOpts:
    def test_m4a_prefers_the_native_aac_stream(self, tmp_path):
        opts = da.build_ydl_opts(str(tmp_path / "%(id)s.%(ext)s"), "m4a", True, False)
        # bestaudio[ext=m4a] first means yt-dlp downloads AAC and ffmpeg has
        # nothing to transcode.
        assert opts["format"].startswith("bestaudio[ext=m4a]")
        assert opts["format"] == "bestaudio[ext=m4a]/bestaudio/best"
        extract = _pp(opts, "FFmpegExtractAudio")
        assert extract is not None
        assert extract["preferredcodec"] == "m4a"

    def test_keep_prefers_m4a_and_adds_no_audio_postprocessor(self, tmp_path):
        opts = da.build_ydl_opts(str(tmp_path / "%(id)s.%(ext)s"), "keep", True, False)
        assert opts["format"].startswith("bestaudio[ext=m4a]")
        # No FFmpegExtractAudio at all: not even a remux pass.
        assert "FFmpegExtractAudio" not in _pp_keys(opts)

    def test_mp3_requests_any_audio_and_re_encodes(self, tmp_path):
        opts = da.build_ydl_opts(str(tmp_path / "%(id)s.%(ext)s"), "mp3", True, False)
        assert opts["format"] == "bestaudio/best"
        assert "ext=m4a" not in opts["format"]
        extract = _pp(opts, "FFmpegExtractAudio")
        assert extract is not None
        assert extract["preferredcodec"] == "mp3"
        assert extract["preferredquality"] == "128"

    def test_sleep_intervals_absent_when_not_polite(self, tmp_path):
        opts = da.build_ydl_opts(str(tmp_path / "%(id)s.%(ext)s"), "m4a", True, False)
        assert "sleep_interval" not in opts
        assert "max_sleep_interval" not in opts

    def test_sleep_intervals_present_when_polite(self, tmp_path):
        opts = da.build_ydl_opts(str(tmp_path / "%(id)s.%(ext)s"), "m4a", True, True)
        assert opts["sleep_interval"] == 1
        assert opts["max_sleep_interval"] == 3
        assert opts["sleep_interval"] <= opts["max_sleep_interval"]

    @pytest.mark.parametrize("audio_format", da.AUDIO_FORMAT_CHOICES)
    @pytest.mark.parametrize("polite", [False, True])
    def test_stall_protection_always_configured(self, tmp_path, audio_format, polite):
        """Without these a stalled socket hangs the download forever."""
        opts = da.build_ydl_opts(
            str(tmp_path / "%(id)s.%(ext)s"), audio_format, True, polite
        )
        assert opts["socket_timeout"] == da.SOCKET_TIMEOUT
        assert 0 < opts["socket_timeout"] <= 120
        assert opts["retries"] == 10
        assert opts["fragment_retries"] == 10
        assert opts["http_chunk_size"] == da.HTTP_CHUNK_SIZE
        assert opts["http_chunk_size"] > 0
        assert opts["concurrent_fragment_downloads"] >= 1
        assert opts["noplaylist"] is True

    @pytest.mark.parametrize("audio_format", da.AUDIO_FORMAT_CHOICES)
    def test_embed_thumbnail_always_keeps_the_sidecar(self, tmp_path, audio_format):
        """REGRESSION GUARD.

        Without `already_have_thumbnail: True` yt-dlp DELETES the .jpg sidecar
        after embedding it, and the Android app is left with no artwork.
        """
        opts = da.build_ydl_opts(
            str(tmp_path / "%(id)s.%(ext)s"), audio_format, True, False
        )
        embed = _pp(opts, "EmbedThumbnail")
        assert embed is not None, "EmbedThumbnail postprocessor is missing"
        assert embed.get("already_have_thumbnail") is True
        assert opts["writethumbnail"] is True

    def test_no_embed_thumbnail_still_converts_the_sidecar(self, tmp_path):
        opts = da.build_ydl_opts(str(tmp_path / "%(id)s.%(ext)s"), "m4a", False, False)
        assert "EmbedThumbnail" not in _pp_keys(opts)
        convertor = _pp(opts, "FFmpegThumbnailsConvertor")
        assert convertor is not None
        assert convertor["format"] == "jpg"
        assert opts["writethumbnail"] is True

    @pytest.mark.parametrize("audio_format", da.AUDIO_FORMAT_CHOICES)
    def test_thumbnail_convertor_always_present(self, tmp_path, audio_format):
        opts = da.build_ydl_opts(
            str(tmp_path / "%(id)s.%(ext)s"), audio_format, True, False
        )
        assert "FFmpegThumbnailsConvertor" in _pp_keys(opts)

    def test_postprocessor_order_extract_then_convert_then_embed(self, tmp_path):
        opts = da.build_ydl_opts(str(tmp_path / "%(id)s.%(ext)s"), "mp3", True, False)
        assert _pp_keys(opts) == [
            "FFmpegExtractAudio",
            "FFmpegThumbnailsConvertor",
            "EmbedThumbnail",
        ]

    @pytest.mark.parametrize("audio_format", da.AUDIO_FORMAT_CHOICES)
    def test_thumbnail_is_converted_before_it_is_embedded(self, tmp_path, audio_format):
        """Order matters as much as already_have_thumbnail.

        EmbedThumbnailPP deletes the sidecar whenever it had to convert the
        image itself, even with already_have_thumbnail=True. Converting to jpg
        first means EmbedThumbnail has nothing to convert and the sidecar
        survives for the Android app to display.
        """
        opts = da.build_ydl_opts(
            str(tmp_path / "%(id)s.%(ext)s"), audio_format, True, False
        )
        keys = _pp_keys(opts)
        assert keys.index("FFmpegThumbnailsConvertor") < keys.index("EmbedThumbnail")

    def test_output_template_is_passed_through(self, tmp_path):
        template = str(tmp_path / "%(id)s.%(ext)s")
        opts = da.build_ydl_opts(template, "m4a", True, False)
        assert opts["outtmpl"] == template

    def test_js_runtimes_included_only_when_detected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(da, "_JS_RUNTIME_CACHE", {})
        opts = da.build_ydl_opts(str(tmp_path / "%(id)s.%(ext)s"), "m4a", True, False)
        assert "js_runtimes" not in opts

        monkeypatch.setattr(da, "_JS_RUNTIME_CACHE", {"node": {"path": "/usr/bin/node"}})
        opts = da.build_ydl_opts(str(tmp_path / "%(id)s.%(ext)s"), "m4a", True, False)
        assert opts["js_runtimes"] == {"node": {"path": "/usr/bin/node"}}

    def test_no_cookies_and_no_client_override_by_default(self, tmp_path):
        opts = da.build_ydl_opts(str(tmp_path / "%(id)s.%(ext)s"), "m4a", True, False)
        assert "cookiefile" not in opts
        assert "extractor_args" not in opts

    def test_cookies_file_is_passed_through(self, tmp_path):
        opts = da.build_ydl_opts(
            str(tmp_path / "%(id)s.%(ext)s"),
            "m4a",
            True,
            False,
            cookies_file="/data/data/com.termux/files/home/.config/c.txt",
        )
        assert opts["cookiefile"] == "/data/data/com.termux/files/home/.config/c.txt"

    def test_player_clients_become_extractor_args(self, tmp_path):
        opts = da.build_ydl_opts(
            str(tmp_path / "%(id)s.%(ext)s"),
            "m4a",
            True,
            False,
            player_clients=["default", "web_embedded"],
        )
        assert opts["extractor_args"] == {
            "youtube": {"player_client": ["default", "web_embedded"]}
        }

    @pytest.mark.parametrize("audio_format", da.AUDIO_FORMAT_CHOICES)
    def test_options_are_accepted_by_the_real_yt_dlp(self, tmp_path, audio_format):
        """Constructing a real YoutubeDL validates keys, codecs and PP kwargs.

        No network happens here: the constructor only builds postprocessors.
        """
        opts = dict(
            da.build_ydl_opts(
                str(tmp_path / "%(id)s.%(ext)s"),
                audio_format,
                True,
                True,
                cookies_file=str(tmp_path / "cookies.txt"),
                player_clients=["default", "web_embedded"],
            )
        )
        opts.update(quiet=True, no_warnings=True)
        ydl = yt_dlp.YoutubeDL(opts)
        built = [type(pp).__name__ for group in ydl._pps.values() for pp in group]
        assert "FFmpegThumbnailsConvertorPP" in built
        assert "EmbedThumbnailPP" in built
        if audio_format == "keep":
            assert "FFmpegExtractAudioPP" not in built
        else:
            assert "FFmpegExtractAudioPP" in built


# ---------------------------------------------------------------------------
# Cookies and player-client resolution
# ---------------------------------------------------------------------------


class TestResolveCookiesFile:
    def test_no_configuration_means_no_cookies(self):
        assert da.resolve_cookies_file(None) is None

    def test_explicit_path_wins(self, tmp_path, monkeypatch):
        explicit = tmp_path / "explicit.txt"
        explicit.write_text("# Netscape HTTP Cookie File\n")
        monkeypatch.setenv("YTP_COOKIES_FILE", str(tmp_path / "env.txt"))
        assert da.resolve_cookies_file(str(explicit)) == str(explicit)

    def test_env_var_used_when_no_explicit_path(self, tmp_path, monkeypatch):
        env_file = tmp_path / "env.txt"
        env_file.write_text("# Netscape HTTP Cookie File\n")
        monkeypatch.setenv("YTP_COOKIES_FILE", str(env_file))
        assert da.resolve_cookies_file(None) == str(env_file)

    def test_default_location_is_picked_up_automatically(self, tmp_path, monkeypatch):
        default = tmp_path / "default-cookies.txt"
        default.write_text("# Netscape HTTP Cookie File\n")
        monkeypatch.setattr(da, "DEFAULT_COOKIES_FILE", str(default))
        assert da.resolve_cookies_file(None) == str(default)

    def test_configured_but_missing_path_warns_and_returns_none(
        self, tmp_path, capsys
    ):
        """Silently proceeding without cookies would leave the user
        bot-checked with no clue their cookies were never loaded."""
        result = da.resolve_cookies_file(str(tmp_path / "gone.txt"))
        assert result is None
        err = capsys.readouterr().err
        assert "not found" in err
        assert "gone.txt" in err

    def test_missing_default_is_silently_no_cookies(self, capsys):
        assert da.resolve_cookies_file(None) is None
        assert "WARNING" not in capsys.readouterr().err

    def test_tilde_paths_are_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cookie = tmp_path / "c.txt"
        cookie.write_text("# Netscape HTTP Cookie File\n")
        assert da.resolve_cookies_file("~/c.txt") == str(cookie)


class TestResolvePlayerClients:
    def test_unset_means_none(self):
        assert da.resolve_player_clients(None) is None

    def test_comma_list_is_split_and_stripped(self):
        assert da.resolve_player_clients(" default, web_embedded ,") == [
            "default",
            "web_embedded",
        ]

    def test_env_var_is_the_fallback(self, monkeypatch):
        monkeypatch.setenv("YTP_PLAYER_CLIENTS", "mweb")
        assert da.resolve_player_clients(None) == ["mweb"]

    def test_explicit_beats_env(self, monkeypatch):
        monkeypatch.setenv("YTP_PLAYER_CLIENTS", "mweb")
        assert da.resolve_player_clients("tv") == ["tv"]

    def test_empty_string_means_none(self):
        assert da.resolve_player_clients("") is None
        assert da.resolve_player_clients(" , ") is None


class TestCookiesReachTheRealYtDlp:
    def test_real_yt_dlp_loads_our_cookie_file(self, tmp_path):
        """End-to-end minus network: the real yt-dlp parses a Netscape file
        handed through build_ydl_opts and exposes the cookie in its jar."""
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text(
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\ttest-value-123\n"
        )
        opts = dict(
            da.build_ydl_opts(
                str(tmp_path / "%(id)s.%(ext)s"),
                "m4a",
                True,
                False,
                cookies_file=str(cookie_file),
            )
        )
        opts.update(quiet=True, no_warnings=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            names = {c.name: c.value for c in ydl.cookiejar}
        assert names.get("SAPISID") == "test-value-123"


# ---------------------------------------------------------------------------
# download_audio (end to end, fake yt-dlp)
# ---------------------------------------------------------------------------


class TestDownloadAudioHappyPath:
    def test_long_video_is_renamed_and_stays_in_output_dir(self, outdir, monkeypatch):
        plan = DownloadPlan(
            video_id="A1M5hduoj3E",
            title="Why AI Agents Are Eating Software",
            duration=1234,
            audio_payload=b"x" * 4096,
        )
        factory = _install_fake_ytdlp(monkeypatch, plan)

        meta = da.download_audio("A1M5hduoj3E", str(outdir))

        assert meta is not None
        stem = "Why AI Agents Are Eating Software"
        audio = outdir / f"{stem}.m4a"
        thumb = outdir / f"{stem}.jpg"
        assert audio.exists(), sorted(p.name for p in outdir.iterdir())
        assert thumb.exists(), sorted(p.name for p in outdir.iterdir())
        # The id-named files are gone: renamed, not copied.
        assert not (outdir / "A1M5hduoj3E.m4a").exists()
        assert not (outdir / "A1M5hduoj3E.jpg").exists()
        assert not (outdir / da.SHORTS_SUBDIR).exists()

        assert meta["id"] == "A1M5hduoj3E"
        assert meta["title"] == stem
        assert meta["filename"] == f"{stem}.m4a"
        assert os.sep not in meta["filename"] and "/" not in meta["filename"]
        assert meta["relpath"] == f"{stem}.m4a"
        assert meta["thumbnail"] == f"{stem}.jpg"
        assert meta["thumbnail_relpath"] == f"{stem}.jpg"
        assert meta["duration"] == 1234
        assert meta["filesize"] == 4096
        assert meta["audio_format"] == "m4a"
        assert meta["is_short"] is False
        assert meta["upload_date"] == "20260714"
        assert meta["description"].startswith("A description")
        assert meta["published"] is False
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", meta["downloaded_at"])
        assert factory.extract_count == 1

    def test_short_video_and_thumbnail_move_into_shorts_subdir(self, outdir, monkeypatch):
        plan = DownloadPlan(
            video_id="SHORTvid123",
            title="Quick Take on GPU Prices",
            duration=59,
            audio_payload=b"y" * 1024,
        )
        _install_fake_ytdlp(monkeypatch, plan)

        meta = da.download_audio("SHORTvid123", str(outdir))

        stem = "Quick Take on GPU Prices"
        shorts = outdir / da.SHORTS_SUBDIR
        assert (shorts / f"{stem}.m4a").exists()
        assert (shorts / f"{stem}.jpg").exists()
        assert not (outdir / f"{stem}.m4a").exists()

        assert meta["is_short"] is True
        # relpath is relative to the index dir, forward slashes, so the Android
        # app can join it onto its own storage root.
        assert meta["relpath"] == f"Shorts/{stem}.m4a"
        assert meta["thumbnail_relpath"] == f"Shorts/{stem}.jpg"
        assert "\\" not in meta["relpath"]
        # filename stays bare so the RSS release URL keeps working.
        assert meta["filename"] == f"{stem}.m4a"
        assert meta["thumbnail"] == f"{stem}.jpg"

    @pytest.mark.parametrize(
        "duration,expect_short",
        [(1, True), (179, True), (180, True), (181, False), (600, False)],
    )
    def test_shorts_boundary_decides_the_directory(
        self, outdir, monkeypatch, duration, expect_short
    ):
        plan = DownloadPlan(video_id="BOUNDARY123", title="Boundary", duration=duration)
        _install_fake_ytdlp(monkeypatch, plan)

        meta = da.download_audio("BOUNDARY123", str(outdir))

        assert meta["is_short"] is expect_short
        assert meta["relpath"] == ("Shorts/Boundary.m4a" if expect_short else "Boundary.m4a")
        assert (outdir / da.SHORTS_SUBDIR).exists() is expect_short

    def test_only_one_extraction_per_video(self, outdir, monkeypatch):
        """REGRESSION GUARD.

        Extraction is the slow step on a phone (player JS + a JS challenge
        through Node). The old code extracted metadata once and downloaded
        again, roughly doubling every download.
        """
        factory = _install_fake_ytdlp(monkeypatch, DownloadPlan(video_id="ONEPASS1234"))

        meta = da.download_audio("ONEPASS1234", str(outdir))

        assert meta is not None
        assert factory.extract_count == 1
        assert len(factory.instances) == 1
        call = factory.extract_calls[0]
        assert call["download"] is True
        assert call["url"] == "https://www.youtube.com/watch?v=ONEPASS1234"
        assert factory.instances[0].entered and factory.instances[0].closed

    def test_mp3_format_reports_mp3(self, outdir, monkeypatch):
        plan = DownloadPlan(
            video_id="MP3vid12345", title="Encoded Episode", audio_ext=".mp3"
        )
        factory = _install_fake_ytdlp(monkeypatch, plan)

        meta = da.download_audio("MP3vid12345", str(outdir), audio_format="mp3")

        assert (outdir / "Encoded Episode.mp3").exists()
        assert meta["filename"] == "Encoded Episode.mp3"
        assert meta["audio_format"] == "mp3"
        assert factory.last_opts["format"] == "bestaudio/best"

    def test_keep_format_leaves_webm_alone(self, outdir, monkeypatch):
        plan = DownloadPlan(
            video_id="KEEPvid1234", title="Opus Episode", audio_ext=".webm"
        )
        factory = _install_fake_ytdlp(monkeypatch, plan)

        meta = da.download_audio("KEEPvid1234", str(outdir), audio_format="keep")

        assert (outdir / "Opus Episode.webm").exists()
        assert meta["audio_format"] == "webm"
        assert "FFmpegExtractAudio" not in _pp_keys(factory.last_opts)

    def test_title_is_sanitized_into_the_filename(self, outdir, monkeypatch):
        plan = DownloadPlan(
            video_id="MESSYtitle1", title="AI/ML: 100% Better?! 🚀 #ai", duration=900
        )
        _install_fake_ytdlp(monkeypatch, plan)

        meta = da.download_audio("MESSYtitle1", str(outdir))

        expected = da.sanitize_filename("AI/ML: 100% Better?! 🚀 #ai")
        assert meta["filename"] == expected + ".m4a"
        assert (outdir / (expected + ".m4a")).exists()
        for bad in "/\\:*?\"<>|":
            assert bad not in meta["filename"]
        # The human-readable title is preserved in the metadata, unsanitized.
        assert meta["title"] == "AI/ML: 100% Better?! 🚀 #ai"

    def test_output_dir_is_created_when_missing(self, tmp_path, monkeypatch):
        target = tmp_path / "does" / "not" / "exist"
        _install_fake_ytdlp(monkeypatch, DownloadPlan(video_id="MKDIRvid12"))

        meta = da.download_audio("MKDIRvid12", str(target))

        assert target.is_dir()
        assert meta is not None

    def test_download_opts_use_the_id_template_and_progress_hooks(
        self, outdir, monkeypatch
    ):
        factory = _install_fake_ytdlp(monkeypatch, DownloadPlan(video_id="OPTSvid123"))

        da.download_audio("OPTSvid123", str(outdir), polite=True)

        opts = factory.last_opts
        assert opts["outtmpl"] == os.path.join(str(outdir), "%(id)s.%(ext)s")
        assert len(opts["progress_hooks"]) == 1
        assert len(opts["postprocessor_hooks"]) == 1
        assert opts["sleep_interval"] == 1  # polite propagated
        assert opts["socket_timeout"] == da.SOCKET_TIMEOUT

    def test_embed_thumbnail_flag_propagates(self, outdir, monkeypatch):
        factory = _install_fake_ytdlp(monkeypatch, DownloadPlan(video_id="NOEMBED123"))

        da.download_audio("NOEMBED123", str(outdir), embed_thumbnail=False)

        assert "EmbedThumbnail" not in _pp_keys(factory.last_opts)

    def test_audio_file_found_without_requested_downloads(self, outdir, monkeypatch):
        """Fallback scan of the output dir when yt-dlp omits the filepath."""
        plan = DownloadPlan(
            video_id="NOPATHvid1", title="Fallback Scan", report_filepath=True
        )
        plan.report_filepath = False
        _install_fake_ytdlp(monkeypatch, plan)

        meta = da.download_audio("NOPATHvid1", str(outdir))

        assert meta is not None
        assert meta["filename"] == "Fallback Scan.m4a"
        assert (outdir / "Fallback Scan.m4a").exists()

    def test_missing_thumbnail_yields_null_thumbnail_fields(self, outdir, monkeypatch):
        plan = DownloadPlan(video_id="NOTHUMB123", title="No Art", write_thumb=False)
        _install_fake_ytdlp(monkeypatch, plan)

        meta = da.download_audio("NOTHUMB123", str(outdir))

        assert meta["thumbnail"] is None
        assert meta["thumbnail_relpath"] is None
        assert meta["filename"] == "No Art.m4a"

    def test_metadata_is_json_serialisable(self, outdir, monkeypatch):
        _install_fake_ytdlp(monkeypatch, DownloadPlan(video_id="JSONvid123"))
        meta = da.download_audio("JSONvid123", str(outdir))
        assert json.loads(json.dumps([meta]))[0] == meta

    def test_existing_destination_name_is_not_overwritten(self, outdir, monkeypatch):
        """A second video with the same sanitized title must not clobber the first."""
        (outdir / "Duplicate Title.m4a").write_bytes(b"the original episode")
        plan = DownloadPlan(
            video_id="DUPEvid1234", title="Duplicate Title", audio_payload=b"new"
        )
        _install_fake_ytdlp(monkeypatch, plan)

        meta = da.download_audio("DUPEvid1234", str(outdir))

        assert (outdir / "Duplicate Title.m4a").read_bytes() == b"the original episode"
        # Current behaviour: the new file keeps its id-based name.
        assert meta["filename"] == "DUPEvid1234.m4a"
        assert (outdir / "DUPEvid1234.m4a").exists()


class TestDownloadAudioRealMedia:
    """One pass over a real (tiny) AAC file produced by the bundled ffmpeg."""

    def test_real_m4a_roundtrip(self, outdir, monkeypatch, tmp_path):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            pytest.skip("ffmpeg not on PATH")
        source = tmp_path / "silence.m4a"
        proc = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-t", "1", "-c:a", "aac", str(source),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not source.exists():
            pytest.skip(f"ffmpeg could not synthesise audio: {proc.stderr[:200]}")

        plan = DownloadPlan(
            video_id="REALmedia1",
            title="Real Media Episode",
            duration=1,
            audio_source=str(source),
        )
        _install_fake_ytdlp(monkeypatch, plan)

        meta = da.download_audio("REALmedia1", str(outdir))

        # duration 1s => a Short
        final = outdir / da.SHORTS_SUBDIR / "Real Media Episode.m4a"
        assert final.exists()
        assert meta["filesize"] == final.stat().st_size == source.stat().st_size
        assert meta["filesize"] > 0
        assert meta["relpath"] == "Shorts/Real Media Episode.m4a"


class TestDownloadAudioFailures:
    def test_extraction_error_returns_none(self, outdir, monkeypatch, capsys):
        plan = DownloadPlan(
            video_id="BOOMvid1234",
            raise_exc=yt_dlp.utils.DownloadError("HTTP Error 403: Forbidden"),
        )
        factory = _install_fake_ytdlp(monkeypatch, plan)

        result = da.download_audio("BOOMvid1234", str(outdir))

        assert result is None
        assert factory.extract_count == 1
        assert "403" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "exc",
        [
            OSError("disk full"),
            KeyError("format"),
            RuntimeError("no supported JavaScript runtime"),
        ],
    )
    def test_any_extraction_exception_is_swallowed(self, outdir, monkeypatch, exc):
        _install_fake_ytdlp(monkeypatch, DownloadPlan(video_id="EXCvid12345", raise_exc=exc))
        assert da.download_audio("EXCvid12345", str(outdir)) is None

    def test_extraction_returning_none_returns_none(self, outdir, monkeypatch):
        _install_fake_ytdlp(monkeypatch, DownloadPlan(video_id="NONEvid1234", return_none=True))
        assert da.download_audio("NONEvid1234", str(outdir)) is None

    def test_missing_audio_file_returns_none(self, outdir, monkeypatch, capsys):
        """yt-dlp claimed success but there is no audio on disk."""
        plan = DownloadPlan(
            video_id="MISSINGvid",
            write_audio=False,
            write_thumb=True,
            report_filepath=False,
        )
        _install_fake_ytdlp(monkeypatch, plan)

        result = da.download_audio("MISSINGvid", str(outdir))

        assert result is None
        assert "could not find the audio file" in capsys.readouterr().err

    def test_stale_filepath_falls_back_then_gives_up(self, outdir, monkeypatch):
        """requested_downloads points at a file that is not there."""
        plan = DownloadPlan(video_id="STALEvid12", write_audio=False)
        plan.extra_info = {
            "requested_downloads": [{"filepath": str(outdir / "STALEvid12.m4a")}]
        }
        _install_fake_ytdlp(monkeypatch, plan)

        assert da.download_audio("STALEvid12", str(outdir)) is None

    def test_no_partial_episode_is_recorded_on_failure(self, outdir, monkeypatch):
        _install_fake_ytdlp(
            monkeypatch, DownloadPlan(video_id="FAILvid1234", return_none=True)
        )
        assert da.download_audio("FAILvid1234", str(outdir)) is None
        assert list(outdir.iterdir()) == []

    def test_blocked_403_is_flagged_for_the_caller(self, outdir, monkeypatch):
        plan = DownloadPlan(
            video_id="BLOCKEDvid1",
            raise_exc=yt_dlp.utils.DownloadError(
                "ERROR: unable to download video data: HTTP Error 403: Forbidden"
            ),
        )
        _install_fake_ytdlp(monkeypatch, plan)

        assert da.download_audio("BLOCKEDvid1", str(outdir)) is None
        assert da._LAST_DOWNLOAD_BLOCKED is True

    @pytest.mark.parametrize(
        "exc",
        [
            yt_dlp.utils.DownloadError("ERROR: Video unavailable"),
            yt_dlp.utils.DownloadError("HTTP Error 404: Not Found"),
            OSError("Connection reset by peer"),
        ],
    )
    def test_other_failures_are_not_flagged_as_blocked(self, outdir, monkeypatch, exc):
        _install_fake_ytdlp(monkeypatch, DownloadPlan(video_id="OTHERvid123", raise_exc=exc))

        assert da.download_audio("OTHERvid123", str(outdir)) is None
        assert da._LAST_DOWNLOAD_BLOCKED is False

    def test_blocked_403_explains_it_is_youtube_not_the_user(
        self, outdir, monkeypatch, capsys
    ):
        plan = DownloadPlan(
            video_id="BLOCKEDvid1",
            raise_exc=yt_dlp.utils.DownloadError(
                "ERROR: unable to download video data: HTTP Error 403: Forbidden"
            ),
        )
        _install_fake_ytdlp(monkeypatch, plan)

        da.download_audio("BLOCKEDvid1", str(outdir))

        err = capsys.readouterr().err
        assert "YouTube refused" in err
        assert "yt-dlp" in err

    def test_yt_dlp_error_prefix_is_not_repeated(self, outdir, monkeypatch, capsys):
        """yt-dlp already printed 'ERROR: ...'; our line should not stack it."""
        plan = DownloadPlan(
            video_id="BOOMvid1234",
            raise_exc=yt_dlp.utils.DownloadError(
                "ERROR: unable to download video data: HTTP Error 403: Forbidden"
            ),
        )
        _install_fake_ytdlp(monkeypatch, plan)

        da.download_audio("BOOMvid1234", str(outdir))

        err = capsys.readouterr().err
        assert "Error downloading BOOMvid1234: unable to download" in err
        assert ": ERROR:" not in err

    def test_failed_download_cleans_up_the_orphaned_thumbnail(self, outdir, monkeypatch):
        """yt-dlp writes the .webp before the media 403s; it must not linger."""
        plan = DownloadPlan(
            video_id="Sg747IAUEfQ",
            thumb_ext=".webp",
            raise_exc=yt_dlp.utils.DownloadError(
                "ERROR: unable to download video data: HTTP Error 403: Forbidden"
            ),
            write_thumb_before_raise=True,
        )
        _install_fake_ytdlp(monkeypatch, plan)

        assert da.download_audio("Sg747IAUEfQ", str(outdir)) is None
        assert list(outdir.iterdir()) == []

    def test_partial_audio_survives_failure_for_resuming(self, outdir, monkeypatch):
        """.part files resume on retry; only the thumbnail is swept up."""
        part = outdir / "PARTIALvid1.m4a.part"
        part.write_bytes(b"half an episode")
        plan = DownloadPlan(
            video_id="PARTIALvid1",
            thumb_ext=".webp",
            raise_exc=yt_dlp.utils.DownloadError("HTTP Error 403: Forbidden"),
            write_thumb_before_raise=True,
        )
        _install_fake_ytdlp(monkeypatch, plan)

        assert da.download_audio("PARTIALvid1", str(outdir)) is None
        assert [p.name for p in outdir.iterdir()] == ["PARTIALvid1.m4a.part"]

    def test_claimed_success_without_audio_cleans_up_the_thumbnail(
        self, outdir, monkeypatch
    ):
        plan = DownloadPlan(
            video_id="MISSINGvid",
            write_audio=False,
            write_thumb=True,
            report_filepath=False,
        )
        _install_fake_ytdlp(monkeypatch, plan)

        assert da.download_audio("MISSINGvid", str(outdir)) is None
        assert list(outdir.iterdir()) == []

    def test_blocked_flag_resets_on_the_next_download(self, outdir, monkeypatch):
        """A 403 on one video must not smear onto the next video's failure."""
        _install_fake_ytdlp(
            monkeypatch,
            DownloadPlan(
                video_id="BLOCKEDvid1",
                raise_exc=yt_dlp.utils.DownloadError("HTTP Error 403: Forbidden"),
            ),
        )
        da.download_audio("BLOCKEDvid1", str(outdir))
        assert da._LAST_DOWNLOAD_BLOCKED is True

        _install_fake_ytdlp(
            monkeypatch,
            DownloadPlan(
                video_id="OTHERvid123",
                raise_exc=yt_dlp.utils.DownloadError("ERROR: Video unavailable"),
            ),
        )
        da.download_audio("OTHERvid123", str(outdir))
        assert da._LAST_DOWNLOAD_BLOCKED is False

    # The exact text from a real phone log, typographic apostrophe included -
    # matching on the apostrophe is the trap _is_bot_checked's docstring warns
    # about.
    BOT_CHECK_MESSAGE = (
        "ERROR: [youtube] mqb7R-ZPcrI: Sign in to confirm you’re not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication."
    )

    def test_bot_check_is_flagged_and_is_not_a_403(self, outdir, monkeypatch):
        plan = DownloadPlan(
            video_id="BOTCHECKvid",
            raise_exc=yt_dlp.utils.DownloadError(self.BOT_CHECK_MESSAGE),
        )
        _install_fake_ytdlp(monkeypatch, plan)

        assert da.download_audio("BOTCHECKvid", str(outdir)) is None
        assert da._LAST_DOWNLOAD_BOT_CHECKED is True
        # Critically NOT the flag that triggers the pip self-update - updating
        # yt-dlp cannot clear a bot check.
        assert da._LAST_DOWNLOAD_BLOCKED is False

    def test_bot_check_explains_network_reputation(self, outdir, monkeypatch, capsys):
        plan = DownloadPlan(
            video_id="BOTCHECKvid",
            raise_exc=yt_dlp.utils.DownloadError(self.BOT_CHECK_MESSAGE),
        )
        _install_fake_ytdlp(monkeypatch, plan)

        da.download_audio("BOTCHECKvid", str(outdir))

        err = capsys.readouterr().err
        assert "not a bot" in err
        assert "cookies" in err
        assert "updating yt-dlp does" in err  # "...not help"

    def test_bot_check_with_cookies_blames_the_cookies(
        self, outdir, monkeypatch, tmp_path, capsys
    ):
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n")
        plan = DownloadPlan(
            video_id="BOTCHECKvid",
            raise_exc=yt_dlp.utils.DownloadError(self.BOT_CHECK_MESSAGE),
        )
        _install_fake_ytdlp(monkeypatch, plan)

        da.download_audio("BOTCHECKvid", str(outdir), cookies_file=str(cookie_file))

        err = capsys.readouterr().err
        assert "expired" in err or "logged out" in err

    def test_age_restriction_is_not_misread_as_a_bot_check(self, outdir, monkeypatch):
        """"Sign in to confirm your age" is permanent for that video - the
        wait-or-switch-networks advice would be confidently wrong."""
        plan = DownloadPlan(
            video_id="AGEGATEvid1",
            raise_exc=yt_dlp.utils.DownloadError(
                "ERROR: [youtube] AGEGATEvid1: Sign in to confirm your age. "
                "This video may be inappropriate for some users. "
                "Use --cookies-from-browser or --cookies for the authentication."
            ),
        )
        _install_fake_ytdlp(monkeypatch, plan)

        assert da.download_audio("AGEGATEvid1", str(outdir)) is None
        assert da._LAST_DOWNLOAD_BOT_CHECKED is False
        assert da._LAST_DOWNLOAD_BLOCKED is False

    def test_a_403_is_not_misread_as_a_bot_check(self, outdir, monkeypatch):
        plan = DownloadPlan(
            video_id="BLOCKEDvid1",
            raise_exc=yt_dlp.utils.DownloadError(
                "ERROR: unable to download video data: HTTP Error 403: Forbidden"
            ),
        )
        _install_fake_ytdlp(monkeypatch, plan)

        da.download_audio("BLOCKEDvid1", str(outdir))

        assert da._LAST_DOWNLOAD_BLOCKED is True
        assert da._LAST_DOWNLOAD_BOT_CHECKED is False

    def test_cookies_and_clients_reach_the_download_opts(
        self, outdir, monkeypatch, tmp_path
    ):
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n")
        factory = _install_fake_ytdlp(monkeypatch, DownloadPlan(video_id="HAPPYvid123"))

        da.download_audio(
            "HAPPYvid123",
            str(outdir),
            cookies_file=str(cookie_file),
            player_clients=["default", "web_embedded"],
        )

        assert factory.last_opts["cookiefile"] == str(cookie_file)
        assert factory.last_opts["extractor_args"] == {
            "youtube": {"player_client": ["default", "web_embedded"]}
        }


# ---------------------------------------------------------------------------
# Keeping yt-dlp alive: freshness check and self-update
# ---------------------------------------------------------------------------


class TestYtdlpFreshness:
    @pytest.mark.parametrize(
        "version,today,expected",
        [
            # The last stable release before YouTube's 2026-08-17 purge of the
            # android_vr client - the exact version this repo got stranded on.
            ("2026.07.04", datetime.date(2026, 8, 23), "broken"),
            ("2025.12.30", datetime.date(2026, 8, 23), "broken"),
            ("2026.08.19", datetime.date(2026, 8, 23), "ok"),
            # Single-digit date parts, as pip's metadata normalises them.
            ("2026.8.19", datetime.date(2026, 8, 23), "ok"),
            # Exactly at the staleness boundary: 60 days is still ok...
            ("2026.08.19", datetime.date(2026, 10, 18), "ok"),
            # ...61 days is not.
            ("2026.08.19", datetime.date(2026, 10, 19), "stale"),
            ("2026.08.19", datetime.date(2027, 3, 1), "stale"),
        ],
    )
    def test_verdicts(self, monkeypatch, version, today, expected):
        monkeypatch.setattr(da, "_ytdlp_version_string", lambda: version)
        assert da.ytdlp_freshness(today=today) == expected

    @pytest.mark.parametrize("version", ["", "unknown", "git-master", None])
    def test_unparseable_versions_do_not_nag(self, monkeypatch, version):
        monkeypatch.setattr(da, "_ytdlp_version_string", lambda: version)
        assert da.ytdlp_freshness(today=datetime.date(2026, 8, 23)) == "ok"

    def test_nightly_style_versions_parse_on_their_date(self, monkeypatch):
        monkeypatch.setattr(
            da, "_ytdlp_version_string", lambda: "2026.8.18.122307.dev0"
        )
        # One day before the stable fix: still counted as broken, and the
        # harmless remedy is an upgrade to stable.
        assert da.ytdlp_freshness(today=datetime.date(2026, 8, 23)) == "broken"


class TestSelfUpdate:
    def _fake_pip(self, monkeypatch, versions, returncode=0, record=None):
        """Answer version probes from `versions` and accept the pip call."""
        seen = iter(versions)
        monkeypatch.setattr(da, "_installed_ytdlp_version_str", lambda: next(seen))

        def run(cmd, **kwargs):
            if record is not None:
                record.append(cmd)
            return types.SimpleNamespace(returncode=returncode)

        monkeypatch.setattr(da.subprocess, "run", run)

    def test_reports_true_when_a_new_version_lands(self, monkeypatch, capsys):
        record = []
        self._fake_pip(monkeypatch, ["2026.7.4", "2026.8.19"], record=record)

        assert da.self_update_ytdlp() is True
        (cmd,) = record
        # Without --upgrade, pip against an installed yt-dlp is a no-op
        # ("Requirement already satisfied") and the self-heal is inert.
        assert "--upgrade" in cmd
        # The pure-Python pair, never "yt-dlp[default]" - its C-extension
        # extras cannot build on Termux and pip would install nothing.
        assert "yt-dlp" in cmd
        assert "yt-dlp-ejs" in cmd
        assert not any("[" in part for part in cmd)
        assert "2026.8.19" in capsys.readouterr().out

    def test_reports_false_when_already_newest(self, monkeypatch, capsys):
        self._fake_pip(monkeypatch, ["2026.8.19", "2026.8.19"])

        assert da.self_update_ytdlp() is False
        assert "already the newest" in capsys.readouterr().out

    def test_reports_false_when_pip_fails(self, monkeypatch):
        self._fake_pip(monkeypatch, ["2026.7.4", "2026.7.4"], returncode=1)
        assert da.self_update_ytdlp() is False

    def test_reports_false_when_pip_cannot_run_at_all(self, monkeypatch):
        monkeypatch.setattr(da, "_installed_ytdlp_version_str", lambda: "2026.7.4")

        def explode(cmd, **kwargs):
            raise OSError("pip is missing")

        monkeypatch.setattr(da.subprocess, "run", explode)
        assert da.self_update_ytdlp() is False

    def test_restart_appends_the_guard_flag_exactly_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(da.os, "execv", lambda exe, argv: calls.append((exe, argv)))
        monkeypatch.setattr(
            da.sys, "argv", ["download_audio.py", "--url", "https://youtu.be/x"]
        )

        da.restart_after_self_update()

        (exe, argv), = calls
        assert exe == sys.executable
        assert argv.count(da.SELF_UPDATE_GUARD_FLAG) == 1
        assert argv[:2] == [sys.executable, "download_audio.py"]
        assert "--url" in argv

    def test_restart_never_stacks_a_second_guard_flag(self, monkeypatch):
        calls = []
        monkeypatch.setattr(da.os, "execv", lambda exe, argv: calls.append(argv))
        monkeypatch.setattr(
            da.sys, "argv", ["download_audio.py", da.SELF_UPDATE_GUARD_FLAG]
        )

        da.restart_after_self_update()

        assert calls[0].count(da.SELF_UPDATE_GUARD_FLAG) == 1


class TestMainSelfHeal:
    """main() must turn a blocked 403 into an updated yt-dlp and a re-run."""

    def _run_main(
        self,
        monkeypatch,
        tmp_path,
        outdir,
        plan,
        extra_argv=(),
        update_succeeds=True,
    ):
        _install_fake_ytdlp(monkeypatch, plan)
        monkeypatch.setattr(da, "ytdlp_freshness", lambda today=None: "ok")

        updates = []
        restarts = []
        monkeypatch.setattr(
            da, "self_update_ytdlp", lambda: updates.append(True) or update_succeeds
        )
        monkeypatch.setattr(
            da, "restart_after_self_update", lambda: restarts.append(True)
        )
        monkeypatch.setattr(
            da.sys,
            "argv",
            [
                "download_audio.py",
                "--url",
                f"https://youtu.be/{plan.video_id}",
                "--output-dir",
                str(outdir),
                "--episodes-file",
                str(tmp_path / "episodes.json"),
                *extra_argv,
            ],
        )

        code = 0
        try:
            da.main()
        except SystemExit as exc:
            code = exc.code
        return updates, restarts, code

    def _blocked_plan(self, video_id="BLOCKEDvid1"):
        return DownloadPlan(
            video_id=video_id,
            raise_exc=yt_dlp.utils.DownloadError(
                "ERROR: unable to download video data: HTTP Error 403: Forbidden"
            ),
        )

    def test_blocked_single_video_updates_and_restarts(
        self, monkeypatch, tmp_path, outdir
    ):
        updates, restarts, code = self._run_main(
            monkeypatch, tmp_path, outdir, self._blocked_plan()
        )
        assert updates == [True]
        assert restarts == [True]
        assert code == 1  # the faked restart returns; real execv never does

    def test_blocked_single_video_without_an_update_just_fails(
        self, monkeypatch, tmp_path, outdir, capsys
    ):
        updates, restarts, code = self._run_main(
            monkeypatch, tmp_path, outdir, self._blocked_plan(), update_succeeds=False
        )
        assert updates == [True]
        assert restarts == []
        assert code == 1
        assert "broken downloads for everyone" in capsys.readouterr().out

    def test_no_self_update_flag_is_respected(self, monkeypatch, tmp_path, outdir):
        updates, restarts, code = self._run_main(
            monkeypatch,
            tmp_path,
            outdir,
            self._blocked_plan(),
            extra_argv=["--no-self-update"],
        )
        assert updates == []
        assert restarts == []
        assert code == 1

    def test_the_guard_flag_prevents_a_second_update(
        self, monkeypatch, tmp_path, outdir
    ):
        updates, restarts, code = self._run_main(
            monkeypatch,
            tmp_path,
            outdir,
            self._blocked_plan(),
            extra_argv=[da.SELF_UPDATE_GUARD_FLAG],
        )
        assert updates == []
        assert restarts == []
        assert code == 1

    def test_ordinary_failures_do_not_trigger_an_update(
        self, monkeypatch, tmp_path, outdir
    ):
        plan = DownloadPlan(video_id="PLAINfail12", return_none=True)
        updates, restarts, code = self._run_main(monkeypatch, tmp_path, outdir, plan)
        assert updates == []
        assert code == 1

    def test_successful_download_does_not_touch_the_updater(
        self, monkeypatch, tmp_path, outdir
    ):
        plan = DownloadPlan(video_id="HAPPYvid123")
        updates, restarts, code = self._run_main(monkeypatch, tmp_path, outdir, plan)
        assert updates == []
        assert code == 0

    def test_a_known_broken_ytdlp_updates_before_downloading(
        self, monkeypatch, tmp_path, outdir
    ):
        plan = DownloadPlan(video_id="HAPPYvid123")
        _install_fake_ytdlp(monkeypatch, plan)
        monkeypatch.setattr(da, "ytdlp_freshness", lambda today=None: "broken")
        monkeypatch.setattr(da, "self_update_ytdlp", lambda: True)
        restarts = []
        monkeypatch.setattr(
            da, "restart_after_self_update", lambda: restarts.append(True)
        )
        monkeypatch.setattr(
            da.sys,
            "argv",
            [
                "download_audio.py",
                "--url",
                "https://youtu.be/HAPPYvid123",
                "--output-dir",
                str(outdir),
                "--episodes-file",
                str(tmp_path / "episodes.json"),
            ],
        )

        da.main()

        assert restarts == [True]

    @pytest.mark.parametrize("flag", ["--no-self-update", "--after-self-update"])
    def test_a_known_broken_ytdlp_respects_the_opt_outs(
        self, monkeypatch, tmp_path, outdir, flag
    ):
        """Neither the user's opt-out nor the re-exec guard may loop pip."""
        plan = DownloadPlan(video_id="HAPPYvid123")
        _install_fake_ytdlp(monkeypatch, plan)
        monkeypatch.setattr(da, "ytdlp_freshness", lambda today=None: "broken")
        updates = []
        monkeypatch.setattr(
            da, "self_update_ytdlp", lambda: updates.append(True) or True
        )
        restarts = []
        monkeypatch.setattr(
            da, "restart_after_self_update", lambda: restarts.append(True)
        )
        monkeypatch.setattr(
            da.sys,
            "argv",
            [
                "download_audio.py",
                "--url",
                "https://youtu.be/HAPPYvid123",
                "--output-dir",
                str(outdir),
                "--episodes-file",
                str(tmp_path / "episodes.json"),
                flag,
            ],
        )

        da.main()

        assert updates == []
        assert restarts == []

    def test_a_stale_ytdlp_gets_a_note_but_no_forced_update(
        self, monkeypatch, tmp_path, outdir, capsys
    ):
        plan = DownloadPlan(video_id="HAPPYvid123")
        _install_fake_ytdlp(monkeypatch, plan)
        monkeypatch.setattr(da, "ytdlp_freshness", lambda today=None: "stale")
        updates = []
        monkeypatch.setattr(da, "self_update_ytdlp", lambda: updates.append(True))
        monkeypatch.setattr(
            da.sys,
            "argv",
            [
                "download_audio.py",
                "--url",
                "https://youtu.be/HAPPYvid123",
                "--output-dir",
                str(outdir),
                "--episodes-file",
                str(tmp_path / "episodes.json"),
            ],
        )

        da.main()

        assert updates == []
        assert "days old" in capsys.readouterr().out

    def test_a_bot_check_never_triggers_the_updater(
        self, monkeypatch, tmp_path, outdir, capsys
    ):
        """Updating yt-dlp cannot clear YouTube's bot gate; pip must stay
        untouched and the user must be pointed at cookies instead."""
        plan = DownloadPlan(
            video_id="BOTCHECKvid",
            raise_exc=yt_dlp.utils.DownloadError(
                TestDownloadAudioFailures.BOT_CHECK_MESSAGE
            ),
        )
        updates, restarts, code = self._run_main(monkeypatch, tmp_path, outdir, plan)
        assert updates == []
        assert restarts == []
        assert code == 1
        out = capsys.readouterr().out
        assert "bot-checking" in out
        assert "Cookies" in out

    def test_a_bot_check_with_cookies_says_to_re_export(
        self, monkeypatch, tmp_path, outdir, capsys
    ):
        """With cookies already in play, "set up cookies" is the wrong advice -
        the session went stale and needs a fresh export."""
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n")
        plan = DownloadPlan(
            video_id="BOTCHECKvid",
            raise_exc=yt_dlp.utils.DownloadError(
                TestDownloadAudioFailures.BOT_CHECK_MESSAGE
            ),
        )
        updates, restarts, code = self._run_main(
            monkeypatch,
            tmp_path,
            outdir,
            plan,
            extra_argv=["--cookies", str(cookie_file)],
        )
        assert updates == []
        assert code == 1
        out = capsys.readouterr().out
        assert "even with cookies" in out
        assert "fresh cookies" in out


class TestMainSessionOpts:
    """--cookies / env overrides must actually reach the yt-dlp options."""

    def _main_argv(self, tmp_path, outdir, extra):
        return [
            "download_audio.py",
            "--url",
            "https://youtu.be/HAPPYvid123",
            "--output-dir",
            str(outdir),
            "--episodes-file",
            str(tmp_path / "episodes.json"),
            *extra,
        ]

    def test_cookies_flag_reaches_the_download(self, monkeypatch, tmp_path, outdir):
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n")
        factory = _install_fake_ytdlp(monkeypatch, DownloadPlan(video_id="HAPPYvid123"))
        monkeypatch.setattr(da, "ytdlp_freshness", lambda today=None: "ok")
        monkeypatch.setattr(
            da.sys,
            "argv",
            self._main_argv(tmp_path, outdir, ["--cookies", str(cookie_file)]),
        )

        da.main()

        assert factory.last_opts["cookiefile"] == str(cookie_file)

    def test_default_cookie_location_reaches_the_download(
        self, monkeypatch, tmp_path, outdir, capsys
    ):
        """Drop the export at the well-known path and everything uses it -
        the zero-configuration promise the README makes."""
        default = tmp_path / "youtube_podcasts.cookies.txt"
        default.write_text("# Netscape HTTP Cookie File\n")
        monkeypatch.setattr(da, "DEFAULT_COOKIES_FILE", str(default))
        factory = _install_fake_ytdlp(monkeypatch, DownloadPlan(video_id="HAPPYvid123"))
        monkeypatch.setattr(da, "ytdlp_freshness", lambda today=None: "ok")
        monkeypatch.setattr(da.sys, "argv", self._main_argv(tmp_path, outdir, []))

        da.main()

        assert factory.last_opts["cookiefile"] == str(default)
        assert "Using YouTube cookies" in capsys.readouterr().out

    def test_env_player_clients_reach_the_download(
        self, monkeypatch, tmp_path, outdir
    ):
        monkeypatch.setenv("YTP_PLAYER_CLIENTS", "default,web_embedded")
        factory = _install_fake_ytdlp(monkeypatch, DownloadPlan(video_id="HAPPYvid123"))
        monkeypatch.setattr(da, "ytdlp_freshness", lambda today=None: "ok")
        monkeypatch.setattr(da.sys, "argv", self._main_argv(tmp_path, outdir, []))

        da.main()

        assert factory.last_opts["extractor_args"] == {
            "youtube": {"player_client": ["default", "web_embedded"]}
        }

    def test_channel_listing_fallback_gets_the_cookies_too(
        self, monkeypatch, tmp_path
    ):
        """The bot gate hits extraction, so channel listing needs the same
        session - not just the downloads."""
        monkeypatch.chdir(tmp_path)
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n")
        factory = _install_fake_ytdlp(monkeypatch, DownloadPlan())

        da.fetch_videos_from_ytdlp(
            3,
            "https://youtube.com/@alpha",
            cookies_file=str(cookie_file),
            player_clients=["mweb"],
        )
        da.discover_channel_id(
            "https://youtube.com/@beta", cookies_file=str(cookie_file)
        )

        for opts in factory.opts_seen:
            assert opts["cookiefile"] == str(cookie_file)
        assert factory.opts_seen[0]["extractor_args"] == {
            "youtube": {"player_client": ["mweb"]}
        }

    def test_fetch_video_list_threads_cookies_into_the_fallback(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.chdir(tmp_path)
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n")
        # No cached channel id and a fake that returns no id: the RSS branch
        # is skipped and fetch_video_list must fall through to yt-dlp.
        factory = _install_fake_ytdlp(monkeypatch, DownloadPlan(return_none=True))

        da.fetch_video_list(
            3,
            "https://youtube.com/@alpha",
            cookies_file=str(cookie_file),
            player_clients=["mweb"],
        )

        assert factory.opts_seen  # discovery and the listing fallback ran
        for opts in factory.opts_seen:
            assert opts["cookiefile"] == str(cookie_file)
            assert opts["extractor_args"] == {"youtube": {"player_client": ["mweb"]}}

    def test_channel_mode_hands_the_session_to_the_listing(
        self, monkeypatch, tmp_path, outdir
    ):
        default = tmp_path / "youtube_podcasts.cookies.txt"
        default.write_text("# Netscape HTTP Cookie File\n")
        monkeypatch.setattr(da, "DEFAULT_COOKIES_FILE", str(default))
        monkeypatch.setenv("YTP_PLAYER_CLIENTS", "mweb")
        _install_fake_ytdlp(monkeypatch, DownloadPlan(video_id="HAPPYvid123"))
        monkeypatch.setattr(da, "ytdlp_freshness", lambda today=None: "ok")

        seen = {}

        def fake_list(max_episodes, channel_url, cookies_file=None, player_clients=None):
            seen["cookies_file"] = cookies_file
            seen["player_clients"] = player_clients
            return [{"id": "HAPPYvid123", "title": "One"}]

        monkeypatch.setattr(da, "fetch_video_list", fake_list)
        monkeypatch.setattr(
            da.sys,
            "argv",
            [
                "download_audio.py",
                "--output-dir",
                str(outdir),
                "--episodes-file",
                str(tmp_path / "episodes.json"),
            ],
        )

        da.main()

        assert seen["cookies_file"] == str(default)
        assert seen["player_clients"] == ["mweb"]


class TestMainChannelMode:
    def _run_channel_main(
        self, monkeypatch, tmp_path, outdir, plan, videos, extra_argv=()
    ):
        _install_fake_ytdlp(monkeypatch, plan)
        monkeypatch.setattr(da, "ytdlp_freshness", lambda today=None: "ok")
        monkeypatch.setattr(da, "fetch_video_list", lambda *a, **kw: videos)

        updates = []
        restarts = []
        monkeypatch.setattr(da, "self_update_ytdlp", lambda: updates.append(True) or True)
        monkeypatch.setattr(
            da, "restart_after_self_update", lambda: restarts.append(True)
        )
        monkeypatch.setattr(
            da.sys,
            "argv",
            [
                "download_audio.py",
                "--output-dir",
                str(outdir),
                "--episodes-file",
                str(tmp_path / "episodes.json"),
                *extra_argv,
            ],
        )

        code = 0
        try:
            da.main()
        except SystemExit as exc:
            code = exc.code
        return updates, restarts, code

    def test_every_video_blocked_updates_restarts_and_fails(
        self, monkeypatch, tmp_path, outdir
    ):
        plan = DownloadPlan(
            video_id="BLOCKEDvid1",
            raise_exc=yt_dlp.utils.DownloadError(
                "ERROR: unable to download video data: HTTP Error 403: Forbidden"
            ),
        )
        videos = [
            {"id": "BLOCKEDvid1", "title": "One"},
            {"id": "BLOCKEDvid2", "title": "Two"},
        ]
        updates, restarts, code = self._run_channel_main(
            monkeypatch, tmp_path, outdir, plan, videos
        )
        assert updates == [True]
        assert restarts == [True]
        assert code == 1

    def test_total_failure_without_403_exits_nonzero_but_never_updates(
        self, monkeypatch, tmp_path, outdir, capsys
    ):
        plan = DownloadPlan(video_id="PLAINfail12", return_none=True)
        videos = [{"id": "PLAINfail12", "title": "One"}]
        updates, restarts, code = self._run_channel_main(
            monkeypatch, tmp_path, outdir, plan, videos
        )
        assert updates == []
        assert code == 1
        assert "No episodes could be downloaded" in capsys.readouterr().out

    def test_a_successful_run_still_exits_zero(self, monkeypatch, tmp_path, outdir):
        plan = DownloadPlan(video_id="HAPPYvid123")
        videos = [{"id": "HAPPYvid123", "title": "One"}]
        updates, restarts, code = self._run_channel_main(
            monkeypatch, tmp_path, outdir, plan, videos
        )
        assert updates == []
        assert code == 0

    @pytest.mark.parametrize("flag", ["--no-self-update", "--after-self-update"])
    def test_channel_mode_respects_the_opt_outs(
        self, monkeypatch, tmp_path, outdir, flag
    ):
        """The cron path is exactly where a guard failure would loop forever."""
        plan = DownloadPlan(
            video_id="BLOCKEDvid1",
            raise_exc=yt_dlp.utils.DownloadError(
                "ERROR: unable to download video data: HTTP Error 403: Forbidden"
            ),
        )
        videos = [{"id": "BLOCKEDvid1", "title": "One"}]
        updates, restarts, code = self._run_channel_main(
            monkeypatch, tmp_path, outdir, plan, videos, extra_argv=[flag]
        )
        assert updates == []
        assert restarts == []
        assert code == 1

    def test_a_mixed_batch_of_403_and_other_failures_still_updates(
        self, monkeypatch, tmp_path, outdir
    ):
        """One blocked video is enough - a later unrelated failure on another
        video must not make the run forget it saw YouTube's block."""
        plan = DownloadPlan(
            video_id="BLOCKEDvid1",
            raise_exc=[
                yt_dlp.utils.DownloadError(
                    "ERROR: unable to download video data: HTTP Error 403: Forbidden"
                ),
                yt_dlp.utils.DownloadError("ERROR: Video unavailable"),
            ],
        )
        videos = [
            {"id": "BLOCKEDvid1", "title": "One"},
            {"id": "OTHERvid123", "title": "Two"},
        ]
        updates, restarts, code = self._run_channel_main(
            monkeypatch, tmp_path, outdir, plan, videos
        )
        assert updates == [True]
        assert restarts == [True]
        assert code == 1

    def test_a_blocked_video_in_an_otherwise_good_batch_still_updates(
        self, monkeypatch, tmp_path, outdir
    ):
        """Partial success does not excuse a 403: the blocked episode is only
        reachable after an update, so the update must still happen."""
        plan = DownloadPlan(
            video_id="HAPPYvid123",
            raise_exc=[
                yt_dlp.utils.DownloadError(
                    "ERROR: unable to download video data: HTTP Error 403: Forbidden"
                ),
            ],
        )
        videos = [
            {"id": "BLOCKEDvid1", "title": "One"},
            {"id": "HAPPYvid123", "title": "Two"},
        ]
        updates, restarts, code = self._run_channel_main(
            monkeypatch, tmp_path, outdir, plan, videos
        )
        assert updates == [True]
        assert restarts == [True]
        assert code == 0  # one episode did land, so the run itself succeeded

    def test_every_video_bot_checked_never_updates_and_points_at_cookies(
        self, monkeypatch, tmp_path, outdir, capsys
    ):
        plan = DownloadPlan(
            video_id="BOTCHECKvid",
            raise_exc=yt_dlp.utils.DownloadError(
                TestDownloadAudioFailures.BOT_CHECK_MESSAGE
            ),
        )
        videos = [
            {"id": "BOTCHECKvid", "title": "One"},
            {"id": "OTHERvid123", "title": "Two"},
        ]
        updates, restarts, code = self._run_channel_main(
            monkeypatch, tmp_path, outdir, plan, videos
        )
        assert updates == []
        assert restarts == []
        assert code == 1
        out = capsys.readouterr().out
        assert "bot-checking" in out
        assert "Cookies" in out

    def test_bot_checked_listing_fails_loudly_instead_of_done(
        self, monkeypatch, tmp_path, outdir, capsys
    ):
        """The gate can kill a refresh before any download starts: with no
        cached channel id both listing extractions bot-check, and that must
        exit nonzero with the cookies advice - not the "Done!" banner."""
        monkeypatch.chdir(tmp_path)  # no .channel_id cache
        plan = DownloadPlan(
            raise_exc=yt_dlp.utils.DownloadError(
                TestDownloadAudioFailures.BOT_CHECK_MESSAGE
            ),
        )
        _install_fake_ytdlp(monkeypatch, plan)
        monkeypatch.setattr(da, "ytdlp_freshness", lambda today=None: "ok")
        updates = []
        monkeypatch.setattr(
            da, "self_update_ytdlp", lambda: updates.append(True) or True
        )
        monkeypatch.setattr(
            da.sys,
            "argv",
            [
                "download_audio.py",
                "--channel-url",
                "https://youtube.com/@alpha",
                "--output-dir",
                str(outdir),
                "--episodes-file",
                str(tmp_path / "episodes.json"),
            ],
        )

        with pytest.raises(SystemExit) as excinfo:
            da.main()

        assert excinfo.value.code == 1
        assert updates == []
        out = capsys.readouterr().out
        assert "bot-checking" in out
        assert "Cookies" in out

    def test_a_plain_empty_listing_still_returns_softly(
        self, monkeypatch, tmp_path, outdir, capsys
    ):
        """An empty listing without a bot check keeps the old soft exit -
        the GitHub Actions feed job runs on YouTube-blocked runners and must
        not go permanently red over it."""
        monkeypatch.chdir(tmp_path)
        plan = DownloadPlan(return_none=True)
        _install_fake_ytdlp(monkeypatch, plan)
        monkeypatch.setattr(da, "ytdlp_freshness", lambda today=None: "ok")
        monkeypatch.setattr(
            da.sys,
            "argv",
            [
                "download_audio.py",
                "--channel-url",
                "https://youtube.com/@alpha",
                "--output-dir",
                str(outdir),
                "--episodes-file",
                str(tmp_path / "episodes.json"),
            ],
        )

        da.main()  # returns, no SystemExit

        assert "Could not fetch any videos" in capsys.readouterr().out

    def test_partial_success_without_a_403_still_exits_zero(
        self, monkeypatch, tmp_path, outdir, capsys
    ):
        """One saved episode is a success even when another video fails."""
        plan = DownloadPlan(
            video_id="HAPPYvid123",
            raise_exc=[
                yt_dlp.utils.DownloadError(
                    "ERROR: Join this channel to get access to members-only content"
                ),
            ],
        )
        videos = [
            {"id": "MEMBERSvid1", "title": "One"},
            {"id": "HAPPYvid123", "title": "Two"},
        ]
        updates, restarts, code = self._run_channel_main(
            monkeypatch, tmp_path, outdir, plan, videos
        )
        assert updates == []  # a non-403 failure must never trigger pip
        assert restarts == []
        assert code == 0
        assert "Done! Downloaded 1" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# is_short_video
# ---------------------------------------------------------------------------


class TestIsShortVideo:
    @pytest.mark.parametrize(
        "duration,expected",
        [
            (1, True),
            (60, True),
            (179, True),
            (180, True),  # exactly at the boundary: still a Short
            (181, False),
            (600, False),
            (0, False),  # falsy duration, no hashtags
            (None, False),
        ],
    )
    def test_duration_boundaries(self, duration, expected):
        assert da.is_short_video(duration, "A Normal Title") is expected

    @pytest.mark.parametrize("duration", [0, None])
    def test_unknown_duration_with_two_hashtags_is_a_short(self, duration):
        assert da.is_short_video(duration, "Quick take #ai #shorts") is True

    def test_unknown_duration_with_one_hashtag_is_not_a_short(self):
        assert da.is_short_video(0, "Quick take #ai") is False
        assert da.is_short_video(None, "Quick take #ai") is False

    def test_long_video_with_many_hashtags_is_not_a_short(self):
        assert da.is_short_video(3600, "Deep dive #ai #ml #agents") is False

    def test_short_duration_wins_regardless_of_title(self):
        assert da.is_short_video(30, "") is True
        assert da.is_short_video(30, "no hashtags here") is True

    def test_title_defaults_to_empty(self):
        assert da.is_short_video(30) is True
        assert da.is_short_video(3000) is False

    def test_hashtag_count_is_case_insensitive_and_counts_symbols(self):
        assert da.is_short_video(0, "#AI #News") is True
        assert da.is_short_video(0, "##") is True


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Hello World", "Hello World"),
            ("Episode 12 - Part_2", "Episode 12 - Part_2"),
            ("  padded  ", "padded"),
            ("AI/ML deep dive", "AIML deep dive"),
            ("path\\with\\backslashes", "pathwithbackslashes"),
            ("Colons: and *stars*?", "Colons and stars"),
            ("100% better!", "100 better"),
            ("Quotes \"and\" 'more'", "Quotes and more"),
            ("Tabs\tand\nnewlines", "Tabsandnewlines"),
            ("#ai #news daily", "ai news daily"),
        ],
    )
    def test_basic_sanitisation(self, title, expected):
        assert da.sanitize_filename(title) == expected

    @pytest.mark.parametrize(
        "title", ["", "   ", "###", "!!!", "🚀🚀🚀", "///", "\n\t", "?!?"]
    )
    def test_titles_that_sanitize_to_nothing_fall_back(self, title):
        assert da.sanitize_filename(title) == "episode"

    def test_emoji_are_dropped_but_surrounding_text_survives(self):
        assert da.sanitize_filename("Rocket 🚀 Launch") == "Rocket  Launch"
        assert da.sanitize_filename("🚀 Launch") == "Launch"

    def test_unicode_letters_and_digits_survive(self):
        # str.isalnum() is unicode-aware, so accents and CJK are kept.
        assert da.sanitize_filename("Café ñandú") == "Café ñandú"
        assert da.sanitize_filename("日本語 episode") == "日本語 episode"

    def test_length_is_capped_at_80(self):
        assert da.sanitize_filename("a" * 200) == "a" * 80
        assert len(da.sanitize_filename("word " * 100)) <= 80
        assert len(da.sanitize_filename("x" * 79)) == 79

    def test_truncation_never_leaves_a_trailing_space(self):
        """The 80-char cap must not leave a trailing space or dot.

        Shared storage on the phone is FAT/exFAT, which silently drops trailing
        spaces and dots. That would invalidate the relpath recorded in the
        episode index, so the app could no longer find the file.
        """
        title = "x" * 79 + " y"  # 81 chars, with a space at index 79
        result = da.sanitize_filename(title)
        assert result == "x" * 79
        assert not result.endswith((" ", "."))

        # A title that is nothing but padding still yields a usable name.
        assert da.sanitize_filename("   ...   ") == "episode"

    def test_result_is_usable_as_a_filename(self, tmp_path):
        for title in [
            "AI/ML: 100% Better?! 🚀 #ai",
            "Ep. 42 — the *long* one",
            "日本語 episode",
            "###",
        ]:
            name = da.sanitize_filename(title) + ".m4a"
            assert "/" not in name and "\\" not in name
            path = tmp_path / name
            path.write_bytes(b"ok")
            assert path.exists()


# ---------------------------------------------------------------------------
# load_episodes / save_episodes / get_existing_ids
# ---------------------------------------------------------------------------


EPISODE = {
    "id": "A1M5hduoj3E",
    "title": "Café ñandú — episode 1",
    "duration": 1234,
    "filename": "Café ñandú  episode 1.m4a",
    "relpath": "Shorts/Café ñandú  episode 1.m4a",
    "is_short": True,
    "published": False,
}


class TestEpisodeIndex:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "episodes.json")
        episodes = [EPISODE, dict(EPISODE, id="OTHERvid123", is_short=False)]

        da.save_episodes(episodes, path)

        assert da.load_episodes(path) == episodes

    def test_saved_file_is_readable_json(self, tmp_path):
        path = str(tmp_path / "episodes.json")
        da.save_episodes([EPISODE], path)
        with open(path, "r", encoding="utf-8") as fh:
            assert json.load(fh) == [EPISODE]

    def test_missing_file_returns_empty_list(self, tmp_path):
        assert da.load_episodes(str(tmp_path / "nope.json")) == []

    @pytest.mark.parametrize(
        "content", ["", "{not json", "[1, 2", "\x00\x01binary", "undefined"]
    )
    def test_corrupt_json_returns_empty_list(self, tmp_path, content):
        path = tmp_path / "episodes.json"
        path.write_text(content, encoding="utf-8", errors="ignore")
        assert da.load_episodes(str(path)) == []

    @pytest.mark.parametrize("content", ['{"a": 1}', '"a string"', "42", "null"])
    def test_non_list_json_returns_empty_list(self, tmp_path, content):
        path = tmp_path / "episodes.json"
        path.write_text(content)
        assert da.load_episodes(str(path)) == []

    def test_unreadable_path_returns_empty_list(self, tmp_path):
        # A directory where a file is expected raises IsADirectoryError (OSError).
        assert da.load_episodes(str(tmp_path)) == []

    def test_save_is_atomic_and_leaves_no_tmp_file(self, tmp_path):
        path = str(tmp_path / "episodes.json")
        da.save_episodes([EPISODE], path)
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []
        assert os.path.exists(path)

    def test_save_creates_missing_parent_directories(self, tmp_path):
        path = str(tmp_path / "deep" / "nested" / "episodes.json")
        da.save_episodes([EPISODE], path)
        assert da.load_episodes(path) == [EPISODE]

    def test_save_overwrites_previous_content(self, tmp_path):
        path = str(tmp_path / "episodes.json")
        da.save_episodes([EPISODE, dict(EPISODE, id="TObeREMOVED")], path)
        da.save_episodes([EPISODE], path)
        assert da.load_episodes(path) == [EPISODE]

    def test_existing_file_survives_a_failed_save(self, tmp_path, monkeypatch):
        """The whole point of writing to .tmp then os.replace()."""
        path = str(tmp_path / "episodes.json")
        da.save_episodes([EPISODE], path)

        def boom(*args, **kwargs):
            raise TypeError("Object of type set is not JSON serializable")

        monkeypatch.setattr(da.json, "dump", boom)
        with pytest.raises(TypeError):
            da.save_episodes([{"id": "BADvid12345", "bad": {1, 2}}], path)

        assert da.load_episodes(path) == [EPISODE]

    def test_save_empty_list(self, tmp_path):
        path = str(tmp_path / "episodes.json")
        da.save_episodes([], path)
        assert da.load_episodes(path) == []

    def test_get_existing_ids(self):
        episodes = [
            {"id": "A1M5hduoj3E"},
            {"id": "OTHERvid123"},
            {"id": None},
            {"id": ""},
            {"title": "no id at all"},
        ]
        assert da.get_existing_ids(episodes) == {"A1M5hduoj3E", "OTHERvid123"}
        assert da.get_existing_ids([]) == set()

    def test_download_then_index_round_trip(self, tmp_path, monkeypatch):
        """The realistic flow: resolve the index path, download, save, reload."""
        repo = tmp_path / "repo"
        repo.mkdir()
        podcasts = tmp_path / "Podcasts"
        podcasts.mkdir()
        monkeypatch.chdir(repo)
        _install_fake_ytdlp(
            monkeypatch, DownloadPlan(video_id="FLOWvid1234", title="Flow", duration=60)
        )

        index = da.resolve_episodes_file(None, str(podcasts))
        meta = da.download_audio("FLOWvid1234", str(podcasts))
        da.save_episodes([meta], index)

        assert index == str(podcasts / ".episodes.json")
        assert list(repo.iterdir()) == []  # the checkout stays clean
        loaded = da.load_episodes(index)
        assert loaded == [meta]
        assert (podcasts / loaded[0]["relpath"]).exists()


# ---------------------------------------------------------------------------
# detect_js_runtimes
# ---------------------------------------------------------------------------


class TestDetectJsRuntimes:
    def test_finds_runtimes_on_path(self, tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        _make_stub_exe(bindir, "node")
        _make_stub_exe(bindir, "deno", "#!/bin/sh\necho deno 2.0.0\n")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.setattr(da, "_JS_RUNTIME_CACHE", None)

        found = da.detect_js_runtimes()

        assert set(found) == {"deno", "node"}
        for name, config in found.items():
            assert isinstance(config, dict)
            assert config["path"] == str(bindir / name)
            assert os.access(config["path"], os.X_OK)

    def test_returns_empty_dict_when_nothing_is_installed(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        monkeypatch.setattr(da, "_JS_RUNTIME_CACHE", None)

        assert da.detect_js_runtimes() == {}

    def test_unknown_executables_are_ignored(self, tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        _make_stub_exe(bindir, "node")
        _make_stub_exe(bindir, "python3")
        _make_stub_exe(bindir, "rhino")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.setattr(da, "_JS_RUNTIME_CACHE", None)

        assert set(da.detect_js_runtimes()) == {"node"}

    def test_result_is_cached(self, tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        _make_stub_exe(bindir, "node")
        _make_stub_exe(bindir, "bun", "#!/bin/sh\necho 1.1.0\n")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.setattr(da, "_JS_RUNTIME_CACHE", None)

        first = da.detect_js_runtimes()
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        second = da.detect_js_runtimes()

        assert second is first  # same object: no second PATH scan
        assert set(second) == {"node", "bun"}

    def test_empty_result_is_also_cached(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        monkeypatch.setattr(da, "_JS_RUNTIME_CACHE", None)

        assert da.detect_js_runtimes() == {}
        bindir = tmp_path / "bin"
        _make_stub_exe(bindir, "node")
        monkeypatch.setenv("PATH", str(bindir))
        assert da.detect_js_runtimes() == {}

    def test_keys_are_accepted_by_yt_dlp(self, monkeypatch):
        """yt-dlp only enables Deno by default; this is why the helper exists."""
        from yt_dlp.globals import supported_js_runtimes

        monkeypatch.setattr(da, "_JS_RUNTIME_CACHE", None)
        found = da.detect_js_runtimes()

        assert set(found) <= set(supported_js_runtimes.value)
        ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "js_runtimes": found})
        # _clean_js_runtimes() pops anything unsupported; nothing should be lost.
        assert ydl.params["js_runtimes"] == found
        # The default without our help is Deno only.
        default = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
        assert set(default.params["js_runtimes"]) == {"deno"}

    def test_detected_runtimes_reach_the_downloader(self, monkeypatch):
        runtimes = {"node": {"path": "/opt/node22/bin/node"}}
        monkeypatch.setattr(da, "_JS_RUNTIME_CACHE", runtimes)
        opts = dict(da.build_ydl_opts("out/%(id)s.%(ext)s", "m4a", True, False))
        opts.update(quiet=True, no_warnings=True)

        ydl = yt_dlp.YoutubeDL(opts)

        assert ydl.params["js_runtimes"] == runtimes

    def test_older_yt_dlp_without_pluggable_runtimes(self, monkeypatch):
        monkeypatch.setattr(da, "_JS_RUNTIME_CACHE", None)
        monkeypatch.setitem(sys.modules, "yt_dlp.globals", types.SimpleNamespace())

        assert da.detect_js_runtimes() == {}

    def test_warns_when_node_is_the_only_runtime_and_too_old(
        self, tmp_path, monkeypatch, capsys
    ):
        bindir = tmp_path / "bin"
        _make_stub_exe(bindir, "node", "#!/bin/sh\necho v18.19.0\n")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.setattr(da, "_JS_RUNTIME_CACHE", None)

        found = da.detect_js_runtimes()

        assert set(found) == {"node"}
        err = capsys.readouterr().err
        assert "older than v22" in err
        assert "nodejs-lts" in err

    def test_no_warning_for_a_modern_node(self, tmp_path, monkeypatch, capsys):
        bindir = tmp_path / "bin"
        _make_stub_exe(bindir, "node", "#!/bin/sh\necho v22.11.0\n")
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.setattr(da, "_JS_RUNTIME_CACHE", None)

        da.detect_js_runtimes()

        assert "older than v22" not in capsys.readouterr().err

    @pytest.mark.parametrize(
        "output,expected",
        [
            ("v22.11.0\n", True),
            ("v24.0.1\n", True),
            ("v22.0.0\n", True),  # exactly the minimum
            ("v21.7.3\n", False),
            ("v18.19.0\n", False),
            # Unparseable output means "can't tell" - stay quiet and let yt-dlp
            # decide, rather than warning about a Node that may be fine.
            ("", True),
            ("not a version\n", True),
        ],
    )
    def test_node_version_check(self, tmp_path, output, expected):
        exe = _make_stub_exe(tmp_path / "bin", "node", f"#!/bin/sh\nprintf '{output}'\n")
        assert da._node_version_ok(str(exe)) is expected

    def test_node_version_check_is_forgiving_when_it_cannot_run(self, tmp_path):
        """If the probe itself fails we say nothing and let yt-dlp decide."""
        assert da._node_version_ok(str(tmp_path / "definitely-not-here")) is True

    def test_node_version_minimum_is_configurable(self, tmp_path):
        exe = _make_stub_exe(tmp_path / "bin", "node", "#!/bin/sh\necho v20.11.0\n")
        assert da._node_version_ok(str(exe), minimum=20) is True
        assert da._node_version_ok(str(exe), minimum=22) is False


# ---------------------------------------------------------------------------
# Module-level sanity
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_defaults_favour_the_fast_path(self):
        assert da.DEFAULT_AUDIO_FORMAT == "m4a"
        assert set(da.AUDIO_FORMAT_CHOICES) == {"m4a", "mp3", "keep"}
        assert da.SHORTS_MAX_DURATION == 180
        assert da.HTTP_CHUNK_SIZE == 10 * 1024 * 1024
        assert da.SOCKET_TIMEOUT == 30
        assert ".m4a" == da.AUDIO_EXTS[0]

    def test_module_lives_in_the_repo_root(self):
        assert os.path.dirname(os.path.abspath(da.__file__)) == REPO_ROOT


class TestChannelIdCacheIsKeyedByUrl:
    """The cache used to be a bare ID in one file with no idea which channel it
    belonged to, so a second channel silently got the first channel's videos."""

    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        da._store_channel_id("https://youtube.com/@alpha", "UCaaaaaaaaaaaaaaaaaaaaaa")
        da._store_channel_id("https://youtube.com/@beta", "UCbbbbbbbbbbbbbbbbbbbbbb")
        cache = da._load_channel_id_cache()
        assert cache["https://youtube.com/@alpha"] == "UCaaaaaaaaaaaaaaaaaaaaaa"
        assert cache["https://youtube.com/@beta"] == "UCbbbbbbbbbbbbbbbbbbbbbb"

    def test_cached_url_skips_the_network(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        da._store_channel_id("https://youtube.com/@alpha", "UCaaaaaaaaaaaaaaaaaaaaaa")

        def explode(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("cache hit should not hit yt-dlp")

        monkeypatch.setattr(da.yt_dlp, "YoutubeDL", explode)
        assert da.discover_channel_id("https://youtube.com/@alpha") == "UCaaaaaaaaaaaaaaaaaaaaaa"

    def test_other_channel_does_not_reuse_the_cached_id(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        da._store_channel_id("https://youtube.com/@alpha", "UCaaaaaaaaaaaaaaaaaaaaaa")

        calls = []

        class _Ydl:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                calls.append(url)
                return None

        monkeypatch.setattr(da.yt_dlp, "YoutubeDL", _Ydl)
        got = da.discover_channel_id("https://youtube.com/@beta")
        assert got != "UCaaaaaaaaaaaaaaaaaaaaaa"
        assert calls == ["https://youtube.com/@beta"]

    def test_legacy_bare_id_file_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # v1 wrote just the ID, with no record of which channel it was for.
        (tmp_path / da.YOUTUBE_RSS_CACHE).write_text("UColdoldoldoldoldoldold")
        assert da._load_channel_id_cache() == {}


class TestArtworkStaysWithItsAudio:
    def test_thumbnail_follows_the_audio_filename_on_collision(self, tmp_path, monkeypatch):
        """If the title is taken, the audio keeps its video-id name - and the
        artwork must keep that same stem or the app shows no cover."""
        out = tmp_path / "Podcasts" / "General"
        out.mkdir(parents=True)
        # Something already occupies the sanitized title.
        (out / "Taken Title.m4a").write_bytes(b"existing")

        plan = DownloadPlan(
            video_id="VID12345678",
            title="Taken Title",
            duration=900,
            audio_ext=".m4a",
            thumb_ext=".jpg",
        )
        monkeypatch.setattr(da.yt_dlp, "YoutubeDL", FakeYoutubeDLFactory(plan))
        meta = da.download_audio("VID12345678", str(out))

        assert meta is not None
        audio_stem = os.path.splitext(meta["relpath"])[0]
        thumb_stem = os.path.splitext(meta["thumbnail_relpath"])[0]
        assert audio_stem == thumb_stem, "artwork must sit next to its audio"
        assert (out / meta["relpath"]).exists()
        assert (out / meta["thumbnail_relpath"]).exists()
        # The pre-existing file was not clobbered.
        assert (out / "Taken Title.m4a").read_bytes() == b"existing"


class TestSaveEpisodesCleansUp:
    def test_temp_file_removed_when_serialisation_fails(self, tmp_path):
        target = tmp_path / "episodes.json"
        unserialisable = [{"id": "x", "bad": object()}]
        with pytest.raises(TypeError):
            da.save_episodes(unserialisable, str(target))
        assert not target.exists()
        assert not (tmp_path / "episodes.json.tmp").exists()
        assert list(tmp_path.iterdir()) == []
