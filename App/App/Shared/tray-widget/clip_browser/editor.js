"use strict";

// The editor: the selected clip in the main pane with a timeline - trim
// handles, a cut tool, and one lane per audio track for volume changes,
// muting and shifting - and the exports. The project (see clip_edit.py)
// is saved as it changes; clip_browser.py turns it into ffmpeg commands.
//
// Preview: the video plays muted and each audio track plays from its own
// extracted .m4a through Web Audio, kept in step with the video, so track
// edits are heard as they'll be exported. With split tracks the mix is
// Mic + Desktop (as OBS records it) with the Mix lane's edits on top.

const MIN_SLIDER_DB = -30; // the slider's left end means "muted"
const MUTE_DB = -60;
const MAX_DB = 12;
const MIN_SPAN = 0.05;
const SAVE_DELAY_MS = 400;
const EXPORT_POLL_MS = 500;
const SYNC_TOLERANCE = 0.12;
const DRAG_THRESHOLD_PX = 4;
const NUDGE_MS = 10;

const ed = {
  name: null, // the clip being edited
  info: null, // {duration, width, height, fps, split, tracks: [{index, title}]}
  trim: [0, 0],
  cuts: [], // removed [start, end] ranges inside the trim
  tracks: [], // the project's tracks
  peaks: {}, // track index -> {per_second, peaks}
  showUnused: false, // lanes for app slots nothing was recorded on
  selection: null, // {start, end, track} - track null: the video lane
  region: null, // {track, index} - the volume change being edited
  solo: new Set(),
  history: [],
  future: [],
  pending: null, // snapshot from the start of an edit still in progress
  saveTimer: null,
  saving: Promise.resolve(),
  discord: null,
  custom: null, // the custom export's options: {heights, fps, video_kbps, audio_kbps}
  missing: [],
  loadToken: 0,
};

const audio = { ctx: null, out: null, master: null, nodes: new Map() };

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

function formatTime(seconds) {
  if (seconds == null || !isFinite(seconds)) return "0:00.0";
  const sign = seconds < 0 ? "-" : "";
  const total = Math.abs(seconds);
  const m = Math.floor(total / 60);
  const s = (total - m * 60).toFixed(1).padStart(4, "0");
  return `${sign}${m}:${s}`;
}

function formatDb(db) {
  if (db <= MUTE_DB) return "Muted";
  return `${db > 0 ? "+" : ""}${Math.round(db * 10) / 10} dB`;
}

function dbToGain(db) {
  return db <= MUTE_DB ? 0 : 10 ** (db / 20);
}

function sliderToDb(value) {
  return Number(value) <= MIN_SLIDER_DB ? MUTE_DB : Number(value);
}

