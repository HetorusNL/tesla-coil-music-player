// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#include <Arduino.h>

#include "protocol.h"
#include "sequencer.h"
#include "spark_generator.h"

void setup() {
    // the code runs from the flash on the AXI bus, which the ART accelerator doesn't speed up, the instruction cache
    // does (the core leaves it off)
    SCB_EnableICache();
    pinMode(LED_BUILTIN, OUTPUT);
    pinMode(LED_BLUE, OUTPUT);
    pinMode(LED_GREEN, OUTPUT);  // same as LED_BUILTIN
    pinMode(LED_RED, OUTPUT);
    // flash the LEDS to show we're starting
    digitalWrite(LED_RED, HIGH);
    delay(100);
    digitalWrite(LED_GREEN, HIGH);
    digitalWrite(LED_RED, LOW);
    delay(100);
    digitalWrite(LED_BLUE, HIGH);
    digitalWrite(LED_GREEN, LOW);
    delay(100);
    digitalWrite(LED_BLUE, LOW);

    SparkGenerator::begin();
    Sequencer::begin();
    Protocol::begin();
}

void loop() {
    Protocol::update();
    Sequencer::update();
    // the green LED shows whether any voice plays, unless the host turned it off
    static bool lit = false;
    if ((Protocol::green_led() && SparkGenerator::active_voices() != 0) != lit) {
        lit = !lit;
        digitalWrite(LED_BUILTIN, lit);
    }
}
