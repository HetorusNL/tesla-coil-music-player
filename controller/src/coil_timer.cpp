// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#include "coil_timer.h"
#include "spark_generator.h"
#include "timer_manager.h"

SparkConfig CoilTimer::spark_config;

CoilTimer::CoilTimer(int id)
    : id(id), _frequency(0), _velocity(0), in_use(false), active(false), period(0), next_spark(0), on_ticks(0) {}

CoilTimer::~CoilTimer() {
    // nothing to do here
}

CoilTimer* CoilTimer::start() {
    // a timer without a frequency has no sparks to make
    if (is_active() || period == 0)
        return this;
    __disable_irq();
    next_spark = (uint64_t)SparkGenerator::earliest_spark() << 32;
    active = true;
    SparkGenerator::reschedule();
    __enable_irq();
    digitalWrite(LED_BUILTIN, HIGH);
    return this;
}

CoilTimer* CoilTimer::stop() {
    if (!is_active())
        return this;
    __disable_irq();
    active = false;
    SparkGenerator::reschedule();
    __enable_irq();
    // the LED shows whether any timer is sparking
    if (TimerManager::get_num_active_timers() == 0)
        digitalWrite(LED_BUILTIN, LOW);
    return this;
}

CoilTimer* CoilTimer::set_frequency(double frequency) {
    if (frequency <= 0)
        return this;
    // the period in ticks with 32 fractional bits, so the frequency is exact and the sparks don't drift
    uint64_t new_period = (uint64_t)(SparkGenerator::clock_hz() / frequency * 4294967296.0);
    _frequency = frequency;
    __disable_irq();
    period = new_period;
    __enable_irq();
    update_on_ticks();
    return this;
}

CoilTimer* CoilTimer::set_velocity(uint8_t velocity) {
    _velocity = velocity;
    update_on_ticks();
    return this;
}

void CoilTimer::update_on_ticks() {
    // the on-time of the frequency, scaled by the volume and (as much as configured) by the velocity of the note
    const SparkConfig& config = spark_config;
    float velocity_factor = 1.0f - config.velocity_sensitivity * (1.0f - _velocity / 127.0f);
    float on_time = get_on_time() * config.volume * velocity_factor;
    // limit the min/max values of the duration, and never beyond the absolute maximum
    uint16_t max_on_time = min(config.max_on_time, SparkConfig::ABSOLUTE_MAX_ON_TIME);
    on_time = constrain(on_time, (float)config.min_on_time, (float)max_on_time);
    // a zero on-time makes no sparks, the generator skips the timer then
    uint32_t ticks = (uint32_t)(on_time * SparkGenerator::ticks_per_us() + 0.5f);
    __disable_irq();
    on_ticks = ticks;
    if (active)
        SparkGenerator::reschedule();
    __enable_irq();
}

PHC::PACKET_HANDLE_RESULT CoilTimer::configure(uint8_t* data, uint8_t len) {
    using PHR = PHC::PACKET_HANDLE_RESULT;
    if (len != 4)
        return PHR::RESULT_NOK;
    uint8_t index = data[1];
    uint16_t value = (uint16_t)data[2] << 8 | data[3];
    SparkConfig& config = spark_config;
    switch (data[0]) {
    case SparkConfig::ON_TIME:
        if (index >= SparkConfig::NUM_ON_TIMES)
            return PHR::RESULT_NOK;
        config.on_times[index] = value;
        break;
    case SparkConfig::MIN_ON_TIME:
        if (value > SparkConfig::ABSOLUTE_MAX_ON_TIME)
            return PHR::RESULT_NOK;
        config.min_on_time = value;
        break;
    case SparkConfig::MAX_ON_TIME:
        if (value > SparkConfig::ABSOLUTE_MAX_ON_TIME)
            return PHR::RESULT_NOK;
        config.max_on_time = value;
        break;
    case SparkConfig::VOLUME:
        config.volume = value / 10000.0f;
        break;
    case SparkConfig::MAX_DUTY:
        if (value > SparkConfig::ABSOLUTE_MAX_DUTY)
            return PHR::RESULT_NOK;
        config.max_duty = value;
        break;
    case SparkConfig::VELOCITY_SENSITIVITY:
        if (value > 10000)
            return PHR::RESULT_NOK;
        config.velocity_sensitivity = value / 10000.0f;
        break;
    default:
        return PHR::RESULT_NOK;
    }
    for (int i = 0; i < TimerManager::NUM_TIMERS; i++)
        TimerManager::timers[i]->update_on_ticks();
    __disable_irq();
    SparkGenerator::update_limits();
    SparkGenerator::reschedule();
    __enable_irq();
    return PHR::RESULT_OK;
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
