"""Layout and legibility regressions found by actually rendering the app.

Every bug guarded here shipped in v3.0.0 while the rest of the suite was
green, because the suite only ever built widgets and called methods.  These
tests assert the *effect* a user would see, not that a call did not raise.

The off-screen geometry check lives in ``check_layout.py``, which needs a real
GL context; this module covers the parts that are decidable without one.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402


# ---------------------------------------------------------------------------
# The selected bottom-navigation tab has to be readable
# ---------------------------------------------------------------------------

# KivyMD 1.2.0 uses exactly [1, 1, 1, 1] as its "caller did not set this"
# sentinel for text_color_active (bottomnavigation.py:377 and :752).  When it
# sees that value it substitutes theme_cls.primary_color -- which this app also
# uses as panel_color, so the selected tab was drawn purple on purple and
# vanished.  Any other value, however close to white, takes the real branch.
KIVYMD_UNSET_COLOR = [1.0, 1.0, 1.0, 1.0]


def test_kivymd_still_uses_the_unset_colour_sentinel():
    """If KivyMD ever drops this quirk, the workaround below can go too."""
    import inspect

    from kivymd.uix.bottomnavigation import bottomnavigation

    source = inspect.getsource(bottomnavigation)
    assert "text_color_active == [1, 1, 1, 1]" in source, (
        "KivyMD no longer treats [1, 1, 1, 1] as 'unset'; re-check whether "
        "app/main.py still needs an off-white active tab colour."
    )


def test_selected_tab_colour_is_not_the_unset_sentinel(app):
    nav = app.root.ids.nav
    assert list(nav.text_color_active) != KIVYMD_UNSET_COLOR, (
        "text_color_active is KivyMD's 'unset' sentinel, so the selected tab "
        "falls back to primary_color and becomes invisible against panel_color."
    )


def test_selected_tab_contrasts_with_the_panel_behind_it(app):
    """The real requirement: active text must not match the bar it sits on.

    Read the colour the header actually got, not the one the KV asked for --
    KivyMD substitutes primary_color for the sentinel *inside* the widget, so
    a test that trusts ``nav.text_color_active`` passes on the broken build.
    """
    nav = app.root.ids.nav
    current = nav.ids.tab_manager.current
    header = nav.ids.tab_manager.get_screen(current).header

    effective = [round(c, 3) for c in header._text_color_normal[:3]]
    panel = [round(c, 3) for c in nav.panel_color[:3]]
    distance = sum(abs(a - p) for a, p in zip(effective, panel))
    assert distance > 0.5, (
        f"the selected tab renders in {effective}, indistinguishable from the "
        f"panel behind it {panel} -- the label is invisible"
    )


def test_inactive_tabs_are_dimmer_than_the_selected_one(app):
    nav = app.root.ids.nav
    assert nav.text_color_normal[3] < nav.text_color_active[3] or list(
        nav.text_color_normal[:3]
    ) != list(nav.text_color_active[:3]), (
        "active and inactive tabs render identically, so nothing shows which "
        "tab you are on"
    )


# ---------------------------------------------------------------------------
# Episode rows show the channel name the user typed, not the folder on disk
# ---------------------------------------------------------------------------


def test_episode_rows_use_the_typed_channel_name(app, tmp_path, monkeypatch):
    library = tmp_path / "Podcasts"
    folder = library / "AI_News_NateBJones"
    folder.mkdir(parents=True)
    (folder / "Episode.m4a").write_bytes(b"\x00" * 2048)

    monkeypatch.setattr(app, "_podcast_dir", str(library))
    monkeypatch.setattr(
        app,
        "_read_channels",
        lambda: [{"name": "Nate B Jones", "folder": "AI_News_NateBJones", "url": ""}],
    )

    episodes, unreadable = app._collect_episodes()
    assert not unreadable
    assert episodes, "the fixture episode was not picked up"
    assert episodes[0]["channel"] == "Nate B Jones", (
        "the Downloads list is showing the sanitised folder name instead of "
        "the channel name entered in the Add tab"
    )


def test_unknown_folder_falls_back_to_the_folder_name(app, tmp_path, monkeypatch):
    """A folder Termux made that the app has no channel entry for still lists."""
    library = tmp_path / "Podcasts"
    folder = library / "General"
    folder.mkdir(parents=True)
    (folder / "Episode.m4a").write_bytes(b"\x00" * 2048)

    monkeypatch.setattr(app, "_podcast_dir", str(library))
    monkeypatch.setattr(app, "_read_channels", lambda: [])

    episodes, _ = app._collect_episodes()
    assert episodes and episodes[0]["channel"] == "General"


def test_channel_names_survive_a_broken_channels_file(app, monkeypatch):
    """A corrupt channels.json must not empty the Downloads tab."""

    def boom():
        raise ValueError("corrupt channels.json")

    monkeypatch.setattr(app, "_read_channels", boom)
    assert app._channel_display_names() == {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    harness.setup(with_android=True)
    storage = tmp_path_factory.mktemp("ui_layout_storage")
    sys.modules["android.storage"].app_storage_path = lambda: str(storage)
    return harness.load_app_module()


@pytest.fixture(scope="module")
def app(app_module):
    harness.install_window()
    instance = harness.get_app_class(app_module)()
    try:
        instance.root = instance.build()
        instance.on_start()
        harness.pump_clock(2.5)
        yield instance
    finally:
        harness.stop_app(instance)
