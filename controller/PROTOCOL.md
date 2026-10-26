# Controller protocol (version 3)

The serial protocol between the GUI (host) and the controller firmware. Version 2 replaced version 1, which streamed
MIDI messages in real time and waited for an ACK after each one. Version 3 adds `SET_LEDS`. The firmware side is in `controller/src/protocol.cpp`,
the host side in `gui/protocol.py`.

## Goals

- **The controller is a voice engine, not a MIDI player.** The host turns notes into voices with a frequency and an
  on-time. The note-to-voice allocation, the on-time table, the volume and the velocity all live in Python. The
  firmware only schedules sparks and enforces the limits of the coil.
- **Exact timing.** Events carry a timestamp and wait in a queue on the controller, which plays them on its own
  clock. USB and PC scheduling jitter no longer reach the coil, and chords start on the same tick.
- **Sub-microsecond on-times** (in ns), which version 1 couldn't express.
- **Robust framing.** A corrupted byte is detected (CRC) and can't desynchronize the parser (COBS).
- **Safe when the host goes away.** A watchdog silences the coil when no frames arrive.

## Transport and framing

- USB virtual COM port of the ST-LINK (USART3), 921600 baud, 8N1, about 92 kB/s.
- Every frame is COBS-encoded and ends with a `0x00` delimiter. After decoding, a frame is:

  | Bytes | Field                                      |
  | ----- | ------------------------------------------ |
  | 1     | type                                       |
  | 1     | seq: sequence number, the reply echoes it  |
  | n     | body, depends on the type                  |
  | 2     | CRC-16/CCITT-FALSE over type, seq and body |

- The body of a request is at most 240 bytes, the body of a reply at most 233 bytes after its header, so an encoded
  frame always fits in 256 bytes.
- All multi-byte values are little-endian.
- A frame with a bad CRC, length or COBS is dropped and counted in the `PING` status. It gets no reply, so the host
  sends it again after a timeout.

## Requests and replies

The host sends requests, and the controller answers each one with exactly one reply, with the same type and `seq`.

- **In order.** `seq` increments per request, wrapping at 255. The controller handles the requests strictly in order
  of `seq`, starting from the `seq` of the `HELLO`.
- **Window.** The host may send up to 4 requests beyond the oldest one without a reply, without waiting.
- **Retransmissions.** A request that gets no reply within 200 ms is sent again, together with the ones after it. The
  controller keeps the replies of its last 8 requests: a request it handled already gets the same reply again, but
  isn't applied twice.
- **Gaps.** A request after a lost one gets `OUT_OF_SEQUENCE` with the `seq` the controller expects (u8) as its body,
  and isn't applied. The host then sends everything again from that `seq`.
- **Lost connection.** The host gives up after 5 retransmissions of the same request.
- Every reply starts with the reply header, followed by a body that depends on the type:

  | Bytes | Field                                                |
  | ----- | ---------------------------------------------------- |
  | 1     | status                                               |
  | 4     | clock: the stream clock (µs) when the reply was sent |
  | 2     | queue_free: the free entries in the event queue      |

  So the host always knows the position of the song and how much it may still send.

| Status | Name            | Meaning                                                                         |
| ------ | --------------- | ------------------------------------------------------------------------------- |
| 0x00   | OK              | applied                                                                         |
| 0x01   | UNKNOWN_TYPE    |                                                                                 |
| 0x02   | BAD_LENGTH      | the body doesn't match the type                                                 |
| 0x03   | OUT_OF_RANGE    | a value beyond the absolute limits or the number of voices, nothing applied     |
| 0x04   | QUEUE_FULL      | none of the events were queued, retry after the clock has moved on              |
| 0x05   | WRONG_STATE     | e.g. events while the stream is stopped, or a request before the `HELLO`        |
| 0x06   | OUT_OF_SEQUENCE | a request after a lost one, not applied, the body is the `seq` it expects       |

## The stream clock and the event queue

- The controller has a stream clock in µs, counted from TIM2 at 108 MHz, so it never drifts against the sparks.
- The stream is **stopped**, **ready** (the clock stands still at its start time, the queue takes events) or
  **running**.
- Queued events are applied in order at their timestamp, exact to a timer tick: the controller hands them to the
  spark generator 100 µs ahead, with their tick. A voice that starts at time _t_ sparks for the first time at _t_, so
  the sparks of a chord line up deterministically.
- An event whose time already passed when it's due is applied right away and counted as late in the `PING` status.
- The queue holds 4095 events. The host keeps it filled up to 500 ms ahead of the clock.
- Live notes (the keyboard) use `IMMEDIATE` instead of the queue.
- Pausing is stopping: the host remembers the position, and resumes with `STREAM_START` at that position, the
  `VOICE_ON` events of the notes that sound there, and the events after it.

## Message types (host to controller)

| Type | Name         | Body                                                  | Reply body after the header |
| ---- | ------------ | ----------------------------------------------------- | --------------------------- |
| 0x01 | HELLO        | protocol_version u8                                   | see below                   |
| 0x02 | PING         | –                                                     | status, see below           |
| 0x03 | SET_LIMITS   | max_on_time ns u32, min_on_time ns u32, max_duty u16  | –                           |
| 0x04 | EVENTS       | count u8, count × event                               | –                           |
| 0x05 | IMMEDIATE    | count u8, count × event (the time is ignored)         | –                           |
| 0x06 | STREAM_START | clock µs u32                                          | –                           |
| 0x07 | STREAM_RUN   | –                                                     | –                           |
| 0x08 | FLUSH        | –                                                     | –                           |
| 0x09 | STOP         | –                                                     | –                           |
| 0x0A | SET_LEDS     | green_led u8                                          | –                           |
| 0xF0 | TEST         | test command (only in the `nucleo_spark_test` build)  | test data                   |

