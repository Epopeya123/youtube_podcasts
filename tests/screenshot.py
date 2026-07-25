#!/usr/bin/env python3
"""Render the real app off-device and save PNG screenshots.

Why this exists
---------------
The pytest suite builds ``app/main.py`` and calls its methods, but it never
draws a frame, so anything that is only wrong *on screen* (a control pushed off
the edge, a missing label, artwork that never loads) sails straight through it.
This script boots the same app through ``tests/harness.py``, gives it a
realistic fake ``Podcasts`` library, renders actual frames through Mesa's
software OpenGL under Xvfb, and writes the framebuffer to disk.

The GL trap this had to get past
--------------------------------
``tests/harness.py`` and ``tests/run_all.sh`` both force
``KIVY_GL_BACKEND=mock``.  The mock backend stubs every GL entry point, so
``glCompileShader`` never compiles anything and Kivy logs::

    Shader: <fragment> failed to compile (gl:0)
    Shader: <vertex> failed to compile (gl:0)

Nothing is ever drawn, and ``glReadPixels`` then returns whatever happened to
be in the framebuffer -- which is why earlier attempts produced noise.  Mesa
was never the problem: llvmpipe here reports OpenGL 4.5 / GLSL 4.50, which is
far more than Kivy needs.  The fix is simply to ask for a *real* backend
(``KIVY_GL_BACKEND=gl``) after the harness has set its own environment, and
before ``kivy`` is imported.  No Mesa version override and no special Xvfb
visual are required.

Usage
-----
    python3 tests/screenshot.py                  # all shots -> screenshots/
    python3 tests/screenshot.py --shot settings  # just one
    python3 tests/screenshot.py --list           # names of the available shots
    python3 tests/screenshot.py --keep-library   # leave the fake library on disk

Xvfb is started automatically when there is no ``DISPLAY``; the virtual screen
is sized to fit the window, because ``glReadPixels`` on the part of a window
that hangs off the edge of the X screen returns undefined pixels.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

# Phone-ish geometry.  The user's Motorola Edge 60 is 1080x2400 at density
# ~2.75 (393x873 dp).  720x1560 at density 2.0 is 360x780 dp -- the same shape
# and the same dp budget, at a size that is comfortable to look at.
DEFAULT_WIDTH = 720
DEFAULT_HEIGHT = 1560
DEFAULT_DENSITY = 2.0


# ---------------------------------------------------------------------------
# 0.  Re-exec under Xvfb
# ---------------------------------------------------------------------------


def _reexec_under_xvfb(width: int, height: int) -> "subprocess.CompletedProcess":
    """Restart this script inside ``xvfb-run`` with a screen big enough."""
    if not shutil.which("xvfb-run"):
        sys.exit(
            "no DISPLAY and xvfb-run is not installed.\n"
            "  apt-get install -y xvfb libmtdev1\n"
            "or run this script under an X display yourself."
        )
    screen = f"-screen 0 {width + 80}x{height + 80}x24"
    env = dict(os.environ, YTP_SCREENSHOT_XVFB="1")
    return subprocess.run(
        ["xvfb-run", "-a", "-s", screen, sys.executable, str(Path(__file__).resolve())]
        + sys.argv[1:],
        env=env,
    )


# ---------------------------------------------------------------------------
# 1.  The fake /sdcard/Podcasts library
# ---------------------------------------------------------------------------

# (channel folder, title, duration s, filesize bytes, downloaded_at, short?)
FIXTURE_EPISODES = [
    (
        "AI_News_NateBJones",
        "You're Not Good at Predicting Things (And That's the Point)",
        947,
        15_402_112,
        "2026-07-24T07:12:44",
        False,
    ),
    (
        "AI_News_NateBJones",
        "The AI Capex Story Nobody Wants to Tell",
        1322,
        21_284_864,
        "2026-07-23T07:09:03",
        False,
    ),
    (
        "AI_News_NateBJones",
        "Three Minutes on Model Pricing",
        44,
        1_207_296,
        "2026-07-22T07:11:20",
        True,
    ),
    (
        "General",
        "Why Agents Keep Failing in Production",
        2733,
        43_991_040,
        "2026-07-21T19:40:02",
        False,
    ),
    (
        "General",
        "A Long Walk Through Retrieval",
        4519,
        72_351_744,
        "2026-07-19T08:02:55",
        False,
    ),
]

FIXTURE_CHANNELS = [
    {
        "name": "Nate B Jones",
        "url": "https://www.youtube.com/@natebjones",
        "folder": "AI_News_NateBJones",
        "added": "2026-06-02T21:14:07",
    },
    {
        "name": "Latent Space",
        "url": "https://www.youtube.com/@LatentSpacePod",
        "folder": "LatentSpacePod",
        "added": "2026-07-11T09:31:55",
    },
]


def _safe_stem(title: str) -> str:
    """Roughly what the downloader does to a title before using it as a name."""
    stem = re.sub(r'[\\/:*?"<>|]+', "", title).strip()
    return stem[:80] or "episode"


def _thumbnail(path: Path, title: str, channel: str, seed: int) -> None:
    """A 16:9 JPEG that looks like cover art, so the app has something to load.

    Everything meaningful is kept in the middle: the Downloads list crops the
    artwork to a square avatar and the now-playing bar crops it again, so art
    with content near the edges would be judging the fixture, not the app.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:  # pragma: no cover
        raise SystemExit("Pillow is required to build the screenshot fixture library")

    width, height = 640, 360
    top = ((seed * 53) % 120 + 55, (seed * 97) % 100 + 40, (seed * 31) % 110 + 105)
    bottom = (max(top[0] - 45, 12), max(top[1] - 35, 12), max(top[2] - 55, 30))
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / (height - 1)
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )

    initials = "".join(word[0] for word in re.split(r"[\s_]+", channel) if word)[:3].upper()
    font = None
    try:
        import kivymd

        roboto = Path(kivymd.__file__).parent / "fonts" / "Roboto-Bold.ttf"
        if roboto.exists():
            font = ImageFont.truetype(str(roboto), 130)
    except Exception:
        font = None
    if font is None:
        try:
            font = ImageFont.load_default(size=130)
        except TypeError:  # pragma: no cover - very old Pillow
            font = ImageFont.load_default()

    left, upper, right, lower = draw.textbbox((0, 0), initials, font=font)
    draw.text(
        ((width - (right - left)) / 2 - left, (height - (lower - upper)) / 2 - upper),
        initials,
        fill=(255, 255, 255),
        font=font,
    )
    img.save(path, "JPEG", quality=88)


