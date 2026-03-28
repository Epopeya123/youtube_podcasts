"""YouTube Podcast Downloader - Android App (KivyMD 1.x)"""

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
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity
        ext_dir = activity.getExternalFilesDir(None)
        if ext_dir:
            dirs_to_try.append(ext_dir.getAbsolutePath())
    except Exception:
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
    from kivy.properties import StringProperty, BooleanProperty
    from kivymd.app import MDApp
    import yt_dlp
except Exception:
    log_crash(*sys.exc_info())
    raise

# Fix stdout/stderr for Android (use devnull, not StringIO to avoid memory leak)
if not hasattr(sys.stdout, 'write') or isinstance(sys.stdout, str):
    sys.stdout = open(os.devnull, 'w')
if not hasattr(sys.stderr, 'write') or isinstance(sys.stderr, str):
    sys.stderr = open(os.devnull, 'w')


class YTDLPLogger:
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


# Disable Android StrictMode file URI check (needed for file:// URIs in intents)
def disable_strict_mode():
    try:
        from jnius import autoclass
        StrictMode = autoclass('android.os.StrictMode')
        VmPolicyBuilder = autoclass('android.os.StrictMode$VmPolicy$Builder')
        StrictMode.setVmPolicy(VmPolicyBuilder().build())
    except Exception:
        pass

disable_strict_mode()

_episodes_lock = threading.Lock()


def get_data_dir():
    try:
        from android.storage import app_storage_path
        return app_storage_path()
    except ImportError:
        return os.path.expanduser("~/.youtube_podcasts")


def get_download_dir():
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
    try:
        from android.storage import app_storage_path
        path = os.path.join(app_storage_path(), "Podcasts")
        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        pass
    path = os.path.expanduser("~/Podcasts/AI_News_NateBJones")
    os.makedirs(path, exist_ok=True)
    return path


