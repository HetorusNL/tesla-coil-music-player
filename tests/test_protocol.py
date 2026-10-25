#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

"""Tests of the protocol of the host, against the framing of the firmware and a fake controller."""

import random
import shutil
import struct
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from time import monotonic, sleep

import pytest

from gui.protocol import (
    EVENT_SIZE,
    MAX_BODY,
    MAX_EVENTS,
    ConnectionLost,
    Controller,
    Kind,
    Link,
    Status,
    Type,
    cobs_decode,
    cobs_encode,
    crc16,
    frame,
    voice_on,
)

FIRMWARE_INCLUDE = Path(__file__).parent.parent / "controller" / "include"


def random_data(rng: random.Random, size: int) -> bytes:
    # plenty of zeros and 0xFF, the special cases of COBS
    return bytes(rng.choice([0, 0, 0xFF, rng.randrange(256)]) for _ in range(size))


def test_crc16_check_value():
    assert crc16(b"123456789") == 0x29B1


@pytest.mark.parametrize("size", [0, 1, 253, 254, 255, 256, 300, 600])
def test_cobs_round_trip(size: int):
    rng = random.Random(size)
    for _ in range(20):
        data = random_data(rng, size)
        encoded = cobs_encode(data)
        assert encoded[-1] == 0 and 0 not in encoded[:-1]
        assert cobs_decode(encoded[:-1]) == data


def test_cobs_rejects_broken_data():
    assert cobs_decode(b"\x05\x01\x02") is None
    assert cobs_decode(b"\x00") is None


def test_event_layout():
    event = voice_on(123_456, 7, 440, 25.5)
    assert event.frequency == 440_000 and event.on_time == 25_500
    assert event.pack() == struct.pack("<IBBII", 123_456, Kind.VOICE_ON, 7, 440_000, 25_500)
    assert len(event.pack()) == EVENT_SIZE
    assert 1 + MAX_EVENTS * EVENT_SIZE <= MAX_BODY


@pytest.fixture(scope="module")
def firmware_framing(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """framing.h of the firmware, compiled into a program that encodes the hex lines of stdin."""
    compiler = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("needs a C++ compiler")
    directory = tmp_path_factory.mktemp("framing")
    source = directory / "framing.cpp"
    source.write_text("""
#include <cstdio>
#include <cstring>
#include "framing.h"
int main() {
    char line[4096];
    while (fgets(line, sizeof line, stdin)) {
        uint8_t data[1024];
        int len = 0;
        for (char* p = line; p[0] && p[1] && p[0] != '\\n'; p += 2) {
            unsigned value;
            sscanf(p, "%2x", &value);
            data[len++] = value;
        }
        uint8_t encoded[1100];
        uint16_t encoded_len = cobs_encode(data, len, encoded);
        for (int i = 0; i < encoded_len; i++)
            printf("%02x", encoded[i]);
        int decoded = cobs_decode(encoded, encoded_len - 1);
        printf(" %d %04x\\n", decoded == len && memcmp(encoded, data, len) == 0, crc16(data, len));
    }
}
""")
    program = directory / "framing"
    subprocess.run([compiler, "-O1", "-I", str(FIRMWARE_INCLUDE), "-o", str(program), str(source)], check=True)
    return program


def test_firmware_framing_matches(firmware_framing: Path):
    rng = random.Random(1)
    cases = [random_data(rng, size) for size in [1, 2, 253, 254, 255, 256, 300, 500] for _ in range(30)]
    result = subprocess.run(
        [str(firmware_framing)], input="".join(case.hex() + "\n" for case in cases), capture_output=True, text=True
    )
    for case, line in zip(cases, result.stdout.splitlines(), strict=True):
        encoded, decoded_ok, crc = line.split()
        assert bytes.fromhex(encoded) == cobs_encode(case)
        assert decoded_ok == "1"
        assert int(crc, 16) == crc16(case)


class FakeController:
    """A serial port with the request handling of the firmware behind it: in order of the sequence numbers, the
    replies of retransmissions replayed. It can drop requests and replies to test how the Link recovers."""

    def __init__(self, answer: bool = True):
        self.answer = answer
        self.expected: int | None = None
        self.history: dict[int, bytes] = {}
        self.handled: list[tuple[int, int]] = []
        self.drop_requests: set[int] = set()
        self.drop_replies: set[int] = set()
        self._received = bytearray()
        self._output = bytearray()
        self._lock = threading.Lock()

    def write(self, data: bytes) -> int:
        for byte in data:
            if byte != 0:
                self._received.append(byte)
                continue
            self._handle(bytes(self._received))
            self._received.clear()
        return len(data)

    def read(self, size: int) -> bytes:
        with self._lock:
            data = bytes(self._output[:size])
            del self._output[:size]
        if not data:
            sleep(0.005)
        return data

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._output)

    def reset_input_buffer(self):
        pass

    def close(self):
        pass

    def _handle(self, encoded: bytes):
        decoded = cobs_decode(encoded)
        assert decoded is not None and crc16(decoded[:-2]) == struct.unpack("<H", decoded[-2:])[0]
        type, seq = decoded[0], decoded[1]
        if not self.answer or seq in self.drop_requests:
            self.drop_requests.discard(seq)
            return
        if type == Type.HELLO:
            self.expected = seq
        if seq != self.expected:
            assert self.expected is not None
            if seq in self.history and 0 < (self.expected - seq) % 256 <= 8:
                self._send(self.history[seq])
            else:
                self._send(self._reply(type, seq, Status.OUT_OF_SEQUENCE, bytes([self.expected])))
            return
        self.handled.append((type, seq))
        reply = self._reply(type, seq, Status.OK, bytes([2, 16, 255, 15, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]))
        self.history[seq] = reply
        self.expected = (seq + 1) % 256
        if seq in self.drop_replies:
            self.drop_replies.discard(seq)
            return
        self._send(reply)

    @staticmethod
    def _reply(type: int, seq: int, status: Status, body: bytes) -> bytes:
        return frame(type, seq, struct.pack("<BIH", status, 0, 100) + body)

    def _send(self, data: bytes):
        with self._lock:
            self._output += data


