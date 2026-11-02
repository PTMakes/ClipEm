"use strict";

// What the Full Editor adds to the editor (editor.js): tracks of imported
// video, pictures and sound over the clip, and fades. The project keeps them
// in "layers" (see clip_edit.py), which the export lays over the clip; the
// preview here does the same: pictures in #layers over the video, sound
// through the same output as the clip's tracks. Loaded before editor.js, so
// nothing here touches it until the page is running.

const MODE_KEY = "clipem.editorMode";
const IMAGE_SECONDS = 5; // how long a picture stays up when it's added
const MIN_ITEM_SECONDS = 0.05;
const MAX_IMAGE_SECONDS = 3600;
const MAX_LAYERS = 16;
const SNAP_PX = 6;
const FADE_ZERO_S = 0.03; // a fade dragged this short is no fade
const LAYER_TOLERANCE = 0.15;

// item id -> {node, sound: {media, source, gain}}, for what's been played or shown.
const layerPreview = new Map();

function round3(value) {
  return Math.round(value * 1000) / 1000;
}

function itemLength(item) {
  return item.out - item.in;
}

function itemEnd(item) {
  return item.start + itemLength(item);
}

function layersEnd() {
  let end = 0;
  for (const layer of ed.layers) for (const item of layer.items) end = Math.max(end, itemEnd(item));
  return end;
}

function assetOf(item) {
  return ed.library[item.asset];
}

// How the sound of an item fades: as clip_edit._sound_fades.
function soundFades(layer, item) {
  let fadeOut = item.fade_out;
  for (const other of layer.items) {
    if (other !== item && other.transition && other.start > item.start && other.start < itemEnd(item)) {
      fadeOut = Math.max(fadeOut, itemEnd(item) - other.start);
    }
  }
  return { fade_in: Math.max(item.fade_in, item.transition_s || 0), fade_out: Math.min(fadeOut, itemLength(item)) };
}

function hasSound(asset) {
  return !!asset && (asset.kind === "audio" || asset.has_audio);
}

function findItem(id) {
  for (const layer of ed.layers) {
    const item = layer.items.find((candidate) => candidate.id === id);
    if (item) return { layer, item };
  }
  return null;
}

function newId(prefix) {
  return `${prefix}${Date.now().toString(36)}${Math.floor(Math.random() * 1e6).toString(36)}`;
}

// The kept part follows the timeline when it ended at the timeline's end.
function followTimeline(before) {
  if (ed.trim[1] >= before - 0.001) ed.trim[1] = duration();
  ed.trim[1] = Math.min(ed.trim[1], duration());
}

// --- Trim and the Full Editor ------------------------------------------------

function loadMode(openIn) {
  if (openIn === "trim" || openIn === "full") return openIn === "full" ? "multi" : "trim";
  try {
    return localStorage.getItem(MODE_KEY) === "multi" ? "multi" : "trim";
  } catch (err) {
    return "trim"; // storage unavailable
  }
}

function setMode(mode) {
  ed.mode = mode;
  try {
    localStorage.setItem(MODE_KEY, mode);
  } catch (err) { /* just not remembered */ }
  ed.selection = null;
  ed.region = null;
  ed.item = null;
  ed.fx = null;
  ed.showAssets = false;
  // Back in Trim the recorded Mix is what's trimmed, whatever the Full Editor
  // picked: hand the choice back so syncSources() turns the Mix track on.
  const chosen = mode === "trim" && ed.sources === "chosen";
  if (chosen) ed.sources = "auto";
  // Trim has no solo buttons - a solo left on would play and export a track
  // that isn't even shown.
  if (mode === "trim") ed.solo.clear();
  if (syncSources() || chosen) scheduleSave();
  renderTimeline();
  renderPanel();
  renderExportBar();
}

// The Full Editor works on what the Mix is made of: Mix and Desktop start on
// their sub-tracks (Settings > Clip Editor can turn that off) until the user
// picks for themselves. Back in Trim, the recorded Mix is what's trimmed.
function syncSources() {
  if (!ed.info || !ed.info.split || ed.sources !== "auto") return false;
  const sub = ed.mode === "multi" && ed.editor.sub_tracks;
  const apps = ed.tracks.some((t) => t.index > DESKTOP && !isUnused(t.index)); // not just empty app slots
  const changed = trackOf(0).muted !== sub || trackOf(DESKTOP).muted !== (sub && apps);
  trackOf(0).muted = sub;
  trackOf(DESKTOP).muted = sub && apps;
  return changed;
}

function chooseSources(fn) {
  change(() => {
    ed.sources = "chosen";
    fn();
  });
}

function renderModeBar() {
  const multi = ed.mode === "multi";
  $("mode-trim").setAttribute("aria-pressed", String(!multi));
  $("mode-multi").setAttribute("aria-pressed", String(multi));
  $("layer-tools").hidden = !multi;
  $("show-assets").setAttribute("aria-pressed", String(ed.showAssets));
}

// In the blank space beside the playhead bar, above the tracks.
function addVideoButton() {
  const add = el("button", "add-track", "+ Add Video Track");
  add.disabled = !ed.canLayer;
  add.title = ed.canLayer ? "Add an empty video track on top" : "Needs a newer ffmpeg - update ClipEm";
  add.addEventListener("click", () => change(() => { newLayer("video"); }));
  return add;
}

