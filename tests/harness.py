"""Desktop test harness for the KivyMD Android app in ``app/main.py``.

The APK is built by GitHub Actions (~30 min per build) and has historically
broken on things that are trivially detectable on a laptop: KivyMD 2.x widget
names, missing KV ids, pyjnius type mistakes.  This module makes
``app/main.py`` importable and *runnable* on a headless desktop so those
failures surface in seconds.

Two things are required to run a Kivy app headlessly here:

1.  Environment must be configured **before** ``kivy`` is imported
    (:func:`setup_kivy_env`).
2.  Kivy needs *a* Window object: ``kivy.uix.widget.Widget.__init__`` calls
    ``EventLoop.ensure_window()`` which ``sys.exit(1)``s when there is none.
    There is no ``mock`` window provider shipped with Kivy 2.3.1, so
    :func:`install_mock_window` builds one from ``WindowBase`` and registers it
    on the EventLoop.  No GL context is ever created.

The Android side (``android.*`` / ``jnius``) is replaced by *recording* fakes so
tests can assert on the exact Intents the app builds -- see :data:`recorder`.

Typical use::

    from tests import harness          # or: import harness
    harness.setup()                    # env + mock window + android fakes
    mod = harness.load_app_module()
    with harness.running_app(mod) as app:
        app.download_single_video()
    assert harness.recorder.activity_starts
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
import types
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
# YT_APP_MAIN lets the suite be pointed at a copy of the app -- used to verify
# the harness actually fails on a broken app (mutation-testing the tests).
APP_MAIN = Path(os.environ.get("YT_APP_MAIN") or (REPO_ROOT / "app" / "main.py"))

# Modules the app tries to import that only exist on an Android device.
ANDROID_MODULE_NAMES = (
    "android",
    "android.storage",
    "android.permissions",
    "android.activity",
    "android.runnable",
    "android.broadcast",
    "jnius",
)


# --------------------------------------------------------------------------
# 1. Environment -- must run before ``import kivy``
# --------------------------------------------------------------------------

KIVY_ENV = {
    # Never let Kivy parse pytest's argv.
    "KIVY_NO_ARGS": "1",
    # KIVY (the default) replaces sys.stderr with a Logger stream, which hides
    # every traceback -- including pytest's.  PYTHON mode routes Kivy logging
    # through the stdlib and leaves stdout/stderr alone.
    "KIVY_LOG_MODE": "PYTHON",
    "KIVY_LOG_LEVEL": "critical",
    "KIVY_NO_CONSOLELOG": "1",
    "KIVY_NO_FILELOG": "1",
    # No real GL calls; textures/shaders become no-ops.
    "KIVY_GL_BACKEND": "mock",
    "KIVY_AUDIO": "mock",
    # NOTE: KIVY_WINDOW is deliberately NOT set.  Kivy 2.3.1 ships no "mock"
    # window provider, so forcing it leaves Window == None.  Under xvfb the
    # normal sdl2 provider works; without a display we fall back to the
    # GL-free MockWindow built in install_window().
}


_env_applied = False


def setup_kivy_env() -> None:
    """Set the Kivy environment variables.  Must be called before importing kivy."""
    global _env_applied
    if _env_applied:
        return
    if "kivy" in sys.modules:  # pragma: no cover - defensive
        raise RuntimeError(
            "harness.setup_kivy_env() must be called before 'kivy' is imported. "
            "Import tests.harness first (tests/conftest.py does this)."
        )
    for key, value in KIVY_ENV.items():
        os.environ[key] = value
    if not os.environ.get("DISPLAY"):
        # No X server (someone ran pytest without tests/run_all.sh).  Two
        # things would otherwise kill the process outright before Python could
        # react: SDL2 probing X11, and Kivy falling through to the window_x11
        # provider whose Xlib I/O error handler calls exit(102).  Pin the
        # provider to sdl2 + the dummy video driver: it then fails *cleanly*
        # (no GL in the dummy driver), Window stays None, and install_window()
        # substitutes the GL-free MockWindow.
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("KIVY_WINDOW", "sdl2")
    _env_applied = True


# --------------------------------------------------------------------------
# 2. Recording fakes for the Android / pyjnius layer
# --------------------------------------------------------------------------


class Recorder:
    """Collects everything the app does through pyjnius / android.*.

    Every list is ``reset()``-able so each test starts from a clean slate.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.autoclass_names: list[str] = []
        self.casts: list[tuple[str, object]] = []
        self.intents: list["FakeIntent"] = []
        self.put_extras: list[tuple["FakeIntent", object, object]] = []
        self.activity_starts: list[tuple[str, object]] = []
        self.stream_writes: list[tuple[str, object]] = []
        self.permission_requests: list[list] = []
        self.calls: list[tuple[str, str, tuple, dict]] = []

    # -- helpers used by assertions -------------------------------------
    def record(self, target: str, method: str, args: tuple, kwargs: dict) -> None:
        self.calls.append((target, method, args, kwargs))

    def method_calls(self, method: str) -> list[tuple[str, str, tuple, dict]]:
        return [c for c in self.calls if c[1] == method]

    def extras(self) -> dict:
        """Flattened {key: value} of every ``putExtra`` seen."""
        return {str(k): v for (_i, k, v) in self.put_extras}

    def started_intents(self) -> list["FakeIntent"]:
        return [obj for (_kind, obj) in self.activity_starts]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Recorder autoclass={len(self.autoclass_names)} intents={len(self.intents)} "
            f"extras={len(self.put_extras)} starts={len(self.activity_starts)}>"
        )


