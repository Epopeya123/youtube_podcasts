"""Headless smoke tests for app/main.py.

These run the *real* app class against the *real* installed KivyMD 1.2.0 /
Kivy 2.3.1 -- no mocking of the UI layer -- so anything that would blow up on
the phone (missing id, KivyMD 2.x widget, bad KV property, unguarded
exception) fails here in seconds instead of after a 30-minute APK build.

Everything is derived from what is actually in app/main.py: ids are discovered
by scanning the source for ``ids.<name>``, handlers by scanning the KV for
``app.<method>(...)``, and methods to exercise by reflecting over the app
class.  Rewriting app/main.py does not require rewriting these tests.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import sys
from pathlib import Path

import pytest

import harness
import lint_kivymd

APP_MAIN = harness.APP_MAIN


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app_source() -> str:
    return APP_MAIN.read_text()


@pytest.fixture(scope="session")
def app_tree(app_source):
    return ast.parse(app_source)


@pytest.fixture(scope="session")
def kv_blocks(app_tree):
    return lint_kivymd.extract_kv_blocks(APP_MAIN, app_tree)


@pytest.fixture(scope="session")
def android_storage(tmp_path_factory):
    """Point the fake ``android.storage.app_storage_path()`` at a temp dir."""
    path = tmp_path_factory.mktemp("android_storage")
    harness.setup(with_android=True)
    sys.modules["android.storage"].app_storage_path = lambda: str(path)
    return path


@pytest.fixture(scope="session")
def app_module(android_storage):
    return harness.load_app_module()


@pytest.fixture(scope="session")
def app(app_module):
    """A built, started app instance shared by the whole session."""
    with harness.running_app(app_module) as instance:
        yield instance


@pytest.fixture(autouse=True)
def clean_recorder():
    harness.recorder.reset()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IDS_ATTR_RE = re.compile(r"\bids\s*\.\s*([A-Za-z_]\w*)")
_IDS_SUB_RE = re.compile(r"""\bids\s*\[\s*['"]([A-Za-z_]\w*)['"]\s*\]""")
_IDS_IN_RE = re.compile(r"""['"]([A-Za-z_]\w*)['"]\s+(?:not\s+)?in\s+\S*\bids\b""")
# app.<name> that is NOT a call -- i.e. a property/attribute read from KV.
_APP_ATTR_RE = re.compile(r"\bapp\.([A-Za-z_]\w*)(?!\w)(?!\s*\()")


def referenced_ids(source: str) -> set[str]:
    """Every KV id the Python code reaches for at runtime."""
    found = set(_IDS_ATTR_RE.findall(source))
    found |= set(_IDS_SUB_RE.findall(source))
    found |= set(_IDS_IN_RE.findall(source))
    return found


def _plausible_value(param_name: str, tmp_path: Path, module):
    name = param_name.lower()
    if "episode" in name:
        return {}
    if "url" in name:
        return "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    if "payload" in name or "target" in name or "command" in name:
        return "https://youtu.be/dQw4w9WgXcQ|||General|||m4a"
    if "folder" in name or "channel" in name:
        return "TestChannel"
    if "fmt" in name or "format" in name:
        return "m4a"
    if "active" in name or "visible" in name or "enabled" in name or name.startswith("is_"):
        return True
    if "path" in name or "file" in name:
        return str(tmp_path / "nonexistent.m4a")
    if "intent" in name:
        return None
    if "text" in name or "title" in name or "name" in name or "msg" in name:
        return "test"
    if "second" in name or "size" in name or "count" in name or "index" in name:
        return 0
    if "widget" in name or "instance" in name:
        return None
    return ""


def own_methods(app_instance):
    """Methods defined by the app's own class(es), not inherited from MDApp."""
    from kivymd.app import MDApp

    out = {}
    for klass in reversed(app_instance.__class__.__mro__):
        if klass in MDApp.__mro__:
            continue
        for name, value in vars(klass).items():
            if isinstance(value, (staticmethod, classmethod)) or inspect.isfunction(value):
                out[name] = getattr(app_instance, name)
    return out


# ---------------------------------------------------------------------------
# 1. The app loads and builds
# ---------------------------------------------------------------------------


def test_app_module_imports(app_module):
    app_cls = harness.get_app_class(app_module)
    assert inspect.isclass(app_cls)


def test_kv_parses_and_build_returns_a_root_widget(app):
    from kivy.uix.widget import Widget

    assert app.root is not None, "build() returned None -- KV failed to load"
    assert isinstance(app.root, Widget)
    assert app.root.ids, "root widget has no ids -- the KV tree did not apply"


def test_kv_block_was_found(kv_blocks):
    assert kv_blocks, "no KV string found in app/main.py -- the test harness needs one"


# ---------------------------------------------------------------------------
# 2. Every id the Python code uses actually exists in the KV tree
# ---------------------------------------------------------------------------


def test_every_referenced_id_exists_in_kv(app, app_source):
    wanted = referenced_ids(app_source)
    assert wanted, "no `ids.<name>` references found -- check the scan regex"
    present = set(app.root.ids.keys())
    missing = sorted(wanted - present)
    assert not missing, (
        f"app/main.py reaches for ids that the KV tree does not define: {missing}. "
        f"Defined ids: {sorted(present)}"
    )


def test_no_orphan_ids_in_kv(app, app_source):
    """Informational: ids declared in KV but never used from Python."""
    present = set(app.root.ids.keys())
    unused = sorted(present - referenced_ids(app_source))
    # Not a failure -- some ids exist purely for KV-internal references.
    print(f"ids declared in KV but unused from Python: {unused}")


# ---------------------------------------------------------------------------
# 3. Everything KV calls on `app` exists with a compatible signature
# ---------------------------------------------------------------------------


def test_kv_bound_methods_exist_with_compatible_signature(app, kv_blocks):
    problems = []
    seen = 0
    for block in kv_blocks:
        for line, method, argc in lint_kivymd.parse_kv_app_calls(block):
            seen += 1
            target = getattr(app, method, None)
            if target is None:
                problems.append(f"{block.path}:{line}: app has no method '{method}'")
                continue
            if not callable(target):
                problems.append(f"{block.path}:{line}: app.{method} is not callable")
                continue
            try:
                inspect.signature(target).bind(*(["x"] * argc))
            except TypeError as exc:
                problems.append(
                    f"{block.path}:{line}: app.{method}() called with {argc} arg(s) "
                    f"in KV but signature is {inspect.signature(target)} ({exc})"
                )
    assert seen, "no `app.method()` handlers found in KV -- check the scan regex"
    assert not problems, "\n".join(problems)


def test_kv_bound_properties_exist(app, kv_blocks):
    problems = []
    for block in kv_blocks:
        for idx, raw in enumerate(block.text.split("\n")):
            line = raw.split("#", 1)[0]
            for name in _APP_ATTR_RE.findall(line):
                if not hasattr(app, name):
                    problems.append(
                        f"{block.path}:{block.line_of(idx)}: KV reads app.{name} "
                        f"but the app class has no such attribute"
                    )
    assert not problems, "\n".join(sorted(set(problems)))


# ---------------------------------------------------------------------------
# 4. Lifecycle + every public method is exception-safe
# ---------------------------------------------------------------------------


def test_lifecycle_hooks_are_safe(app):
    for hook in ("on_start", "on_resume", "on_pause", "on_stop"):
        method = getattr(app, hook, None)
        if method is None:
            continue
        method()  # must not raise
    harness.pump_clock(2.0)


def test_every_public_method_is_exception_guarded(app, app_module, tmp_path):
    """CLAUDE.md audit item 3: 'All methods have try/except'.

    Anything that escapes here would be an unhandled exception inside a Kivy
    event handler on the phone, i.e. a visible crash.
    """
    skip = {"build", "run", "stop", "on_stop"}
    failures = []
    for name, method in sorted(own_methods(app).items()):
        if name.startswith("_") or name in skip:
            continue
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            continue
        args = []
        ok = True
        for param in signature.parameters.values():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            if param.default is not param.empty:
                continue
            if param.name == "self":
                continue
            args.append(_plausible_value(param.name, tmp_path, app_module))
        if not ok:
            continue
        try:
            method(*args)
        except Exception as exc:  # noqa: BLE001 - that is the point of the test
            failures.append(f"app.{name}({', '.join(map(repr, args))}) raised {exc!r}")
    harness.pump_clock(1.0)
    assert not failures, "\n".join(failures)


def test_private_helpers_do_not_crash_the_ui(app, tmp_path):
    """The `_`-prefixed helpers KV/lifecycle code reaches indirectly."""
    safe_private = {"_refresh_help_text", "_load_settings", "_save_settings", "_safe_load"}
    for name in sorted(safe_private):
        method = getattr(app, name, None)
        if method is None:
            continue
        method()


# ---------------------------------------------------------------------------
# 5. The Android/Termux hand-off (asserted through the recording fakes)
# ---------------------------------------------------------------------------


def test_download_single_video_builds_a_valid_share_intent(app):
    if "url_input" not in app.root.ids:
        pytest.skip("no url_input field in this build")
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    app.root.ids.url_input.text = url
    harness.recorder.reset()

    app.download_single_video()

    assert harness.recorder.autoclass_names, "no pyjnius classes were requested"
    assert "android.content.Intent" in harness.recorder.autoclass_names
    assert harness.recorder.activity_starts, "nothing was ever handed to Android"

    sends = [i for i in harness.recorder.intents if i.getAction() == "android.intent.action.SEND"]
    services = [
        c for c in harness.recorder.calls if c[1] in ("startForegroundService", "startService")
    ]
    assert sends or services, "neither ACTION_SEND nor the Termux RUN_COMMAND service was used"

    payloads = [str(v) for v in harness.recorder.extras().values() if not isinstance(v, list)]
    payloads += [str(x) for v in harness.recorder.extras().values() if isinstance(v, list) for x in v]
    assert any(url in p for p in payloads), (
        f"the shared URL never reached an Intent extra; extras were {harness.recorder.extras()}"
    )


def test_share_intent_uses_java_string_and_charsequence_cast(app):
    """CLAUDE.md: all Java String args must be autoclass('java.lang.String'),
    and Intent.createChooser needs cast('java.lang.CharSequence', title)."""
    if "url_input" not in app.root.ids:
        pytest.skip("no url_input field in this build")
    app.one_tap_termux = False if hasattr(app, "one_tap_termux") else None
    app.root.ids.url_input.text = "https://youtu.be/dQw4w9WgXcQ"
    harness.recorder.reset()
    app.download_single_video()

    chooser_calls = [c for c in harness.recorder.calls if c[1] == "createChooser"]
    if not chooser_calls:
        pytest.skip("this build does not use Intent.createChooser")
    assert "java.lang.String" in harness.recorder.autoclass_names, (
        "raw Python str passed to Java; use autoclass('java.lang.String')"
    )
    assert any(sig == "java.lang.CharSequence" for sig, _obj in harness.recorder.casts), (
        "Intent.createChooser title was not cast to java.lang.CharSequence"
    )


def _send_intent(url: str, action: str = "android.intent.action.SEND"):
    intent = harness.FakeIntent()
    intent.setAction(action)
    intent.putExtra("android.intent.extra.TEXT", f"Look at this {url} - great video")
    return intent


def test_shared_link_lands_in_the_url_field(app):
    """A YouTube link shared into the app must be extracted from the caption."""
    process = getattr(app, "_process_intent", None)
    if process is None:
        pytest.skip("this build has no _process_intent")
    if not hasattr(app, "auto_download_on_share"):
        pytest.skip("this build has no auto_download_on_share setting")

    original = app.auto_download_on_share
    app.auto_download_on_share = False
    try:
        url = "https://www.youtube.com/watch?v=SHAREDVID1"
        harness.recorder.reset()
        process(_send_intent(url))
        assert url in app.root.ids.url_input.text, (
            f"shared link was not extracted; url_input is {app.root.ids.url_input.text!r}"
        )
        assert not harness.recorder.activity_starts, (
            "auto-download is off but the app still handed the link to Termux"
        )
    finally:
        app.auto_download_on_share = original


def test_shared_link_auto_downloads_once(app):
    """With auto-download on, a shared link goes straight to Termux -- and a
    repeat of the same intent (Android replays getIntent() after a resume) must
    not download it a second time."""
    process = getattr(app, "_process_intent", None)
    if process is None:
        pytest.skip("this build has no _process_intent")
    if not hasattr(app, "auto_download_on_share"):
        pytest.skip("this build has no auto_download_on_share setting")

    original = app.auto_download_on_share
    app.auto_download_on_share = True
    try:
        url = "https://www.youtube.com/watch?v=SHAREDVID2"
        harness.recorder.reset()
        process(_send_intent(url))
        first = len(harness.recorder.activity_starts) + len(
            harness.recorder.method_calls("startForegroundService")
        )
        assert first, "auto-download is on but nothing was handed to Android"
        payloads = [str(v) for v in harness.recorder.extras().values()]
        assert any(url in p for p in payloads), (
            f"the shared URL never reached an Intent extra; extras={harness.recorder.extras()}"
        )

        harness.recorder.reset()
        process(_send_intent(url))
        assert not harness.recorder.activity_starts, (
            "the same shared link was downloaded twice -- the replay guard is gone"
        )
    finally:
        app.auto_download_on_share = original


def test_no_python_bytes_reach_a_java_byte_array(app):
    """CLAUDE.md: pyjnius cannot convert bytes -> byte[]; must be bytearray.

    The fake OutputStream raises on ``bytes`` exactly like pyjnius does, so any
    such write during this session would already have been logged.
    """
    offenders = [kind for kind, _data in harness.recorder.stream_writes if kind == "bytes"]
    assert not offenders, f"Python bytes written to a Java OutputStream: {offenders}"


# ---------------------------------------------------------------------------
# 6. Episode library / player -- driven by the module's own constants
# ---------------------------------------------------------------------------


@pytest.fixture
def podcast_library(app, app_module, tmp_path):
    """A realistic /sdcard/Podcasts tree, wired into the running app."""
    if not hasattr(app, "_podcast_dir"):
        pytest.skip("this build has no _podcast_dir")
    manifest_name = getattr(app_module, "MANIFEST_NAME", ".episodes.json")
    base = tmp_path / "Podcasts"
    channel = base / "natebjones"
    channel.mkdir(parents=True)
    audio = channel / "Episode One.m4a"
    audio.write_bytes(b"\x00" * 2048)
    thumb = channel / "Episode One.jpg"
    thumb.write_bytes(b"\xff\xd8\xff\xd9")
    (channel / manifest_name).write_text(
        json.dumps(
            [
                {
                    "id": "vid1",
                    "title": "Episode One",
                    "relpath": "Episode One.m4a",
                    "thumbnail_relpath": "Episode One.jpg",
                    "duration": 3671,
                    "filesize": 2048,
                    "downloaded_at": "2026-07-25T10:00:00",
                }
            ]
        )
    )
    original = app._podcast_dir
    app._podcast_dir = str(base)
    yield {"base": base, "audio": audio, "thumb": thumb, "channel": channel}
    app._podcast_dir = original


def test_episode_list_renders_from_manifest(app, podcast_library):
    if "episode_list" not in app.root.ids:
        pytest.skip("this build has no episode_list")
    app.load_episodes()
    items = app.root.ids.episode_list.children
    assert items, "manifest episode produced no list rows"
    titles = [getattr(w, "text", "") for w in items]
    assert any("Episode One" in t for t in titles), titles
    assert "1 episode" in app.library_summary or "1 " in app.library_summary


def test_selecting_an_episode_sets_up_mediaplayer_correctly(app, podcast_library):
    """CLAUDE.md: setAudioStreamType(STREAM_MUSIC) must come *before*
    setDataSource, and toggling must use pause()/start()."""
    if not hasattr(app, "select_episode"):
        pytest.skip("this build has no select_episode")
    app.load_episodes()
    episodes = getattr(app, "_episodes", [])
    assert episodes, "no episodes collected from the fixture library"
    harness.recorder.reset()

    app.select_episode(episodes[0])

    if "android.media.MediaPlayer" not in harness.recorder.autoclass_names:
        pytest.skip("this build does not use android.media.MediaPlayer")
    order = [c[1] for c in harness.recorder.calls]
    assert "setAudioStreamType" in order, "MediaPlayer.setAudioStreamType() was never called"
    assert "setDataSource" in order
    assert order.index("setAudioStreamType") < order.index("setDataSource"), (
        "setAudioStreamType(STREAM_MUSIC) must be called before setDataSource"
    )

    harness.recorder.reset()
    app.toggle_play()
    toggled = [c[1] for c in harness.recorder.calls]
    assert "prepare" not in toggled, (
        "toggle_play() re-prepares the MediaPlayer; use pause()/start() instead"
    )


def test_delete_removes_the_files_and_the_manifest_entry(app, app_module, podcast_library):
    if not hasattr(app, "delete_episode"):
        pytest.skip("this build has no delete_episode")
    app.load_episodes()
    episodes = list(getattr(app, "_episodes", []))
    assert episodes
    target = episodes[0]

    app.delete_episode(target)

    assert not podcast_library["audio"].exists(), "audio file was not deleted"
    manifest_name = getattr(app_module, "MANIFEST_NAME", ".episodes.json")
    manifest = podcast_library["channel"] / manifest_name
    assert json.loads(manifest.read_text()) == [], "manifest entry was not removed"


# ---------------------------------------------------------------------------
# 7. Desktop path: the Android layer genuinely absent
# ---------------------------------------------------------------------------


def test_module_imports_and_builds_without_android(tmp_path, monkeypatch):
    """Every android/jnius import in app/main.py is try/except-guarded; make
    sure the fallbacks are real and the app still builds."""
    monkeypatch.setenv("HOME", str(tmp_path))
    with harness.no_android():
        with pytest.raises(ImportError):
            import jnius  # noqa: F401

        module = harness.load_app_module(name="app_main_desktop")
        data_dir = module.get_data_dir()
        assert str(tmp_path) in data_dir or data_dir.startswith(os.path.expanduser("~"))

        app_cls = harness.get_app_class(module)
        instance = app_cls()
        try:
            instance.root = instance.build()
            assert instance.root is not None
            instance.on_start()
            harness.pump_clock(2.0)
            # The Termux hand-off must fail gracefully, not explode.
            instance.root.ids.url_input.text = "https://youtu.be/abc"
            instance.download_single_video()
        finally:
            harness.stop_app(instance)


# ---------------------------------------------------------------------------
# 8. The linter itself
# ---------------------------------------------------------------------------


def test_linter_reports_no_errors_for_app_main():
    result = lint_kivymd.lint_file(APP_MAIN)
    assert result.ok, "\n".join(str(f) for f in result.errors)


def test_kivymd2_denylist_is_accurate():
    """Nothing on the 2.x denylist may resolve in the installed KivyMD.

    Guards against the denylist drifting and producing false positives.
    """
    resolvable = [
        name for name in sorted(lint_kivymd.KIVYMD_2X_ONLY)
        if lint_kivymd.resolve_widget_name(name)[0]
    ]
    assert not resolvable, (
        f"these names DO exist in KivyMD {lint_kivymd._kivymd_version()} and must be "
        f"removed from KIVYMD_2X_ONLY: {resolvable}"
    )


@pytest.mark.parametrize(
    "name",
    [
        "MDSeparator",
        "MDBottomNavigation",
        "MDBottomNavigationItem",
        "ImageLeftWidget",
        "IconLeftWidget",
        "ThreeLineAvatarListItem",
        "TwoLineAvatarListItem",
        "OneLineListItem",
        "MDCard",
        "MDSwitch",
        "AsyncImage",
        "Image",
        "ScrollView",
        "MDIconButton",
        "MDSpinner",
        "MDDialog",
        "MDTopAppBar",
        "MDFlatButton",
        "MDRaisedButton",
        "MDTextField",
        "MDList",
        "MDLabel",
        "MDBoxLayout",
        "MDScreen",
    ],
)
def test_known_good_widgets_resolve(name):
    """The resolver must not reject widgets that really are in KivyMD 1.2.0."""
    ok, _cls = lint_kivymd.resolve_widget_name(name)
    assert ok, f"{name} should resolve in KivyMD 1.2.0 but the linter says it does not"


def test_linter_catches_every_seeded_bug():
    """Proves the linter is not vacuous: a file with one of each bug in it."""
    fixture = Path(__file__).resolve().parent / "fixtures" / "bad_app.py"
    result = lint_kivymd.lint_file(fixture)
    codes = {f.code for f in result.errors}
    for expected in ("KVMD001", "KVMD002", "KVMD003", "KVMD004", "PYJN001", "LAMB001"):
        assert expected in codes, (
            f"linter missed {expected} in {fixture}; it reported {sorted(codes)}"
        )
    assert all(f.line > 0 for f in result.errors), "findings must carry a line number"
