// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#include "timer_manager.h"

CoilTimer* TimerManager::timers[NUM_TIMERS] = {0};

TimerManager::TimerManager() {
    // give USART3 (Serial) the highest priority, the timers get priority 5
    NVIC_SetPriority(USART3_IRQn, 1);  // lower number = higher priority

    // create the timer objects
    for (int i = 0; i < NUM_TIMERS; i++) {
        timers[i] = new CoilTimer(i, &timer_config[i]);
    }
}

TimerManager::~TimerManager() {
    // remove the timer objects
    for (auto& timer : timers) {
        delete timer;
        timer = nullptr;
    }
}

// get the first available timer, returns nullptr if all timers are occupied
CoilTimer* TimerManager::get_timer() {
    for (auto& timer : timers)
        if (timer->request())
            return timer;

    // no timer is available, return nullptr
    return nullptr;
}

// release a timer for future use
void TimerManager::release_timer(CoilTimer* requested_timer) {
    for (auto& timer : timers)
        if (timer == requested_timer)
            timer->release();
}

// release all timers so they can be used for other purposes
void TimerManager::release_all_timers() {
    for (auto& timer : timers)
        timer->release();
}

PHC::PACKET_HANDLE_RESULT TimerManager::perform_reset() {
    // reset this manager by releasing all timers
    release_all_timers();
    return PHC::PACKET_HANDLE_RESULT::RESULT_OK;
}
