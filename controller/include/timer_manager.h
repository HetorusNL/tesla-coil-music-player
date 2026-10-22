// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#pragma once

#include "coil_timer.h"
#include "packet_handler_constants.h"

class TimerManager {
public:
    static constexpr int NUM_TIMERS = 9;

private:
    CoilTimer::Config timer_config[NUM_TIMERS] = {
        {TIM1, TIM1_UP_TIM10_IRQn},    //
        {TIM2, TIM8_UP_TIM13_IRQn},    //
        {TIM3, TIM3_IRQn},             //
        {TIM4, TIM4_IRQn},             //
        {TIM5, TIM6_DAC_IRQn},         //
        {TIM6, TIM7_IRQn},             //
        {TIM7, TIM2_IRQn},             //
        {TIM8, TIM5_IRQn},             //
        {TIM12, TIM8_BRK_TIM12_IRQn},  //
    };

public:
    TimerManager();
    ~TimerManager();

    CoilTimer* get_timer();
    void release_timer(CoilTimer* timer);
    void release_all_timers();
    PHC::PACKET_HANDLE_RESULT perform_reset();

private:
    inline static uint16_t get_num_active_timers() {
        uint16_t res = 0;
        for (int i = 0; i < NUM_TIMERS; i++) {
            res += timers[i]->is_active();
        }
        return res;
    }

protected:
    // give CoilTimer access to this class' privates
    friend class CoilTimer;

    static CoilTimer* timers[];
};
