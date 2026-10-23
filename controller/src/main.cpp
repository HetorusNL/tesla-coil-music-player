// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#include <Arduino.h>

#include "midi_controller.h"
#include "packet_handler.h"
#include "timer_manager.h"
#include "utils.h"

PacketHandler* packet_handler = nullptr;

void setup() {
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
    // ensure the I/O is setup before initializing the packet handler
    packet_handler = new PacketHandler();
}

void loop() {
    // let the packet handler handle all communication
    packet_handler->update();
}
