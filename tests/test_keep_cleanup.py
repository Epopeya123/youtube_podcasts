"""Starred episodes, and the automatic removal of the ones you did not star.

This is the only code in the app that deletes a file the user did not ask to
delete, so the tests lean on the ways it could destroy something: the grace
period after an update, the library boundary, unknown ages, and starring.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402

DAY = 86400.0


def make_episode(folder: Path, name: str, age_days: float, size: int = 2048) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)
    when = time.time() - age_days * DAY
    os.utime(path, (when, when))
    return path


# ---------------------------------------------------------------------------
# Starring
# ---------------------------------------------------------------------------


def test_starring_survives_a_restart(app, library):
    episode = {"title": "Keep me", "path": str(make_episode(library / "General", "a.m4a", 1))}
    app._selected = episode
    app.toggle_keep()

    assert app.is_kept(episode)
    app._load_keep_list()  # as a fresh launch would
    assert app.is_kept(episode), "the star did not survive re-reading keep.json"


def test_unstarring_removes_it(app, library):
    episode = {"title": "x", "path": str(make_episode(library / "General", "b.m4a", 1))}
    app._selected = episode
    app.toggle_keep()
    app.toggle_keep()
    assert not app.is_kept(episode)


def test_star_is_stored_relative_to_the_library(app, library):
    """The same card mounts at /sdcard and /storage/emulated/0.

    An absolute key would silently forget every star the first time the
    library was reached by its other name.
    """
    path = make_episode(library / "AI_News", "c.m4a", 1)
    app._selected = {"path": str(path)}
    app.toggle_keep()

    stored = json.loads(Path(app_keep_file()).read_text())["kept"]
    assert stored == [os.path.join("AI_News", "c.m4a")]
    assert not any(os.path.isabs(k) for k in stored)


def test_toggle_keep_without_a_selection_is_harmless(app):
    app._selected = None
    app.toggle_keep()  # must not raise


def test_star_icon_tracks_the_selection(app, library):
    path = make_episode(library / "General", "d.m4a", 1)
    episode = {"title": "d", "path": str(path), "thumb": ""}

    app.select_episode(episode)
    assert app.keep_icon == "star-outline"
    app.toggle_keep()
    assert app.keep_icon == "star"


def test_starred_episodes_are_labelled_in_the_list(app, library):
    make_episode(library / "General", "e.m4a", 1)
    app.load_episodes()
    rows = app.root.ids.episode_list.children
    assert rows and "Starred" not in rows[0].secondary_text

    app._selected = app._episodes[0]
    app.toggle_keep()
    rows = app.root.ids.episode_list.children
    assert "Starred" in rows[0].secondary_text


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def test_nothing_is_deleted_during_the_grace_period(app, library):
    """Installing the update must not wipe a library collected over months.

    Every episode here is far older than the cleanup age; the only thing
    protecting them is that this phone has just learned the rule exists.
    """
    old = make_episode(library / "General", "ancient.m4a", 400)
    app._settings["cleanup_baseline"] = time.time()

    removed, _freed = app.cleanup_unstarred()

    assert removed == 0
    assert old.exists(), "a 400-day-old episode was deleted on the first launch"


def test_unstarred_and_old_is_deleted(app, library):
    old = make_episode(library / "General", "stale.m4a", 45)
    app._settings["cleanup_baseline"] = time.time() - 60 * DAY

    removed, freed = app.cleanup_unstarred()

    assert removed == 1
    assert freed == 2048
    assert not old.exists()


def test_starred_is_never_deleted(app, library):
    kept = make_episode(library / "General", "precious.m4a", 400)
    app._settings["cleanup_baseline"] = time.time() - 500 * DAY
    app._selected = {"path": str(kept)}
    app.toggle_keep()

    removed, _ = app.cleanup_unstarred()

    assert removed == 0
    assert kept.exists(), "a starred episode was deleted"


def test_recent_episodes_are_left_alone(app, library):
    fresh = make_episode(library / "General", "fresh.m4a", 3)
    app._settings["cleanup_baseline"] = time.time() - 500 * DAY

    removed, _ = app.cleanup_unstarred()

    assert removed == 0
    assert fresh.exists()


def test_the_thumbnail_goes_with_the_audio(app, library):
    audio = make_episode(library / "General", "withart.m4a", 45)
    thumb = library / "General" / "withart.jpg"
    thumb.write_bytes(b"\xff" * 64)
    app._settings["cleanup_baseline"] = time.time() - 60 * DAY

    app.cleanup_unstarred()

    assert not audio.exists()
    assert not thumb.exists(), "the artwork sidecar was orphaned"


def test_non_audio_files_are_never_touched(app, library):
    folder = library / "General"
    make_episode(folder, "old.m4a", 45)
    notes = folder / "notes.txt"
    notes.write_bytes(b"my notes")
    old = time.time() - 400 * DAY
    os.utime(notes, (old, old))
    app._settings["cleanup_baseline"] = time.time() - 60 * DAY

    app.cleanup_unstarred()

    assert notes.exists(), "cleanup deleted a file that was not an episode"


def test_cleanup_refuses_to_step_outside_the_library(app, library, tmp_path):
    outside = tmp_path / "elsewhere" / "family-photo.m4a"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"\x00" * 16)

    assert not app._is_inside_library(str(outside))
    assert not app._is_inside_library(str(library))  # the root itself is not an episode
    assert app._is_inside_library(str(library / "General" / "x.m4a"))


def test_an_unreadable_library_deletes_nothing(app, monkeypatch):
    monkeypatch.setattr(app, "_collect_episodes", lambda: ([], True))
    app._settings["cleanup_baseline"] = time.time() - 500 * DAY

    removed, _ = app.cleanup_unstarred()

    assert removed == 0


def test_an_episode_of_unknown_age_is_left_alone(app):
    app._settings["cleanup_baseline"] = time.time() - 500 * DAY
    assert app._episode_age_days({"path": "/nope/gone.m4a", "downloaded_at": ""}) is None


# ---------------------------------------------------------------------------
# Age
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stamp, expected_days",
    [
        ("2026-07-15T00:00:00", 10),
        ("2026-07-15T00:00:00Z", 10),
        ("2026-07-15T00:00:00+00:00", 10),
    ],
)
def test_manifest_timestamps_are_understood(app, stamp, expected_days):
    now = time.mktime(time.strptime("2026-07-25 00:00:00", "%Y-%m-%d %H:%M:%S"))
    now = _as_utc(now)
    age = app._episode_age_days({"downloaded_at": stamp, "path": ""}, now=now)
    assert age == pytest.approx(expected_days, abs=1.0)


def test_a_garbled_timestamp_falls_back_to_the_file(app, library):
    path = make_episode(library / "General", "weird.m4a", 12)
    age = app._episode_age_days({"downloaded_at": "not a date", "path": str(path)})
    assert age == pytest.approx(12, abs=0.1)


def _as_utc(local_epoch):
    """time.mktime() is local; the parser treats bare stamps as UTC."""
    import calendar

    return calendar.timegm(time.gmtime(local_epoch))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_KEEP_FILE = {}


def app_keep_file():
    return _KEEP_FILE["path"]


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    harness.setup(with_android=True)
    storage = tmp_path_factory.mktemp("keep_storage")
    sys.modules["android.storage"].app_storage_path = lambda: str(storage)
    return harness.load_app_module()


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "Podcasts"
    (root / "General").mkdir(parents=True)
    return root


@pytest.fixture
def app(app_module, library, tmp_path):
    harness.install_window()
    instance = harness.get_app_class(app_module)()
    instance.root = instance.build()

    # Point both the library and the keep list at this test's own directories,
    # after build() so they replace whatever the real ones were.
    app_module.KEEP_FILE = str(tmp_path / "keep.json")
    app_module.SETTINGS_FILE = str(tmp_path / "settings.json")
    _KEEP_FILE["path"] = app_module.KEEP_FILE
    instance._podcast_dir = str(library)
    instance._kept = set()

    instance.on_start()
    harness.pump_clock(2.5)
    try:
        yield instance
    finally:
        harness.stop_app(instance)