recorder = Recorder()


# Real values for the Android constants the app relies on, so tests can assert
# on the literal strings that end up in the Intent instead of on placeholders.
_ANDROID_CONSTANTS = {
    "android.content.Intent": {
        "ACTION_SEND": "android.intent.action.SEND",
        "ACTION_SENDTO": "android.intent.action.SENDTO",
        "ACTION_VIEW": "android.intent.action.VIEW",
        "ACTION_MAIN": "android.intent.action.MAIN",
        "EXTRA_TEXT": "android.intent.extra.TEXT",
        "EXTRA_STREAM": "android.intent.extra.STREAM",
        "EXTRA_SUBJECT": "android.intent.extra.SUBJECT",
        "CATEGORY_LAUNCHER": "android.intent.category.LAUNCHER",
        "FLAG_ACTIVITY_NEW_TASK": 0x10000000,
        "FLAG_ACTIVITY_CLEAR_TOP": 0x04000000,
        "FLAG_GRANT_READ_URI_PERMISSION": 0x00000001,
        "FLAG_GRANT_WRITE_URI_PERMISSION": 0x00000002,
    },
    "android.os.Environment": {
        "DIRECTORY_MUSIC": "Music",
        "DIRECTORY_PODCASTS": "Podcasts",
        "DIRECTORY_DOWNLOADS": "Download",
    },
    "android.media.AudioManager": {"STREAM_MUSIC": 3},
}


class FakeJavaObject:
    """Instance returned by calling a faked Java class.

    Unknown attributes become recording callables, so the app can call any Java
    method it likes without the harness having to know about it up front.
    """

    def __init__(self, java_name: str, *args, **kwargs):
        object.__setattr__(self, "_java_name", java_name)
        object.__setattr__(self, "_args", args)
        object.__setattr__(self, "_kwargs", kwargs)
        recorder.record(java_name, "__init__", args, kwargs)

    def __getattr__(self, item):
        java_name = object.__getattribute__(self, "_java_name")
        const = _ANDROID_CONSTANTS.get(java_name, {})
        if item in const:
            return const[item]

        def _method(*args, **kwargs):
            recorder.record(java_name, item, args, kwargs)
            if item in ("toString", "getAbsolutePath", "getPath"):
                return str(self)
            if item.startswith("get") or item in ("createChooser", "parse", "fromFile"):
                return FakeJavaObject(f"{java_name}.{item}()")
            return None

        return _method

    def __str__(self) -> str:
        args = object.__getattribute__(self, "_args")
        if len(args) == 1 and isinstance(args[0], (str, bytes)):
            return args[0] if isinstance(args[0], str) else args[0].decode("utf8", "replace")
        return f"<{object.__getattribute__(self, '_java_name')}{args!r}>"

    def __eq__(self, other):
        return str(self) == str(other)

    def __hash__(self):
        return hash(str(self))

    def __repr__(self) -> str:
        return f"<FakeJavaObject {object.__getattribute__(self, '_java_name')}>"


