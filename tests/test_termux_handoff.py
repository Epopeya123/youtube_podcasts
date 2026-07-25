"""What happens when the user presses DOWNLOAD.

Issue #3: pasting a link and pressing the app's own DOWNLOAD button opened the
Android share sheet.  The app cannot download by itself (Termux owns yt-dlp and
ffmpeg), but Termux's RUN_COMMAND service starts the download with no chooser
at all -- so that is the default now, and the chooser only survives as a
fallback for when Termux cannot be reached.

These tests assert on *behaviour*, not on "did not raise": the recording fakes
in tests/harness.py capture every Intent, extra, cast and start* call, so
"pressing DOWNLOAD downloads" and "the chooser only appears as a fallback" are
both checkable on a laptop.

Issue #6 is covered here too: no user-facing string may contain a filesystem
path.  "/storage/emulated/0/Podcasts" and "Internal storage > Podcasts" are the
same folder, and showing the raw path made the user think it had moved.
"""

from __future__ import annotations

import ast
import configparser
import re
import sys
import time

import pytest

import harness

APP_MAIN = harness.APP_MAIN
REPO_ROOT = harness.REPO_ROOT
# The build config always comes from the repo, even when YT_APP_MAIN points the
# behavioural tests at a copy of the app.
APP_DIR = REPO_ROOT / "app"

VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
RUN_COMMAND_ACTION = "com.termux.RUN_COMMAND"
ACTION_SEND = "android.intent.action.SEND"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def android_storage(tmp_path_factory):
    path = tmp_path_factory.mktemp("termux_handoff_storage")
    harness.setup(with_android=True)
    sys.modules["android.storage"].app_storage_path = lambda: str(path)
    return path


@pytest.fixture(scope="session")
def app_module(android_storage):
    return harness.load_app_module()


@pytest.fixture(scope="session")
def app(app_module):
    with harness.running_app(app_module) as instance:
        yield instance


class StubPackageManager:
    """``android.content.pm.PackageManager`` stand-in.

    Only the two queries the app makes: whether Termux's RunCommandService
    resolves, and whether the package exists at all.  A real device answers
    "no" to both when the manifest has no <queries> element for com.termux.
    """

    def __init__(self, service: bool = True, installed: bool = True):
        self.service = service
        self.installed = installed
        self.resolve_calls: list[tuple] = []
        self.package_queries: list[str] = []

    def resolveService(self, intent, flags=0):  # noqa: N802 - Java name
        self.resolve_calls.append((intent, flags))
        if not self.service:
            return None  # pyjnius turns a Java null into None
        return harness.FakeJavaObject("android.content.pm.ResolveInfo")

    def getPackageInfo(self, name, flags=0):  # noqa: N802 - Java name
        self.package_queries.append(str(name))
        if not self.installed:
            import jnius

            raise jnius.JavaException("android.content.pm.PackageManager$NameNotFoundException")
        return harness.FakeJavaObject("android.content.pm.PackageInfo")


@pytest.fixture
def termux(app):
    """Decide what this phone will report about Termux, then clean up."""
    PythonActivity = harness.fake_autoclass("org.kivy.android.PythonActivity")
    activity = PythonActivity.mActivity

    def install(service: bool = True, installed: bool = True) -> StubPackageManager:
        manager = StubPackageManager(service=service, installed=installed)
        activity.getPackageManager = lambda: manager
        harness.recorder.reset()
        return manager

    yield install

    from kivy.clock import Clock

    activity.__dict__.pop("getPackageManager", None)
    with_hint = getattr(app, "_termux_silent_hint", None)
    if with_hint is not None:
        Clock.unschedule(with_hint)
    app._termux_handoff_at = None
    app.termux_direct_launch = True
    app.status_text = "Ready"


# ---------------------------------------------------------------------------
# Helpers over the recorder
# ---------------------------------------------------------------------------


def service_starts() -> list:
    return [c for c in harness.recorder.calls if c[1] in ("startForegroundService", "startService")]


def chooser_calls() -> list:
    return harness.recorder.method_calls("createChooser")


def send_intents() -> list:
    return [i for i in harness.recorder.intents if i.getAction() == ACTION_SEND]


