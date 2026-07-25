#!/data/data/com.termux/files/usr/bin/bash
# Pull the latest scripts and re-install the bits that live outside the repo.
#
# Run this in Termux whenever the project changes:
#   bash ~/youtube_podcasts/termux/update.sh
#
# setup.sh copies termux-url-opener into ~/bin and writes ~/run_podcast_download.sh,
# so a plain "git pull" would leave those two stale. This fixes that.

set -e

INSTALL_DIR="$HOME/youtube_podcasts"
CONFIG_FILE="$HOME/.config/youtube_podcasts.conf"

echo "=== Updating YouTube Podcast Downloader ==="
echo ""

if [ ! -d "$INSTALL_DIR/.git" ]; then
    echo "No checkout at $INSTALL_DIR. Run termux/setup.sh first."
    exit 1
fi

cd "$INSTALL_DIR"

# episodes.json is tracked but the downloader used to write to it, which makes
# a pull fail with "local changes would be overwritten". Drop any such edit;
# the phone's real episode index now lives next to the audio instead.
if ! git diff --quiet -- episodes.json 2>/dev/null; then
    echo "Discarding local edits to episodes.json (index now lives with the audio)..."
    git checkout -- episodes.json
fi

echo "[1/4] Pulling latest code..."
git pull --ff-only

echo "[2/4] Updating yt-dlp..."
pip install --upgrade --quiet yt-dlp yt-dlp-ejs mutagen || echo "  (pip upgrade skipped)"

echo "[3/4] Re-installing the share handler..."
mkdir -p "$HOME/bin"
cp "$INSTALL_DIR/termux/termux-url-opener" "$HOME/bin/termux-url-opener"
chmod +x "$HOME/bin/termux-url-opener"

echo "[4/4] Refreshing the scheduled download script..."
cat > "$HOME/run_podcast_download.sh" << 'SCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
# Wait up to 60 seconds for network (Android may have WiFi asleep)
for i in $(seq 1 12); do
    ping -c 1 -W 5 google.com >/dev/null 2>&1 && break
    sleep 5
done
CONFIG_FILE="$HOME/.config/youtube_podcasts.conf"
AUDIO_FORMAT="m4a"
# shellcheck source=/dev/null
[ -f "$CONFIG_FILE" ] && . "$CONFIG_FILE"
cd "$HOME/youtube_podcasts"
python download_audio.py \
    --max-episodes 3 \
    --audio-format "$AUDIO_FORMAT" \
    --output-dir "$HOME/storage/shared/Podcasts/AI_News_NateBJones" 2>&1 \
    | tee -a "$HOME/podcast_download.log"
echo "--- $(date) ---" >> "$HOME/podcast_download.log"
SCRIPT
chmod +x "$HOME/run_podcast_download.sh"

if [ ! -f "$CONFIG_FILE" ]; then
    mkdir -p "$(dirname "$CONFIG_FILE")"
    cat > "$CONFIG_FILE" << 'CONF'
# Settings for the YouTube podcast downloader.
# AUDIO_FORMAT: m4a  YouTube's own audio, saved as-is (default). Best quality,
#                    no conversion step. Plays in every Android player.
#               mp3  re-encode to MP3. Older codec, slower, slightly worse.
#               keep whatever container YouTube served
AUDIO_FORMAT=m4a
MAX_EPISODES=3
# Set to 1 to git pull before every download.
AUTO_UPDATE=0
CONF
    echo "Wrote default settings to $CONFIG_FILE"
fi

echo ""
echo "=== Up to date ==="
echo "Audio format is set in $CONFIG_FILE"
