# CLAUDE.md — Project context for Claude Code

## Project overview
YouTube Podcasts — download audio from YouTube videos and listen as podcasts.

Two tools in one repo:
1. **Termux CLI tool** (`download_audio.py`, `termux/`) — runs on Android phone via Termux, downloads audio via yt-dlp + ffmpeg, scheduled every 6 hours
2. **Android app** (`app/`) — KivyMD 1.2.0 app built with Buildozer via GitHub Actions. The app is a UI control panel that launches Termux for all downloads (MP3 conversion requires Termux's ffmpeg). Supports multi-channel management with per-channel refresh.

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
