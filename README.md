# YouTube Podcasts

Downloads audio from YouTube and saves it to your phone so you can listen to it like a podcast.

Two halves that work together:

- **The Android app** — share a YouTube link to it and the download starts. The Downloads tab lists your episodes with their thumbnails, plays them with the artwork on screen, and can delete or export them.
- **Termux** — does the actual downloading, on a schedule and on demand.

Episodes are saved as ordinary files under `Internal storage > Podcasts > <channel>`, so you can browse them in any file manager and upload them to your own drive.

## The app's settings

The Settings tab is deliberately terse — one line per choice. The reasoning lives here.

### Audio format — M4A or MP3

By default episodes keep YouTube's own audio exactly as sent: AAC in an `.m4a` file.

YouTube has no MP3 to give — it serves AAC and Opus. Asking for MP3 means decoding its audio and re-encoding it into an older codec, which is both slower and slightly worse sounding, since you are making a lossy copy of an already-lossy file. Keeping the `.m4a` avoids that entirely. It plays in every music and podcast app on Android.

Existing `.mp3` files keep working and play alongside the new ones. To go back to MP3, use the app's Settings tab or set `AUDIO_FORMAT=mp3` in `~/.config/youtube_podcasts.conf`.

### Episodes — where they are, and GRANT FILE ACCESS

Episodes go to `Internal storage > Podcasts > <channel>`, which is shared storage: ordinary files, browsable in any file manager, uploadable to your own drive. The app has to be allowed to read that folder, which on Android 11 and newer means the "Allow access to manage all files" switch — that is what **GRANT FILE ACCESS** opens. Without it the Downloads tab stays empty even though the files are there.

### Download shared links

On: sharing a YouTube link to the app starts the download immediately. Off: the link is only pasted into the Add tab and waits for you to press DOWNLOAD.

### Start Termux directly

On (the default): pressing DOWNLOAD hands the job straight to Termux through its `RUN_COMMAND` service and the download starts — no app chooser, no extra taps. This needs three things, and `termux/setup.sh` plus the app's own manifest set up all of them:

- `allow-external-apps = true` in `~/.termux/termux.properties` (written by `setup.sh`; run `bash ~/youtube_podcasts/termux/update.sh` on a phone set up before this existed),
- the `com.termux.permission.RUN_COMMAND` permission, granted under Android Settings → Apps → YouTube Podcasts → Additional permissions,
- Termux visible to the app at all: Android 11+ hides other apps unless they are declared in the manifest's `<queries>` element (`app/extra_manifest.xml`).

Before every download the app asks the package manager whether Termux's service actually resolves, because starting a service that is not there looks like success from the app's side and would leave you with nothing happening. If it does not resolve, the share menu opens instead and the status line says why.

Turn the switch **off** if you would rather pick Termux from the share menu each time. That is also the escape hatch if a download never appears: Termux can accept the request and drop it when `allow-external-apps` is not set, and the app says so on screen a few seconds later.

## Option 1: Termux (Android phone) — Recommended

Run everything on your phone. Downloads happen automatically every 6 hours using your phone's internet (not blocked by YouTube).

### Setup (one-time)

1. Install **[F-Droid](https://f-droid.org/)** (open-source app store)
2. Install **Termux** from F-Droid
3. Open Termux and paste these commands:

```bash
pkg install -y git
git clone https://github.com/Epopeya123/youtube_podcasts.git ~/youtube_podcasts
cd ~/youtube_podcasts/termux
bash setup.sh
```

4. When prompted, **allow storage access**
5. To download episodes right now:

```bash
~/run_podcast_download.sh
```

### Where are my episodes?

Saved to your phone's storage at:
```
Internal storage > Podcasts > <channel folder>
```

Each episode is an audio file plus a `.jpg` of its thumbnail. Play them in the app's Downloads tab, in any music app, or point **AntennaPod** (free, from F-Droid) at the folder.

Because they are ordinary files in shared storage, you can upload them to your own cloud drive or attach them to a message — from a file manager, from WhatsApp's own attach-file picker, or with the share button in the app's now-playing bar. Deleting an episode in the Downloads tab removes the file from this folder for real.

### Starring: what is kept and what goes away

Every download lands in `Podcasts/<channel>` and stays an ordinary file there. Nothing is ever hidden away somewhere you cannot reach it.

The **star** in the now-playing bar decides what survives:

- **Starred** — kept for good. Nothing removes it but you.
- **Not starred** — removed automatically 30 days after it arrived, so the phone does not slowly fill with things you listened to once.

Starring does not move the file or change how you get at it. It only marks it.

Two deliberate safety rules: nothing is deleted until the app has been installed for 30 days, so updating never wipes a library you already had; and the cleanup only ever touches audio files inside the `Podcasts` folder.

### Downloading a single video

Share any YouTube link to the **YouTube Podcasts** app and the download starts on its own. You can also paste a link into the app's Add tab and press DOWNLOAD — that hands the link straight to Termux, with no app chooser in the way (see [Start Termux directly](#start-termux-directly)).

### Updating

```bash
bash ~/youtube_podcasts/termux/update.sh
```

This pulls the latest code, upgrades yt-dlp, and refreshes the two scripts that live outside the checkout (`~/bin/termux-url-opener` and `~/run_podcast_download.sh`) — a plain `git pull` leaves those stale.

> **Node must be v22 or newer.** yt-dlp uses it to solve YouTube's JavaScript challenges; on an older Node it falls back to weaker clients and downloads get slow. `pkg upgrade nodejs-lts` fixes it.

### Every download suddenly fails with "HTTP Error 403: Forbidden"

That is YouTube, not the phone: every few months YouTube blocks the player
clients that older yt-dlp releases use (most recently on 2026-08-17), and from
that day every download 403s while titles and thumbnails still load. The fix is
always a newer yt-dlp. The downloader now handles this itself — when it sees
that 403 it upgrades yt-dlp and retries once, and it warns at startup when
yt-dlp is old enough to be at risk. If it still fails, run the update script
above; and if yt-dlp is already current, YouTube has broken downloads for
everyone and a fixed release usually appears within a day or two.

## Option 2: GitHub Actions (cloud)

> Note: YouTube blocks downloads from GitHub's servers. This option works only if YouTube unblocks datacenter IPs or you set up a self-hosted runner.

1. Fork this repo
2. Enable **GitHub Pages** (source: `gh-pages` branch)
3. Enable **GitHub Actions**
4. Trigger the first run: Actions > Update Podcast Feed > Run workflow
5. Subscribe to `https://YOUR_USERNAME.github.io/youtube_podcasts/feed.xml`

## Option 3: Local (computer)

```bash
pip install -r requirements.txt
# Also need ffmpeg installed

# Download latest 5 episodes to a custom folder
python download_audio.py --max-episodes 5 --output-dir ~/Music/NateBJones

# One specific video
python download_audio.py --url "https://youtu.be/VIDEO_ID" --output-dir ~/Music

# Re-encode to MP3 instead of keeping the original AAC (slower)
python download_audio.py --url "https://youtu.be/VIDEO_ID" --audio-format mp3

# Generate RSS feed (for GitHub Actions mode)
python generate_feed.py
```

## Tests

No Android device needed — the app runs headless, so KivyMD breakage is caught in seconds instead of after a 30-minute APK build.

```bash
bash tests/run_all.sh
```