// Below every other track.
function addAudioRow() {
  const row = el("div", "row add-track-row");
  const head = el("div", "row-head");
  const add = el("button", "add-track", "+ Add Audio Track");
  add.title = "Add an empty audio track";
  add.addEventListener("click", () => change(() => { newLayer("audio"); }));
  head.append(add);
  row.append(head, el("div"));
  return row;
}

// --- Adding ------------------------------------------------------------------

function newLayer(kind) {
  if (ed.layers.length >= MAX_LAYERS) {
    setStatus(`That's the most tracks a clip can have (${MAX_LAYERS}).`, true);
    return null;
  }
  const number = ed.layers.filter((layer) => layer.kind === kind).length + (kind === "video" ? 2 : 1);
  const layer = { id: newId("l"), kind, title: `${kind === "video" ? "Video" : "Audio"} ${number}`, muted: false,
    hidden: false, volume_db: 0, items: [] };
  ed.layers.push(layer);
  return layer;
}

function overlaps(layer, start, end) {
  return layer.items.some((item) => item.start < end && itemEnd(item) > start);
}

// Puts a file on the timeline at `at`: on the first free track of its kind, or a new one.
function placeAsset(asset, at) {
  const kind = asset.kind === "audio" ? "audio" : "video";
  if (kind === "video" && !ed.canLayer) {
    setStatus("Video tracks need a newer ffmpeg - update ClipEm.", true);
    return;
  }
  const length = asset.duration ?? IMAGE_SECONDS;
  change(() => {
    const before = duration();
    const layer = ed.layers.find((l) => l.kind === kind && !overlaps(l, at, at + length)) || newLayer(kind);
    if (!layer) return;
    const item = { id: newId("i"), asset: asset.id, start: round3(at), in: 0, out: round3(length), fade_in: 0,
      fade_out: 0, transition: null, transition_s: 0, volume_db: 0, x: 0.5, y: 0.5, scale: 1, opacity: 1 };
    layer.items.push(item);
    ed.item = item.id;
    ed.selection = null;
    ed.region = null;
    ed.fx = null;
    ed.showAssets = false;
    followTimeline(before);
  });
}

async function importFiles(files, at) {
  const name = ed.name;
  let time = at;
  for (const file of files) {
    setStatus(`Importing ${file.name}...`);
    try {
      const res = await fetch(`api/asset?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
      ed.library[data.id] = data;
      if (ed.name !== name) return;
      placeAsset(data, time);
      time += data.duration ?? IMAGE_SECONDS;
      setStatus("");
    } catch (err) {
      setStatus(`Couldn't import ${file.name}: ${err.message}`, true);
      return;
    }
  }
}

$("mode-trim").addEventListener("click", () => setMode("trim"));
$("mode-multi").addEventListener("click", () => setMode("multi"));
$("import").addEventListener("click", () => $("import-file").click());
$("import-file").addEventListener("change", (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  importFiles(files, position());
});
$("show-assets").addEventListener("click", () => {
  ed.showAssets = !ed.showAssets;
  ed.item = null;
  ed.selection = null;
  ed.region = null;
  ed.fx = null;
  renderModeBar();
  renderPanel();
});

// Files dropped on the tracks go where they're dropped.
$("editor").addEventListener("dragover", (e) => {
  if (ed.info && e.dataTransfer.types.includes("Files")) e.preventDefault();
});
$("editor").addEventListener("drop", (e) => {
  if (!ed.info || !e.dataTransfer.files.length) return;
  e.preventDefault();
  if (ed.mode !== "multi") setMode("multi");
  importFiles([...e.dataTransfer.files], laneTime(e.clientX));
});

// --- Moving the clip -------------------------------------------------------------

// The clip at `to` on the timeline, from where it was at `base`: its cuts and
// volume changes go with it, and so does the trim, unless it's what's being
// made room in front of. A trimmed start then has to become a cut, or the part
// trimmed off would show up again.
function moveClipState() {
  return { start: ed.clip_start, trim: [...ed.trim], cuts: ed.cuts.map((c) => [...c]), length: duration(),
    regions: ed.tracks.map((t) => t.regions.map((r) => [r.start, r.end])) };
}

function moveClip(base, to) {
  const shift = to - base.start;
  ed.clip_start = to;
  ed.cuts = base.cuts.map(([a, b]) => [a + shift, b + shift]);
  ed.tracks.forEach((track, t) => track.regions.forEach((region, r) => {
    [region.start, region.end] = base.regions[t][r].map((v) => round3(v + shift));
  }));
  const firstItem = Math.min(...ed.layers.flatMap((l) => l.items.map((item) => item.start)));
  const moved = base.trim[0] + shift;
  if (base.trim[0] <= base.start) {
    ed.trim[0] = Math.min(base.trim[0], firstItem, to);
  } else {
    ed.trim[0] = Math.min(moved, firstItem);
    if (ed.trim[0] < moved) ed.cuts = mergeRanges([...ed.cuts, [to, moved]]);
  }
  ed.trim[1] = base.trim[1] >= base.length - 0.001 ? duration() : Math.min(base.trim[1] + shift, duration());
}

function clipBar() {
  const bar = el("div", "clip-bar", "Clip");
  placeSpan(bar, ed.clip_start, clipEnd());
  bar.title = "Drag to move the clip along the timeline - leave room before it for an intro";
  bar.addEventListener("pointerdown", dragClip);
  return bar;
}

function dragClip(e) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  const startX = e.clientX;
  const perPx = secondsPerPixel();
  const base = moveClipState();
  beginEdit();
  trackPointer(e, (ev) => {
    const wanted = Math.max(0, base.start + (ev.clientX - startX) * perPx);
    const end = snapToEdges(wanted + clipLength(), null, perPx) - clipLength();
    const start = snapToEdges(wanted, null, perPx);
    moveClip(base, round3(Math.max(0, start !== wanted ? start : end)));
    renderTimeline();
  }, finishItemDrag);
}

