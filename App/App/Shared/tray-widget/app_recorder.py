"""Records every app that plays sound in memory, for the clip editor's
multitrack output (see obs_config's notes on the layout).

app_audio.AppTrackMonitor says which apps have audio streams; this module
captures them - one helper process per stream, each writing raw 16-bit stereo
48 kHz audio to its stdout - and keeps the last minute or so of every app in
a ring buffer. When a clip is saved, tray_tracks.py takes a snapshot of the
rings, lines them up with the clip's own audio and makes a track of each app
that was heard.

- Linux: `parec --monitor-stream`, which records one PulseAudio/PipeWire
  stream's own sound wherever it's playing, without touching the routing.
- Windows: clipem-app-capture.exe (Dev/app-capture), WASAPI process loopback
  for an app's process tree (Windows 10 2004+).

Time: every ring shares one clock counting 48 kHz frames since the recorder
started. A helper's sound is placed by when it arrived (StreamPlacer), which
is only good to a few dozen milliseconds - tray_tracks.py corrects for that
by matching each track against the clip's Desktop track.
"""

import logging
import subprocess
import sys
import threading
import time
from array import array

import obs_config

try:
    import audioop  # removed in Python 3.13
except ImportError:
    audioop = None

log = logging.getLogger("tray")

RATE = 48000
CHANNELS = 2
BYTES_PER_FRAME = 4
CHUNK_FRAMES = 960  # 20 ms - what a helper writes at a time
# The rings hold the replay buffer plus this: a minute of margin around the
# clip for lining it up (WINDOW_MARGIN) and for the save event arriving late.
RING_EXTRA_SECONDS = 8
# A chunk that arrives this much later than the stream's own count says is a
# gap in the sound (the app paused), not jitter.
RESYNC_FRAMES = RATE // 10
RESYNC_CHUNKS = 5
# Don't restart a helper that keeps exiting more often than this.
RESTART_SECONDS = 10


def mix_pcm(a, b):
    """The sum of two equal-length 16-bit PCM byte strings, clipped."""
    if audioop is not None:
        return audioop.add(a, b, 2)
    x, y = array("h"), array("h")
    x.frombytes(a)
    y.frombytes(b)
    if sys.byteorder == "big":
        x.byteswap()
        y.byteswap()
    for i, value in enumerate(y):
        total = x[i] + value
        x[i] = 32767 if total > 32767 else -32768 if total < -32768 else total
    if sys.byteorder == "big":
        x.byteswap()
    return x.tobytes()


class SampleClock:
    """48 kHz frames since it was made."""

    def __init__(self, monotonic=time.monotonic):
        self._monotonic = monotonic
        self._start = monotonic()

    def frame(self, at=None):
        return int(((self._monotonic() if at is None else at) - self._start) * RATE)


