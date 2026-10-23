// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#ifdef SPARK_TEST

#include "spark_test.h"

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

PHC::PACKET_HANDLE_RESULT SparkTest::handle(uint8_t* data, uint8_t len) {
    using PHR = PHC::PACKET_HANDLE_RESULT;
    if (len != 1)
        return PHR::RESULT_NOK;
    switch (data[0]) {
    case START:
        start();
        return PHR::RESULT_OK;
    case DUMP:
        dump();
        return PHR::RESULT_OK;
    default:
        return PHR::RESULT_NOK;
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

void SparkTest::dump() {
    __disable_irq();
    logging = false;
    TIM2->DIER &= ~TIM_DIER_CC4IE;
    uint32_t now = SparkGenerator::now();
    __enable_irq();

    // header: magic, clock (Hz), tick of the dump, number of sparks, number of pulses, missed pulses, dropped entries
    uint32_t header[] = {
        0x4B525053,  // "SPRK"
        SparkGenerator::clock_hz(),
        now,
        num_sparks,
        num_pulses,
        missed_pulses,
        dropped,
    };
    Serial.write((uint8_t*)header, sizeof(header));
    Serial.write((uint8_t*)sparks, num_sparks * sizeof(Spark));
    Serial.write((uint8_t*)pulses, num_pulses * sizeof(Pulse));
}

void SparkTest::log_spark(uint32_t tick, uint8_t timer_id, uint32_t on_ticks) {
    if (!logging)
        return;
    if (num_sparks >= MAX_ENTRIES) {
        dropped++;
        return;
    }
    sparks[num_sparks++] = {tick, (uint16_t)on_ticks, timer_id, 0};
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
