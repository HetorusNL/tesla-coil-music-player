// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#pragma once

#include <Arduino.h>

// an event of the protocol: a voice that starts, changes or stops at a time of the stream clock
struct Event {
    enum Kind : uint8_t {
        VOICE_ON = 0x01,   // frequency (mHz), on-time (ns)
        VOICE_SET = 0x02,  // frequency (mHz), on-time (ns), keeps the phase of the voice
        VOICE_OFF = 0x03,
        ALL_OFF = 0x04,  // the voice is ignored
    };

    uint32_t time;  // us of the stream clock
    uint8_t kind;
    uint8_t voice;
    uint32_t frequency;
    uint32_t on_time;
};

// The stream clock and the queue of events, which are applied to the voices of the SparkGenerator at their time.
//
// The stream clock counts in us from TIM2, so it never drifts against the sparks. The compare of channel 2 of TIM2
// fires EVENT_LEAD before the first queued event, its interrupt hands the events that are due to the SparkGenerator
// with their exact tick.
//
// The functions other than isr() are called from the main loop.
class Sequencer {
public:
    enum State : uint8_t {
        STOPPED = 0,  // the clock stands still and the queue doesn't take events
        READY = 1,    // the clock stands still at the start time and the queue takes events
        RUNNING = 2,  // the clock runs and the events are applied
    };
    static constexpr uint16_t QUEUE_SIZE = 4096;

    static void begin();
    // stops the voices, empties the queue and sets the clock to the time (us), ready to run
    static void start(uint32_t clock);
    static void run();
    // stops the voices, empties the queue and stops the clock
    static void stop();
    // empties the queue, the voices and the clock go on
    static void flush();
    // queues the events, false when they don't fit (nothing is queued then)
    static bool push(const Event* events, uint8_t count);
    // applies the event right away, ignoring its time
    static void apply_now(const Event& event);
    // keeps the clock valid while the 32-bit ticks wrap around, call it at least every 10 s
    static void update();

    static uint32_t clock();
    static State state() { return _state; }
    static uint16_t queue_free();
    // the events that were applied after their time since the previous call
    static uint16_t take_late_events();
    // the tick at which the stream clock was 0 when it last ran (for the spark test)
    static uint32_t tick_at_zero();

    // handles the compare interrupt of channel 2 of TIM2
    static void isr();

private:
    static void arm();
    static void apply(const Event& event, uint32_t tick);
    static uint32_t tick_of(uint32_t time);

private:
    static Event queue[QUEUE_SIZE];
    // the interrupt takes events at the head, the main loop adds them at the tail
    static volatile uint16_t head;
    static volatile uint16_t tail;
    static volatile State _state;
    // while running: the clock is origin_clock (us) at origin_tick, while not running it's origin_clock
    static uint32_t origin_tick;
    static uint32_t origin_clock;
    static uint32_t zero_tick;
    static uint16_t late_events;
};
