"""Playback-control tests for the now-playing bar in app/main.py.

ISSUES.md #2: podcasts need a 10-second skip, a position readout and a scrub
bar.  These tests drive the *real* app class against a fake
``android.media.MediaPlayer`` that behaves like the Java one -- integer
positions, integer duration, an ``isPlaying`` flag and a hard record of any
call made after ``release()``.

The harness's generic ``FakeJavaObject`` cannot be used for this: every
``get*()`` on it returns another ``FakeJavaObject``, so there is no arithmetic
to assert on.  :class:`FakeMediaPlayer` fills that gap and still records into
``harness.recorder`` so recorder-based assertions keep working.

What is checked here, in one line each:

* +10s / -10s call ``seekTo`` with the right millisecond value, and clamp at
  both ends of the track;
* the readout renders ``"12:04 / 15:47"`` and reaches the label in the KV;
* the scrub bar follows playback, is left alone while a finger holds it, and
  seeks once on release;
* the ``Clock`` interval is cancelled on stop / pause / episode change / app
  stop, and **no MediaPlayer call is ever made after release()** -- the crash
  CLAUDE.md warns about.
"""

from __future__ import annotations

import sys

import pytest

import harness
import lint_kivymd

APP_MAIN = harness.APP_MAIN

DURATION_MS = 947_000  # 15:47


# ---------------------------------------------------------------------------
# A MediaPlayer stand-in with real numbers
# ---------------------------------------------------------------------------


class FakeMediaPlayer:
    """android.media.MediaPlayer with integer semantics and a call log."""

    instances: list["FakeMediaPlayer"] = []
    used_after_release: list[str] = []

    def __init__(self):
        self.position = 0
        self.duration = DURATION_MS
        self.playing = False
        self.prepared = False
        self.released = False
        self.calls: list[tuple] = []
        FakeMediaPlayer.instances.append(self)
        harness.recorder.record("android.media.MediaPlayer", "__init__", (), {})

    # -- helpers for tests ------------------------------------------------
    @classmethod
    def reset(cls):
        cls.instances = []
        cls.used_after_release = []

    @classmethod
    def latest(cls) -> "FakeMediaPlayer":
        assert cls.instances, "no MediaPlayer was ever created"
        return cls.instances[-1]

    def names(self) -> list[str]:
        return [name for name, _args in self.calls]

    def args_for(self, name: str) -> list[tuple]:
        return [args for call, args in self.calls if call == name]

    # -- the Java API the app uses ---------------------------------------
    def _record(self, name, *args):
        if self.released:
            # Real pyjnius would throw IllegalStateException here and the app
            # would die; app/main.py swallows exceptions, so record instead of
            # raising and let the test assert on it.
            FakeMediaPlayer.used_after_release.append(name)
        self.calls.append((name, args))
        harness.recorder.record("android.media.MediaPlayer", name, args, {})

    def setAudioStreamType(self, stream):
        self._record("setAudioStreamType", stream)

    def setDataSource(self, path):
        self._record("setDataSource", path)

    def prepare(self):
        self._record("prepare")
        self.prepared = True

    def start(self):
        self._record("start")
        self.playing = True

    def pause(self):
        self._record("pause")
        self.playing = False

    def stop(self):
        self._record("stop")
        self.playing = False

    def isPlaying(self):
        self._record("isPlaying")
        return self.playing

    def getCurrentPosition(self):
        self._record("getCurrentPosition")
        return self.position

    def getDuration(self):
        self._record("getDuration")
        return self.duration

    def seekTo(self, ms):
        self._record("seekTo", ms)
        self.position = int(ms)

    def release(self):
        self._record("release")
        self.released = True
        self.playing = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    harness.setup(with_android=True)
    storage = tmp_path_factory.mktemp("player_storage")
    sys.modules["android.storage"].app_storage_path = lambda: str(storage)
    return harness.load_app_module()


