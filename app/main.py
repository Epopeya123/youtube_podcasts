"""YouTube Podcast Downloader - Android App (KivyMD 1.x)"""

import json
import os
import re
import sys
import threading
import traceback
from datetime import datetime

# === CRASH LOGGING (runs before ANY imports that could fail) ===
CRASH_LOG_PATH = None

def setup_crash_logging():
    """Set up crash log in app-private storage (always writable, no permissions needed)."""
    global CRASH_LOG_PATH
    try:
        # Try app-private dir first (always writable on Android)
        from android.storage import app_storage_path
        crash_dir = app_storage_path()
    except ImportError:
        crash_dir = os.path.expanduser("~")
    try:
        os.makedirs(crash_dir, exist_ok=True)
        CRASH_LOG_PATH = os.path.join(crash_dir, "crash_log.txt")
    except Exception:
        CRASH_LOG_PATH = "/tmp/crash_log.txt"

def log_crash(exc_type, exc_value, exc_tb):
    error_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        if CRASH_LOG_PATH:
            with open(CRASH_LOG_PATH, "a") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"CRASH at {datetime.now()}\n")
                f.write(error_text)
                f.write(f"\n{'='*60}\n")
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)

setup_crash_logging()
sys.excepthook = log_crash

# === Now safe to import Kivy/KivyMD ===
try:
    from kivy.clock import Clock
    from kivy.lang import Builder
    from kivy.metrics import dp
    from kivy.properties import StringProperty, BooleanProperty
    from kivymd.app import MDApp
    from kivymd.uix.snackbar import Snackbar
    import yt_dlp
except Exception:
    log_crash(*sys.exc_info())
    raise

# Thread lock for episodes.json
_episodes_lock = threading.Lock()


def get_data_dir():
    try:
        from android.storage import app_storage_path
        return app_storage_path()
    except ImportError:
        return os.path.expanduser("~/.youtube_podcasts")


def get_download_dir():
    """Get download dir with multiple fallbacks."""
    # Try 1: App-private external dir (scoped storage safe)
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity
        ext_dir = activity.getExternalFilesDir(None)
        if ext_dir:
            path = os.path.join(ext_dir.getAbsolutePath(), "Podcasts")
            os.makedirs(path, exist_ok=True)
            return path
    except Exception:
        pass

    # Try 2: App internal storage
    try:
        from android.storage import app_storage_path
        path = os.path.join(app_storage_path(), "Podcasts")
        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        pass

    # Try 3: Home directory (desktop fallback)
    path = os.path.expanduser("~/Podcasts/AI_News_NateBJones")
    os.makedirs(path, exist_ok=True)
    return path


EPISODES_FILE = None

KV = '''
MDScreen:
    md_bg_color: app.theme_cls.bg_dark

    MDBoxLayout:
        orientation: "vertical"

        MDTopAppBar:
            title: "YouTube Podcasts"
            elevation: 4
            md_bg_color: app.theme_cls.primary_color

        ScrollView:
            MDBoxLayout:
                orientation: "vertical"
                padding: dp(20)
                spacing: dp(16)
                adaptive_height: True

                MDTextField:
                    id: url_input
                    hint_text: "Paste YouTube link here"
                    icon_left: "link-variant"
                    mode: "rectangle"
                    size_hint_x: 1

                MDBoxLayout:
                    orientation: "horizontal"
                    spacing: dp(12)
                    adaptive_height: True
                    size_hint_y: None
                    height: dp(48)

                    MDFlatButton:
                        text: "PASTE"
                        on_release: app.paste_from_clipboard()
                        size_hint_x: 0.4

                    MDRaisedButton:
                        id: download_btn
                        text: "DOWNLOAD"
                        on_release: app.start_download()
                        size_hint_x: 0.6

                MDProgressBar:
                    id: progress_bar
                    size_hint_x: 1
                    opacity: 0
                    value: 0

                MDLabel:
                    id: status_label
                    text: app.status_text
                    theme_text_color: "Secondary"
                    adaptive_height: True
                    font_style: "Caption"

                MDSeparator:

                MDLabel:
                    text: "Downloaded Episodes"
                    theme_text_color: "Primary"
                    font_style: "Subtitle1"
                    adaptive_height: True

                MDList:
                    id: episode_list
'''


