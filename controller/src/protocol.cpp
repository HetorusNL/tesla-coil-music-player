// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#include "protocol.h"

#include "framing.h"
#include "sequencer.h"
#include "spark_generator.h"
#include "spark_test.h"

// the size of an event in a frame: time u32, kind u8, voice u8, frequency u32, on-time u32
static constexpr uint8_t EVENT_SIZE = 14;

uint8_t Protocol::rx_buffer[MAX_ENCODED];
uint16_t Protocol::rx_len = 0;
bool Protocol::rx_overflow = false;
bool Protocol::connected = false;
uint8_t Protocol::expected_seq = 0;
uint8_t Protocol::history[HISTORY][MAX_ENCODED + 1];
uint16_t Protocol::history_len[HISTORY] = {};
uint8_t Protocol::history_seq[HISTORY] = {};
uint32_t Protocol::last_frame_ms = 0;
bool Protocol::watchdog_tripped = true;
uint16_t Protocol::rejected_frames = 0;

static inline uint16_t read_u16(const uint8_t* data) { return data[0] | (uint16_t)data[1] << 8; }

static inline uint32_t read_u32(const uint8_t* data) {
    return data[0] | (uint32_t)data[1] << 8 | (uint32_t)data[2] << 16 | (uint32_t)data[3] << 24;
}

static inline uint8_t* write_u16(uint8_t* data, uint16_t value) {
    data[0] = value;
    data[1] = value >> 8;
    return data + 2;
}

static inline uint8_t* write_u32(uint8_t* data, uint32_t value) {
    data[0] = value;
    data[1] = value >> 8;
    data[2] = value >> 16;
    data[3] = value >> 24;
    return data + 4;
}

void Protocol::begin() {
    Serial.begin(921600);
    // give USART3 (Serial) a higher priority than the timers (lower number = higher priority)
    NVIC_SetPriority(USART3_IRQn, 1);
    while (Serial.available())
        Serial.read();
    last_frame_ms = millis();
}

void Protocol::update() {
    while (Serial.available()) {
        uint8_t c = Serial.read();
        if (c != 0) {
            // a frame that doesn't fit is dropped up to the next delimiter
            if (rx_len < MAX_ENCODED)
                rx_buffer[rx_len++] = c;
            else
                rx_overflow = true;
            continue;
        }
        if (rx_overflow || rx_len == 0) {
            rejected_frames += rx_overflow;
        } else {
            handle_frame(rx_buffer, rx_len);
        }
        rx_len = 0;
        rx_overflow = false;
    }

    // silence the coil when the host stopped sending, e.g. when the GUI crashed or the USB cable came loose
    if (!watchdog_tripped && millis() - last_frame_ms > WATCHDOG_TIME) {
        watchdog_tripped = true;
        Sequencer::stop();
        digitalWrite(LED_RED, HIGH);
    }
}

void Protocol::handle_frame(uint8_t* frame, uint16_t len) {
    int decoded = cobs_decode(frame, len);
    if (decoded < 4 || decoded > 4 + MAX_BODY || crc16(frame, decoded - 2) != read_u16(frame + decoded - 2)) {
        rejected_frames++;
        digitalWrite(LED_RED, HIGH);
        return;
    }
    last_frame_ms = millis();
    watchdog_tripped = false;
    uint8_t type = frame[0];
    uint8_t seq = frame[1];
    const uint8_t* body = frame + 2;
    uint8_t body_len = decoded - 4;

    if (type == HELLO) {
        // a new connection starts the sequence numbers again
        connected = true;
        expected_seq = seq;
        for (uint8_t& history_entry : history_seq)
            history_entry = seq - 1;
        digitalWrite(LED_RED, LOW);
    } else if (!connected) {
        send_reply(type, seq, WRONG_STATE, nullptr, 0, false);
        return;
    }
    if (seq != expected_seq) {
        // a retransmission of a request that was handled already gets the same reply again
        uint8_t slot = seq % HISTORY;
        if ((uint8_t)(expected_seq - seq) <= HISTORY && history_seq[slot] == seq && history_len[slot]) {
            Serial.write(history[slot], history_len[slot]);
            return;
        }
        // a request after a lost one is dropped, the host sends again from the lost one
        uint8_t expected = expected_seq;
        send_reply(type, seq, OUT_OF_SEQUENCE, &expected, 1, false);
        return;
    }

    uint8_t reply[MAX_BODY];
    uint8_t reply_len = 0;
    Status status = handle_request(type, body, body_len, reply, reply_len);
    send_reply(type, seq, status, reply, reply_len, true);
    expected_seq++;
}