def extra_values() -> list[str]:
    """Every putExtra value seen, flattened to strings (String[] included)."""
    values: list[str] = []
    for value in harness.recorder.extras().values():
        if isinstance(value, (list, tuple)):
            values.extend(str(v) for v in value)
        else:
            values.append(str(value))
    return values


def class_names() -> list[tuple[str, ...]]:
    return [tuple(str(a) for a in args) for (_t, m, args, _k) in harness.recorder.calls
            if m == "setClassName"]


def press_download(app, url: str = VIDEO_URL) -> None:
    app.root.ids.url_input.text = url
    app.download_single_video()


# ---------------------------------------------------------------------------
# 1. Pressing DOWNLOAD downloads
# ---------------------------------------------------------------------------


def test_download_goes_straight_to_termux_with_no_chooser(app, termux):
    """Issue #3: the app's own DOWNLOAD button must not ask which app to use."""
    termux(service=True)

    press_download(app)

    assert service_starts(), "DOWNLOAD did not start Termux's RUN_COMMAND service"
    assert not chooser_calls(), "DOWNLOAD opened a share chooser instead of downloading"
    assert not send_intents(), "DOWNLOAD built an ACTION_SEND intent instead of downloading"
    kinds = {kind for (kind, _intent) in harness.recorder.activity_starts}
    assert "startActivity" not in kinds, f"an activity was launched: {kinds}"


def test_direct_launch_is_on_by_default(app_module):
    """A fresh install downloads directly; the chooser is opt-in, not opt-out."""
    assert app_module.DEFAULT_SETTINGS.get("termux_direct_launch") is True


def test_old_settings_file_cannot_keep_the_chooser(app, app_module, tmp_path):
    """v3.0.0 saved one_tap_termux=false on every phone that ran it.

    The fix has to survive an upgrade, so the setting is a *new* key -- a
    changed default would never have reached the phone that reported the bug.
    """
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"one_tap_termux": false, "audio_format": "m4a"}')
    original = app_module.SETTINGS_FILE
    app_module.SETTINGS_FILE = str(settings_file)
    try:
        app._load_settings()
        assert app.termux_direct_launch is True, (
            "an upgraded phone is still stuck on the share-sheet behaviour"
        )
    finally:
        app_module.SETTINGS_FILE = original
        app._load_settings()


def test_run_command_intent_carries_the_payload_termux_expects(app, termux):
    """<url>|||<folder>|||<format> handed to ~/bin/termux-url-opener."""
    termux(service=True)

    press_download(app)

    values = extra_values()
    expected = f"{VIDEO_URL}|||{app._settings.get('default_folder', 'General')}|||" \
               f"{app._settings.get('audio_format', 'm4a')}"
    assert expected in values, f"payload missing from the intent extras: {values}"
    assert app_url_opener(app) in values, "RUN_COMMAND_PATH is not termux-url-opener"

    started = [i for (_k, i) in harness.recorder.activity_starts]
    actions = {i.getAction() for i in harness.recorder.intents}
    assert RUN_COMMAND_ACTION in actions
    assert ("com.termux", "com.termux.app.RunCommandService") in class_names()
    assert started or service_starts()


def app_url_opener(app) -> str:
    module = sys.modules[type(app).__module__]
    return module.TERMUX_URL_OPENER


def test_refresh_channel_also_skips_the_chooser(app, termux):
    termux(service=True)

    app.refresh_channel("https://www.youtube.com/@natebjones", "AI_News")

    assert service_starts(), "refreshing a channel did not reach Termux directly"
    assert not chooser_calls()
    assert any("REFRESH:https://www.youtube.com/@natebjones|||AI_News|||" in v
               for v in extra_values()), extra_values()


def test_background_extra_is_a_boolean_not_a_string(app, termux):
    """Termux documents RUN_COMMAND_BACKGROUND as a boolean.

    Sent as a String it is the wrong extra type entirely: getBooleanExtra()
    finds no boolean under that key and returns its default, so the value we
    thought we set is simply discarded.
    """
    termux(service=True)
    press_download(app)

    extras = {}
    for _t, method, args, _k in harness.recorder.calls:
        if method == "putExtra":
            extras[str(args[0])] = args[1]

    value = extras.get("com.termux.RUN_COMMAND_BACKGROUND")
    assert value is not None, "the background extra was not sent at all"
    assert isinstance(value, bool), (
        f"RUN_COMMAND_BACKGROUND must be a bool, got {type(value).__name__}"
    )
    assert value is False, "the download must run in a visible session, not silently"


