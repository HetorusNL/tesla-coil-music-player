#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

"""The serial protocol with the controller, version 2 (see controller/PROTOCOL.md)."""

import struct
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import IntEnum
from time import monotonic
from typing import Protocol

import serial

VERSION = 2
BAUDRATE = 921600
# requests that may wait for their reply at the same time
WINDOW = 4
# a request without a reply after this long (s) is sent again, together with the ones after it
REPLY_TIMEOUT = 0.2
# the connection counts as lost after this many retransmissions of the same request
MAX_RETRIES = 5
MAX_BODY = 240
EVENT_SIZE = 14
MAX_EVENTS = (MAX_BODY - 1) // EVENT_SIZE
REPLY_HEADER = struct.Struct("<BIH")


class Type(IntEnum):
    HELLO = 0x01
    PING = 0x02
    SET_LIMITS = 0x03
    EVENTS = 0x04
    IMMEDIATE = 0x05
    STREAM_START = 0x06
    STREAM_RUN = 0x07
    FLUSH = 0x08
    STOP = 0x09
    TEST = 0xF0


class Status(IntEnum):
    OK = 0x00
    UNKNOWN_TYPE = 0x01
    BAD_LENGTH = 0x02
    OUT_OF_RANGE = 0x03
    QUEUE_FULL = 0x04
    WRONG_STATE = 0x05
    OUT_OF_SEQUENCE = 0x06


class Kind(IntEnum):
    VOICE_ON = 0x01
    VOICE_SET = 0x02
    VOICE_OFF = 0x03
    ALL_OFF = 0x04


class StreamState(IntEnum):
    STOPPED = 0
    READY = 1
    RUNNING = 2


@dataclass(frozen=True)
class Event:
    time: int  # us of the stream clock
    kind: Kind
    voice: int = 0
    frequency: int = 0  # mHz
    on_time: int = 0  # ns

    def pack(self) -> bytes:
        return struct.pack("<IBBII", self.time, self.kind, self.voice, self.frequency, self.on_time)


def voice_on(time: int, voice: int, frequency: float, on_time: float) -> Event:
    """A voice that starts at the time (us), with the frequency (Hz) and on-time (us)."""
    return Event(time, Kind.VOICE_ON, voice, round(frequency * 1000), round(on_time * 1000))


def voice_set(time: int, voice: int, frequency: float, on_time: float) -> Event:
    return Event(time, Kind.VOICE_SET, voice, round(frequency * 1000), round(on_time * 1000))


def voice_off(time: int, voice: int) -> Event:
    return Event(time, Kind.VOICE_OFF, voice)


@dataclass(frozen=True)
class Reply:
    type: int
    seq: int
    status: Status
    clock: int  # us of the stream clock when the controller replied
    queue_free: int
    body: bytes


@dataclass(frozen=True)
class Hello:
    version: int
    num_voices: int
    queue_size: int
    clock_hz: int
    max_on_time: int  # ns
    max_duty: float
    min_off_time: int  # ns


@dataclass(frozen=True)
class PingStatus:
    state: StreamState
    active_voices: int  # bit n is voice n
    limited: bool  # the sparks got shorter to stay within the max duty cycle since the previous ping
    late_events: int
    rejected_frames: int


class Port(Protocol):
    """What the Link uses of a serial port, so the tests can put a simulated controller behind it."""

    @property
    def in_waiting(self) -> int: ...

    def write(self, data: bytes, /) -> int | None: ...

    def read(self, size: int, /) -> bytes: ...

    def reset_input_buffer(self) -> None: ...

    def close(self) -> None: ...


class ConnectionLost(Exception):
    """The controller stopped answering, or the serial port went away."""


class RequestFailed(Exception):
    def __init__(self, reply: Reply):
        super().__init__(f"request {reply.type:02x} failed: {reply.status.name}")
        self.reply = reply


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    """Encodes the data with COBS and adds the 0x00 delimiter."""
    out = bytearray([0])
    code_index = 0
    code = 1
    for byte in data:
        if byte == 0:
            out[code_index] = code
            code_index = len(out)
            out.append(0)
            code = 1
            continue
        out.append(byte)
        code += 1
        if code == 0xFF:
            out[code_index] = code
            code_index = len(out)
            out.append(0)
            code = 1
    out[code_index] = code
    out.append(0)
    return bytes(out)