function clipStartLine() {
  const line = el("label", "shift-line");
  const input = el("input");
  input.type = "number";
  input.min = 0;
  input.step = 0.1;
  input.value = ed.clip_start;
  input.title = "Where the clip starts on the timeline";
  input.addEventListener("change", () => {
    const seconds = Number(input.value);
    if (Number.isFinite(seconds) && seconds >= 0) {
      const base = moveClipState();
      change(() => moveClip(base, round3(seconds)));
    }
  });
  line.append(el("span", "small muted", "Starts"), input, el("span", "small muted", "s"));
  return line;
}

// --- Transitions -----------------------------------------------------------------

const TRANSITION_NAMES = { crossfade: "Crossfade", "slide-left": "Slide from right", "slide-right": "Slide from left",
  "slide-up": "Slide from bottom", "slide-down": "Slide from top" };
const TRANSITION_SECONDS = 1;
const TOUCHING_S = 0.05;

// A transition plays over what's underneath, so an item right after another on
// its track is pulled back over it by the transition's length, the items
// after it following - and let go again when it's made shorter or taken off.
function setTransition(layer, item, kind, seconds) {
  const before = duration();
  const old = item.transition ? item.transition_s : 0;
  const length = kind ? Math.min(seconds, itemLength(item)) : 0;
  const previous = layer.items.filter((other) => other.start < item.start)
    .sort((a, b) => itemEnd(b) - itemEnd(a))[0];
  const touching = previous && Math.abs(itemEnd(previous) - item.start - old) <= TOUCHING_S;
  item.transition = kind;
  item.transition_s = length;
  if (touching) {
    const shift = Math.min(length, itemLength(previous)) - old;
    for (const other of layer.items) {
      if (other.start >= item.start && other !== item) other.start = round3(other.start - shift);
    }
    item.start = round3(item.start - shift);
  }
  followTimeline(before);
}

// --- Fades -------------------------------------------------------------------

// 0-1 at t, for something with fade_in and fade_out running from start to end.
function fadeLevel(owner, t, start, end) {
  let level = 1;
  if (owner.fade_in > 0 && t < start + owner.fade_in) level = Math.max(0, (t - start) / owner.fade_in);
  if (owner.fade_out > 0 && t > end - owner.fade_out) level = Math.min(level, Math.max(0, (end - t) / owner.fade_out));
  return level;
}

function fadeShade(lane, start, end, side) {
  const shade = el("div", `fade-shade ${side}`);
  placeSpan(shade, start, end);
  lane.append(shade);
}

// Fades of a whole lane (a recorded track, or the picture): a dot at each end
// of what's kept, dragged inwards - owner is the track, or ed.
function addLaneFades(lane, owner) {
  const [start, end] = ed.trim;
  if (owner.fade_in) fadeShade(lane, start, start + owner.fade_in, "in");
  if (owner.fade_out) fadeShade(lane, end - owner.fade_out, end, "out");
  for (const key of ["fade_in", "fade_out"]) {
    const dot = el("div", `fade-dot ${key === "fade_in" ? "in" : "out"}`);
    dot.style.left = pct(key === "fade_in" ? start + owner.fade_in : end - owner.fade_out);
    dot.title = `Drag to fade ${key === "fade_in" ? "in" : "out"}`
      + (owner[key] ? ` (${owner[key].toFixed(2)} s, double-click: none)` : "");
    dot.addEventListener("pointerdown", (e) => dragLaneFade(e, owner, key));
    dot.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      change(() => { owner[key] = 0; });
    });
    lane.append(dot);
  }
}

function dragLaneFade(e, owner, key) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  const [start, end] = ed.trim;
  beginEdit();
  trackPointer(e, (ev) => {
    const t = laneTime(ev.clientX);
    const other = key === "fade_in" ? owner.fade_out : owner.fade_in;
    const length = clamp(key === "fade_in" ? t - start : end - t, 0, end - start - other);
    owner[key] = length < FADE_ZERO_S ? 0 : round3(length);
    renderTimeline();
  }, endEdit);
}

// --- Added tracks on the timeline ----------------------------------------------

