# Tesla Coil Music Player

Plays MIDI songs on a tesla coil. The `controller/` folder contains the PlatformIO firmware of the tesla coil
controller, the `gui/` folder contains a NiceGUI web interface that sends the songs to the controller over serial.

The controller (a Nucleo-F746ZG) plays up to 16 voices on the coil pin D10. The sparks are made in hardware by its
timers, exact to 9.26 ns, and kept within a maximum duty cycle. The web interface turns the notes into voices and
streams them half a second ahead with timestamps, so the controller plays them on its own clock. See
[controller/PROTOCOL.md](controller/PROTOCOL.md) for the serial protocol.

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
  and reconnects by itself when the USB connection drops, resuming a playing song. The controller silences the coil
  when the web interface stops sending for a second
- browse folders for songs and upload new `.mid` files into the current folder
- play, pause, stop and seek the songs on the coil or on the speaker of the browser with the player bar at the bottom
  of every page (the space bar toggles play / pause)
- toggle the parts of a song (tracks and the MIDI channels within them), also during playback
- play the pitch bends of a song (also its bend range, RPN 0), on the coil and in the speaker preview
- preview a single part on the speaker of the device the browser runs on
- play notes live on the coil and/or the speaker with the on-screen piano or the laptop keyboard (like LMMS: the
  `Z`/`S` rows play the lower octave, the `Q`/number rows the upper octave) on the keyboard page
- set the spark on-times on the settings page: the on-time per frequency band (also by dragging the points of the
  chart), the volume, the min and max on-time, the velocity sensitivity and the max duty cycle. They're saved to
  `./coil-config.json` (see `--config`) and apply right away, also to a playing song. The controller needs the
  firmware of this version

## Firmware

```sh
pio run -t upload
```

Builds and flashes the firmware of the controller (`~/.platformio/penv/bin/pio` when `pio` isn't on the `PATH`).

## Tests

```sh
uv run pytest
```

Runs the tests that need no hardware: the protocol, the framing (against the C++ code of the firmware), the planning
of the voices, and the player against a simulated controller.

The hardware tests run on the controller itself, with the test firmware, which logs the sparks and measures the pulses
on the coil pin. Wire D10 to A0 and disconnect the coil (a speaker with a series resistor may stay on D10), then:

```sh
./test-on-board.sh [port] [pytest arguments]
```

It flashes the test firmware, runs all tests (the port defaults to `/dev/ttyACM0`, e.g. `./test-on-board.sh -k bend`
runs only the tests about pitch bend) and flashes the normal firmware again afterwards, also when a test fails.