def cobs_decode(data: bytes) -> bytes | None:
    """Decodes COBS data (without the delimiter), None when it isn't valid."""
    out = bytearray()
    index = 0
    while index < len(data):
        code = data[index]
        index += 1
        if code == 0 or index + code - 1 > len(data):
            return None
        out += data[index : index + code - 1]
        index += code - 1
        if code < 0xFF and index < len(data):
            out.append(0)
    return bytes(out)


def frame(type: int, seq: int, body: bytes) -> bytes:
    data = bytes([type, seq]) + body
    return cobs_encode(data + struct.pack("<H", crc16(data)))


class Pending:
    """A request waiting for its reply."""

    def __init__(self, seq: int, encoded: bytes):
        self.seq = seq
        self.encoded = encoded
        self.sent = monotonic()
        self.retries = 0
        self.reply: Reply | None = None
        self.done = threading.Event()


class Link:
    """Sends requests to the controller and matches their replies, with up to WINDOW requests in flight.

    The controller handles the requests in order of their sequence number. A request that isn't answered in time is
    sent again together with the ones after it, the controller answers a request it handled already with the same
    reply without handling it again.
    """

    def __init__(self, port: "str | Port", on_log: Callable[[int, str], None] | None = None):
        # called with the type and a description of every request and reply, for a debug log
        self._on_log = on_log
        # a port that's already open (or a fake one in the tests) is used as it is
        self.serial = serial.Serial(port, BAUDRATE, timeout=0.02) if isinstance(port, str) else port
        self._seq = 0
        self._in_flight: list[Pending] = []
        self._condition = threading.Condition()
        self._lost: Exception | None = None
        self._closing = False
        self._last_resend = 0.0
        # the clock and free queue of the latest reply, and when it arrived (monotonic)
        self.clock = 0
        self.clock_time = monotonic()
        self.queue_free = 0
        self.serial.reset_input_buffer()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @property
    def next_seq(self) -> int:
        """The sequence number of the next request."""
        return self._seq

    def close(self):
        self._closing = True
        self._reader.join()
        self.serial.close()

    def post(self, type: int, body: bytes = b"") -> Pending:
        """Sends the request, waits only while WINDOW requests are in flight already."""
        with self._condition:
            # counted from the oldest request without a reply, the controller only keeps the replies of the last 8
            while self._in_flight and (self._seq - self._in_flight[0].seq) % 256 >= WINDOW and self._lost is None:
                self._condition.wait(0.05)
            self._check_lost()
            pending = Pending(self._seq, frame(type, self._seq, body))
            self._seq = (self._seq + 1) % 256
            self._in_flight.append(pending)
            self._write(pending.encoded)
            if self._on_log is not None:
                name = Type(type).name if type in Type else f"{type:02x}"
                self._on_log(type, f"→ {name} seq {pending.seq}" + (f": {body.hex(' ')}" if body else ""))
            return pending

    def wait(self, pending: Pending, check: bool = True) -> Reply:
        """Waits for the reply, raises RequestFailed when check is set and its status isn't OK."""
        while not pending.done.wait(0.05):
            with self._condition:
                self._check_lost()
        with self._condition:
            self._check_lost()
        assert pending.reply is not None
        if check and pending.reply.status != Status.OK:
            raise RequestFailed(pending.reply)
        return pending.reply

    def request(self, type: int, body: bytes = b"", check: bool = True) -> Reply:
        return self.wait(self.post(type, body), check)

    def write_raw(self, data: bytes):
        """Writes bytes as they are, to test how the controller handles broken frames."""
        with self._condition:
            self._write(data)

    def _write(self, data: bytes):
        try:
            self.serial.write(data)
        except (serial.SerialException, OSError) as e:
            self._lost = e

    def _check_lost(self):
        if self._lost is not None:
            raise ConnectionLost(str(self._lost))

    def _read_loop(self):
        buffer = bytearray()
        while not self._closing:
            try:
                # what arrived so far, or the first byte that arrives within the timeout
                data = self.serial.read(max(1, self.serial.in_waiting))
            except (serial.SerialException, OSError, TypeError) as e:
                with self._condition:
                    self._lost = e
                    self._condition.notify_all()
                return
            for byte in data:
                if byte != 0:
                    buffer.append(byte)
                    continue
                self._handle_frame(bytes(buffer))
                buffer.clear()
            self._retransmit()

    def _handle_frame(self, encoded: bytes):
        decoded = cobs_decode(encoded)
        if decoded is None or len(decoded) < 2 + REPLY_HEADER.size + 2:
            return
        if crc16(decoded[:-2]) != struct.unpack("<H", decoded[-2:])[0]:
            return
        status, clock, queue_free = REPLY_HEADER.unpack_from(decoded, 2)
        reply = Reply(decoded[0], decoded[1], Status(status), clock, queue_free, decoded[2 + REPLY_HEADER.size : -2])
        with self._condition:
            pending = next((p for p in self._in_flight if p.seq == reply.seq), None)
            if pending is None:
                return
            if reply.status == Status.OUT_OF_SEQUENCE:
                # an earlier request got lost, send everything again from the one the controller expects, once for
                # all the requests after the lost one
                if monotonic() - self._last_resend > REPLY_TIMEOUT / 2:
                    self._resend_from(reply.body[0] if reply.body else pending.seq)
                return
            self.clock = reply.clock
            self.clock_time = monotonic()
            self.queue_free = reply.queue_free
            if self._on_log is not None:
                body = f": {reply.body.hex(' ')}" if reply.body else ""
                self._on_log(reply.type, f"← {reply.status.name} seq {reply.seq}, clock {reply.clock} us{body}")
            # the replies come in order, so the requests before this one were answered (or lost) already
            self._in_flight.remove(pending)
            pending.reply = reply
            pending.done.set()
            self._condition.notify_all()

    def _resend_from(self, seq: int):
        self._last_resend = monotonic()
        start = next((i for i, p in enumerate(self._in_flight) if p.seq == seq), 0)
        for pending in self._in_flight[start:]:
            pending.sent = monotonic()
            pending.retries += 1
            if pending.retries > MAX_RETRIES:
                self._lost = TimeoutError(f"no reply to request {pending.seq}")
                self._condition.notify_all()
                return
            self._write(pending.encoded)

    def _retransmit(self):
        with self._condition:
            if self._in_flight and monotonic() - self._in_flight[0].sent > REPLY_TIMEOUT:
                self._resend_from(self._in_flight[0].seq)