function layerRow(layer) {
  const row = el("div", "row layer-row");
  row.classList.toggle("muted-track", layer.muted && (layer.kind === "audio" || layer.hidden));
  const head = el("div", "row-head");
  const titleLine = el("div", "title-line");
  const title = el("input", "layer-title");
  title.value = layer.title;
  title.spellcheck = false;
  title.setAttribute("aria-label", "Track name");
  title.title = "Track name - its name in the .mkv export too";
  title.addEventListener("change", () => change(() => { layer.title = title.value.trim() || layer.title; }));
  title.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === "Escape") title.blur();
  });
  const mute = el("button", "toggle", "M");
  mute.title = "Mute this track's sound";
  mute.setAttribute("aria-pressed", String(layer.muted));
  mute.addEventListener("click", () => change(() => { layer.muted = !layer.muted; }));
  titleLine.append(title, mute);
  if (layer.kind === "video") {
    const hide = el("button", "toggle", "H");
    hide.title = "Hide this track's picture";
    hide.setAttribute("aria-pressed", String(layer.hidden));
    hide.addEventListener("click", () => change(() => { layer.hidden = !layer.hidden; }));
    titleLine.append(hide);
  }

  const volume = el("label", "slider-line");
  const slider = el("input");
  slider.type = "range";
  slider.min = MIN_SLIDER_DB;
  slider.max = MAX_DB;
  slider.step = 0.5;
  slider.value = dbToSlider(layer.volume_db);
  const value = el("span", "small mono", formatDb(layer.volume_db));
  slider.addEventListener("input", () => {
    beginEdit();
    layer.volume_db = sliderToDb(slider.value);
    value.textContent = formatDb(layer.volume_db);
    scheduleSave();
  });
  slider.addEventListener("change", endEdit);
  resetOnDoubleClick(slider, 0);
  slider.title = "Volume of the whole track (double-click: 0 dB)";
  volume.append(el("span", "small muted", "Vol"), slider, value);
  const remove = el("button", "remove-track", "Remove Track");
  remove.addEventListener("click", () => change(() => {
    const before = duration();
    if (ed.item && layer.items.some((item) => item.id === ed.item)) ed.item = null;
    ed.layers = ed.layers.filter((other) => other !== layer);
    followTimeline(before);
  }));
  head.append(titleLine, volume, remove);

  const lane = el("div", `lane layer-lane ${layer.kind}`);
  lane.dataset.layer = layer.id;
  addRemoved(lane, false);
  for (const item of layer.items) lane.append(itemNode(layer, item));
  lane.append(el("div", "playhead"));
  lane.addEventListener("pointerdown", layerLaneDown);
  row.append(head, lane);
  return row;
}

function layerLaneDown(e) {
  if (e.button !== 0) return;
  e.preventDefault();
  ed.selection = null;
  ed.region = null;
  ed.item = null;
  seek(laneTime(e.clientX));
  renderTimeline();
  renderPanel();
}

function itemNode(layer, item) {
  const asset = assetOf(item);
  const node = el("div", "item");
  node.classList.toggle("active", ed.item === item.id);
  node.dataset.item = item.id;
  placeSpan(node, item.start, itemEnd(item));
  node.title = asset ? asset.name : item.asset;
  const length = itemLength(item);
  if (hasSound(asset)) node.append(el("canvas", "wave"));
  if (item.fade_in) {
    const shade = el("div", "fade-shade in");
    shade.style.width = `${(item.fade_in / length) * 100}%`;
    node.append(shade);
  }
  if (item.transition) {
    const mark = el("div", "transition-mark");
    mark.style.width = `${(item.transition_s / length) * 100}%`;
    mark.title = TRANSITION_NAMES[item.transition];
    node.append(mark);
  }
  if (item.fade_out) {
    const shade = el("div", "fade-shade out");
    shade.style.right = "0";
    shade.style.width = `${(item.fade_out / length) * 100}%`;
    node.append(shade);
  }
  node.append(el("span", "item-name", asset ? asset.name : "Missing file"));
  for (const side of ["in", "out"]) {
    const edge = el("div", `item-edge ${side}`);
    edge.title = "Drag to change where it starts or ends";
    edge.addEventListener("pointerdown", (e) => dragItemEdge(e, item, side));
    node.append(edge);
    const dot = el("div", `fade-dot ${side}`);
    const key = `fade_${side}`;
    dot.style.left = `${((side === "in" ? item.fade_in : length - item.fade_out) / length) * 100}%`;
    dot.title = `Drag to fade ${side}`;
    dot.addEventListener("pointerdown", (e) => dragItemFade(e, item, key));
    node.append(dot);
  }
  node.addEventListener("pointerdown", (e) => dragItem(e, layer, item));
  return node;
}

function selectItem(id) {
  ed.item = id;
  ed.selection = null;
  ed.region = null;
  ed.fx = null;
  ed.showAssets = false;
  renderModeBar();
  renderPanel();
}

// Where an edge should land: on the playhead or another item's edge, when it's
// that close. skip: the item being dragged - null for the clip, which only
// snaps to items: the playhead and trim handles move with it.
function snapToEdges(t, skip, perPx) {
  const targets = skip === null ? [0] : [position(), ed.trim[0], ed.trim[1]];
  for (const layer of ed.layers) {
    for (const item of layer.items) {
      if (item !== skip) targets.push(item.start, itemEnd(item));
    }
  }
  let best = t;
  let distance = SNAP_PX * perPx;
  for (const target of targets) {
    if (Math.abs(target - t) <= distance) {
      best = target;
      distance = Math.abs(target - t);
    }
  }
  return best;
}