def test_session_action_stays_a_string(app, termux):
    """The neighbouring extra genuinely IS a String -- do not 'fix' it too."""
    termux(service=True)
    press_download(app)

    for _t, method, args, _k in harness.recorder.calls:
        if method == "putExtra" and str(args[0]) == "com.termux.RUN_COMMAND_SESSION_ACTION":
            assert isinstance(args[1], harness.FakeJavaObject)
            return
    pytest.fail("RUN_COMMAND_SESSION_ACTION was never sent")


def test_start_service_is_preferred_over_start_foreground_service(app, termux):
    """Termux documents startService() for RUN_COMMAND.

    startForegroundService() promises Android that the service will call
    startForeground() promptly; when it does not, Android 12+ treats that as a
    violation.  We are always called straight from a button press, so the app
    is in the foreground and plain startService() is allowed.
    """
    termux(service=True)
    press_download(app)

    starts = [c[1] for c in service_starts()]
    assert starts, "no service was started at all"
    assert starts[0] == "startService", (
        f"first attempt was {starts[0]}, but Termux documents startService"
    )


def test_java_types_on_the_direct_path(app, termux):
    """CLAUDE.md: every Java String argument must be a real java.lang.String."""
    termux(service=True)

    press_download(app)

    assert "java.lang.String" in harness.recorder.autoclass_names
    for _target, method, args, _kwargs in harness.recorder.calls:
        if method != "putExtra":
            continue
        key, value = args
        assert isinstance(key, harness.FakeJavaObject), f"raw str key {key!r} passed to putExtra"
        if isinstance(value, bool):
            # A boolean extra must stay a Python bool: pyjnius maps it onto
            # Java's boolean overload directly.  Wrapping it in a
            # java.lang.String would make putExtra store a *String*, and the
            # receiver's getBooleanExtra() would silently fall back to its
            # default -- which is exactly the bug this rule is meant to catch,
            # only in the other direction.
            continue
        if isinstance(value, (list, tuple)):
            assert all(isinstance(v, harness.FakeJavaObject) for v in value), (
                f"String[] extra contains a raw Python str: {value!r}"
            )
        else:
            assert isinstance(value, harness.FakeJavaObject), (
                f"raw str value {value!r} passed to putExtra"
            )


# ---------------------------------------------------------------------------
# 2. The chooser, strictly as a fallback
# ---------------------------------------------------------------------------


def test_chooser_when_the_service_does_not_resolve(app, termux):
    """Termux installed but the service is unreachable (old build, disabled).

    startForegroundService() would "succeed" and nothing would happen, so the
    package manager is asked first and the chooser takes over.
    """
    manager = termux(service=False, installed=True)

    press_download(app)

    assert manager.resolve_calls, "the service was never probed before being used"
    assert not service_starts(), "a service that does not resolve was started anyway"
    assert chooser_calls(), "no chooser -- the user got nothing at all"
    assert send_intents(), "the fallback did not build an ACTION_SEND intent"
    assert any(VIDEO_URL in v for v in extra_values())


def test_chooser_when_termux_is_not_installed(app, termux):
    """Nothing to start: the user still gets a chooser and an explanation."""
    manager = termux(service=False, installed=False)

    press_download(app)

    assert manager.package_queries == ["com.termux"]
    assert chooser_calls(), "no Termux and no chooser: a dead end"
    assert app.status_text.strip(), "the app went quiet"
    assert "not installed" in app.status_text.lower(), (
        f"the chooser appeared with no explanation: {app.status_text!r}"
    )


def test_fallback_explains_why_a_chooser_appeared(app, termux):
    termux(service=False, installed=True)

    press_download(app)

    status = app.status_text.lower()
    assert "termux" in status
    assert "list" in status or "menu" in status, app.status_text


