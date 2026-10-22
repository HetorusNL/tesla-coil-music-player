// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#include "packet_handler.h"

#include "utils.h"

PacketHandler::PacketHandler()
    : timer_manager(), midi_controller(timer_manager), buffer{0}, message_index(0), sequence_number(0),
      packet_waiting(false) {
    // initialize communication and all submodules
    Serial.begin(921600);
    // clear the Serial buffer
    while (Serial.available())
        Serial.read();
}

PacketHandler::~PacketHandler() {
    // nothing to do here
}

void PacketHandler::update() {
    // if we're waiting for a packet to be handled, retry the packet until OK/NOK
    if (packet_waiting) {
        handle_packet();
        return;
    }
    // if we're not waiting for a packet to be handled, monitor serial connection
    if (!Serial.available())
        return;
    uint8_t c = Serial.read();
    switch (message_index) {
    case LOC_STX:
        // if we're pointing to the first buffer index, simply wait for STX
        if (c != STX)
            return;
        break;
    case LOC_SEQ:
        if (c != sequence_number) {
            // sequence number mismatch, send back NACK and expected sequence number
            send_reply(NACK, sequence_number);
            message_index = LOC_STX;
            digitalWrite(LED_RED, true);
            return;
        }
        break;
    case LOC_TYPE:
        break;  // nothing to do here
    case LOC_LEN:
        break;  // nothing to do here
    default:
        // if we're pointing to the ETX location, we can handle the message
        if (message_index == (ETX_OFFSET + buffer[LOC_LEN])) {
            if (c != ETX) {
                // invalid message received, as ETX is not in the correct location
                send_reply(NACK, sequence_number);
                message_index = LOC_STX;
                digitalWrite(LED_RED, true);
                return;
            }
            // add the ETX to the buffer and handle it
            buffer[message_index] = c;
            handle_packet();
            message_index = LOC_STX;
            return;
        }
        break;
    }
    buffer[message_index] = c;
    message_index++;
}

void PacketHandler::send_reply(uint8_t result, uint8_t sequence_num) {
    uint8_t reply[] = {STX, result, sequence_num, ETX};
    Serial.write(reply, sizeof(reply));
    if (result != ACK) {
        Serial.write(buffer, message_index + 1);
        uint8_t nullarr[] = {0x00};
        Serial.write(nullarr, sizeof(nullarr));
    }
}

void PacketHandler::handle_packet() {
    uint8_t* data = &buffer[LOC_DATA];
    uint8_t len = buffer[LOC_LEN];
    debugprint("handle message: ");
    debugprint(buffer[LOC_TYPE]);
    switch (buffer[LOC_TYPE]) {
    case TYPE_MIDI_START:
        handle_result(midi_controller.start_stream(data, len));
        break;
    case TYPE_MIDI_MSG:
        handle_result(midi_controller.handle_message(data, len));
        break;
    case TYPE_MIDI_END:
        handle_result(midi_controller.end_stream(data, len));
        break;
    case TYPE_RESET:
        handle_result(perform_reset());
        break;
    default:
        break;  // invalid packet received!
    }
}

void PacketHandler::handle_result(PHC::PACKET_HANDLE_RESULT result) {
    switch (result) {
    case PHC::PACKET_HANDLE_RESULT::RESULT_OK:
        send_reply(ACK, sequence_number);
        break;
    case PHC::PACKET_HANDLE_RESULT::RESULT_NOK:
        send_reply(NACK, sequence_number);
        break;
    case PHC::PACKET_HANDLE_RESULT::RESULT_WAIT:
        packet_waiting = true;
        return;
    }
    sequence_number++;
    packet_waiting = false;
    memset(buffer, 0, sizeof(buffer));
}

PHC::PACKET_HANDLE_RESULT PacketHandler::perform_reset() {
    using PHR = PHC::PACKET_HANDLE_RESULT;
    bool result = true;
    result &= midi_controller.perform_reset() == PHR::RESULT_OK;
    result &= timer_manager.perform_reset() == PHR::RESULT_OK;
    return result ? PHR::RESULT_OK : PHR::RESULT_NOK;
}