function dbToSlider(db) {
  return db <= MUTE_DB ? MIN_SLIDER_DB : clamp(db, MIN_SLIDER_DB + 1, MAX_DB);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function duration() {
  return (ed.info && ed.info.duration) || 0;
}

function trackTitle(index) {
  const track = ed.info.tracks.find((t) => t.index === index);
  return track ? track.title : `Track ${index + 1}`;
}

// ---------------------------------------------------------------------------
// The project: trim + cuts <-> the kept ranges clip_edit.py exports
// ---------------------------------------------------------------------------

function keepRanges() {
  const [start, end] = ed.trim;
  const cuts = ed.cuts.filter(([a, b]) => b > start && a < end).sort((x, y) => x[0] - y[0]);
  const keep = [];
  let pos = start;
  for (const [a, b] of cuts) {
    if (a > pos) keep.push([pos, a]);
    pos = Math.max(pos, b);
  }
  if (end > pos) keep.push([pos, end]);
  return keep.filter(([a, b]) => b - a >= MIN_SPAN);
}

function loadKeep(keep) {
  if (!keep.length) {
    ed.trim = [0, duration()];
    ed.cuts = [];
    return;
  }
  ed.trim = [keep[0][0], keep[keep.length - 1][1]];
  ed.cuts = keep.slice(1).map((segment, i) => [keep[i][1], segment[0]]);
}

function project() {
  return { version: 1, keep: keepRanges(), tracks: ed.tracks };
}

function keptLength() {
  return keepRanges().reduce((sum, [a, b]) => sum + b - a, 0);
}

function snapshot() {
  return JSON.stringify({ trim: ed.trim, cuts: ed.cuts, tracks: ed.tracks });
}

function restore(snap) {
  const data = JSON.parse(snap);
  ed.trim = data.trim;
  ed.cuts = data.cuts;
  ed.tracks = data.tracks;
  ed.region = null;
}

// An edit made in one go (a click) - or, with beginEdit/endEdit, one made
// over many events (a drag, a slider) - is one undo step.
function change(fn) {
  beginEdit();
  fn();
  endEdit();
}

function beginEdit() {
  if (ed.pending === null) ed.pending = snapshot();
}

function endEdit() {
  if (ed.pending === null) return;
  const before = ed.pending;
  ed.pending = null;
  if (snapshot() === before) return;
  if (!keepRanges().length) {
    restore(before);
    setStatus("That would remove the whole clip.", true);
  } else {
    ed.history.push(before);
    ed.future = [];
  }
  edited();
}

// Something changed (mid-edit too): redraw and save soon.
function edited() {
  renderTimeline();
  renderPanel();
  renderToolbar();
  renderCustomExport();
  scheduleSave();
}

function undo() {
  if (!ed.history.length) return;
  ed.future.push(snapshot());
  restore(ed.history.pop());
  edited();
}

function redo() {
  if (!ed.future.length) return;
  ed.history.push(snapshot());
  restore(ed.future.pop());
  edited();
}

function scheduleSave() {
  clearTimeout(ed.saveTimer);
  ed.saveTimer = setTimeout(flushSave, SAVE_DELAY_MS);
}

function flushSave() {
  clearTimeout(ed.saveTimer);
  ed.saveTimer = null;
  const name = ed.name;
  if (!name) return ed.saving;
  const body = { name, project: project() };
  ed.saving = ed.saving.then(async () => {
    try {
      const result = await api("api/project", body);
      if (ed.name === name) {
        ed.discord = result.discord;
        renderExportBar();
      }
    } catch (err) {
      setStatus(`Couldn't save the edit: ${err.message}`, true);
    }
  });
  return ed.saving;
}

// ---------------------------------------------------------------------------
// Loading a clip
// ---------------------------------------------------------------------------

function select(name) {
  if (state.selected === name) {
    renderDetails();
    return;
  }
  if (ed.name && ed.saveTimer) flushSave();
  state.selected = name;
  for (const li of list.children) li.classList.toggle("selected", li.dataset.name === name);
  setStatus("");
  player.src = mediaUrl(name);
  player.muted = true;
  loadEditor(name);
  renderDetails();
}

// Stops showing the selected clip and lets go of its files (before it's deleted).
function closeClip() {
  if (ed.name && ed.saveTimer) flushSave();
  ed.loadToken++;
  ed.name = null;
  ed.info = null;
  teardownAudio();
  player.pause();
  state.selected = null;
  for (const li of list.children) li.classList.remove("selected");
  renderDetails();
}

async function loadEditor(name) {
  const token = ++ed.loadToken;
  $("out-name").value = name.replace(/\.[^.]+$/, "");
  ed.name = null;
  ed.info = null;
  teardownAudio();
  renderDetails();
  let data;
  try {
    data = await api("api/project?name=" + encodeURIComponent(name));
  } catch (err) {
    if (token === ed.loadToken) {
      setStatus(err.message, true);
      renderDetails();
    }
    return;
  }
  if (token !== ed.loadToken) return;
  ed.name = name;
  ed.info = data.info;
  ed.tracks = data.project.tracks;
  loadKeep(data.project.keep);
  ed.discord = data.discord;
  ed.custom = data.custom;
  ed.missing = data.missing || [];
  ed.peaks = {};
  ed.selection = null;
  ed.region = null;
  ed.solo = new Set();
  ed.showUnused = false;
  ed.history = [];
  ed.future = [];
  ed.pending = null;
  setupAudio(name);
  renderDetails();
  for (const track of ed.info.tracks) loadPeaks(name, track.index, token);
}

async function loadPeaks(name, index, token) {
  try {
    const data = await api(`peaks/${index}/${encodeURIComponent(name)}`);
    if (token !== ed.loadToken) return;
    ed.peaks[index] = data;
    drawWaveform(index);
  } catch (err) {
    /* no waveform - the lane still works */
  }
}

function renderDetails() {
  const clip = selectedClip();
  const ready = !!(clip && ed.info && ed.name === clip.name);
  $("editor").hidden = !ready;
  $("splitter").hidden = !ready;
  $("meter").hidden = !ready || !audio.out;
  $("no-selection").hidden = !!clip;
  $("out-name-wrap").hidden = !clip;
  $("clip-meta").textContent = clip
    ? [formatWhen(clip.mtime), formatDuration(clip.duration), formatSize(clip.size)].filter(Boolean).join(" · ")
    : "";
  if (!clip) {
    player.removeAttribute("src");
    player.load();
  }
  renderToolbar();
  if (ready) {
    renderTimeline();
    renderPanel();
    renderExportBar();
    renderEditorButtons();
  }
}

function renderToolbar() {
  const ready = !!ed.info;
  $("undo").disabled = !ready || !ed.history.length;
  $("redo").disabled = !ready || !ed.future.length;
  $("reset").disabled = !ready || isUnedited();
}

// Nothing changed - in one track, or (no index) in the whole clip.
function isUnedited(index) {
  const plain = (t) => !t.volume_db && !t.offset && !t.muted && !t.regions.length;
  if (index !== undefined) return plain(trackOf(index));
  const [start, end] = ed.trim;
  return start <= 0.001 && end >= duration() - 0.001 && !ed.cuts.length && ed.tracks.every(plain);
}

function resetEdits() {
  change(() => {
    ed.trim = [0, duration()];
    ed.cuts = [];
    ed.tracks = ed.tracks.map((t) => ({ index: t.index, volume_db: 0, offset: 0, muted: false, regions: [] }));
    ed.selection = null;
  });
}

// ---------------------------------------------------------------------------
// Timeline
// ---------------------------------------------------------------------------

const timeline = $("timeline");

function pct(seconds) {
  return `${(clamp(seconds, 0, duration()) / (duration() || 1)) * 100}%`;
}

function placeSpan(node, start, end) {
  node.style.left = pct(start);
  node.style.width = `calc(${pct(end)} - ${pct(start)})`;
}

// Every lane spans the same x range; the timeline is redrawn during drags,
// so this always measures the lanes on the page now.
function laneRect() {
  const lane = timeline.querySelector(".lane");
  return lane ? lane.getBoundingClientRect() : { left: 0, width: 1 };
}

function laneTime(clientX) {
  const rect = laneRect();
  return clamp((clientX - rect.left) / (rect.width || 1), 0, 1) * duration();
}

function secondsPerPixel() {
  return duration() / (laneRect().width || 1);
}

// "Track 5 (unused)": an app slot no app had when the clip was saved.
function isUnused(index) {
  return /\(unused\)$/.test(trackTitle(index)) && isUnedited(index);
}

function shownTracks() {
  return ed.info.tracks.filter((t) => ed.showUnused || !isUnused(t.index));
}

function renderTimeline() {
  if (!ed.info) return;
  const shown = shownTracks();
  timeline.replaceChildren(scrubRow(), videoRow(), ...shown.map((t) => audioRow(t.index)));
  for (const track of shown) drawWaveform(track.index);
  const hidden = ed.info.tracks.length - shown.length;
  const toggle = $("toggle-unused");
  toggle.hidden = !hidden && !ed.showUnused;
  toggle.textContent = ed.showUnused ? "Hide unused tracks" : `Show ${hidden} unused track${hidden === 1 ? "" : "s"}`;
  updatePlayheads();
  $("kept-length").textContent = `Export length ${formatTime(keptLength())}`;
  applyEditorHeight(); // the tracks' height may have changed
}

function removedSpans() {
  const spans = [];
  if (ed.trim[0] > 0) spans.push({ start: 0, end: ed.trim[0], kind: "trimmed" });
  if (ed.trim[1] < duration()) spans.push({ start: ed.trim[1], end: duration(), kind: "trimmed" });
  ed.cuts.forEach(([start, end], index) => spans.push({ start, end, kind: "cut", index }));
  return spans;
}

function addRemoved(lane, interactive) {
  for (const span of removedSpans()) {
    const node = el("div", `removed ${span.kind}`);
    placeSpan(node, span.start, span.end);
    if (interactive && span.kind === "cut") {
      const restoreBtn = el("button", "restore", "↺");
      restoreBtn.title = "Put this section back";
      restoreBtn.addEventListener("pointerdown", (e) => e.stopPropagation());
      restoreBtn.addEventListener("click", () => change(() => ed.cuts.splice(span.index, 1)));
      node.append(restoreBtn);
    }
    lane.append(node);
  }
}

function addSelection(lane, track) {
  const sel = ed.selection;
  if (!sel || sel.track !== track) return;
  const node = el("div", "selection");
  placeSpan(node, sel.start, sel.end);
  lane.append(node);
}

function rulerTicks(lane) {
  const width = timeline.getBoundingClientRect().width || 800;
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
  const step = steps.find((s) => (s / (duration() || 1)) * width >= 70) || 600;
  for (let t = 0; t <= duration() + 1e-6; t += step) {
    const tick = el("span", "tick", formatDuration(t));
    tick.style.left = pct(t);
    lane.append(tick);
  }
}

// A thin lane above the clip with a playhead knob: click or drag anywhere
// on it to move the playhead (playback carries on from where it's let go).
function scrubRow() {
  const row = el("div", "row scrub-row");
  const lane = el("div", "lane scrub-lane");
  lane.title = "Drag to move the playhead";
  lane.append(el("div", "playhead scrub-head"));
  lane.addEventListener("pointerdown", dragPlayhead);
  row.append(el("div", "row-head"), lane);
  return row;
}

function dragPlayhead(e) {
  if (e.button !== 0) return;
  e.preventDefault();
  const wasPlaying = !player.paused;
  if (wasPlaying) player.pause();
  seek(laneTime(e.clientX));
  trackPointer(e, (ev) => seek(laneTime(ev.clientX)), () => {
    if (wasPlaying) togglePlay();
  });
}

function videoRow() {
  const row = el("div", "row video-row");
  const head = el("div", "row-head");
  head.append(el("strong", null, "Clip"), el("span", "muted small", "Trim & cut"));
  const lane = el("div", "lane video-lane");
  rulerTicks(lane);
  addRemoved(lane, true);
  addSelection(lane, null);
  for (const which of [0, 1]) {
    const handle = el("div", `handle ${which ? "end" : "start"}`);
    handle.title = which ? "Drag to trim the end (O)" : "Drag to trim the start (I)";
    handle.style.left = pct(ed.trim[which]);
    handle.addEventListener("pointerdown", (e) => dragTrim(e, which));
    lane.append(handle);
  }
  lane.append(el("div", "playhead"));
  lane.addEventListener("pointerdown", (e) => laneDown(e, null));
  row.append(head, lane);
  return row;
}

function trackOf(index) {
  return ed.tracks.find((t) => t.index === index);
}

function isMaster(index) {
  return ed.info.split && index === 0;
}

function audioRow(index) {
  const track = trackOf(index);
  const row = el("div", "row audio-row");
  row.classList.toggle("muted-track", track.muted);
  const head = el("div", "row-head");
  const titleLine = el("div", "title-line");
  const title = el("strong", null, isMaster(index) ? "Mix" : trackTitle(index));
  titleLine.append(title);
  if (isMaster(index)) {
    head.title = "Mic + Desktop, as recorded. Changes here apply to the whole mix.";
    titleLine.append(el("span", "muted small", "whole mix"));
  } else if (ed.info.split && index >= 3) {
    head.title = "This app is also in Desktop - changes here only affect this track in the .mkv export.";
    titleLine.append(el("span", "muted small", ".mkv only"));
  }
  const mute = el("button", "toggle", "M");
  mute.title = "Mute this track";
  mute.setAttribute("aria-pressed", String(track.muted));
  mute.addEventListener("click", () => change(() => { track.muted = !track.muted; }));
  const solo = el("button", "toggle", "S");
  solo.title = "Listen to only this track (preview only)";
  solo.setAttribute("aria-pressed", String(ed.solo.has(index)));
  solo.addEventListener("click", () => {
    if (ed.solo.has(index)) ed.solo.delete(index);
    else ed.solo.add(index);
    renderTimeline();
  });
  titleLine.append(mute, solo);

  const volume = el("label", "slider-line");
  const slider = el("input");
  slider.type = "range";
  slider.min = MIN_SLIDER_DB;
  slider.max = MAX_DB;
  slider.step = 0.5;
  slider.value = dbToSlider(track.volume_db);
  const value = el("span", "small mono", formatDb(track.volume_db));
  slider.addEventListener("input", () => {
    beginEdit();
    track.volume_db = sliderToDb(slider.value);
    value.textContent = formatDb(track.volume_db);
    scheduleSave();
  });
  slider.addEventListener("change", endEdit);
  slider.title = "Volume of the whole track";
  volume.append(el("span", "small muted", "Vol"), slider, value);
  head.append(titleLine, volume);

  if (!isMaster(index)) {
    const shift = el("div", "shift-line");
    const grip = el("span", "grip", "⇔");
    grip.title = "Drag to shift this track earlier or later";
    grip.addEventListener("pointerdown", (e) => dragShift(e, index));
    const minus = el("button", "nudge", "−");
    const plus = el("button", "nudge", "+");
    minus.title = plus.title = `Shift by ${NUDGE_MS} ms (Shift-click: 100 ms)`;
    minus.addEventListener("click", (e) => nudge(index, e.shiftKey ? -100 : -NUDGE_MS));
    plus.addEventListener("click", (e) => nudge(index, e.shiftKey ? 100 : NUDGE_MS));
    const ms = el("span", "small mono shift-value", `${Math.round(track.offset * 1000)} ms`);
    shift.append(el("span", "small muted", "Shift"), grip, minus, ms, plus);
    head.append(shift);
  }

  const lane = el("div", "lane audio-lane");
  lane.dataset.track = index;
  const canvas = el("canvas", "wave");
  lane.append(canvas);
  addRemoved(lane, false);
  track.regions.forEach((region, regionIndex) => {
    const node = el("div", "region", formatDb(region.db));
    node.classList.toggle("boost", region.db > 0);
    node.classList.toggle("active", !!ed.region && ed.region.track === index && ed.region.index === regionIndex);
    placeSpan(node, region.start, region.end);
    node.title = "Click to change this volume change";
    node.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      ed.region = { track: index, index: regionIndex };
      ed.selection = null;
      renderTimeline();
      renderPanel();
    });
    lane.append(node);
  });
  addSelection(lane, index);
  lane.append(el("div", "playhead"));
  lane.addEventListener("pointerdown", (e) => laneDown(e, index));
  row.append(head, lane);
  return row;
}

