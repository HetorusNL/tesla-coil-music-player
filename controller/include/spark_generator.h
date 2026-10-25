// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#pragma once

#include <Arduino.h>

// the priority of the interrupt of TIM2, the serial interrupt has a higher one (1, a lower number)
static constexpr uint32_t TIMER_PRIORITY = 5;

// masks the interrupt of TIM2 (and the ones of a lower priority), but not the serial interrupt: the USART holds a
// single received byte, which is lost when the interrupt waits longer than the next byte takes (10.8 us)
inline void lock_timers() { __set_BASEPRI(TIMER_PRIORITY << (8 - __NVIC_PRIO_BITS)); }

inline void unlock_timers() { __set_BASEPRI(0); }

// a voice of the coil: sparks at its frequency with its on-time
struct Voice {
    bool active;
    // time between the sparks and the time of the next spark, in ticks with 32 fractional bits
    uint64_t period;
    uint64_t next_spark;
    // the length of the sparks (ticks) as requested, the generator limits it to the max on-time
    uint32_t on_ticks;
    // a new period and on-time, from the first spark at or after change_tick
    bool change_pending;
    uint32_t change_tick;
    uint64_t change_period;
    uint32_t change_on_ticks;
    // the voice stops before its first spark at or after stop_tick
    bool stop_pending;
    uint32_t stop_tick;
};

// Generates the sparks of the voices on the coil pin, PD14 (Arduino pin 10).
//
// TIM2 counts freely (32-bit) and its compare channel 1 marks the start of the next spark. The compare triggers TIM4
// in hardware, which makes the pulse on its channel 3 (PD14) in one-pulse mode. So the start and the length of a
// spark are exact to a timer tick, whatever the interrupt latency. The compare interrupt only arms the next spark.
//
// Only one spark is on the coil at a time: a spark that is due while the coil is busy waits until the coil has been
// off for at least the minimum off-time.
//
// When the active voices together want the coil on for more than the maximum duty cycle, the on-times of all their
// sparks are scaled down by the same factor (but not below the minimum on-time), so every note gets equally quieter.
// On top of that, the sparks share a budget of on-time that refills at the maximum duty cycle and holds up to
// DUTY_WINDOW of it: a spark gets at most the budget that is left, and is skipped when that's less than the minimum
// on-time.
//
// All functions that change the voices or the limits must be called with the timers locked (lock_timers()), or from
// the interrupt of TIM2.
class SparkGenerator {
public:
    static constexpr uint8_t NUM_VOICES = 16;
    // no setting can make a spark longer than this (ns), or let the coil be on more of the time (in 1/10000)
    static constexpr uint32_t ABSOLUTE_MAX_ON_TIME = 200000;
    static constexpr uint16_t ABSOLUTE_MAX_DUTY = 1500;
    // the coil is off at least this long (ns) after a spark, otherwise the sparks of the voices playing at once
    // follow each other so closely that they add up to a single long spark
    static constexpr uint32_t MIN_OFF_TIME = 10000;
    // the frequencies (mHz) a voice can play
    static constexpr uint32_t MIN_FREQUENCY = 1000;
    static constexpr uint32_t MAX_FREQUENCY = 20000000;

    // configures the timers and the coil pin
    static void begin();
    // the limits of the sparks, within the absolute limits: max and min on-time (ns), max duty cycle (1/10000)
    static void set_limits(uint32_t max_on_time, uint32_t min_on_time, uint16_t max_duty);

    // starts the voice with its first spark at the tick
    static void start_voice(uint8_t voice, uint32_t tick, uint32_t frequency, uint32_t on_time);
    // changes the frequency (mHz) and on-time (ns) of the voice from its first spark at or after the tick, keeping
    // its phase, a voice that doesn't play starts at the tick
    static void change_voice(uint8_t voice, uint32_t tick, uint32_t frequency, uint32_t on_time);
    // stops the voice before its first spark at or after the tick
    static void stop_voice(uint8_t voice, uint32_t tick);
    static void stop_all_voices();
    // the voices that play, bit n is voice n
    static uint32_t active_voices();
    // whether the sparks got shorter to stay within the max duty cycle since the previous call
    static bool take_limited();

    // arms the spark that is due first again, after the voices changed
    static void reschedule();
    // handles the compare interrupt of channel 1 of TIM2
    static void isr();

    static inline uint32_t now() { return TIM2->CNT; }
    // the first tick a new spark can be armed for without being delayed, leaving time for the arming itself
    static inline uint32_t earliest_spark() { return now() + rearm_margin_ticks; }
    static inline uint32_t clock_hz() { return _clock_hz; }
    static inline uint32_t ticks_per_us() { return _clock_hz / 1000000; }
    static inline uint32_t ns_to_ticks(uint32_t ns) {
        return (uint32_t)(((uint64_t)ns * _clock_hz + 500000000) / 1000000000);
    }

private:
    static void arm_next();
    static void update_scale();
    static uint64_t frequency_to_period(uint32_t frequency);
    // the budget (ticks of on-time, with 16 fractional bits) at the tick, which can't be before budget_tick
    static inline uint64_t budget_at(uint32_t tick) {
        uint64_t budget = budget_q16 + (uint64_t)(tick - budget_tick) * duty_q16;
        return budget < budget_cap_q16 ? budget : budget_cap_q16;
    }

private:
    static Voice voices[NUM_VOICES];
    static uint32_t _clock_hz;
    // constants in ticks, computed from the timer clock
    static uint32_t min_off_ticks;
    static uint32_t min_lead_ticks;
    static uint32_t rearm_margin_ticks;
    static uint32_t max_busy_ticks;

    // the spark the compare is armed for, armed_voice is nullptr when no spark is due
    static Voice* armed_voice;
    static uint64_t armed_spark;
    static uint32_t armed_tick;
    static uint32_t armed_on_ticks;
    // the tick from which the coil may spark again
    static uint32_t coil_free;

    // the limits in ticks
    static uint32_t max_on_ticks;
    static uint32_t min_on_ticks;
    // the duty cycle limit: the budget refills at duty_q16 (on-time per tick, with 16 fractional bits)
    static uint32_t duty_q16;
    static uint64_t budget_cap_q16;
    static uint64_t budget_q16;
    static uint32_t budget_tick;
    // the factor of the on-times of all sparks (with 16 fractional bits), to stay within the maximum duty cycle
    static uint32_t scale_q16;
    static bool limited;
};
