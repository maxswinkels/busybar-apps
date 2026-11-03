# Apple Music Winamp

A macOS Apple Music controller with a nostalgic 72×16 LED display. Python standard library only; no pip or Node installation required to run the app.

```sh
python3 app.py                         # USB bar at 10.0.4.20
python3 app.py --host 127.0.0.1:8321    # BUSY Bar Manager
BUSY_HTTP_PASSWORD=your-token python3 app.py --host 192.168.1.50
```

Open Music and select a song. Allow the launching terminal or host app to control Music when macOS prompts for Automation permission. Keep the Mac and controller running and put the bar in APPS mode.

| Button | Action |
| --- | --- |
| OK | Play / pause |
| BACK | Previous track |
| START | Next track |

White song title and gray artist scroll independently above a green progress bar. The permanent elapsed counter sits below a rotating spectrum, waveform, and dot scope. Song changes trigger a bright burst; successful button commands flash their action. Animated lightning accompanies the Winamp credit, with an occasional llama Easter egg.

The visualizers are decorative: no audio, microphone, or screen capture. Winamp is a visual homage, not a runtime dependency. Music metadata and controls use local AppleScript. Apple Music on an iPhone is not supported.

Higher-priority display owners cause the app to yield and retry. Controls are checked against a fresh successful draw and, when available, Manager's screen owner. Mode-switch input events disable controls outside APPS mode. Disable other interactive controllers to avoid multiple apps reacting to the same buttons.

Ctrl+C or SIGTERM stops the app and clears only its own display. Lost connections are retried. If Automation permission was denied, check System Settings → Privacy & Security → Automation.

## Preview and checks

From the gallery root:

```sh
npm run preview -- apple-music-winamp --seconds 8 -- --demo
npm run check -- apple-music-winamp --run -- --demo
```

`--demo` uses sample track metadata and renders the actual app on any platform without controlling Music or listening to hardware buttons. It is intended for previews and review, not as a music source.

MIT License. Copyright (c) 2026 Nachenko. Unofficial and unaffiliated with Apple, Winamp, or Flipper Devices.
