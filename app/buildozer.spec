[app]

# App metadata
title = YouTube Podcasts
package.name = ytpodcasts
package.domain = org.epopeya123
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json,xml
source.include_patterns = ffmpeg_bin/*
version = 1.1.0

# Dependencies
requirements = python3,kivy==2.3.1,kivymd==1.2.0,yt-dlp,certifi,urllib3,requests,mutagen,pycryptodome

# Android settings
android.permissions = INTERNET,READ_MEDIA_AUDIO
android.api = 34
android.minapi = 26
android.ndk = 25b
android.accept_sdk_license = True
android.arch = arm64-v8a
android.allow_backup = False

# Bootstrap
p4a.bootstrap = sdl2

# Android intent filters for receiving shared URLs
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
