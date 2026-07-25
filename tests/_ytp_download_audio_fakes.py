"""Test doubles used by tests/test_download_audio.py.

Deliberately named with a leading underscore and a `_ytp_` prefix so pytest does
not collect it and it cannot collide with other test helpers in this directory.

The container running these tests is blocked by YouTube (media fetches return
HTTP 403), so nothing here touches the network. `FakeYoutubeDLFactory` stands in
for `yt_dlp.YoutubeDL`: it honours the `outtmpl` it is handed, writes the files a
real download would have left on disk, and returns an info dict shaped like the
real one (including `requested_downloads[*].filepath`).
"""

from __future__ import annotations

import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class DownloadPlan:
    """What the fake yt-dlp should pretend happened for one video."""

    def __init__(
        self,
        video_id="dQw4w9WgXcQ",
        title="Episode Title",
        duration=1800,
        audio_ext=".m4a",
        thumb_ext=".jpg",
        write_audio=True,
        write_thumb=True,
        audio_payload=b"\x00\x00\x00\x20ftypM4A fake aac payload",
        audio_source=None,
        report_filepath=True,
        raise_exc=None,
        return_none=False,
        extra_info=None,
    ):
        self.video_id = video_id
        self.title = title
        self.duration = duration
        self.audio_ext = audio_ext
        self.thumb_ext = thumb_ext
        self.write_audio = write_audio
        self.write_thumb = write_thumb
        self.audio_payload = audio_payload
        # When set, this real file is copied in place of `audio_payload`.
        self.audio_source = audio_source
        # When False the info dict omits `requested_downloads`, forcing
        # download_audio to fall back to scanning the output directory.
        self.report_filepath = report_filepath
        self.raise_exc = raise_exc
        self.return_none = return_none
        self.extra_info = extra_info or {}


class _FakeYoutubeDL:
    def __init__(self, opts, factory):
        self.opts = opts
        self.factory = factory
        self.entered = False
        self.closed = False

    # yt-dlp is used as a context manager in download_audio().
    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    # -- helpers ---------------------------------------------------------
    def _output_dir(self):
        outtmpl = self.opts.get("outtmpl")
        if isinstance(outtmpl, dict):
            outtmpl = outtmpl.get("default", "")
        return os.path.dirname(outtmpl) or "."

    def _write_audio(self, directory, plan):
        path = os.path.join(directory, plan.video_id + plan.audio_ext)
        if plan.audio_source:
            shutil.copyfile(plan.audio_source, path)
        else:
            with open(path, "wb") as fh:
                fh.write(plan.audio_payload)
        self.factory.files_written.append(path)
        return path

    def _write_thumb(self, directory, plan):
        path = os.path.join(directory, plan.video_id + plan.thumb_ext)
        with open(path, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0 fake jpeg bytes")
        self.factory.files_written.append(path)
        return path

    # -- the one method download_audio calls -----------------------------
    def extract_info(self, url, download=False, **kwargs):
        plan = self.factory.plan
        self.factory.extract_calls.append({"url": url, "download": download})

        if plan.raise_exc is not None:
            raise plan.raise_exc
        if plan.return_none:
            return None

        directory = self._output_dir()
        os.makedirs(directory, exist_ok=True)

        audio_path = None
        if download:
            if plan.write_audio:
                audio_path = self._write_audio(directory, plan)
            if plan.write_thumb:
                self._write_thumb(directory, plan)

        info = {
            "id": plan.video_id,
            "title": plan.title,
            "duration": plan.duration,
            "description": "A description of the episode.\nSecond line.",
            "upload_date": "20260714",
            "uploader": "Nate B. Jones",
            "channel_id": "UCfakefakefakefakefake",
            "webpage_url": url,
            "ext": plan.audio_ext.lstrip("."),
            "thumbnails": [{"url": "https://i.ytimg.com/vi/x/hq720.jpg"}],
        }
        if audio_path and plan.report_filepath:
            info["requested_downloads"] = [
                {
                    "filepath": audio_path,
                    "ext": plan.audio_ext.lstrip("."),
                    "format_id": "140",
                }
            ]
        info.update(plan.extra_info)
        return info


class FakeYoutubeDLFactory:
    """Drop-in replacement for `yt_dlp.YoutubeDL` that records everything."""

    def __init__(self, plan=None):
        self.plan = plan or DownloadPlan()
        self.instances = []
        self.opts_seen = []
        self.extract_calls = []
        self.files_written = []

    def __call__(self, opts):
        instance = _FakeYoutubeDL(opts, self)
        self.instances.append(instance)
        self.opts_seen.append(opts)
        return instance

    @property
    def extract_count(self):
        return len(self.extract_calls)

    @property
    def last_opts(self):
        return self.opts_seen[-1]
