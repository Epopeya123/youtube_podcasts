# Open issues — reported from the phone (v3.0.0 APK)

Reported after installing the first v3.0.0 build. Ordered by severity.

| # | Issue | State |
|---|-------|-------|
| 1 | Black screen after resuming | **fixed**, needs confirming on the phone |
| 2 | No 10s skip, no seek | **fixed** |
| 3 | DOWNLOAD opens the share sheet | **fixed** |
| 4 | Settings is a wall of text | **fixed** |
| 5 | Keep vs. listen-and-forget | **fixed** |
| 6 | Storage location wording | **fixed** (was never a bug) |

Nothing here is verified on the real device yet: none of it can be, in this
container. Everything below is verified against tests and rendered
screenshots, which is a weaker claim.

---

## 1. Black screen after leaving and returning to the app  🔴 blocker  — FIXED

Leave the app, come back, and the whole screen is black. The only way out is
to kill the app from the recents list and start it again from scratch.

Almost certainly the SDL2/EGL surface being destroyed when Android backgrounds
the activity: Kivy's GL context and every texture are lost, so nothing draws.
`on_pause` returning True is necessary but not sufficient — the textures still
have to be reloaded on the way back.

**Root cause.** Kivy 2.3.1's SDL2 bootstrap answers Android's
"returned to foreground" by dispatching `on_resume` and *nothing else* — no
repaint, no GL reload (`window_sdl2.py:266`). Android may hand back a new EGL
context, so every texture id the app holds is dead. Kivy has both remedies in
its own tree (`get_context().reload()`, and a redraw pump in
`kivy/support.py` commented *"after wakeup, we need to redraw more than once,
otherwise we get a black screen"*) but that hook belongs to the retired pygame
bootstrap and was never wired up for SDL2.

So `Window.canvas.ask_update()` alone — the usual advice — would not have been
enough: the old code already dirtied the canvas by rebuilding the episode list
on resume, so frames were plausibly being drawn *and still black*. The texture
reload is the load-bearing half.

The app made it worse by running `load_channels()` + `load_episodes()`
synchronously inside SDL's event filter, under one bare `except: pass`.

**Fix.** `on_resume` only schedules. On the first real frame: `reload()` →
`flag_update_canvas()` → `update_viewport()`, then a 5 fps repaint pump for
3 s (Kivy's own rate). The library re-read happens 0.5 s later on its own
frame, so it can neither delay the repaint nor suppress it by throwing.
`tests/test_lifecycle.py` drives this through the real `WindowSDL._event_filter`;
14 of its 15 tests fail against the old code.

**Not verified on a device.** No Android, no GPU here — the tests cover the
flags and callbacks that gate rendering, not pixels. If it still goes black,
`logcat` around `SDL_APP_DIDENTERFOREGROUND` plus the presence or absence of
Kivy's `Context: Reloading graphics data...` line separates the remaining causes.

## 2. No 10-second skip, no seek  🔴 missing feature  — FIXED

While playing there is no way to jump forward or back 10 seconds, and no way to
scrub. For podcasts this is essential — it is the control people reach for most.

**Fix.** −10s / +10s buttons, a draggable progress bar, and a `12:04 / 15:47`
readout driven by a 0.5 s `Clock` interval. The interval is cancelled first
thing in `_release_player()`, so a tick can never outlive the `MediaPlayer` it
polls. Completion is detected by polling rather than `setOnCompletionListener`,
which would need a pyjnius `PythonJavaClass` proxy — the class of thing
CLAUDE.md records as breaking on device. 39 tests in `tests/test_player.py`.

## 3. Paste + DOWNLOAD opens the Android share sheet  🟠 wrong behaviour  — FIXED

Paste a link in the Add tab, press DOWNLOAD, and Android asks which app to
share with. The user pasted a link into *this* app and pressed *its* download
button — being asked to pick an app makes no sense there.

**Root cause.** With `targetSdk 34`, Android 11+ package visibility hides
`com.termux` from this app entirely, so `resolveService()` returned null and the
`RUN_COMMAND` intent could never work — it silently fell through to the chooser
every time. Fixed by declaring `<queries><package android:name='com.termux'/></queries>`
via `android.extra_manifest_xml` (`app/extra_manifest.xml`).

**Fix.** The direct path is now the default, behind a real pre-flight check
(`_termux_service_state()` → ready / missing / blocked / unknown); only "ready"
uses `startForegroundService`, everything else falls back to the chooser, so the
worst case is exactly today's behaviour and never a hang. The setting key was
**renamed** `one_tap_termux` → `termux_direct_launch` rather than re-defaulted:
v3.0.0 already wrote `one_tap_termux: false` into every phone's `settings.json`,
so a changed default would never have reached the person who reported this.
24 tests in `tests/test_termux_handoff.py`.

## 4. Settings tab is a wall of text  🟡 polish  — FIXED

Four controls, one short line of help each; the reasoning moved to the README.

## 5. Two kinds of download: keep vs. listen-and-forget  🟡 feature  — FIXED

**Chosen design** (the user picked it): everything downloads to
`Podcasts/<channel>` and stays an ordinary file there, so a file manager,
WhatsApp's attach picker and any cloud-drive app can always reach it. A star in
the now-playing bar marks what to keep; unstarred episodes are removed 30 days
after they arrived.

Deliberately *not* app-private storage: on Android 11+ `Android/data/...` is
unreadable by other apps, which would have taken away the one thing the user
asked for — being able to get at the audio.

Two safety rules, both mutation-tested in `tests/test_keep_cleanup.py`:

* **First-run grace period.** Nothing is deleted until 30 days after the phone
  first saw this feature. Without it, installing the update would have wiped
  every episode already older than 30 days the first time the app opened — a
  library collected over months, gone before the user was told the rule existed.
* **Library boundary.** Cleanup only touches audio files inside the Podcasts
  tree, never a starred one, and never one whose age it could not establish.

Also fixed alongside it: the in-app share button built a `file://` URI, which
receiving apps reject on Android 11+ (`FLAG_GRANT_READ_URI_PERMISSION` grants
nothing without a provider behind it). It now asks MediaStore for a `content://`
URI and falls back to the old behaviour only if the file is not indexed.

<details><summary>Original report</summary>

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

</details>

## 6. Storage location — NOT a bug, but the wording confused the user  ⚪ docs  — FIXED

The user believed episodes used to be saved somewhere different from what was
described. They are not: **`/storage/emulated/0/Podcasts/<folder>` and
"Internal storage → Podcasts → General / AI_News_NateBJones" are the same
place.** `/storage/emulated/0` is exactly what file managers show as "Internal
storage"; `/sdcard` is a symlink to it. Nothing about the location changed.

Action: only ever use the file-manager wording in anything user-facing. Never
write `/storage/emulated/0` or `/sdcard` in UI text or instructions.

---

## Testing gaps this round exposed — and what closed them

The 236-test suite passed while every one of issues 1–4 was present, because it
only ever *built* the app and called methods. It never rendered a frame, never
simulated a pause/resume, and never checked that a user action produced the
intended effect rather than merely not raising.

Closed:

* **Rendering.** `KIVY_GL_BACKEND=mock` stubs every GL entry point, so
  `glCompileShader` compiled nothing and `glReadPixels` returned uninitialised
  framebuffer noise — that, not Mesa, was why screenshots were garbage. Under
  Xvfb, llvmpipe reports OpenGL 4.5 / GLSL 4.50, which is ample.
  `tests/screenshot.py` renders five real screens in ~4 s.
* **Rendering found three more bugs nobody had reported**, all now fixed and
  guarded by `tests/test_ui_layout.py`: the *selected* bottom-nav tab was
  invisible (KivyMD 1.2.0 reads `text_color_active: 1,1,1,1` as "not set" and
  substitutes `primary_color`, which is also `panel_color` — purple on purple);
  both Settings switch thumbs ran off the right screen edge; and episode rows
  showed the sanitised folder name (`AI_News_NateBJones`) instead of the channel
  name the user typed.
* **Clipping.** `tests/check_layout.py` fails if any control lands off screen,
  checked at 720×1560 @2.0 and at the reporting phone's real 1080×2400 @2.75.
  It needs a true GL context, so it runs as its own step in `run_all.sh`.
* **Lifecycle.** `tests/test_lifecycle.py` drives pause/resume through Kivy's
  real `WindowSDL._event_filter`.
* **Behaviour, not "did not raise".** The Termux tests assert DOWNLOAD produces
  a `RUN_COMMAND` service start and **no** `createChooser`.

Still open:

* **No real Android emulator is possible here** — `/dev/kvm` is absent and the
  CPU exposes no virtualisation flags, so there is nothing to run an AVD on.
  Do not promise emulator screenshots.
* **No test here can prove pixels reach a real screen**, that pyjnius resolves a
  Java overload the way it will on device, or that YouTube downloads work — this
  container's IP is blocked by YouTube (HTTP 403).
* Rendering is llvmpipe, not the phone's GPU, and the fonts are the CI ones.
