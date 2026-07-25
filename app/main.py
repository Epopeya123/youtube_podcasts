"""YouTube Podcast Downloader - Android App (KivyMD 1.x)
Version 3.0.0 - Downloads tab with artwork, in-app player, share-to-download

The app is a control panel: Termux does the downloading (it has ffmpeg and
yt-dlp), and writes the audio plus a .episodes.json manifest into shared
storage under /sdcard/Podcasts/<channel>/. This app reads those folders, so
the files stay ordinary files you can browse, upload elsewhere, or delete.
"""

import json
import os
import re
import sys
import threading
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

YOUTUBE_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/\S+|youtu\.be/\S+)", re.IGNORECASE
)

TERMUX_URL_OPENER = "/data/data/com.termux/files/home/bin/termux-url-opener"

DEFAULT_SETTINGS = {
    "audio_format": "m4a",
    "default_folder": "General",
    "auto_download_on_share": True,
    "one_tap_termux": False,
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
<EpisodeRow@TwoLineAvatarListItem>:

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
            text_color_active: 1, 1, 1, 1

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
                        orientation: "horizontal"
                        size_hint_y: None
                        height: dp(0)
                        opacity: 0
                        disabled: True
                        padding: dp(8)
                        spacing: dp(10)
                        md_bg_color: app.theme_cls.bg_darkest
                        radius: [dp(10),]

                        Image:
                            id: now_playing_art
                            source: app.now_playing_art
                            fit_mode: "cover"
                            size_hint_x: None
                            width: dp(104)

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
                                adaptive_height: True

                            MDBoxLayout:
                                orientation: "horizontal"
                                spacing: dp(4)
                                adaptive_height: True

                                MDIconButton:
                                    icon: app.play_icon
                                    on_release: app.toggle_play()

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

                        MDBoxLayout:
                            id: add_channel_box
                            orientation: "vertical"
                            adaptive_height: True
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

                        MDLabel:
                            text: app.format_help
                            font_style: "Caption"
                            theme_text_color: "Hint"
                            adaptive_height: True

                        MDBoxLayout:
                            orientation: "horizontal"
                            spacing: dp(8)
                            adaptive_height: True
                            size_hint_y: None
                            height: dp(44)

                            MDRaisedButton:
                                text: "M4A (FAST)"
                                on_release: app.set_audio_format("m4a")
                                size_hint_x: 0.5

                            MDRaisedButton:
                                text: "MP3"
                                on_release: app.set_audio_format("mp3")
                                size_hint_x: 0.5

                        MDSeparator:

                        MDLabel:
                            text: "Where your episodes are saved"
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

                            MDLabel:
                                text: "Download when a link is shared"
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

                            MDLabel:
                                text: "One-tap Termux launch (skip chooser)"
                                font_style: "Body2"

                            MDSwitch:
                                id: one_tap_switch
                                active: app.one_tap_termux
                                on_active: app.set_one_tap(self.active)
                                pos_hint: {"center_y": .5}

                        MDLabel:
                            text: app.one_tap_help
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
    play_icon = StringProperty("play")
    format_help = StringProperty("")
    storage_help = StringProperty("")
    one_tap_help = StringProperty(
        "Needs 'allow-external-apps = true' in Termux. Falls back to the share "
        "menu if Termux refuses."
    )
    is_downloading = BooleanProperty(False)
    auto_download_on_share = BooleanProperty(True)
    one_tap_termux = BooleanProperty(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._player = None
        self._playing_path = None
        self._selected = None
        self._episodes = []
        self._settings = dict(DEFAULT_SETTINGS)
        self._handled_urls = set()
        self._dialog = None
        self._podcast_dir = find_podcast_dir()
        self._episodes_lock = threading.Lock()

    # === LIFECYCLE ===

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
        return True

    def on_resume(self):
        try:
            self.load_channels()
            self.load_episodes()
        except Exception:
            pass

    def on_stop(self):
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
        self.auto_download_on_share = bool(self._settings.get("auto_download_on_share", True))
        self.one_tap_termux = bool(self._settings.get("one_tap_termux", False))

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

    def set_one_tap(self, active):
        try:
            self.one_tap_termux = bool(active)
            self._settings["one_tap_termux"] = bool(active)
            self._save_settings()
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def _refresh_help_text(self):
        fmt = self._settings.get("audio_format", "m4a")
        if fmt == "mp3":
            self.format_help = (
                "MP3: re-encoded on the phone. Works everywhere, but noticeably "
                "slower to download. Currently: MP3."
            )
        else:
            self.format_help = (
                "M4A: keeps YouTube's own audio, so there is no re-encoding step. "
                "Plays in every podcast app. Currently: M4A."
            )
        self.storage_help = (
            f"{self._podcast_dir}/<channel>\n"
            "These are ordinary files - browse them in any file manager and "
            "upload them to your own drive. Deleting an episode here removes "
            "the file from this folder too."
        )

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

    def _collect_episodes(self):
        base = self._podcast_dir
        episodes = []
        try:
            folders = sorted(os.listdir(base))
        except Exception:
            return episodes, True  # unreadable -> probably a permission problem

        for folder in folders:
            folder_path = os.path.join(base, folder)
            try:
                if not os.path.isdir(folder_path):
                    continue
                episodes.extend(self._scan_folder(folder_path, folder))
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
            card.height = dp(120) if visible else dp(0)
            card.opacity = 1 if visible else 0
            card.disabled = not visible
        except Exception:
            pass

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

            self._release_player()

            player = MediaPlayer()
            player.setAudioStreamType(AudioManager.STREAM_MUSIC)
            player.setDataSource(path)
            player.prepare()
            player.start()
            self._player = player
            self._playing_path = path
            self.play_icon = "pause"
            self.now_playing_status = "Playing"
            self.status_text = f"Playing: {os.path.basename(path)[:40]}"
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
            else:
                self._player.start()
                self.play_icon = "pause"
                self.now_playing_status = "Playing"
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self._release_player()

    def stop_playback(self):
        try:
            self._release_player()
            self.play_icon = "play"
            self.now_playing_status = "Stopped"
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def _release_player(self):
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
        threading.Thread(target=self._export_worker, args=(path,), daemon=True).start()

    def _export_worker(self, path):
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
            Clock.schedule_once(lambda dt: safe_snackbar("Could not open share menu"))

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
        """Hand the download off to Termux, one tap if it will allow it."""
        if self.one_tap_termux and self._run_command_in_termux(payload):
            return True
        return self._share_to_termux(payload)

    def _run_command_in_termux(self, payload):
        """Termux's RUN_COMMAND service - no chooser, but needs allow-external-apps."""
        try:
            from jnius import autoclass

            Intent = autoclass("android.content.Intent")
            String = autoclass("java.lang.String")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")

            intent = Intent()
            intent.setClassName(String("com.termux"), String("com.termux.app.RunCommandService"))
            intent.setAction(String("com.termux.RUN_COMMAND"))
            intent.putExtra(String("com.termux.RUN_COMMAND_PATH"), String(TERMUX_URL_OPENER))
            intent.putExtra(String("com.termux.RUN_COMMAND_ARGUMENTS"), [String(payload)])
            intent.putExtra(String("com.termux.RUN_COMMAND_BACKGROUND"), String("false"))
            intent.putExtra(String("com.termux.RUN_COMMAND_SESSION_ACTION"), String("0"))

            activity = PythonActivity.mActivity
            try:
                activity.startForegroundService(intent)
            except Exception:
                activity.startService(intent)
            return True
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            return False

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

            self.status_text = "Handing the download to Termux..."
            if self._send_to_termux(self._termux_payload(url)):
                self.root.ids.url_input.text = ""
                self.status_text = "Termux is downloading. Pull down Downloads to refresh."
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self.status_text = f"Error: {e}"

    def refresh_channel(self, channel_url, channel_folder):
        try:
            self.status_text = f"Refreshing {channel_folder}..."
            payload = self._termux_payload(f"REFRESH:{channel_url}", channel_folder)
            if self._send_to_termux(payload):
                self.status_text = f"Termux is updating {channel_folder}."
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
                    text="      ✖ Remove channel",
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
