// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#pragma once

#ifdef SPARK_TEST

#include <Arduino.h>

#include "packet_handler_constants.h"

// Test mode of the nucleo_spark_test build environment: logs the sparks the SparkGenerator arms, and captures the
// pulses on the coil pin with TIM2 channels 3 and 4, so both are in ticks of the same clock. Wire the coil pin D10
// (PD14) to A0 (PA3, TIM2 channel 4). controller/spark_test.py plays notes and checks the pulses against the log.
class SparkTest {
public:
    // the test message: 0x01 starts a new log, 0x02 stops logging and dumps the log before the ACK
    enum Command : uint8_t {
        START = 0x01,
        DUMP = 0x02,
    };

    static void begin();
    static PHC::PACKET_HANDLE_RESULT handle(uint8_t* data, uint8_t len);
    // called by the interrupt of the SparkGenerator when the armed spark starts
    static void log_spark(uint32_t tick, uint8_t timer_id, uint32_t on_ticks);
    // handles the capture interrupt of TIM2
    static void capture_isr();

private:
    static void start();
    static void dump();

private:
    static constexpr uint32_t MAX_ENTRIES = 8192;
    struct Spark {
        uint32_t tick;
        uint16_t on_ticks;
        uint8_t timer_id;
        uint8_t reserved;
    };
    struct Pulse {
        uint32_t rise;
        uint32_t fall;
    };

    static Spark sparks[MAX_ENTRIES];
    static Pulse pulses[MAX_ENTRIES];
    static volatile uint32_t num_sparks;
    static volatile uint32_t num_pulses;
    // pulses whose edges were overwritten before they were read, and entries that didn't fit in the log
    static volatile uint32_t missed_pulses;
    static volatile uint32_t dropped;
    static volatile bool logging;
};

#endif
