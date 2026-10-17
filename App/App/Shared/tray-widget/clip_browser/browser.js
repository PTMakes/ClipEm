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
  text.append(when, size, badges);

  const remove = document.createElement("button");
  remove.className = "clip-delete";
  remove.title = `Move to the ${TRASH_NAME}`;
  remove.setAttribute("aria-label", `Delete clip`);
  remove.innerHTML = TRASH_ICON;
  remove.addEventListener("click", (e) => {
    e.stopPropagation();
    deleteClip(clip.name);
  });
  remove.addEventListener("keydown", (e) => e.stopPropagation());

  li.append(thumb, text, remove);
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
  for (const [key, label] of [["tracks_file", "+tracks"], ["small_file", "+small"]]) {
    if (clip[key]) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = label;
      badges.append(badge);
    }
  }
  li.classList.toggle("selected", clip.name === state.selected);
}

const TRASH_NAME = /Windows/.test(navigator.userAgent) ? "Recycle Bin" : "Trash";
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

function selectedClip() {
  return state.clips.find((c) => c.name === state.selected) || null;
}

async function loadClips() {
  try {
    const data = await api("api/clips");
    const newest = data.clips[0] && data.clips[0].name;
    const hadNewest = state.clips[0] && state.clips[0].name;
    const first = state.clips.length === 0 && state.selected === null;
    state.clips = data.clips;
    renderList();
    if (!selectedClip()) {
      // First load (or the selected clip was deleted): open the latest one.
      state.selected = null;
      if (newest) select(newest);
      else renderDetails();
    } else {
      if (!first && newest && newest !== hadNewest) setStatus(`New clip: ${newest}`);
      renderDetails();
    }
  } catch (err) {
    setStatus(`Can't reach ClipEm: ${err.message}`, true);
  }
}
