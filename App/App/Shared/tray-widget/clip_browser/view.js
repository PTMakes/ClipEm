"use strict";

// The video's view and crop:
// - Zoom (view only, not exported): the mouse wheel over the video, or the
//   magnifier buttons that show on hover; "Full frame" goes back. Drag to
//   pan while zoomed (or with the middle button).
// - Crop (saved with the edit, applied on export - clip_edit.normalize_crop):
//   drag a corner or the top/left/right edge's handle. The shape stays the
//   video's own (16:9 for most captures) and snaps to standard sizes (720p,
//   480p...) unless unlocked - with the lock button near the handles, or by
//   holding Ctrl, Alt or Shift while dragging. Drag inside a crop to move it.
// Crop coordinates are the source video's pixels, as the export uses them.

const stage = $("stage");
const wrap = $("player-wrap");
const cropBox = $("crop-box");
const cropSize = $("crop-size");
const cropLock = $("crop-lock");

const view = { scale: 1, x: 0, y: 0, width: 0, height: 0, name: null };
const ZOOM_STEP = 1.25;
const MAX_ZOOM = 8;
const MIN_CROP = 64;
const SNAP_SCREEN_PX = 8; // how close (on screen) a size has to be to snap
const NEAR_HANDLE_PX = 56;
const STANDARD_HEIGHTS = [2160, 1440, 1080, 900, 720, 540, 480, 360, 240];
const STANDARD_WIDTHS = [3840, 2560, 1920, 1600, 1280, 960, 854, 640, 426];
ed.cropLocked = true;

const MAGNIFIER = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="6.75" cy="6.75" r="4.25"/><path d="M10 10l4 4"/>';
$("zoom-in").innerHTML = MAGNIFIER + '<path d="M4.75 6.75h4M6.75 4.75v4"/></svg>';
$("zoom-out").innerHTML = MAGNIFIER + '<path d="M4.75 6.75h4"/></svg>';
$("zoom-fit").innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 6V2h4M10 2h4v4M14 10v4h-4M6 14H2v-4"/></svg>';
const LOCKED_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3.5" y="7" width="9" height="7" rx="1"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/></svg>';
const UNLOCKED_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3.5" y="7" width="9" height="7" rx="1"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5-.5"/></svg>';

function sourceSize() {
  if (ed.info && ed.info.width && ed.info.height) return [ed.info.width, ed.info.height];
  return [player.videoWidth || 16, player.videoHeight || 9];
}

function cropRect() {
  const [width, height] = sourceSize();
  return ed.crop || { x: 0, y: 0, w: width, h: height };
}

// --- Zoom -----------------------------------------------------------------------

// The stage is the video's own shape, as big as fits in the player's space.
function fitStage() {
  const style = getComputedStyle(wrap);
  const room = [
    wrap.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
    wrap.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom),
  ];
  const [width, height] = sourceSize();
  const fit = Math.max(0, Math.min(room[0] / width, room[1] / height));
  view.width = Math.floor(width * fit);
  view.height = Math.floor(height * fit);
  stage.style.width = `${view.width}px`;
  stage.style.height = `${view.height}px`;
  applyView();
}

function applyView() {
  // Never shows anything outside the video: zoom is at least 1, panning stops at its edges.
  view.scale = clamp(view.scale, 1, MAX_ZOOM);
  view.x = clamp(view.x, view.width - view.width * view.scale, 0);
  view.y = clamp(view.y, view.height - view.height * view.scale, 0);
  stage.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
  stage.style.setProperty("--zoom", view.scale);
  wrap.classList.toggle("zoomed", view.scale > 1);
}

// Zooms by factor keeping the point under (clientX, clientY) where it is.
function zoomAt(factor, clientX, clientY) {
  const rect = stage.getBoundingClientRect();
  const left = rect.left - view.x;
  const top = rect.top - view.y;
  const pointX = (clientX - left - view.x) / view.scale;
  const pointY = (clientY - top - view.y) / view.scale;
  view.scale = clamp(view.scale * factor, 1, MAX_ZOOM);
  view.x = clientX - left - pointX * view.scale;
  view.y = clientY - top - pointY * view.scale;
  applyView();
}

function zoomAtCentre(factor) {
  const rect = wrap.getBoundingClientRect();
  zoomAt(factor, rect.left + rect.width / 2, rect.top + rect.height / 2);
}

function resetZoom() {
  view.scale = 1;
  view.x = view.y = 0;
  applyView();
}

wrap.addEventListener("wheel", (e) => {
  if (!ed.info || stage.hidden) return;
  e.preventDefault();
  zoomAt(e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP, e.clientX, e.clientY);
}, { passive: false });
$("zoom-in").addEventListener("click", () => zoomAtCentre(ZOOM_STEP));
$("zoom-out").addEventListener("click", () => zoomAtCentre(1 / ZOOM_STEP));
$("zoom-fit").addEventListener("click", resetZoom);
$("crop-reset").addEventListener("click", () => change(() => { ed.crop = null; }));