function drawWaveform(index) {
  const lane = timeline.querySelector(`.audio-lane[data-track="${index}"]`);
  const canvas = lane && lane.querySelector("canvas");
  const data = ed.peaks[index];
  if (!canvas || !data) return;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
  const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--wave").trim() || "#7d72ff";
  const offset = trackOf(index).offset;
  const perPixel = duration() / width;
  const mid = height / 2;
  // Scaled to the track's own loudest moment, so quiet tracks still show.
  const loudest = data.peaks.reduce((max, peak) => (peak > max ? peak : max), 24);
  const scale = (height - 2) / loudest;
  for (let x = 0; x < width; x++) {
    const from = Math.floor((x * perPixel - offset) * data.per_second);
    const to = Math.max(from + 1, Math.floor(((x + 1) * perPixel - offset) * data.per_second));
    let peak = 0;
    for (let i = Math.max(0, from); i < Math.min(data.peaks.length, to); i++) peak = Math.max(peak, data.peaks[i]);
    const h = Math.max(peak ? 1 : 0, peak * scale);
    ctx.fillRect(x, mid - h / 2, 1, h);
  }
}

window.addEventListener("resize", () => {
  if (ed.info) renderTimeline();
});

// --- Pointer interactions ----------------------------------------------------

// Follows a drag on the whole document - the element it started on may be
// replaced by a redraw before it ends.
function trackPointer(e, onMove, onUp) {
  const id = e.pointerId;
  const move = (ev) => {
    if (ev.pointerId === id) onMove(ev);
  };
  const up = (ev) => {
    if (ev.pointerId !== id) return;
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    document.removeEventListener("pointercancel", up);
    document.body.classList.remove("dragging");
    onUp(ev);
  };
  document.body.classList.add("dragging");
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
  document.addEventListener("pointercancel", up);
}