@pytest.fixture(scope="module")
def app(app_module):
    """A built, started app whose KV bindings really point at *this* instance.

    ``kivy.lang.parser.ProxyApp`` (the ``app`` name inside KV) caches the first
    running App it is ever asked for and only drops it when that app dispatches
    ``on_stop``.  Another test module having built an app first would therefore
    leave this one's KV bound to the *other* app, and every assertion about the
    now-playing labels would silently test the wrong object.  So the proxy is
    pointed at this instance for the duration of the module and restored after.
    """
    from kivy.app import App
    from kivy.lang.parser import global_idmap

    proxy = global_idmap["app"]
    previous_app = App._running_app
    previous_obj = object.__getattribute__(proxy, "_obj")

    harness.install_window()
    instance = harness.get_app_class(app_module)()
    object.__setattr__(proxy, "_obj", instance)
    try:
        instance.root = instance.build()
        instance.on_start()
        harness.pump_clock(2.5)
        yield instance
    finally:
        harness.stop_app(instance)
        object.__setattr__(proxy, "_obj", previous_obj)
        App._running_app = previous_app


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "Episode One.m4a"
    path.write_bytes(b"\x00" * 1024)
    return path


@pytest.fixture
def episode(audio_file):
    return {"title": "Episode One", "path": str(audio_file), "thumb": ""}


@pytest.fixture
def player(app, monkeypatch):
    """Route ``autoclass('android.media.MediaPlayer')`` to FakeMediaPlayer."""
    jnius = sys.modules["jnius"]
    real_autoclass = jnius.autoclass

    def autoclass(name, *args, **kwargs):
        if name == "android.media.MediaPlayer":
            harness.recorder.autoclass_names.append(name)
            return FakeMediaPlayer
        return real_autoclass(name, *args, **kwargs)

    monkeypatch.setattr(jnius, "autoclass", autoclass)
    harness.recorder.reset()
    FakeMediaPlayer.reset()
    yield FakeMediaPlayer
    # Never let an interval survive into the next test.
    app.stop_playback()
    app._show_now_playing(False)


@pytest.fixture
def playing(app, player, episode):
    """An episode selected and playing, with a fake player behind it."""
    app.select_episode(episode)
    current = FakeMediaPlayer.latest()
    assert current.playing, "select_episode() did not start the MediaPlayer"
    return current


# ---------------------------------------------------------------------------
# 1. The controls exist and are wired up
# ---------------------------------------------------------------------------


def test_now_playing_card_has_a_scrub_bar(app):
    assert "progress_slider" in app.root.ids, (
        "the now-playing card has no progress_slider; ids are "
        f"{sorted(app.root.ids)}"
    )


@pytest.mark.parametrize("method", ["skip_back", "skip_forward", "on_seek_active"])
def test_kv_wires_the_new_controls(app, method):
    blocks = lint_kivymd.extract_kv_blocks(APP_MAIN)
    wired = {name for block in blocks for _l, name, _n in lint_kivymd.parse_kv_app_calls(block)}
    assert method in wired, f"nothing in the KV calls app.{method}(); wired: {sorted(wired)}"
    assert callable(getattr(app, method, None))


def test_readout_label_is_bound_to_the_property(app):
    label = app.root.ids.now_playing_time_label
    app.now_playing_time = "1:23 / 4:56"
    harness.pump_clock(0.2)
    assert label.text == "1:23 / 4:56", (
        f"the KV label does not follow app.now_playing_time (shows {label.text!r})"
    )


# ---------------------------------------------------------------------------
# 2. Time formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "milliseconds,expected",
    [
        (0, "0:00"),
        (999, "0:00"),
        (1000, "0:01"),
        (61_000, "1:01"),
        (724_000, "12:04"),
        (947_000, "15:47"),
        (3_671_000, "1:01:11"),
        (-5000, "0:00"),  # clamped, never "-1:-5"
        (None, "0:00"),
        ("nonsense", "0:00"),
    ],
)
def test_format_clock(app_module, milliseconds, expected):
    assert app_module.format_clock(milliseconds) == expected