function pan(e) {
  const start = { x: view.x, y: view.y, cx: e.clientX, cy: e.clientY };
  trackPointer(e, (ev) => {
    view.x = start.x + ev.clientX - start.cx;
    view.y = start.y + ev.clientY - start.cy;
    applyView();
  }, () => {});
}

// Middle button: pan, anywhere on the video.
wrap.addEventListener("pointerdown", (e) => {
  if (e.button !== 1 || view.scale <= 1) return;
  e.preventDefault();
  pan(e);
});

new ResizeObserver(fitStage).observe(wrap);
player.addEventListener("loadedmetadata", fitStage);

// --- Crop -----------------------------------------------------------------------

function renderCrop() {
  if (ed.name !== view.name) { // another clip: start from the full frame
    view.name = ed.name;
    view.scale = 1;
    view.x = view.y = 0;
  }
  fitStage();
  const [width, height] = sourceSize();
  const rect = cropRect();
  cropBox.style.left = `${(rect.x / width) * 100}%`;
  cropBox.style.top = `${(rect.y / height) * 100}%`;
  cropBox.style.width = `${(rect.w / width) * 100}%`;
  cropBox.style.height = `${(rect.h / height) * 100}%`;
  placeShades(ed.crop, width, height);
  cropBox.classList.toggle("cropped", !!ed.crop);
  cropBox.classList.toggle("can-crop", !!ed.canCrop);
  cropSize.textContent = `${rect.w} × ${rect.h}`;
  $("crop-reset").hidden = !ed.crop;
  const ratio = aspectLabel(width, height);
  cropLock.innerHTML = ed.cropLocked ? LOCKED_ICON : UNLOCKED_ICON;
  cropLock.classList.toggle("unlocked", !ed.cropLocked);
  cropLock.title = ed.cropLocked
    ? `Crop shape locked to ${ratio} - click to unlock (or hold Ctrl, Alt or Shift while dragging)`
    : `Crop shape free - click to lock it to ${ratio}`;
}

function placeShades(crop, width, height) {
  const pct = (v, of) => `${(v / of) * 100}%`;
  for (const shade of document.querySelectorAll(".crop-shade")) {
    shade.hidden = !crop;
    if (!crop) continue;
    const side = shade.dataset.side;
    const box = {
      top: [0, 0, width, crop.y],
      bottom: [0, crop.y + crop.h, width, height - crop.y - crop.h],
      left: [0, crop.y, crop.x, crop.h],
      right: [crop.x + crop.w, crop.y, width - crop.x - crop.w, crop.h],
    }[side];
    Object.assign(shade.style, { left: pct(box[0], width), top: pct(box[1], height),
      width: pct(box[2], width), height: pct(box[3], height) });
  }
}

function aspectLabel(width, height) {
  const gcd = (a, b) => (b ? gcd(b, a % b) : a);
  const d = gcd(width, height);
  const label = `${width / d}:${height / d}`;
  return label.length > 7 ? `${(width / height).toFixed(2)}:1` : label;
}

cropLock.addEventListener("pointerdown", (e) => e.stopPropagation());
cropLock.addEventListener("click", (e) => {
  e.stopPropagation();
  ed.cropLocked = !ed.cropLocked;
  renderCrop();
});

// The lock shows while the pointer is near a handle.
wrap.addEventListener("pointermove", (e) => {
  const near = !!ed.canCrop && [...cropBox.querySelectorAll(".crop-handle")].some((handle) => {
    const r = handle.getBoundingClientRect();
    return Math.hypot(e.clientX - (r.left + r.width / 2), e.clientY - (r.top + r.height / 2)) < NEAR_HANDLE_PX;
  });
  cropBox.classList.toggle("near-handle", near);
});
wrap.addEventListener("pointerleave", () => cropBox.classList.remove("near-handle"));

// The closest of choices within tolerance of value, else value.
function snapTo(value, choices, tolerance) {
  let best = value;
  let bestDistance = tolerance;
  for (const choice of choices) {
    const distance = Math.abs(choice - value);
    if (distance <= bestDistance) {
      best = choice;
      bestDistance = distance;
    }
  }
  return best;
}