class FakeIntent(FakeJavaObject):
    """android.content.Intent stand-in; records action/type/extras/flags."""

    def __init__(self, *args, **kwargs):
        super().__init__("android.content.Intent", *args, **kwargs)
        object.__setattr__(self, "action", args[0] if args else None)
        object.__setattr__(self, "mime_type", None)
        object.__setattr__(self, "extras", {})
        object.__setattr__(self, "flags", 0)
        object.__setattr__(self, "chooser_of", None)
        recorder.intents.append(self)

    # -- the calls the app actually makes -------------------------------
    def setAction(self, action):
        recorder.record("Intent", "setAction", (action,), {})
        object.__setattr__(self, "action", _as_py(action))
        return self

    def getAction(self):
        return object.__getattribute__(self, "action")

    def setType(self, mime):
        recorder.record("Intent", "setType", (mime,), {})
        object.__setattr__(self, "mime_type", _as_py(mime))
        return self

    def getType(self):
        return object.__getattribute__(self, "mime_type")

    def setPackage(self, pkg):
        recorder.record("Intent", "setPackage", (pkg,), {})
        object.__setattr__(self, "package", _as_py(pkg))
        return self

    def setClassName(self, *args):
        recorder.record("Intent", "setClassName", args, {})
        return self

    def putExtra(self, key, value):
        recorder.record("Intent", "putExtra", (key, value), {})
        recorder.put_extras.append((self, _as_py(key), value))
        object.__getattribute__(self, "extras")[_as_py(key)] = value
        return self

    def getStringExtra(self, key):
        value = object.__getattribute__(self, "extras").get(_as_py(key))
        return None if value is None else _as_py(value)

    def getExtras(self):
        return dict(object.__getattribute__(self, "extras"))

    def hasExtra(self, key):
        return _as_py(key) in object.__getattribute__(self, "extras")

    def addFlags(self, flags):
        recorder.record("Intent", "addFlags", (flags,), {})
        object.__setattr__(self, "flags", object.__getattribute__(self, "flags") | int(flags or 0))
        return self

    def setFlags(self, flags):
        recorder.record("Intent", "setFlags", (flags,), {})
        object.__setattr__(self, "flags", int(flags or 0))
        return self

    def setData(self, data):
        recorder.record("Intent", "setData", (data,), {})
        object.__setattr__(self, "data", data)
        return self

    def setDataAndType(self, data, mime):
        recorder.record("Intent", "setDataAndType", (data, mime), {})
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "mime_type", _as_py(mime))
        return self


def _as_py(value):
    """Unwrap a faked ``java.lang.String`` (or anything else) into a Python value."""
    if isinstance(value, FakeJavaObject) and not isinstance(value, FakeIntent):
        return str(value)
    return value


class FakeOutputStream(FakeJavaObject):
    """Records ``write()`` payloads so the bytes-vs-bytearray rule is testable."""

    def __init__(self, java_name="java.io.OutputStream", *args, **kwargs):
        super().__init__(java_name, *args, **kwargs)

    def write(self, data, *args):
        recorder.record("OutputStream", "write", (data,) + args, {})
        recorder.stream_writes.append((type(data).__name__, data))
        if isinstance(data, bytes):
            # pyjnius refuses to marshal Python ``bytes`` into a Java byte[]:
            # this is the exact runtime failure CLAUDE.md warns about.
            raise TypeError(
                "jnius: Python 'bytes' cannot be converted to Java byte[]; use bytearray"
            )
        return None

    def flush(self):
        recorder.record("OutputStream", "flush", (), {})

    def close(self):
        recorder.record("OutputStream", "close", (), {})


class FakeJavaClassMeta(type):
    """Makes static constants/methods available on the *class* object."""

    def __getattr__(cls, item):
        java_name = cls._java_name
        const = _ANDROID_CONSTANTS.get(java_name, {})
        if item in const:
            return const[item]
        if item == "mActivity" or item == "mService":
            return cls._activity

        def _static(*args, **kwargs):
            recorder.record(java_name, item, args, kwargs)
            if item == "createChooser":
                chooser = FakeIntent()
                object.__setattr__(chooser, "chooser_of", args[0] if args else None)
                return chooser
            return FakeJavaObject(f"{java_name}.{item}()", *args)

        return _static

    def __repr__(cls):  # pragma: no cover - debugging aid
        return f"<FakeJavaClass {cls._java_name}>"


