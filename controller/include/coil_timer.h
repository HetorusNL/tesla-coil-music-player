// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#pragma once

#include <Arduino.h>

#include "packet_handler_constants.h"

#define COIL_PIN 10

// the settings of the sparks, the GUI sends them with the config message after connecting
struct SparkConfig {
    // the settings of the config message
    enum Setting : uint8_t {
        ON_TIME = 0x01,               // index: 100 Hz band, value: on-time (us) of the band
        MIN_ON_TIME = 0x02,           // value: shortest spark (us)
        MAX_ON_TIME = 0x03,           // value: longest spark (us)
        VOLUME = 0x04,                // value: factor of the on-time, in 1/10000
        VELOCITY_SENSITIVITY = 0x05,  // value: how much a lower velocity shortens the spark, in 1/10000
    };
    static constexpr uint16_t NUM_ON_TIMES = 20;
    // no setting can make a spark longer than this (us), to protect the coil
    static constexpr uint16_t ABSOLUTE_MAX_ON_TIME = 200;

    // on-time (us) per 100 Hz band at full volume, interpolated between the bands
    uint16_t on_times[NUM_ON_TIMES] = {80, 75, 70, 66, 62, 59, 57, 56, 55, 54,
                                       53, 52, 51, 50, 50, 50, 50, 50, 50, 50};
    uint16_t min_on_time = 10;
    uint16_t max_on_time = 100;
    // the factor of the on-times of the table, like a master fader
    float volume = 1.0f;
    float velocity_sensitivity = 0.0f;
};

class CoilTimer {
public:
    // hardware_timer related properties
    struct Config {
        TIM_TypeDef* type_def;
        IRQn_Type irq_type;
    };

public:
    CoilTimer(int id, Config* config);
    ~CoilTimer();

    CoilTimer* start();
    CoilTimer* stop();
    CoilTimer* set_frequency(double frequency);
    inline void set_velocity(uint8_t velocity) { _velocity = velocity; };
    void create_spark(uint16_t num_active_timers);
    // handles the config message: setting, index, value (2 bytes, big endian)
    static PHC::PACKET_HANDLE_RESULT configure(uint8_t* data, uint8_t len);

    inline double get_frequency() { return _frequency; }
    inline bool is_active() { return active; }
    bool request();
    void release();

private:
    void isr();
    inline float get_on_time() {
        const uint16_t* on_times = spark_config.on_times;
        uint16_t index = (uint16_t)_frequency / 100;
        // interpolating needs the next entry as well, so use the last entry beyond the end of the table
        if (index >= SparkConfig::NUM_ON_TIMES - 1)
            return on_times[SparkConfig::NUM_ON_TIMES - 1];
        float fraction = (((uint16_t)_frequency % 100) + 1) / 100.0f;
        return on_times[index] + ((int)on_times[index + 1] - (int)on_times[index]) * fraction;
    }
    bool is32bit() const {
        // STM32F7: TIM2 and TIM5 are 32-bit
        return (config->type_def == TIM2 || config->type_def == TIM5);
    }
    uint32_t get_arr_limit() const { return is32bit() ? 0xFFFFFFFF : 0xFFFF; }

private:
    HardwareTimer* hardware_timer;
    const int id;
    Config* config;  // pointer to config set in constructor, no deletion here
    double _frequency;
    uint8_t _velocity;
    bool active;
    bool spark_enabled;
    bool in_use;

    // shared by all timers
    static SparkConfig spark_config;
};
