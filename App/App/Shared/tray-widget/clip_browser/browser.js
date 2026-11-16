"use strict";

// The clip list sidebar (hover to play with sound, move to scrub) and what
// the editor (editor.js, which defines select() and renderDetails()) shares
// with it. Served by clip_browser.py under a per-run token path - every
// request here is relative to it.
const POLL_MS = 3000;
const SIDEBAR_KEY = "clipem.sidebarHidden";

const state = {
  clips: [],
  selected: null,
  editors: null,
  soundAllowed: true, // false after the browser refused to play with sound
  hovering: null, // {name, thumb, fraction}
};

const $ = (id) => document.getElementById(id);
const list = $("clip-list");
const EMPTY_TEXT = $("empty").textContent;
const player = $("player");
const hoverVideo = document.createElement("video");
hoverVideo.preload = "auto";
hoverVideo.playsInline = true;

function mediaUrl(name) {
  return "media/" + encodeURIComponent(name);
}

function formatDuration(seconds) {
  if (seconds == null || !isFinite(seconds)) return "";
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = String(total % 60).padStart(2, "0");
  return `${m}:${s}`;
}

function formatSize(bytes) {
  if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toFixed(1) + " GB";
  return Math.max(1, Math.round(bytes / 1024 ** 2)) + " MB";
}

function formatWhen(mtime) {
  return new Date(mtime * 1000).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function setStatus(message, isError = false) {
  const status = $("status");
  status.textContent = message || "";
  status.title = message || "";
  status.classList.toggle("error", isError);
}

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

function clipItem(clip) {
  const li = document.createElement("li");
  li.className = "clip";
  li.dataset.name = clip.name;
  li.tabIndex = 0;

  const thumb = document.createElement("div");
  thumb.className = "thumb";
  const img = document.createElement("img");
  img.loading = "lazy";
  img.alt = "";
  img.src = "thumb/" + encodeURIComponent(clip.name) + "?m=" + clip.mtime;
  img.onerror = () => { img.remove(); };
  const scrub = document.createElement("div");
  scrub.className = "scrub";
  const length = document.createElement("span");
  length.className = "length";
  thumb.append(img, scrub, length);

  const text = document.createElement("div");
  text.className = "clip-text";
  const when = document.createElement("span");
  when.className = "when";
  const size = document.createElement("span");
  size.className = "size muted";
  const badges = document.createElement("span");
  badges.className = "badges";
  // Links on their own row under the thumbnail (the text beside it is too
  // narrow) - see updateItem: the clip's newest export - pointed out in the
  // list when it's there, else shown in its folder - and for an export, the
  // clip it was made from.
  const exported = linkButton("clip-export", () => {
    const name = exported.dataset.file;
    if (exported.dataset.listed === "1" && state.clips.some((c) => c.name === name)) highlightClip(name);
    else revealFile({ name, path: exported.dataset.path });
  });
  const origin = linkButton("clip-origin", () => highlightClip(origin.dataset.clip));
  const meta = document.createElement("div");
  meta.className = "clip-meta";
  meta.append(size, badges);
  text.append(when, meta);

  const reveal = document.createElement("button");
  reveal.className = "clip-action clip-reveal";
  reveal.title = "Show in folder";
  reveal.setAttribute("aria-label", "Show in folder");
  reveal.innerHTML = FOLDER_ICON;
  reveal.addEventListener("click", (e) => {
    e.stopPropagation();
    revealFile({ name: clip.name });
  });
  reveal.addEventListener("keydown", (e) => e.stopPropagation());

  const remove = document.createElement("button");
  remove.className = "clip-action clip-delete";
  remove.title = `Move to the ${TRASH_NAME}`;
  remove.setAttribute("aria-label", `Delete clip`);
  remove.innerHTML = TRASH_ICON;
  remove.addEventListener("click", (e) => {
    e.stopPropagation();
    deleteClip(clip.name);
  });
  remove.addEventListener("keydown", (e) => e.stopPropagation());

  const exportRow = document.createElement("div");
  exportRow.className = "clip-export-row";
  exportRow.append(exported, origin);
  li.append(thumb, text, exportRow, reveal, remove);
  li.addEventListener("click", () => select(clip.name));
  li.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(clip.name); }
  });
  thumb.addEventListener("mouseenter", (e) => startHover(clip.name, thumb, e));
  thumb.addEventListener("mousemove", (e) => moveHover(thumb, e));
  thumb.addEventListener("mouseleave", () => stopHover(thumb));
  updateItem(li, clip);
  return li;
}

