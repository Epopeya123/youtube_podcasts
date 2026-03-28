[app]

# App metadata
title = YouTube Podcasts
package.name = ytpodcasts
package.domain = org.epopeya123
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json,xml
version = 2.0.0

# Dependencies (lightweight - Termux handles downloading)
requirements = python3,kivy==2.3.1,kivymd==1.2.0,certifi,pyjnius

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
orientation = portrait
log_level = 2

[buildozer]
log_level = 2
warn_on_root = 1