// A press that moved nothing is no edit, but it did select the item.
function finishItemDrag() {
  endEdit();
  renderTimeline();
}

function dragItem(e, layer, item) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  selectItem(item.id);
  const startX = e.clientX;
  const startTime = item.start;
  const perPx = secondsPerPixel();
  const length = itemLength(item);
  let home = layer;
  let moved = false;
  beginEdit();
  trackPointer(e, (ev) => {
    if (!moved && Math.abs(ev.clientX - startX) < DRAG_THRESHOLD_PX) return;
    moved = true;
    const before = duration();
    const start = Math.max(0, startTime + (ev.clientX - startX) * perPx);
    const byEnd = snapToEdges(start + length, item, perPx) - length;
    const byStart = snapToEdges(start, item, perPx);
    item.start = round3(Math.max(0, byStart !== start ? byStart : byEnd));
    const under = document.elementFromPoint(ev.clientX, ev.clientY);
    const target = under && under.closest(".layer-lane");
    const other = target && ed.layers.find((l) => l.id === target.dataset.layer);
    if (other && other !== home && other.kind === home.kind) {
      home.items = home.items.filter((candidate) => candidate !== item);
      other.items.push(item);
      home = other;
    }
    followTimeline(before);
    renderTimeline();
  }, finishItemDrag);
}

function dragItemEdge(e, item, side) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  selectItem(item.id);
  const asset = assetOf(item);
  const start = { ...item };
  const startX = e.clientX;
  const perPx = secondsPerPixel();
  const longest = asset && asset.duration ? asset.duration : MAX_IMAGE_SECONDS;
  const picture = asset && asset.kind === "image";
  beginEdit();
  trackPointer(e, (ev) => {
    const before = duration();
    const moved = (ev.clientX - startX) * perPx;
    if (side === "in") {
      const edge = snapToEdges(start.start + moved, item, perPx);
      // A picture has no part to choose: its left edge just moves.
      const earliest = picture ? -start.start : -Math.min(start.start, start.in);
      const shift = clamp(edge - start.start, earliest, start.out - start.in - MIN_ITEM_SECONDS);
      item.start = round3(start.start + shift);
      if (picture) item.out = round3(start.out - shift);
      else item.in = round3(start.in + shift);
    } else {
      const edge = snapToEdges(start.start + start.out - start.in + moved, item, perPx);
      item.out = round3(clamp(edge - start.start + start.in, start.in + MIN_ITEM_SECONDS, longest));
    }
    item.fade_in = Math.min(item.fade_in, itemLength(item));
    item.fade_out = Math.min(item.fade_out, itemLength(item) - item.fade_in);
    followTimeline(before);
    renderTimeline();
  }, finishItemDrag);
}

function dragItemFade(e, item, key) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  selectItem(item.id);
  const startX = e.clientX;
  const startValue = item[key];
  const perPx = secondsPerPixel();
  beginEdit();
  trackPointer(e, (ev) => {
    const moved = (ev.clientX - startX) * perPx * (key === "fade_in" ? 1 : -1);
    const room = itemLength(item) - item[key === "fade_in" ? "fade_out" : "fade_in"];
    const length = clamp(startValue + moved, 0, room);
    item[key] = length < FADE_ZERO_S ? 0 : round3(length);
    renderTimeline();
    renderPanel();
  }, finishItemDrag);
}

function deleteItem(id) {
  change(() => {
    const found = findItem(id);
    if (!found) return;
    const before = duration();
    found.layer.items = found.layer.items.filter((item) => item !== found.item);
    ed.item = null;
    followTimeline(before);
  });
}

// Two items, cut where the playhead is.
function splitItem(id) {
  const found = findItem(id);
  const t = position();
  if (!found || t <= found.item.start + MIN_ITEM_SECONDS || t >= itemEnd(found.item) - MIN_ITEM_SECONDS) {
    setStatus("Move the playhead onto the item to split it there.", true);
    return;
  }
  change(() => {
    const { layer, item } = found;
    const at = round3(item.in + (t - item.start));
    const asset = assetOf(item);
    const picture = !!asset && asset.kind === "image";
    const later = { ...item, id: newId("i"), start: round3(t), in: picture ? 0 : at,
      out: picture ? round3(item.out - at) : item.out, fade_in: 0, transition: null, transition_s: 0 };
    item.out = at;
    item.fade_out = 0;
    item.fade_in = Math.min(item.fade_in, itemLength(item));
    later.fade_out = Math.min(later.fade_out, itemLength(later));
    layer.items.push(later);
    ed.item = later.id;
  });
}

async function loadItemPeaks(asset) {
  if (ed.itemPeaks[asset.id] !== undefined) return;
  ed.itemPeaks[asset.id] = null;
  try {
    ed.itemPeaks[asset.id] = await api(`asset-peaks/${encodeURIComponent(asset.id)}`);
    drawItemWaves();
  } catch (err) {
    /* no waveform - the item still works */
  }
}