def safe_snackbar(text):
    try:
        from kivymd.uix.snackbar import Snackbar
        Snackbar(text=str(text)).open()
    except Exception:
        pass


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

                MDLabel:
                    text: "Tap play icon | Tap share icon to send"
                    theme_text_color: "Hint"
                    adaptive_height: True
                    font_style: "Caption"

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
            # Surface error instead of silent failure
            self.status_text = "Error: cannot access data directory"

        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "DeepPurple"

        try:
            self.download_dir = get_download_dir()
        except Exception:
            self.download_dir = "/tmp"

        return Builder.load_string(KV)

    def on_start(self):
        self.request_android_permissions()
        Clock.schedule_once(lambda dt: self._safe_load(), 1.0)

    def _safe_load(self):
        try:
            self.load_episodes()
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self.status_text = "Ready"
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

    def on_stop(self):
        try:
            if hasattr(self, '_player') and self._player:
                self._player.stop()
                self._player.release()
                self._player = None
        except Exception:
            pass

    def request_android_permissions(self):
        try:
            from android.permissions import request_permissions, Permission
            perms = [Permission.INTERNET]
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
                    safe_snackbar("YouTube link received! Tap Download.")
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
        try:
            from kivymd.uix.list import TwoLineAvatarIconListItem, IconLeftWidget, IconRightWidget
        except ImportError:
            return

        episodes = self._read_episodes()
        if not self.root or not hasattr(self.root, 'ids') or 'episode_list' not in self.root.ids:
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
                filename = ep.get("filename", "")

                # Play icon (left) - triggers playback
                play_icon = IconLeftWidget(icon="play-circle")
                play_icon.bind(on_release=lambda x, f=filename: self.play_episode(f))

                # Share icon (right) - triggers share
                share_icon = IconRightWidget(icon="share-variant")
                share_icon.bind(on_release=lambda x, f=filename: self.share_episode(f))

                # Use TwoLineAvatarIconListItem for left + right icons
                # No on_release here to avoid double-firing with icon callbacks
                item = TwoLineAvatarIconListItem(
                    text=str(ep.get("title", "Unknown")),
                    secondary_text=f"{mins} min  |  {date}",
                )

                item.add_widget(play_icon)
                item.add_widget(share_icon)
                episode_list.add_widget(item)
            except Exception:
                continue

    def _get_filepath(self, filename):
        if not filename:
            return None
        filepath = os.path.join(self.download_dir, filename)
        if os.path.exists(filepath):
            return filepath
        return None

    def play_episode(self, filename):
        """Play audio: try system player first, fall back to in-app MediaPlayer."""
        filepath = self._get_filepath(filename)
        if not filepath:
            safe_snackbar("File not found")
            return

        # Try 1: Open in system audio player (has full controls: seek, pause, etc.)
        try:
            from jnius import autoclass
            Intent = autoclass('android.content.Intent')
            Uri = autoclass('android.net.Uri')
            File = autoclass('java.io.File')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')

            java_file = File(filepath)
            uri = Uri.fromFile(java_file)
            intent = Intent(Intent.ACTION_VIEW)
            intent.setDataAndType(uri, "audio/*")
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            PythonActivity.mActivity.startActivity(intent)
            self.status_text = f"Playing: {os.path.basename(filepath)[:50]}"
            return
        except Exception:
            pass

        # Try 2: In-app MediaPlayer (no UI controls but works reliably)
        try:
            if hasattr(self, '_player') and self._player:
                try:
                    self._player.stop()
                    self._player.release()
                except Exception:
                    pass
                self._player = None

            from jnius import autoclass
            MediaPlayer = autoclass('android.media.MediaPlayer')
            player = MediaPlayer()
            player.setDataSource(filepath)
            player.prepare()
            player.start()
            self._player = player
            self.status_text = f"Playing: {os.path.basename(filepath)[:50]}"
            safe_snackbar("Playing (in-app)")
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self.status_text = f"Error: {str(e)[:100]}"
            safe_snackbar("Could not play audio")

    def share_episode(self, filename):
        """Share audio file via Android share menu."""
        filepath = self._get_filepath(filename)
        if not filepath:
            safe_snackbar("File not found")
            return
        try:
            from jnius import autoclass, cast
            Intent = autoclass('android.content.Intent')
            Uri = autoclass('android.net.Uri')
            File = autoclass('java.io.File')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')

            java_file = File(filepath)
            uri = Uri.fromFile(java_file)
            intent = Intent(Intent.ACTION_SEND)
            intent.setType("audio/*")
            intent.putExtra(Intent.EXTRA_STREAM, cast('android.os.Parcelable', uri))
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            chooser = Intent.createChooser(intent, "Share audio")
            chooser.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            PythonActivity.mActivity.startActivity(chooser)
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self.status_text = f"Error sharing: {str(e)[:100]}"
            safe_snackbar("Could not share file")

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

    def _save_episode_atomic(self, metadata):
        """Thread-safe read-modify-write for adding one episode."""
        with _episodes_lock:
            episodes = []
            if EPISODES_FILE and os.path.exists(EPISODES_FILE):
                try:
                    with open(EPISODES_FILE, "r") as f:
                        data = json.load(f)
                        episodes = data if isinstance(data, list) else []
                except Exception:
                    episodes = []
            episodes.insert(0, metadata)
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
        try:
            if self.is_downloading:
                safe_snackbar("Download already in progress...")
                return

            url = self.root.ids.url_input.text.strip()
            if not url:
                self.status_text = "Please paste a YouTube link first"
                return

            video_id = self.extract_video_id(url)
            if not video_id:
                self.status_text = "Invalid YouTube URL"
                return

            episodes = self._read_episodes()
            if any(ep.get("id") == video_id for ep in episodes):
                self.status_text = "Already downloaded!"
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
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self.is_downloading = False
            self.status_text = f"Error: {e}"

    def _download_thread(self, video_id):
        output_template = os.path.join(self.download_dir, "%(id)s.%(ext)s")

        def progress_hook(d):
            try:
                if d["status"] == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes", 0)
                    if total > 0:
                        pct = downloaded / total * 100
                        Clock.schedule_once(lambda dt, p=pct: self._update_progress(p, "Downloading..."))
                elif d["status"] == "finished":
                    Clock.schedule_once(lambda dt: self._update_progress(95, "Finishing up..."))
            except Exception:
                pass

        ydl_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio",
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
            "logger": YTDLPLogger(),
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}",
                    download=True,
                )
                if not info:
                    Clock.schedule_once(lambda dt: self._download_error("No video info"))
                    return

                title = info.get("title", "Unknown")
                ext = info.get("ext", "m4a")

                final_path = None
                for name in [f"{video_id}.{ext}", f"{video_id}.m4a", f"{video_id}.webm", f"{video_id}.opus"]:
                    p = os.path.join(self.download_dir, name)
                    if os.path.exists(p):
                        final_path = p
                        break

                if not final_path:
                    try:
                        for f in os.listdir(self.download_dir):
                            if f.startswith(video_id):
                                final_path = os.path.join(self.download_dir, f)
                                break
                    except Exception:
                        pass

                if not final_path or not os.path.exists(final_path):
                    Clock.schedule_once(lambda dt: self._download_error("File not found after download"))
                    return

                try:
                    file_ext = os.path.splitext(final_path)[1]
                    safe_name = f"{self._sanitize_filename(title)}_{video_id}{file_ext}"
                    new_path = os.path.join(self.download_dir, safe_name)
                    if final_path != new_path and not os.path.exists(new_path):
                        os.rename(final_path, new_path)
                        final_path = new_path
                except Exception:
                    pass

                filesize = 0
                try:
                    filesize = os.path.getsize(final_path)
                except Exception:
                    pass

                metadata = {
                    "id": video_id,
                    "title": title,
                    "upload_date": info.get("upload_date", ""),
                    "duration": info.get("duration", 0),
                    "filename": os.path.basename(final_path),
                    "filesize": filesize,
                }
                # Atomic read-modify-write (thread-safe)
                self._save_episode_atomic(metadata)
                Clock.schedule_once(lambda dt, t=title: self._download_complete(t))

        except Exception as e:
            msg = str(e).split('\n')[0][:200]
            log_crash(type(e), e, e.__traceback__)
            Clock.schedule_once(lambda dt, m=msg: self._download_error(m))

    def _sanitize_filename(self, title):
        safe = "".join(c if c.isalnum() or c in " -_" else "" for c in title)
        return safe.strip()[:60] or "episode"

    def _update_progress(self, value, text):
        try:
            self.root.ids.progress_bar.value = value
            self.status_text = text
        except Exception:
            pass

    def _download_complete(self, title):
        try:
            self.is_downloading = False
            self.root.ids.progress_bar.value = 100
            Clock.schedule_once(lambda dt: self._hide_progress(), 0.5)
            self.root.ids.download_btn.disabled = False
            self.root.ids.url_input.text = ""
            self.status_text = f"Downloaded: {title}"
            safe_snackbar("Download complete!")
            self.load_episodes()
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self.is_downloading = False
            self.status_text = "Downloaded (UI refresh failed)"

    def _hide_progress(self):
        try:
            self.root.ids.progress_bar.opacity = 0
            self.root.ids.progress_bar.value = 0
        except Exception:
            pass

    def _download_error(self, error):
        try:
            self.is_downloading = False
            self.root.ids.progress_bar.opacity = 0
            self.root.ids.download_btn.disabled = False
            self.status_text = f"Error: {error}"
            safe_snackbar("Download failed")
        except Exception:
            pass


if __name__ == "__main__":
    try:
        YouTubePodcastApp().run()
    except Exception:
        log_crash(*sys.exc_info())
        raise