class TrackRing:
    """The last `seconds` of one track, addressed by absolute frame number
    (SampleClock's). Several streams can add into it - their sound is summed.
    Frames nobody wrote are silence."""

    def __init__(self, seconds):
        self.capacity = int(seconds * RATE)
        self._buf = bytearray(self.capacity * BYTES_PER_FRAME)
        self._silence = bytes(len(self._buf))
        self.head = 0  # frames before this are in the ring (or were); after it, nothing yet
        self._lock = threading.Lock()

    def _zero(self, start, end):
        """Silences frames [start, end), clipped to what the ring still holds."""
        start = max(start, end - self.capacity)
        while start < end:
            offset = start % self.capacity
            count = min(end - start, self.capacity - offset)
            self._buf[offset * BYTES_PER_FRAME:(offset + count) * BYTES_PER_FRAME] = \
                self._silence[:count * BYTES_PER_FRAME]
            start += count

    def advance(self, frame):
        """Moves time on to `frame`: what falls out of the ring is dropped and
        what's new starts out silent."""
        with self._lock:
            self._advance(frame)

    def _advance(self, frame):
        if frame > self.head:
            self._zero(self.head, frame)
            self.head = frame

    def add(self, frame, pcm):
        """Mixes `pcm` (16-bit stereo) in starting at `frame`."""
        pcm = pcm[:len(pcm) // BYTES_PER_FRAME * BYTES_PER_FRAME]
        count = len(pcm) // BYTES_PER_FRAME
        with self._lock:
            self._advance(frame + count)
            oldest = self.head - self.capacity
            if frame < oldest:  # older than the ring holds
                skip = min(count, oldest - frame)
                pcm, frame, count = pcm[skip * BYTES_PER_FRAME:], frame + skip, count - skip
            done = 0
            while done < count:
                offset = (frame + done) % self.capacity
                chunk = min(count - done, self.capacity - offset)
                lo, hi = offset * BYTES_PER_FRAME, (offset + chunk) * BYTES_PER_FRAME
                piece = pcm[done * BYTES_PER_FRAME:(done + chunk) * BYTES_PER_FRAME]
                if self._buf[lo:hi] == self._silence[:hi - lo]:
                    self._buf[lo:hi] = piece
                else:
                    self._buf[lo:hi] = mix_pcm(bytes(self._buf[lo:hi]), piece)
                done += chunk

    def read(self, start, end):
        """Frames [start, end) as bytes; what the ring doesn't hold is silence."""
        with self._lock:
            out = bytearray((end - start) * BYTES_PER_FRAME)
            low = max(start, self.head - self.capacity, 0)
            high = min(end, self.head)
            frame = low
            while frame < high:
                offset = frame % self.capacity
                count = min(high - frame, self.capacity - offset)
                dest = (frame - start) * BYTES_PER_FRAME
                out[dest:dest + count * BYTES_PER_FRAME] = \
                    self._buf[offset * BYTES_PER_FRAME:(offset + count) * BYTES_PER_FRAME]
                frame += count
            return bytes(out)

    def clear(self):
        with self._lock:
            self._buf[:] = self._silence


class StreamPlacer:
    """Where in time a helper's chunks of sound belong. A chunk is read some
    time after it was captured, by an unknown and varying delay - but never
    before, so the smallest (wall clock - frames counted so far) seen is the
    best estimate of where the stream starts, and chunks go where that says.
    A gap in the sound (the app paused) makes that difference jump; a jump
    that lasts is taken as the new start."""

    def __init__(self):
        self._offset = None
        self._total = 0
        self._late = 0

    def place(self, now_frame, count):
        """The frame the next `count` frames of this stream start at; now_frame
        is the clock's frame as they were read."""
        after = now_frame - (self._total + count)
        if self._offset is None:
            self._offset = after
        elif after < self._offset:
            self._offset = after
            self._late = 0
        elif after - self._offset > RESYNC_FRAMES:
            self._late += 1
            if self._late >= RESYNC_CHUNKS:
                self._offset, self._late = after, 0
        else:
            self._offset += 1  # follows the capture clock drifting later
            self._late = 0
        late = after - self._offset > RESYNC_FRAMES
        position = (after if late else self._offset) + self._total
        self._total += count
        return position


def capture_command(handle, system=None):
    """The helper command line for a stream handle, or None."""
    system = system or sys.platform
    if handle[0] == "pa":
        _kind, index, source = handle
        cmd = ["parec", f"--monitor-stream={index}", "--raw", "--format=s16le", f"--rate={RATE}",
               f"--channels={CHANNELS}", "--latency-msec=20", "--client-name=ClipEm",
               "--stream-name=ClipEm app track"]
        if source:
            cmd += ["-d", source]
        return cmd
    if handle[0] == "win":
        return [obs_config.APP_CAPTURE_HELPER_PATH, "--pid", str(handle[1])]
    return None


def _process_kwargs():
    if sys.platform == "win32":
        create_no_window = 0x08000000
        return {"creationflags": create_no_window}
    return {}


class CaptureStream:
    """One helper process feeding one ring."""

    def __init__(self, handle, command, ring, clock, monotonic=time.monotonic, popen=subprocess.Popen):
        self.handle = handle
        self.started = monotonic()
        self._command = command
        self._ring = ring
        self._clock = clock
        self._popen = popen
        self._proc = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"capture-{handle[1]}", daemon=True)
        self._thread.start()

    @property
    def alive(self):
        return self._thread.is_alive()

    def _run(self):
        placer = StreamPlacer()
        try:
            self._proc = self._popen(self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, **_process_kwargs())
            read = self._proc.stdout.read
            while not self._stop.is_set():
                data = read(CHUNK_FRAMES * BYTES_PER_FRAME)
                if not data:
                    break
                count = len(data) // BYTES_PER_FRAME
                if count:
                    self._ring.add(placer.place(self._clock.frame(), count), data)
        except OSError as exc:
            log.warning(f"App track capture {self.handle} failed: {exc}")
        except Exception:
            log.exception(f"App track capture {self.handle} crashed")
        finally:
            self._kill()

    def _kill(self):
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # the Windows helper quits when this closes
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        self._kill()