class FakeActivity:
    """org.kivy.android.PythonActivity.mActivity"""

    def startActivity(self, intent):
        recorder.record("Activity", "startActivity", (intent,), {})
        recorder.activity_starts.append(("startActivity", intent))

    def startActivityForResult(self, intent, code=0):
        recorder.record("Activity", "startActivityForResult", (intent, code), {})
        recorder.activity_starts.append(("startActivityForResult", intent))

    def startService(self, intent):
        recorder.record("Activity", "startService", (intent,), {})
        recorder.activity_starts.append(("startService", intent))

    def startForegroundService(self, intent):
        recorder.record("Activity", "startForegroundService", (intent,), {})
        recorder.activity_starts.append(("startForegroundService", intent))

    def getPackageName(self):
        return "org.test.youtubepodcasts"

    def getContentResolver(self):
        return FakeContentResolver()

    def getExternalFilesDir(self, kind=None):
        base = Path(os.environ.get("HARNESS_APP_DIR", "/tmp/harness-app-files"))
        base.mkdir(parents=True, exist_ok=True)
        return FakeJavaObject("java.io.File", str(base))

    def __getattr__(self, item):
        def _method(*args, **kwargs):
            recorder.record("Activity", item, args, kwargs)
            return FakeJavaObject(f"Activity.{item}()")

        return _method


class FakeContentResolver:
    def insert(self, uri, values):
        recorder.record("ContentResolver", "insert", (uri, values), {})
        return FakeJavaObject("android.net.Uri", "content://media/external/audio/media/1")

    def openOutputStream(self, uri):
        recorder.record("ContentResolver", "openOutputStream", (uri,), {})
        return FakeOutputStream("java.io.OutputStream")

    def update(self, *args):
        recorder.record("ContentResolver", "update", args, {})
        return 1

    def __getattr__(self, item):
        def _method(*args, **kwargs):
            recorder.record("ContentResolver", item, args, kwargs)
            return FakeJavaObject(f"ContentResolver.{item}()")

        return _method


_JAVA_CLASS_CACHE: dict[str, type] = {}


def _make_java_class(name: str) -> type:
    if name in _JAVA_CLASS_CACHE:
        return _JAVA_CLASS_CACHE[name]

    if name == "android.content.Intent":
        base = FakeIntent

        def __init__(self, *args, **kwargs):
            FakeIntent.__init__(self, *args, **kwargs)

    elif "OutputStream" in name:
        base = FakeOutputStream

        def __init__(self, *args, **kwargs):
            FakeOutputStream.__init__(self, name, *args, **kwargs)

    else:
        base = FakeJavaObject

        def __init__(self, *args, **kwargs):
            FakeJavaObject.__init__(self, name, *args, **kwargs)

    namespace = {
        "_java_name": name,
        "_activity": FakeActivity(),
        "__init__": __init__,
    }
    cls = FakeJavaClassMeta(f"Fake_{name.rsplit('.', 1)[-1]}", (base,), namespace)
    _JAVA_CLASS_CACHE[name] = cls
    return cls


def fake_autoclass(name, *args, **kwargs):
    recorder.autoclass_names.append(name)
    recorder.record("jnius", "autoclass", (name,), {})
    return _make_java_class(name)


def fake_cast(signature, obj):
    recorder.casts.append((signature, obj))
    recorder.record("jnius", "cast", (signature, obj), {})
    return obj


def make_jnius_module() -> types.ModuleType:
    mod = types.ModuleType("jnius")
    mod.autoclass = fake_autoclass
    mod.cast = fake_cast
    mod.detach = lambda *a, **k: recorder.record("jnius", "detach", a, k)

    class JavaException(Exception):
        pass

    class PythonJavaClass:  # pragma: no cover - only needed for import-time use
        __javainterfaces__ = []

        def __init__(self, *a, **k):
            pass

    def java_method(signature, name=None):  # pragma: no cover
        def deco(fn):
            return fn

        return deco

    mod.JavaException = JavaException
    mod.PythonJavaClass = PythonJavaClass
    mod.java_method = java_method
    mod.__harness_fake__ = True
    return mod


class FakePermission:
    INTERNET = "android.permission.INTERNET"
    WRITE_EXTERNAL_STORAGE = "android.permission.WRITE_EXTERNAL_STORAGE"
    READ_EXTERNAL_STORAGE = "android.permission.READ_EXTERNAL_STORAGE"
    READ_MEDIA_AUDIO = "android.permission.READ_MEDIA_AUDIO"
    POST_NOTIFICATIONS = "android.permission.POST_NOTIFICATIONS"
    FOREGROUND_SERVICE = "android.permission.FOREGROUND_SERVICE"