// Click: move the playhead. Drag: select a range on this lane.
function laneDown(e, track) {
  if (e.button !== 0) return;
  e.preventDefault();
  const startX = e.clientX;
  const anchor = laneTime(e.clientX);
  let dragging = false;
  trackPointer(e, (ev) => {
    if (!dragging && Math.abs(ev.clientX - startX) < DRAG_THRESHOLD_PX) return;
    dragging = true;
    const t = laneTime(ev.clientX);
    ed.selection = { start: Math.min(anchor, t), end: Math.max(anchor, t), track };
    ed.region = null;
    renderTimeline();
  }, () => {
    if (!dragging) {
      ed.selection = null;
      ed.region = null;
      seek(anchor);
      renderTimeline();
    } else if (ed.selection.end - ed.selection.start < MIN_SPAN) {
      ed.selection = null;
      renderTimeline();
    }
    renderPanel();
  });
}

function dragTrim(e, which) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  beginEdit();
  trackPointer(e, (ev) => {
    const t = laneTime(ev.clientX);
    if (which === 0) ed.trim[0] = clamp(t, 0, ed.trim[1] - MIN_SPAN);
    else ed.trim[1] = clamp(t, ed.trim[0] + MIN_SPAN, duration());
    seek(ed.trim[which]);
    renderTimeline();
  }, endEdit);
}

function dragShift(e, index) {
  if (e.button !== 0) return;
  e.preventDefault();
  const track = trackOf(index);
  const startX = e.clientX;
  const startOffset = track.offset;
  const perPx = secondsPerPixel();
  beginEdit();
  trackPointer(e, (ev) => {
    track.offset = Math.round(clamp(startOffset + (ev.clientX - startX) * perPx, -30, 30) * 1000) / 1000;
    const row = timeline.querySelector(`.audio-lane[data-track="${index}"]`).parentElement;
    row.querySelector(".shift-value").textContent = `${Math.round(track.offset * 1000)} ms`;
    drawWaveform(index);
  }, endEdit);
}

function nudge(index, ms) {
  change(() => {
    const track = trackOf(index);
    track.offset = Math.round(clamp(track.offset + ms / 1000, -30, 30) * 1000) / 1000;
  });
}

// --- Actions on the selection --------------------------------------------------

function removeSelection() {
  const sel = ed.selection;
  if (!sel) return;
  change(() => {
    ed.cuts.push([sel.start, sel.end]);
    ed.cuts = mergeRanges(ed.cuts);
    ed.selection = null;
  });
}

function trimToSelection() {
  const sel = ed.selection;
  if (!sel) return;
  change(() => {
    ed.trim = [sel.start, sel.end];
    ed.cuts = ed.cuts.filter(([a, b]) => b > sel.start && a < sel.end);
    ed.selection = null;
  });
  seek(ed.trim[0]);
}

function mergeRanges(ranges) {
  const sorted = ranges.map((r) => [...r]).sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const [a, b] of sorted) {
    if (merged.length && a <= merged[merged.length - 1][1]) {
      merged[merged.length - 1][1] = Math.max(merged[merged.length - 1][1], b);
    } else {
      merged.push([a, b]);
    }
  }
  return merged;
}

