"""YouTube Podcast Downloader - Android App"""

import json
import os
import re
import threading
from pathlib import Path

from kivy.clock import Clock
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import StringProperty, BooleanProperty
from kivymd.app import MDApp
from kivymd.uix.list import MDListItem, MDListItemHeadlineText, MDListItemSupportingText, MDListItemLeadingIcon
from kivymd.uix.snackbar import MDSnackbar, MDSnackbarText

import yt_dlp

# Thread lock for episodes.json access
_episodes_lock = threading.Lock()


def get_data_dir():
    """Get the app's data directory."""
    try:
        from android.storage import app_storage_path
        return app_storage_path()
    except ImportError:
        return os.path.expanduser("~/.youtube_podcasts")


def get_download_dir():
    """Get the download directory for audio files."""
    try:
        from android.storage import primary_external_storage_path
        path = os.path.join(primary_external_storage_path(), "Podcasts", "AI_News_NateBJones")
    except ImportError:
        path = os.path.expanduser("~/Podcasts/AI_News_NateBJones")
    os.makedirs(path, exist_ok=True)
    return path


EPISODES_FILE = None  # Set in build()

KV = '''
<EpisodeItem>:
    MDListItemLeadingIcon:
        icon: root.icon
    MDListItemHeadlineText:
        text: root.title
    MDListItemSupportingText:
        text: root.subtitle

MDScreen:
    md_bg_color: app.theme_cls.surfaceColor

    MDBoxLayout:
        orientation: "vertical"

        # Top App Bar
        MDTopAppBar:
            MDTopAppBarTitle:
                text: "YouTube Podcasts"

        # Main content
        MDBoxLayout:
            orientation: "vertical"
            padding: dp(20)
            spacing: dp(16)
            adaptive_height: True

            # URL Input
            MDTextField:
                id: url_input
                mode: "outlined"
                size_hint_x: 1

                MDTextFieldHintText:
                    text: "Paste YouTube link here"

                MDTextFieldLeadingIcon:
                    icon: "link-variant"

            # Buttons row
            MDBoxLayout:
                orientation: "horizontal"
                spacing: dp(12)
                adaptive_height: True

                MDButton:
                    id: paste_btn
                    style: "outlined"
                    on_release: app.paste_from_clipboard()
                    size_hint_x: 0.4

                    MDButtonIcon:
                        icon: "content-paste"

                    MDButtonText:
                        text: "Paste"

                MDButton:
                    id: download_btn
                    style: "filled"
                    on_release: app.start_download()
                    size_hint_x: 0.6

                    MDButtonIcon:
                        icon: "download"

                    MDButtonText:
                        text: "Download"

            # Progress bar (hidden by default)
            MDLinearProgressIndicator:
                id: progress_bar
                size_hint_x: 1
                opacity: 0
                value: 0

            # Status text
            MDLabel:
                id: status_label
                text: app.status_text
                theme_text_color: "Secondary"
                adaptive_height: True
                font_style: "Body"
                role: "medium"

        # Divider
        MDDivider:

        # Episode list header
        MDBoxLayout:
            padding: [dp(20), dp(12), dp(20), dp(4)]
            adaptive_height: True

            MDLabel:
                text: "Downloaded Episodes"
                theme_text_color: "Primary"
                font_style: "Title"
                role: "small"
                adaptive_height: True

        # Scrollable episode list
        ScrollView:
            size_hint_y: 1

            MDList:
                id: episode_list
                padding: [dp(4), 0, dp(4), 0]
'''


class EpisodeItem(MDListItem):
    """A single episode in the list."""
    title = StringProperty("")
    subtitle = StringProperty("")
    icon = StringProperty("music-note")


