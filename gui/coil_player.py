#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

import threading
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from time import perf_counter, sleep

from gui.coil_model import CoilModel
from gui.config import SparkConfig, load_config, save_config
from gui.serial_handler import SerialHandler
from gui.song import PartKey, Song

# message types understood by the tesla coil controller
TYPE_MIDI_START = 0x01
TYPE_MIDI_MSG = 0x02
TYPE_MIDI_END = 0x03
TYPE_CONFIG = 0x04
TYPE_PING = 0x05
TYPE_RESET = 0xFF
TYPE_NAMES = {
    TYPE_MIDI_START: "midi start",
    TYPE_MIDI_MSG: "midi",
    TYPE_MIDI_END: "midi end",
    TYPE_CONFIG: "config",
    TYPE_PING: "ping",
    TYPE_RESET: "reset",
}
# the settings of the config message (SparkConfig::Setting of the firmware)
SETTING_ON_TIME = 0x01
SETTING_MIN_ON_TIME = 0x02
SETTING_MAX_ON_TIME = 0x03
SETTING_VOLUME = 0x04
SETTING_VELOCITY_SENSITIVITY = 0x05
SETTING_MAX_DUTY = 0x06
# the volume, velocity sensitivity and max duty cycle are sent in 1/10000
FRACTION_SCALE = 10000
# control values of the replies of the tesla coil controller
STX = 0x02
ETX = 0x03
ACK = 0x06
NACK = 0x15
DEBUG = 0xFF
# the kind of reply for a NACK of a message the controller dropped, as it expected another sequence number
OUT_OF_SYNC = -1

# MIDI channel used for notes played live on the keyboard
LIVE_CHANNEL = 0

# time between the attempts to reconnect after the connection with the controller was lost
RECONNECT_INTERVAL = 0.5
# seconds to wait for the reply to a message whose reply matters
REPLY_TIMEOUT = 0.1
# while connected and idle the controller is pinged, a few unanswered pings count as a lost connection
PING_INTERVAL = 1.0
MAX_MISSED_PINGS = 3
# stable names of the USB serial devices, the device itself can get another name when it reconnects
SERIAL_BY_ID = Path("/dev/serial/by-id")


# the number of log lines kept, the debug messages add two lines per note
MAX_LOG_LINES = 5000


@dataclass(frozen=True)
class LogLine:
    number: int
    debug: bool
    text: str


def hex_bytes(data: list[int]) -> str:
    return " ".join(f"{byte:02x}" for byte in data)


def stable_port_path(port_name: str) -> Path | None:
    """Returns the /dev/serial/by-id path of the serial port, if it has one."""
    try:
        return next(path for path in SERIAL_BY_ID.iterdir() if path.resolve() == Path(port_name).resolve())
    except OSError, StopIteration:
        return None


class ConnectionLost(Exception):
    """The serial connection with the controller was lost, e.g. when the coil reset its USB connection."""


class Delivery(Enum):
    """What the controller replied to a message."""

    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    NO_REPLY = "no reply"


