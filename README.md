# Tesla Coil Music Player

Plays MIDI songs on a tesla coil. The `controller/` folder contains the PlatformIO firmware of the tesla coil
controller, the `gui/` folder contains a NiceGUI web interface that sends the songs to the controller over serial.

## Web interface

```sh
TESLA_COIL_PASSWORD=<password> uv run tesla-coil-gui
```

Then open `http://<this machine>:8080` from any device on the network. A password is required when listening on
the network, use `--host 127.0.0.1` to run it on this machine only without a password. See `--help` for the serial
port, web port and songs folder options. The song browser starts in `./songs` of the directory it's launched from.

Features:

- connect to the controller (auto-connects to `/dev/ttyACM0` on startup) and reset it
- browse folders for songs, play / stop / seek them on the coil or on the speaker of the browser, and upload new
  `.mid` files into the current folder
- toggle the parts of a song (tracks and the MIDI channels within them), also during playback
- preview a single part on the speaker of the device the browser runs on
- play notes live on the coil and/or the speaker with the on-screen piano or the laptop keyboard (like LMMS: the
  `Z`/`S` rows play the lower octave, the `Q`/number rows the upper octave)
