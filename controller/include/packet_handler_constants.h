// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

#pragma once

namespace PHC {
// return values for packet handler calls
enum class PACKET_HANDLE_RESULT {
    RESULT_OK,    // packet handled successfully, send ACK
    RESULT_NOK,   // packet not handled successfully, send NACK
    RESULT_WAIT,  // packet not yet handled, will be retried later, send nothing
};
}  // namespace PHC
