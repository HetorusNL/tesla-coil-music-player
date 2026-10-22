// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

// #define DEBUG_PRINTS

#ifdef DEBUG_PRINTS
#define debugprint(obj) \
    { \
        /* TODO: update to STX, 0xff, 0x00, ETX */ \
        uint8_t reply[] = {0x02, 0xff, 0x00, 0x03}; \
        Serial.write(reply, sizeof(reply)); \
        Serial.print(obj); \
    }
#else
#define debugprint(obj)
#endif