def build_fixture_library(root: Path) -> tuple[Path, Path]:
    """Create a believable ``Podcasts`` tree plus the app's data directory.

    Returns ``(podcasts_dir, app_data_dir)``.  The manifest written here uses
    the exact schema ``download_audio.download_audio()`` produces, so this stays
    honest about what the app reads.
    """
    podcasts = root / "Podcasts"
    data_dir = root / "appdata"
    podcasts.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    manifests: dict[str, list] = {}
    for index, (channel, title, duration, filesize, when, is_short) in enumerate(
        FIXTURE_EPISODES
    ):
        folder = podcasts / channel
        target_dir = folder / "Shorts" if is_short else folder
        target_dir.mkdir(parents=True, exist_ok=True)

        stem = _safe_stem(title)
        audio = target_dir / f"{stem}.m4a"
        thumb = target_dir / f"{stem}.jpg"
        # Placeholder bytes: nothing ever decodes these.  The size shown in the
        # UI comes from the manifest, exactly as it does on the phone.
        audio.write_bytes(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 2048)
        _thumbnail(thumb, title, channel, index + 3)

        rel = f"Shorts/{stem}" if is_short else stem
        manifests.setdefault(channel, []).append(
            {
                "id": f"SCRNSHOT{index:03d}",
                "title": title,
                "description": "",
                "upload_date": when[:10].replace("-", ""),
                "duration": duration,
                "filename": f"{stem}.m4a",
                "relpath": f"{rel}.m4a",
                "thumbnail": f"{stem}.jpg",
                "thumbnail_relpath": f"{rel}.jpg",
                "filesize": filesize,
                "is_short": is_short,
                "audio_format": "m4a",
                "downloaded_at": when,
                "published": False,
            }
        )

    for channel, entries in manifests.items():
        (podcasts / channel / ".episodes.json").write_text(json.dumps(entries, indent=2))

    (data_dir / "channels.json").write_text(json.dumps(FIXTURE_CHANNELS, indent=2))
    (data_dir / "settings.json").write_text(
        json.dumps(
            {
                "audio_format": "m4a",
                "default_folder": "General",
                "auto_download_on_share": True,
                "one_tap_termux": True,
            },
            indent=2,
        )
    )
    return podcasts, data_dir