def make_android_modules(storage_dir: str | None = None) -> dict[str, types.ModuleType]:
    """Build the fake ``android`` package tree (not yet installed in sys.modules)."""
    storage = storage_dir or os.environ.get(
        "HARNESS_ANDROID_STORAGE", "/tmp/harness-android-storage"
    )
    Path(storage).mkdir(parents=True, exist_ok=True)

    android = types.ModuleType("android")
    android.__path__ = []  # mark as a package so submodule imports work
    android.__harness_fake__ = True

    def _api_version():
        return 34

    android.api_version = _api_version
    android.mActivity = FakeActivity()

    storage_mod = types.ModuleType("android.storage")
    storage_mod.app_storage_path = lambda: storage
    storage_mod.primary_external_storage_path = lambda: "/sdcard"
    storage_mod.secondary_external_storage_path = lambda: None
    storage_mod.__harness_fake__ = True

    perms_mod = types.ModuleType("android.permissions")

    def request_permissions(perms, callback=None):
        recorder.permission_requests.append(list(perms))
        recorder.record("android.permissions", "request_permissions", (list(perms),), {})
        if callback:
            callback(list(perms), [True] * len(perms))

    perms_mod.request_permissions = request_permissions
    perms_mod.check_permission = lambda p: True
    perms_mod.Permission = FakePermission
    perms_mod.__harness_fake__ = True

    activity_mod = types.ModuleType("android.activity")
    activity_mod._bindings = {}

    def bind(**kwargs):
        activity_mod._bindings.update(kwargs)
        recorder.record("android.activity", "bind", (), dict(kwargs))

    def unbind(**kwargs):
        for key in kwargs:
            activity_mod._bindings.pop(key, None)
        recorder.record("android.activity", "unbind", (), dict(kwargs))

    activity_mod.bind = bind
    activity_mod.unbind = unbind
    activity_mod._intent = None
    activity_mod.getIntent = lambda: activity_mod._intent or FakeIntent()
    activity_mod.__harness_fake__ = True

    runnable_mod = types.ModuleType("android.runnable")
    runnable_mod.run_on_ui_thread = lambda fn: fn
    runnable_mod.__harness_fake__ = True

    broadcast_mod = types.ModuleType("android.broadcast")

    class BroadcastReceiver:  # pragma: no cover - only for import compatibility
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    broadcast_mod.BroadcastReceiver = BroadcastReceiver
    broadcast_mod.__harness_fake__ = True

    android.storage = storage_mod
    android.permissions = perms_mod
    android.activity = activity_mod
    android.runnable = runnable_mod
    android.broadcast = broadcast_mod

    return {
        "android": android,
        "android.storage": storage_mod,
        "android.permissions": perms_mod,
        "android.activity": activity_mod,
        "android.runnable": runnable_mod,
        "android.broadcast": broadcast_mod,
        "jnius": make_jnius_module(),
    }


def install_android_fakes(storage_dir: str | None = None) -> dict[str, types.ModuleType]:
    """Install the recording Android/pyjnius fakes into ``sys.modules``."""
    mods = make_android_modules(storage_dir)
    sys.modules.update(mods)
    return mods


def remove_android_fakes() -> None:
    for name in ANDROID_MODULE_NAMES:
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, "__harness_fake__", False):
            del sys.modules[name]


def android_fakes_installed() -> bool:
    return getattr(sys.modules.get("jnius"), "__harness_fake__", False)


class _BlockAndroidImports:
    """meta_path hook that makes ``android`` / ``jnius`` genuinely unimportable."""

    def find_module(self, fullname, path=None):  # pragma: no cover - py<3.12 shim
        return self if self._blocks(fullname) else None

    def find_spec(self, fullname, path=None, target=None):
        if self._blocks(fullname):
            raise ImportError(f"harness: '{fullname}' blocked (simulating desktop)")
        return None

    @staticmethod
    def _blocks(fullname: str) -> bool:
        root = fullname.split(".")[0]
        return root in ("android", "jnius")


