"""YouTube Podcast Downloader - Android App (KivyMD 1.x)
Version 3.0.0 - Downloads tab with artwork, in-app player, share-to-download

The app is a control panel: Termux does the downloading (it has ffmpeg and
yt-dlp), and writes the audio plus a .episodes.json manifest into shared
storage - "Internal storage > Podcasts > <channel>" in a file manager. This
app reads those folders, so the files stay ordinary files you can browse,
upload elsewhere, or delete.
"""

import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime

# === CRASH LOGGING ===
CRASH_LOG_PATHS = []


def setup_crash_logging():
    global CRASH_LOG_PATHS
    dirs_to_try = []
    try:
        from android.storage import app_storage_path

        dirs_to_try.append(app_storage_path())
    except ImportError:
        pass
    dirs_to_try.append(os.path.expanduser("~"))
    for d in dirs_to_try:
        try:
            os.makedirs(d, exist_ok=True)
            CRASH_LOG_PATHS.append(os.path.join(d, "crash_log.txt"))
        except Exception:
            pass


def log_crash(exc_type, exc_value, exc_tb):
    error_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    for path in CRASH_LOG_PATHS:
        try:
            with open(path, "a") as f:
                f.write(f"\n{'='*60}\nCRASH at {datetime.now()}\n{error_text}\n{'='*60}\n")
        except Exception:
            pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)


setup_crash_logging()
sys.excepthook = log_crash

# === Import Kivy/KivyMD ===
try:
    from kivy.clock import Clock
    from kivy.lang import Builder
    from kivy.metrics import dp
    from kivy.properties import BooleanProperty, StringProperty
    from kivymd.app import MDApp
except Exception:
    log_crash(*sys.exc_info())
    raise

# Fix stdout/stderr for Android
if not hasattr(sys.stdout, "write") or isinstance(sys.stdout, str):
    sys.stdout = open(os.devnull, "w")
if not hasattr(sys.stderr, "write") or isinstance(sys.stderr, str):
    sys.stderr = open(os.devnull, "w")


# === CONSTANTS ===

MANIFEST_NAME = ".episodes.json"
AUDIO_EXTS = (".m4a", ".mp3", ".opus", ".ogg", ".webm", ".aac", ".flac", ".wav", ".mka")
THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")

MIME_BY_EXT = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
}

# Shared storage, so the episodes stay browsable in any file manager and can be
# uploaded to your own cloud drive.
PODCAST_DIR_CANDIDATES = (
    "/storage/emulated/0/Podcasts",
    "/sdcard/Podcasts",
    os.path.expanduser("~/storage/shared/Podcasts"),
    os.path.expanduser("~/Podcasts"),
)

# How that folder is named *to the user*.  /storage/emulated/0 and /sdcard are
# the same thing a file manager calls "Internal storage", and showing the raw
# path once had the user believing the download location had changed when it
# had not.  Nothing user-facing may print a filesystem path.
STORAGE_LOCATION_TEXT = "Internal storage > Podcasts > <channel>"

YOUTUBE_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/\S+|youtu\.be/\S+)", re.IGNORECASE
)

TERMUX_URL_OPENER = "/data/data/com.termux/files/home/bin/termux-url-opener"

# Termux's RUN_COMMAND service: the download starts with no app chooser in
# sight.  It needs three things on the phone, none of which this app can set:
# the com.termux.permission.RUN_COMMAND permission (declared in buildozer.spec
# and granted by the user), `allow-external-apps = true` in termux.properties
# (termux/setup.sh writes it), and - because targetSdk is 30+ - com.termux in
# the manifest's <queries> element, or the package manager hides Termux from us
# entirely.  See app/extra_manifest.xml.
TERMUX_PACKAGE = "com.termux"
TERMUX_RUN_COMMAND_SERVICE = "com.termux.app.RunCommandService"
TERMUX_RUN_COMMAND_ACTION = "com.termux.RUN_COMMAND"
# Termux accepts the intent and then opens its own window, which pushes this app
# into the background.  If that has not happened this many seconds later,
# nothing came up and the user is told what to do about it.
TERMUX_SILENT_AFTER = 12.0

# Player. SKIP_MS is the classic podcast jump; PROGRESS_TICK drives the
# position readout and the scrub bar (twice a second keeps the bar smooth
# without polling the MediaPlayer hard).
SKIP_MS = 10000
PROGRESS_TICK = 0.5
# MediaPlayer has no completion callback here (a Java listener would need a
# pyjnius proxy class), so completion is detected by polling: stopped, and
# within this much of the end.  One tick's worth of slack covers the gap
# between the last position we read and the real end of the file.
PLAYBACK_END_SLACK_MS = 1500

DEFAULT_SETTINGS = {
    "audio_format": "m4a",
    "default_folder": "General",
    "auto_download_on_share": True,
    # Deliberately a new key rather than a new default for "one_tap_termux":
    # phones that already ran v3.0.0 have the old key saved as false, and a
    # changed default would never reach them.
    "termux_direct_launch": True,
}

CHANNELS_FILE = None
SETTINGS_FILE = None


def get_data_dir():
    try:
        from android.storage import app_storage_path

        return app_storage_path()
    except ImportError:
        return os.path.expanduser("~/.youtube_podcasts")


def safe_snackbar(text):
    try:
        from kivymd.uix.snackbar import Snackbar

        Snackbar(text=str(text)).open()
    except Exception:
        pass


def find_podcast_dir():
    """First shared-storage Podcasts folder that exists."""
    for path in PODCAST_DIR_CANDIDATES:
        try:
            if os.path.isdir(path):
                return path
        except Exception:
            continue
    return PODCAST_DIR_CANDIDATES[0]


def human_duration(seconds):
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_clock(milliseconds):
    """Milliseconds -> '12:04' / '1:01:11'.  Never raises, never returns ''.

    Unlike human_duration() this is for a *live* readout, so 0 has to render as
    '0:00' rather than disappearing.
    """
    try:
        total = int(milliseconds or 0) // 1000
    except (TypeError, ValueError):
        total = 0
    if total < 0:
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def human_size(num_bytes):
    try:
        num_bytes = int(num_bytes or 0)
    except (TypeError, ValueError):
        return ""
    if num_bytes <= 0:
        return ""
    return f"{num_bytes / 1048576:.0f} MB"


def find_sidecar_thumbnail(audio_path):
    """Cover art saved next to the audio, matching its filename."""
    stem = os.path.splitext(audio_path)[0]
    for ext in THUMB_EXTS:
        candidate = stem + ext
        try:
            if os.path.exists(candidate):
                return candidate
        except Exception:
            continue
    return None


