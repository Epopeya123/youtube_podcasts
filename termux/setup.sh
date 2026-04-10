#!/data/data/com.termux/files/usr/bin/bash
# Termux setup script for YouTube Podcast Downloader
# Run this ONCE after installing Termux from F-Droid

set -e

echo "=== YouTube Podcast Downloader - Termux Setup ==="
echo ""

# Update packages
echo "[1/5] Updating Termux packages..."
pkg update -y && pkg upgrade -y

# Install required packages
echo "[2/5] Installing Python, ffmpeg, Node.js, and tools..."
pkg install -y python ffmpeg nodejs-lts cronie termux-services

# Install yt-dlp and its YouTube challenge solver
echo "[3/5] Installing yt-dlp..."
pip install yt-dlp yt-dlp-ejs

# Request storage permission
echo "[4/5] Requesting storage access..."
echo "A popup will appear asking for storage permission. Tap ALLOW."
termux-setup-storage
sleep 3

# Create podcast directory on phone storage
PODCAST_DIR="$HOME/storage/shared/Podcasts/AI_News_NateBJones"
mkdir -p "$PODCAST_DIR"
echo "Podcasts will be saved to: $PODCAST_DIR"

# Clone the repo (or copy the script)
echo "[5/5] Setting up the downloader..."
INSTALL_DIR="$HOME/youtube_podcasts"
mkdir -p "$INSTALL_DIR"

# Download the script directly from the repo
if command -v git &>/dev/null; then
    pkg install -y git
fi

if [ -d "$INSTALL_DIR/.git" ]; then
    cd "$INSTALL_DIR" && git pull
else
    git clone https://github.com/Epopeya123/youtube_podcasts.git "$INSTALL_DIR" 2>/dev/null || {
        echo "Could not clone repo. Please copy download_audio.py manually to $INSTALL_DIR"
    }
fi

# Create the run script
cat > "$HOME/run_podcast_download.sh" << 'SCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
# Wait up to 60 seconds for network (Android may have WiFi asleep)
for i in $(seq 1 12); do
    ping -c 1 -W 5 google.com >/dev/null 2>&1 && break
    sleep 5
done
cd "$HOME/youtube_podcasts"
python download_audio.py --max-episodes 3 --output-dir "$HOME/storage/shared/Podcasts/AI_News_NateBJones" 2>&1 | tee -a "$HOME/podcast_download.log"
echo "--- $(date) ---" >> "$HOME/podcast_download.log"
SCRIPT
chmod +x "$HOME/run_podcast_download.sh"

# Set up URL opener (Share > Termux to download)
echo "[Setting up Share-to-Download...]"
mkdir -p "$HOME/bin"
cp "$INSTALL_DIR/termux/termux-url-opener" "$HOME/bin/termux-url-opener"
chmod +x "$HOME/bin/termux-url-opener"

# Set up cron job (every 6 hours)
echo "[Setting up automatic schedule...]"
sv-enable crond 2>/dev/null || true

# Add cron job
(crontab -l 2>/dev/null | grep -v "run_podcast_download"; echo "0 */6 * * * $HOME/run_podcast_download.sh") | crontab -

echo ""
echo "=== Setup Complete! ==="
echo ""
echo "Your podcasts will be saved to:"
echo "  Internal Storage > Podcasts > AI_News_NateBJones"
echo ""
echo "To download episodes right now, run:"
echo "  ~/run_podcast_download.sh"
echo ""
echo "Automatic downloads are scheduled every 6 hours."
echo ""
echo "Play the MP3 files with any music/podcast app on your phone."
echo "Recommended: use a folder-based player or AntennaPod (local folder)."
