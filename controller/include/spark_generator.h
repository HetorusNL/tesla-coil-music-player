// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#pragma once

#include <Arduino.h>

class CoilTimer;

// Generates the sparks of all active CoilTimers on the coil pin, PD14 (Arduino pin 10).
//
// TIM2 counts freely (32-bit) and its compare channel 1 marks the start of the next spark. The compare triggers TIM4
// in hardware, which makes the pulse on its channel 3 (PD14) in one-pulse mode. So the start and the length of a
// spark are exact to a timer tick, whatever the interrupt latency. The compare interrupt only arms the next spark.
//
// Only one spark is on the coil at a time: a spark that is due while the coil is busy waits until the coil has been
// off for at least the minimum off-time.
//
// When the active timers together want the coil on for more than the maximum duty cycle, the on-times of all their
// sparks are scaled down by the same factor (but not below the minimum on-time), so every note gets equally quieter.
// On top of that, the sparks share a budget of on-time that refills at the maximum duty cycle and holds up to
// DUTY_WINDOW of it: a spark gets at most the budget that is left, and is skipped when that's less than the minimum
// on-time.
class SparkGenerator {
public:
    // configures the timers and the coil pin, the timers stay owned by the caller
    static void begin(CoilTimer** timers, int num_timers);
    // arms the spark that is due first again, after a timer started, stopped or changed
    // must be called with the interrupts disabled
    static void reschedule();
    // takes over the maximum duty cycle and the minimum on-time of the spark config, reschedule() arms them
    // must be called with the interrupts disabled
    static void update_limits();
    // handles the compare interrupt of TIM2
    static void isr();

    static inline uint32_t now() { return TIM2->CNT; }
    // the first tick a new spark can be armed for without being delayed, leaving time for the arming itself
    static inline uint32_t earliest_spark() { return now() + rearm_margin_ticks; }
    static inline uint32_t clock_hz() { return _clock_hz; }
    static inline float ticks_per_us() { return _clock_hz / 1e6f; }

private:
    static void arm_next();
    static void update_scale();
    // the budget (ticks of on-time, with 16 fractional bits) at the tick, which can't be before budget_tick
    static inline uint64_t budget_at(uint32_t tick) {
        uint64_t budget = budget_q16 + (uint64_t)(tick - budget_tick) * duty_q16;
        return budget < budget_cap_q16 ? budget : budget_cap_q16;
    }
    static inline uint32_t us_to_ticks(float us) { return (uint32_t)(us * ticks_per_us() + 0.5f); }

private:
    static CoilTimer** timers;
    static int num_timers;
    static uint32_t _clock_hz;
    // constants in ticks, computed from the timer clock
    static uint32_t min_off_ticks;
    static uint32_t min_lead_ticks;
    static uint32_t rearm_margin_ticks;
    static uint32_t max_busy_ticks;

    // the spark the compare is armed for, armed_timer is nullptr when no spark is due
    static CoilTimer* armed_timer;
    static uint64_t armed_spark;
    static uint32_t armed_tick;
    static uint32_t armed_on_ticks;
    // the tick from which the coil may spark again
    static uint32_t coil_free;

    // the duty cycle limit: the budget refills at duty_q16 (on-time per tick, with 16 fractional bits)
    static uint32_t duty_q16;
    static uint64_t budget_cap_q16;
    static uint64_t budget_q16;
    static uint32_t budget_tick;
    static uint32_t min_on_ticks;
    // the factor of the on-times of all sparks (with 16 fractional bits), to stay within the maximum duty cycle
    static uint32_t scale_q16;
};