KV = '''
MDScreen:
    md_bg_color: app.theme_cls.bg_dark

    MDBoxLayout:
        orientation: "vertical"

        MDTopAppBar:
            title: "YouTube Podcasts"
            elevation: 4
            md_bg_color: app.theme_cls.primary_color

        MDBottomNavigation:
            id: nav
            panel_color: app.theme_cls.primary_color
            selected_color_background: 0, 0, 0, .2
            # NOT 1,1,1,1 - KivyMD 1.2.0 treats exactly that value as "not
            # set" and substitutes theme_cls.primary_color, which is the same
            # purple as panel_color, so the selected tab becomes invisible.
            # An imperceptibly off-white dodges the sentinel.
            text_color_active: .99, .99, .99, 1
            text_color_normal: 1, 1, 1, .6

            # =============== DOWNLOADS ===============
            MDBottomNavigationItem:
                name: "downloads"
                text: "Downloads"
                icon: "playlist-music"

                MDBoxLayout:
                    orientation: "vertical"

                    # --- Now playing ---
                    MDCard:
                        id: now_playing_card
                        orientation: "vertical"
                        size_hint_y: None
                        height: dp(0)
                        opacity: 0
                        disabled: True
                        padding: dp(8)
                        spacing: dp(4)
                        md_bg_color: app.theme_cls.bg_darkest
                        radius: [dp(10),]

                        # Artwork + title/status
                        MDBoxLayout:
                            orientation: "horizontal"
                            spacing: dp(10)
                            size_hint_y: None
                            height: dp(72)

                            Image:
                                id: now_playing_art
                                source: app.now_playing_art
                                fit_mode: "cover"
                                size_hint_x: None
                                width: dp(72)

                            MDBoxLayout:
                                orientation: "vertical"
                                spacing: dp(2)

                                MDLabel:
                                    text: app.now_playing_title
                                    font_style: "Subtitle2"
                                    shorten: True
                                    shorten_from: "right"
                                    max_lines: 2

                                MDLabel:
                                    text: app.now_playing_status
                                    font_style: "Caption"
                                    theme_text_color: "Hint"

                        # Scrub bar + "12:04 / 15:47" readout
                        MDBoxLayout:
                            orientation: "horizontal"
                            spacing: dp(4)
                            size_hint_y: None
                            height: dp(32)

                            MDSlider:
                                id: progress_slider
                                min: 0
                                max: 1
                                value: 0
                                hint: False
                                show_off: False
                                on_active: app.on_seek_active(self.active)

                            MDLabel:
                                # adaptive_width, not a fixed one: "1:04:12 /
                                # 1:23:45" must not be clipped, and an empty
                                # readout must not steal room from the bar.
                                id: now_playing_time_label
                                text: app.now_playing_time
                                font_style: "Caption"
                                theme_text_color: "Hint"
                                halign: "right"
                                valign: "center"
                                adaptive_width: True

                        # Transport
                        MDBoxLayout:
                            orientation: "horizontal"
                            size_hint_y: None
                            height: dp(48)

                            MDIconButton:
                                icon: "rewind-10"
                                on_release: app.skip_back()

                            MDIconButton:
                                icon: app.play_icon
                                on_release: app.toggle_play()

                            MDIconButton:
                                icon: "fast-forward-10"
                                on_release: app.skip_forward()

                            MDIconButton:
                                icon: "stop"
                                on_release: app.stop_playback()

                            MDIconButton:
                                icon: "export-variant"
                                on_release: app.export_selected()

                            MDIconButton:
                                icon: "delete"
                                theme_text_color: "Custom"
                                text_color: 0.9, 0.35, 0.35, 1
                                on_release: app.confirm_delete_selected()

                    MDBoxLayout:
                        orientation: "horizontal"
                        adaptive_height: True
                        size_hint_y: None
                        height: dp(40)
                        padding: dp(12), 0, dp(12), 0

                        MDLabel:
                            text: app.library_summary
                            font_style: "Caption"
                            theme_text_color: "Hint"

                        MDIconButton:
                            icon: "refresh"
                            on_release: app.load_episodes()

                    ScrollView:
                        MDList:
                            id: episode_list

            # =============== ADD / CHANNELS ===============
            MDBottomNavigationItem:
                name: "add"
                text: "Add"
                icon: "plus-circle"

                ScrollView:
                    MDBoxLayout:
                        orientation: "vertical"
                        padding: dp(16)
                        spacing: dp(12)
                        adaptive_height: True

                        MDLabel:
                            text: "Download Single Video"
                            font_style: "Subtitle1"
                            adaptive_height: True

                        MDLabel:
                            text: "Or just share a YouTube link to this app."
                            font_style: "Caption"
                            theme_text_color: "Hint"
                            adaptive_height: True

                        MDTextField:
                            id: url_input
                            hint_text: "Paste YouTube video link"
                            mode: "rectangle"
                            size_hint_x: 1

                        MDBoxLayout:
                            orientation: "horizontal"
                            spacing: dp(8)
                            adaptive_height: True
                            size_hint_y: None
                            height: dp(44)

                            MDFlatButton:
                                text: "PASTE"
                                on_release: app.paste_from_clipboard()
                                size_hint_x: 0.35

                            MDRaisedButton:
                                text: "DOWNLOAD"
                                on_release: app.download_single_video()
                                size_hint_x: 0.65

                        MDLabel:
                            id: status_label
                            text: app.status_text
                            theme_text_color: "Secondary"
                            adaptive_height: True
                            font_style: "Caption"

                        MDSeparator:

                        MDBoxLayout:
                            orientation: "horizontal"
                            adaptive_height: True
                            size_hint_y: None
                            height: dp(40)

                            MDLabel:
                                text: "Your Channels"
                                font_style: "Subtitle1"

                            MDRaisedButton:
                                text: "+ ADD"
                                on_release: app.show_add_channel()
                                size_hint_x: None
                                width: dp(100)

                        # No adaptive_height here: it binds height to
                        # minimum_height, which would fight the explicit height
                        # that show_add_channel/hide_add_channel set.
                        MDBoxLayout:
                            id: add_channel_box
                            orientation: "vertical"
                            opacity: 0
                            disabled: True
                            size_hint_y: None
                            height: dp(0)

                            MDTextField:
                                id: channel_url_input
                                hint_text: "Channel URL (e.g. youtube.com/@natebjones)"
                                mode: "rectangle"
                                size_hint_x: 1

                            MDTextField:
                                id: channel_name_input
                                hint_text: "Channel name (e.g. Nate B Jones)"
                                mode: "rectangle"
                                size_hint_x: 1

                            MDBoxLayout:
                                orientation: "horizontal"
                                spacing: dp(8)
                                adaptive_height: True
                                size_hint_y: None
                                height: dp(44)

                                MDFlatButton:
                                    text: "CANCEL"
                                    on_release: app.hide_add_channel()
                                    size_hint_x: 0.5

                                MDRaisedButton:
                                    text: "ADD CHANNEL"
                                    on_release: app.add_channel()
                                    size_hint_x: 0.5

                        MDList:
                            id: channel_list

            # =============== SETTINGS ===============
            MDBottomNavigationItem:
                name: "settings"
                text: "Settings"
                icon: "cog"

                ScrollView:
                    MDBoxLayout:
                        orientation: "vertical"
                        padding: dp(16)
                        spacing: dp(10)
                        adaptive_height: True

                        MDLabel:
                            text: "Audio format"
                            font_style: "Subtitle1"
                            adaptive_height: True

                        MDBoxLayout:
                            orientation: "horizontal"
                            spacing: dp(8)
                            adaptive_height: True
                            size_hint_y: None
                            height: dp(44)

                            MDRaisedButton:
                                text: "M4A"
                                on_release: app.set_audio_format("m4a")
                                size_hint_x: 0.5

                            MDRaisedButton:
                                text: "MP3"
                                on_release: app.set_audio_format("mp3")
                                size_hint_x: 0.5

                        MDLabel:
                            text: app.format_help
                            font_style: "Caption"
                            theme_text_color: "Hint"
                            adaptive_height: True

                        MDSeparator:

                        MDLabel:
                            text: "Episodes"
                            font_style: "Subtitle1"
                            adaptive_height: True

                        MDLabel:
                            text: app.storage_help
                            font_style: "Caption"
                            theme_text_color: "Hint"
                            adaptive_height: True

                        MDRaisedButton:
                            text: "GRANT FILE ACCESS"
                            on_release: app.request_all_files_access()
                            size_hint_x: 1

                        MDSeparator:

                        MDBoxLayout:
                            orientation: "horizontal"
                            adaptive_height: True
                            size_hint_y: None
                            height: dp(56)

                            # KivyMD 1.2.0's switch thumb overhangs its own
                            # widget by dp(20) when the switch is on, so
                            # without this the thumb runs off the screen edge.
                            padding: 0, 0, dp(24), 0

                            MDLabel:
                                text: "Download shared links"
                                font_style: "Body2"

                            MDSwitch:
                                id: auto_share_switch
                                active: app.auto_download_on_share
                                on_active: app.set_auto_download(self.active)
                                pos_hint: {"center_y": .5}

                        MDBoxLayout:
                            orientation: "horizontal"
                            adaptive_height: True
                            size_hint_y: None
                            height: dp(56)

                            padding: 0, 0, dp(24), 0

                            MDLabel:
                                text: "Start Termux directly"
                                font_style: "Body2"

                            MDSwitch:
                                id: termux_direct_switch
                                active: app.termux_direct_launch
                                on_active: app.set_termux_direct(self.active)
                                pos_hint: {"center_y": .5}

                        MDLabel:
                            text: app.termux_direct_help
                            font_style: "Caption"
                            theme_text_color: "Hint"
                            adaptive_height: True
'''