def test_readout_shows_position_and_duration(app, playing):
    playing.position = 724_000
    harness.pump_clock(1.0)
    assert app.now_playing_time == "12:04 / 15:47", app.now_playing_time


def test_readout_is_populated_as_soon_as_playback_starts(app, playing):
    assert app.now_playing_time == "0:00 / 15:47", app.now_playing_time


# ---------------------------------------------------------------------------
# 3. -10s / +10s
# ---------------------------------------------------------------------------


def test_skip_forward_seeks_ten_seconds_ahead(app, playing):
    playing.position = 60_000
    app.skip_forward()
    assert playing.args_for("seekTo") == [(70_000,)]
    assert playing.position == 70_000


def test_skip_back_seeks_ten_seconds_back(app, playing):
    playing.position = 60_000
    app.skip_back()
    assert playing.args_for("seekTo") == [(50_000,)]


def test_skip_back_clamps_at_the_start(app, playing):
    playing.position = 4_000
    app.skip_back()
    assert playing.args_for("seekTo") == [(0,)], "-10s ran off the front of the track"


def test_skip_forward_clamps_at_the_end(app, playing):
    playing.position = DURATION_MS - 3_000
    app.skip_forward()
    assert playing.args_for("seekTo") == [(DURATION_MS,)], (
        "+10s seeked past the end of the track"
    )


def test_skip_updates_the_readout_immediately(app, playing):
    playing.position = 60_000
    app.skip_forward()
    assert app.now_playing_time == "1:10 / 15:47", app.now_playing_time


def test_skipping_with_no_player_is_a_no_op(app, player):
    app.stop_playback()
    harness.recorder.reset()
    app.skip_forward()
    app.skip_back()
    assert not harness.recorder.method_calls("seekTo")


def test_skipping_works_while_paused(app, playing):
    playing.position = 60_000
    app.toggle_play()  # pause
    assert not playing.playing
    app.skip_forward()
    assert playing.args_for("seekTo") == [(70_000,)]


# ---------------------------------------------------------------------------
# 4. The scrub bar
# ---------------------------------------------------------------------------


def test_slider_follows_playback(app, playing):
    playing.position = 300_000
    harness.pump_clock(1.0)
    slider = app.root.ids.progress_slider
    assert slider.max == pytest.approx(DURATION_MS / 1000.0)
    assert slider.value == pytest.approx(300.0)


def test_slider_is_not_moved_while_the_user_drags(app, playing):
    slider = app.root.ids.progress_slider
    app.on_seek_active(True)  # finger down
    slider.value = 120.0
    playing.position = 800_000
    harness.pump_clock(1.0)
    assert slider.value == pytest.approx(120.0), (
        "the Clock yanked the thumb away from the finger"
    )


def test_releasing_the_thumb_seeks_there(app, playing):
    slider = app.root.ids.progress_slider
    app.on_seek_active(True)
    slider.value = 120.0
    app.on_seek_active(False)  # finger up
    assert playing.args_for("seekTo") == [(120_000,)]
    assert app.now_playing_time == "2:00 / 15:47", app.now_playing_time


def test_scrubbing_past_the_end_is_clamped(app, playing):
    slider = app.root.ids.progress_slider
    slider.max = 100_000.0  # a stale/oversized range must not seek past the file
    slider.value = 99_000.0
    app.on_seek_active(False)
    assert playing.args_for("seekTo") == [(DURATION_MS,)]


def test_slider_resets_when_the_card_is_hidden(app, playing):
    playing.position = 300_000
    harness.pump_clock(1.0)
    app._show_now_playing(False)
    slider = app.root.ids.progress_slider
    assert slider.value == 0
    assert app.now_playing_time == ""


# ---------------------------------------------------------------------------
# 5. The Clock interval: scheduled, and always cancelled
# ---------------------------------------------------------------------------


