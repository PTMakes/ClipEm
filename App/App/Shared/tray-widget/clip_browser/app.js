"use strict";

// Served by clip_browser.py under a per-run token path - every request here
// is relative to it.
const POLL_MS = 3000;
const INSTALL_POLL_MS = 1000;

const state = {
  clips: [],
  selected: null,
  editors: null,
  soundAllowed: true, // false after the browser refused to play with sound
  hovering: null, // {name, thumb, fraction}
  installing: false, // LosslessCut download in progress
};

const $ = (id) => document.getElementById(id);
const list = $("clip-list");
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
  text.append(when, size, badges);

  li.append(thumb, text);
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

function updateItem(li, clip) {
  li.querySelector(".when").textContent = formatWhen(clip.mtime);
  li.querySelector(".size").textContent = formatSize(clip.size);
  li.querySelector(".length").textContent = formatDuration(clip.duration);
  li.querySelector(".length").hidden = clip.duration == null;
  const badges = li.querySelector(".badges");
  badges.replaceChildren();
  for (const [key, label] of [["tracks_file", "tracks"], ["small_file", "small"]]) {
    if (clip[key]) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = label;
      badges.append(badge);
    }
  }
  li.classList.toggle("selected", clip.name === state.selected);
}

function renderList() {
  const existing = new Map([...list.children].map((li) => [li.dataset.name, li]));
  const wanted = state.clips.map((clip) => {
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
  $("clip-count").textContent = state.clips.length ? `${state.clips.length}` : "";
  $("empty").hidden = state.clips.length > 0;
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
// Main pane
// ---------------------------------------------------------------------------

function selectedClip() {
  return state.clips.find((c) => c.name === state.selected) || null;
}

function select(name) {
  if (state.selected !== name) {
    state.selected = name;
    player.src = mediaUrl(name);
    setStatus("");
  }
  for (const li of list.children) li.classList.toggle("selected", li.dataset.name === name);
  renderDetails();
}

function renderDetails() {
  const clip = selectedClip();
  $("details").hidden = !clip;
  $("no-selection").hidden = !!clip;
  if (!clip) {
    player.removeAttribute("src");
    player.load();
    return;
  }
  $("clip-title").textContent = clip.name;
  $("clip-meta").textContent = [formatWhen(clip.mtime), formatDuration(clip.duration), formatSize(clip.size)]
    .filter(Boolean).join(" · ");
  $("tracks-note").textContent = clip.tracks_file
    ? `Editors open ${clip.tracks_file}, with every audio track kept separate.`
    : "";
  renderEditorButtons();
}

function renderEditorButtons() {
  const editors = state.editors;
  const lc = $("edit-losslesscut");
  const custom = $("edit-custom");
  if (!editors) return;
  lc.hidden = !editors.losslesscut.supported;
  if (state.installing) {
    lc.disabled = true;
  } else {
    lc.disabled = false;
    lc.textContent = editors.losslesscut.installed
      ? "Edit in LosslessCut"
      : `Download LosslessCut (~${editors.losslesscut.download_mb} MB)`;
  }
  custom.hidden = !editors.custom;
  if (editors.custom) custom.textContent = `Open in ${editors.custom.label}`;
}

async function openIn(editor) {
  const clip = selectedClip();
  if (!clip) return;
  try {
    const result = await api("api/open", { name: clip.name, editor });
    setStatus(`Opened ${result.file}.`);
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function installLosslessCut() {
  state.installing = true;
  renderEditorButtons();
  try {
    let status = await api("api/install-losslesscut", {});
    while (status.state === "running") {
      $("edit-losslesscut").textContent = status.message || "Downloading LosslessCut...";
      await new Promise((resolve) => setTimeout(resolve, INSTALL_POLL_MS));
      status = await api("api/install-status");
    }
    setStatus(status.message, status.state === "error");
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    state.installing = false;
    await loadEditors();
  }
}

$("edit-losslesscut").addEventListener("click", () => {
  if (state.editors && state.editors.losslesscut.installed) openIn("losslesscut");
  else installLosslessCut();
});
$("edit-custom").addEventListener("click", () => openIn("custom"));
$("reveal").addEventListener("click", async () => {
  const clip = selectedClip();
  if (!clip) return;
  try {
    await api("api/reveal", { name: clip.name });
  } catch (err) {
    setStatus(err.message, true);
  }
});
player.addEventListener("play", () => {
  if (state.hovering) stopHover(state.hovering.thumb);
});

document.addEventListener("keydown", (e) => {
  if (e.target instanceof HTMLButtonElement || !["ArrowUp", "ArrowDown"].includes(e.key)) return;
  const index = state.clips.findIndex((c) => c.name === state.selected);
  const next = state.clips[index + (e.key === "ArrowDown" ? 1 : -1)];
  if (next) {
    e.preventDefault();
    select(next.name);
    list.querySelector(`[data-name="${CSS.escape(next.name)}"]`)?.scrollIntoView({ block: "nearest" });
  }
});

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------

async function loadEditors() {
  try {
    state.editors = await api("api/editors");
  } catch (err) {
    setStatus(err.message, true);
  }
  renderEditorButtons();
}

async function loadClips() {
  try {
    const data = await api("api/clips");
    const newest = data.clips[0] && data.clips[0].name;
    const hadNewest = state.clips[0] && state.clips[0].name;
    state.clips = data.clips;
    renderList();
    if (!selectedClip()) {
      // First load (or the selected clip was deleted): open the latest one.
      state.selected = null;
      if (newest) select(newest);
      else renderDetails();
    } else {
      if (newest && newest !== hadNewest) setStatus(`New clip: ${newest}`);
      renderDetails();
    }
  } catch (err) {
    setStatus(`Can't reach ClipEm: ${err.message}`, true);
  }
}

loadEditors();
loadClips();
setInterval(loadClips, POLL_MS);
