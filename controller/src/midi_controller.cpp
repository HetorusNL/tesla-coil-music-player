// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#include "midi_controller.h"

#include "packet_handler.h"
#include "utils.h"

using PACKET_HANDLE_RESULT = PHC::PACKET_HANDLE_RESULT;

constexpr float MidiController::midi_frequency[];

MidiController::MidiController(TimerManager& timer_manager) : timer_manager(timer_manager), midi_tones{0} {}

MidiController::~MidiController() {
    // nothing to do here
}

PACKET_HANDLE_RESULT MidiController::start_stream(uint8_t* data, uint8_t len) {
    // release the timers before starting the MIDI playback
    timer_manager.release_all_timers();
    return PACKET_HANDLE_RESULT::RESULT_OK;
}

PACKET_HANDLE_RESULT MidiController::handle_message(uint8_t* data, uint8_t len) {
    // TODO add queueing functionality here
    process_message(data);
    return PACKET_HANDLE_RESULT::RESULT_OK;
}

PACKET_HANDLE_RESULT MidiController::end_stream(uint8_t* data, uint8_t len) {
    // release the timers to make sure all sounds are stopped
    timer_manager.release_all_timers();
    return PACKET_HANDLE_RESULT::RESULT_OK;
}

PACKET_HANDLE_RESULT MidiController::perform_reset() {
    // release the timers to make sure all sounds are stopped
    timer_manager.release_all_timers();
    stop_all_notes();
    return PACKET_HANDLE_RESULT::RESULT_OK;
}

bool MidiController::process_message(uint8_t* msg) {
    debugprint((int)msg[0]);
    if ((msg[0] & 0xf0) == 0x90) {
        if (msg[2] == 0) {
            //   Serial.println("note on with velocity zero");
            note_off(msg);
        } else {
            //   Serial.println("note on");
            note_on(msg);
        }
    } else if ((msg[0] & 0xf0) == 0x80) {
        // Serial.println("note off");
        note_off(msg);
    } else if (msg[0] == 0xff) {
        // Serial.println("exit cmd");
        // the ending command, exit midi mode
        return false;
    } else {
        // Serial.println("unknown cmd");
    }
    return true;
}

void MidiController::note_on(uint8_t* msg) {
    // try to fetch a timer from TimerManager first
    CoilTimer* timer = timer_manager.get_timer();
    if (!timer) {
        return;
    }
    // see if there is a midi_tone available to use for this note
    for (int i = 0; i < TimerManager::NUM_TIMERS; i++) {
        if (!midi_tones[i].used) {
            midi_tones[i].used = true;
            midi_tones[i].channel = msg[0] & 0xf;
            midi_tones[i].note = msg[1];
            midi_tones[i].velocity = msg[2];
            midi_tones[i].delay = (uint16_t)msg[3] << 8 | (uint16_t)msg[4];
            debugprint("delay: ");
            debugprint(midi_tones[i].delay);
            midi_tones[i].coil_timer = timer;
            midi_tones[i].coil_timer->set_velocity(midi_tones[i].velocity);
            midi_tones[i].coil_timer->set_frequency(midi_frequency[midi_tones[i].note]);
            midi_tones[i].coil_timer->start();
            return;
        }
    }
}

void MidiController::note_off(uint8_t* msg) {
    // find the note that should be stopped
    for (int i = 0; i < TimerManager::NUM_TIMERS; i++) {
        if (midi_tones[i].used && midi_tones[i].channel == (msg[0] & 0xf) && midi_tones[i].note == msg[1]) {
            // found the node, now stop the timer and release the midi_tone
            debugprint("delay: ");
            debugprint(midi_tones[i].delay);
            midi_tones[i].coil_timer->stop();
            timer_manager.release_timer(midi_tones[i].coil_timer);
            midi_tones[i].used = false;
            return;
        }
    }
    Serial.println("note not found!");
    for (int i = 0; i < TimerManager::NUM_TIMERS; i++) {
        debugprint(i);
        debugprint(": ");
        debugprint(midi_tones[i].used);
        debugprint(", ");
        debugprint(midi_tones[i].channel);
        debugprint(", ");
        debugprint(midi_tones[i].note);
    }
    debugprint(midi_tones[4].used);
    debugprint(" ");
    debugprint(midi_tones[4].channel);
    debugprint(" == ");
    debugprint(msg[0] & 0xf);
    debugprint("; ");
    debugprint(midi_tones[4].note);
    debugprint(" == ");
    debugprint(msg[1]);
    debugprint("; ");
    debugprint(midi_tones[4].note == msg[1]);
    debugprint(" with cast: ");
    debugprint(midi_tones[4].note == (uint8_t)msg[1]);
    debugprint("; ");
    debugprint(midi_tones[4].channel == (msg[0] & 0xf));
}

void MidiController::stop_all_notes() {
    for (int i = 0; i < TimerManager::NUM_TIMERS; i++) {
        if (midi_tones[i].used) {
            midi_tones[i].coil_timer->stop();
            timer_manager.release_timer(midi_tones[i].coil_timer);
            midi_tones[i].used = false;
        }
    }
}
