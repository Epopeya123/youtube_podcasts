# CLAUDE.md — Project context for Claude Code

## Project overview
YouTube Podcasts — download audio from YouTube videos and listen as podcasts.

Two tools in one repo:
1. **Termux CLI tool** (`download_audio.py`, `termux/`) — runs on Android phone via Termux, downloads audio via yt-dlp + ffmpeg, scheduled every 6 hours
2. **Android app** (`app/`) — KivyMD 1.2.0 app built with Buildozer via GitHub Actions. The app is a UI control panel that launches Termux for all downloads (Termux has ffmpeg + yt-dlp). Supports multi-channel management, a Downloads tab that plays/deletes episodes, and share-to-download.

## How the two halves talk
- Termux writes audio **and** a `.episodes.json` manifest into `/sdcard/Podcasts/<channel>/`. Shared storage on purpose: the files stay browsable, uploadable to the user's own drive, and deletable by the app.
- The app sends Termux a payload string: `<url>|||<folder>|||<format>` or `REFRESH:<channel_url>|||<folder>|||<format>`, parsed by `termux/termux-url-opener`.
- The app never downloads anything itself; it reads the manifests to build the Downloads tab.

## When every download 403s (learned the hard way, 2026-08)
- **YouTube kills yt-dlp's player clients every few months.** Extraction still
  works (title, thumbnail download fine) and then googlevideo.com answers
  HTTP 403 for the media bytes. On 2026-08-17 YouTube 403'd ALL formats via the
  `android_vr` client that yt-dlp used by default; yt-dlp 2026.08.19 moved its
  defaults to `visionos` + `web`. The fix is ALWAYS "update yt-dlp" — never
  header tweaks or stream retries.
- `download_audio.py` self-heals: `MIN_KNOWN_GOOD_YTDLP` triggers an update
  before the first extraction, an unexplained 403 triggers
  `pip install --upgrade "yt-dlp[default]"` plus one guarded re-exec
  (`--after-self-update`), and a >60-day-old yt-dlp gets a startup warning.
  When YouTube purges again: bump `MIN_KNOWN_GOOD_YTDLP` to the yt-dlp release
  that fixes it, and check `_DEFAULT_CLIENTS` in yt-dlp's
  `extractor/youtube/_video.py` to see what changed.
- **Install plain `yt-dlp yt-dlp-ejs`, NEVER `yt-dlp[default]`, on the phone.**
  The [default] extra drags in brotli and pycryptodomex - C extensions with no
  Termux-compatible wheels (bionic libc matches no manylinux tag) and no
  compiler on the phone to build them, so pip fails and installs *nothing*.
  Under `set -e` that aborted setup.sh entirely; in update.sh it silently left
  yt-dlp at the broken version while printing "Up to date". The plain pair is
  pure Python; a solver version mismatch is only a runtime warning.
- A 403 in a dev container proves nothing — this container's IP is blocked by
  YouTube outright. Only the phone can confirm downloads work.

## Download speed (learned the hard way)
- **One extraction per video.** Metadata extraction is the slow step on a phone (player JS + a JS challenge through Node). The old code called `extract_info` twice per video — once just to read the duration — which roughly doubled the wait. Duration comes from the single download extraction; Shorts get sorted afterwards.
- **`js_runtimes` must be passed explicitly.** yt-dlp only enables **Deno** by default. Termux ships Node, so without `js_runtimes` yt-dlp logs "No supported JavaScript runtime could be found", falls back to weaker player clients and loses formats. Node must be **v22+** or yt-dlp marks it `(unsupported)`.
- **m4a costs nothing, MP3 costs a lot — and sounds worse.** YouTube serves AAC and Opus, never MP3, so asking for MP3 is a lossy-to-lossy re-encode. `bestaudio[ext=m4a]` + `preferredcodec: "m4a"` means yt-dlp sees the file is already m4a and **skips ffmpeg entirely**. MP3 forces a full Opus→MP3 decode+encode of the whole episode (~13s CPU for a 16-min episode on x86, far worse on a phone ARM core).
- **`sleep_interval` only when walking a channel.** It was adding 2-5s to every single-video download for no reason.
- **Always set `socket_timeout`/`retries`/`http_chunk_size`.** Without a timeout a stalled read hangs forever; `http_chunk_size` (ranged requests) also dodges YouTube's throttling of long single-connection reads.
- **`EmbedThumbnail` DELETES the thumbnail file** unless you pass `already_have_thumbnail: True`. That is why episodes had no artwork sidecar for the app to show.
- **Never write `episodes.json` into the checkout on the phone** — it is tracked, so the next `git pull` fails. `resolve_episodes_file()` puts the index next to the audio when the output dir is outside the repo.

