"""YouTube Podcast Downloader - Android App (KivyMD 1.x)
Version 2.0.0 - Multi-channel support, Termux integration
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
    from kivy.properties import StringProperty, BooleanProperty
    from kivymd.app import MDApp
except Exception:
    log_crash(*sys.exc_info())
    raise

# Fix stdout/stderr for Android
if not hasattr(sys.stdout, 'write') or isinstance(sys.stdout, str):
    sys.stdout = open(os.devnull, 'w')
if not hasattr(sys.stderr, 'write') or isinstance(sys.stderr, str):
    sys.stderr = open(os.devnull, 'w')


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


CHANNELS_FILE = None

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
                padding: dp(16)
                spacing: dp(12)
                adaptive_height: True

                # === DOWNLOAD SINGLE VIDEO ===
                MDLabel:
                    text: "Download Single Video"
                    theme_text_color: "Primary"
                    font_style: "Subtitle1"
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
                        text: "DOWNLOAD VIA TERMUX"
                        on_release: app.download_single_video()
                        size_hint_x: 0.65

                MDLabel:
                    id: status_label
                    text: app.status_text
                    theme_text_color: "Secondary"
                    adaptive_height: True
                    font_style: "Caption"

                MDSeparator:

                # === CHANNELS ===
                MDBoxLayout:
                    orientation: "horizontal"
                    adaptive_height: True
                    size_hint_y: None
                    height: dp(40)

                    MDLabel:
                        text: "Your Channels"
                        theme_text_color: "Primary"
                        font_style: "Subtitle1"

                    MDRaisedButton:
                        text: "+ ADD"
                        on_release: app.show_add_channel()
                        size_hint_x: None
                        width: dp(100)

                # Add channel input (hidden by default)
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
                        hint_text: "YouTube channel URL (e.g. youtube.com/@natebjones)"
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

                MDLabel:
                    text: "Tap refresh to get latest episodes via Termux"
                    theme_text_color: "Hint"
                    adaptive_height: True
                    font_style: "Caption"

                MDList:
                    id: channel_list
'''


class YouTubePodcastApp(MDApp):
    status_text = StringProperty("Ready")
    is_downloading = BooleanProperty(False)

    def build(self):
        global CHANNELS_FILE
        try:
            data_dir = get_data_dir()
            os.makedirs(data_dir, exist_ok=True)
            CHANNELS_FILE = os.path.join(data_dir, "channels.json")
        except Exception as e:
            CHANNELS_FILE = None
            log_crash(type(e), e, e.__traceback__)

        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "DeepPurple"

        return Builder.load_string(KV)

    def on_start(self):
        try:
            from android.permissions import request_permissions, Permission
            perms = [Permission.INTERNET]
            try:
                perms.append(Permission.READ_MEDIA_AUDIO)
            except AttributeError:
                pass
            request_permissions(perms)
        except (ImportError, Exception):
            pass
        Clock.schedule_once(lambda dt: self._safe_load(), 1.0)

    def _safe_load(self):
        try:
            self.load_channels()
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
        try:
            self._handle_android_intent()
        except Exception:
            pass

    def on_pause(self):
        try:
            return True
        except Exception:
            return True

    def on_resume(self):
        try:
            self.load_channels()
        except Exception:
            pass

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
                if url:
                    self.root.ids.url_input.text = url
                    safe_snackbar("Link received! Tap Download.")
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

    # === TERMUX INTEGRATION ===

    def _send_to_termux(self, text):
        """Send a URL/command to Termux via Android share intent."""
        try:
            from jnius import autoclass, cast
            Intent = autoclass('android.content.Intent')
            String = autoclass('java.lang.String')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            ComponentName = autoclass('android.content.ComponentName')

            intent = Intent()
            intent.setAction(Intent.ACTION_SEND)
            intent.setType("text/plain")
            intent.putExtra(Intent.EXTRA_TEXT, String(text))

            # Target Termux specifically
            intent.setComponent(ComponentName(
                String("com.termux"),
                String("com.termux.app.TermuxOpenReceiver"),
            ))

            PythonActivity.mActivity.startActivity(intent)
            return True
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self.status_text = f"Error: Could not launch Termux ({str(e)[:80]})"
            safe_snackbar("Could not open Termux. Is it installed?")
            return False

    def download_single_video(self):
        """Download a single video via Termux."""
        try:
            url = self.root.ids.url_input.text.strip()
            if not url:
                self.status_text = "Please paste a YouTube link first"
                return

            # Basic validation
            if 'youtube' not in url and 'youtu.be' not in url:
                self.status_text = "Invalid YouTube URL"
                return

            self.status_text = "Opening Termux to download..."
            if self._send_to_termux(url):
                self.root.ids.url_input.text = ""
                self.status_text = "Termux is downloading. Check Podcasts folder when done."
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            self.status_text = f"Error: {e}"

    def refresh_channel(self, channel_url, channel_folder):
        """Refresh a channel's episodes via Termux."""
        try:
            self.status_text = f"Refreshing {channel_folder}..."
            # Send special command that termux-url-opener understands
            command = f"REFRESH:{channel_url}"
            if self._send_to_termux(command):
                self.status_text = f"Termux is updating {channel_folder}. Check folder when done."
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
                os.rename(tmp, CHANNELS_FILE)
            except Exception as e:
                log_crash(type(e), e, e.__traceback__)

    def _url_to_folder_name(self, url):
        """Extract a folder name from a YouTube channel URL."""
        # Extract handle: youtube.com/@natebjones -> natebjones
        match = re.search(r'@([a-zA-Z0-9_.-]+)', url)
        if match:
            return match.group(1)
        # Extract channel name from /c/ or /channel/ URL
        match = re.search(r'/(?:c|channel)/([a-zA-Z0-9_.-]+)', url)
        if match:
            return match.group(1)
        # Fallback
        return re.sub(r'[^a-zA-Z0-9_-]', '_', url.split('/')[-1])[:30] or "channel"

    def show_add_channel(self):
        """Show the add channel form."""
        try:
            box = self.root.ids.add_channel_box
            box.opacity = 1
            box.disabled = False
            box.height = dp(160)
        except Exception:
            pass

    def hide_add_channel(self):
        """Hide the add channel form."""
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
        """Add a new channel."""
        try:
            url = self.root.ids.channel_url_input.text.strip()
            name = self.root.ids.channel_name_input.text.strip()

            if not url:
                safe_snackbar("Please enter a channel URL")
                return

            # Normalize URL
            if not url.startswith("http"):
                url = "https://www.youtube.com/" + url

            if not name:
                name = self._url_to_folder_name(url)

            folder = self._url_to_folder_name(url)

            channels = self._read_channels()

            # Check for duplicates
            if any(c.get("url") == url for c in channels):
                safe_snackbar("Channel already added!")
                return

            channels.append({
                "name": name,
                "url": url,
                "folder": folder,
                "added": datetime.now().isoformat(),
            })
            self._save_channels(channels)
            self.hide_add_channel()
            self.load_channels()
            safe_snackbar(f"Added: {name}")
            self.status_text = f"Added channel: {name}"
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)
            safe_snackbar("Could not add channel")

    def remove_channel(self, url):
        """Remove a channel."""
        try:
            channels = self._read_channels()
            channels = [c for c in channels if c.get("url") != url]
            self._save_channels(channels)
            self.load_channels()
            safe_snackbar("Channel removed")
        except Exception as e:
            log_crash(type(e), e, e.__traceback__)

    def load_channels(self):
        """Load and display channels."""
        try:
            from kivymd.uix.list import TwoLineAvatarListItem, IconLeftWidget, OneLineListItem
        except ImportError:
            return

        channels = self._read_channels()
        if not self.root or 'channel_list' not in self.root.ids:
            return

        channel_list = self.root.ids.channel_list
        channel_list.clear_widgets()

        if not channels:
            try:
                item = OneLineListItem(
                    text="No channels yet. Tap + ADD to add one.",
                    theme_text_color="Hint",
                )
                channel_list.add_widget(item)
            except Exception:
                pass
            return

        for ch in channels:
            try:
                name = ch.get("name", "Unknown")
                url = ch.get("url", "")
                folder = ch.get("folder", "")

                # Channel icon
                icon = IconLeftWidget(icon="podcast")
                icon.bind(on_release=lambda x, u=url, f=folder: self.refresh_channel(u, f))

                # Channel row
                item = TwoLineAvatarListItem(
                    text=name,
                    secondary_text=f"Tap podcast icon to refresh",
                )
                item.add_widget(icon)
                channel_list.add_widget(item)

                # Refresh button
                refresh_item = OneLineListItem(
                    text=f"      \u21b3 Refresh latest episodes",
                    theme_text_color="Custom",
                    text_color=(0.4, 0.8, 0.4, 0.9),
                    on_release=lambda x, u=url, f=folder: self.refresh_channel(u, f),
                )
                channel_list.add_widget(refresh_item)

                # Remove button
                remove_item = OneLineListItem(
                    text=f"      \u2716 Remove channel",
                    theme_text_color="Custom",
                    text_color=(0.8, 0.3, 0.3, 0.7),
                    on_release=lambda x, u=url: self.remove_channel(u),
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
