// Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
//
// This file is part of the Tesla Coil Music Player.

// chart of the spark on-time per frequency, the points of the on-time table can be dragged up and down
window.teslaSparkChart = (() => {
  const MAX_FREQUENCY = 2000; // Hz, beyond the last band the on-time stays the same
  const BAND = 100; // Hz per entry of the on-time table
  const MARGIN = { left: 44, right: 24, top: 12, bottom: 28 };
  const TICK_STEPS = [5, 10, 20, 25, 50];
  const NOTES = [
    [110, "A2"],
    [220, "A3"],
    [440, "A4"],
    [880, "A5"],
    [1760, "A6"],
  ];
  const charts = new Map();

  function get(id) {
    const container = getHtmlElement(id);
    if (!container) return null;
    let chart = charts.get(id);
    if (!chart || chart.container !== container) {
      chart = { container, data: null, drag: null };
      new ResizeObserver(() => draw(chart)).observe(container);
      charts.set(id, chart);
    }
    return chart;
  }

  // mirrors CoilTimer::get_on_time and CoilTimer::create_spark of the firmware
  function sparkOnTime(points, data, frequency, factor) {
    const index = Math.floor(frequency / BAND);
    let onTime = points[points.length - 1];
    if (index < points.length - 1) {
      onTime = points[index] + ((points[index + 1] - points[index]) * ((frequency % BAND) + 1)) / BAND;
    }
    const maxOnTime = Math.min(data.max, data.absoluteMax);
    return Math.trunc(Math.min(Math.max(onTime * factor, data.min), maxOnTime));
  }

  // the top of the on-time axis: room above the max on-time and the points, but not beyond the absolute maximum
  function axisTop(data) {
    const highest = Math.max(Math.min(data.max, data.absoluteMax), ...data.points) * 1.15;
    const step = tickStep(highest);
    return Math.min(Math.max(Math.ceil(highest / step) * step, 20), data.absoluteMax);
  }

  function tickStep(top) {
    return TICK_STEPS.find((step) => top / step <= 6) ?? TICK_STEPS[TICK_STEPS.length - 1];
  }

  function draw(chart) {
    const data = chart.data;
    const width = chart.container.clientWidth;
    const height = chart.container.clientHeight;
    if (!data || !width || !height) return;
    const plotWidth = width - MARGIN.left - MARGIN.right;
    const plotHeight = height - MARGIN.top - MARGIN.bottom;
    // keep the scale while dragging, so the point stays under the pointer
    const top = chart.drag ? chart.drag.top : axisTop(data);
    const x = (frequency) => MARGIN.left + (frequency / MAX_FREQUENCY) * plotWidth;
    const y = (onTime) => MARGIN.top + (1 - Math.min(onTime, top) / top) * plotHeight;
    const points = chart.drag ? chart.drag.points : data.points;

    const parts = [];
    // grid with the on-time and frequency axes, and some notes for orientation
    for (let onTime = 0; onTime <= top; onTime += tickStep(top)) {
      parts.push(`<line x1="${MARGIN.left}" x2="${width - MARGIN.right}" y1="${y(onTime)}" y2="${y(onTime)}"
        stroke="currentColor" stroke-opacity="0.12"/>`);
      parts.push(`<text x="${MARGIN.left - 6}" y="${y(onTime) + 4}" text-anchor="end" font-size="11"
        fill="currentColor" fill-opacity="0.6">${onTime} µs</text>`);
    }
    for (let frequency = 0; frequency <= MAX_FREQUENCY; frequency += 500) {
      parts.push(`<text x="${x(frequency)}" y="${height - 8}" text-anchor="middle" font-size="11"
        fill="currentColor" fill-opacity="0.6">${frequency} Hz</text>`);
    }
    for (const [frequency, name] of NOTES) {
      parts.push(`<line x1="${x(frequency)}" x2="${x(frequency)}" y1="${MARGIN.top}" y2="${height - MARGIN.bottom}"
        stroke="currentColor" stroke-opacity="0.12" stroke-dasharray="2 3"/>`);
      parts.push(`<text x="${x(frequency) + 3}" y="${MARGIN.top + 10}" font-size="10"
        fill="currentColor" fill-opacity="0.45">${name}</text>`);
    }
    // the min and max on-time limit every spark
    for (const [onTime, label] of [
      [data.min, "min"],
      [Math.min(data.max, data.absoluteMax), "max"],
    ]) {
      parts.push(`<line x1="${MARGIN.left}" x2="${width - MARGIN.right}" y1="${y(onTime)}" y2="${y(onTime)}"
        stroke="#eb5757" stroke-opacity="0.7" stroke-dasharray="6 4"/>`);
      parts.push(`<text x="${width - MARGIN.right - 2}" y="${y(onTime) - 4}" text-anchor="end" font-size="10"
        fill="#eb5757">${label} ${onTime} µs</text>`);
    }
    // the resulting spark at full velocity, and at a soft velocity when the velocity matters
    const curve = (factor) => {
      const coordinates = [];
      for (let frequency = 0; frequency <= MAX_FREQUENCY; frequency += 5) {
        coordinates.push(`${x(frequency)},${y(sparkOnTime(points, data, frequency, data.volume * factor))}`);
      }
      return coordinates.join(" ");
    };
    if (data.softFactor !== null) {
      parts.push(`<polyline points="${curve(data.softFactor)}" fill="none" stroke="var(--q-primary)"
        stroke-opacity="0.5" stroke-width="1.5" stroke-dasharray="4 3"/>`);
    }
    parts.push(`<polyline points="${curve(1)}" fill="none" stroke="var(--q-primary)" stroke-width="2"/>`);
    // the points of the table, before the volume, min and max apply
    points.forEach((onTime, index) => {
      const clipped = onTime > top;
      parts.push(`<circle class="spark-point" data-index="${index}" cx="${x(index * BAND)}" cy="${y(onTime)}"
        r="6" fill="${clipped ? "#eb5757" : "var(--q-primary)"}" stroke="white" stroke-width="1.5"
        style="cursor: ns-resize"/>`);
    });
    if (chart.drag) {
      const onTime = points[chart.drag.index];
      parts.push(`<text x="${x(chart.drag.index * BAND)}" y="${y(onTime) - 12}" text-anchor="middle" font-size="12"
        font-weight="bold" fill="currentColor">${Math.round(onTime)} µs</text>`);
    }

    chart.container.innerHTML = `<svg width="${width}" height="${height}">${parts.join("")}</svg>`;
    chart.container.querySelectorAll(".spark-point").forEach((circle) => {
      circle.addEventListener("pointerdown", (event) => startDrag(chart, event, Number(circle.dataset.index)));
    });
  }

  function startDrag(chart, event, index) {
    event.preventDefault();
    const plotHeight = chart.container.clientHeight - MARGIN.top - MARGIN.bottom;
    chart.drag = { index, points: [...chart.data.points], top: axisTop(chart.data) };
    const move = (moveEvent) => {
      // the svg is replaced on every redraw, so measure the container that stays
      const plotTop = chart.container.getBoundingClientRect().top + MARGIN.top;
      const fraction = 1 - (moveEvent.clientY - plotTop) / plotHeight;
      chart.drag.points[index] = Math.round(Math.min(Math.max(fraction, 0), 1) * chart.drag.top);
      draw(chart);
    };
    const end = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      const value = chart.drag.points[index];
      // keep showing the dragged value until the new settings arrive
      chart.data = { ...chart.data, points: chart.drag.points };
      chart.drag = null;
      draw(chart);
      chart.container.dispatchEvent(new CustomEvent("pointmoved", { detail: { index, value } }));
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    move(event);
  }

  return {
    set(id, data) {
      const chart = get(id);
      if (!chart) return;
      chart.data = data;
      if (!chart.drag) draw(chart);
    },
  };
})();