function drawItemWaves() {
  for (const canvas of timeline.querySelectorAll(".item canvas.wave")) {
    const found = findItem(canvas.parentElement.dataset.item);
    if (!found) continue;
    const asset = assetOf(found.item);
    if (!asset) continue;
    const data = ed.itemPeaks[asset.id];
    if (data === undefined) loadItemPeaks(asset);
    if (!data) continue;
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
    const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--wave").trim() || "#6c9bc7";
    const perPixel = itemLength(found.item) / width;
    for (let x = 0; x < width; x++) {
      const from = Math.floor((found.item.in + x * perPixel) * data.per_second);
      const to = Math.max(from + 1, Math.floor((found.item.in + (x + 1) * perPixel) * data.per_second));
      let peak = 0;
      for (let i = from; i < Math.min(data.peaks.length, to); i++) peak = Math.max(peak, data.peaks[i]);
      const level = peak / (data.scale || 255);
      if (level <= 0) continue;
      const h = clamp(1 + (20 * Math.log10(level)) / WAVE_RANGE_DB, 0, 1) * height;
      if (h >= 0.5) ctx.fillRect(x, (height - h) / 2, 1, h);
    }
  }
}

// --- The panel -----------------------------------------------------------------

function rangeField(label, value, min, max, step, format, set, fallback) {
  const wrap = el("label", "item-field");
  const slider = el("input");
  slider.type = "range";
  slider.min = min;
  slider.max = max;
  slider.step = step;
  slider.value = value;
  const reading = el("span", "small mono", format(value));
  slider.addEventListener("input", () => {
    beginEdit();
    set(Number(slider.value));
    reading.textContent = format(Number(slider.value));
    scheduleSave();
  });
  slider.addEventListener("change", endEdit);
  slider.addEventListener("dblclick", () => {
    slider.value = fallback;
    slider.dispatchEvent(new Event("input", { bubbles: true }));
    slider.dispatchEvent(new Event("change", { bubbles: true }));
  });
  slider.title = "Double-click to reset";
  wrap.append(el("span", "muted", label), slider, reading);
  return wrap;
}

function secondsField(label, value, max, set) {
  const wrap = el("label", "item-field");
  const input = el("input");
  input.type = "number";
  input.min = 0;
  input.max = max;
  input.step = 0.1;
  input.value = value;
  input.addEventListener("change", () => {
    const seconds = Number(input.value);
    if (Number.isFinite(seconds)) change(() => set(round3(clamp(seconds, 0, max))));
  });
  wrap.append(el("span", "muted", label), input, el("span", "small muted", "s"));
  return wrap;
}

function transitionFields(layer, item) {
  const select = el("select");
  select.title = "How it comes in, over what's underneath";
  for (const [value, label] of [["", "No transition"], ...Object.entries(TRANSITION_NAMES)]) {
    const option = el("option", null, label);
    option.value = value;
    select.append(option);
  }
  select.value = item.transition || "";
  select.addEventListener("change", () => change(() => {
    setTransition(layer, item, select.value || null, item.transition_s || TRANSITION_SECONDS);
  }));
  const fields = [select];
  if (item.transition) {
    fields.push(secondsField("for", item.transition_s, itemLength(item), (v) => {
      setTransition(layer, item, item.transition, Math.max(v, MIN_ITEM_SECONDS));
    }));
  }
  return fields;
}

function renderItemPanel(panel, found) {
  const { item, layer } = found;
  const asset = assetOf(item);
  const percent = (value) => `${Math.round(value * 100)}%`;
  panel.append(el("strong", null, asset ? asset.name : "Missing file"));
  panel.append(el("span", "muted", `${formatTime(item.start)} – ${formatTime(itemEnd(item))}`));
  panel.append(
    secondsField("Fade in", item.fade_in, itemLength(item) - item.fade_out, (v) => { item.fade_in = v; }),
    secondsField("Fade out", item.fade_out, itemLength(item) - item.fade_in, (v) => { item.fade_out = v; }),
    ...transitionFields(layer, item),
  );
  if (hasSound(asset)) {
    panel.append(rangeField("Volume", dbToSlider(item.volume_db), MIN_SLIDER_DB, MAX_DB, 0.5,
      (v) => formatDb(sliderToDb(v)), (v) => { item.volume_db = sliderToDb(v); }, 0));
  }
  if (asset && asset.kind !== "audio") {
    panel.append(
      rangeField("Size", item.scale, 0.05, 2, 0.01, percent, (v) => { item.scale = v; }, 1),
      rangeField("Left–right", item.x, -0.5, 1.5, 0.005, percent, (v) => { item.x = v; }, 0.5),
      rangeField("Up–down", item.y, -0.5, 1.5, 0.005, percent, (v) => { item.y = v; }, 0.5),
      rangeField("Opacity", item.opacity, 0, 1, 0.01, percent, (v) => { item.opacity = v; }, 1),
    );
  }
  const split = el("button", null, "Split");
  split.title = "Cut it in two at the playhead";
  split.addEventListener("click", () => splitItem(item.id));
  const remove = el("button", null, "Delete");
  remove.title = "Delete this item (Delete)";
  remove.addEventListener("click", () => deleteItem(item.id));
  panel.append(split, remove, closeButton());
}