function activeRegion() {
  if (!ed.region) return null;
  const track = trackOf(ed.region.track);
  return (track && track.regions[ed.region.index]) || null;
}

// A volume slider for the selected range of a track: the first move makes
// the volume change, later ones adjust it.
function setRangeVolume(db) {
  beginEdit();
  let region = activeRegion();
  if (!region) {
    const sel = ed.selection;
    if (!sel || sel.track === null) return;
    const track = trackOf(sel.track);
    region = { start: sel.start, end: sel.end, db };
    track.regions.push(region);
    track.regions.sort((a, b) => a.start - b.start);
    ed.region = { track: sel.track, index: track.regions.indexOf(region) };
    ed.selection = null;
  }
  region.db = db;
  renderTimeline();
  scheduleSave();
}

function deleteRegion() {
  const region = activeRegion();
  if (!region) return;
  change(() => {
    const track = trackOf(ed.region.track);
    track.regions.splice(ed.region.index, 1);
    ed.region = null;
  });
}

function renderPanel() {
  const panel = $("panel");
  const sel = ed.selection;
  const region = activeRegion();
  panel.replaceChildren();
  panel.hidden = !ed.info || (!sel && !region);
  if (panel.hidden) return;

  if (sel && sel.track === null) {
    panel.append(
      el("span", null, `Selected ${formatTime(sel.start)} – ${formatTime(sel.end)} `),
      el("span", "muted", `(${(sel.end - sel.start).toFixed(1)} s)`),
    );
    const remove = el("button", "primary", "Remove section");
    remove.title = "Cut this part out (Delete)";
    remove.addEventListener("click", removeSelection);
    const keepOnly = el("button", null, "Keep only this");
    keepOnly.title = "Trim the clip to this part";
    keepOnly.addEventListener("click", trimToSelection);
    panel.append(remove, keepOnly, closeButton());
    return;
  }

  const start = region ? region.start : sel.start;
  const end = region ? region.end : sel.end;
  const index = region ? ed.region.track : sel.track;
  const db = region ? region.db : 0;
  panel.append(el("span", null, `${isMaster(index) ? "Mix" : trackTitle(index)} ${formatTime(start)} – ${formatTime(end)}`));
  const slider = el("input");
  slider.type = "range";
  slider.min = MIN_SLIDER_DB;
  slider.max = MAX_DB;
  slider.step = 0.5;
  slider.value = dbToSlider(db);
  slider.className = "wide";
  const value = el("span", "mono", formatDb(db));
  slider.addEventListener("input", () => {
    const newDb = sliderToDb(slider.value);
    value.textContent = formatDb(newDb);
    setRangeVolume(newDb);
  });
  slider.addEventListener("change", () => {
    endEdit();
  });
  const mute = el("button", null, "Mute section");
  mute.addEventListener("click", () => {
    setRangeVolume(MUTE_DB);
    endEdit();
  });
  panel.append(el("span", "muted", "Volume"), slider, value, mute);
  if (region) {
    const remove = el("button", null, "Remove change");
    remove.addEventListener("click", deleteRegion);
    panel.append(remove);
  }
  panel.append(closeButton());
}

function closeButton() {
  const close = el("button", null, "Done");
  close.addEventListener("click", () => {
    ed.selection = null;
    ed.region = null;
    renderTimeline();
    renderPanel();
  });
  return close;
}

// ---------------------------------------------------------------------------
// Playback
// ---------------------------------------------------------------------------

function seek(t) {
  player.currentTime = clamp(t, 0, duration());
  updatePlayheads();
}

function togglePlay() {
  if (!ed.info) return;
  if (player.paused) {
    const keep = keepRanges();
    const end = keep.length ? keep[keep.length - 1][1] : duration();
    if (player.currentTime >= end - 0.05 || player.currentTime < ed.trim[0]) player.currentTime = ed.trim[0];
    if (audio.ctx && audio.ctx.state === "suspended") audio.ctx.resume();
    player.play().catch((err) => setStatus(`Can't play: ${err.message}`, true));
  } else {
    player.pause();
  }
}

function updatePlayheads() {
  const t = player.currentTime || 0;
  for (const head of timeline.querySelectorAll(".playhead")) head.style.left = pct(t);
  $("time").textContent = `${formatTime(t)} / ${formatTime(duration())}`;
  $("play").innerHTML = player.paused ? "&#9654;" : "&#10074;&#10074;";
  $("play").setAttribute("aria-label", player.paused ? "Play" : "Pause");
}

// While playing: jump over what's cut, stop at the end.
function skipRemoved() {
  const t = player.currentTime;
  const keep = keepRanges();
  if (!keep.length) return;
  if (keep.some(([a, b]) => t >= a - 0.02 && t < b)) return;
  const next = keep.find(([a]) => a > t);
  if (next) {
    player.currentTime = next[0];
  } else {
    player.pause();
    player.currentTime = keep[keep.length - 1][1];
  }
}

function gainAt(track, t) {
  if (track.muted) return 0;
  let gain = dbToGain(track.volume_db);
  for (const region of track.regions) {
    if (t >= region.start && t < region.end) gain *= dbToGain(region.db);
  }
  return gain;
}

// Which tracks to hear: the export's mix sources, or just the soloed ones.
function audibleTracks() {
  const heard = new Set();
  if (ed.solo.size) {
    for (const index of ed.solo) {
      if (isMaster(index)) {
        heard.add(1);
        heard.add(2);
      } else {
        heard.add(index);
      }
    }
  } else if (ed.info.split) {
    heard.add(1);
    heard.add(2);
  } else if (ed.info.tracks.length) {
    heard.add(ed.info.tracks[0].index);
  }
  return heard;
}