# ---------------------------------------------------------------------------
# 2.  Booting the app with a real GL context
# ---------------------------------------------------------------------------


def setup_render_env(width: int, height: int, density: float) -> None:
    """Configure Kivy for real rendering.  Must run before ``import kivy``."""
    if TESTS_DIR.as_posix() not in sys.path:
        sys.path.insert(0, TESTS_DIR.as_posix())
    import harness  # noqa: E402  (import after sys.path is fixed)

    harness.setup_kivy_env()

    # The harness pins the mock GL backend, which is what makes shaders "fail
    # to compile" and screenshots come out as noise.  Rendering needs the real
    # one.  setup_kivy_env() is idempotent, so this override sticks.
    os.environ["KIVY_GL_BACKEND"] = os.environ.get("YTP_SCREENSHOT_GL", "gl")
    os.environ["KIVY_METRICS_DENSITY"] = str(density)
    # Set by the harness only when DISPLAY is missing; we have one.
    os.environ.pop("SDL_VIDEODRIVER", None)
    os.environ.pop("KIVY_WINDOW", None)
    if not os.environ.get("HARNESS_KIVY_LOGS"):
        os.environ["KIVY_LOG_LEVEL"] = "error"

    from kivy.config import Config

    Config.set("graphics", "width", str(width))
    Config.set("graphics", "height", str(height))
    Config.set("graphics", "resizable", "0")
    Config.set("graphics", "borderless", "1")
    Config.set("kivy", "exit_on_escape", "0")


class Renderer:
    """Draws frames and captures the framebuffer."""

    def __init__(self, window):
        self.window = window

    def _tick(self):
        from kivy.clock import Clock
        from kivy.lang import Builder

        Clock.tick()
        Builder.sync()
        Clock.tick_draw()
        Builder.sync()

    def frame(self):
        """One complete frame: layout, draw, swap."""
        self._tick()
        self.window.canvas.ask_update()
        self.window.dispatch("on_draw")
        self.window.dispatch("on_flip")

    def settle(self, frames: int = 4):
        for _ in range(frames):
            self.frame()

    def capture(self, path: Path) -> Path:
        """Render and write a PNG at ``path``.

        The last draw is deliberately *not* followed by a flip: after a buffer
        swap the back buffer contents are undefined, and ``glReadPixels`` reads
        the back buffer.  Drawing and then reading without swapping is what
        guarantees the PNG holds the frame we just composed.
        """
        self.settle()
        self._tick()
        self.window.canvas.ask_update()
        self.window.dispatch("on_draw")

        path.parent.mkdir(parents=True, exist_ok=True)
        # Kivy always splices a 4-digit counter into the filename, so capture to
        # a scratch directory and move the result to the name we actually want.
        scratch = Path(tempfile.mkdtemp(prefix="ytp-shot-"))
        try:
            produced = self.window.screenshot(name=str(scratch / "frame.png"))
            if not produced or not os.path.exists(produced):
                raise RuntimeError(f"Window.screenshot() produced nothing for {path.name}")
            shutil.move(produced, path)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return path


