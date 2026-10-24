// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#pragma once

// The CRC and COBS of the frames of the protocol. Only depends on stdint.h, so the tests can compile it on the host.

#include <stdint.h>

inline uint16_t crc16(const uint8_t* data, uint16_t len) {
    // CRC-16/CCITT-FALSE
    uint16_t crc = 0xFFFF;
    for (uint16_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (int bit = 0; bit < 8; bit++)
            crc = crc & 0x8000 ? (crc << 1) ^ 0x1021 : crc << 1;
    }
    return crc;
}

// decodes the COBS data in place, returns the decoded length or -1 when it isn't valid COBS
inline int cobs_decode(uint8_t* data, uint16_t len) {
    uint16_t read = 0;
    uint16_t write = 0;
    while (read < len) {
        uint8_t code = data[read++];
        if (code == 0 || read + code - 1 > len)
            return -1;
        for (uint8_t i = 1; i < code; i++)
            data[write++] = data[read++];
        if (code < 0xFF && read < len)
            data[write++] = 0;
    }
    return write;
}

// encodes the data with COBS and adds the 0x00 delimiter, returns the encoded length
inline uint16_t cobs_encode(const uint8_t* data, uint16_t len, uint8_t* out) {
    uint16_t write = 1;
    uint16_t code_index = 0;
    uint8_t code = 1;
    for (uint16_t i = 0; i < len; i++) {
        if (data[i] == 0) {
            out[code_index] = code;
            code_index = write++;
            code = 1;
            continue;
        }
        out[write++] = data[i];
        if (++code == 0xFF) {
            out[code_index] = code;
            code_index = write++;
            code = 1;
        }
    }
    out[code_index] = code;
    out[write++] = 0;
    return write;
}
