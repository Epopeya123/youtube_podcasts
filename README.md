# YouTube Podcasts

Automatically downloads audio from [Nate B. Jones' YouTube channel](https://www.youtube.com/@natebjones) (AI News & Strategy Daily) and serves it as a podcast RSS feed.

## How it works

1. A GitHub Actions workflow runs every 6 hours
2. It checks for new YouTube videos and downloads the audio as MP3 (64kbps mono)
3. Audio files are uploaded to GitHub Releases (stable download URLs)
4. An RSS podcast feed is generated and deployed to GitHub Pages

## Subscribe

Add this RSS feed URL to your podcast app (iVoox, Pocket Casts, AntennaPod, Overcast, etc.):

```
https://epopeya123.github.io/youtube_podcasts/feed.xml
```

### iVoox

1. Open iVoox app
2. Go to **Suscripciones** (Subscriptions)
3. Tap the **+** icon
4. Paste the feed URL above in the "Nombre o URL" field

## Setup (if you fork this repo)

1. Enable **GitHub Pages** in your repo settings (source: `gh-pages` branch)
2. Enable **GitHub Actions** (it should be enabled by default)
3. Optionally trigger the first run manually: Actions > Update Podcast Feed > Run workflow
4. Update the `GITHUB_REPO` and `GITHUB_PAGES_URL` constants in `generate_feed.py`

## Manual trigger

You can manually trigger a download from the Actions tab, optionally specifying how many episodes to download (default: 5).

## Local usage

```bash
pip install -r requirements.txt
# Also need ffmpeg installed

# Download latest 5 episodes
python download_audio.py --max-episodes 5

# Generate RSS feed
python generate_feed.py
```