@contextlib.contextmanager
def no_android():
    """Run a block with the Android layer *absent*, exercising the desktop path.

    ``app/main.py`` guards every Android import with try/except; this makes sure
    those fallbacks are actually taken (and actually work).
    """
    saved = {name: sys.modules[name] for name in ANDROID_MODULE_NAMES if name in sys.modules}
    for name in saved:
        del sys.modules[name]
    blocker = _BlockAndroidImports()
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            sys.meta_path.remove(blocker)
        sys.modules.update(saved)


# --------------------------------------------------------------------------
# 3. Mock Kivy Window (no GL, no X server)
# --------------------------------------------------------------------------

_window = None
WINDOW_KIND = None  # "sdl2" or "mock", filled in by install_window()


def silence_kivy_logging() -> None:
    """Stop Kivy's own logging from drowning the test report.

    With the (deliberately mocked) GL backend Kivy logs a shader-compile error
    for every canvas instruction, plus provider tracebacks for xclip/camera/etc.
    None of it matters here, and in PYTHON log mode it all propagates to the
    root logger, where pytest picks it up.

    Set ``HARNESS_KIVY_LOGS=1`` to keep Kivy's logging when debugging.
    """
    import logging

    if os.environ.get("HARNESS_KIVY_LOGS"):
        return

    kivy_logger = logging.getLogger("kivy")
    kivy_logger.setLevel(logging.CRITICAL + 1)
    kivy_logger.propagate = False
    kivy_logger.addHandler(logging.NullHandler())


def install_window():
    """Make sure Kivy has *a* Window so widgets can be instantiated.

    ``kivy.uix.widget.Widget.__init__`` calls ``EventLoop.ensure_window()``,
    which ``sys.exit(1)``s when there is none -- so this must succeed before any
    widget (or ``dp()``) is used.

    Preferred path: run the suite under ``xvfb-run`` (see ``tests/run_all.sh``)
    and use the real sdl2 provider, which also initialises the (mocked) CGL
    backend so canvas textures can be created.

    Fallback path (no DISPLAY): a ``WindowBase`` subclass that never creates a
    native window.  The CGL backend is initialised by hand, otherwise the first
    texture upload dereferences NULL GL function pointers and segfaults.
    """
    global _window, WINDOW_KIND
    if _window is not None:
        return _window

    setup_kivy_env()
    silence_kivy_logging()

    import kivy.core.window as kivy_core_window
    from kivy.base import EventLoop

    win = kivy_core_window.Window
    if win is not None:
        WINDOW_KIND = "sdl2"
    else:
        win = _build_mock_window()
        kivy_core_window.Window = win
        WINDOW_KIND = "mock"

    EventLoop.set_window(win)
    _window = win
    return win


def _build_mock_window():
    from kivy.core.window import WindowBase

    class MockWindow(WindowBase):
        """Headless Window: no native window, no GL context, no flip."""

        def create_window(self, *args, **kwargs):
            _init_cgl()
            return None

        def flip(self):
            return None

        def set_title(self, title):
            return None

        def set_icon(self, filename):
            return None

        def screenshot(self, *args, **kwargs):  # pragma: no cover
            return None

        def _get_gl_size(self):
            return self._size

    return MockWindow()


def _init_cgl():
    """Bind the (mock) OpenGL function table without a real GL context."""
    try:
        from kivy.graphics.cgl import cgl_init

        cgl_init()
    except Exception:  # pragma: no cover - backend already initialised
        pass


# Backwards-compatible alias.
install_mock_window = install_window


# --------------------------------------------------------------------------
# 4. Loading app/main.py
# --------------------------------------------------------------------------

_load_counter = 0


def load_app_module(path: str | os.PathLike | None = None, name: str | None = None):
    """Import ``app/main.py`` as a module object from its path.

    A fresh module name is used on every call so a test can load the app twice
    (e.g. once with the Android fakes and once without) without cache reuse.
    """
    global _load_counter
    target = Path(path) if path else APP_MAIN
    if not target.exists():  # pragma: no cover
        raise FileNotFoundError(f"app entrypoint not found: {target}")

    if name is None:
        _load_counter += 1
        name = f"app_main_harness_{_load_counter}"

    saved_hook = sys.excepthook
    spec = importlib.util.spec_from_file_location(name, str(target))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    finally:
        # app/main.py installs its own sys.excepthook; don't let that leak into
        # the test process (it would swallow pytest's own error reporting).
        sys.excepthook = saved_hook
    return module