- **HELLO** is the first request after connecting. It stops the stream and the voices, and its `seq` starts the
  sequence. Its reply body tells the host what the firmware supports: protocol_version u8, num_voices u8, queue_size
  u16, clock_hz u32, absolute_max_on_time ns u32, absolute_max_duty u16 (1/10000), min_off_time ns u32. When the host
  asks for another version, the status is `OUT_OF_RANGE` and the host disconnects.
- **PING** keeps the connection (and the watchdog) alive. Its reply body: state u8 (0 stopped, 1 ready, 2 running),
  active voices u32 (bitmask), duty limited u8 (whether sparks were shortened to stay within the max duty cycle),
  late events u16, rejected frames u16. The counters and the flag count since the previous `PING`.
- **SET_LIMITS** takes the coil settings that the firmware enforces itself. A spark is never longer than
  `max_on_time`. A spark that the duty-cycle limit would make shorter than `min_on_time` is skipped. `max_duty` is in
  1/10000. Values beyond the absolute limits, or a `min_on_time` beyond `max_on_time`, reply `OUT_OF_RANGE`. Until the
  host sets them, the limits are 100 µs, 10 µs and 10%.
- **EVENTS** queues the events, which needs a ready or running stream.
- **STREAM_START** stops all voices, empties the queue and sets the clock to the given time, ready. Starting a song
  from a position, resuming and seeking all use it.
- **STREAM_RUN** starts the clock of a ready stream, 2 ms after the request, so the events at the start time are
  applied in time.
- **FLUSH** empties the queue, but keeps the clock and the voices as they are. The host uses it when the future
  changes: a part is muted, a setting changes. It then sends `IMMEDIATE` corrections (e.g. `VOICE_OFF` for the
  muted notes) and refills the queue from the current clock.
- **STOP** stops all voices, empties the queue and stops the clock.
- **SET_LEDS** sets what the LEDs show: `green_led` 1 lights the green LED while voices play, 0 keeps it off.
  Other values reply `OUT_OF_RANGE`. Until the host sets it, the green LED shows the voices.
- **TEST** commands of the test firmware: 0x01 starts a new spark log, 0x02 stops logging and replies clock_hz u32,
  the tick at stream clock 0 u32, the current tick u32 and the numbers of sparks, pulses, missed pulses and dropped
  entries (u32 each), 0x03 which u8 (0 sparks, 1 pulses), first u16 replies up to 29 entries of 8 bytes.

### Events

Every event is 14 bytes: time µs u32, kind u8, voice u8, frequency mHz u32, on_time ns u32 (zero when unused).

| Kind | Name      | Data                     |
| ---- | --------- | ------------------------ |
| 0x01 | VOICE_ON  | frequency, on_time       |
| 0x02 | VOICE_SET | frequency, on_time       |
| 0x03 | VOICE_OFF | –                        |
| 0x04 | ALL_OFF   | – (the voice is ignored) |

- `VOICE_ON` starts the voice with its first spark at the event time.
- `VOICE_SET` changes a sounding voice from its first spark at or after the event time, and keeps its phase: pitch
  bend, vibrato, a volume change. A voice that doesn't sound starts.
- `VOICE_OFF` stops the voice before its first spark at or after the event time.
- Frequencies are 1 Hz to 20 kHz, on-times up to the absolute max on-time, voices below num_voices. One event out of
  range makes the whole request `OUT_OF_RANGE`.
- `EVENTS` holds at most 17 events (239 bytes), so a chord of up to 17 notes fits in a single frame.

## Watchdog

When no valid frame arrives for 1 s, the controller stops the stream and all voices, and lights the red LED. The
host sends a `PING` at least every 250 ms while connected, also while a song plays.

## Capacity

- **Queue.** 500 ms of a dense song is a few hundred events: 16 voices that each change notes 20 times a second are
  320 note ons and offs. Vibrato on all 16 voices at 100 updates a second adds 800 `VOICE_SET` events. The 4095
  entries leave room for 4 times that, in 64 kB of the 320 kB RAM.
- **Bandwidth.** 921600 baud carries about 92 kB/s. A full `EVENTS` frame costs about 14.4 bytes per event, so the
  link carries about 6400 events a second, against a few hundred for normal music. Filling the whole lookahead after a
  seek (1000 events, about 15 kB) takes about 160 ms.
- **Latency.** A full frame takes 2.7 ms on the wire, plus about 1 ms for the USB frames of the ST-LINK, which is
  fine for the live keyboard.
- **Receive buffer.** The Arduino core buffers only 64 received bytes by default, less than a single frame. The
  firmware builds with `SERIAL_RX_BUFFER_SIZE=1024`, so a busy main loop doesn't lose bytes.
- **Faster links**, if ever needed: the ST-LINK/V2-1 may accept higher baud rates (to be measured), and the USB
  connector of the STM32F746 itself can run a native USB CDC port at about 1 MB/s, at the cost of a second cable and
  the USB stack in the firmware. Neither is needed for this protocol.

## Tests

`uv run pytest` runs the tests of `tests/`: the framing against the C++ code of the firmware, and the retransmissions
against a fake controller. With the test firmware on the controller (`pio run -e nucleo_spark_test -t upload`), D10
wired to A0 and the coil disconnected, `uv run pytest --port /dev/ttyACM0` also runs the hardware tests, which
measure the sparks on the coil pin with the controller itself.
