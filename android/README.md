# JobPilot — Android companion

A thin WebView client, ported from TechRadar's Android app (same shape,
same build toolchain). It is **not** a standalone app — it points at a
JobPilot server that's already running somewhere reachable (your PC on
the same Wi-Fi, `jobpilot web --port 8765` with the machine's firewall
allowing LAN access), the same way TechRadar's own client works. It's a
way to check fit scores and application outcomes from a phone while
JobPilot runs on the desktop — not a way for someone else to run JobPilot
without a desktop machine anywhere in the picture.

## Build

    python build_apk.py

Requires the Android SDK build-tools (35.0.0) and an Android Studio JBR on
the machine doing the build — same requirement as TechRadar's, set via the
`ANDROID_SDK` / `ANDROID_JBR` environment variables if they're not at the
default paths hardcoded in the script.

Produces `build/jobpilot.apk`, self-signed with a debug keystore generated
on first build (`build/jobpilot.keystore` — gitignored, per-machine, fine
to regenerate, never treat it as a real release key).

## TODO

The launcher icon is copied unchanged from TechRadar's — same visual
placeholder, easy to tell apart only by the label under it. Swap
`res/mipmap-*/ic_launcher.png` for something JobPilot-specific whenever
that's worth the time.