function setupAudio(name) {
  teardownAudio();
  try {
    audio.ctx = audio.ctx || new AudioContext();
  } catch (err) {
    setStatus("This window can't play the tracks separately - preview is silent.", true);
    return;
  }
  if (!audio.out) setupOutput();
  audio.master = audio.ctx.createGain();
  audio.master.connect(audio.out);
  for (const track of ed.info.tracks) {
    if (isMaster(track.index)) continue; // heard through Mic + Desktop
    const media = new Audio();
    media.preload = "auto";
    media.src = `track/${track.index}/${encodeURIComponent(name)}`;
    const source = audio.ctx.createMediaElementSource(media);
    const gain = audio.ctx.createGain();
    gain.gain.value = 0;
    source.connect(gain);
    gain.connect(ed.info.split && (track.index === 1 || track.index === 2) ? audio.master : audio.out);
    audio.nodes.set(track.index, { media, source, gain });
  }
}

function teardownAudio() {
  for (const node of audio.nodes.values()) {
    node.media.pause();
    node.media.removeAttribute("src");
    node.media.load();
    node.source.disconnect();
    node.gain.disconnect();
  }
  audio.nodes.clear();
  if (audio.master) audio.master.disconnect();
  audio.master = null;
}

function syncAudio() {
  if (!audio.ctx || !ed.info) return;
  const t = player.currentTime;
  const playing = !player.paused && !player.seeking;
  const heard = audibleTracks();
  const now = audio.ctx.currentTime;
  if (audio.master) audio.master.gain.setTargetAtTime(gainAt(trackOf(0), t), now, 0.015);
  for (const [index, node] of audio.nodes) {
    const track = trackOf(index);
    const desired = t - track.offset;
    const inRange = desired >= 0 && (!isFinite(node.media.duration) || desired < node.media.duration);
    const on = playing && heard.has(index) && inRange;
    node.gain.gain.setTargetAtTime(on ? gainAt(track, t) : 0, now, 0.015);
    if (!on) {
      if (!node.media.paused) node.media.pause();
      continue;
    }
    if (node.media.paused) {
      node.media.currentTime = desired;
      node.media.play().catch(() => {});
    } else if (Math.abs(node.media.currentTime - desired) > SYNC_TOLERANCE) {
      node.media.currentTime = desired;
    }
  }
}

function frame() {
  if (ed.info) {
    if (!player.paused) skipRemoved();
    updatePlayheads();
    syncAudio();
    drawMeter();
  }
  requestAnimationFrame(frame);
}

// ---------------------------------------------------------------------------
// Level meter: the RMS of what the preview plays, after the track edits
// ---------------------------------------------------------------------------

const METER_FLOOR_DB = -60;
const METER_TICKS = [0, -6, -12, -18, -24, -36, -48, -60];
const METER_WARN_DB = -18; // yellow from here, red from METER_HOT_DB
const METER_HOT_DB = -6;
const METER_RELEASE_S = 0.3; // how fast a bar falls back
const METER_HOLD_S = 1.5; // how long the highest level stays marked
const meter = { canvas: $("meter"), channels: [], last: 0, colors: null };

// Everything the preview plays goes through one stereo bus, which the meter
// taps (mono tracks count on both sides, as they're heard).
function setupOutput() {
  audio.out = audio.ctx.createGain();
  audio.out.channelCount = 2;
  audio.out.channelCountMode = "explicit";
  audio.out.channelInterpretation = "speakers";
  audio.out.connect(audio.ctx.destination);
  const splitter = audio.ctx.createChannelSplitter(2);
  audio.out.connect(splitter);
  meter.channels = [0, 1].map((channel) => {
    const analyser = audio.ctx.createAnalyser();
    analyser.fftSize = 2048;
    splitter.connect(analyser, channel);
    return { analyser, samples: new Float32Array(analyser.fftSize), power: 0, hold: -Infinity, holdAt: 0 };
  });
}

function meterColors() {
  if (!meter.colors) {
    const css = getComputedStyle(document.documentElement);
    const color = (name) => css.getPropertyValue(name).trim();
    meter.colors = { ok: color("--boost"), warn: color("--warn"), hot: color("--cut") };
  }
  return meter.colors;
}

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { meter.colors = null; });

// RMS with an instant rise and a smooth fall, like a hardware meter.
function readLevels() {
  const now = performance.now() / 1000;
  const fall = Math.exp(-Math.min(0.1, now - (meter.last || now)) / METER_RELEASE_S);
  meter.last = now;
  return meter.channels.map((ch) => {
    ch.analyser.getFloatTimeDomainData(ch.samples);
    let sum = 0;
    for (const sample of ch.samples) sum += sample * sample;
    ch.power = Math.max(sum / ch.samples.length, ch.power * fall);
    const db = ch.power > 1e-10 ? 10 * Math.log10(ch.power) : -Infinity;
    if (db >= ch.hold || now - ch.holdAt > METER_HOLD_S) {
      ch.hold = db;
      ch.holdAt = now;
    }
    return { db, hold: ch.hold };
  });
}

function drawMeter() {
  const canvas = meter.canvas;
  if (canvas.hidden) return;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
  const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  const w = width / ratio;
  const top = 12;
  const bottom = height / ratio - 12;
  const yOf = (db) => top + (clamp(db, METER_FLOOR_DB, 0) / METER_FLOOR_DB) * (bottom - top);
  ctx.clearRect(0, 0, w, height / ratio);

  ctx.font = "10px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "rgba(255, 255, 255, 0.5)";
  for (const db of METER_TICKS) {
    ctx.fillText(String(db), 22, yOf(db));
    ctx.fillRect(24, Math.round(yOf(db)) - 0.5, 3, 1);
  }

  const { ok, warn, hot } = meterColors();
  const zones = [[METER_FLOOR_DB, METER_WARN_DB, ok], [METER_WARN_DB, METER_HOT_DB, warn], [METER_HOT_DB, 0, hot]];
  const levels = meter.channels.length ? readLevels() : [{ db: -Infinity, hold: -Infinity }, { db: -Infinity, hold: -Infinity }];
  levels.forEach(({ db, hold }, i) => {
    const x = 30 + i * 13;
    ctx.fillStyle = "rgba(255, 255, 255, 0.08)";
    ctx.fillRect(x, top, 10, bottom - top);
    for (const [from, to, color] of zones) {
      if (db <= from) break;
      ctx.fillStyle = color;
      ctx.fillRect(x, yOf(Math.min(db, to)), 10, yOf(from) - yOf(Math.min(db, to)));
    }
    if (hold > METER_FLOOR_DB) {
      ctx.fillStyle = (zones.find(([from, to]) => hold <= to) || zones[2])[2];
      ctx.fillRect(x, yOf(hold) - 1, 10, 2);
    }
  });
}

