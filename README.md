# YouTube Podcasts

Downloads audio from YouTube and saves it to your phone so you can listen to it like a podcast.

Two halves that work together:

- **The Android app** — share a YouTube link to it and the download starts. The Downloads tab lists your episodes with their thumbnails, plays them with the artwork on screen, and can delete or export them.
- **Termux** — does the actual downloading, on a schedule and on demand.

Episodes are saved as ordinary files under `Internal Storage > Podcasts > <channel>`, so you can browse them in any file manager and upload them to your own drive.

## Audio format

By default episodes keep YouTube's own audio exactly as sent: AAC in an `.m4a` file.

YouTube has no MP3 to give — it serves AAC and Opus. Asking for MP3 means decoding its audio and re-encoding it into an older codec, which is both slower and slightly worse sounding, since you are making a lossy copy of an already-lossy file. Keeping the `.m4a` avoids that entirely. It plays in every music and podcast app on Android.

Existing `.mp3` files keep working and play alongside the new ones. To go back to MP3, use the app's Settings tab or set `AUDIO_FORMAT=mp3` in `~/.config/youtube_podcasts.conf`.

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
Internal Storage > Podcasts > <channel folder>
```

Each episode is an audio file plus a `.jpg` of its thumbnail. Play them in the app's Downloads tab, in any music app, or point **AntennaPod** (free, from F-Droid) at the folder.

Because they are ordinary files in shared storage, you can upload them to your own cloud drive — either from a file manager, or with the export button in the app's now-playing bar. Deleting an episode in the Downloads tab removes the file from this folder for real.

### Downloading a single video

Share any YouTube link to the **YouTube Podcasts** app and the download starts on its own. You can also paste a link into the app's Add tab.

### Updating

```bash
bash ~/youtube_podcasts/termux/update.sh
```

This pulls the latest code, upgrades yt-dlp, and refreshes the two scripts that live outside the checkout (`~/bin/termux-url-opener` and `~/run_podcast_download.sh`) — a plain `git pull` leaves those stale.

> **Node must be v22 or newer.** yt-dlp uses it to solve YouTube's JavaScript challenges; on an older Node it falls back to weaker clients and downloads get slow. `pkg upgrade nodejs-lts` fixes it.

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
