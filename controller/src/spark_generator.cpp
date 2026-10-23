// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#include "spark_generator.h"

#include "coil_timer.h"
#include "spark_test.h"

// the trigger input of TIM4 that is connected to the trigger output of TIM2 (RM0385 rev 9, table 154)
static constexpr uint32_t TIM4_TRIGGER_TIM2 = TIM_SMCR_TS_0;  // ITR1
// when nothing is due, the compare is armed this far ahead (about 10 s at 108 MHz), it then fires without a spark
static constexpr int32_t IDLE_TICKS = 0x40000000;
// the next spark is armed at least this far ahead (us), so the compare can't be set to a tick that already passed
static constexpr float MIN_LEAD = 2.0f;
// an armed spark that starts sooner than this (us) is left to the interrupt, which arms the spark after it
static constexpr float REARM_MARGIN = 5.0f;
// the duty-cycle budget holds the on-time of this long (us) at the maximum duty cycle
static constexpr float DUTY_WINDOW = 10000.0f;

CoilTimer** SparkGenerator::timers = nullptr;
int SparkGenerator::num_timers = 0;
uint32_t SparkGenerator::_clock_hz = 0;
uint32_t SparkGenerator::min_off_ticks = 0;
uint32_t SparkGenerator::min_lead_ticks = 0;
uint32_t SparkGenerator::rearm_margin_ticks = 0;
uint32_t SparkGenerator::max_busy_ticks = 0;
CoilTimer* SparkGenerator::armed_timer = nullptr;
uint64_t SparkGenerator::armed_spark = 0;
uint32_t SparkGenerator::armed_tick = 0;
uint32_t SparkGenerator::armed_on_ticks = 0;
uint32_t SparkGenerator::coil_free = 0;
uint32_t SparkGenerator::duty_q16 = 0;
uint64_t SparkGenerator::budget_cap_q16 = 0;
uint64_t SparkGenerator::budget_q16 = 0;
uint32_t SparkGenerator::budget_tick = 0;
uint32_t SparkGenerator::min_on_ticks = 0;
uint32_t SparkGenerator::scale_q16 = 1 << 16;

extern "C" void TIM2_IRQHandler(void) {
#ifdef SPARK_TEST
    SparkTest::capture_isr();
#endif
    if (TIM2->SR & TIM_SR_CC1IF)
        SparkGenerator::isr();
}

void SparkGenerator::begin(CoilTimer** timers, int num_timers) {
    SparkGenerator::timers = timers;
    SparkGenerator::num_timers = num_timers;

    // TIM2 and TIM4 are on APB1, their clock is twice the APB1 clock when APB1 is divided (108 MHz)
    _clock_hz = HAL_RCC_GetPCLK1Freq();
    if ((RCC->CFGR & RCC_CFGR_PPRE1) != RCC_CFGR_PPRE1_DIV1)
        _clock_hz *= 2;
    min_off_ticks = us_to_ticks(SparkConfig::MIN_OFF_TIME);
    min_lead_ticks = us_to_ticks(MIN_LEAD);
    rearm_margin_ticks = us_to_ticks(REARM_MARGIN);
    max_busy_ticks = us_to_ticks(SparkConfig::ABSOLUTE_MAX_ON_TIME + SparkConfig::MIN_OFF_TIME + 1);

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
    // to TIM4 (compare pulse) and raises the interrupt that arms the next spark
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
    update_limits();
    // start with a full budget
    budget_q16 = budget_cap_q16;
    budget_tick = now();
    arm_next();
    __enable_irq();
}

void SparkGenerator::reschedule() {
    update_scale();
    // an armed spark that is about to start can't be moved safely anymore, the interrupt arms the next spark then
    if ((int32_t)(armed_tick - now()) > (int32_t)rearm_margin_ticks)
        arm_next();
}