player.addEventListener("play", () => {
  if (state.hovering) stopHover(state.hovering.thumb);
});
player.addEventListener("click", togglePlay);
$("play").addEventListener("click", togglePlay);

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

function renderExportBar() {
  const missing = ed.missing && ed.missing.length;
  const running = $("export-progress").hidden === false;
  $("export-full").disabled = !!missing || running;
  $("export-discord").disabled = !!missing || running;
  $("export-custom").disabled = !!missing || running || !ed.custom;
  renderCustomExport();
  const note = $("discord-note");
  note.classList.remove("warn");
  if (missing) {
    note.textContent = "Update ClipEm to export edits (its ffmpeg is too old).";
    note.classList.add("warn");
    return;
  }
  const d = ed.discord;
  if (!d) {
    note.textContent = "";
    return;
  }
  $("export-discord").textContent = `Export for Discord (${d.target_mb} MB)`;
  // Only a warning - the button already says the size, so the bar fits one line.
  note.textContent = d.too_long ? `Too long for ${d.target_mb} MB - trim it for a sharper video.` : "";
  note.classList.toggle("warn", d.too_long);
}

// The custom export's dropdowns: the clip's options, keeping what was
// picked, with each bitrate's file size for the clip's current length.
function fillSelect(select, options, fallback) {
  const previous = select.value;
  select.replaceChildren(...options.map(([value, label]) => {
    const option = el("option", null, label);
    option.value = value;
    return option;
  }));
  const values = options.map(([value]) => value);
  select.value = values.includes(previous) ? previous : values.includes(fallback) ? fallback : values[0];
}

function formatMb(mb) {
  return mb < 10 ? mb.toFixed(1) : String(Math.round(mb));
}

function renderCustomExport() {
  const options = ed.custom;
  $("custom-export").hidden = !options;
  if (!options) return;
  const sourceHeight = ed.info && ed.info.height ? ` (${ed.info.height}p)` : "";
  const sourceFps = ed.info && ed.info.fps ? ` (${Math.round(ed.info.fps)})` : "";
  fillSelect($("custom-height"), [["", `Source${sourceHeight}`],
    ...options.heights.map((h) => [String(h), `${h}p`])], "1080");
  fillSelect($("custom-fps"), [["", `Source fps${sourceFps}`],
    ...options.fps.map((f) => [String(f), `${f} fps`])], "60");
  const seconds = keptLength();
  fillSelect($("custom-bitrate"), options.video_kbps.map((kbps) => {
    const mb = ((kbps + options.audio_kbps) * seconds) / 8 / 1000;
    return [String(kbps), `${kbps / 1000} Mbps · ~${formatMb(mb)} MB`];
  }), "8000");
}

function customSettings() {
  const height = $("custom-height").value;
  const fps = $("custom-fps").value;
  return {
    height: height ? Number(height) : null,
    fps: fps ? Number(fps) : null,
    video_kbps: Number($("custom-bitrate").value),
  };
}

async function startExport(preset) {
  if (!ed.name) return;
  setStatus("");
  try {
    await flushSave();
    const body = { name: ed.name, preset, out_name: $("out-name").value };
    if (preset === "custom") body.custom = customSettings();
    showExportProgress(await api("api/export", body));
    pollExport();
  } catch (err) {
    setStatus(err.message, true);
  }
}

let exportPoll = null;
let lastExportFile = null;

function showExportProgress(status) {
  const running = status.state === "running";
  $("export-progress").hidden = !running;
  $("export-progress").querySelector(".fill").style.width = `${Math.round((status.progress || 0) * 100)}%`;
  $("export-message").textContent = `${status.message || ""} ${Math.round((status.progress || 0) * 100)}%`;
  renderExportBar();
  return running;
}

async function pollExport() {
  clearTimeout(exportPoll);
  let status;
  try {
    status = await api("api/export-status");
  } catch (err) {
    exportPoll = setTimeout(pollExport, EXPORT_POLL_MS * 4);
    return;
  }
  if (showExportProgress(status)) {
    exportPoll = setTimeout(pollExport, EXPORT_POLL_MS);
    return;
  }
  if (status.state === "done") {
    lastExportFile = status.files[0] || null;
    await loadClips(); // first, so the export isn't announced as a new clip
    setStatus(`Saved ${status.files.join(" and ")} next to your clips.`);
  } else if (status.state === "error") {
    setStatus(`Export failed: ${status.message}`, true);
  } else if (status.state === "cancelled") {
    setStatus("Export cancelled.");
  }
}

$("export-full").addEventListener("click", () => startExport("full"));
$("export-discord").addEventListener("click", () => startExport("discord"));
$("export-custom").addEventListener("click", () => startExport("custom"));
$("out-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === "Escape") e.target.blur();
});
$("export-cancel").addEventListener("click", () => api("api/export-cancel", {}).catch(() => {}));

// ---------------------------------------------------------------------------
// Resizing the video and the tracks
// ---------------------------------------------------------------------------

const EDITOR_HEIGHT_KEY = "clipem.editorHeight";
const MIN_EDITOR_PX = 240;
const MIN_PLAYER_PX = 180;
let editorHeight = null; // px, or null: the default (CSS max-height)

