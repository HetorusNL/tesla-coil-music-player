// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#include "spark_generator.h"

#include "sequencer.h"
#include "spark_test.h"

// the trigger input of TIM4 that is connected to the trigger output of TIM2 (RM0385 rev 9, table 154)
static constexpr uint32_t TIM4_TRIGGER_TIM2 = TIM_SMCR_TS_0;  // ITR1
// when nothing is due, the compare is armed this far ahead (about 10 s at 108 MHz), it then fires without a spark
static constexpr int32_t IDLE_TICKS = 0x40000000;
// the next spark is armed at least this far ahead (ns), so the compare can't be set to a tick that already passed
static constexpr uint32_t MIN_LEAD = 2000;
// an armed spark that starts sooner than this (ns) is left to the interrupt, which arms the spark after it
static constexpr uint32_t REARM_MARGIN = 5000;
// the duty-cycle budget holds the on-time of this long (ns) at the maximum duty cycle
static constexpr uint32_t DUTY_WINDOW = 10000000;
// the limits until the host sets them: max and min on-time (ns), max duty cycle (1/10000)
static constexpr uint32_t DEFAULT_MAX_ON_TIME = 100000;
static constexpr uint32_t DEFAULT_MIN_ON_TIME = 10000;
static constexpr uint16_t DEFAULT_MAX_DUTY = 1000;

Voice SparkGenerator::voices[NUM_VOICES] = {};
uint32_t SparkGenerator::_clock_hz = 0;
uint32_t SparkGenerator::min_off_ticks = 0;
uint32_t SparkGenerator::min_lead_ticks = 0;
uint32_t SparkGenerator::rearm_margin_ticks = 0;
uint32_t SparkGenerator::max_busy_ticks = 0;
Voice* SparkGenerator::armed_voice = nullptr;
uint64_t SparkGenerator::armed_spark = 0;
uint32_t SparkGenerator::armed_tick = 0;
uint32_t SparkGenerator::armed_on_ticks = 0;
uint32_t SparkGenerator::coil_free = 0;
uint32_t SparkGenerator::max_on_ticks = 0;
uint32_t SparkGenerator::min_on_ticks = 0;
uint32_t SparkGenerator::duty_q16 = 0;
uint64_t SparkGenerator::budget_cap_q16 = 0;
uint64_t SparkGenerator::budget_q16 = 0;
uint32_t SparkGenerator::budget_tick = 0;
uint32_t SparkGenerator::scale_q16 = 1 << 16;
bool SparkGenerator::limited = false;

extern "C" void TIM2_IRQHandler(void) {
#ifdef SPARK_TEST
    SparkTest::capture_isr();
#endif
    // keep the serial interrupt from delaying the arming of the compares
    __disable_irq();
    // the events first, so a voice that starts now gets its spark armed right away
    if (TIM2->SR & TIM_SR_CC2IF)
        Sequencer::isr();
    if (TIM2->SR & TIM_SR_CC1IF)
        SparkGenerator::isr();
    __enable_irq();
}

void SparkGenerator::begin() {
    // TIM2 and TIM4 are on APB1, their clock is twice the APB1 clock when APB1 is divided (108 MHz)
    _clock_hz = HAL_RCC_GetPCLK1Freq();
    if ((RCC->CFGR & RCC_CFGR_PPRE1) != RCC_CFGR_PPRE1_DIV1)
        _clock_hz *= 2;
    min_off_ticks = ns_to_ticks(MIN_OFF_TIME);
    min_lead_ticks = ns_to_ticks(MIN_LEAD);
    rearm_margin_ticks = ns_to_ticks(REARM_MARGIN);
    max_busy_ticks = ns_to_ticks(ABSOLUTE_MAX_ON_TIME + MIN_OFF_TIME + 1000);

    __HAL_RCC_TIM2_CLK_ENABLE();
    __HAL_RCC_TIM4_CLK_ENABLE();
    __HAL_RCC_GPIOD_CLK_ENABLE();

    // TIM4 makes the pulse: PWM mode 2 is active from CCR3 up to and including ARR, after which one-pulse mode stops
    // the counter at 0. ARR and CCR3 are preloaded, so arming the next spark doesn't change the running pulse.
    TIM4->CR1 = TIM_CR1_OPM | TIM_CR1_ARPE;
    TIM4->PSC = 0;
    TIM4->CCMR2 = TIM_CCMR2_OC3M_2 | TIM_CCMR2_OC3M_1 | TIM_CCMR2_OC3M_0 | TIM_CCMR2_OC3PE;
    TIM4->CCER = TIM_CCER_CC3E;
    // no pulse: CCR3 beyond ARR keeps the output inactive
    TIM4->ARR = 1;
    TIM4->CCR3 = 2;
    TIM4->EGR = TIM_EGR_UG;
    // combined reset + trigger mode: the trigger of TIM2 loads the preloaded pulse and starts the counter
    // the trigger input may only be selected while the slave mode is disabled
    TIM4->SMCR = TIM4_TRIGGER_TIM2;
    TIM4->SMCR |= TIM_SMCR_SMS_3;
    TIM4->SR = 0;

    // only connect the pin once TIM4 keeps it low
    GPIO_InitTypeDef gpio = {};
    gpio.Pin = GPIO_PIN_14;
    gpio.Mode = GPIO_MODE_AF_PP;
    gpio.Pull = GPIO_NOPULL;
    gpio.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    gpio.Alternate = GPIO_AF2_TIM4;
    HAL_GPIO_Init(GPIOD, &gpio);

    // TIM2 is the clock of the sparks: it counts through the full 32 bits, the compare of channel 1 sends a trigger
    // to TIM4 (compare pulse) and raises the interrupt that arms the next spark. Channel 2 is the compare of the
    // event queue of the Sequencer.
    TIM2->CR1 = 0;
    TIM2->PSC = 0;
    TIM2->ARR = 0xFFFFFFFF;
    TIM2->CR2 = TIM_CR2_MMS_1 | TIM_CR2_MMS_0;
    TIM2->CCMR1 = 0;
    TIM2->EGR = TIM_EGR_UG;
    TIM2->SR = 0;
    TIM2->DIER = TIM_DIER_CC1IE;
    // lower priority than the serial connection, the sparks don't depend on the interrupt latency
    NVIC_SetPriority(TIM2_IRQn, 5);
    NVIC_EnableIRQ(TIM2_IRQn);
#ifdef SPARK_TEST
    SparkTest::begin();
#endif

    __disable_irq();
    TIM2->CR1 = TIM_CR1_CEN;
    coil_free = now();
    budget_tick = now();
    set_limits(DEFAULT_MAX_ON_TIME, DEFAULT_MIN_ON_TIME, DEFAULT_MAX_DUTY);
    // start with a full budget
    budget_q16 = budget_cap_q16;
    arm_next();
    __enable_irq();
}