@pytest.fixture
def fake() -> Iterator[FakeController]:
    yield FakeController()


def connect(fake: FakeController) -> Link:
    link = Link(fake)
    link.request(Type.HELLO, bytes([2]))
    return link


def assert_handled_once_in_order(fake: FakeController, count: int):
    seqs = [seq for _, seq in fake.handled]
    assert seqs == list(range(count))


def test_requests_in_flight(fake: FakeController):
    link = connect(fake)
    replies = [link.wait(pending) for pending in [link.post(Type.PING) for _ in range(30)]]
    assert all(reply.status == Status.OK for reply in replies)
    assert_handled_once_in_order(fake, 31)
    link.close()


def test_lost_request_is_sent_again(fake: FakeController):
    link = connect(fake)
    fake.drop_requests = {3, 10}
    replies = [link.wait(pending) for pending in [link.post(Type.PING) for _ in range(20)]]
    assert all(reply.status == Status.OK for reply in replies)
    assert_handled_once_in_order(fake, 21)
    link.close()


def test_lost_reply_is_replayed(fake: FakeController):
    link = connect(fake)
    fake.drop_replies = {2, 7}
    replies = [link.wait(pending) for pending in [link.post(Type.PING) for _ in range(20)]]
    assert all(reply.status == Status.OK for reply in replies)
    assert_handled_once_in_order(fake, 21)
    link.close()


def test_events_are_split_into_frames(fake: FakeController):
    controller = Controller(fake)
    controller.link.request(Type.HELLO, bytes([2]))
    events = [voice_on(i, i % 16, 440, 20) for i in range(40)]
    for pending in controller.events(events):
        controller.link.wait(pending)
    assert [type for type, _ in fake.handled] == [Type.HELLO, Type.EVENTS, Type.EVENTS, Type.EVENTS]
    controller.close()


def test_silent_controller_is_a_lost_connection():
    link = Link(FakeController(answer=False))
    start = monotonic()
    with pytest.raises(ConnectionLost):
        link.request(Type.PING)
    assert monotonic() - start < 3
    link.close()
