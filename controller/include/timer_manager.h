// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#pragma once

#include "coil_timer.h"
#include "packet_handler_constants.h"

class TimerManager {
public:
    static constexpr int NUM_TIMERS = 9;

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