void SparkGenerator::set_limits(uint32_t max_on_time, uint32_t min_on_time, uint16_t max_duty) {
    uint32_t now = SparkGenerator::now();
    // keep the budget built up so far, but not beyond what the new limit holds
    budget_q16 = budget_at(now);
    budget_tick = now;
    max_on_ticks = ns_to_ticks(min(max_on_time, ABSOLUTE_MAX_ON_TIME));
    min_on_ticks = ns_to_ticks(min_on_time);
    duty_q16 = (uint32_t)min(max_duty, ABSOLUTE_MAX_DUTY) * 65536 / 10000;
    budget_cap_q16 = (uint64_t)ns_to_ticks(DUTY_WINDOW) * duty_q16;
    if (budget_q16 > budget_cap_q16)
        budget_q16 = budget_cap_q16;
    reschedule();
}

uint64_t SparkGenerator::frequency_to_period(uint32_t frequency) {
    // the period in ticks with 32 fractional bits, so the frequency is exact and the sparks don't drift
    uint64_t ticks_per_khz = (uint64_t)_clock_hz * 1000;
    uint64_t whole = ticks_per_khz / frequency;
    uint64_t remainder = ticks_per_khz % frequency;
    return (whole << 32) | ((remainder << 32) / frequency);
}

void SparkGenerator::start_voice(uint8_t voice, uint32_t tick, uint32_t frequency, uint32_t on_time) {
    Voice& v = voices[voice];
    v.period = frequency_to_period(frequency);
    v.on_ticks = ns_to_ticks(on_time);
    v.next_spark = (uint64_t)tick << 32;
    v.change_pending = false;
    v.stop_pending = false;
    v.active = true;
}

void SparkGenerator::change_voice(uint8_t voice, uint32_t tick, uint32_t frequency, uint32_t on_time) {
    Voice& v = voices[voice];
    if (!v.active) {
        start_voice(voice, tick, frequency, on_time);
        return;
    }
    v.change_period = frequency_to_period(frequency);
    v.change_on_ticks = ns_to_ticks(on_time);
    v.change_tick = tick;
    v.change_pending = true;
}

void SparkGenerator::stop_voice(uint8_t voice, uint32_t tick) {
    Voice& v = voices[voice];
    v.stop_tick = tick;
    v.stop_pending = v.active;
}

void SparkGenerator::stop_all_voices() {
    for (Voice& v : voices) {
        v.active = false;
        v.change_pending = false;
        v.stop_pending = false;
    }
}

uint32_t SparkGenerator::active_voices() {
    uint32_t bits = 0;
    for (int i = 0; i < NUM_VOICES; i++)
        bits |= (uint32_t)voices[i].active << i;
    return bits;
}

bool SparkGenerator::take_limited() {
    bool was_limited = limited || scale_q16 < (1 << 16);
    limited = false;
    return was_limited;
}

void SparkGenerator::reschedule() {
    update_scale();
    // an armed spark that is about to start can't be moved safely anymore, the interrupt arms the next spark then
    if ((int32_t)(armed_tick - now()) > (int32_t)rearm_margin_ticks)
        arm_next();
}

