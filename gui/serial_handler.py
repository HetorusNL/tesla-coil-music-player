#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

import serial


class SerialHandler:
    def __init__(self, port, baudrate=921600):
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

    def write(self, msg):
        self.port.write(msg)

    def write_msg(self, msg):
        self.port.write(bytearray([0x02, self.sequence_number, *msg, 0x03]))
        self.sequence_number = (self.sequence_number + 1) % 256

    def read(self, num_bytes=256):
        return self.port.read(num_bytes)

    def read_msg(self, num_bytes=256):
        msg = self.read(num_bytes)
        # TODO: temporary sequence number mismatch handling
        # change to retransmitting the broken message (with sequence number)
        result = True
        if len(msg) > 2:
            if msg[1] == 0x15:
                self.sequence_number = msg[2]
                result = False
            elif msg[1] == 0xFF:
                result = False
                print(f"debug print: " f'"{bytearray(msg)[3:].decode("utf-8", "ignore")}" ' f"{list(msg)}")
        return result, list(msg)