def _calls_during_pump(target: FakeMediaPlayer, seconds: float = 2.0) -> list[str]:
    before = len(target.calls)
    harness.pump_clock(seconds)
    return target.names()[before:]


def test_playing_schedules_a_progress_interval(app, playing):
    assert app._progress_event is not None, "no Clock interval drives the readout"
    assert _calls_during_pump(playing), "the interval never polled the MediaPlayer"


def test_stop_cancels_the_interval(app, playing):
    app.stop_playback()
    assert app._progress_event is None, "the readout interval outlived playback"
    assert not _calls_during_pump(playing), (
        "the interval kept polling a released MediaPlayer"
    )
    assert FakeMediaPlayer.used_after_release == [], (
        f"MediaPlayer used after release(): {FakeMediaPlayer.used_after_release}"
    )


def test_pause_cancels_the_interval_and_resume_restarts_it(app, playing):
    app.toggle_play()  # pause
    assert app._progress_event is None
    assert not _calls_during_pump(playing), "the readout kept polling while paused"
    assert app.now_playing_status == "Paused"

    app.toggle_play()  # resume
    assert app._progress_event is not None
    assert _calls_during_pump(playing), "the readout did not restart on resume"


def test_changing_episode_releases_the_old_player_and_reschedules(app, player, tmp_path):
    first = tmp_path / "one.m4a"
    second = tmp_path / "two.m4a"
    for path in (first, second):
        path.write_bytes(b"\x00" * 512)

    app.select_episode({"title": "One", "path": str(first)})
    old = FakeMediaPlayer.latest()
    app.select_episode({"title": "Two", "path": str(second)})
    new = FakeMediaPlayer.latest()

    assert new is not old, "the second episode reused the first player"
    assert old.released, "the previous MediaPlayer was never released"
    assert app._progress_event is not None
    assert not _calls_during_pump(old), "the old episode's interval is still running"
    assert FakeMediaPlayer.used_after_release == [], FakeMediaPlayer.used_after_release


def test_hiding_the_card_cancels_the_interval(app, playing):
    app._show_now_playing(False)
    assert app._progress_event is None
    assert not _calls_during_pump(playing)


def test_app_stop_cancels_the_interval(app, playing):
    app.on_stop()
    assert app._progress_event is None, "on_stop() left a Clock interval running"
    assert not _calls_during_pump(playing)
    assert FakeMediaPlayer.used_after_release == [], FakeMediaPlayer.used_after_release


def test_a_dead_player_unschedules_itself(app, playing):
    """If the player disappears from under the interval it must stop, not spin."""
    app._player = None
    harness.pump_clock(1.0)
    assert app._progress_event is None


# ---------------------------------------------------------------------------
# 6. Reaching the end of the episode
# ---------------------------------------------------------------------------


def test_finishing_an_episode_stops_claiming_it_is_playing(app, playing):
    playing.position = DURATION_MS
    playing.playing = False  # what MediaPlayer does at the end of the file
    harness.pump_clock(1.0)

    assert app.play_icon == "play", "the pause icon is still showing after the end"
    assert app.now_playing_status == "Finished", app.now_playing_status
    assert app.now_playing_time == "15:47 / 15:47", app.now_playing_time
    assert app._progress_event is None, "the interval kept running after the end"


def test_a_pause_near_the_end_is_not_mistaken_for_completion(app, playing):
    playing.position = DURATION_MS - 60_000  # a minute left
    playing.playing = False
    harness.pump_clock(1.0)
    assert app.now_playing_status != "Finished"


def test_stop_leaves_the_readout_at_the_start(app, playing):
    playing.position = 300_000
    harness.pump_clock(1.0)
    app.stop_playback()
    assert app.now_playing_status == "Stopped"
    assert app.play_icon == "play"
    assert app.now_playing_time == "0:00 / 15:47", app.now_playing_time
