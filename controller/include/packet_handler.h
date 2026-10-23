// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#pragma once

#include <Arduino.h>

#include "midi_controller.h"
#include "packet_handler_constants.h"
#include "timer_manager.h"

class PacketHandler {
public:
    PacketHandler();
    ~PacketHandler();

    void update();

private:
    void send_reply(uint8_t result, uint8_t sequence_num);
    void handle_packet();
    void handle_result(PHC::PACKET_HANDLE_RESULT result);
    PHC::PACKET_HANDLE_RESULT perform_reset();

private:
    // control values
    static constexpr uint8_t STX = 0x02;   // start of text
    static constexpr uint8_t ETX = 0x03;   // end of text
    static constexpr uint8_t ACK = 0x06;   // acknowledge
    static constexpr uint8_t NACK = 0x15;  // negative acknowledge
    // control byte indexes
    static constexpr uint8_t LOC_STX = 0x00;     // location of 'start of text'
    static constexpr uint8_t LOC_SEQ = 0x01;     // location of 'sequence number'
    static constexpr uint8_t LOC_TYPE = 0x02;    // location of 'message type'
    static constexpr uint8_t LOC_LEN = 0x03;     // location of 'message length'
    static constexpr uint8_t LOC_DATA = 0x04;    // location of 'message data'
    static constexpr uint8_t ETX_OFFSET = 0x04;  // location offset of ETX
    // message types
    static constexpr uint8_t TYPE_MIDI_START = 0x01;  // start of MIDI stream
    static constexpr uint8_t TYPE_MIDI_MSG = 0x02;    // MIDI message stream message
    static constexpr uint8_t TYPE_MIDI_END = 0x03;    // end of MIDI message stream
    static constexpr uint8_t TYPE_CONFIG = 0x04;      // change a setting of the sparks
    static constexpr uint8_t TYPE_PING = 0x05;        // only replies, to check the connection
    static constexpr uint8_t TYPE_TEST = 0x06;        // spark test commands, only in the nucleo_spark_test build
    static constexpr uint8_t TYPE_RESET = 0xff;       // perform reset sequence
    // constants
    static constexpr uint8_t MAX_MSG_SIZE = 0x10;

private:
    MidiController midi_controller;
    TimerManager timer_manager;

    uint8_t buffer[MAX_MSG_SIZE];
    uint8_t message_index;
    uint8_t sequence_number;
    bool packet_waiting;
};