def verify_png(path: Path) -> str:
    """Fail loudly on a screenshot that is blank or noise.

    A rendered UI has large flat regions (the background) and a modest palette.
    A framebuffer that was never drawn into is either uniform or effectively
    random, and both are worse than no screenshot at all.
    """
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        pixels = img.width * img.height
        colours = img.getcolors(maxcolors=pixels) or []
        if not colours:
            raise AssertionError(f"{path.name}: could not read colours")
        colours.sort(reverse=True)
        top_count, top_colour = colours[0]
        share = top_count / pixels
        distinct = len(colours)

    if share > 0.995:
        raise AssertionError(
            f"{path.name}: the whole frame is one colour {top_colour} -- nothing rendered"
        )
    if share < 0.05:
        raise AssertionError(
            f"{path.name}: no dominant background colour (largest run {share:.1%} of "
            f"{distinct} distinct colours) -- this looks like noise, not a UI"
        )
    return f"{img.width}x{img.height}, {distinct} colours, background {share:.0%}"


# ---------------------------------------------------------------------------
# 3.  The shots
# ---------------------------------------------------------------------------


class Session:
    """A booted app plus everything needed to drive and photograph it."""

    def __init__(self, app, module, window, renderer, data_dir):
        self.app = app
        self.module = module
        self.window = window
        self.renderer = renderer
        self.data_dir = data_dir

    def switch_tab(self, name: str):
        import harness

        self.app.root.ids.nav.switch_tab(name)
        harness.pump_clock(1.0)
        self.renderer.settle()

    def pump(self, seconds: float = 1.0):
        import harness

        harness.pump_clock(seconds)
        self.renderer.settle()


def shot_downloads(session: Session, out: Path) -> Path:
    session.switch_tab("downloads")
    return session.renderer.capture(out)


def shot_now_playing(session: Session, out: Path) -> Path:
    session.switch_tab("downloads")
    episodes = session.app._episodes
    if not episodes:
        raise RuntimeError("no episodes in the fixture library -- cannot show now playing")
    # The one with artwork, so the shot actually proves artwork works.
    episode = next((e for e in episodes if e.get("thumb")), episodes[0])
    session.app.select_episode(episode)
    session.pump(1.5)
    return session.renderer.capture(out)


def shot_add(session: Session, out: Path) -> Path:
    session.switch_tab("add")
    return session.renderer.capture(out)


def shot_add_channel_form(session: Session, out: Path) -> Path:
    session.switch_tab("add")
    session.app.show_add_channel()
    session.app.root.ids.channel_url_input.text = "youtube.com/@LatentSpacePod"
    session.app.root.ids.channel_name_input.text = "Latent Space"
    session.pump(0.6)
    path = session.renderer.capture(out)
    session.app.hide_add_channel()
    session.pump(0.4)
    return path


def shot_settings(session: Session, out: Path) -> Path:
    session.switch_tab("settings")
    return session.renderer.capture(out)


# Order matters and is deliberate: starting playback writes into the shared
# status line that the Add tab displays, so the quiet tabs are photographed
# first and the now-playing shot comes last.
SHOTS = {
    "downloads": ("01-downloads.png", shot_downloads),
    "add": ("02-add.png", shot_add),
    "add-channel": ("03-add-channel-form.png", shot_add_channel_form),
    "settings": ("04-settings.png", shot_settings),
    "now-playing": ("05-downloads-now-playing.png", shot_now_playing),
}


# ---------------------------------------------------------------------------
# 4.  Driver
# ---------------------------------------------------------------------------


def boot(app_main: Path, podcasts: Path, data_dir: Path, renderer_cls=Renderer) -> Session:
    """Load ``app/main.py`` and bring it up with the fixture library attached."""
    import harness

    # Must be installed before the app module is imported: get_data_dir() reads
    # android.storage.app_storage_path() at build() time.
    harness.install_android_fakes(str(data_dir))

    window = harness.install_window()
    if harness.WINDOW_KIND != "sdl2":
        raise RuntimeError(
            "Kivy fell back to the GL-free mock window -- there is no usable X "
            "display, so nothing can be rendered."
        )

    module = harness.load_app_module(app_main)
    # The app picks its library from the first existing candidate directory at
    # __init__ time; point it at the fixture instead of a real /sdcard.
    module.PODCAST_DIR_CANDIDATES = (str(podcasts),)

    app_cls = harness.get_app_class(module)
    app = app_cls()
    root = app.build()
    app.root = root
    # App.run() is what normally parents the root widget to the Window.  Without
    # it the widget tree exists but is never drawn.
    window.add_widget(root)

    app.on_start()
    harness.pump_clock(3.0)

    renderer = renderer_cls(window)
    renderer.settle(6)
    return Session(app, module, window, renderer, data_dir)