function assetLabel(asset) {
  const length = asset.duration ? ` · ${formatDuration(asset.duration)}` : "";
  return `${asset.name} (${{ video: "video", audio: "sound", image: "picture" }[asset.kind]}${length})`;
}

function renderAssetsPanel(panel) {
  const assets = Object.values(ed.library).sort((a, b) => a.name.localeCompare(b.name));
  panel.append(el("strong", null, "Assets"));
  if (!assets.length) panel.append(el("span", "muted", "Nothing imported yet."));
  for (const asset of assets) {
    const row = el("span", "asset-row");
    const add = el("button", "asset", assetLabel(asset));
    add.title = "Add it at the playhead";
    add.addEventListener("click", () => placeAsset(asset, position()));
    const remove = el("button", "toggle", "×");
    remove.title = "Delete it from ClipEm - only if no clip's edit uses it";
    remove.addEventListener("click", () => deleteAsset(asset));
    row.append(add, remove);
    panel.append(row);
  }
  if (assets.length) {
    const size = assets.reduce((sum, asset) => sum + (asset.size || 0), 0);
    panel.append(el("span", "muted", formatSize(size)));
    const clean = el("button", null, "Delete unused");
    clean.title = "Delete every file no clip's edit uses (they also go by themselves after 30 days)";
    clean.addEventListener("click", cleanAssets);
    panel.append(clean);
  }
  panel.append(closeButton());
}

async function refreshLibrary() {
  const data = await api("api/assets");
  ed.library = Object.fromEntries(data.assets.map((asset) => [asset.id, asset]));
  renderPanel();
}

// The edit on screen first: what's on the timeline now is what counts as used.
async function deleteAsset(asset) {
  try {
    await flushSave();
    await api("api/asset-delete", { id: asset.id });
    await refreshLibrary();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function cleanAssets() {
  try {
    await flushSave();
    const { removed } = await api("api/assets-clean", {});
    await refreshLibrary();
    setStatus(`Deleted ${removed} unused file${removed === 1 ? "" : "s"}.`);
  } catch (err) {
    setStatus(err.message, true);
  }
}

// --- Preview -------------------------------------------------------------------

function itemPicture(asset) {
  const node = el(asset.kind === "image" ? "img" : "video", "layer-item");
  node.src = `asset/${encodeURIComponent(asset.id)}`;
  if (asset.kind === "video") {
    node.muted = true;
    node.preload = "auto";
    node.playsInline = true;
  }
  node.draggable = false;
  node.hidden = true;
  return node;
}

function itemSound(asset) {
  if (!audio.ctx || !audio.out) return null;
  const media = new Audio();
  media.preload = "auto";
  media.src = `asset/${encodeURIComponent(asset.id)}`;
  const source = audio.ctx.createMediaElementSource(media);
  const gain = audio.ctx.createGain();
  gain.gain.value = 0;
  source.connect(gain);
  gain.connect(audio.out);
  return { media, source, gain };
}

function dropPreview(entry) {
  if (entry.node) {
    if (entry.node.pause) entry.node.pause();
    entry.node.remove();
  }
  if (entry.sound) {
    entry.sound.media.pause();
    entry.sound.media.removeAttribute("src");
    entry.sound.source.disconnect();
    entry.sound.gain.disconnect();
  }
}

function teardownLayers() {
  for (const entry of layerPreview.values()) dropPreview(entry);
  layerPreview.clear();
}

// Keeps a media element on `wanted` seconds, playing or stopped there.
function steer(media, wanted, on) {
  if (on) {
    if (media.paused) {
      media.currentTime = wanted;
      media.play().catch(() => {});
    } else if (Math.abs(media.currentTime - wanted) > LAYER_TOLERANCE) {
      media.currentTime = wanted;
    }
  } else if (!media.paused) {
    media.pause();
  }
}

// A picture's box as fractions of the frame, sliding in if that's its
// transition - clip_edit.item_box and _item_position.
function pictureBox(item, asset, t) {
  const [width, height] = [ed.info.width, ed.info.height];
  const fit = Math.min(width / asset.width, height / asset.height) * item.scale;
  const w = (asset.width * fit) / width;
  const h = (asset.height * fit) / height;
  let left = item.x - w / 2;
  let top = item.y - h / 2;
  if (item.transition && item.transition.startsWith("slide") && t < item.start + item.transition_s) {
    const rest = 1 - clamp((t - item.start) / item.transition_s, 0, 1);
    if (item.transition === "slide-left") left += (1 - left) * rest;
    if (item.transition === "slide-right") left += (-w - left) * rest;
    if (item.transition === "slide-up") top += (1 - top) * rest;
    if (item.transition === "slide-down") top += (-h - top) * rest;
  }
  return { left, top, w, h };
}

function placeBox(node, box) {
  node.style.left = `${box.left * 100}%`;
  node.style.top = `${box.top * 100}%`;
  node.style.width = `${box.w * 100}%`;
  node.style.height = `${box.h * 100}%`;
}

function transitionLevel(item, t) {
  return item.transition === "crossfade" ? clamp((t - item.start) / item.transition_s, 0, 1) : 1;
}

// --- Moving pictures on the video ------------------------------------------------

// The picture showing at this point of the stage, top first.
function pictureAt(clientX, clientY) {
  const rect = $("stage").getBoundingClientRect();
  const x = (clientX - rect.left) / rect.width;
  const y = (clientY - rect.top) / rect.height;
  const t = position();
  for (const layer of [...ed.layers].reverse()) {
    if (layer.kind !== "video" || layer.hidden) continue;
    for (const item of [...layer.items].reverse()) {
      const asset = assetOf(item);
      if (!asset || t < item.start || t >= itemEnd(item)) continue;
      const box = pictureBox(item, asset, t);
      if (x >= box.left && x <= box.left + box.w && y >= box.top && y <= box.top + box.h) return item;
    }
  }
  return null;
}

const CENTRE_SNAP = 0.012; // of the frame

function dragPicture(e, item) {
  const rect = $("stage").getBoundingClientRect();
  const start = { x: item.x, y: item.y, cx: e.clientX, cy: e.clientY };
  beginEdit();
  trackPointer(e, (ev) => {
    const snap = (v) => (Math.abs(v - 0.5) < CENTRE_SNAP ? 0.5 : round3(v));
    item.x = snap(clamp(start.x + (ev.clientX - start.cx) / rect.width, -1, 2));
    item.y = snap(clamp(start.y + (ev.clientY - start.cy) / rect.height, -1, 2));
  }, finishPictureDrag);
}

function dragPictureCorner(e, item) {
  const rect = $("stage").getBoundingClientRect();
  const centre = [rect.left + item.x * rect.width, rect.top + item.y * rect.height];
  const from = Math.hypot(e.clientX - centre[0], e.clientY - centre[1]) || 1;
  const startScale = item.scale;
  beginEdit();
  trackPointer(e, (ev) => {
    const to = Math.hypot(ev.clientX - centre[0], ev.clientY - centre[1]);
    item.scale = Math.round(clamp((startScale * to) / from, 0.05, 4) * 1000) / 1000;
  }, finishPictureDrag);
}

function finishPictureDrag() {
  endEdit();
  renderPanel();
}

// Before the crop and the video see it: a press on a picture is for the picture.
$("stage").addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || !ed.info || ed.mode !== "multi" || ed.cropping) return;
  const corner = e.target instanceof Element && e.target.closest(".item-corner");
  const found = corner ? findItem(ed.item) : null;
  const item = found ? found.item : pictureAt(e.clientX, e.clientY);
  if (!item) return;
  e.preventDefault();
  e.stopPropagation();
  if (ed.item !== item.id) {
    selectItem(item.id);
    renderTimeline();
  }
  if (corner) dragPictureCorner(e, item);
  else dragPicture(e, item);
}, true);

