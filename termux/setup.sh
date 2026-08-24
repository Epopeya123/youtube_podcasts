#!/data/data/com.termux/files/usr/bin/bash
# Termux setup script for YouTube Podcast Downloader
# Run this ONCE after installing Termux from F-Droid:
#   bash setup.sh
# To pick up later changes, run termux/update.sh instead.

set -e

echo "=== YouTube Podcast Downloader - Termux Setup ==="
echo ""

INSTALL_DIR="$HOME/youtube_podcasts"
PODCAST_DIR="$HOME/storage/shared/Podcasts"
CONFIG_FILE="$HOME/.config/youtube_podcasts.conf"

echo "[1/8] Updating Termux packages..."
pkg update -y && pkg upgrade -y

echo "[2/8] Installing Python, ffmpeg, Node.js, and tools..."
# nodejs-lts must be v22 or newer: yt-dlp rejects older versions for YouTube's
# JavaScript challenges, and without a usable runtime downloads get slow.
pkg install -y python ffmpeg nodejs-lts cronie termux-services git

NODE_MAJOR=$(node --version 2>/dev/null | sed 's/^v//; s/\..*//')
if [ -n "$NODE_MAJOR" ] && [ "$NODE_MAJOR" -lt 22 ]; then
    echo ""
    echo "  WARNING: Node $(node --version) is too old for yt-dlp (needs v22+)."
    echo "           Run 'pkg upgrade nodejs-lts' or downloads will be slow."
    echo ""
fi

echo "[3/8] Installing yt-dlp..."
# Plain packages, NOT "yt-dlp[default]": the [default] extra needs brotli and
# pycryptodomex, C extensions with no Termux wheels, so under set -e that pip
# run would abort this whole setup in a doomed source build. These three are
# pure Python and install everywhere. yt-dlp-ejs solves YouTube's JS
# challenges; mutagen writes cover art into m4a without spawning ffmpeg.
pip install --upgrade yt-dlp yt-dlp-ejs mutagen

echo "[4/8] Requesting storage access..."
echo "A popup will appear asking for storage permission. Tap ALLOW."
termux-setup-storage
sleep 3

# Shared storage, so the files show up in any file manager and can be uploaded
# to your own cloud drive, and the app can list and delete them.
mkdir -p "$PODCAST_DIR"
echo "Podcasts will be saved to: $PODCAST_DIR"

echo "[5/8] Letting the app start downloads directly..."
# Without allow-external-apps the app has to go through the Android share
# chooser every time; with it, sharing a link starts the download in one tap.
mkdir -p "$HOME/.termux"
if grep -q "^allow-external-apps" "$HOME/.termux/termux.properties" 2>/dev/null; then
    sed -i 's/^allow-external-apps.*/allow-external-apps = true/' "$HOME/.termux/termux.properties"
else
    echo "allow-external-apps = true" >> "$HOME/.termux/termux.properties"
fi
termux-reload-settings 2>/dev/null || true

echo "[6/8] Setting up the downloader..."
mkdir -p "$INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone https://github.com/Epopeya123/youtube_podcasts.git "$INSTALL_DIR" || {
        echo "Could not clone repo. Please copy download_audio.py manually to $INSTALL_DIR"
    }
fi

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
fi

echo "[7/8] Setting up Share-to-Download..."
mkdir -p "$HOME/bin"
cp "$INSTALL_DIR/termux/termux-url-opener" "$HOME/bin/termux-url-opener"
chmod +x "$HOME/bin/termux-url-opener"

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

echo "[8/8] Setting up automatic schedule..."
sv-enable crond 2>/dev/null || true
(crontab -l 2>/dev/null | grep -v "run_podcast_download"; echo "0 */6 * * * $HOME/run_podcast_download.sh") | crontab -

echo ""
echo "=== Setup Complete! ==="
echo ""
echo "Your podcasts are saved to:"
echo "  Internal Storage > Podcasts > <channel folder>"
echo "They are ordinary files, so you can upload them to your own drive,"
echo "and the app's Downloads tab can play and delete them."
echo ""
echo "Audio format is 'm4a' (YouTube's own audio, no re-encoding). Change in:"
echo "  $CONFIG_FILE"
echo ""
echo "To download episodes right now, run:"
echo "  ~/run_podcast_download.sh"
echo ""
echo "Automatic downloads are scheduled every 6 hours."
echo "To update later:  bash ~/youtube_podcasts/termux/update.sh"