// The file manager, with the file selected.
async function revealFile(body) {
  try {
    await api("api/reveal", body);
  } catch (err) {
    setStatus(err.message, true);
  }
}

function linkButton(className, onClick) {
  const button = document.createElement("button");
  button.className = `clip-export ${className}`;
  button.innerHTML = LINK_ICON + "<span></span>";
  button.addEventListener("click", (e) => {
    e.stopPropagation();
    onClick();
  });
  button.addEventListener("keydown", (e) => e.stopPropagation());
  return button;
}

// Scrolls a clip in the list into view and makes it flash (not selected).
function highlightClip(name) {
  const li = [...list.children].find((item) => item.dataset.name === name);
  if (!li) return;
  li.scrollIntoView({ block: "nearest", behavior: "smooth" });
  li.classList.remove("flash");
  void li.offsetWidth; // restart the animation
  li.classList.add("flash");
}

function shortWhen(seconds) {
  return new Date(seconds * 1000).toLocaleString(undefined,
    { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function updateItem(li, clip) {
  // An export goes by its file name (they'd all show their export time).
  const from = clip.exported_from;
  const stem = clip.name.replace(/\.[^.]+$/, "");
  li.querySelector(".when").textContent = from ? stem : formatWhen(clip.mtime);
  li.querySelector(".when").title = from ? clip.name : "";
  li.querySelector(".when").classList.toggle("named", !!from);
  li.querySelector(".size").textContent = formatSize(clip.size);
  li.querySelector(".size").title = li.querySelector(".size").textContent; // when cut short
  li.querySelector(".length").textContent = formatDuration(clip.duration);
  li.querySelector(".length").hidden = clip.duration == null;
  const badges = li.querySelector(".badges");
  badges.replaceChildren();
  for (const [key, label] of [["tracks_file", "+tracks"], ["small_file", "+small"]]) {
    if (clip[key]) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = label;
      badges.append(badge);
    }
  }
  const exported = li.querySelector(".clip-export:not(.clip-origin)");
  const origin = li.querySelector(".clip-origin");
  const last = clip.last_export;
  exported.hidden = !last;
  origin.hidden = !from;
  exported.parentElement.hidden = !last && !from;
  if (last) {
    exported.dataset.file = last.name;
    exported.dataset.path = last.path;
    exported.dataset.listed = last.in_folder ? "1" : "";
    exported.title = last.in_folder
      ? `Show ${last.name} in the list (exported ${shortWhen(last.time)})`
      : `Show ${last.name} in its folder (exported ${shortWhen(last.time)})`;
    exported.querySelector("span").textContent = `Last Exported As ${last.name}`;
  }
  if (from) {
    origin.dataset.clip = from.name;
    origin.title = `Show the clip this was exported from: ${from.name}`;
    // With seconds, as the original clip's own date shows them.
    origin.querySelector("span").textContent = `Exported From ${formatWhen(from.time)}`;
  }
  li.classList.toggle("selected", clip.name === state.selected);
}

const LINK_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6.8 9.2a2.6 2.6 0 0 0 3.7 0l2.3-2.3a2.6 2.6 0 0 0-3.7-3.7l-1 1M9.2 6.8a2.6 2.6 0 0 0-3.7 0L3.2 9.1a2.6 2.6 0 0 0 3.7 3.7l1-1"/></svg>';

const TRASH_NAME = /Windows/.test(navigator.userAgent) ? "Recycle Bin" : "Trash";
const FOLDER_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1.75 4.25v8a1 1 0 0 0 1 1h10.5a1 1 0 0 0 1-1v-6.5a1 1 0 0 0-1-1H8L6.5 3.25H2.75a1 1 0 0 0-1 1z"/></svg>';
const TRASH_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 4h11M6 4V2.5h4V4M4 4l.7 9.5h6.6L12 4M6.6 6.5v4.5M9.4 6.5v4.5"/></svg>';

async function deleteClip(name) {
  const clip = state.clips.find((c) => c.name === name);
  if (!clip) return;
  const copies = [clip.tracks_file, clip.small_file].filter(Boolean);
  const also = copies.length ? `\n\nIts ${copies.length === 1 ? "copy" : "copies"} go too: ${copies.join(", ")}` : "";
  if (!window.confirm(`Move ${name} to the ${TRASH_NAME}?${also}`)) return;
  if (state.hovering) stopHover(state.hovering.thumb);
  hoverVideo.removeAttribute("src");
  hoverVideo.load();
  if (state.selected === name) closeClip(); // lets go of the file first
  try {
    await api("api/delete", { name });
    await loadClips();
    setStatus(`Moved ${name} to the ${TRASH_NAME}.`);
  } catch (err) {
    await loadClips();
    window.alert(`Couldn't delete ${name}: ${err.message}`);
  }
}

// ---------------------------------------------------------------------------
// Filters (the row under the title): game, day or hour, and which copies
// ---------------------------------------------------------------------------

const DAY_MS = 24 * 3600 * 1000;
const filters = { game: "", date: "", tracks: false, small: false, exports: false };
const filterControls = {
  game: $("filter-game"), date: $("filter-date"),
  tracks: $("filter-tracks"), small: $("filter-small"), exports: $("filter-exports"),
};

function dayKey(when) {
  const d = new Date(when);
  return `day:${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function hourStart(when) {
  const d = new Date(when);
  d.setMinutes(0, 0, 0);
  return d.getTime();
}

function dayName(when) {
  const key = dayKey(when);
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  if (key === dayKey(Date.now())) return "Today";
  if (key === dayKey(yesterday)) return "Yesterday";
  return new Date(when).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function hourName(start) {
  const time = new Date(start).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return `${dayName(start)} ${time}`;
}

// Whether a clip is in the date filter's day, or hour ("hour:<start ms>").
function inDate(clip, value) {
  const when = clip.mtime * 1000;
  if (value.startsWith("hour:")) {
    const start = Number(value.slice(5));
    return when >= start && when < start + 3600 * 1000;
  }
  return dayKey(when) === value;
}

function matchesFilters(clip) {
  if (filters.game && (clip.game || "") !== filters.game) return false;
  if (filters.date && !inDate(clip, filters.date)) return false;
  // Ticked boxes: the clips with any of them.
  const kinds = [[filters.tracks, !!clip.tracks_file], [filters.small, !!clip.small_file],
    [filters.exports, !!clip.exported_from]].filter(([on]) => on);
  return kinds.length === 0 || kinds.some(([, has]) => has);
}

function filtering() {
  return !!(filters.game || filters.date || filters.tracks || filters.small || filters.exports);
}

function visibleClips() {
  return filtering() ? state.clips.filter(matchesFilters) : state.clips;
}

function countUp(map, key, label) {
  const entry = map.get(key) || { label, count: 0 };
  entry.count += 1;
  map.set(key, entry);
}

// Fills a <select> with [{value, label}] or [{group, options}], unless it
// already has them (rebuilding it would close it while it's open).
function setOptions(select, items) {
  const signature = JSON.stringify(items);
  if (select.dataset.signature === signature) return;
  select.dataset.signature = signature;
  const option = ({ value, label }) => new Option(label, value);
  select.replaceChildren(new Option("All", ""), ...items.map((item) => {
    if (!item.group) return option(item);
    const group = document.createElement("optgroup");
    group.label = item.group;
    group.append(...item.options.map(option));
    return group;
  }));
}

// The games and dates the clips have, with how many of each. One that's
// picked stays listed (with 0) after its clips are gone or its hour is past.
function renderFilterOptions() {
  const games = new Map();
  const days = new Map();
  const hours = new Map();
  const since = Date.now() - DAY_MS;
  for (const clip of state.clips) {
    const when = clip.mtime * 1000;
    if (clip.game) countUp(games, clip.game, clip.game);
    countUp(days, dayKey(when), dayName(when));
    if (when >= since) {
      const start = hourStart(when);
      countUp(hours, `hour:${start}`, hourName(start));
    }
  }
  if (filters.game && !games.has(filters.game)) games.set(filters.game, { label: filters.game, count: 0 });
  if (filters.date.startsWith("hour:") && !hours.has(filters.date)) {
    hours.set(filters.date, { label: hourName(Number(filters.date.slice(5))), count: 0 });
  } else if (filters.date.startsWith("day:") && !days.has(filters.date)) {
    const [y, m, d] = filters.date.slice(4).split("-").map(Number);
    days.set(filters.date, { label: dayName(new Date(y, m - 1, d).getTime()), count: 0 });
  }
  const options = (map) => [...map].map(([value, { label, count }]) => ({ value, label: `${label} (${count})` }));
  const gameOptions = options(games).sort((a, b) => a.value.localeCompare(b.value, undefined, { sensitivity: "base" }));
  // Newest first; keys of the same kind sort by time as text too.
  const newest = (a, b) => (a.value < b.value ? 1 : a.value > b.value ? -1 : 0);
  const hourOptions = [...hours].sort((a, b) => Number(b[0].slice(5)) - Number(a[0].slice(5)))
    .map(([value, { label, count }]) => ({ value, label: `${label} (${count})` }));
  const dateItems = [];
  if (hourOptions.length) dateItems.push({ group: "Last 24 hours", options: hourOptions });
  if (days.size) dateItems.push({ group: "Days", options: options(days).sort(newest) });
  setOptions(filterControls.game, gameOptions);
  setOptions(filterControls.date, dateItems);
  filterControls.game.value = filters.game;
  filterControls.date.value = filters.date;
  filterControls.game.classList.toggle("active", !!filters.game);
  filterControls.date.classList.toggle("active", !!filters.date);
}

for (const [key, control] of Object.entries(filterControls)) {
  control.addEventListener("change", () => {
    filters[key] = control.type === "checkbox" ? control.checked : control.value;
    renderList();
  });
}

function renderList() {
  renderFilterOptions();
  const shown = visibleClips();
  const existing = new Map([...list.children].map((li) => [li.dataset.name, li]));
  const wanted = shown.map((clip) => {
    const li = existing.get(clip.name) || clipItem(clip);
    existing.delete(clip.name);
    updateItem(li, clip);
    return li;
  });
  for (const li of existing.values()) {
    if (state.hovering && state.hovering.thumb.parentElement === li) stopHover(state.hovering.thumb);
    li.remove();
  }
  wanted.forEach((li, index) => {
    if (list.children[index] !== li) list.insertBefore(li, list.children[index] || null);
  });
  const total = state.clips.length;
  $("clip-count").textContent = !total ? "" : filtering() ? `${shown.length} of ${total}` : `${total}`;
  $("empty").textContent = total ? "No clips match these filters." : EMPTY_TEXT;
  $("empty").hidden = shown.length > 0;
}

// ---------------------------------------------------------------------------
// Hover to play (with sound), move to scrub
// ---------------------------------------------------------------------------

function fractionAt(thumb, event) {
  const rect = thumb.getBoundingClientRect();
  return Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
}

let seekFrame = null;
function seekHover() {
  seekFrame = null;
  const hover = state.hovering;
  if (!hover || !isFinite(hoverVideo.duration)) return;
  hoverVideo.currentTime = hover.fraction * hoverVideo.duration;
}

function scheduleSeek() {
  if (seekFrame === null) seekFrame = requestAnimationFrame(seekHover);
}

function playHover() {
  hoverVideo.muted = !state.soundAllowed;
  hoverVideo.play().catch((err) => {
    if (err && err.name === "NotAllowedError" && !hoverVideo.muted) {
      // Autoplay with sound blocked until the page is clicked - play muted
      // for now and say how to get sound.
      state.soundAllowed = false;
      $("sound-banner").hidden = false;
      hoverVideo.muted = true;
      hoverVideo.play().catch(() => {});
    }
  });
}

function startHover(name, thumb, event) {
  player.pause();
  state.hovering = { name, thumb, fraction: fractionAt(thumb, event) };
  const url = mediaUrl(name);
  if (!hoverVideo.src.endsWith(url)) {
    hoverVideo.src = url;
  }
  thumb.insertBefore(hoverVideo, thumb.querySelector(".scrub"));
  thumb.querySelector(".scrub").style.width = `${state.hovering.fraction * 100}%`;
  if (isFinite(hoverVideo.duration)) {
    scheduleSeek();
    playHover();
  } else {
    hoverVideo.addEventListener("loadedmetadata", () => {
      if (state.hovering && state.hovering.name === name) {
        scheduleSeek();
        playHover();
      }
    }, { once: true });
  }
}

function moveHover(thumb, event) {
  if (!state.hovering || state.hovering.thumb !== thumb) return;
  state.hovering.fraction = fractionAt(thumb, event);
  thumb.querySelector(".scrub").style.width = `${state.hovering.fraction * 100}%`;
  scheduleSeek();
}

function stopHover(thumb) {
  if (state.hovering && state.hovering.thumb === thumb) state.hovering = null;
  hoverVideo.pause();
  if (hoverVideo.parentElement === thumb) hoverVideo.remove();
  const scrub = thumb.querySelector(".scrub");
  if (scrub) scrub.style.width = "0";
}

document.addEventListener("click", () => {
  if (state.soundAllowed) return;
  state.soundAllowed = true;
  $("sound-banner").hidden = true;
  if (state.hovering) playHover();
}, { capture: true });

// ---------------------------------------------------------------------------
// Showing and hiding the sidebar
// ---------------------------------------------------------------------------

function setSidebarHidden(hidden) {
  document.body.classList.toggle("sidebar-hidden", hidden);
  $("toggle-sidebar").setAttribute("aria-pressed", String(!hidden));
  if (hidden && state.hovering) stopHover(state.hovering.thumb);
  try {
    localStorage.setItem(SIDEBAR_KEY, hidden ? "1" : "");
  } catch (err) { /* storage unavailable - just not remembered */ }
  window.dispatchEvent(new Event("resize"));
}

$("toggle-sidebar").addEventListener("click", () => {
  setSidebarHidden(!document.body.classList.contains("sidebar-hidden"));
});

try {
  if (localStorage.getItem(SIDEBAR_KEY)) setSidebarHidden(true);
} catch (err) { /* storage unavailable */ }

function typingIn(target) {
  return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement
    || target instanceof HTMLSelectElement;
}

document.addEventListener("keydown", (e) => {
  if (typingIn(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
  // Tab only toggles when nothing has focus, so keyboard focus moves as usual.
  if (e.key === "Tab" && !e.shiftKey && e.target === document.body) {
    e.preventDefault();
    setSidebarHidden(!document.body.classList.contains("sidebar-hidden"));
    return;
  }
  if (!["ArrowUp", "ArrowDown"].includes(e.key)) return;
  const shown = visibleClips();
  const index = shown.findIndex((c) => c.name === state.selected);
  const next = shown[index + (e.key === "ArrowDown" ? 1 : -1)];
  if (next) {
    e.preventDefault();
    select(next.name);
    list.querySelector(`[data-name="${CSS.escape(next.name)}"]`)?.scrollIntoView({ block: "nearest" });
  }
});

// ---------------------------------------------------------------------------
// Changing the folder (ClipEm's own window has a folder dialog)
// ---------------------------------------------------------------------------

if (window.clipem && window.clipem.chooseFolder) {
  const button = $("change-folder");
  button.hidden = false;
  button.addEventListener("click", async () => {
    try {
      const path = await window.clipem.chooseFolder(state.folder);
      if (!path) return; // cancelled
      await api("api/folder", { path });
      state.clips = [];
      state.selected = null;
      await loadClips();
      setStatus(`Viewing clips in ${path}`);
    } catch (err) {
      setStatus(err.message, true);
    }
  });
}

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------

function selectedClip() {
  return state.clips.find((c) => c.name === state.selected) || null;
}

// Settings > Use Dark Mode and its colors (clip_browser.py's api/theme),
// polled so a Save there shows here straight away. Tells editor.js, whose
// canvases read the colors when they draw.
async function loadTheme() {
  let theme;
  try {
    theme = await api("api/theme");
  } catch (err) {
    return; // loadClips reports ClipEm being unreachable
  }
  const root = document.documentElement;
  let style = $("custom-colors");
  if (!style) {
    style = document.head.appendChild(document.createElement("style"));
    style.id = "custom-colors";
  }
  if (root.dataset.theme === theme.theme && style.textContent === theme.css) return;
  root.dataset.theme = theme.theme;
  style.textContent = theme.css;
  document.dispatchEvent(new Event("clipem-theme"));
}

// The file a clip is edited from (clip_browser.file_to_edit): its
// "-extra.mkv", else its "-tracks.mkv", else the clip - or null if it isn't listed.
function editSource(clips, name) {
  const clip = clips.find((c) => c.name === name);
  return clip ? clip.extra_file || clip.tracks_file || clip.name : null;
}

async function loadClips() {
  try {
    const data = await api("api/clips");
    const newest = data.clips[0] && data.clips[0].name;
    const hadNewest = state.clips[0] && state.clips[0].name;
    const first = state.clips.length === 0 && state.selected === null;
    const loaded = state.selected && editSource(state.clips, state.selected);
    state.clips = data.clips;
    state.folder = data.folder;
    const folderBox = $("folder-path");
    folderBox.value = data.folder;
    folderBox.title = data.folder;
    renderList();
    if (!selectedClip()) {
      // First load (or the selected clip was deleted): open the latest one.
      state.selected = null;
      if (newest) select(newest);
      else renderDetails();
    } else if (loaded !== editSource(state.clips, state.selected)) {
      // Its "-extra.mkv"/"-tracks.mkv" was finished after it was opened
      // (they're written just after the clip): open it again from that.
      const name = state.selected;
      state.selected = null; // or select() takes it as already open
      select(name);
    } else {
      if (!first && newest && newest !== hadNewest) setStatus(`New clip: ${newest}`);
      renderDetails();
    }
  } catch (err) {
    setStatus(`Can't reach ClipEm: ${err.message}`, true);
  }
}
