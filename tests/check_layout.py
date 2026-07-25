#!/usr/bin/env python3
"""Fail if any control is drawn outside the screen.

This needs a *real* GL context (the mock backend draws nothing and lays
nothing out), so it cannot live in the pytest run, which forces
``KIVY_GL_BACKEND=mock``.  It reuses screenshot.py's boot path instead and is
driven by tests/run_all.sh as its own step.

What it caught the first time: KivyMD 1.2.0's ``MDSwitch`` draws its thumb
dp(20) past its own right edge when the switch is on, so both Settings
switches ran off the screen -- on the test window *and* on the real phone.

Checked at more than one geometry on purpose: a control can fit at one width
and clip at another, and the phone this app targets is neither of the sizes a
developer happens to render at.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

# (width, height, density).  The second is the reporting user's Motorola Edge
# 60, so "fits on my test window" can never be mistaken for "fits on a phone".
GEOMETRIES = [
    (720, 1560, 2.0),
    (1080, 2400, 2.75),
]

# Widgets a user actually touches or reads.  A blanket "nothing may leave the
# window" check is useless here: KivyMD parks unused internal labels far off
# screen by design, and flagging those would train everyone to ignore this.
#
# MDLabel is deliberately absent.  KivyMD list items and captions routinely
# give the label widget more width than the glyphs need and let ``text_size``
# ellipsise inside it, so a label wider than the screen is normal and says
# nothing about what the user sees.  Truncated text is a legibility question
# for the screenshots, not a clipping bug.
CHECKED_TYPES = (
    "MDSwitch",
    "Thumb",
    "MDRaisedButton",
    "MDFlatButton",
    "MDIconButton",
    "MDTextField",
    "MDSlider",
)


def _is_exempt(widget) -> bool:
    return False


def _describe(widget) -> str:
    name = type(widget).__name__
    text = getattr(widget, "text", "") or ""
    if text:
        text = f" {text[:32]!r}"
    return f"{name}{text}"


def check_screen(session, tab: str, width: int, tolerance: float) -> list[str]:
    """Return a human-readable problem for every control off the screen."""
    session.switch_tab(tab)
    problems = []
    for widget in session.app.root.walk():
        if type(widget).__name__ not in CHECKED_TYPES or _is_exempt(widget):
            continue
        if not widget.width or not widget.height:
            continue
        if widget.right > width + tolerance:
            problems.append(
                f"[{tab}] {_describe(widget)} extends to x={widget.right:.0f} "
                f"on a {width}px screen (over by {widget.right - width:.0f}px)"
            )
        elif widget.x < -tolerance:
            problems.append(
                f"[{tab}] {_describe(widget)} starts at x={widget.x:.0f}, "
                f"off the left edge"
            )
    return problems


def run(width: int, height: int, density: float, tolerance: float) -> list[str]:
    import screenshot as S

    tmp = Path(tempfile.mkdtemp(prefix="ytp-layout-"))
    S.setup_render_env(width, height, density)
    podcasts, _ = S.build_fixture_library(tmp / "Podcasts")
    # YT_APP_MAIN mirrors tests/harness.py, so this check can be pointed at a
    # copy of the app to confirm it still fails on a build that has the bug.
    app_main = Path(os.environ.get("YT_APP_MAIN") or (REPO_ROOT / "app" / "main.py"))
    session = S.boot(app_main, podcasts, tmp / "data")

    problems = []
    for tab in ("downloads", "add", "settings"):
        problems.extend(check_screen(session, tab, width, tolerance))
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1.0,
        help="pixels of overhang to forgive (default: 1, for rounding)",
    )
    args = parser.parse_args(argv)

    if not os.environ.get("DISPLAY"):
        # Re-exec under a virtual display tall enough for the largest geometry;
        # a window taller than the X screen lays out against the screen, not
        # the window, and every measurement below it becomes fiction.
        if os.environ.get("YTP_LAYOUT_XVFB"):
            print("xvfb-run started but DISPLAY is still unset", file=sys.stderr)
            return 1
        w = max(g[0] for g in GEOMETRIES)
        h = max(g[1] for g in GEOMETRIES)
        env = dict(os.environ, YTP_LAYOUT_XVFB="1")
        return subprocess.run(
            ["xvfb-run", "-a", "-s", f"-screen 0 {w + 100}x{h + 100}x24",
             sys.executable, __file__, *(argv or sys.argv[1:])],
            env=env,
        ).returncode

    failures = 0
    for width, height, density in GEOMETRIES:
        # Each geometry needs a fresh interpreter: Kivy's Window and metrics
        # are process-global and cannot be resized between runs reliably.
        if os.environ.get("YTP_LAYOUT_GEOMETRY"):
            continue
        result = subprocess.run(
            [sys.executable, __file__, "--tolerance", str(args.tolerance)],
            env=dict(
                os.environ,
                YTP_LAYOUT_GEOMETRY=f"{width}x{height}@{density}",
            ),
            capture_output=True,
            text=True,
        )
        sys.stdout.write(result.stdout)
        if result.stderr.strip():
            sys.stderr.write(result.stderr)
        if result.returncode != 0:
            failures += 1

    if os.environ.get("YTP_LAYOUT_GEOMETRY"):
        spec = os.environ["YTP_LAYOUT_GEOMETRY"]
        size, _, density = spec.partition("@")
        width, height = (int(v) for v in size.split("x"))
        problems = run(width, height, float(density), args.tolerance)
        label = f"{width}x{height} @ {density}"
        if problems:
            print(f"  FAIL  {label}")
            for problem in problems:
                print(f"          {problem}")
            return 1
        print(f"  ok    {label}  nothing clipped")
        return 0

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
