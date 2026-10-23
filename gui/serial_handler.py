#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

import serial


class SerialHandler:
    def __init__(self, port: str, baudrate: int = 921600):
        self.port = serial.Serial()
        self.port.port = port
        self.port.baudrate = baudrate
        self.port.timeout = 0
        self.sequence_number = 0

    def open(self):
        self.port.open()
        return self.port.is_open

    def close(self):
        self.port.close()
        return not self.port.is_open

    def write(self, msg: bytes | bytearray):
        self.port.write(msg)

    def write_msg(self, msg: list[int]):
        self.port.write(bytearray([0x02, self.sequence_number, *msg, 0x03]))
        self.sequence_number = (self.sequence_number + 1) % 256

    def read(self, num_bytes: int = 256) -> bytes:
        return self.port.read(num_bytes)

    def read_msg(self, num_bytes: int = 256) -> list[int]:
        # the replies are parsed by the caller, which knows the message it awaits a reply to
        return list(self.read(num_bytes))
