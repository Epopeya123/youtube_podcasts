"""Tests for termux/termux-url-opener - the share handler itself.

The ||| payload parsing, folder sanitising, config plumbing, and exit-status
propagation are all shell logic the Python suite cannot see, and the script
shipped untested for a long time. These tests run it with bash under a
throwaway HOME, with a stub `python` first on PATH that records its argv and
YTP_* environment instead of downloading anything.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_TESTS_DIR)
OPENER = os.path.join(REPO_ROOT, "termux", "termux-url-opener")


@pytest.fixture
def opener_env(tmp_path):
    """A fake phone HOME with a recording python stub."""
    home = tmp_path / "home"
    (home / "youtube_podcasts").mkdir(parents=True)
    (home / ".config").mkdir()

    stub_dir = home / "stubbin"
    stub_dir.mkdir()
    stub = stub_dir / "python"
    stub.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$RECORD_ARGS"\n'
        'env | grep "^YTP_" > "$RECORD_ENV" || true\n'
        'exit "${STUB_EXIT:-0}"\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    record_args = tmp_path / "argv.txt"
    record_env = tmp_path / "env.txt"
    env = {
        "HOME": str(home),
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "RECORD_ARGS": str(record_args),
        "RECORD_ENV": str(record_env),
        "STUB_EXIT": "0",
    }
    return {
        "home": home,
        "env": env,
        "record_args": record_args,
        "record_env": record_env,
    }


def _run(opener_env, payload, stub_exit=0):
    env = dict(opener_env["env"])
    env["STUB_EXIT"] = str(stub_exit)
    return subprocess.run(
        ["bash", OPENER, payload],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _argv(opener_env):
    return opener_env["record_args"].read_text().splitlines()


def _ytp_env(opener_env):
    lines = opener_env["record_env"].read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


class TestPayloadParsing:
    def test_plain_url_downloads_into_general(self, opener_env):
        result = _run(opener_env, "https://youtu.be/HAPPYvid123")
        argv = _argv(opener_env)

        assert result.returncode == 0
        assert "Done!" in result.stdout
        assert argv[0] == "download_audio.py"
        url_at = argv.index("--url")
        assert argv[url_at + 1] == "https://youtu.be/HAPPYvid123"
        out_at = argv.index("--output-dir")
        assert argv[out_at + 1].endswith("/storage/shared/Podcasts/General")

    def test_folder_and_format_suffix(self, opener_env):
        _run(opener_env, "https://youtu.be/HAPPYvid123|||MyShows|||mp3")
        argv = _argv(opener_env)

        fmt_at = argv.index("--audio-format")
        assert argv[fmt_at + 1] == "mp3"
        out_at = argv.index("--output-dir")
        assert argv[out_at + 1].endswith("/Podcasts/MyShows")

    def test_refresh_payload_runs_channel_mode(self, opener_env):
        _run(opener_env, "REFRESH:https://youtube.com/@natebjones|||NateB|||m4a")
        argv = _argv(opener_env)

        chan_at = argv.index("--channel-url")
        assert argv[chan_at + 1] == "https://youtube.com/@natebjones"
        assert "--url" not in argv


class TestFolderSanitising:
    def test_path_separators_cannot_escape_the_podcasts_tree(self, opener_env):
        _run(opener_env, "https://youtu.be/HAPPYvid123|||../evil|||m4a")
        argv = _argv(opener_env)

        out_at = argv.index("--output-dir")
        assert "/../" not in argv[out_at + 1]
        assert argv[out_at + 1].endswith("/Podcasts/.._evil")

    def test_bare_dotdot_falls_back_to_general(self, opener_env):
        _run(opener_env, "https://youtu.be/HAPPYvid123|||..|||m4a")
        argv = _argv(opener_env)

        out_at = argv.index("--output-dir")
        assert argv[out_at + 1].endswith("/Podcasts/General")


class TestConfigPlumbing:
    def test_cookies_file_with_spaces_survives_into_the_env(self, opener_env):
        conf = opener_env["home"] / ".config" / "youtube_podcasts.conf"
        conf.write_text('COOKIES_FILE="/data/my cookie jar.txt"\n')

        _run(opener_env, "https://youtu.be/HAPPYvid123")

        assert _ytp_env(opener_env)["YTP_COOKIES_FILE"] == "/data/my cookie jar.txt"

    def test_player_clients_survive_into_the_env(self, opener_env):
        conf = opener_env["home"] / ".config" / "youtube_podcasts.conf"
        conf.write_text("PLAYER_CLIENTS=default,web_embedded\n")

        _run(opener_env, "https://youtu.be/HAPPYvid123")

        assert _ytp_env(opener_env)["YTP_PLAYER_CLIENTS"] == "default,web_embedded"

    def test_unset_keys_export_nothing(self, opener_env):
        _run(opener_env, "https://youtu.be/HAPPYvid123")
        assert _ytp_env(opener_env) == {}


class TestHeredocLockstep:
    """run_podcast_download.sh is written by TWO heredocs - setup.sh's and
    update.sh's - and update.sh rewrites the installed copy on every run, so
    a change made in only one of them silently reverts. Same for the config
    template. Keep them byte-identical."""

    @staticmethod
    def _heredoc(path, marker):
        text = open(path).read()
        start = text.index(f"<< '{marker}'\n") + len(f"<< '{marker}'\n")
        end = text.index(f"\n{marker}\n", start)
        return text[start:end]

    def test_run_script_heredocs_are_identical(self):
        setup = self._heredoc(os.path.join(REPO_ROOT, "termux", "setup.sh"), "SCRIPT")
        update = self._heredoc(os.path.join(REPO_ROOT, "termux", "update.sh"), "SCRIPT")
        assert setup == update

    def test_conf_template_heredocs_are_identical(self):
        setup = self._heredoc(os.path.join(REPO_ROOT, "termux", "setup.sh"), "CONF")
        update = self._heredoc(os.path.join(REPO_ROOT, "termux", "update.sh"), "CONF")
        assert setup == update

    def test_cron_script_exports_the_session_env(self):
        """The 6-hour cron path must hand cookies/client overrides to python
        just like the share handler does."""
        script = self._heredoc(os.path.join(REPO_ROOT, "termux", "setup.sh"), "SCRIPT")
        assert 'export YTP_COOKIES_FILE="$COOKIES_FILE"' in script
        assert 'export YTP_PLAYER_CLIENTS="$PLAYER_CLIENTS"' in script

    def test_conf_template_documents_the_new_keys(self):
        conf = self._heredoc(os.path.join(REPO_ROOT, "termux", "setup.sh"), "CONF")
        assert "COOKIES_FILE=" in conf
        assert "PLAYER_CLIENTS=" in conf


class TestFailureReporting:
    def test_downloader_failure_propagates_and_explains(self, opener_env):
        result = _run(opener_env, "https://youtu.be/HAPPYvid123", stub_exit=1)

        assert result.returncode == 1
        assert "Something went wrong" in result.stdout
        # Both current YouTube failure modes get their own honest hint.
        assert "not a bot" in result.stdout
        assert "403" in result.stdout
        assert "Done!" not in result.stdout

    def test_success_does_not_show_the_failure_hints(self, opener_env):
        result = _run(opener_env, "https://youtu.be/HAPPYvid123")

        assert result.returncode == 0
        assert "Something went wrong" not in result.stdout