void SparkGenerator::update_limits() {
    const SparkConfig& config = CoilTimer::spark_config;
    uint32_t now = SparkGenerator::now();
    // keep the budget built up so far, but not beyond what the new limit holds
    budget_q16 = budget_at(now);
    budget_tick = now;
    uint16_t max_duty = min(config.max_duty, SparkConfig::ABSOLUTE_MAX_DUTY);
    duty_q16 = (uint32_t)max_duty * 65536 / 10000;
    budget_cap_q16 = (uint64_t)us_to_ticks(DUTY_WINDOW) * duty_q16;
    if (budget_q16 > budget_cap_q16)
        budget_q16 = budget_cap_q16;
    min_on_ticks = us_to_ticks(config.min_on_time);
}

void SparkGenerator::update_scale() {
    // the share of the time the active timers want the coil on
    float demand = 0.0f;
    for (int i = 0; i < num_timers; i++) {
        CoilTimer* timer = timers[i];
        if (timer->active && timer->period)
            demand += timer->on_ticks / (timer->period / 4294967296.0f);
    }
    float max_duty = duty_q16 / 65536.0f;
    scale_q16 = demand > max_duty ? (uint32_t)(max_duty / demand * 65536.0f) : 1 << 16;
}

void SparkGenerator::isr() {
    TIM2->SR = ~TIM_SR_CC1IF;
    // keep the serial interrupt from delaying the arming of the compare
    __disable_irq();
    // the armed spark just started
    CoilTimer* timer = armed_timer;
    if (timer && armed_on_ticks) {
        coil_free = armed_tick + 1 + armed_on_ticks + min_off_ticks;
        // the spark uses its on-time of the budget
        uint64_t budget = budget_at(armed_tick);
        uint64_t used = (uint64_t)armed_on_ticks << 16;
        budget_q16 = budget > used ? budget - used : 0;
        budget_tick = armed_tick;
#ifdef SPARK_TEST
        SparkTest::log_spark(armed_tick, timer->id, armed_on_ticks);
#endif
    }
    if (timer) {
        // the next spark of the timer, skipping the ones that it missed while the coil was busy, unless the timer
        // changed since it was armed
        if (timer->active && timer->next_spark == armed_spark) {
            do {
                timer->next_spark += timer->period;
            } while ((int32_t)((uint32_t)(timer->next_spark >> 32) - armed_tick) <= 0);
        }
    }
    arm_next();
    __enable_irq();
}

void SparkGenerator::arm_next() {
    uint32_t now = SparkGenerator::now();
    // the budget refilled until now, so budget_tick stays recent while the coil is idle
    budget_q16 = budget_at(now);
    budget_tick = now;

    // the timer whose spark is due first
    CoilTimer* next = nullptr;
    int32_t delay = IDLE_TICKS;
    for (int i = 0; i < num_timers; i++) {
        CoilTimer* timer = timers[i];
        if (!timer->active || timer->on_ticks == 0)
            continue;
        int32_t timer_delay = (int32_t)((uint32_t)(timer->next_spark >> 32) - now);
        if (!next || timer_delay < delay) {
            next = timer;
            delay = timer_delay;
        }
    }
    // never before the coil is free, a coil_free further ahead than a spark can take is an old one that wrapped around
    uint32_t busy = coil_free - now;
    if (next && busy <= max_busy_ticks && (int32_t)busy > delay)
        delay = busy;
    if (delay < (int32_t)min_lead_ticks)
        delay = min_lead_ticks;

    armed_timer = next;
    armed_spark = next ? next->next_spark : 0;
    armed_tick = now + delay;
    armed_on_ticks = next ? next->on_ticks : 0;
    // all sparks get shorter by the same factor when the timers together exceed the maximum duty cycle
    uint32_t scaled = (uint32_t)(((uint64_t)armed_on_ticks * scale_q16) >> 16);
    armed_on_ticks = scaled >= min_on_ticks ? scaled : min(armed_on_ticks, min_on_ticks);
    // the spark gets at most the budget left when it starts, a spark shorter than the minimum on-time is skipped
    uint32_t available = budget_at(armed_tick) >> 16;
    if (armed_on_ticks > available)
        armed_on_ticks = available >= min_on_ticks && available > 0 ? available : 0;
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