## Testing before a 30-minute APK build
Kivy + KivyMD 1.2.0 run headless in CI/dev containers, so app breakage is catchable in seconds:
```
xvfb-run -a -s "-screen 0 1024x768x24" env KIVY_NO_ARGS=1 KIVY_LOG_MODE=PYTHON \
  KIVY_GL_BACKEND=mock KIVY_AUDIO=mock python3 ...
```
- `KIVY_WINDOW=mock` does **not** exist in Kivy 2.3.1 — it aborts with "Unable to get a Window". Use xvfb instead.
- Kivy hijacks stdout/stderr; without `KIVY_LOG_MODE=PYTHON` tracebacks are invisible.
- Needs system packages `xvfb` and `libmtdev1`.
- `bash tests/run_all.sh` runs the linter + suite.

## Critical rules (learned the hard way)
- **KivyMD version**: Must use **1.2.0** (not 2.x). Widget names are completely different between versions. TwoLineAvatarIconListItem does NOT work in this build — use TwoLineAvatarListItem.
- **pyjnius**: Python `bytes` does NOT convert to Java `byte[]`. Must use `bytearray`. All Java String args must use `autoclass('java.lang.String')`. `Intent.createChooser` needs `cast('java.lang.CharSequence', title)`.
- **Cython**: Must pin `cython<3` in CI — pyjnius fails to compile with Cython 3.x (`long` type removed).
- **Buildozer root**: Set `BUILDOZER_WARN_ON_ROOT=0` env var in CI. Without it, buildozer silently exits with no APK.
- **FFmpeg**: p4a recipe is broken. Bundle prebuilt static binary from Tyrrrz/FFmpegBin (SHA256 verified). Set `ffmpeg_location` in yt-dlp opts.
- **Android scoped storage**: App-private files (`getExternalFilesDir`) are NOT accessible by other apps. Use MediaStore API to share files. `relative_path` needs trailing `/`. Requires API 29+.
- **StrictMode**: Must be disabled for `file://` URIs to work on Android 7+.
- **MediaPlayer**: Must call `setAudioStreamType(STREAM_MUSIC)` before `setDataSource`. Use `pause()`/`start()` for toggle, not `stop()`/`prepare()`/`start()`.
- **stdout/stderr**: Kivy on Android replaces these with non-file objects. Patch with `open(os.devnull, 'w')` before importing yt-dlp.

## Audit checklist (run before every push)
0. `bash tests/run_all.sh` — headless app smoke test + KivyMD linter + downloader tests
1. Syntax: `py_compile.compile('app/main.py', doraise=True)`
2. No KivyMD 2.x widgets (MDButton, MDTopAppBarTitle, MDListItem, MDSnackbar with MDSnackbarText)
3. All methods have try/except
4. All pyjnius calls use proper Java types (String, cast)
5. bytearray for OutputStream writes, not bytes
6. Lambda closures capture variables by value (default args)
7. Background threads for file I/O (share, download)
8. _episodes_lock for all JSON file access
9. Crash logging active (sys.excepthook)
10. No unused imports

## Build
- GitHub Actions workflow: `.github/workflows/build_apk.yml`
- Trigger: push to `app/` or workflow file, or manual dispatch
- Uses official `kivy/buildozer` cache + Tyrrrz/FFmpegBin for ffmpeg
- APK artifact uploaded on success
- Build log saved to `build-logs/latest-build.log` (readable via GitHub API)

## User's phone
- Motorola Edge 60, Android 14 (API 34)
- Termux installed with yt-dlp, ffmpeg, nodejs-lts, yt-dlp-ejs
- F-Droid as app store for Termux

## Repository
- Owner: epopeya123
- Branch: main
- Termux tool: download_audio.py, generate_feed.py, termux/
- Android app: app/main.py, app/buildozer.spec
- Tests: tests/ (headless, no device needed)

## Updating the phone
`git pull` alone reaches `download_audio.py` and `generate_feed.py`. It does **not**
reach `~/bin/termux-url-opener` or `~/run_podcast_download.sh`, which `setup.sh`
copies outside the checkout — run `bash ~/youtube_podcasts/termux/update.sh` for those.