class YouTubePodcastApp(MDApp):
    status_text = StringProperty("Ready to download")
    is_downloading = BooleanProperty(False)

    def build(self):
        global EPISODES_FILE
        data_dir = get_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        EPISODES_FILE = os.path.join(data_dir, "episodes.json")

        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "DeepPurple"

        self.download_dir = get_download_dir()
        return Builder.load_string(KV)

    def on_start(self):
        self.request_android_permissions()
        self.load_episodes()
        self._handle_android_intent()

    def request_android_permissions(self):
        """Request runtime permissions on Android 6+."""
        try:
            from android.permissions import request_permissions, Permission
            request_permissions([
                Permission.READ_MEDIA_AUDIO,
                Permission.INTERNET,
            ])
        except ImportError:
            pass

    def _handle_android_intent(self):
        """Handle URLs shared to this app from other apps."""
        try:
            from android import activity
            activity.bind(on_new_intent=self._on_new_intent)
            intent = activity.getIntent()
            self._process_intent(intent)
        except ImportError:
            pass

    def _on_new_intent(self, intent):
        self._process_intent(intent)

    def _process_intent(self, intent):
        try:
            if intent.getAction() == "android.intent.action.SEND":
                url = intent.getStringExtra("android.intent.extra.TEXT")
                if url and self.extract_video_id(url):
                    self.root.ids.url_input.text = url
                    self.show_snackbar("YouTube link received! Tap Download.")
        except Exception:
            pass

    def paste_from_clipboard(self):
        """Paste URL from clipboard."""
        try:
            from kivy.core.clipboard import Clipboard
            text = Clipboard.paste()
            if text:
                self.root.ids.url_input.text = text.strip()
        except Exception:
            self.show_snackbar("Could not access clipboard")

    def load_episodes(self):
        """Load and display downloaded episodes."""
        episodes = self._read_episodes()
        episode_list = self.root.ids.episode_list
        episode_list.clear_widgets()
        for ep in episodes:
            duration = ep.get("duration", 0)
            mins = int(duration) // 60 if duration else 0
            date = ep.get("upload_date", "")
            if len(date) == 8:
                date = f"{date[:4]}-{date[4:6]}-{date[6:]}"

            item = EpisodeItem(
                title=ep.get("title", "Unknown"),
                subtitle=f"{mins} min  |  {date}",
                icon="music-note",
            )
            episode_list.add_widget(item)

    def _read_episodes(self):
        with _episodes_lock:
            if EPISODES_FILE and os.path.exists(EPISODES_FILE):
                try:
                    with open(EPISODES_FILE, "r") as f:
                        data = json.load(f)
                        return data if isinstance(data, list) else []
                except (json.JSONDecodeError, ValueError):
                    return []
        return []

    def _save_episodes(self, episodes):
        with _episodes_lock:
            if EPISODES_FILE:
                tmp = EPISODES_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(episodes, f, indent=2)
                os.rename(tmp, EPISODES_FILE)

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
            self.show_snackbar("Download already in progress...")
            return

        url = self.root.ids.url_input.text.strip()
        if not url:
            self.show_snackbar("Please paste a YouTube link first")
            return

        video_id = self.extract_video_id(url)
        if not video_id:
            self.show_snackbar("Invalid YouTube URL")
            return

        # Check if already downloaded
        episodes = self._read_episodes()
        if any(ep.get("id") == video_id for ep in episodes):
            self.show_snackbar("Already downloaded!")
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
        """Run download in background thread."""
        output_template = os.path.join(self.download_dir, "%(id)s.%(ext)s")

        def progress_hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                if total > 0:
                    pct = downloaded / total * 100
                    Clock.schedule_once(lambda dt: self._update_progress(pct, "Downloading..."))
            elif d["status"] == "finished":
                Clock.schedule_once(lambda dt: self._update_progress(95, "Finishing up..."))

        # Download as m4a directly (no FFmpeg needed on Android)
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
                    elif not os.path.exists(src_path) and not os.path.exists(final_path):
                        # File might have a different extension, find it
                        for f in os.listdir(self.download_dir):
                            if f.startswith(video_id):
                                final_path = os.path.join(self.download_dir, f)
                                break

                    filesize = os.path.getsize(final_path) if os.path.exists(final_path) else 0

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

                    Clock.schedule_once(lambda dt: self._download_complete(title))
                else:
                    Clock.schedule_once(lambda dt: self._download_error("No video info returned"))
        except Exception as e:
            error_msg = str(e)
            Clock.schedule_once(lambda dt: self._download_error(error_msg))

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
        self.show_snackbar("Download complete!")
        self.load_episodes()

    def _hide_progress(self):
        self.root.ids.progress_bar.opacity = 0
        self.root.ids.progress_bar.value = 0

    def _download_error(self, error):
        self.is_downloading = False
        self.root.ids.progress_bar.opacity = 0
        self.root.ids.download_btn.disabled = False
        self.status_text = f"Error: {error[:200]}"
        self.show_snackbar("Download failed")

    def show_snackbar(self, text):
        MDSnackbar(
            MDSnackbarText(text=text),
            y=dp(24),
            pos_hint={"center_x": 0.5},
            size_hint_x=0.9,
        ).open()


if __name__ == "__main__":
    YouTubePodcastApp().run()