function applyEditorHeight() {
  const editor = $("editor");
  if (editorHeight == null) {
    editor.style.height = "";
    editor.style.maxHeight = "";
    return;
  }
  // Whatever's above the editor besides the video keeps its room.
  const main = $("main").getBoundingClientRect().height;
  const fixed = ["toolbar", "panel", "splitter"].reduce((sum, id) => sum + $(id).getBoundingClientRect().height, 0);
  let max = main - fixed - MIN_PLAYER_PX;
  if (!editor.hidden) {
    // No taller than it takes to show every track: no empty space below them.
    // Measured from unrounded sizes (and rounded up), so the answer doesn't
    // depend on the height it's at now - e.g. on how fast the splitter moved.
    const scroll = $("timeline-scroll").getBoundingClientRect().height;
    const toggle = $("toggle-unused");
    const content = timeline.getBoundingClientRect().height + (toggle.hidden ? 0 : toggle.getBoundingClientRect().height);
    max = Math.min(max, Math.ceil(editor.getBoundingClientRect().height - scroll + content));
  }
  const min = Math.min(MIN_EDITOR_PX, max);
  const height = clamp(editorHeight, min, Math.max(min, max));
  editor.style.maxHeight = "none";
  editor.style.height = `${height}px`;
}

function saveEditorHeight() {
  try {
    if (editorHeight == null) localStorage.removeItem(EDITOR_HEIGHT_KEY);
    else localStorage.setItem(EDITOR_HEIGHT_KEY, String(Math.round(editorHeight)));
  } catch (err) { /* storage unavailable - just not remembered */ }
}

$("splitter").addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  e.preventDefault();
  const startY = e.clientY;
  const startHeight = $("editor").getBoundingClientRect().height;
  document.body.classList.add("resizing");
  trackPointer(e, (ev) => {
    editorHeight = startHeight + (startY - ev.clientY);
    applyEditorHeight();
  }, () => {
    document.body.classList.remove("resizing");
    editorHeight = $("editor").getBoundingClientRect().height; // as clamped
    saveEditorHeight();
  });
});

$("splitter").addEventListener("dblclick", () => {
  editorHeight = null;
  applyEditorHeight();
  saveEditorHeight();
});

try {
  const saved = Number(localStorage.getItem(EDITOR_HEIGHT_KEY));
  if (saved > 0) editorHeight = saved;
} catch (err) { /* storage unavailable */ }
window.addEventListener("resize", applyEditorHeight);
new ResizeObserver(applyEditorHeight).observe($("panel"));
applyEditorHeight();

// ---------------------------------------------------------------------------
// Other buttons and keys
// ---------------------------------------------------------------------------

function renderEditorButtons() {
  const editors = state.editors;
  const custom = $("edit-custom");
  custom.hidden = !(editors && editors.custom);
  if (editors && editors.custom) custom.textContent = `Open in ${editors.custom.label}`;
}

$("edit-custom").addEventListener("click", async () => {
  const clip = selectedClip();
  if (!clip) return;
  try {
    const result = await api("api/open", { name: clip.name });
    setStatus(`Opened ${result.file}.`);
  } catch (err) {
    setStatus(err.message, true);
  }
});

$("reveal").addEventListener("click", async () => {
  const name = lastExportFile || state.selected;
  if (!name) return;
  try {
    await api("api/reveal", { name });
  } catch (err) {
    setStatus(err.message, true);
  }
});

$("toggle-unused").addEventListener("click", () => {
  ed.showUnused = !ed.showUnused;
  renderTimeline();
});
$("undo").addEventListener("click", undo);
$("redo").addEventListener("click", redo);
$("reset").addEventListener("click", resetEdits);

// Space always plays/pauses - even with a button, slider, dropdown or clip
// focused - except while typing text. Captured first so nothing else sees it.
function spaceForPlayback(e) {
  if (e.key !== " " || e.ctrlKey || e.metaKey || e.altKey || !ed.info) return false;
  const t = e.target;
  if (t instanceof HTMLTextAreaElement || t.isContentEditable) return false;
  return !(t instanceof HTMLInputElement) || ["range", "checkbox", "radio", "button"].includes(t.type);
}

window.addEventListener("keydown", (e) => {
  if (!spaceForPlayback(e)) return;
  e.preventDefault();
  e.stopPropagation();
  if (!e.repeat) togglePlay();
}, { capture: true });

// A focused button would otherwise click on release.
window.addEventListener("keyup", (e) => {
  if (!spaceForPlayback(e)) return;
  e.preventDefault();
  e.stopPropagation();
}, { capture: true });

document.addEventListener("keydown", (e) => {
  if (!ed.info || e.defaultPrevented || typingIn(e.target)) return;
  const key = e.key.toLowerCase();
  if ((e.ctrlKey || e.metaKey) && key === "z") {
    e.preventDefault();
    if (e.shiftKey) redo();
    else undo();
  } else if ((e.ctrlKey || e.metaKey) && key === "y") {
    e.preventDefault();
    redo();
  } else if (e.ctrlKey || e.metaKey || e.altKey) {
    // leave everything else alone
  } else if (key === "i") {
    change(() => { ed.trim[0] = clamp(player.currentTime, 0, ed.trim[1] - MIN_SPAN); });
  } else if (key === "o") {
    change(() => { ed.trim[1] = clamp(player.currentTime, ed.trim[0] + MIN_SPAN, duration()); });
  } else if ((key === "delete" || key === "backspace") && ed.selection && ed.selection.track === null) {
    e.preventDefault();
    removeSelection();
  } else if ((key === "delete" || key === "backspace") && activeRegion()) {
    e.preventDefault();
    deleteRegion();
  } else if (key === "arrowleft" || key === "arrowright") {
    e.preventDefault();
    seek(player.currentTime + (key === "arrowleft" ? -1 : 1) * (e.shiftKey ? 5 : 1));
  } else if (key === "escape") {
    ed.selection = null;
    ed.region = null;
    renderTimeline();
    renderPanel();
  }
});

window.addEventListener("beforeunload", () => {
  if (ed.saveTimer && ed.name) {
    navigator.sendBeacon("api/project", new Blob([JSON.stringify({ name: ed.name, project: project() })],
      { type: "application/json" }));
  }
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

async function loadEditors() {
  try {
    state.editors = await api("api/editors");
  } catch (err) {
    /* no custom editor button */
  }
  renderEditorButtons();
}

loadEditors();
loadClips();
setInterval(loadClips, POLL_MS);
pollExport();
requestAnimationFrame(frame);