class PlayerState(Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    RECONNECTING = "reconnecting"


class CoilPlayer:
    """Plays songs and live notes on the tesla coil controller, shared by all GUI clients."""

    def __init__(self, song_dir: Path, config_path: Path):
        # the folder new browser tabs start browsing in
        self.song_dir = song_dir
        # the settings of the sparks, sent to the controller after connecting
        self.config_path = config_path
        self.spark_config = load_config(config_path)
        self.config_version = 0
        # the value of every (setting, index) the controller has, so a change only sends what differs
        self._controller_settings: dict[tuple[int, int], int] = {}
        # loaded songs per path, with the modification time they were loaded at (None when not a valid MIDI file)
        self._song_cache: dict[Path, tuple[float, Song | None]] = {}
        self.songs_version = 0
        self.log_lines: deque[LogLine] = deque(maxlen=MAX_LOG_LINES)
        self._log_number = 0
        self._log_lock = threading.Lock()

        # muted parts per song, shared so every client (and the running playback) sees the same
        self.muted: dict[Path, set[PartKey]] = {}
        self.muted_version = 0

        self.port_name: str | None = None
        self.current_song: Song | None = None
        self.position = 0.0
        # a paused song resumes from its position, the controller is idle meanwhile
        self.paused = False
        self._pausing = False
        self._play_origin = 0.0
        self.notes_on = 0
        self.notes_off = 0

        self._sh: SerialHandler | None = None
        self._serial_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # reconnecting after a lost connection, by the playback thread (to resume the song) or in the background
        self._stable_port: Path | None = None
        self._reconnecting = False
        self._cancel_reconnect = threading.Event()
        self._reconnect_thread: threading.Thread | None = None
        self._partial_reply: list[int] = []
        self._echo_remaining = 0
        self._live_notes: set[int] = set()
        # what the controller is playing, for the meter
        self.coil_model = CoilModel()
        threading.Thread(target=self._watch_connection, daemon=True).start()

    @property
    def state(self) -> PlayerState:
        # a song keeps playing while the playback thread reconnects to resume it
        if self._thread is not None and self._thread.is_alive():
            return PlayerState.PLAYING
        if self._reconnecting:
            return PlayerState.RECONNECTING
        if self._sh is None:
            return PlayerState.DISCONNECTED
        if self.paused:
            return PlayerState.PAUSED
        return PlayerState.IDLE

    @property
    def reconnecting(self) -> bool:
        return self._reconnecting

    def log(self, line: str, debug: bool = False):
        with self._log_lock:
            self._log_number += 1
            self.log_lines.append(LogLine(self._log_number, debug, f"{datetime.now():%H:%M:%S.%f}"[:-3] + f" {line}"))

    def log_since(self, number: int) -> list[LogLine]:
        """Returns the log lines after the given log line number."""
        with self._log_lock:
            return [line for line in self.log_lines if line.number > number]

    def list_folder(self, folder: Path) -> tuple[list[Path], list[Song]]:
        """Returns the (non-hidden) subfolders and the songs in the folder."""
        folders: list[Path] = []
        songs: list[Song] = []
        try:
            paths = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError as e:
            self.log(f"can't open {folder}: {e}")
            return folders, songs
        for path in paths:
            if path.name.startswith("."):
                continue
            if path.is_dir():
                folders.append(path)
            elif path.suffix.lower() in [".mid", ".midi"] and path.is_file():
                song = self.load_song(path)
                if song is not None:
                    songs.append(song)
        return folders, songs

    def load_song(self, path: Path) -> Song | None:
        mtime = path.stat().st_mtime
        cached = self._song_cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        song: Song | None = None
        try:
            song = Song(path)
        except Exception as e:
            self.log(f'skipping "{path.name}": {e}')
        self._song_cache[path] = (mtime, song)
        return song

    def songs_changed(self):
        # let every browser tab reload its folder
        self.songs_version += 1

    def is_muted(self, song: Song, part: PartKey) -> bool:
        return part in self.muted.get(song.path, set())

    def set_muted(self, song: Song, parts: list[PartKey], muted: bool):
        song_muted = self.muted.setdefault(song.path, set())
        if muted:
            song_muted.update(parts)
        else:
            song_muted.difference_update(parts)
        self.muted_version += 1

    def connect(self, port_name: str):
        self.disconnect()
        self._cancel_reconnect.clear()
        self._stable_port = stable_port_path(port_name)
        self._open(port_name)

    def disconnect(self):
        # also cancels reconnecting, the playback thread checks it as well
        self._cancel_reconnect.set()
        self.stop()
        if self._reconnect_thread is not None:
            self._reconnect_thread.join()
            self._reconnect_thread = None
        if self.port_name is None:
            return
        with suppress(ConnectionLost):
            self.live_release_all()
            with self._serial():
                self._reset()
        self._close()
        self._live_notes.clear()
        self.log(f"disconnected from {self.port_name}")
        self.port_name = None

    def reset(self):
        self.stop()
        try:
            with self._serial():
                self.log("resetting tesla coil controller...")
                self._reset()
        except ConnectionLost:
            self._reconnect_in_background()
        self._live_notes.clear()

    def set_spark_config(self, config: SparkConfig) -> Delivery | None:
        """Saves the settings and sends them to the controller, returns its reply (None when not connected).

        Raises ValueError for invalid settings."""
        if errors := config.errors():
            raise ValueError(", ".join(errors))
        save_config(self.config_path, config)
        self.spark_config = config
        self.config_version += 1
        if self._sh is None:
            return None
        try:
            with self._serial():
                return self._send_config()
        except ConnectionLost:
            # the reconnect sends the settings as well
            self._reconnect_in_background()
            return None

    def play(self, song: Song, start: float = 0.0):
        if self._sh is None:
            reconnecting = "still reconnecting to" if self._reconnecting else "not connected to"
            raise RuntimeError(f"{reconnecting} the tesla coil controller")
        self.stop()
        self.live_release_all()
        self.current_song = song
        self.position = start
        self.notes_on = 0
        self.notes_off = 0
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._play_song, args=(song, start), daemon=True)
        self._thread.start()

    def stop(self):
        self.paused = False
        self._stop_thread()

    def pause(self):
        if self.state != PlayerState.PLAYING or self.current_song is None:
            return
        self._pausing = True
        self._stop_thread()
        self._pausing = False
        # resume from where the song was, also during a silence between the notes
        self.position = min(perf_counter() - self._play_origin, self.current_song.length)
        self.paused = True

    def pause_at(self, song: Song, position: float):
        """Holds the song paused at the position, playing resumes it from there."""
        if self._sh is None:
            raise RuntimeError("not connected to the tesla coil controller")
        self.stop()
        self.live_release_all()
        self.current_song = song
        self.position = min(max(position, 0.0), song.length)
        self.notes_on = 0
        self.notes_off = 0
        self.paused = True

    def seek_paused(self, position: float):
        """Moves the position a paused song resumes from."""
        if self.paused and self.current_song is not None:
            self.position = min(max(position, 0.0), self.current_song.length)

    def _stop_thread(self):
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join()
        self._thread = None

    def live_note_on(self, note: int, velocity: int = 100):
        if self.state not in [PlayerState.IDLE, PlayerState.PAUSED] or note in self._live_notes:
            return
        self._live_notes.add(note)
        try:
            with self._serial():
                self._send_midi([0x90 | LIVE_CHANNEL, note, velocity], 0)
        except ConnectionLost:
            self._live_notes.clear()
            self._reconnect_in_background()

    def live_note_off(self, note: int):
        if self._sh is None or note not in self._live_notes:
            return
        self._live_notes.discard(note)
        try:
            with self._serial():
                self._send_midi([0x80 | LIVE_CHANNEL, note, 0], 0)
        except ConnectionLost:
            self._live_notes.clear()
            self._reconnect_in_background()

    def live_release_all(self):
        for note in list(self._live_notes):
            self.live_note_off(note)

    def _open(self, port_name: str):
        # setup the serial connection with the tesla coil controller
        sh = SerialHandler(port_name)
        sh.open()
        self._sh = sh
        self._partial_reply = []
        self._echo_remaining = 0
        self.port_name = port_name
        self.log(f"connected to {port_name}")
        sleep(0.05)

        # force a reset of the tesla coil controller, the first one syncs the sequence number
        try:
            with self._serial_lock:
                for _ in range(2):
                    self._reset()
                # the controller might have restarted meanwhile, so send all settings
                self._controller_settings.clear()
                self._send_config()
        except OSError:
            self._close()
            raise

    def _close(self):
        if self._sh is not None:
            # closing a port that's gone fails as well
            with suppress(OSError):
                self._sh.close()
        self._sh = None
        self.coil_model.clear()

    @contextmanager
    def _serial(self) -> Generator[None]:
        """Holds the serial lock, and turns a serial error into closing the port and raising ConnectionLost."""
        with self._serial_lock:
            if self._sh is None:
                raise ConnectionLost("not connected")
            try:
                yield
            # pyserial's SerialException is an OSError as well, like the errno 5 when the USB device disappears
            except OSError as e:
                self._close()
                self.log(f"lost the connection with {self.port_name}: {e}")
                raise ConnectionLost(str(e)) from e

    def _reconnect(self, abort: threading.Event | None = None) -> bool:
        """Reconnects until it succeeds (returns True), disconnect() cancels it or the abort event is set."""
        self._reconnecting = True
        self.log(f"reconnecting to {self.port_name}...")
        try:
            while not self._cancel_reconnect.is_set() and not (abort is not None and abort.is_set()):
                # follow the stable name, as the device can come back under another name
                port = str(self._stable_port.resolve()) if self._stable_port is not None else self.port_name
                if port is not None and Path(port).exists():
                    try:
                        self._open(port)
                        return True
                    except OSError as e:
                        self.log(f"could not reconnect yet: {e}", debug=True)
                self._cancel_reconnect.wait(RECONNECT_INTERVAL)
            return False
        finally:
            self._reconnecting = False

    def _reconnect_in_background(self):
        if self.port_name is None or self._cancel_reconnect.is_set():
            return
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return
        # already reconnecting before the thread starts, so the state doesn't show disconnected meanwhile
        self._reconnecting = True
        self._reconnect_thread = threading.Thread(target=self._reconnect, daemon=True)
        self._reconnect_thread.start()

    def _reset(self):
        self.coil_model.clear()
        self._request([TYPE_RESET, 0x00])

    def _unconfirmed_settings(self) -> list[tuple[int, int, int]]:
        """The (setting, index, value) of the settings the controller didn't confirm yet."""
        config = self.spark_config
        settings = [(SETTING_ON_TIME, idx, on_time) for idx, on_time in enumerate(config.on_times)]
        settings += [
            (SETTING_MIN_ON_TIME, 0, config.min_on_time),
            (SETTING_MAX_ON_TIME, 0, config.max_on_time),
            (SETTING_VOLUME, 0, round(config.volume * FRACTION_SCALE)),
            (SETTING_VELOCITY_SENSITIVITY, 0, round(config.velocity_sensitivity * FRACTION_SCALE)),
            (SETTING_MAX_DUTY, 0, round(config.max_duty * FRACTION_SCALE)),
        ]
        return [(s, i, v) for s, i, v in settings if self._controller_settings.get((s, i)) != v]

    def _send_config(self) -> Delivery:
        """Sends the settings that differ from the controller's, returns the worst reply to them."""
        result = Delivery.ACKNOWLEDGED
        for setting, index, value in self._unconfirmed_settings():
            delivery = self._request([TYPE_CONFIG, 4, setting, index, value >> 8, value & 0xFF])
            if delivery == Delivery.ACKNOWLEDGED:
                self._controller_settings[(setting, index)] = value
                continue
            # not recorded, so the next change sends it again
            self.log(
                f"the controller {'rejected' if delivery == Delivery.REJECTED else 'did not confirm'} "
                f"setting {setting:02x} index {index}: {value}"
            )
            if delivery == Delivery.NO_REPLY:
                # the controller isn't answering, the next change or reconnect sends the remaining settings
                return delivery
            result = delivery
        return result

    def _request(self, msg: list[int], quiet: bool = False, retry: bool = True) -> Delivery:
        """Writes the message and waits for the reply to it."""
        assert self._sh is not None
        seq = self._sh.sequence_number
        self._write(msg, quiet)
        deadline = perf_counter() + REPLY_TIMEOUT
        while True:
            for reply, reply_seq in self._read(quiet):
                if reply == OUT_OF_SYNC and retry:
                    # the controller dropped the message, send it again with the sequence number it expects
                    return self._request(msg, quiet, retry=False)
                if reply_seq == seq and reply != OUT_OF_SYNC:
                    return Delivery.ACKNOWLEDGED if reply == ACK else Delivery.REJECTED
            if perf_counter() >= deadline:
                return Delivery.NO_REPLY
            sleep(0.001)

    def _watch_connection(self):
        # pings the controller while connected and idle, so a lost connection shows up without playing a song
        missed = 0
        while True:
            sleep(PING_INTERVAL)
            if self.state not in [PlayerState.IDLE, PlayerState.PAUSED]:
                missed = 0
                continue
            try:
                with self._serial():
                    delivery = self._request([TYPE_PING, 0x00], quiet=True)
                    # settings the controller didn't confirm (e.g. after a lost reply) are sent again
                    if delivery == Delivery.ACKNOWLEDGED and self._unconfirmed_settings():
                        self._send_config()
            except ConnectionLost:
                self._reconnect_in_background()
                missed = 0
                continue
            missed = 0 if delivery == Delivery.ACKNOWLEDGED else missed + 1
            if missed >= MAX_MISSED_PINGS:
                self.log(f"the controller did not reply to {missed} pings")
                with self._serial_lock:
                    self._close()
                self._reconnect_in_background()
                missed = 0

    def _write(self, msg: list[int], quiet: bool = False):
        assert self._sh is not None
        if not quiet:
            name = TYPE_NAMES.get(msg[0], f"type {msg[0]:02x}")
            data = f": {hex_bytes(msg[2:])}" if msg[2:] else ""
            self.log(f"→ {name} seq {self._sh.sequence_number}{data}", debug=True)
        self._sh.write_msg(msg)

    def _read(self, quiet: bool = False) -> list[tuple[int, int]]:
        """Reads and logs the replies of the controller, returns their (ACK or NACK, sequence number)."""
        assert self._sh is not None
        # a reply can be split over two reads, so the start of an incomplete one is kept for the next read
        msg = self._partial_reply + self._sh.read_msg()
        self._partial_reply = []
        # the rest of the echo of a NACK of the previous read
        skipped = msg[: self._echo_remaining]
        msg = msg[self._echo_remaining :]
        self._echo_remaining -= len(skipped)
        replies: list[tuple[int, int]] = []
        texts: list[str] = []
        debug = True
        while msg:
            if len(msg) < 4 and msg[0] == STX:
                self._partial_reply = msg
                break
            if len(msg) >= 4 and msg[0] == STX and msg[1] == ACK and msg[3] == ETX:
                # the regular ACKs are only interesting when debugging
                replies.append((ACK, msg[2]))
                texts.append(f"ACK seq {msg[2]}")
                msg = msg[4:]
                continue
            if len(msg) >= 4 and msg[0] == STX and msg[1] == NACK and msg[3] == ETX:
                # the NACK echoes what the controller received and a 0x00: the whole message (STX, seq, type,
                # length, data, ETX) when it rejected it, only the STX and a zeroed seq when it was out of sync
                if len(msg) < 7 or (msg[6] != 0 and len(msg) < 8):
                    self._partial_reply = msg
                    break
                seq, echo = msg[2], msg[4:]
                rejected = echo[1] == seq and echo[2] != 0
                echo_length = 6 + echo[3] if rejected else 3
                echo, msg = echo[:echo_length], echo[echo_length:]
                self._echo_remaining = echo_length - len(echo)
                if rejected:
                    replies.append((NACK, seq))
                    texts.append(f"NACK seq {seq}, rejected {hex_bytes(echo)}")
                else:
                    self._sh.sequence_number = seq
                    replies.append((OUT_OF_SYNC, seq))
                    texts.append(f"NACK, expected seq {seq}, received {hex_bytes(echo)}")
                debug = False
                continue
            if len(msg) >= 4 and msg[0] == STX and msg[1] == DEBUG:
                texts.append(f'controller debug: "{bytes(msg[4:]).decode("utf-8", "ignore")}"')
            else:
                texts.append(hex_bytes(msg))
            # the rest belongs to the debug message (it has no length), or isn't a reply at all
            break
        if texts and not (quiet and debug):
            self.log(f"← {', '.join(texts)}", debug=debug)
        return replies

    def _send_midi(self, midi_bytes: list[int], delay: float):
        # the delay (in ms) since the previous message is sent along with the MIDI message
        time = min(int(delay * 1000), 0xFFFF)
        midi_msg = [*midi_bytes, time // 256, time % 256]
        self._write([TYPE_MIDI_MSG, len(midi_msg), *midi_msg])
        self._read()
        self.coil_model.handle_midi(midi_bytes)

    def _play_song(self, song: Song, start: float):
        self.log(f'playing "{song.name}"' + (f" from {start:.1f}s" if start else ""))
        while True:
            try:
                terminated = self._play_song_from(song, start)
                break
            except ConnectionLost:
                # resume the song once reconnected, from the note that didn't make it
                if not self._reconnect(abort=self._stop_event):
                    # stopped while reconnecting, keep reconnecting unless it was disconnect() that stopped it
                    self._reconnect_in_background()
                    terminated = True
                    break
                start = self.position
                self.log(f'resuming "{song.name}" from {start:.1f}s')

        # log some statistics
        song_end = "paused" if self._pausing else "stopped playing" if terminated else "finished playing"
        self.log(f'{song_end} "{song.name}" (notes_on: {self.notes_on}, notes_off: {self.notes_off})')

    def _play_song_from(self, song: Song, start: float) -> bool:
        # enter midi mode
        with self._serial():
            self._write([TYPE_MIDI_START, 0x00])
            sleep(0.05)
            self._read()

        # play the whole song
        terminated = self._play_note_events(song, start)

        # exit midi mode
        with self._serial():
            self._write([TYPE_MIDI_END, 0x00])
            sleep(0.01)
            self._read()
        return terminated

    def _play_note_events(self, song: Song, start: float) -> bool:
        # notes that are currently on, so their note_off is sent even when the part is muted halfway
        sounding: set[tuple[int, int]] = set()
        try:
            return self._play_sounding_note_events(song, start, sounding)
        finally:
            # the controller doesn't release the notes at the end of the stream, so turn them off here
            with self._serial():
                for channel, note in sounding:
                    self._send_midi([0x80 | channel, note, 0], 0)

    def _wait_until(self, time: float) -> bool:
        """Waits until the perf_counter time, returns whether playback was stopped meanwhile."""
        delay = time - perf_counter()
        if delay > 0:
            return self._stop_event.wait(delay)
        return self._stop_event.is_set()

    def _play_sounding_note_events(self, song: Song, start: float, sounding: set[tuple[int, int]]) -> bool:
        # schedule the events against the start time, so the serial overhead doesn't accumulate
        start_time = perf_counter() - start
        self._play_origin = start_time
        previous_time = start
        for event in song.events:
            # when starting halfway, the notes before the start (and ones still held at the start) are skipped
            if event.time < start:
                continue
            # wait for muted notes too, so the song keeps playing when all parts are muted
            if self._wait_until(start_time + event.time):
                return True
            self.position = event.time
            note = (event.channel, event.note)
            if event.is_note_on:
                if self.is_muted(song, event.part) or note in sounding:
                    continue
                sounding.add(note)
                self.notes_on += 1
            else:
                if note not in sounding:
                    continue
                sounding.discard(note)
                self.notes_off += 1
            with self._serial():
                self._send_midi(event.midi_bytes, event.time - previous_time)
            previous_time = event.time
        # play the silence until the end of the song as well
        if self._wait_until(start_time + song.length):
            return True
        self.position = song.length
        return False