Protocol::Status Protocol::handle_request(uint8_t type, const uint8_t* body, uint8_t len, uint8_t* reply,
                                          uint8_t& reply_len) {
    uint8_t* out = reply;
    switch (type) {
    case HELLO:
        if (len != 1)
            return BAD_LENGTH;
        // start from silence, the host sends its limits next
        Sequencer::stop();
        *out++ = VERSION;
        *out++ = SparkGenerator::NUM_VOICES;
        out = write_u16(out, Sequencer::QUEUE_SIZE - 1);
        out = write_u32(out, SparkGenerator::clock_hz());
        out = write_u32(out, SparkGenerator::ABSOLUTE_MAX_ON_TIME);
        out = write_u16(out, SparkGenerator::ABSOLUTE_MAX_DUTY);
        out = write_u32(out, SparkGenerator::MIN_OFF_TIME);
        reply_len = out - reply;
        return body[0] == VERSION ? OK : OUT_OF_RANGE;
    case PING:
        if (len != 0)
            return BAD_LENGTH;
        *out++ = Sequencer::state();
        __disable_irq();
        out = write_u32(out, SparkGenerator::active_voices());
        *out = SparkGenerator::take_limited();
        __enable_irq();
        out++;
        out = write_u16(out, Sequencer::take_late_events());
        out = write_u16(out, rejected_frames);
        rejected_frames = 0;
        reply_len = out - reply;
        return OK;
    case SET_LIMITS:
        {
            if (len != 10)
                return BAD_LENGTH;
            uint32_t max_on_time = read_u32(body);
            uint32_t min_on_time = read_u32(body + 4);
            uint16_t max_duty = read_u16(body + 8);
            if (max_on_time > SparkGenerator::ABSOLUTE_MAX_ON_TIME || min_on_time > max_on_time ||
                max_duty > SparkGenerator::ABSOLUTE_MAX_DUTY)
                return OUT_OF_RANGE;
            __disable_irq();
            SparkGenerator::set_limits(max_on_time, min_on_time, max_duty);
            __enable_irq();
            return OK;
        }
    case EVENTS:
    case IMMEDIATE:
        return handle_events(type, body, len);
    case STREAM_START:
        if (len != 4)
            return BAD_LENGTH;
        Sequencer::start(read_u32(body));
        return OK;
    case STREAM_RUN:
        if (len != 0)
            return BAD_LENGTH;
        if (Sequencer::state() != Sequencer::READY)
            return WRONG_STATE;
        Sequencer::run();
        return OK;
    case FLUSH:
        if (len != 0)
            return BAD_LENGTH;
        Sequencer::flush();
        return OK;
    case STOP:
        if (len != 0)
            return BAD_LENGTH;
        Sequencer::stop();
        return OK;
#ifdef SPARK_TEST
    case TEST:
        return (Status)SparkTest::handle(body, len, reply, reply_len);
#endif
    default:
        return UNKNOWN_TYPE;
    }
}

Protocol::Status Protocol::handle_events(uint8_t type, const uint8_t* body, uint8_t len) {
    if (len < 1 || len != 1 + body[0] * EVENT_SIZE)
        return BAD_LENGTH;
    uint8_t count = body[0];
    Event events[MAX_BODY / EVENT_SIZE];
    for (uint8_t i = 0; i < count; i++) {
        const uint8_t* data = body + 1 + i * EVENT_SIZE;
        Event& event = events[i];
        event.time = read_u32(data);
        event.kind = data[4];
        event.voice = data[5];
        event.frequency = read_u32(data + 6);
        event.on_time = read_u32(data + 10);
        // all events are checked before any of them is applied
        switch (event.kind) {
        case Event::VOICE_ON:
        case Event::VOICE_SET:
            if (event.frequency < SparkGenerator::MIN_FREQUENCY || event.frequency > SparkGenerator::MAX_FREQUENCY ||
                event.on_time > SparkGenerator::ABSOLUTE_MAX_ON_TIME)
                return OUT_OF_RANGE;
            // fall through to check the voice
        case Event::VOICE_OFF:
            if (event.voice >= SparkGenerator::NUM_VOICES)
                return OUT_OF_RANGE;
            break;
        case Event::ALL_OFF:
            break;
        default:
            return OUT_OF_RANGE;
        }
    }
    if (type == IMMEDIATE) {
        for (uint8_t i = 0; i < count; i++)
            Sequencer::apply_now(events[i]);
        return OK;
    }
    if (Sequencer::state() == Sequencer::STOPPED)
        return WRONG_STATE;
    return Sequencer::push(events, count) ? OK : QUEUE_FULL;
}

void Protocol::send_reply(uint8_t type, uint8_t seq, Status status, const uint8_t* body, uint8_t len, bool keep) {
    uint8_t frame[2 + REPLY_HEADER + MAX_BODY + 2];
    uint8_t* out = frame;
    *out++ = type;
    *out++ = seq;
    *out++ = status;
    out = write_u32(out, Sequencer::clock());
    out = write_u16(out, Sequencer::queue_free());
    memcpy(out, body, len);
    out += len;
    out = write_u16(out, crc16(frame, out - frame));

    uint8_t encoded[MAX_ENCODED + 1];
    uint16_t encoded_len = cobs_encode(frame, out - frame, encoded);
    Serial.write(encoded, encoded_len);
    if (keep) {
        uint8_t slot = seq % HISTORY;
        memcpy(history[slot], encoded, encoded_len);
        history_len[slot] = encoded_len;
        history_seq[slot] = seq;
    }
}