def describe_gl() -> str:
    from kivy.graphics.opengl import (
        GL_RENDERER,
        GL_SHADING_LANGUAGE_VERSION,
        GL_VERSION,
        glGetString,
    )

    def _s(value):
        return value.decode() if isinstance(value, bytes) else str(value)

    return (
        f"backend={os.environ.get('KIVY_GL_BACKEND')} "
        f"renderer={_s(glGetString(GL_RENDERER))} "
        f"gl={_s(glGetString(GL_VERSION))} glsl={_s(glGetString(GL_SHADING_LANGUAGE_VERSION))}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--out-dir", default=str(REPO_ROOT / "screenshots"), help="where to write the PNGs"
    )
    parser.add_argument(
        "--shot",
        action="append",
        dest="shots",
        choices=sorted(SHOTS),
        help="render only this shot (repeatable); default is all of them",
    )
    parser.add_argument("--list", action="store_true", help="list the shot names and exit")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--density", type=float, default=DEFAULT_DENSITY)
    parser.add_argument(
        "--app",
        default=str(REPO_ROOT / "app" / "main.py"),
        help="the app entrypoint to render (default: app/main.py)",
    )
    parser.add_argument(
        "--library",
        default=None,
        help="use this directory for the fake library instead of a temp one",
    )
    parser.add_argument(
        "--keep-library", action="store_true", help="do not delete the fake library afterwards"
    )
    args = parser.parse_args(argv)

    if args.list:
        for name, (filename, _fn) in SHOTS.items():
            print(f"{name:<12} -> {filename}")
        return 0

    if not os.environ.get("DISPLAY"):
        if os.environ.get("YTP_SCREENSHOT_XVFB"):
            print("xvfb-run started but DISPLAY is still unset", file=sys.stderr)
            return 1
        return _reexec_under_xvfb(args.width, args.height).returncode

    app_main = Path(args.app).resolve()
    if not app_main.exists():
        print(f"no such app entrypoint: {app_main}", file=sys.stderr)
        return 1

    library_root = Path(args.library).resolve() if args.library else Path(
        tempfile.mkdtemp(prefix="ytp-library-")
    )
    keep = args.keep_library or bool(args.library)

    out_dir = Path(args.out_dir).resolve()
    started = time.time()
    failures: list[str] = []

    try:
        # Before anything else: nothing may import kivy until KIVY_NO_ARGS and
        # the GL backend are in the environment.
        setup_render_env(args.width, args.height, args.density)
        podcasts, data_dir = build_fixture_library(library_root)
        session = boot(app_main, podcasts, data_dir)

        print(describe_gl())
        print(f"window {tuple(session.window.size)} px @ density {args.density}")
        print(f"library {podcasts}")
        print(
            f"loaded {len(session.app._episodes)} episodes -> "
            f"{session.app.library_summary!r}"
        )
        if not session.app._episodes:
            failures.append("the app found no episodes in the fixture library")
        print()

        wanted = args.shots or list(SHOTS)
        for name in wanted:
            filename, fn = SHOTS[name]
            target = out_dir / filename
            try:
                fn(session, target)
                detail = verify_png(target)
                print(f"  ok    {filename:<32} {detail}")
            except Exception as exc:
                failures.append(f"{name}: {exc}")
                print(f"  FAIL  {filename:<32} {exc}")

        crash_log = Path(data_dir) / "crash_log.txt"
        if crash_log.exists() and crash_log.stat().st_size:
            print(f"\nthe app logged crashes while rendering -> {crash_log}")
            print("  " + "\n  ".join(crash_log.read_text().strip().splitlines()[-12:]))
    finally:
        if not keep:
            shutil.rmtree(library_root, ignore_errors=True)
        elif library_root.exists():
            print(f"\nfake library kept at {library_root}")

    print(f"\n{len(SHOTS if not args.shots else args.shots)} shot(s) in {time.time() - started:.1f}s")
    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print(f"screenshots in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