def test_direct_hand_off_does_not_mention_the_chooser(app, termux):
    termux(service=True)

    press_download(app)

    assert "downloading" in app.status_text.lower(), app.status_text
    assert "pick" not in app.status_text.lower(), app.status_text


def test_switch_off_uses_the_chooser_without_probing(app, termux):
    """The setting still works: off means the share menu, as before."""
    manager = termux(service=True)
    app.termux_direct_launch = False

    press_download(app)

    assert not manager.resolve_calls
    assert not service_starts()
    assert chooser_calls()


def test_chooser_title_is_cast_to_charsequence(app, termux):
    termux(service=False, installed=True)

    press_download(app)

    assert any(sig == "java.lang.CharSequence" for sig, _obj in harness.recorder.casts), (
        "Intent.createChooser title was not cast to java.lang.CharSequence"
    )


def test_both_routes_failing_still_says_something(app, termux):
    """Neither route works (no Android, no chooser): never fail silently."""
    termux(service=True)
    PythonActivity = harness.fake_autoclass("org.kivy.android.PythonActivity")
    activity = PythonActivity.mActivity

    def boom(*_args, **_kwargs):
        raise RuntimeError("no such service")

    for name in ("startForegroundService", "startService", "startActivity"):
        setattr(activity, name, boom)
    try:
        press_download(app)
    finally:
        for name in ("startForegroundService", "startService", "startActivity"):
            activity.__dict__.pop(name, None)

    status = app.status_text.lower()
    assert "termux" in status, app.status_text
    assert "could not" in status or "install" in status, app.status_text


# ---------------------------------------------------------------------------
# 3. The failure Android cannot report: Termux accepts and ignores
# ---------------------------------------------------------------------------


def test_a_silent_termux_is_reported_to_the_user(app, termux):
    """`allow-external-apps` unset: the intent is accepted and dropped.

    Nothing comes back -- no exception, no callback -- so the app waits, and
    since it was never pushed into the background (Termux never opened), it
    tells the user which switch to flip.
    """
    termux(service=True)

    press_download(app)
    handed_off = app.status_text
    harness.pump_clock(sys.modules[type(app).__module__].TERMUX_SILENT_AFTER + 2.0)

    assert app.status_text != handed_off, "the app waited forever with no explanation"
    assert "settings" in app.status_text.lower(), app.status_text


def test_no_hint_when_termux_actually_took_the_screen(app, termux):
    """Termux opening pauses this app, which freezes Kivy's clock.

    A callback that fires far later than it was scheduled for means exactly
    that -- so the "nothing happened" hint must not fire.
    """
    termux(service=True)
    module = sys.modules[type(app).__module__]

    press_download(app)
    handed_off = app.status_text
    # As if the phone had been on Termux for a minute before coming back.
    app._termux_handoff_at = time.monotonic() - (module.TERMUX_SILENT_AFTER * 10)
    app._termux_silent_hint()

    assert app.status_text == handed_off, "the hint fired even though Termux opened"


def test_the_hint_is_armed_only_for_the_direct_route(app, termux):
    termux(service=False, installed=True)

    press_download(app)

    assert app._termux_handoff_at is None, "the chooser route armed the silent-Termux timer"


# ---------------------------------------------------------------------------
# 4. Settings: terse, and no filesystem paths anywhere (issues #4 and #6)
# ---------------------------------------------------------------------------

PATH_RE = re.compile(r"/sdcard|/storage/emulated|/data/data|~/storage|/Podcasts/")


@pytest.fixture(scope="session")
def app_source() -> str:
    return APP_MAIN.read_text()


@pytest.fixture(scope="session")
def app_tree(app_source):
    return ast.parse(app_source)


def _strings(node) -> list[str]:
    """Every literal string reachable from an expression node."""
    out: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            out.append(child.value)
    return out


ON_SCREEN_ATTRS = {
    "status_text",
    "storage_help",
    "format_help",
    "termux_direct_help",
    "library_summary",
    "now_playing_status",
    "now_playing_title",
}


