// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#pragma once

#include <Arduino.h>

// The serial protocol with the host, version 2, see controller/PROTOCOL.md.
//
// Frames are COBS-encoded and end with a 0x00. A decoded frame is type, seq, body and a CRC-16/CCITT-FALSE. Every
// request gets one reply with the same type and seq: status, the stream clock, the free entries of the queue and a
// body that depends on the type.
class Protocol {
public:
    static constexpr uint8_t VERSION = 2;

    enum Type : uint8_t {
        HELLO = 0x01,
        PING = 0x02,
        SET_LIMITS = 0x03,
        EVENTS = 0x04,
        IMMEDIATE = 0x05,
        STREAM_START = 0x06,
        STREAM_RUN = 0x07,
        FLUSH = 0x08,
        STOP = 0x09,
        TEST = 0xF0,
    };

    enum Status : uint8_t {
        OK = 0x00,
        UNKNOWN_TYPE = 0x01,
        BAD_LENGTH = 0x02,
        OUT_OF_RANGE = 0x03,
        QUEUE_FULL = 0x04,
        WRONG_STATE = 0x05,
        OUT_OF_SEQUENCE = 0x06,
    };

    // the largest body of a request or a reply
    static constexpr uint8_t MAX_BODY = 240;
    // the reply header: status u8, clock u32, queue_free u16
    static constexpr uint8_t REPLY_HEADER = 7;

    static void begin();
    // reads and handles the received frames, and silences the coil when the host went quiet
    static void update();

private:
    static void handle_frame(uint8_t* frame, uint16_t len);
    static Status handle_request(uint8_t type, const uint8_t* body, uint8_t len, uint8_t* reply, uint8_t& reply_len);
    static Status handle_events(uint8_t type, const uint8_t* body, uint8_t len);
    static void send_reply(uint8_t type, uint8_t seq, Status status, const uint8_t* body, uint8_t len, bool keep);

private:
    // a frame of at most 2 + MAX_BODY + 2 bytes, COBS adds 1 byte per 254
    static constexpr uint16_t MAX_ENCODED = 256;
    // the replies kept for retransmissions
    static constexpr uint8_t HISTORY = 8;
    // the coil goes silent when no valid frame arrives for this long (ms)
    static constexpr uint32_t WATCHDOG_TIME = 1000;

    static uint8_t rx_buffer[MAX_ENCODED];
    static uint16_t rx_len;
    static bool rx_overflow;
    static bool connected;
    static uint8_t expected_seq;
    static uint8_t history[HISTORY][MAX_ENCODED + 1];
    static uint16_t history_len[HISTORY];
    static uint8_t history_seq[HISTORY];
    static uint32_t last_frame_ms;
    static bool watchdog_tripped;
    static uint16_t rejected_frames;
};
