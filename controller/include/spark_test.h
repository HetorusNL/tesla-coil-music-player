// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#pragma once

#ifdef SPARK_TEST

#include <Arduino.h>

// Test mode of the nucleo_spark_test build environment: logs the sparks the SparkGenerator arms, and captures the
// pulses on the coil pin with TIM2 channels 3 and 4, so both are in ticks of the same clock. Wire the coil pin D10
// (PD14) to A0 (PA3, TIM2 channel 4). controller/spark_test.py plays notes and checks the pulses against the log.
class SparkTest {
public:
    // the commands of the TEST request
    enum Command : uint8_t {
        START = 0x01,  // starts a new log
        STOP = 0x02,   // stops logging, replies clock_hz, tick at stream clock 0, now, the numbers of the log
        READ = 0x03,   // which (0 sparks, 1 pulses) u8, first entry u16: replies up to READ_ENTRIES entries
    };
    static constexpr uint8_t READ_ENTRIES = 29;

    static void begin();
    // handles the body of a TEST request, returns the status of the reply
    static uint8_t handle(const uint8_t* body, uint8_t len, uint8_t* reply, uint8_t& reply_len);
    // called by the interrupt of the SparkGenerator when the armed spark starts
    static void log_spark(uint32_t tick, uint8_t voice, uint32_t on_ticks);
    // handles the capture interrupt of TIM2
    static void capture_isr();

private:
    static void start();

private:
    static constexpr uint32_t MAX_ENTRIES = 8192;
    struct Spark {
        uint32_t tick;
        uint16_t on_ticks;
        uint8_t voice;
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