def user_facing_strings(tree) -> list[str]:
    """Literals that end up on screen: help/status assignments, snackbars, text=."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            attrs = {t.attr for t in node.targets if isinstance(t, ast.Attribute)}
            if attrs & ON_SCREEN_ATTRS:
                found.extend(_strings(node.value))
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in ("safe_snackbar", "_handoff_status"):
                for arg in node.args:
                    found.extend(_strings(arg))
            for keyword in node.keywords:
                if keyword.arg in ("text", "secondary_text", "hint_text"):
                    found.extend(_strings(keyword.value))
    return found


def test_no_filesystem_path_is_ever_shown_to_the_user(app_tree, app_source):
    """Issue #6: file managers say "Internal storage", not /storage/emulated/0."""
    offenders = [s for s in user_facing_strings(app_tree) if PATH_RE.search(s)]
    for block in harness.get_kv_strings(app_source):
        offenders.extend(
            line.strip() for line in block.splitlines() if PATH_RE.search(line)
        )
    assert not offenders, f"filesystem paths in user-facing text: {offenders}"


def test_storage_help_uses_file_manager_wording(app):
    app._refresh_help_text()

    assert "Internal storage" in app.storage_help, app.storage_help
    assert not PATH_RE.search(app.storage_help), app.storage_help


def test_settings_captions_are_one_short_line(app):
    """Issue #4: the Settings tab was a wall of text."""
    app._refresh_help_text()
    captions = {
        "format_help": app.format_help,
        "storage_help": app.storage_help,
        "termux_direct_help": app.termux_direct_help,
    }
    for name, text in captions.items():
        assert text, f"{name} is empty"
        assert "\n" not in text, f"{name} is multi-line:\n{text}"
        assert len(text) <= 80, f"{name} is {len(text)} chars: {text}"


def test_settings_switch_still_drives_the_setting(app, tmp_path, app_module):
    """Trimming the text must not cost any functionality."""
    settings_file = tmp_path / "settings.json"
    original = app_module.SETTINGS_FILE
    app_module.SETTINGS_FILE = str(settings_file)
    try:
        app.set_termux_direct(False)
        assert app.termux_direct_launch is False
        assert app._settings["termux_direct_launch"] is False
        assert "menu" in app.termux_direct_help.lower(), app.termux_direct_help

        app.set_termux_direct(True)
        assert app.termux_direct_launch is True
        assert app._settings["termux_direct_launch"] is True
    finally:
        app_module.SETTINGS_FILE = original
        app._load_settings()


def test_every_settings_control_is_still_wired(app, app_source):
    """The tab kept all four controls: format, file access, and both switches."""
    kv = "\n".join(harness.get_kv_strings(app_source))
    for handler in (
        "app.set_audio_format",
        "app.request_all_files_access",
        "app.set_auto_download",
        "app.set_termux_direct",
    ):
        assert handler in kv, f"{handler} disappeared from the Settings tab"
    for widget_id in ("auto_share_switch", "termux_direct_switch"):
        assert widget_id in kv, f"{widget_id} disappeared from the Settings tab"


# ---------------------------------------------------------------------------
# 5. The build has to declare what the direct path needs
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def spec() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(APP_DIR / "buildozer.spec")
    return parser


def test_manifest_declares_the_termux_permissions(spec):
    permissions = spec.get("app", "android.permissions")
    assert "com.termux.permission.RUN_COMMAND" in permissions
    assert "FOREGROUND_SERVICE" in permissions


def test_manifest_can_see_the_termux_package(spec):
    """Android 11+ hides every package not named in <queries>.

    Without this the package manager reports Termux as missing, resolveService
    returns null, and the app falls back to the chooser on every download.
    """
    extra = spec.get("app", "android.extra_manifest_xml", fallback="")
    assert extra, "no android.extra_manifest_xml -> no <queries> -> Termux is invisible"
    path = (APP_DIR / extra.lstrip("./")).resolve()
    assert path.exists(), f"{path} is missing"
    text = path.read_text()
    assert "<queries>" in text
    assert "com.termux" in text


def test_extra_manifest_survives_buildozers_quoting():
    """buildozer interpolates the file into a shell command inside double
    quotes with no escaping, so a double quote here would split the argument
    and silently drop the <queries> element."""
    text = (APP_DIR / "extra_manifest.xml").read_text()
    assert '"' not in text, "double quote in extra_manifest.xml breaks buildozer's quoting"
