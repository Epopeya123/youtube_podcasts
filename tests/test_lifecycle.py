"""Pause/resume regression tests -- ISSUES.md #1, the black screen on return.

What actually goes wrong on the phone
-------------------------------------
Android throws the SDL/EGL surface away whenever the activity is backgrounded,
and may hand the process a *brand new* GL context on the way back (Kivy says so
itself in the Texture docs).  Kivy 2.3.1's SDL2 path does nothing about either:

* ``WindowSDL._event_filter()`` answers ``app_didenterforeground`` by
  dispatching ``on_resume`` on the App and nothing else.
* ``WindowBase.create_window()`` -- the one place Kivy reloads GL resources
  with ``get_context().reload()`` -- is unbound on Android after the first
  call, so it never runs again.
* ``EventLoop.idle()`` only paints when ``window.canvas.needs_redraw`` is set.

So the app must reload the GL resources and ask for the repaints itself.  These
tests pin that behaviour down.

What these tests can and cannot prove
-------------------------------------
There is no Android and no real GPU here (no ``/dev/kvm``, and the GL backend is
mocked), so nothing here can prove that pixels reach the screen of a Motorola
Edge 60.  What they *can* prove, and do:

* the pause/resume handlers run to completion through Kivy's own SDL2 event
  filter, with the real Window and the real widget tree;
* the app triggers a full graphics-context reload -- the call that re-uploads
  every texture and shader -- which the old code never did;
* the window canvas is flagged for redraw *repeatedly* over the seconds after a
  resume, which is the difference between ``EventLoop.idle()`` painting a frame
  and skipping it entirely;
* the repaint pump then stops, so it is not a permanent 5 fps wakeup;
* none of the resume work happens synchronously inside SDL's event filter, and
  a failure in it cannot stop the repaint;
* the root widget, its canvas, the KV ids and the episode list all survive.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import pytest

import harness


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def window():
    return harness.install_window()


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    storage = tmp_path_factory.mktemp("lifecycle_storage")
    harness.setup(with_android=True)
    sys.modules["android.storage"].app_storage_path = lambda: str(storage)
    return harness.load_app_module()


@pytest.fixture(scope="module")
def app(app_module, window):
    with harness.running_app(app_module) as instance:
        yield instance


@pytest.fixture(scope="module")
def podcast_library(app, app_module, tmp_path_factory):
    """A Podcasts tree with one real episode, wired into the running app."""
    manifest_name = getattr(app_module, "MANIFEST_NAME", ".episodes.json")
    base = tmp_path_factory.mktemp("lifecycle_podcasts") / "Podcasts"
    channel = base / "natebjones"
    channel.mkdir(parents=True)
    (channel / "Episode One.m4a").write_bytes(b"\x00" * 2048)
    (channel / manifest_name).write_text(
        json.dumps(
            [
                {
                    "id": "vid1",
                    "title": "Episode One",
                    "relpath": "Episode One.m4a",
                    "duration": 900,
                    "filesize": 2048,
                    "downloaded_at": "2026-07-25T10:00:00",
                }
            ]
        )
    )
    original = getattr(app, "_podcast_dir", None)
    app._podcast_dir = str(base)
    yield base
    app._podcast_dir = original


@pytest.fixture(autouse=True)
def quiet_app_afterwards(app, window):
    """Leave no repaint pump (or paused flag) behind for the next test."""
    yield
    with contextlib.suppress(Exception):
        app._cancel_resume_redraw()
    app._paused = False
    with contextlib.suppress(Exception):
        window._pause_loop = False


# ---------------------------------------------------------------------------
# Helpers (deliberately local: harness.py is shared, this file is not)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def virtual_time():
    """Freeze Kivy's clock and hand back an ``advance(seconds)`` function.

    ``harness.pump_clock()`` re-anchors ``Clock._last_tick`` to wall-clock time
    on the way out, so two consecutive calls leave the scheduled events sitting
    a second in the "future" and a short second pump fires nothing.  These tests
    need to paint frames *between* advances, so they need one continuous
    timeline instead.
    """
    from kivy.clock import Clock

    original_time = Clock.time
    original_fps = getattr(Clock, "_max_fps", 0)
    state = {"now": max(float(original_time()), float(Clock._last_tick) + 0.05)}

    def advance(seconds: float, step: float = 0.05) -> None:
        end = state["now"] + seconds
        guard = int(seconds / step) + 10
        while state["now"] < end and guard > 0:
            guard -= 1
            state["now"] += step
            Clock.tick()

    Clock._max_fps = 0
    Clock.time = lambda: state["now"]
    try:
        yield advance
    finally:
        Clock.time = original_time
        with contextlib.suppress(Exception):
            Clock._max_fps = original_fps
            Clock._last_tick = float(original_time())


def paint_a_frame(window) -> bool:
    """Do what ``EventLoop.idle()`` does when it decides to draw.

    Returns ``window.canvas.needs_redraw`` afterwards, which is False in a
    healthy idle app: nothing will be painted again until something asks.
    """
    window.dispatch("on_draw")
    window.dispatch("on_flip")
    return bool(window.canvas.needs_redraw)


def background_and_return(app, window) -> None:
    """Send the app to the background and bring it back.

    Where possible this goes through Kivy's *own* SDL2 event filter, i.e. the
    exact code Android drives on the phone: ``app_willenterbackground`` ->
    ``App.on_pause`` -> pause loop -> ``app_didenterforeground`` ->
    ``App.on_resume``.
    """
    from kivy.app import App
    from kivy.base import EventLoop

    previous_app = App._running_app
    previous_quit = EventLoop.quit
    previous_stopping = EventLoop.stopping
    App._running_app = app
    try:
        if hasattr(window, "_event_filter"):
            window._event_filter("app_willenterbackground")
            assert getattr(window, "_pause_loop", False) is True, (
                "Kivy did not enter its pause loop -- on_pause must return True"
            )
            window._event_filter("app_didenterforeground")
        else:  # GL-free fallback window, no SDL event filter to drive
            assert app.dispatch("on_pause") is True
            app.dispatch("on_resume")
    finally:
        with contextlib.suppress(Exception):
            window._pause_loop = False
        App._running_app = previous_app
        EventLoop.quit = previous_quit
        EventLoop.stopping = previous_stopping


class ReloadSpy:
    """Counts full graphics-context reloads.

    ``Context.reload()`` is what re-uploads every texture and shader after the
    GL context has been replaced; its observers are the documented hook for
    "the context went away, put your data back".  If this never fires, artwork
    and labels come back as dead texture ids -- i.e. a black screen.
    """

    def __init__(self):
        self.count = 0

    def __call__(self, context):
        self.count += 1

    @contextlib.contextmanager
    def watching(self):
        from kivy.graphics.context import get_context

        context = get_context()
        context.add_reload_observer(self)
        try:
            yield self
        finally:
            with contextlib.suppress(Exception):
                context.remove_reload_observer(self)


# ---------------------------------------------------------------------------
# 1. The mechanism these tests rely on
# ---------------------------------------------------------------------------


def test_painting_clears_the_redraw_flag(app, window):
    """The premise: an idle Kivy app does not repaint, so somebody must ask.

    ``EventLoop.idle()`` runs ``window.dispatch('on_draw')`` only
    ``if window.canvas.needs_redraw``.  A freshly created (black) EGL surface
    plus a clear flag is exactly the reported bug.
    """
    assert paint_a_frame(window) is False, "drawing did not clear needs_redraw"
    window.canvas.ask_update()
    assert window.canvas.needs_redraw is True


# ---------------------------------------------------------------------------
# 2. Pause
# ---------------------------------------------------------------------------


def test_on_pause_keeps_the_process_alive(app):
    """Returning anything but True makes Kivy stop (or finish) the activity."""
    assert app.on_pause() is True
    assert app._paused is True


def test_pause_cancels_a_running_repaint_pump(app, window):
    with virtual_time() as advance:
        background_and_return(app, window)
        advance(0.5)
        assert app._resume_redraw_event is not None, "no repaint pump started"
        app.on_pause()
        assert app._resume_redraw_event is None, "pump kept running in background"


def test_on_stop_cancels_the_repaint_pump(app, window, monkeypatch):
    monkeypatch.setattr(app, "stop_playback", lambda *a, **k: None)
    with virtual_time() as advance:
        background_and_return(app, window)
        advance(0.5)
        assert app._resume_redraw_event is not None
        app.on_stop()
        assert app._resume_redraw_event is None


# ---------------------------------------------------------------------------
# 3. Resume: the graphics context
# ---------------------------------------------------------------------------


def test_resume_reloads_the_graphics_context(app, window):
    """The core of the fix: textures and shaders are put back.

    The old on_resume only re-read the filesystem, so after Android handed the
    process a new GL context every texture id in the scene was dangling.
    """
    with ReloadSpy().watching() as spy, virtual_time() as advance:
        background_and_return(app, window)
        assert spy.count == 0, (
            "the GL reload ran inside SDL's event filter; the replacement "
            "surface may not exist yet at that point"
        )
        advance(1.0)
        assert spy.count >= 1, "resume never reloaded the graphics context"


def test_resume_flags_the_window_for_repaint(app, window):
    with virtual_time() as advance:
        assert paint_a_frame(window) is False
        background_and_return(app, window)
        advance(1.0)
        assert window.canvas.needs_redraw is True, (
            "nothing asked for a repaint after resume -- EventLoop.idle() will "
            "skip on_draw and the new surface stays black"
        )


def test_resume_keeps_asking_for_repaints_for_a_few_seconds(app, window):
    """One repaint is not enough.

    Android hands SDL the replacement surface some frames after onResume, and
    that surface is double buffered, so a single frame only fills one buffer.
    Kivy's own android hook repaints at 5 fps for 5 seconds for this reason.
    """
    with virtual_time() as advance:
        paint_a_frame(window)
        background_and_return(app, window)
        advance(0.5)

        repaints = 0
        for _ in range(4):
            assert paint_a_frame(window) is False
            advance(0.4)
            if window.canvas.needs_redraw:
                repaints += 1
        assert repaints == 4, (
            f"only {repaints}/4 follow-up repaints were requested; a surface "
            "that arrives late would never be drawn into"
        )


def test_the_repaint_pump_stops_itself(app, window):
    """It must not become a permanent 5 fps wakeup on a phone battery."""
    with virtual_time() as advance:
        background_and_return(app, window)
        advance(app.RESUME_REDRAW_SECONDS + 2.0)
        assert app._resume_redraw_event is None, "repaint pump never stopped"
        assert paint_a_frame(window) is False
        advance(2.0)
        assert window.canvas.needs_redraw is False, "still repainting when idle"


# ---------------------------------------------------------------------------
# 4. Resume: the app's own work
# ---------------------------------------------------------------------------


def test_resume_does_no_disk_or_widget_work_inside_the_event_filter(app, monkeypatch):
    """on_resume is dispatched from inside SDL's event filter.

    Walking /sdcard and rebuilding the list there runs before the mainloop is
    going again, and creates textures on a context that may not be back yet.
    It has to be deferred to a real frame.
    """
    calls: list[str] = []
    monkeypatch.setattr(app, "load_channels", lambda: calls.append("channels"))
    monkeypatch.setattr(app, "load_episodes", lambda: calls.append("episodes"))

    with virtual_time() as advance:
        app.on_resume()
        assert calls == [], f"on_resume did {calls} synchronously"
        advance(app.RESUME_DATA_DELAY + 1.0)
        assert "episodes" in calls and "channels" in calls, (
            f"the library was never re-read after resume: {calls}"
        )


def test_a_failing_library_reload_still_leaves_the_screen_painted(app, window, monkeypatch):
    """A throw in the resume work must not cost the repaint.

    The old code wrapped both loads in one try/except, so the first exception
    skipped everything after it -- and nothing after it asked for a repaint.
    """

    def boom(*args, **kwargs):
        raise RuntimeError("storage permission revoked while backgrounded")

    monkeypatch.setattr(app, "load_channels", boom)
    monkeypatch.setattr(app, "load_episodes", boom)

    with ReloadSpy().watching() as spy, virtual_time() as advance:
        assert paint_a_frame(window) is False
        background_and_return(app, window)
        advance(app.RESUME_DATA_DELAY + 1.0)
        assert spy.count >= 1
        assert window.canvas.needs_redraw is True


def test_resume_does_not_walk_the_disk_if_backgrounded_again(app, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(app, "load_channels", lambda: calls.append("channels"))
    monkeypatch.setattr(app, "load_episodes", lambda: calls.append("episodes"))

    with virtual_time() as advance:
        app.on_resume()
        advance(0.1)
        app.on_pause()  # user flicked straight back out
        advance(app.RESUME_DATA_DELAY + 1.0)
        assert calls == [], f"re-read the library while backgrounded: {calls}"


# ---------------------------------------------------------------------------
# 5. The whole cycle: is the app renderable afterwards?
# ---------------------------------------------------------------------------


def test_app_is_renderable_after_a_pause_resume_cycle(app, window, podcast_library):
    app.load_episodes()
    root = app.root
    rows_before = len(root.ids.episode_list.children)
    assert rows_before, "fixture library produced no rows to begin with"

    with virtual_time() as advance:
        paint_a_frame(window)
        background_and_return(app, window)
        advance(app.RESUME_DATA_DELAY + 1.5)

        assert app.root is root, "the root widget was replaced"
        assert app.root.canvas is not None, "the root widget lost its canvas"
        assert app.root.children, "the root widget tree is empty"
        for name in ("nav", "episode_list", "now_playing_card", "status_label"):
            assert name in app.root.ids, f"KV id {name!r} no longer resolves"
        assert len(app.root.ids.episode_list.children) == rows_before, (
            "the episode list did not come back after resume"
        )
        assert app._episodes, "the episode index is empty after resume"
        assert window.canvas.needs_redraw is True


def test_a_real_frame_renders_after_resume(app, window, podcast_library):
    """Attach the real widget tree to the Window and draw it after a resume.

    With a mocked GL backend this cannot prove anything about pixels; it does
    prove the post-resume scene graph is walkable and drawable end to end
    without raising, textures included.
    """
    app.load_episodes()
    try:
        window.add_widget(app.root)
    except Exception as exc:  # pragma: no cover - another test left it attached
        pytest.skip(f"cannot attach root to window: {exc}")
    try:
        with virtual_time() as advance:
            paint_a_frame(window)
            background_and_return(app, window)
            advance(app.RESUME_DATA_DELAY + 1.5)
            assert window.canvas.needs_redraw is True
            assert paint_a_frame(window) is False, "the frame was never drawn"
    finally:
        window.remove_widget(app.root)


def test_repeated_pause_resume_cycles(app, window, podcast_library):
    """Three trips out and back: still one pump, still a full episode list."""
    app.load_episodes()
    rows = len(app.root.ids.episode_list.children)

    with ReloadSpy().watching() as spy, virtual_time() as advance:
        for cycle in range(3):
            paint_a_frame(window)
            background_and_return(app, window)
            advance(0.6)
            assert window.canvas.needs_redraw is True, f"cycle {cycle}: no repaint"
            assert app._resume_redraw_event is not None
        advance(app.RESUME_REDRAW_SECONDS + 2.0)
        assert spy.count >= 3, f"only {spy.count} context reloads for 3 resumes"
        assert app._resume_redraw_event is None
    assert len(app.root.ids.episode_list.children) == rows
    assert app.root.ids.episode_list.children, "episode list emptied out"


# ---------------------------------------------------------------------------
# 6. Source-level guards
# ---------------------------------------------------------------------------


def test_on_resume_source_defers_to_the_clock():
    """Cheap guard against someone putting the loads back into on_resume."""
    import ast

    source = Path(harness.APP_MAIN).read_text()
    tree = ast.parse(source)
    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "on_resume"
    ]
    assert handlers, "app/main.py has no on_resume"
    called = {
        node.func.attr
        for node in ast.walk(handlers[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "load_episodes" not in called and "load_channels" not in called, (
        "on_resume walks the disk inside SDL's event filter again"
    )
    assert "schedule_once" in called or "schedule_interval" in called, (
        "on_resume no longer defers its work to the Clock"
    )
