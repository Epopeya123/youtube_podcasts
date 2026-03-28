[app]

# App metadata
title = YouTube Podcasts
package.name = ytpodcasts
package.domain = org.epopeya123
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
version = 1.0.0

# Dependencies
requirements = python3==3.11.6,kivy==2.3.1,kivymd==2.0.1.dev0,materialyoucolor==2.0.10,exceptiongroup,yt-dlp,certifi,urllib3,requests,brotli,websockets,mutagen,pycryptodomex

# Android settings
android.permissions = INTERNET,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE,FOREGROUND_SERVICE,READ_MEDIA_AUDIO
android.api = 34
android.minapi = 26
android.ndk = 25b
android.accept_sdk_license = True
android.arch = arm64-v8a
android.allow_backup = True

# Include FFmpeg for audio conversion
android.add_recipes = ffmpeg

# App icon and presplash
# icon.filename = %(source.dir)s/icon.png
# presplash.filename = %(source.dir)s/presplash.png

# Android specific
android.manifest.intent_filters = intent_filters.xml
fullscreen = 0
android.presplash_color = #1a1a2e

# Orientation
orientation = portrait

# Logging
log_level = 2

[buildozer]
log_level = 2
warn_on_root = 1
