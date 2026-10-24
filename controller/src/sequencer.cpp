// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#include "sequencer.h"

#include "spark_generator.h"

// the events are handed to the SparkGenerator this long (us) before their time, which leaves time to work out a step
// that changes all voices at once, they still apply at their exact tick
static constexpr int32_t EVENT_LEAD = 100;
// immediate events start this long (us) after they arrive
static constexpr int32_t IMMEDIATE_LEAD = 20;
// the compare is armed at least this far ahead (us), otherwise the interrupt is triggered right away
static constexpr int32_t MIN_LEAD = 2;
// the clock starts running this long (us) after the run command, so the events at the start are applied in time
static constexpr uint32_t RUN_DELAY = 2000;
// the compare is armed at most this far ahead (us, 10 s), so the 32-bit ticks don't wrap around in between
static constexpr int32_t MAX_WAIT = 10000000;
// update() moves the origin of the clock forward once it's this many ticks (about 5 s) old
static constexpr int32_t REBASE_TICKS = 0x20000000;

Event Sequencer::queue[QUEUE_SIZE];
volatile uint16_t Sequencer::head = 0;
volatile uint16_t Sequencer::tail = 0;
volatile Sequencer::State Sequencer::_state = Sequencer::STOPPED;
uint32_t Sequencer::origin_tick = 0;
uint32_t Sequencer::origin_clock = 0;
uint32_t Sequencer::zero_tick = 0;
uint16_t Sequencer::late_events = 0;

void Sequencer::begin() {
    _state = STOPPED;
    origin_clock = 0;
}

void Sequencer::start(uint32_t clock) {
    __disable_irq();
    SparkGenerator::stop_all_voices();
    head = tail;
    origin_clock = clock;
    _state = READY;
    arm();
    SparkGenerator::reschedule();
    __enable_irq();
}

void Sequencer::run() {
    __disable_irq();
    origin_tick = SparkGenerator::now() + RUN_DELAY * SparkGenerator::ticks_per_us();
    zero_tick = origin_tick - origin_clock * SparkGenerator::ticks_per_us();
    _state = RUNNING;
    arm();
    __enable_irq();
}

void Sequencer::stop() {
    __disable_irq();
    SparkGenerator::stop_all_voices();
    origin_clock = clock();
    head = tail;
    _state = STOPPED;
    arm();
    SparkGenerator::reschedule();
    __enable_irq();
}

void Sequencer::flush() {
    __disable_irq();
    head = tail;
    arm();
    __enable_irq();
}

bool Sequencer::push(const Event* events, uint8_t count) {
    if (count > queue_free())
        return false;
    // only the main loop writes beyond the tail, the interrupt sees the events once the tail moves
    uint16_t end = tail;
    for (uint8_t i = 0; i < count; i++) {
        queue[end] = events[i];
        end = (end + 1) % QUEUE_SIZE;
    }
    __disable_irq();
    tail = end;
    arm();
    __enable_irq();
    return true;
}

void Sequencer::apply_now(const Event& event) {
    __disable_irq();
    apply(event, SparkGenerator::now() + IMMEDIATE_LEAD * SparkGenerator::ticks_per_us());
    SparkGenerator::reschedule();
    __enable_irq();
}

void Sequencer::update() {
    if (_state != RUNNING || (int32_t)(SparkGenerator::now() - origin_tick) < REBASE_TICKS)
        return;
    __disable_irq();
    // move the origin forward by whole us, so the clock stays the same
    uint32_t elapsed = (SparkGenerator::now() - origin_tick) / SparkGenerator::ticks_per_us();
    origin_clock += elapsed;
    origin_tick += elapsed * SparkGenerator::ticks_per_us();
    __enable_irq();
}

uint32_t Sequencer::clock() {
    if (_state != RUNNING)
        return origin_clock;
    int32_t elapsed = (int32_t)(SparkGenerator::now() - origin_tick);
    // the clock stands still until it starts running
    if (elapsed < 0)
        return origin_clock;
    return origin_clock + (uint32_t)elapsed / SparkGenerator::ticks_per_us();
}

uint16_t Sequencer::queue_free() { return QUEUE_SIZE - 1 - (uint16_t)((tail - head + QUEUE_SIZE) % QUEUE_SIZE); }

uint16_t Sequencer::take_late_events() {
    __disable_irq();
    uint16_t late = late_events;
    late_events = 0;
    __enable_irq();
    return late;
}

uint32_t Sequencer::tick_at_zero() { return zero_tick; }

uint32_t Sequencer::tick_of(uint32_t time) {
    // only valid within about 19 s of the origin, the ticks wrap around beyond it
    return origin_tick + (uint32_t)((int32_t)(time - origin_clock) * (int32_t)SparkGenerator::ticks_per_us());
}

void Sequencer::arm() {
    // called with the interrupts disabled
    if (_state != RUNNING || head == tail) {
        TIM2->DIER &= ~TIM_DIER_CC2IE;
        return;
    }
    uint32_t now = SparkGenerator::now();
    int32_t until = (int32_t)(queue[head].time - clock());
    int32_t delay;
    if (until > MAX_WAIT)
        delay = MAX_WAIT * SparkGenerator::ticks_per_us();
    else if (until < -MAX_WAIT)
        delay = 0;
    else
        delay = (int32_t)(tick_of(queue[head].time) - now) - EVENT_LEAD * SparkGenerator::ticks_per_us();
    TIM2->SR = ~TIM_SR_CC2IF;
    TIM2->DIER |= TIM_DIER_CC2IE;
    // a compare that can't be armed in time is triggered by software, the interrupt then handles the event
    uint32_t tick = now + delay;
    if ((int32_t)(tick - SparkGenerator::now()) < MIN_LEAD * (int32_t)SparkGenerator::ticks_per_us())
        TIM2->EGR = TIM_EGR_CC2G;
    else
        TIM2->CCR2 = tick;
}

void Sequencer::isr() {
    TIM2->SR = ~TIM_SR_CC2IF;
    if (_state == RUNNING) {
        uint32_t now = SparkGenerator::now();
        bool applied = false;
        // the events up to EVENT_LEAD after the next one are applied as well, they get their exact tick
        while (head != tail) {
            const Event& event = queue[head];
            int32_t until = (int32_t)(event.time - clock());
            if (until > 2 * EVENT_LEAD)
                break;
            uint32_t tick = until < -MAX_WAIT ? now : tick_of(event.time);
            if ((int32_t)(tick - now) < 0)
                late_events++;
            apply(event, tick);
            head = (head + 1) % QUEUE_SIZE;
            applied = true;
        }
        if (applied)
            SparkGenerator::reschedule();
    }
    arm();
}

void Sequencer::apply(const Event& event, uint32_t tick) {
    switch (event.kind) {
    case Event::VOICE_ON:
        SparkGenerator::start_voice(event.voice, tick, event.frequency, event.on_time);
        break;
    case Event::VOICE_SET:
        SparkGenerator::change_voice(event.voice, tick, event.frequency, event.on_time);
        break;
    case Event::VOICE_OFF:
        SparkGenerator::stop_voice(event.voice, tick);
        break;
    case Event::ALL_OFF:
        for (uint8_t voice = 0; voice < SparkGenerator::NUM_VOICES; voice++)
            SparkGenerator::stop_voice(voice, tick);
        break;
    }
}
