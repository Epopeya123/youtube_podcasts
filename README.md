# YouTube Podcasts

Automatically downloads audio from [Nate B. Jones' YouTube channel](https://www.youtube.com/@natebjones) (AI News & Strategy Daily) and saves it as MP3 files you can listen to like a podcast.

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

MP3 files are saved to your phone's storage at:
```
Internal Storage > Podcasts > AI_News_NateBJones
```

Play them with any music app, or use **AntennaPod** (free, from F-Droid) to monitor the folder like a podcast feed.

### Updating yt-dlp

If downloads stop working, update yt-dlp in Termux:
```bash
pip install --upgrade yt-dlp
```

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

# Generate RSS feed (for GitHub Actions mode)
python generate_feed.py
```
