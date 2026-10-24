// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#ifdef SPARK_TEST

#include "spark_test.h"

#include "protocol.h"
#include "sequencer.h"
#include "spark_generator.h"

SparkTest::Spark SparkTest::sparks[MAX_ENTRIES];
SparkTest::Pulse SparkTest::pulses[MAX_ENTRIES];
volatile uint32_t SparkTest::num_sparks = 0;
volatile uint32_t SparkTest::num_pulses = 0;
volatile uint32_t SparkTest::missed_pulses = 0;
volatile uint32_t SparkTest::dropped = 0;
volatile bool SparkTest::logging = false;

void SparkTest::begin() {
    // A0 (PA3) is TIM2 channel 4, pulled down so a missing wire doesn't capture noise
    __HAL_RCC_GPIOA_CLK_ENABLE();
    GPIO_InitTypeDef gpio = {};
    gpio.Pin = GPIO_PIN_3;
    gpio.Mode = GPIO_MODE_AF_PP;
    gpio.Pull = GPIO_PULLDOWN;
    gpio.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    gpio.Alternate = GPIO_AF1_TIM2;
    HAL_GPIO_Init(GPIOA, &gpio);

    // both channels capture the input of channel 4: channel 3 the rising edge, channel 4 the falling edge
    TIM2->CCMR2 = TIM_CCMR2_CC3S_1 | TIM_CCMR2_CC4S_0;
    TIM2->CCER = TIM_CCER_CC3E | TIM_CCER_CC4E | TIM_CCER_CC4P;
}

uint8_t SparkTest::handle(const uint8_t* body, uint8_t len, uint8_t* reply, uint8_t& reply_len) {
    if (len < 1)
        return Protocol::BAD_LENGTH;
    switch (body[0]) {
    case START:
        start();
        return Protocol::OK;
    case STOP:
        {
            __disable_irq();
            logging = false;
            TIM2->DIER &= ~TIM_DIER_CC4IE;
            uint32_t values[] = {SparkGenerator::clock_hz(),
                                 Sequencer::tick_at_zero(),
                                 SparkGenerator::now(),
                                 num_sparks,
                                 num_pulses,
                                 missed_pulses,
                                 dropped};
            __enable_irq();
            memcpy(reply, values, sizeof(values));
            reply_len = sizeof(values);
            return Protocol::OK;
        }
    case READ:
        {
            if (len != 4)
                return Protocol::BAD_LENGTH;
            uint16_t first = body[2] | body[3] << 8;
            uint32_t total = body[1] == 0 ? num_sparks : num_pulses;
            const uint8_t* entries = body[1] == 0 ? (const uint8_t*)sparks : (const uint8_t*)pulses;
            if (first > total)
                return Protocol::OUT_OF_RANGE;
            uint32_t count = min(total - first, (uint32_t)READ_ENTRIES);
            memcpy(reply, entries + first * 8, count * 8);
            reply_len = count * 8;
            return Protocol::OK;
        }
    default:
        return Protocol::OUT_OF_RANGE;
    }
}

void SparkTest::start() {
    __disable_irq();
    // reading the captures clears their flags, so the first pulse doesn't count as missed
    (void)TIM2->CCR3;
    (void)TIM2->CCR4;
    TIM2->SR = ~(TIM_SR_CC3OF | TIM_SR_CC4OF);
    num_sparks = 0;
    num_pulses = 0;
    missed_pulses = 0;
    dropped = 0;
    logging = true;
    TIM2->DIER |= TIM_DIER_CC4IE;
    __enable_irq();
}

void SparkTest::log_spark(uint32_t tick, uint8_t voice, uint32_t on_ticks) {
    if (!logging)
        return;
    if (num_sparks >= MAX_ENTRIES) {
        dropped++;
        return;
    }
    sparks[num_sparks++] = {tick, (uint16_t)on_ticks, voice, 0};
}

void SparkTest::capture_isr() {
    uint32_t status = TIM2->SR;
    if (!(status & TIM_SR_CC4IF))
        return;
    // the falling edge ends the pulse, reading the captures clears their flags
    uint32_t rise = TIM2->CCR3;
    uint32_t fall = TIM2->CCR4;
    if (status & (TIM_SR_CC3OF | TIM_SR_CC4OF)) {
        TIM2->SR = ~(TIM_SR_CC3OF | TIM_SR_CC4OF);
        missed_pulses++;
    }
    if (!logging)
        return;
    if (num_pulses >= MAX_ENTRIES) {
        dropped++;
        return;
    }
    pulses[num_pulses++] = {rise, fall};
}

#endif
