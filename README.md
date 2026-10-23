# Tesla Coil Music Player

Plays MIDI songs on a tesla coil. The `controller/` folder contains the PlatformIO firmware of the tesla coil
controller, the `gui/` folder contains a NiceGUI web interface that sends the songs to the controller over serial.

## Converting audio to MIDI

```sh
uv run --extra convert tesla-coil-convert song.mp3
```

Converts audio files (mp3, wav, flac, ogg, ...) into `.mid` files next to them. The instruments are first separated
with [demucs](https://github.com/adefossez/demucs) into vocals, guitar, piano, other and bass, and each is
transcribed into its own part with Spotify's [basic-pitch](https://github.com/spotify/basic-pitch) model. Together
the parts can have more notes at once than the coil can play, select the ones you want in the web interface.

- `--drums` also transcribes the drums (as pitched notes)
- `--max-notes` keeps only the loudest notes when more play at once within a part
- `--model htdemucs_ft` separates more cleanly, but only into vocals, bass and other
- `--no-separate` skips the separation and transcribes everything into a single part, which is much faster

The `convert` extra installs the CPU build of PyTorch, the web interface itself doesn't need it. The demucs model is
downloaded on the first run. See `--help` for the other detection thresholds.

## Web interface

```sh
TESLA_COIL_PASSWORD=<password> uv run tesla-coil-gui
```

Then open `http://<this machine>:8080` from any device on the network. A password is required when listening on
the network, use `--host 127.0.0.1` to run it on this machine only without a password. See `--help` for the serial
port, web port and songs folder options. The song browser starts in `./songs` of the directory it's launched from.

Features:

- connect to the controller (auto-connects to `/dev/ttyACM0` on startup) and reset it, it pings the controller
  while idle and reconnects by itself when the USB connection drops, resuming a playing song
- browse folders for songs and upload new `.mid` files into the current folder
- play, pause, stop and seek the songs on the coil or on the speaker of the browser with the player bar at the bottom
  of every page (the space bar toggles play / pause)
- toggle the parts of a song (tracks and the MIDI channels within them), also during playback
- preview a single part on the speaker of the device the browser runs on
- play notes live on the coil and/or the speaker with the on-screen piano or the laptop keyboard (like LMMS: the
  `Z`/`S` rows play the lower octave, the `Q`/number rows the upper octave) on the keyboard page
- set the spark on-times on the settings page: the on-time per frequency band (also by dragging the points of the
  chart), the volume, the min and max on-time and the velocity sensitivity. They're saved to `./coil-config.json`
  (see `--config`) and sent to the controller after connecting, which needs the firmware of this version