class YouTubePodcastApp(MDApp):
    status_text = StringProperty("Ready")
    library_summary = StringProperty("No episodes yet")
    now_playing_title = StringProperty("")
    now_playing_status = StringProperty("")
    now_playing_art = StringProperty("")
    now_playing_time = StringProperty("")
    play_icon = StringProperty("play")
    format_help = StringProperty("")
    storage_help = StringProperty("")
    termux_direct_help = StringProperty("")
    is_downloading = BooleanProperty(False)
    auto_download_on_share = BooleanProperty(True)
    termux_direct_launch = BooleanProperty(True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._player = None
        self._playing_path = None
        # Player state. _progress_event is the Clock interval driving the
        # readout; it must never outlive self._player (a tick against a
        # released MediaPlayer crashes the app).
        self._progress_event = None
        self._duration_ms = 0
        self._seeking = False
        self._selected = None
        self._episodes = []
        self._settings = dict(DEFAULT_SETTINGS)
        self._handled_urls = set()
        self._dialog = None
        # When the last download was handed to Termux (see
        # _watch_for_silent_termux); None once the check has been made.
        self._termux_handoff_at = None
        # Why the chooser had to be used, in the user's words.
        self._termux_fallback_reason = "Termux would not start on its own"
        self._podcast_dir = find_podcast_dir()
        self._episodes_lock = threading.Lock()
        # Resume state (see the LIFECYCLE notes below).  _resume_redraw_event is
        # the Clock interval that keeps asking for a repaint after a wakeup.
        self._paused = False
        self._resume_redraw_event = None
        self._resume_redraws_left = 0

    # === LIFECYCLE ===
    #
    # Coming back from the background used to leave the whole screen black
    # until the app was force-stopped.  Android destroys the SDL/EGL surface
    # whenever the activity is backgrounded, and Kivy 2.3.1's SDL2 path does
    # nothing to recover from it:
    #
    #   * WindowSDL._event_filter() handles 'app_didenterforeground' by
    #     dispatching on_resume and nothing else -- no repaint, no reload.
    #   * WindowBase.create_window() is where Kivy reloads GL resources after a
    #     window is recreated (get_context().reload()), but on Android it is
    #     unbound after the first call (`if platform in ('android'):
    #     self._unbind_create_window()`), so it never runs a second time.
    #   * EventLoop.idle() only paints when `window.canvas.needs_redraw` is set,
    #     and on the way back nothing marks the canvas dirty -- so the brand new
    #     (and therefore undefined, i.e. black) EGL surface is never drawn into.
    #
    # Both halves of the fix below are Kivy's own remedies; they simply were
    # never wired up for the SDL2 bootstrap.  Everything is deferred to the
    # Clock because on_resume is dispatched from inside SDL's event filter,
    # before the mainloop is running again and before Android has necessarily
    # handed SDL the replacement surface.

    # Kivy's android hook (kivy/support.py) repaints at 5 fps for 5 seconds
    # after a wakeup: "after wakeup, we need to redraw more than once,
    # otherwise we get a black screen".  Same rate, shorter tail.
    RESUME_REDRAW_HZ = 5.0
    RESUME_REDRAW_SECONDS = 3.0
    # How long to wait before re-reading the library, so the repaint goes first.
    RESUME_DATA_DELAY = 0.5

    def build(self):
        global CHANNELS_FILE, SETTINGS_FILE
        try:
            data_dir = get_data_dir()
            os.makedirs(data_dir, exist_ok=True)
            CHANNELS_FILE = os.path.join(data_dir, "channels.json")
            SETTINGS_FILE = os.path.join(data_dir, "settings.json")
        except Exception as e:
            CHANNELS_FILE = None
            SETTINGS_FILE = None
            log_crash(type(e), e, e.__traceback__)

        self._load_settings()
        self._disable_strict_mode()

        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "DeepPurple"

        return Builder.load_string(KV)

    def on_start(self):
        try:
            from android.permissions import Permission, request_permissions

            perms = [Permission.INTERNET]
            for name in ("READ_MEDIA_AUDIO", "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE"):
                try:
                    perms.append(getattr(Permission, name))
                except AttributeError:
                    pass
            request_permissions(perms)
        except Exception:
            pass
        Clock.schedule_once(lambda dt: self._safe_load(), 1.0)

    def _safe_load(self):
        for step in (self.load_channels, self.load_episodes, self._refresh_help_text):
            try:
                step()
            except Exception as e:
                log_crash(type(e), e, e.__traceback__)
        try:
            self._handle_android_intent()
        except Exception:
            pass

    def on_pause(self):
        """Returning True lets Android background us instead of killing us."""
        self._paused = True
        try:
            self._cancel_resume_redraw()
        except Exception:
            pass
        return True

    def on_resume(self):
        """Dispatched from inside SDL's event filter: schedule, never work.

        Anything done here runs before the mainloop is going again and possibly
        before the new surface exists -- including any texture upload, which
        would land on a dead GL context.  So the only job is to book a callback
        for the first real frame.
        """
        self._paused = False
        try:
            Clock.schedule_once(self._restore_after_resume, 0)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            # Clock unusable (should not happen): better inline than black.
            self._restore_after_resume()

    def _restore_after_resume(self, *_dt):
        """First frame back: rebuild GL state, repaint, then re-read the disk."""
        try:
            self._reload_gl_state()
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
        try:
            self._start_resume_redraw()
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
        # The disk walk and widget rebuild go last, on their own frame: they
        # must never delay the repaint, and must never be able to stop it by
        # raising.  (They used to be the whole of on_resume.)
        try:
            Clock.schedule_once(self._reload_data_after_resume, self.RESUME_DATA_DELAY)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self._reload_data_after_resume()

    def _reload_gl_state(self):
        """Re-upload every texture, shader and VBO, and re-issue the viewport.

        This is what WindowBase.create_window() does for a recreated window;
        Android never reaches that code, so we do it here.  It is the call that
        drives Kivy's reload observers, which is how artwork (textures with a
        source) and every label and icon (textures with a fill callback) get
        themselves back onto a GL context that may be brand new.
        """
        try:
            from kivy.core.window import Window
            from kivy.graphics.context import get_context
        except Exception:
            return
        try:
            get_context().reload()
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
        # Each of these is useful on its own, so none may block the others.
        for step in (
            lambda: get_context().flag_update_canvas(),
            lambda: Window.update_viewport(),
            lambda: Window.canvas.ask_update(),
        ):
            try:
                step()
            except Exception:
                pass

    def _start_resume_redraw(self):
        """Ask for a repaint now, and keep asking for a few seconds.

        One ask_update() is not enough.  Android hands SDL the replacement
        surface some frames after onResume, and that surface is double
        buffered, so a single frame only fills one of the two buffers.
        """
        self._resume_redraws_left = max(
            1, int(self.RESUME_REDRAW_SECONDS * self.RESUME_REDRAW_HZ)
        )
        self._cancel_resume_redraw()
        if self._ask_redraw() is False:
            return
        try:
            self._resume_redraw_event = Clock.schedule_interval(
                self._ask_redraw, 1.0 / self.RESUME_REDRAW_HZ
            )
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def _ask_redraw(self, *_dt):
        """Mark the window canvas dirty so EventLoop.idle() actually paints."""
        try:
            from kivy.core.window import Window

            Window.canvas.ask_update()
        except Exception:
            return False
        self._resume_redraws_left -= 1
        if self._paused or self._resume_redraws_left <= 0:
            self._resume_redraw_event = None
            return False  # unschedules the interval
        return True

    def _cancel_resume_redraw(self):
        event, self._resume_redraw_event = self._resume_redraw_event, None
        if event is not None:
            try:
                event.cancel()
            except Exception:
                pass

    def _reload_data_after_resume(self, *_dt):
        """Re-read the library, once the surface is safely back."""
        if self._paused:  # backgrounded again before this fired
            return
        for step in (self.load_channels, self.load_episodes):
            try:
                step()
            except Exception as e:
                log_crash(type(e), e, e.__traceback__)
        try:
            from kivy.core.window import Window

            Window.canvas.ask_update()
        except Exception:
            pass

    def on_stop(self):
        try:
            self._cancel_resume_redraw()
        except Exception:
            pass
        self.stop_playback()

    def _disable_strict_mode(self):
        """Needed for file:// URIs in share intents on Android 7+."""
        try:
            from jnius import autoclass

            StrictMode = autoclass("android.os.StrictMode")
            VmPolicyBuilder = autoclass("android.os.StrictMode$VmPolicy$Builder")
            StrictMode.setVmPolicy(VmPolicyBuilder().build())
        except Exception:
            pass

    # === SETTINGS ===

    def _load_settings(self):
        data = {}
        if SETTINGS_FILE and os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
            except Exception:
                data = {}
        self._settings = dict(DEFAULT_SETTINGS)
        self._settings.update(data)
        # v3.0.0 wrote this key with a false default nobody ever chose.
        self._settings.pop("one_tap_termux", None)
        self.auto_download_on_share = bool(self._settings.get("auto_download_on_share", True))
        self.termux_direct_launch = bool(self._settings.get("termux_direct_launch", True))

    def _save_settings(self):
        if not SETTINGS_FILE:
            return
        try:
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._settings, f, indent=2)
            os.replace(tmp, SETTINGS_FILE)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def set_audio_format(self, fmt):
        try:
            if fmt not in ("m4a", "mp3", "keep"):
                return
            self._settings["audio_format"] = fmt
            self._save_settings()
            self._refresh_help_text()
            safe_snackbar(f"Downloads will use {fmt.upper()}")
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def set_auto_download(self, active):
        try:
            self.auto_download_on_share = bool(active)
            self._settings["auto_download_on_share"] = bool(active)
            self._save_settings()
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def set_termux_direct(self, active):
        try:
            self.termux_direct_launch = bool(active)
            self._settings["termux_direct_launch"] = bool(active)
            self._save_settings()
            self._refresh_help_text()
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def _refresh_help_text(self):
        """One line per setting: enough to choose, no more. (README has the why.)"""
        fmt = self._settings.get("audio_format", "m4a")
        if fmt == "mp3":
            self.format_help = "MP3: re-encoded on the phone. Slower, slightly worse."
        else:
            self.format_help = "M4A: YouTube's audio as sent. Faster, better."
        self.storage_help = STORAGE_LOCATION_TEXT
        if self.termux_direct_launch:
            self.termux_direct_help = "Downloads start in Termux without asking."
        else:
            self.termux_direct_help = "You pick Termux from the share menu each time."

    # === EPISODE LIBRARY ===

    def _scan_folder(self, folder_path, channel_name):
        """Episodes in one channel folder, manifest first then loose files."""
        episodes = []
        seen = set()

        manifest_path = os.path.join(folder_path, MANIFEST_NAME)
        entries = []
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, list):
                        entries = loaded
            except Exception:
                entries = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rel = entry.get("relpath") or entry.get("filename")
            if not rel:
                continue
            audio_path = os.path.normpath(os.path.join(folder_path, rel))
            if not os.path.exists(audio_path):
                continue
            seen.add(os.path.abspath(audio_path))
            thumb_rel = entry.get("thumbnail_relpath") or entry.get("thumbnail")
            thumb = None
            if thumb_rel:
                candidate = os.path.normpath(os.path.join(folder_path, thumb_rel))
                if os.path.exists(candidate):
                    thumb = candidate
            episodes.append(
                {
                    "id": entry.get("id", ""),
                    "title": entry.get("title") or os.path.splitext(os.path.basename(audio_path))[0],
                    "path": audio_path,
                    "thumb": thumb or find_sidecar_thumbnail(audio_path),
                    "duration": entry.get("duration", 0),
                    "filesize": entry.get("filesize", 0),
                    "channel": channel_name,
                    "manifest": manifest_path,
                    "downloaded_at": entry.get("downloaded_at", ""),
                }
            )

        # Anything downloaded before manifests existed, or dropped in by hand.
        for root, _dirs, files in os.walk(folder_path):
            for name in sorted(files):
                if not name.lower().endswith(AUDIO_EXTS):
                    continue
                audio_path = os.path.join(root, name)
                if os.path.abspath(audio_path) in seen:
                    continue
                try:
                    size = os.path.getsize(audio_path)
                except OSError:
                    size = 0
                episodes.append(
                    {
                        "id": "",
                        "title": os.path.splitext(name)[0],
                        "path": audio_path,
                        "thumb": find_sidecar_thumbnail(audio_path),
                        "duration": 0,
                        "filesize": size,
                        "channel": channel_name,
                        "manifest": manifest_path,
                        "downloaded_at": "",
                    }
                )
        return episodes

    def _channel_display_names(self):
        """Map each folder to the channel name the user typed in the Add tab.

        The folder is a sanitised, machine-made string ("AI_News_NateBJones").
        Showing that in the list is both ugly and long enough to push the
        duration and size off the end of the line.
        """
        names = {}
        try:
            for channel in self._read_channels():
                folder = (channel.get("folder") or "").strip()
                name = (channel.get("name") or "").strip()
                if folder and name:
                    names[folder] = name
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
        return names

    def _collect_episodes(self):
        base = self._podcast_dir
        episodes = []
        try:
            folders = sorted(os.listdir(base))
        except Exception:
            return episodes, True  # unreadable -> probably a permission problem

        display_names = self._channel_display_names()
        for folder in folders:
            folder_path = os.path.join(base, folder)
            try:
                if not os.path.isdir(folder_path):
                    continue
                episodes.extend(self._scan_folder(folder_path, display_names.get(folder, folder)))
            except Exception as e:
                log_crash(type(e), e, e.__traceback__)
        episodes.sort(key=lambda e: (e.get("downloaded_at") or "", e.get("title") or ""), reverse=True)
        return episodes, False

    def load_episodes(self):
        """Rebuild the Downloads list from what is on disk."""
        try:
            from kivymd.uix.list import ImageLeftWidget, IconLeftWidget, OneLineListItem, TwoLineAvatarListItem
        except ImportError:
            return

        if not self.root or "episode_list" not in self.root.ids:
            return

        with self._episodes_lock:
            episodes, unreadable = self._collect_episodes()
            self._episodes = episodes

        episode_list = self.root.ids.episode_list
        episode_list.clear_widgets()

        if unreadable:
            self.library_summary = "Cannot read the Podcasts folder"
            try:
                episode_list.add_widget(
                    OneLineListItem(
                        text="Tap Settings > GRANT FILE ACCESS",
                        theme_text_color="Hint",
                    )
                )
            except Exception:
                pass
            return

        if not episodes:
            self.library_summary = "No episodes yet"
            try:
                episode_list.add_widget(
                    OneLineListItem(
                        text="Nothing downloaded yet. Share a link to this app.",
                        theme_text_color="Hint",
                    )
                )
            except Exception:
                pass
            return

        total = sum(e.get("filesize") or 0 for e in episodes)
        self.library_summary = f"{len(episodes)} episodes | {human_size(total) or '0 MB'}"

        for episode in episodes:
            try:
                details = " | ".join(
                    part
                    for part in (
                        episode.get("channel", ""),
                        human_duration(episode.get("duration")),
                        human_size(episode.get("filesize")),
                    )
                    if part
                )
                item = TwoLineAvatarListItem(
                    text=episode.get("title", "Unknown"),
                    secondary_text=details or "Tap to play",
                )
                thumb = episode.get("thumb")
                if thumb:
                    item.add_widget(ImageLeftWidget(source=thumb))
                else:
                    item.add_widget(IconLeftWidget(icon="music-note"))
                # Default arg binds this episode now, not the last one in the loop.
                item.bind(on_release=lambda widget, ep=episode: self.select_episode(ep))
                episode_list.add_widget(item)
            except Exception as e:
                log_crash(type(e), e, e.__traceback__)
                continue

    # === PLAYER ===

    def select_episode(self, episode):
        """Tapping an episode selects it, shows its artwork, and plays it."""
        try:
            self._selected = episode
            self.now_playing_title = episode.get("title", "")
            self.now_playing_art = episode.get("thumb") or ""
            self._show_now_playing(True)
            self.play_episode(episode)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def _show_now_playing(self, visible):
        try:
            card = self.root.ids.now_playing_card
            card.height = dp(180) if visible else dp(0)
            card.opacity = 1 if visible else 0
            card.disabled = not visible
        except Exception:
            pass
        if not visible:
            # The card is gone; nothing may keep ticking behind it.
            self._stop_progress_updates()
            self.now_playing_time = ""
            self._duration_ms = 0
            self._reset_slider()

    def play_episode(self, episode):
        """Start playback. Re-selecting the same file toggles pause/resume."""
        path = episode.get("path")
        if not path or not os.path.exists(path):
            safe_snackbar("File not found")
            return
        try:
            from jnius import autoclass

            MediaPlayer = autoclass("android.media.MediaPlayer")
            AudioManager = autoclass("android.media.AudioManager")

            if self._playing_path == path and self._player:
                self.toggle_play()
                return

            # Releases the old player *and* cancels its Clock interval.
            self._release_player()

            player = MediaPlayer()
            player.setAudioStreamType(AudioManager.STREAM_MUSIC)
            player.setDataSource(path)
            player.prepare()
            player.start()
            self._player = player
            self._playing_path = path
            self._seeking = False
            self.play_icon = "pause"
            self.now_playing_status = "Playing"
            self.status_text = f"Playing: {os.path.basename(path)[:40]}"
            self._duration_ms = 0
            self._refresh_progress()
            self._start_progress_updates()
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self.now_playing_status = "Could not play this file"
            safe_snackbar("Could not play audio")

    def toggle_play(self):
        try:
            if not self._player:
                if self._selected:
                    self.play_episode(self._selected)
                return
            if self._player.isPlaying():
                self._player.pause()
                self.play_icon = "play"
                self.now_playing_status = "Paused"
                # Show where we stopped, then stop polling.
                self._refresh_progress()
                self._stop_progress_updates()
            else:
                self._player.start()
                self.play_icon = "pause"
                self.now_playing_status = "Playing"
                self._start_progress_updates()
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self._release_player()

    def stop_playback(self):
        try:
            self._release_player()
            self.play_icon = "play"
            self.now_playing_status = "Stopped"
            # Back to the start of the (still selected) episode.
            self._show_progress(0, self._duration_ms)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def _release_player(self):
        # Order matters: the interval has to go before the MediaPlayer does,
        # or a tick can land on a released player and take the app down.
        self._stop_progress_updates()
        self._seeking = False
        if self._player:
            try:
                self._player.stop()
            except Exception:
                pass
            try:
                self._player.release()
            except Exception:
                pass
        self._player = None
        self._playing_path = None

    # --- Position readout, skipping and scrubbing --------------------------

    def skip_back(self):
        """Jump back 10 seconds (clamped at the start of the episode)."""
        try:
            self.seek_by(-SKIP_MS)
        except Exception as e:  # pragma: no cover - seek_by is already guarded
            log_crash(type(e), e, e.__traceback__)

    def skip_forward(self):
        """Jump forward 10 seconds (clamped at the end of the episode)."""
        try:
            self.seek_by(SKIP_MS)
        except Exception as e:  # pragma: no cover - seek_by is already guarded
            log_crash(type(e), e, e.__traceback__)

    def seek_by(self, delta_ms):
        """Move the play position by delta_ms, clamped to [0, duration]."""
        try:
            if not self._player:
                return
            try:
                delta = int(delta_ms)
            except (TypeError, ValueError):
                return
            position = self._player_position()
            if position is None:
                return
            self.seek_to(position + delta)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def seek_to(self, position_ms):
        """Seek to an absolute position in milliseconds, clamped to the track."""
        try:
            if not self._player:
                return
            try:
                target = int(position_ms)
            except (TypeError, ValueError):
                return
            duration = self._player_duration()
            if target < 0:
                target = 0
            if duration > 0 and target > duration:
                target = duration
            self._player.seekTo(target)
            self._show_progress(target, duration)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def on_seek_active(self, active):
        """MDSlider.active: True while a finger holds the thumb, False on release.

        Position updates are suspended while dragging so the Clock does not
        yank the thumb back, and the seek happens once, on release.
        """
        try:
            if active:
                self._seeking = True
                return
            self._seeking = False
            slider = self._progress_slider()
            if slider is None:
                return
            self.seek_to(float(slider.value) * 1000.0)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def _progress_slider(self):
        try:
            return self.root.ids.progress_slider
        except Exception:
            return None

    def _player_position(self):
        """Position in ms, or None when there is no player to ask."""
        if not self._player:
            return None
        try:
            return max(0, int(self._player.getCurrentPosition()))
        except Exception:
            return None

    def _player_duration(self):
        """Track length in ms (0 when unknown). Cached: it never changes."""
        if self._duration_ms > 0 or not self._player:
            return self._duration_ms
        try:
            value = int(self._player.getDuration())
        except Exception:
            return self._duration_ms
        if value > 0:
            self._duration_ms = value
        return self._duration_ms

    def _is_playing(self):
        if not self._player:
            return False
        try:
            return bool(self._player.isPlaying())
        except Exception:
            return False

    def _start_progress_updates(self):
        self._stop_progress_updates()
        try:
            self._progress_event = Clock.schedule_interval(
                self._tick_progress, PROGRESS_TICK
            )
        except Exception as e:
            self._progress_event = None
            log_crash(type(e), e, e.__traceback__)

    def _stop_progress_updates(self):
        """Cancel the readout interval. Safe to call any number of times."""
        event = self._progress_event
        self._progress_event = None
        if event is None:
            return
        try:
            event.cancel()
        except Exception:
            try:
                Clock.unschedule(event)
            except Exception:
                pass

    def _tick_progress(self, *args):
        """Clock callback. Returns False to unschedule itself when done."""
        try:
            if not self._player:
                self._stop_progress_updates()
                return False
            position = self._player_position()
            if position is None:
                # The player is gone or unusable - never poll it again.
                self._stop_progress_updates()
                return False
            duration = self._player_duration()
            if not self._is_playing():
                if duration > 0 and position >= duration - PLAYBACK_END_SLACK_MS:
                    self._playback_finished(duration)
                    return False
                # Paused (or a stall): show the position, keep waiting.
            self._show_progress(position, duration)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self._stop_progress_updates()
            return False
        return True

    def _refresh_progress(self):
        """Update the readout once, right now."""
        position = self._player_position()
        self._show_progress(position or 0, self._player_duration())

    def _playback_finished(self, duration):
        """The episode ran to the end: stop claiming it is playing."""
        self._stop_progress_updates()
        self.play_icon = "play"
        self.now_playing_status = "Finished"
        self._show_progress(duration, duration)

    def _show_progress(self, position_ms, duration_ms):
        """Push a position into the label and the scrub bar."""
        try:
            if duration_ms and duration_ms > 0:
                self.now_playing_time = (
                    f"{format_clock(position_ms)} / {format_clock(duration_ms)}"
                )
            else:
                self.now_playing_time = format_clock(position_ms)
            self._update_slider(position_ms, duration_ms)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def _update_slider(self, position_ms, duration_ms):
        slider = self._progress_slider()
        if slider is None:
            return
        try:
            if self._seeking or slider.active:
                return  # never fight the finger
            duration_s = (duration_ms or 0) / 1000.0
            slider.max = duration_s if duration_s > 0 else 1
            position_s = max(0.0, (position_ms or 0) / 1000.0)
            slider.value = min(position_s, slider.max)
        except Exception:
            pass

    def _reset_slider(self):
        slider = self._progress_slider()
        if slider is None:
            return
        try:
            slider.value = 0
            slider.max = 1
        except Exception:
            pass

    # === EXPORT / DELETE ===

    def export_selected(self):
        """Hand the file to another app - Drive, email, whatever you use."""
        episode = self._selected
        if not episode:
            safe_snackbar("Select an episode first")
            return
        path = episode.get("path")
        if not path or not os.path.exists(path):
            safe_snackbar("File not found")
            return
        # Deliberately on the main thread. This only builds an Intent - there is
        # no file copying to get off the UI thread - and pyjnius needs an
        # explicit attach/detach to be used from a worker thread at all.
        self._start_export_intent(path)

    def _start_export_intent(self, path):
        try:
            from jnius import autoclass, cast

            Intent = autoclass("android.content.Intent")
            String = autoclass("java.lang.String")
            File = autoclass("java.io.File")
            Uri = autoclass("android.net.Uri")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")

            mime = MIME_BY_EXT.get(os.path.splitext(path)[1].lower(), "audio/*")

            intent = Intent()
            intent.setAction(Intent.ACTION_SEND)
            intent.setType(String(mime))
            intent.putExtra(Intent.EXTRA_STREAM, cast("android.os.Parcelable", Uri.fromFile(File(String(path)))))
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

            title = String("Save or upload episode")
            chooser = Intent.createChooser(intent, cast("java.lang.CharSequence", title))
            chooser.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            PythonActivity.mActivity.startActivity(chooser)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            safe_snackbar("Could not open share menu")

    def confirm_delete_selected(self):
        episode = self._selected
        if not episode:
            safe_snackbar("Select an episode first")
            return
        try:
            from kivymd.uix.button import MDFlatButton
            from kivymd.uix.dialog import MDDialog

            if self._dialog:
                try:
                    self._dialog.dismiss()
                except Exception:
                    pass
                self._dialog = None

            self._dialog = MDDialog(
                title="Delete episode?",
                text=(
                    f"{episode.get('title', '')}\n\n"
                    "This removes the audio and its cover art from your Podcasts "
                    "folder. It cannot be undone."
                ),
                buttons=[
                    MDFlatButton(text="CANCEL", on_release=lambda *a: self._dismiss_dialog()),
                    MDFlatButton(
                        text="DELETE",
                        theme_text_color="Custom",
                        text_color=(0.9, 0.35, 0.35, 1),
                        on_release=lambda *a, ep=episode: self._delete_confirmed(ep),
                    ),
                ],
            )
            self._dialog.open()
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            # No dialog available - fall back to deleting directly.
            self._delete_confirmed(episode)

    def _dismiss_dialog(self):
        try:
            if self._dialog:
                self._dialog.dismiss()
        except Exception:
            pass
        self._dialog = None

    def _delete_confirmed(self, episode):
        self._dismiss_dialog()
        try:
            self.delete_episode(episode)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def delete_episode(self, episode):
        """Remove the audio, its artwork, and its manifest entry."""
        path = episode.get("path")
        if not path:
            return

        if self._playing_path == path:
            self.stop_playback()

        removed = False
        for target in (path, episode.get("thumb")):
            if not target:
                continue
            try:
                if os.path.exists(target):
                    os.remove(target)
                    removed = True
            except Exception as e:
                log_crash(type(e), e, e.__traceback__)

        manifest = episode.get("manifest")
        if manifest and os.path.exists(manifest):
            with self._episodes_lock:
                try:
                    with open(manifest, "r") as f:
                        entries = json.load(f)
                    if isinstance(entries, list):
                        folder = os.path.dirname(manifest)
                        kept = []
                        for entry in entries:
                            rel = entry.get("relpath") or entry.get("filename") or ""
                            entry_path = os.path.normpath(os.path.join(folder, rel))
                            if os.path.abspath(entry_path) != os.path.abspath(path):
                                kept.append(entry)
                        tmp = manifest + ".tmp"
                        with open(tmp, "w") as f:
                            json.dump(kept, f, indent=2)
                        os.replace(tmp, manifest)
                except Exception as e:
                    log_crash(type(e), e, e.__traceback__)

        if self._selected is episode:
            self._selected = None
            self._show_now_playing(False)
            self.now_playing_title = ""
            self.now_playing_art = ""

        safe_snackbar("Deleted" if removed else "Could not delete - check file access")
        self.load_episodes()

    def request_all_files_access(self):
        """Open the Android screen that grants access to the Podcasts folder."""
        try:
            from jnius import autoclass

            Build = autoclass("android.os.Build$VERSION")
            if Build.SDK_INT < 30:
                safe_snackbar("Storage permission is granted at startup on this Android version")
                return

            Intent = autoclass("android.content.Intent")
            Settings = autoclass("android.provider.Settings")
            String = autoclass("java.lang.String")
            Uri = autoclass("android.net.Uri")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")

            activity = PythonActivity.mActivity
            package_uri = Uri.parse(String("package:" + activity.getPackageName()))
            intent = Intent(
                Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, package_uri
            )
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            activity.startActivity(intent)
            safe_snackbar("Turn on 'Allow access to manage all files'")
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            safe_snackbar("Could not open the permission screen")

    # === SHARE INTENT -> DOWNLOAD ===

    def _handle_android_intent(self):
        try:
            from android import activity

            activity.bind(on_new_intent=self._on_new_intent)
            self._process_intent(activity.getIntent())
        except Exception:
            pass

    def _on_new_intent(self, intent):
        try:
            self._process_intent(intent)
        except Exception:
            pass

    def _process_intent(self, intent):
        """A shared YouTube link starts a download without any further taps."""
        try:
            if not intent:
                return
            action = intent.getAction()
            if action not in ("android.intent.action.SEND", "android.intent.action.VIEW"):
                return

            text = None
            if action == "android.intent.action.VIEW":
                data = intent.getDataString()
                text = data
            else:
                text = intent.getStringExtra("android.intent.extra.TEXT")
                if not text:
                    text = intent.getStringExtra("android.intent.extra.SUBJECT")
            if not text:
                return

            url = self.extract_youtube_url(text)
            if not url:
                return

            # getIntent() keeps returning the same intent after a resume, so
            # without this guard one share would download over and over.
            if url in self._handled_urls:
                return
            self._handled_urls.add(url)

            self.root.ids.url_input.text = url
            if self.auto_download_on_share:
                safe_snackbar("Link received - starting download")
                self.download_single_video()
            else:
                safe_snackbar("Link received! Tap Download.")
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    @staticmethod
    def extract_youtube_url(text):
        """Pull a YouTube link out of shared text, which often has a caption."""
        if not text:
            return None
        text = str(text).strip()
        match = YOUTUBE_URL_RE.search(text)
        if match:
            return match.group(0).rstrip(".,)”\"'")
        if "youtube" in text or "youtu.be" in text:
            return text.split()[0]
        return None

    def paste_from_clipboard(self):
        try:
            from kivy.core.clipboard import Clipboard

            text = Clipboard.paste()
            if text:
                self.root.ids.url_input.text = text.strip()
        except Exception:
            pass

    # === TERMUX ===

    def _termux_payload(self, target, folder=None):
        folder = folder or self._settings.get("default_folder", "General")
        fmt = self._settings.get("audio_format", "m4a")
        return f"{target}|||{folder}|||{fmt}"

    def _send_to_termux(self, payload):
        """Hand the download to Termux.

        Pressing DOWNLOAD must download.  The chooser is a fallback, never the
        first thing the user sees: it only appears when Termux cannot be started
        directly, and when it does the status line says why.

        Returns "direct", "chooser" (the user asked for the menu), "fallback"
        (the direct route was unavailable), or None if nothing could be started.
        """
        if self.termux_direct_launch:
            state = self._termux_service_state()
            if state == "ready" and self._run_command_in_termux(payload):
                self._watch_for_silent_termux()
                return "direct"
            self._termux_fallback_reason = (
                "Termux is not installed, or is hidden from this app"
                if state == "missing"
                else "Termux would not start on its own"
            )
            if self._share_to_termux(payload):
                return "fallback"
            self._report_termux_unreachable()
            return None
        if self._share_to_termux(payload):
            return "chooser"
        self._report_termux_unreachable()
        return None

    def _report_termux_unreachable(self):
        """Last resort: neither route worked, so say so rather than go quiet."""
        try:
            self.status_text = (
                "Could not reach Termux. Install it from F-Droid and run its "
                "setup, then try again."
            )
            safe_snackbar("Could not reach Termux")
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def _termux_service_state(self):
        """Ask the package manager whether Termux will take a RUN_COMMAND intent.

        startForegroundService() reports success as soon as Android has queued
        the intent, even when the target does not exist, so the check has to
        happen first - otherwise a missing or hidden Termux means the user taps
        DOWNLOAD and *nothing* happens, with nothing to explain it.

        "ready"    resolveService() found the service: it exists, it is
                   exported, and package visibility lets us see it.
        "missing"  Termux is not installed, or is invisible to this app (no
                   <queries> element -> getPackageInfo throws NameNotFound).
        "blocked"  Termux is installed but the service did not resolve - too
                   old to have RunCommandService, or disabled.
        "unknown"  Not on Android at all, or the query itself failed.

        Every one of those except "ready" means: use the chooser, which the
        system resolves for us and which package visibility never filters.
        """
        try:
            from jnius import autoclass

            Intent = autoclass("android.content.Intent")
            String = autoclass("java.lang.String")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")

            package_manager = PythonActivity.mActivity.getPackageManager()
            probe = Intent()
            probe.setClassName(String(TERMUX_PACKAGE), String(TERMUX_RUN_COMMAND_SERVICE))
            probe.setAction(String(TERMUX_RUN_COMMAND_ACTION))
            if package_manager.resolveService(probe, 0) is not None:
                return "ready"
            try:
                package_manager.getPackageInfo(String(TERMUX_PACKAGE), 0)
            except Exception:
                # PackageManager.NameNotFoundException, via jnius.JavaException.
                return "missing"
            return "blocked"
        except Exception:
            return "unknown"

    def _run_command_in_termux(self, payload):
        """Termux's RUN_COMMAND service - starts the download with no chooser."""
        try:
            from jnius import autoclass

            Intent = autoclass("android.content.Intent")
            String = autoclass("java.lang.String")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")

            intent = Intent()
            intent.setClassName(String(TERMUX_PACKAGE), String(TERMUX_RUN_COMMAND_SERVICE))
            intent.setAction(String(TERMUX_RUN_COMMAND_ACTION))
            intent.putExtra(String("com.termux.RUN_COMMAND_PATH"), String(TERMUX_URL_OPENER))
            intent.putExtra(String("com.termux.RUN_COMMAND_ARGUMENTS"), [String(payload)])
            intent.putExtra(String("com.termux.RUN_COMMAND_BACKGROUND"), String("false"))
            # 0 = switch to a new session and open Termux, so the download is
            # visible on screen rather than happening invisibly.
            intent.putExtra(String("com.termux.RUN_COMMAND_SESSION_ACTION"), String("0"))
            # Termux quotes these back in its own error notification if it
            # refuses the command, which is the only diagnosis the user gets.
            intent.putExtra(
                String("com.termux.RUN_COMMAND_COMMAND_LABEL"), String("YouTube Podcasts")
            )
            intent.putExtra(
                String("com.termux.RUN_COMMAND_COMMAND_DESCRIPTION"),
                String("Download requested from the YouTube Podcasts app."),
            )

            activity = PythonActivity.mActivity
            try:
                activity.startForegroundService(intent)
            except Exception:
                activity.startService(intent)
            return True
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            return False

    def _watch_for_silent_termux(self):
        """Guard against the one failure Android cannot report back.

        With `allow-external-apps` unset, Termux accepts the intent and drops
        the command; there is no callback, no exception, no result.  What does
        happen when it works is that Termux opens its own window, which pauses
        this app and stops Kivy's clock.  So: if this callback fires roughly on
        time, we were on screen the whole while, nothing came up, and the user
        gets told what to change instead of being left staring at nothing.
        """
        try:
            self._termux_handoff_at = time.monotonic()
            Clock.unschedule(self._termux_silent_hint)
            Clock.schedule_once(self._termux_silent_hint, TERMUX_SILENT_AFTER)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def _termux_silent_hint(self, *_dt):
        try:
            started = getattr(self, "_termux_handoff_at", None)
            if started is None:
                return
            self._termux_handoff_at = None
            if time.monotonic() - started > TERMUX_SILENT_AFTER * 1.5:
                # The clock was frozen: Termux (or something) took the screen.
                return
            self.status_text = (
                "Termux did not open. Turn off 'Start Termux directly' in "
                "Settings to pick it from the menu instead."
            )
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def _handoff_status(self, mode, direct, chooser):
        """What the status line says, per route the hand-off actually took."""
        if mode == "direct":
            return direct
        if mode == "fallback":
            return f"{self._termux_fallback_reason} - pick it from the list."
        return chooser

    def _share_to_termux(self, payload):
        """Send the payload through the Android share chooser."""
        try:
            from jnius import autoclass, cast

            Intent = autoclass("android.content.Intent")
            String = autoclass("java.lang.String")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")

            intent = Intent()
            intent.setAction(Intent.ACTION_SEND)
            intent.setType("text/plain")
            intent.putExtra(Intent.EXTRA_TEXT, String(payload))
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

            title = String("Open with Termux")
            chooser = Intent.createChooser(intent, cast("java.lang.CharSequence", title))
            chooser.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            PythonActivity.mActivity.startActivity(chooser)
            return True
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self.status_text = f"Error: {str(e)[:80]}"
            safe_snackbar("Could not open share menu")
            return False

    def download_single_video(self):
        try:
            url = self.root.ids.url_input.text.strip()
            if not url:
                self.status_text = "Please paste a YouTube link first"
                return

            url = self.extract_youtube_url(url) or url
            if "youtube" not in url and "youtu.be" not in url:
                self.status_text = "Invalid YouTube URL"
                safe_snackbar("That does not look like a YouTube link")
                return

            self.status_text = "Starting the download in Termux..."
            mode = self._send_to_termux(self._termux_payload(url))
            if mode:
                self.root.ids.url_input.text = ""
                self.status_text = self._handoff_status(
                    mode,
                    "Termux is downloading. Pull down Downloads to refresh.",
                    "Pick Termux from the list to start the download.",
                )
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self.status_text = f"Error: {e}"

    def refresh_channel(self, channel_url, channel_folder):
        try:
            self.status_text = f"Refreshing {channel_folder}..."
            payload = self._termux_payload(f"REFRESH:{channel_url}", channel_folder)
            mode = self._send_to_termux(payload)
            if mode:
                self.status_text = self._handoff_status(
                    mode,
                    f"Termux is updating {channel_folder}.",
                    f"Pick Termux from the list to update {channel_folder}.",
                )
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self.status_text = f"Error: {e}"

    # === CHANNEL MANAGEMENT ===

    def _read_channels(self):
        if CHANNELS_FILE and os.path.exists(CHANNELS_FILE):
            try:
                with open(CHANNELS_FILE, "r") as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
            except Exception:
                return []
        return []

    def _save_channels(self, channels):
        if CHANNELS_FILE:
            try:
                tmp = CHANNELS_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(channels, f, indent=2)
                os.replace(tmp, CHANNELS_FILE)
            except Exception as e:
                log_crash(type(e), e, e.__traceback__)

    def _url_to_folder_name(self, url):
        """Extract a folder name from a YouTube channel URL."""
        match = re.search(r"@([a-zA-Z0-9_.-]+)", url)
        if match:
            return match.group(1)
        match = re.search(r"/(?:c|channel)/([a-zA-Z0-9_.-]+)", url)
        if match:
            return match.group(1)
        return re.sub(r"[^a-zA-Z0-9_-]", "_", url.split("/")[-1])[:30] or "channel"

    def show_add_channel(self):
        try:
            box = self.root.ids.add_channel_box
            box.opacity = 1
            box.disabled = False
            box.height = dp(180)
        except Exception:
            pass

    def hide_add_channel(self):
        try:
            box = self.root.ids.add_channel_box
            box.opacity = 0
            box.disabled = True
            box.height = dp(0)
            self.root.ids.channel_url_input.text = ""
            self.root.ids.channel_name_input.text = ""
        except Exception:
            pass

    def add_channel(self):
        try:
            url = self.root.ids.channel_url_input.text.strip()
            name = self.root.ids.channel_name_input.text.strip()

            if not url:
                safe_snackbar("Please enter a channel URL")
                return

            if not url.startswith("http"):
                url = "https://www.youtube.com/" + url.lstrip("/")

            folder = self._url_to_folder_name(url)
            if not name:
                name = folder

            channels = self._read_channels()
            if any(c.get("url") == url for c in channels):
                safe_snackbar("Channel already added!")
                return

            channels.append(
                {
                    "name": name,
                    "url": url,
                    "folder": folder,
                    "added": datetime.now().isoformat(),
                }
            )
            self._save_channels(channels)
            self.hide_add_channel()
            self.load_channels()
            safe_snackbar(f"Added: {name}")
            self.status_text = f"Added channel: {name}"
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            safe_snackbar("Could not add channel")

    def remove_channel(self, url):
        try:
            channels = self._read_channels()
            channels = [c for c in channels if c.get("url") != url]
            self._save_channels(channels)
            self.load_channels()
            safe_snackbar("Channel removed")
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def load_channels(self):
        try:
            from kivymd.uix.list import IconLeftWidget, OneLineListItem, TwoLineAvatarListItem
        except ImportError:
            return

        if not self.root or "channel_list" not in self.root.ids:
            return

        channels = self._read_channels()
        channel_list = self.root.ids.channel_list
        channel_list.clear_widgets()

        if not channels:
            try:
                channel_list.add_widget(
                    OneLineListItem(
                        text="No channels yet. Tap + ADD to add one.",
                        theme_text_color="Hint",
                    )
                )
            except Exception:
                pass
            return

        for ch in channels:
            try:
                name = ch.get("name", "Unknown")
                url = ch.get("url", "")
                folder = ch.get("folder", "")

                icon = IconLeftWidget(icon="podcast")
                icon.bind(on_release=lambda w, u=url, f=folder: self.refresh_channel(u, f))

                item = TwoLineAvatarListItem(
                    text=name,
                    secondary_text="Tap to fetch the latest episodes",
                )
                item.add_widget(icon)
                item.bind(on_release=lambda w, u=url, f=folder: self.refresh_channel(u, f))
                channel_list.add_widget(item)

                remove_item = OneLineListItem(
                    # Plain ASCII on purpose: the Roboto that ships with Kivy
                    # has no glyph for U+2716, so it drew as an empty box.
                    text="      Remove channel",
                    theme_text_color="Custom",
                    text_color=(0.8, 0.3, 0.3, 0.7),
                    on_release=lambda w, u=url: self.remove_channel(u),
                )
                channel_list.add_widget(remove_item)
            except Exception as e:
                log_crash(type(e), e, e.__traceback__)
                continue


if __name__ == "__main__":
    try:
        YouTubePodcastApp().run()
    except Exception:
        log_crash(*sys.exc_info())
        raise
