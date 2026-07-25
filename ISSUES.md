# Open issues — reported from the phone (v3.0.0 APK)

Reported after installing the first v3.0.0 build. Ordered by severity.
Tick them off here as they land.

---

## 1. Black screen after leaving and returning to the app  🔴 blocker

Leave the app, come back, and the whole screen is black. The only way out is
to kill the app from the recents list and start it again from scratch.

Almost certainly the SDL2/EGL surface being destroyed when Android backgrounds
the activity: Kivy's GL context and every texture are lost, so nothing draws.
`on_pause` returning True is necessary but not sufficient — the textures still
have to be reloaded on the way back.

Look at: `on_pause`/`on_resume` in `app/main.py`, and whether the canvas needs
`Window.canvas.ask_update()` / a full reload after resume. Note `load_channels()`
and `load_episodes()` already run on resume and rebuild widgets; check they are
not making it worse (or throwing) before the surface is back.

## 2. No 10-second skip, no seek  🔴 missing feature

While playing there is no way to jump forward or back 10 seconds, and no way to
scrub. For podcasts this is essential — it is the control people reach for most.

Needs: −10s / +10s buttons, a position/duration readout, and ideally a draggable
progress bar. `android.media.MediaPlayer` has `seekTo`, `getCurrentPosition`
and `getDuration`; drive the readout off a `Clock` interval and stop it when
playback stops.

## 3. Paste + DOWNLOAD opens the Android share sheet  🟠 wrong behaviour

Paste a link in the Add tab, press DOWNLOAD, and Android asks which app to
share with. The user pasted a link into *this* app and pressed *its* download
button — being asked to pick an app makes no sense there.

It should just download. The one-tap Termux path (`RUN_COMMAND`) already exists
in `_run_command_in_termux` but is off by default; `setup.sh` now sets
`allow-external-apps = true`, so it should work. Make the direct path the
default and keep the chooser strictly as a fallback when Termux refuses.

## 4. Settings tab is a wall of text  🟡 polish

Too much explanation on screen. Cut it down to what someone actually needs to
decide, and move the reasoning to the README.

## 5. Two kinds of download: keep vs. listen-and-forget  🟡 feature

Right now everything is treated as precious and goes to shared storage. The
user wants a distinction:

* **Keep** — "I want this file." Goes to `Podcasts/<folder>` as it does today,
  browsable in a file manager, uploadable to their own drive. Permanent.
* **Listen only** — "I just want to hear it in the app." Can live somewhere
  disposable (app cache / app-private storage) and be cleaned up automatically.
  The user explicitly does not care where these live or if they get deleted.

Design question to settle: how does the user pick? Probably a toggle on the
download action rather than a global setting, since it is a per-episode
intention.

## 6. Storage location — NOT a bug, but the wording confused the user  ⚪ docs

The user believed episodes used to be saved somewhere different from what was
described. They are not: **`/storage/emulated/0/Podcasts/<folder>` and
"Internal storage → Podcasts → General / AI_News_NateBJones" are the same
place.** `/storage/emulated/0` is exactly what file managers show as "Internal
storage"; `/sdcard` is a symlink to it. Nothing about the location changed.

Action: only ever use the file-manager wording in anything user-facing. Never
write `/storage/emulated/0` or `/sdcard` in UI text or instructions.

---

## Testing gaps this round exposed

The 236-test suite passed while every one of issues 1–4 was present, because it
only ever *builds* the app and calls methods. It never renders a frame, never
simulates a pause/resume, and never checks that a user action produces the
intended effect rather than merely not raising.

* **No real Android emulator is possible here** — `/dev/kvm` is absent and the
  CPU exposes no virtualisation flags, so there is nothing to run an AVD on.
  Do not promise emulator screenshots.
* **Rendered screenshots via xvfb are the fallback** and are worth having, but
  need a working software GL: as of now Kivy's shaders fail to compile
  (`Shader: <fragment> failed to compile`) and screenshots come out as noise.
  Needs a real Mesa/llvmpipe GL context before screenshots mean anything.
* **Lifecycle needs coverage**: pause → resume must be exercised in tests.
* **Behavioural assertions needed**, not just "did not raise": pressing DOWNLOAD
  must produce a download, not a chooser.