def get_app_class(module):
    """Return the single ``MDApp`` subclass defined in ``module``."""
    from kivymd.app import MDApp

    candidates = [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, MDApp) and obj is not MDApp
    ]
    if not candidates:  # pragma: no cover
        raise AssertionError(f"no MDApp subclass found in {module.__name__}")
    if len(candidates) > 1:  # pragma: no cover
        # Prefer the most derived one.
        candidates.sort(key=lambda c: len(c.__mro__), reverse=True)
    return candidates[0]


def get_kv_strings(source: str) -> list[str]:
    """Extract every KV-language string literal from Python source.

    Finds both ``Builder.load_string(<literal>)`` and module-level constants
    that are passed to ``Builder.load_string`` (which is how ``app/main.py``
    does it: ``KV = '''...'''`` then ``Builder.load_string(KV)``).
    """
    import ast

    tree = ast.parse(source)
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = getattr(func, "attr", None) or getattr(func, "id", None)
        if fname not in ("load_string", "load_file"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in constants:
                found.append(constants[arg.id])

    if not found:
        # Fall back to any constant that smells like KV.
        for value in constants.values():
            if _looks_like_kv(value):
                found.append(value)
    return found


def _looks_like_kv(text: str) -> bool:
    import re

    return bool(re.search(r"^\s*<?[A-Z][A-Za-z0-9_]*>?:\s*$", text, re.M))


# --------------------------------------------------------------------------
# 5. Running the app headlessly
# --------------------------------------------------------------------------


def pump_clock(seconds: float = 2.5, step: float = 0.05) -> None:
    """Advance Kivy's Clock by ``seconds`` of *virtual* time.

    ``Clock.schedule_once(cb, 1.0)`` has to actually fire in a test without
    burning a real second, so the clock's time source is swapped for a fake that
    jumps forward ``step`` per tick.

    Two traps, both hit while building this:

    * ``ClockBase.idle()`` busy-waits until ``1/fps - (time() - _last_tick)``
      drops below the clock resolution.  A frozen ``time()`` makes that
      condition unreachable -> infinite loop.  ``_max_fps = 0`` disables the
      frame-rate sleep entirely.
    * ``_last_tick`` is left in the (future) fake timeline, so the *next* real
      tick would sleep until wall-clock caught up.  It is re-anchored to real
      time on the way out.
    """
    from kivy.clock import Clock

    original_time = Clock.time
    original_fps = getattr(Clock, "_max_fps", 0)

    now = float(original_time())
    try:
        now = max(now, float(Clock._last_tick) + step)
    except Exception:  # pragma: no cover - attribute layout changed
        pass
    end = now + seconds
    guard = int(seconds / step) + 10

    try:
        Clock._max_fps = 0
        Clock.time = lambda: now
        while now < end and guard > 0:
            guard -= 1
            now += step
            Clock.tick()
    finally:
        Clock.time = original_time
        try:
            Clock._max_fps = original_fps
            Clock._last_tick = float(original_time())
        except Exception:  # pragma: no cover
            pass


def stop_app(app) -> None:
    """Tear an app instance down without touching the (nonexistent) EventLoop."""
    from kivy.app import App
    from kivy.clock import Clock

    with contextlib.suppress(Exception):
        Clock.unschedule(app._install_settings_keys)
    with contextlib.suppress(Exception):
        if app.root is not None:
            app.root.clear_widgets()
    app.root = None
    if App._running_app is app:
        App._running_app = None


@contextlib.contextmanager
def running_app(module=None, start: bool = True, pump: float = 2.5):
    """Instantiate the app, run ``build()`` (+ optionally ``on_start()``), pump Clock.

    Yields the live app instance with ``app.root`` populated.
    """
    install_window()
    module = module or load_app_module()
    app_cls = get_app_class(module)
    app = app_cls()
    try:
        root = app.build()
        app.root = root
        if start:
            app.on_start()
            pump_clock(pump)
        yield app
    finally:
        stop_app(app)


# --------------------------------------------------------------------------
# 6. One-call setup
# --------------------------------------------------------------------------

_setup_done = False


def setup(with_android: bool = True):
    """Configure env, install the Android fakes and the mock Window."""
    global _setup_done
    setup_kivy_env()
    if with_android and not android_fakes_installed():
        install_android_fakes()
    win = install_window()
    _setup_done = True
    return win
