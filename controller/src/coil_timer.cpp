// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#include "coil_timer.h"
#include "timer_manager.h"
#include <HardwareTimer.h>

CoilTimer::CoilTimer(int id, Config* config)
    : id(id), config(config), _frequency(1), _velocity(0), active(false), spark_enabled(true), in_use(false) {
    // create the hardware timer with the timer typedef
    hardware_timer = new HardwareTimer(config->type_def);

    // set the interrupt priority to lower than the serial connection
    NVIC_SetPriority(config->irq_type, 5);  // TIM1

    // configure the hardware_timer for a default frequency of 1 (Hz)
    set_frequency(_frequency);
}

CoilTimer::~CoilTimer() {
    // nothing to do here
}

CoilTimer* CoilTimer::start() {
    if (is_active())
        return this;
    active = true;
    hardware_timer->resume();
    return this;
}

CoilTimer* CoilTimer::stop() {
    if (!is_active())
        return this;
    active = false;
    hardware_timer->pause();
    return this;
}

CoilTimer* CoilTimer::set_frequency(double frequency) {
    uint32_t clk = hardware_timer->getTimerClkFreq();
    uint32_t max_arr = get_arr_limit();
    bool wide = is32bit();
    uint32_t best_psc = 0;
    uint32_t best_arr = 0;
    float best_err = 1.0f;
    const float max_rel_err = 0.001f;  // ±0.1%
    uint32_t max_psc = wide ? 0xFFFFFFFF : 0xFFFF;

    for (uint32_t psc = 0; psc <= max_psc; ++psc) {
        float arr_f = (clk / ((psc + 1.0f) * frequency)) - 1.0f;
        if (arr_f < 0.0f)
            break;
        uint32_t arr = (uint32_t)(arr_f + 0.5f);
        if (arr > max_arr)
            continue;

        float real_hz = clk / ((psc + 1.0f) * (arr + 1.0f));
        float err = fabsf((real_hz - frequency) / frequency);
        if (err < best_err) {
            best_err = err;
            best_psc = psc;
            best_arr = arr;
            if (err < max_rel_err)
                break;
        }
    }

    if (best_err > max_rel_err) {
        Serial.print("timer has too large error:");
        Serial.print(best_err);
        digitalWrite(LED_RED, true);
        // set frequency to 1 Hz
        best_psc = 0;    // 1 is added later, so no prescaler
        best_arr = clk;  // set the overflow value to the timer frequency
    }

    bool was_active = active;

    if (was_active)
        stop();

    hardware_timer->detachInterrupt();
    hardware_timer->setPrescaleFactor(best_psc + 1);
    hardware_timer->setOverflow(best_arr, TICK_FORMAT);
    hardware_timer->attachInterrupt(std::bind(&CoilTimer::isr, this));
    // disable spark generation when calling refresh, as the timer gets triggered
    spark_enabled = false;
    hardware_timer->refresh();

    if (was_active)
        start();

    _frequency = clk / ((best_psc + 1.0f) * (best_arr + 1.0f));
    return this;
}

void CoilTimer::create_spark(uint16_t num_active_timers) {
    // calculate the on duration here
    uint16_t on_time = get_on_time();
    uint16_t duration = on_time;
    // take the number of active timers into account
    double active_timer_factor = (0.5 + (double)num_active_timers / 2);
    // base the active hardware_timer volume adjustment on the frequency
    double vol_adjusted_factor = 1. + (active_timer_factor - 1);  // * (on_time[0]) / (on_time);
    vol_adjusted_factor = 1.5;
    duration = (uint16_t)((double)duration * VOLUME_MULTIPLIER / vol_adjusted_factor);
    // reduce duration by velocity if velocity is low
    duration *= (127 + _velocity / 2);
    duration >>= 8;
    // limit the min/max values of the duration
    if (duration < MIN_ON_TIME)
        duration = MIN_ON_TIME;
    if (duration > MAX_ON_TIME)
        duration = MAX_ON_TIME;
    // apply the spark for duration (us) ON and then duration (us) OFF
    digitalWrite(LED_BUILTIN, HIGH);
    digitalWrite(COIL_PIN, HIGH);
    delayMicroseconds(duration);
    digitalWrite(LED_BUILTIN, LOW);
    digitalWrite(COIL_PIN, LOW);
    delayMicroseconds(duration);
}

bool CoilTimer::request() {
    // if already in use, we can't request this timer
    if (in_use)
        return false;

    // otherwise set the timer to be in_use and return true
    in_use = true;
    return true;
}

void CoilTimer::release() {
    // stop the timer and set to not being in use
    stop();
    in_use = false;
}

void CoilTimer::isr() {
    // ISR of this hardware_timer, time to create a spark!
    if (spark_enabled)
        create_spark(TimerManager::get_num_active_timers());

    // enable spark generation if it was disabled previously
    spark_enabled = true;
}