class Controller:
    """The requests of the protocol."""

    def __init__(self, port: "str | Port", on_log: Callable[[int, str], None] | None = None):
        self.link = Link(port, on_log)
        self.info: Hello | None = None

    def close(self):
        self.link.close()

    def hello(self) -> Hello:
        reply = self.link.request(Type.HELLO, bytes([VERSION]), check=False)
        values = struct.unpack_from("<BBHIIHI", reply.body)
        info = Hello(values[0], values[1], values[2], values[3], values[4], values[5] / 10000, values[6])
        if reply.status != Status.OK or info.version != VERSION:
            raise RequestFailed(reply)
        self.info = info
        return info

    def ping(self) -> PingStatus:
        body = self.link.request(Type.PING).body
        state, active, limited, late, rejected = struct.unpack("<BIBHH", body)
        return PingStatus(StreamState(state), active, bool(limited), late, rejected)

    def set_limits(self, max_on_time: float, min_on_time: float, max_duty: float):
        """The max and min on-time (us) and the max duty cycle (fraction) of the sparks."""
        body = struct.pack("<IIH", round(max_on_time * 1000), round(min_on_time * 1000), round(max_duty * 10000))
        self.link.request(Type.SET_LIMITS, body)

    def events(self, events: Iterable[Event], immediate: bool = False) -> list[Pending]:
        """Sends the events in frames of up to MAX_EVENTS, without waiting for the replies."""
        events = list(events)
        type = Type.IMMEDIATE if immediate else Type.EVENTS
        pendings: list[Pending] = []
        for start in range(0, len(events), MAX_EVENTS):
            chunk = events[start : start + MAX_EVENTS]
            body = bytes([len(chunk)]) + b"".join(event.pack() for event in chunk)
            pendings.append(self.link.post(type, body))
        return pendings

    def immediate(self, events: Iterable[Event]):
        for pending in self.events(events, immediate=True):
            self.link.wait(pending)

    def stream_start(self, clock: int):
        self.link.request(Type.STREAM_START, struct.pack("<I", clock))

    def stream_run(self):
        self.link.request(Type.STREAM_RUN)

    def flush(self):
        self.link.request(Type.FLUSH)

    def stop(self) -> Reply:
        return self.link.request(Type.STOP)
