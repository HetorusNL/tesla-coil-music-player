// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

// piano roll of a song with the coil load below it, drawn on the canvases inside a roll element
window.teslaRoll = (() => {
  const MUTED_ALPHA = 0.15;
  const rolls = new Map();

  function get(id) {
    const container = getHtmlElement(id);
    if (!container) return null;
    let roll = rolls.get(id);
    if (!roll || roll.container !== container) {
      roll = {
        container,
        notes: container.querySelector(".roll-notes"),
        load: container.querySelector(".roll-load"),
        played: container.querySelector(".roll-played"),
        song: null,
        enabled: new Set(),
        loadBins: [],
        maxLoad: 9,
      };
      new ResizeObserver(() => draw(roll)).observe(container);
      rolls.set(id, roll);
    }
    return roll;
  }

  function prepare(canvas) {
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(canvas.clientWidth * ratio);
    canvas.height = Math.round(canvas.clientHeight * ratio);
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    return [ctx, canvas.width, canvas.height, ratio];
  }

  function loadColor(load, maxLoad) {
    if (load > maxLoad) return "#eb5757"; // more notes than coil timers, so notes are dropped
    if (load >= maxLoad - 1) return "#f2994a";
    if (load > 4) return "#f2c94c";
    return "#27ae60";
  }

  function draw(roll) {
    const song = roll.song;
    const [ctx, width, height, ratio] = prepare(roll.notes);
    if (song && song.length > 0) {
      const rows = song.high - song.low + 1;
      const rowHeight = height / rows;
      // draw the muted parts first, so the enabled parts are drawn on top of them
      for (const enabledPass of [false, true]) {
        for (const [start, duration, note, part] of song.notes) {
          if (roll.enabled.has(part) !== enabledPass) continue;
          ctx.globalAlpha = enabledPass ? 1 : MUTED_ALPHA;
          ctx.fillStyle = song.colors[part];
          const x = (start / song.length) * width;
          const w = Math.max((duration / song.length) * width, 1.5 * ratio);
          const h = Math.max(rowHeight, 2 * ratio);
          ctx.fillRect(x, height - (note - song.low + 1) * rowHeight, w, h);
        }
      }
      ctx.globalAlpha = 1;
    }

    const [loadCtx, loadWidth, loadHeight] = prepare(roll.load);
    const bins = roll.loadBins;
    bins.forEach((load, idx) => {
      if (!load) return;
      loadCtx.fillStyle = loadColor(load, roll.maxLoad);
      const x = Math.floor((idx / bins.length) * loadWidth);
      loadCtx.fillRect(x, 0, Math.ceil(loadWidth / bins.length) + 1, loadHeight);
    });
  }

  return {
    // song: {length, low, high, colors: [color per part], notes: [[start, duration, note, part index], ...]}
    setSong(id, song) {
      const roll = get(id);
      if (!roll) return;
      roll.song = song;
      draw(roll);
    },
    setEnabled(id, enabledParts, loadBins, maxLoad) {
      const roll = get(id);
      if (!roll) return;
      roll.enabled = new Set(enabledParts);
      roll.loadBins = loadBins;
      roll.maxLoad = maxLoad;
      draw(roll);
    },
    setPosition(id, fraction, animate) {
      const roll = get(id);
      if (!roll) return;
      // animate the regular updates, but jump straight to a new position when seeking or stopping
      roll.played.style.transition = animate ? "width 0.2s linear" : "none";
      roll.played.style.width = `${Math.min(Math.max(fraction, 0), 1) * 100}%`;
    },
  };
})();