// The crop rectangle after dragging `edge` (n, w, e, nw, ne, sw, se) by
// (dx, dy) source pixels from `start`, inside a width x height frame.
function resizeCrop(start, edge, dx, dy, free, width, height, snapPx) {
  let left = start.x;
  let top = start.y;
  let right = start.x + start.w;
  let bottom = start.y + start.h;
  if (edge.includes("w")) left = clamp(left + dx, 0, right - MIN_CROP);
  if (edge.includes("e")) right = clamp(right + dx, left + MIN_CROP, width);
  if (edge.includes("n")) top = clamp(top + dy, 0, bottom - MIN_CROP);
  if (edge.includes("s")) bottom = clamp(bottom + dy, top + MIN_CROP, height);
  let w = right - left;
  let h = bottom - top;
  const centreX = start.x + start.w / 2;
  const centreY = start.y + start.h / 2;
  if (free) {
    // Each side snaps on its own - only to a size that still fits.
    const roomX = edge.includes("w") ? right : width - left;
    const roomY = edge.includes("n") ? bottom : height - top;
    if (edge.includes("w") || edge.includes("e")) w = snapTo(w, STANDARD_WIDTHS.filter((v) => v <= roomX), snapPx);
    if (edge.includes("n") || edge.includes("s")) h = snapTo(h, STANDARD_HEIGHTS.filter((v) => v <= roomY), snapPx);
  } else {
    const ratio = width / height;
    const byWidth = edge === "w" || edge === "e"
      || (edge.length === 2 && Math.abs(w - start.w) / start.w >= Math.abs(h - start.h) / start.h);
    if (byWidth) h = w / ratio;
    h = snapTo(h, STANDARD_HEIGHTS, snapPx);
    w = h * ratio;
    // Shrink to fit the frame, keeping the side opposite the handle in place
    // (or, for an edge handle, the crop centred across it).
    const roomX = edge.includes("w") ? right : edge.includes("e") ? width - left
      : 2 * Math.min(centreX, width - centreX);
    const roomY = edge.includes("n") ? bottom : edge.includes("s") ? height - top
      : 2 * Math.min(centreY, height - centreY);
    const fit = Math.min(1, roomX / w, roomY / h);
    w *= fit;
    h *= fit;
  }
  const x = edge.includes("w") ? right - w : edge.includes("e") ? left : centreX - w / 2;
  const y = edge.includes("n") ? bottom - h : edge.includes("s") ? top : centreY - h / 2;
  return { x, y, w, h };
}

// Whole, even pixels inside the frame (as the export needs) - or null for
// the whole frame. With ratio (a locked shape), the width follows the
// height, so the shape stays exact (720 high is exactly 1280 wide at 16:9).
function toCrop(rect, width, height, ratio = null) {
  const even = (v) => Math.round(v / 2) * 2;
  const h = Math.min(even(clamp(rect.h, MIN_CROP, height)), height - (height % 2));
  const w = ratio ? Math.min(even(h * ratio), width - (width % 2))
    : Math.min(even(clamp(rect.w, MIN_CROP, width)), width - (width % 2));
  const x = even(clamp(Math.round(rect.x), 0, width - w));
  const y = even(clamp(Math.round(rect.y), 0, height - h));
  if (x === 0 && y === 0 && w === even(width) && h === even(height)) return null;
  return { x, y, w, h };
}

function sourcePerScreenPixel() {
  return sourceSize()[0] / (stage.getBoundingClientRect().width || 1);
}

function dragHandle(e, edge) {
  const [width, height] = sourceSize();
  const start = cropRect();
  const perPixel = sourcePerScreenPixel();
  const x0 = e.clientX;
  const y0 = e.clientY;
  beginEdit();
  cropBox.classList.add("dragging");
  trackPointer(e, (ev) => {
    const free = !ed.cropLocked || ev.ctrlKey || ev.altKey || ev.shiftKey;
    const rect = resizeCrop(start, edge, (ev.clientX - x0) * perPixel, (ev.clientY - y0) * perPixel,
      free, width, height, SNAP_SCREEN_PX * perPixel);
    ed.crop = toCrop(rect, width, height, free ? null : width / height);
    renderCrop();
  }, () => {
    cropBox.classList.remove("dragging");
    endEdit();
  });
}

// Inside the crop: a click plays/pauses; a drag moves the crop - or, with
// nothing cropped, pans the zoomed video.
function pressBox(e) {
  const [width, height] = sourceSize();
  const start = cropRect();
  const perPixel = sourcePerScreenPixel();
  const x0 = e.clientX;
  const y0 = e.clientY;
  let moving = null;
  trackPointer(e, (ev) => {
    if (!moving && Math.hypot(ev.clientX - x0, ev.clientY - y0) < DRAG_THRESHOLD_PX) return;
    if (!moving) {
      moving = ed.crop && ed.canCrop ? "crop" : view.scale > 1 ? "pan" : "none";
      if (moving === "crop") beginEdit();
      if (moving === "pan") {
        pan(ev);
        return;
      }
    }
    if (moving !== "crop") return;
    ed.crop = toCrop({
      x: clamp(start.x + (ev.clientX - x0) * perPixel, 0, width - start.w),
      y: clamp(start.y + (ev.clientY - y0) * perPixel, 0, height - start.h),
      w: start.w,
      h: start.h,
    }, width, height);
    renderCrop();
  }, () => {
    if (!moving) togglePlay();
    else if (moving === "crop") endEdit();
  });
}

cropBox.addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || !ed.info) return;
  e.preventDefault();
  e.stopPropagation();
  const handle = e.target.closest(".crop-handle");
  if (handle && ed.canCrop) dragHandle(e, handle.dataset.edge);
  else pressBox(e);
});

renderCrop();