// Called each frame: everything added, as it is at t.
function syncLayers(t, playing) {
  const overlay = $("layers");
  $("stage").classList.toggle("past-clip", outsideClip(t));
  $("picture-fade").style.opacity = String(1 - fadeLevel(ed, t, ed.trim[0], ed.trim[1]));
  const seen = new Set();
  const box = $("item-box");
  const selected = ed.item && ed.mode === "multi" ? findItem(ed.item) : null;
  const framed = selected && assetOf(selected.item);
  box.hidden = !(framed && framed.kind !== "audio" && !selected.layer.hidden && !ed.cropping
    && t >= selected.item.start && t < itemEnd(selected.item));
  if (!box.hidden) placeBox(box, pictureBox(selected.item, framed, t));
  ed.layers.forEach((layer, order) => {
    for (const item of layer.items) {
      const asset = assetOf(item);
      if (!asset) continue;
      seen.add(item.id);
      let entry = layerPreview.get(item.id);
      if (!entry) {
        entry = { node: null, sound: null };
        if (asset.kind !== "audio") {
          entry.node = itemPicture(asset);
          overlay.append(entry.node);
        }
        if (hasSound(asset)) entry.sound = itemSound(asset);
        layerPreview.set(item.id, entry);
      }
      const here = t >= item.start && t < itemEnd(item);
      const wanted = item.in + (t - item.start);
      const level = fadeLevel(item, t, item.start, itemEnd(item));
      if (entry.node) {
        const shown = here && !layer.hidden;
        entry.node.hidden = !shown;
        if (shown) {
          placeBox(entry.node, pictureBox(item, asset, t));
          entry.node.style.zIndex = order + 1;
          entry.node.style.opacity = String(item.opacity * level * transitionLevel(item, t));
        }
        if (asset.kind === "video") {
          steer(entry.node, wanted, shown && playing);
          if (shown && !playing && Math.abs(entry.node.currentTime - wanted) > 0.04) entry.node.currentTime = wanted;
        }
      }
      if (entry.sound) {
        const on = here && playing && !layer.muted && !ed.solo.size;
        steer(entry.sound.media, wanted, on);
        const fades = fadeLevel(soundFades(layer, item), t, item.start, itemEnd(item));
        const gain = on ? dbToGain(layer.volume_db) * dbToGain(item.volume_db) * fades : 0;
        entry.sound.gain.gain.setTargetAtTime(gain, audio.ctx.currentTime, 0.015);
      }
    }
  });
  for (const [id, entry] of layerPreview) {
    if (!seen.has(id)) {
      dropPreview(entry);
      layerPreview.delete(id);
    }
  }
}
