// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

// tiny square wave synth that previews songs and live notes on the speaker of the browser
window.teslaSynth = (() => {
  const LOOKAHEAD = 1.0; // seconds of notes scheduled ahead of time
  const START_DELAY = 0.1; // seconds between starting a sequence and its first note
  let ctx = null;
  let master = null;
  const liveVoices = new Map();
  let sequence = [];
  let sequenceIndex = 0;
  let sequenceStart = 0;
  let sequenceVoices = [];
  let sequenceTimer = null;

  function audio() {
    if (!ctx) {
      ctx = new AudioContext();
      master = ctx.createGain();
      master.gain.value = 0.1;
      master.connect(ctx.destination);
    }
    if (ctx.state === "suspended") ctx.resume();
    return ctx;
  }

  function startVoice(note, velocity, start) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "square";
    osc.frequency.value = 440 * Math.pow(2, (note - 69) / 12);
    gain.gain.setValueAtTime(0, start);
    gain.gain.linearRampToValueAtTime((0.3 + 0.7 * velocity / 127) * 0.5, start + 0.005);
    osc.connect(gain).connect(master);
    osc.start(start);
    return { osc, gain };
  }

  function stopVoice(voice, end) {
    voice.gain.gain.cancelScheduledValues(end);
    voice.gain.gain.setTargetAtTime(0, end, 0.01);
    try {
      voice.osc.stop(end + 0.1);
    } catch (e) {
      // the oscillator was already stopped
    }
  }

  function scheduleSequence() {
    const horizon = ctx.currentTime + LOOKAHEAD;
    while (sequenceIndex < sequence.length && sequenceStart + sequence[sequenceIndex][0] < horizon) {
      const [start, duration, note, velocity] = sequence[sequenceIndex++];
      const voice = startVoice(note, velocity, sequenceStart + start);
      voice.end = sequenceStart + start + Math.max(duration, 0.02);
      stopVoice(voice, voice.end);
      sequenceVoices.push(voice);
    }
    // forget the voices that already finished
    sequenceVoices = sequenceVoices.filter((voice) => voice.end > ctx.currentTime);
    if (sequenceIndex >= sequence.length) {
      clearInterval(sequenceTimer);
      sequenceTimer = null;
    }
  }

  return {
    noteOn(note, velocity) {
      audio();
      if (liveVoices.has(note)) return;
      liveVoices.set(note, startVoice(note, velocity, ctx.currentTime));
    },
    noteOff(note) {
      const voice = liveVoices.get(note);
      if (!voice) return;
      stopVoice(voice, ctx.currentTime);
      liveVoices.delete(note);
    },
    releaseAll() {
      for (const note of [...liveVoices.keys()]) this.noteOff(note);
    },
    // plays [start, duration, note, velocity] notes (sorted by start), from the offset in seconds
    playSequence(notes, offset = 0) {
      this.stopSequence();
      audio();
      sequence = notes;
      sequenceIndex = 0;
      while (sequenceIndex < sequence.length && sequence[sequenceIndex][0] < offset) sequenceIndex++;
      sequenceStart = ctx.currentTime + START_DELAY - offset;
      scheduleSequence();
      sequenceTimer = setInterval(scheduleSequence, 200);
    },
    stopSequence() {
      if (sequenceTimer) clearInterval(sequenceTimer);
      sequenceTimer = null;
      if (ctx) for (const voice of sequenceVoices) stopVoice(voice, ctx.currentTime);
      sequenceVoices = [];
      sequence = [];
    },
  };
})();