void SparkGenerator::update_scale() {
    // the share of the time the active voices want the coil on
    float demand = 0.0f;
    for (const Voice& v : voices) {
        if (v.active && v.period)
            demand += min(v.on_ticks, max_on_ticks) / (v.period / 4294967296.0f);
    }
    float max_duty = duty_q16 / 65536.0f;
    scale_q16 = demand > max_duty ? (uint32_t)(max_duty / demand * 65536.0f) : 1 << 16;
}

void SparkGenerator::isr() {
    TIM2->SR = ~TIM_SR_CC1IF;
    // the armed spark just started
    Voice* voice = armed_voice;
    if (voice && armed_on_ticks) {
        coil_free = armed_tick + 1 + armed_on_ticks + min_off_ticks;
        // the spark uses its on-time of the budget
        uint64_t budget = budget_at(armed_tick);
        uint64_t used = (uint64_t)armed_on_ticks << 16;
        budget_q16 = budget > used ? budget - used : 0;
        budget_tick = armed_tick;
#ifdef SPARK_TEST
        SparkTest::log_spark(armed_tick, voice - voices, armed_on_ticks);
#endif
    }
    // the next spark of the voice, skipping the ones that it missed while the coil was busy, unless the voice changed
    // since it was armed
    if (voice && voice->active && voice->next_spark == armed_spark) {
        do {
            voice->next_spark += voice->period;
        } while ((int32_t)((uint32_t)(voice->next_spark >> 32) - armed_tick) <= 0);
    }
    arm_next();
}

void SparkGenerator::arm_next() {
    uint32_t now = SparkGenerator::now();
    // the budget refilled until now, so budget_tick stays recent while the coil is idle
    budget_q16 = budget_at(now);
    budget_tick = now;

    // the voice whose spark is due first, after applying the changes and stops that are due by then
    Voice* next = nullptr;
    int32_t delay = IDLE_TICKS;
    bool changed = false;
    while (true) {
        next = nullptr;
        delay = IDLE_TICKS;
        for (Voice& v : voices) {
            if (!v.active)
                continue;
            uint32_t due = v.next_spark >> 32;
            if (v.stop_pending && (int32_t)(due - v.stop_tick) >= 0) {
                v.active = false;
                v.stop_pending = false;
                v.change_pending = false;
                changed = true;
                continue;
            }
            if (v.change_pending && (int32_t)(due - v.change_tick) >= 0) {
                v.period = v.change_period;
                v.on_ticks = v.change_on_ticks;
                v.change_pending = false;
                changed = true;
            }
            if (v.on_ticks == 0)
                continue;
            int32_t voice_delay = (int32_t)(due - now);
            if (!next || voice_delay < delay) {
                next = &v;
                delay = voice_delay;
            }
        }
        // never before the coil is free, a coil_free further ahead than a spark can take is an old one that wrapped
        // around
        uint32_t busy = coil_free - now;
        if (next && busy <= max_busy_ticks && (int32_t)busy > delay)
            delay = busy;
        if (delay < (int32_t)min_lead_ticks)
            delay = min_lead_ticks;
        // a spark that waited for the coil until the voice stops is left out
        if (next && next->stop_pending && (int32_t)(now + delay - next->stop_tick) >= 0) {
            next->active = false;
            next->stop_pending = false;
            next->change_pending = false;
            changed = true;
            continue;
        }
        break;
    }
    if (changed)
        update_scale();

    armed_voice = next;
    armed_spark = next ? next->next_spark : 0;
    armed_tick = now + delay;
    uint32_t on_ticks = next ? min(next->on_ticks, max_on_ticks) : 0;
    // all sparks get shorter by the same factor when the voices together exceed the maximum duty cycle
    uint32_t scaled = (uint32_t)(((uint64_t)on_ticks * scale_q16) >> 16);
    on_ticks = scaled >= min_on_ticks ? scaled : min(on_ticks, min_on_ticks);
    // the spark gets at most the budget left when it starts, a spark shorter than the minimum on-time is skipped
    uint32_t available = budget_at(armed_tick) >> 16;
    if (on_ticks > available) {
        on_ticks = available >= min_on_ticks && available > 0 ? available : 0;
        limited = true;
    }
    armed_on_ticks = on_ticks;
    // working it out takes a few us with many voices, so a spark that is due by now moves to the first tick that can
    // still be armed, a compare in the past would only match after the 32-bit ticks wrapped around (40 s)
    uint32_t latest = SparkGenerator::now() + min_lead_ticks;
    if ((int32_t)(armed_tick - latest) < 0)
        armed_tick = latest;
    // preload the pulse, TIM4 loads it when the compare triggers it
    if (armed_on_ticks) {
        TIM4->CCR3 = 1;
        TIM4->ARR = armed_on_ticks;
    } else {
        TIM4->CCR3 = 2;
        TIM4->ARR = 1;
    }
    TIM2->CCR1 = armed_tick;
}