class YouTubePodcastApp(MDApp):
    status_text = StringProperty("Ready to download")
    is_downloading = BooleanProperty(False)

    def build(self):
        global EPISODES_FILE
        try:
            data_dir = get_data_dir()
            os.makedirs(data_dir, exist_ok=True)
            EPISODES_FILE = os.path.join(data_dir, "episodes.json")
        except Exception as e:
            EPISODES_FILE = None
            log_crash(type(e), e, e.__traceback__)

        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "DeepPurple"

        try:
            self.download_dir = get_download_dir()
        except Exception:
            self.download_dir = "/tmp"

        return Builder.load_string(KV)

    def on_start(self):
        # Request permissions first, then load data after a short delay
        self.request_android_permissions()
        # Delay loading to let permissions settle
        Clock.schedule_once(lambda dt: self._safe_load(), 1.0)

    def _safe_load(self):
        """Load episodes safely after permissions are granted."""
        try:
            self.load_episodes()
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self.status_text = "Ready (no episodes loaded)"
        try:
            self._handle_android_intent()
        except Exception:
            pass

    def on_pause(self):
        return True

    def on_resume(self):
        try:
            self.load_episodes()
        except Exception:
            pass

    def request_android_permissions(self):
        try:
            from android.permissions import request_permissions, Permission
            perms = [Permission.INTERNET]
            # Only request storage permissions if available
            try:
                perms.append(Permission.READ_MEDIA_AUDIO)
            except AttributeError:
                try:
                    perms.append(Permission.READ_EXTERNAL_STORAGE)
                    perms.append(Permission.WRITE_EXTERNAL_STORAGE)
                except AttributeError:
                    pass
            request_permissions(perms)
        except ImportError:
            pass
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def _handle_android_intent(self):
        try:
            from android import activity
            activity.bind(on_new_intent=self._on_new_intent)
            intent = activity.getIntent()
            self._process_intent(intent)
        except ImportError:
            pass
        except Exception:
            pass

    def _on_new_intent(self, intent):
        try:
            self._process_intent(intent)
        except Exception:
            pass

    def _process_intent(self, intent):
        try:
            if intent.getAction() == "android.intent.action.SEND":
                url = intent.getStringExtra("android.intent.extra.TEXT")
                if url and self.extract_video_id(url):
                    self.root.ids.url_input.text = url
                    Snackbar(text="YouTube link received! Tap Download.").open()
        except Exception:
            pass

    def paste_from_clipboard(self):
        try:
            from kivy.core.clipboard import Clipboard
            text = Clipboard.paste()
            if text:
                self.root.ids.url_input.text = text.strip()
        except Exception:
            pass

    def load_episodes(self):
        from kivymd.uix.list import TwoLineAvatarListItem, IconLeftWidget
        episodes = self._read_episodes()
        if not hasattr(self.root, 'ids') or 'episode_list' not in self.root.ids:
            return
        episode_list = self.root.ids.episode_list
        episode_list.clear_widgets()
        for ep in episodes:
            try:
                duration = ep.get("duration", 0)
                mins = int(duration) // 60 if duration else 0
                date = ep.get("upload_date", "")
                if len(date) == 8:
                    date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
                icon = IconLeftWidget(icon="music-note")
                item = TwoLineAvatarListItem(
                    text=str(ep.get("title", "Unknown")),
                    secondary_text=f"{mins} min  |  {date}",
                )
                item.add_widget(icon)
                episode_list.add_widget(item)
            except Exception:
                continue

    def _read_episodes(self):
        with _episodes_lock:
            if EPISODES_FILE and os.path.exists(EPISODES_FILE):
                try:
                    with open(EPISODES_FILE, "r") as f:
                        data = json.load(f)
                        return data if isinstance(data, list) else []
                except Exception:
                    return []
        return []

    def _save_episodes(self, episodes):
        with _episodes_lock:
            if EPISODES_FILE:
                try:
                    tmp = EPISODES_FILE + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(episodes, f, indent=2)
                    os.rename(tmp, EPISODES_FILE)
                except Exception as e:
                    log_crash(type(e), e, e.__traceback__)

    def extract_video_id(self, url):
        patterns = [
            r'(?:v=|/v/|youtu\.be/|/shorts/)([a-zA-Z0-9_-]{11})',
            r'^([a-zA-Z0-9_-]{11})$',
        ]
        for pattern in patterns:
            match = re.search(pattern, url.strip())
            if match:
                return match.group(1)
        return None

    def start_download(self):
        if self.is_downloading:
            Snackbar(text="Download already in progress...").open()
            return

        url = self.root.ids.url_input.text.strip()
        if not url:
            Snackbar(text="Please paste a YouTube link first").open()
            return

        video_id = self.extract_video_id(url)
        if not video_id:
            Snackbar(text="Invalid YouTube URL").open()
            return

        episodes = self._read_episodes()
        if any(ep.get("id") == video_id for ep in episodes):
            Snackbar(text="Already downloaded!").open()
            return

        self.is_downloading = True
        self.root.ids.progress_bar.opacity = 1
        self.root.ids.progress_bar.value = 0
        self.root.ids.download_btn.disabled = True
        self.status_text = "Starting download..."

        thread = threading.Thread(
            target=self._download_thread,
            args=(video_id,),
            daemon=True,
        )
        thread.start()

    def _download_thread(self, video_id):
        output_template = os.path.join(self.download_dir, "%(id)s.%(ext)s")

        def progress_hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                if total > 0:
                    pct = downloaded / total * 100
                    Clock.schedule_once(lambda dt, p=pct: self._update_progress(p, "Downloading..."))
            elif d["status"] == "finished":
                Clock.schedule_once(lambda dt: self._update_progress(95, "Finishing up..."))

        ydl_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio",
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}",
                    download=True,
                )
                if info:
                    title = info.get("title", "Unknown")
                    ext = info.get("ext", "m4a")
                    safe_name = f"{self._sanitize_filename(title)}_{video_id}.{ext}"
                    src_path = os.path.join(self.download_dir, f"{video_id}.{ext}")
                    final_path = os.path.join(self.download_dir, safe_name)

                    if os.path.exists(src_path) and not os.path.exists(final_path):
                        os.rename(src_path, final_path)
                    elif not os.path.exists(final_path):
                        for f in os.listdir(self.download_dir):
                            if f.startswith(video_id):
                                final_path = os.path.join(self.download_dir, f)
                                break

                    if not os.path.exists(final_path):
                        Clock.schedule_once(lambda dt: self._download_error("File not found after download"))
                        return

                    filesize = os.path.getsize(final_path)
                    metadata = {
                        "id": video_id,
                        "title": title,
                        "upload_date": info.get("upload_date", ""),
                        "duration": info.get("duration", 0),
                        "filename": os.path.basename(final_path),
                        "filesize": filesize,
                    }
                    episodes = self._read_episodes()
                    episodes.insert(0, metadata)
                    self._save_episodes(episodes)
                    Clock.schedule_once(lambda dt, t=title: self._download_complete(t))
                else:
                    Clock.schedule_once(lambda dt: self._download_error("No video info"))
        except Exception as e:
            msg = str(e).split('\n')[0][:200]
            log_crash(type(e), e, e.__traceback__)
            Clock.schedule_once(lambda dt, m=msg: self._download_error(m))

    def _sanitize_filename(self, title):
        safe = "".join(c if c.isalnum() or c in " -_" else "" for c in title)
        return safe.strip()[:60] or "episode"

    def _update_progress(self, value, text):
        self.root.ids.progress_bar.value = value
        self.status_text = text

    def _download_complete(self, title):
        self.is_downloading = False
        self.root.ids.progress_bar.value = 100
        Clock.schedule_once(lambda dt: self._hide_progress(), 0.5)
        self.root.ids.download_btn.disabled = False
        self.root.ids.url_input.text = ""
        self.status_text = f"Downloaded: {title}"
        Snackbar(text="Download complete!").open()
        self.load_episodes()

    def _hide_progress(self):
        self.root.ids.progress_bar.opacity = 0
        self.root.ids.progress_bar.value = 0

    def _download_error(self, error):
        self.is_downloading = False
        self.root.ids.progress_bar.opacity = 0
        self.root.ids.download_btn.disabled = False
        self.status_text = f"Error: {error}"
        Snackbar(text="Download failed").open()


if __name__ == "__main__":
    try:
        YouTubePodcastApp().run()
    except Exception:
        log_crash(*sys.exc_info())
        raise
