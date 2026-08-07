# Spotify Now Playing

Display the active Spotify track, artist, playback state, and progress on a
BUSY Bar. The physical controls provide play/pause, next track, and volume.

## Spotify setup

1. In the Spotify developer dashboard, add this redirect URI to your app:
   `http://127.0.0.1:4381/callback`
2. Install the official BUSY Bar client used for hardware input events:
   `python -m pip install -r requirements.txt`
3. Start the app with your public Spotify client ID:
   `python app.py --spotify-client-id YOUR_CLIENT_ID`
4. Approve the requested playback permissions in the browser. The refresh
   token is stored in `~/.config/spotify-now-playing/spotify-token.json` with
   user-only permissions. A Spotify client secret is not needed or stored.

For a Wi-Fi-connected BUSY Bar, add `--host BAR_IP --busy-token TOKEN`. USB
uses the default address, `10.0.4.20`, without authentication.

## Controls

- Start/Pause: single tap toggles playback, double tap skips forward, triple tap goes back
- Wheel press: next track
- Wheel rotation: volume down/up
- Back: exit the app on hardware

Playback controls require an active, controllable Spotify device. Spotify may
also require Premium for Player API controls.

## Emulator

Run a credential-free sample:

```sh
python app.py --host 127.0.0.1:8080 --demo
```

The emulator's Start button supports the same single/double/triple-tap gestures.
OK, Back, Up, and Down exercise next, previous, volume up, and volume down directly.