class Snapshot:
    """What the rings held at one moment: the clock's frame then, and each
    app's audio for the `frames` before it."""

    def __init__(self, end_frame, frames, tracks, labels=None):
        self.end_frame = end_frame
        self.frames = frames
        self.tracks = tracks  # app key -> bytes, `frames` long, ending at end_frame; in first-heard order
        self.labels = labels or {}  # app key -> the app's name

    def window(self, key, start, end):
        """Frames [start, end) (the clock's) of an app, silence where the
        snapshot has nothing."""
        pcm = self.tracks.get(key)
        first = self.end_frame - self.frames
        out = bytearray((end - start) * BYTES_PER_FRAME)
        if pcm:
            low, high = max(start, first), min(end, self.end_frame)
            if low < high:
                out[(low - start) * BYTES_PER_FRAME:(high - start) * BYTES_PER_FRAME] = \
                    pcm[(low - first) * BYTES_PER_FRAME:(high - first) * BYTES_PER_FRAME]
        return bytes(out)


class AppRecorder:
    """Keeps a ring per app and a helper per stream. set_targets() is the
    only thing that starts and stops helpers."""

    def __init__(self, seconds=lambda: 60, monotonic=time.monotonic, command_for=capture_command,
                 popen=subprocess.Popen, stream_class=CaptureStream):
        self._seconds = seconds
        self._monotonic = monotonic
        self._command_for = command_for
        self._popen = popen
        self._stream_class = stream_class
        self.clock = SampleClock(monotonic)
        self._rings = {}  # app key -> TrackRing, in the order the apps were first heard
        self._labels = {}  # app key -> name
        self._seen = {}  # app key -> when it last had a stream
        self._streams = {}  # (app key, handle) -> CaptureStream
        self._failed_at = {}  # (app key, handle) -> when its helper last stopped by itself
        self._lock = threading.Lock()

    def ring_seconds(self):
        return int(self._seconds()) + RING_EXTRA_SECONDS

    def set_targets(self, targets):
        """targets: {app key: (label, [stream handles])} - see
        app_audio.app_targets. An app that has no streams any more keeps what
        it recorded until the clip it could be in has passed."""
        with self._lock:
            now = self._monotonic()
            seconds = self.ring_seconds()
            for key, (label, handles) in targets.items():
                if handles:
                    self._seen[key] = now
                    self._labels[key] = label
                    ring = self._rings.get(key)
                    if ring is None or ring.capacity != seconds * RATE:
                        self._rings[key] = TrackRing(seconds)
            for key in list(self._rings):
                if now - self._seen.get(key, now) > seconds:
                    del self._rings[key]
                    self._labels.pop(key, None)
                    self._seen.pop(key, None)

            wanted = {(key, handle) for key, (_label, handles) in targets.items() for handle in handles}
            for stream_key in list(self._streams):
                stream = self._streams[stream_key]
                if stream_key not in wanted:
                    stream.stop()
                    del self._streams[stream_key]
                elif not stream.alive:
                    self._failed_at[stream_key] = now
                    del self._streams[stream_key]
            for stream_key in wanted - set(self._streams):
                if now - self._failed_at.get(stream_key, -RESTART_SECONDS) < RESTART_SECONDS:
                    continue
                command = self._command_for(stream_key[1])
                if command is None:
                    continue
                self._streams[stream_key] = self._stream_class(
                    stream_key[1], command, self._rings[stream_key[0]], self.clock, popen=self._popen)
            self._failed_at = {k: t for k, t in self._failed_at.items() if k in wanted}

    def snapshot(self):
        """A copy of every ring's contents up to now."""
        with self._lock:
            rings = dict(self._rings)
            labels = dict(self._labels)
        end = self.clock.frame()
        tracks = {}
        frames = 0
        for key, ring in rings.items():
            ring.advance(end)
            frames = ring.capacity
            tracks[key] = ring.read(end - frames, end)
        return Snapshot(end, frames, tracks, labels)

    def stop(self):
        """Stops every helper and forgets everything recorded."""
        with self._lock:
            for stream in self._streams.values():
                stream.stop()
            self._streams.clear()
            self._failed_at.clear()
            self._rings.clear()
            self._labels.clear()
            self._seen.clear()
