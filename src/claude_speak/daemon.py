"""Persistent Kokoro TTS daemon.

Keeps the mlx_audio Kokoro model loaded in memory and listens on a Unix
socket. Messages are played in queue order; the TUI and the Stop hook are
its clients.

Each connection sends one JSON object and gets one JSON reply:

  {"cmd": "speak", "text": "...", "voice": "af_heart", "speed": 1.1}
      enqueue a message                       -> {"ok": true, "id": N}
  {"cmd": "status"}   current item, queue, history, position, rate, paused
  {"cmd": "skip"}     stop the current item, continue with the next
  {"cmd": "play_now", "id": N}   jump to a queued/history item immediately
  {"cmd": "seek", "seconds": -5}   rewind/fast-forward the current item
  {"cmd": "rate", "delta": 0.1}    playback speed (or "value": 1.5); 0.5-3.0
  {"cmd": "spectrum", "bands": 24} band magnitudes at the playback cursor
  {"cmd": "spotify", "action": "playpause"|"next"|"previous"}
  {"cmd": "clear"}    drop all queued items (current keeps playing)
  {"cmd": "pause"} / {"cmd": "resume"}
  {"cmd": "ping"}     liveness check           -> {"ok": true, "pong": true}
  {"cmd": "quit"}     shut the daemon down

Playback goes through a seekable player: the current item's audio is kept in
memory as it is generated, and the stream callback reads from a position
cursor. Speed changes are pitch-preserving (WSOLA time-stretching).

NOTE: must not be started from a sandboxed shell (no CoreAudio access there);
the audio self-test catches that and refuses to start.
"""

import fcntl
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque

from . import config
from .config import (
    LOCK_PATH,
    MAX_HISTORY,
    MAX_QUEUE,
    MAX_RATE,
    MIN_BUFFER_SECONDS,
    MIN_RATE,
    MODEL,
    SOCKET_PATH,
)

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def audio_available() -> bool:
    """A daemon spawned from a sandboxed process has no CoreAudio access.
    Refuse to start in that case, so a later unsandboxed spawn (e.g. from the
    real Stop hook) gets the socket instead of a deaf daemon squatting on it."""
    import sounddevice as sd

    try:
        stream = sd.OutputStream(samplerate=24_000, channels=1)
        stream.start()
        stream.stop()
        stream.close()
        return True
    except Exception as e:
        print(f"no audio output access, not starting: {e}", flush=True)
        return False


def load():
    from mlx_audio.tts.utils import load_model

    try:
        return load_model(MODEL)
    except Exception:
        # model may not be cached yet; retry online
        os.environ["HF_HUB_OFFLINE"] = "0"
        return load_model(MODEL)


class Spotify:
    """Duck (or pause) the local Spotify app while speech plays, and expose
    the current track for the TUI. Uses AppleScript; needs the one-time
    macOS automation approval for controlling Spotify."""

    QUERY = (
        'if application "Spotify" is running then '
        'tell application "Spotify" to return (player state as text) & "|" '
        '& (sound volume as text) & "|" & (name of current track) & "|" '
        "& (artist of current track)"
    )

    def __init__(self):
        self.mode = config.SPOTIFY_MODE
        self.duck_volume = config.SPOTIFY_DUCK_VOLUME
        self.lock = threading.Lock()
        self.active = False  # we are currently ducking/pausing
        self.prev_volume = None
        self.paused_by_us = False
        self._cache = (0.0, None)

    def _osa(self, script):
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=4,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _query(self):
        parts = self._osa(self.QUERY).split("|")
        if len(parts) != 4:
            return None
        try:
            volume = int(parts[1])
        except ValueError:
            volume = 0
        return {
            "state": parts[0],
            "volume": volume,
            "track": parts[2],
            "artist": parts[3],
        }

    def info(self):
        """Cached snapshot for status replies (throttles osascript calls)."""
        if self.mode == "off":
            return None
        with self.lock:
            ts, data = self._cache
            if time.time() - ts > 2.5:
                data = self._query()
                if data is not None:
                    data["ducked"] = self.active
                self._cache = (time.time(), data)
            return data

    def command(self, action):
        """Direct playback control from the TUI (independent of ducking)."""
        scripts = {
            "playpause": 'tell application "Spotify" to playpause',
            "next": 'tell application "Spotify" to next track',
            "previous": 'tell application "Spotify" to previous track',
        }
        if action not in scripts:
            return False
        self._osa(scripts[action])
        with self.lock:
            self._cache = (0.0, None)
        return True

    def duck(self):
        if self.mode == "off":
            return
        with self.lock:
            if self.active:
                return
            q = self._query()
            if not q or q["state"] != "playing":
                return
            if self.mode == "pause":
                self._osa('tell application "Spotify" to pause')
                self.paused_by_us = True
            else:
                self.prev_volume = q["volume"]
                self._osa(
                    f'tell application "Spotify" to set sound volume to {self.duck_volume}'
                )
            self.active = True
            self._cache = (0.0, None)

    def restore(self):
        if self.mode == "off":
            return
        with self.lock:
            if not self.active:
                return
            if self.prev_volume is not None:
                self._osa(
                    f'tell application "Spotify" to set sound volume to {self.prev_volume}'
                )
                self.prev_volume = None
            if self.paused_by_us:
                self._osa('tell application "Spotify" to play')
                self.paused_by_us = False
            self.active = False
            self._cache = (0.0, None)


class SeekPlayer:
    """Plays one item's audio with seek and pitch-preserving speed control.

    The original 24kHz audio (`src`) is the source of truth and grows as
    Kokoro generates. What the stream callback actually plays is `out`: a
    WSOLA time-stretched copy at the current rate (verbatim at 1.0x), so
    faster playback keeps its natural pitch. On seek or rate change, `out` is
    rebuilt from the wanted source position; WSOLA runs ~100x real time, so
    that costs a barely audible moment. `mod_lock` serializes the mutators
    (append/seek/rate/reset) across threads; `lock` guards the data the
    audio callback reads."""

    def __init__(self, sample_rate):
        import numpy as np
        import sounddevice as sd
        from audiotsm import wsola
        from audiotsm.io.array import ArrayReader, ArrayWriter

        self.np = np
        self.sd = sd
        self._wsola = wsola
        self._reader = ArrayReader
        self._writer = ArrayWriter
        self.sample_rate = sample_rate
        self.lock = threading.Lock()
        self.mod_lock = threading.RLock()
        self.src = np.zeros(0, dtype=np.float32)
        self.out = np.zeros(0, dtype=np.float32)
        self.out_pos = 0
        self.out_src_base = 0.0  # src sample where `out` starts
        self.rate = 1.0  # persists across items
        self.generating = False
        self.stream = None
        self.playing = False

    def _stretch(self, samples, rate):
        if abs(rate - 1.0) < 0.01 or len(samples) < 512:
            return samples
        reader = self._reader(samples[self.np.newaxis, :])
        writer = self._writer(1)
        self._wsola(1, speed=rate).run(reader, writer)
        return writer.data[0].astype(self.np.float32)

    def reset(self):
        with self.mod_lock:
            with self.lock:
                self.src = self.np.zeros(0, dtype=self.np.float32)
                self.out = self.np.zeros(0, dtype=self.np.float32)
                self.out_pos = 0
                self.out_src_base = 0.0
                self.generating = True

    def append(self, samples):
        with self.mod_lock:
            stretched = self._stretch(samples, self.rate)
            with self.lock:
                self.src = self.np.concatenate([self.src, samples])
                self.out = self.np.concatenate([self.out, stretched])

    def _src_pos(self):
        # caller holds self.lock; approximate mapping out cursor -> src sample
        return min(self.out_src_base + self.out_pos * self.rate, float(len(self.src)))

    def _rebuild(self, src_start):
        # caller holds mod_lock
        src_start = min(max(0.0, src_start), float(len(self.src)))
        with self.lock:
            tail = self.src[int(src_start):].copy()
        stretched = self._stretch(tail, self.rate)
        with self.lock:
            self.out = stretched
            self.out_pos = 0
            self.out_src_base = src_start

    def seek(self, seconds):
        with self.mod_lock:
            with self.lock:
                target = self._src_pos() + seconds * self.sample_rate
            self._rebuild(target)

    def set_rate(self, value=None, delta=None):
        with self.mod_lock:
            rate = self.rate if value is None else float(value)
            if delta is not None:
                rate += float(delta)
            rate = min(max(MIN_RATE, rate), MAX_RATE)
            if abs(rate - self.rate) > 0.001:
                with self.lock:
                    here = self._src_pos()
                self.rate = rate
                self._rebuild(here)
            return self.rate

    def snapshot(self):
        with self.lock:
            return {
                "position": self._src_pos() / self.sample_rate,
                "duration": len(self.src) / self.sample_rate,
                "rate": round(self.rate, 2),
                "generating": self.generating,
            }

    def remaining(self):
        with self.lock:
            return len(self.out) - self.out_pos

    def spectrum(self, n_bands):
        """Raw magnitude per log-spaced frequency band at the playback
        cursor, for the TUI's analyzer. None when nothing is playing."""
        np = self.np
        with self.lock:
            if not self.playing or len(self.src) == 0:
                return None
            i = int(self._src_pos())
            window = self.src[i : i + 2048].copy()
        if len(window) < 512:
            return None
        window = window * np.hanning(len(window))
        mag = np.abs(np.fft.rfft(window))
        freqs = np.fft.rfftfreq(len(window), 1 / self.sample_rate)
        edges = np.geomspace(60, 8000, n_bands + 1)
        bands = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            sel = mag[(freqs >= lo) & (freqs < hi)]
            bands.append(round(float(sel.mean()), 4) if len(sel) else 0.0)
        return bands

    def finished(self):
        with self.lock:
            return self.out_pos >= len(self.out) and not self.generating

    def _callback(self, outdata, frames, time_info, status):
        outdata.fill(0)
        with self.lock:
            n = min(frames, max(0, len(self.out) - self.out_pos))
            if n > 0:
                outdata[:n, 0] = self.out[self.out_pos : self.out_pos + n]
                self.out_pos += n
            if self.out_pos >= len(self.out) and not self.generating:
                self.playing = False
                raise self.sd.CallbackStop()
            # underrun while still generating: emit silence, keep the stream

    def start_stream(self):
        self.stream = self.sd.OutputStream(
            samplerate=self.sample_rate,
            channels=1,
            callback=self._callback,
            blocksize=2048,
        )
        self.stream.start()
        self.playing = True

    def stop_stream(self):
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        finally:
            self.stream = None
            self.playing = False


class Speaker:
    """Queue worker: plays items in order; skip/seek/rate/pause via commands."""

    def __init__(self, model):
        import numpy as np

        self.np = np
        self.model = model
        self.player = SeekPlayer(model.sample_rate)
        self.spotify = Spotify()
        self.lock = threading.Lock()
        self.wakeup = threading.Condition(self.lock)
        self.queue = deque()
        self.history = deque(maxlen=MAX_HISTORY)
        self.current = None
        self.paused = False
        self.skip_flag = threading.Event()
        self.shutdown = False
        self.next_id = 1
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    # ---- commands (called from the socket thread) ----

    def enqueue(self, text, voice, speed):
        with self.lock:
            item = {
                "id": self.next_id,
                "text": text,
                "voice": voice,
                "speed": speed,
                "ts": time.time(),
            }
            self.next_id += 1
            self.queue.append(item)
            while len(self.queue) > MAX_QUEUE:
                self.queue.popleft()
            self.wakeup.notify()
            return item["id"]

    def skip(self):
        self.skip_flag.set()
        self.player.stop_stream()

    def play_now(self, item_id):
        with self.lock:
            for source in (self.queue, self.history):
                for item in source:
                    if item["id"] == item_id:
                        if item in self.queue:
                            self.queue.remove(item)
                        replay = dict(item, id=self.next_id, ts=time.time())
                        self.next_id += 1
                        self.queue.appendleft(replay)
                        self.wakeup.notify()
                        self.skip_flag.set()
                        self.player.stop_stream()
                        return True
        return False

    def clear(self):
        with self.lock:
            self.queue.clear()

    def pause(self):
        self.paused = True
        self.player.stop_stream()

    def resume(self):
        self.paused = False

    def status(self):
        def trim(item):
            return {**item, "text": item["text"][:200]}

        with self.lock:
            state = {
                "ok": True,
                "paused": self.paused,
                "current": trim(self.current) if self.current else None,
                "queue": [trim(i) for i in self.queue],
                "history": [trim(i) for i in self.history],
            }
        state.update(self.player.snapshot())
        state["spotify"] = self.spotify.info()
        return state

    def stop_all(self):
        self.clear()
        self.shutdown = True
        self.skip_flag.set()
        self.player.stop_stream()
        with self.lock:
            self.wakeup.notify()
        self.spotify.restore()

    # ---- playback internals (worker thread) ----

    def _maybe_start_stream(self, min_samples=0):
        if (
            not self.paused
            and not self.player.playing
            and self.player.remaining() > min_samples
        ):
            try:
                self.player.start_stream()
            except Exception as e:
                # a changed default output device (headphones plugged in,
                # AirPods connected, ...) leaves PortAudio's device list
                # stale and every stream open fails; reinitialize and retry
                print(f"stream open failed ({e}), reinitializing PortAudio", flush=True)
                import sounddevice as sd

                try:
                    sd._terminate()
                    sd._initialize()
                except Exception:
                    pass
                self.player.stream = None
                self.player.playing = False
                self.player.start_stream()

    def _loop(self):
        idle = True
        while not self.shutdown:
            with self.lock:
                while not self.queue and not self.shutdown:
                    self.wakeup.wait()
                if self.shutdown:
                    return
                item = self.queue.popleft()
                self.current = item
            if idle:
                self.spotify.duck()
                idle = False
            self.skip_flag.clear()
            try:
                self._play(item)
            except Exception as e:
                print(f"playback failed for #{item['id']}: {e}", flush=True)
            with self.lock:
                self.history.appendleft(item)
                self.current = None
                empty = not self.queue
            if empty:
                # give the next response a moment before un-ducking Spotify,
                # so quick back-to-back messages don't pump the volume
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not self.shutdown:
                    with self.lock:
                        if self.queue:
                            empty = False
                            break
                    time.sleep(0.1)
                if empty:
                    self.spotify.restore()
                    idle = True

    def _play(self, item):
        player = self.player
        player.reset()
        voice = item["voice"]
        threshold = int(MIN_BUFFER_SECONDS * self.model.sample_rate)
        try:
            for result in self.model.generate(
                text=item["text"],
                voice=voice,
                speed=item["speed"],
                lang_code=voice[0] if voice else "a",
            ):
                if self.skip_flag.is_set():
                    return
                samples = self.np.asarray(result.audio, dtype=self.np.float32)
                player.append(samples.reshape(-1))
                self._maybe_start_stream(min_samples=threshold)
        finally:
            with player.lock:
                player.generating = False
        # drain: wait until played out (or skipped); restart the stream after
        # a pause, since it only stops itself when the item is finished
        while not self.skip_flag.is_set():
            if player.finished() and not player.playing:
                return
            self._maybe_start_stream()
            time.sleep(0.1)


def reply(conn, obj):
    try:
        conn.sendall(json.dumps(obj).encode("utf-8"))
    except OSError:
        pass


def run():
    config.ensure_runtime_dir()

    # single-instance guard: held for the daemon's lifetime
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another daemon holds the lock, exiting", flush=True)
        return

    if not audio_available():
        sys.exit(1)

    # refuse to start if another daemon already owns the socket
    if SOCKET_PATH.exists():
        try:
            probe = socket.socket(socket.AF_UNIX)
            probe.settimeout(1)
            probe.connect(str(SOCKET_PATH))
            probe.sendall(b'{"cmd": "ping"}')
            probe.shutdown(socket.SHUT_WR)
            if probe.recv(64):
                print("daemon already running", flush=True)
                return
        except OSError:
            pass  # stale socket
        SOCKET_PATH.unlink(missing_ok=True)

    print(f"loading {MODEL}...", flush=True)
    model = load()
    # warm up: the first generate compiles Metal kernels and is much slower
    for _ in model.generate(text="Ready.", voice="af_heart", lang_code="a"):
        pass
    speaker = Speaker(model)
    print("model loaded and warm", flush=True)

    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(SOCKET_PATH))
    except OSError:
        # lost a startup race against another daemon instance
        print("socket already bound, exiting", flush=True)
        return
    os.chmod(SOCKET_PATH, 0o600)
    server.listen(8)

    try:
        while True:
            conn, _ = server.accept()
            try:
                conn.settimeout(5)
                chunks = []
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                try:
                    req = json.loads(b"".join(chunks).decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                cmd = req.get("cmd")
                if cmd == "speak" and req.get("text"):
                    item_id = speaker.enqueue(
                        req["text"],
                        req.get("voice", "af_heart"),
                        float(req.get("speed", 1.0)),
                    )
                    reply(conn, {"ok": True, "id": item_id})
                elif cmd == "status":
                    reply(conn, speaker.status())
                elif cmd == "skip":
                    speaker.skip()
                    reply(conn, {"ok": True})
                elif cmd == "play_now":
                    found = speaker.play_now(int(req.get("id", -1)))
                    reply(conn, {"ok": found})
                elif cmd == "spotify":
                    ok = speaker.spotify.command(req.get("action", ""))
                    reply(conn, {"ok": ok})
                elif cmd == "spectrum":
                    n = min(max(int(req.get("bands", 24)), 8), 64)
                    reply(conn, {"ok": True, "bands": speaker.player.spectrum(n)})
                elif cmd == "seek":
                    speaker.player.seek(float(req.get("seconds", 0)))
                    reply(conn, {"ok": True})
                elif cmd == "rate":
                    rate = speaker.player.set_rate(
                        value=req.get("value"), delta=req.get("delta")
                    )
                    reply(conn, {"ok": True, "rate": rate})
                elif cmd == "clear":
                    speaker.clear()
                    reply(conn, {"ok": True})
                elif cmd == "pause":
                    speaker.pause()
                    reply(conn, {"ok": True})
                elif cmd == "resume":
                    speaker.resume()
                    reply(conn, {"ok": True})
                elif cmd == "ping":
                    reply(conn, {"ok": True, "pong": True})
                elif cmd == "quit":
                    reply(conn, {"ok": True, "bye": True})
                    break
                else:
                    reply(conn, {"ok": False, "error": "unknown command"})
            except OSError:
                pass
            finally:
                conn.close()
    finally:
        speaker.stop_all()
        server.close()
        SOCKET_PATH.unlink(missing_ok=True)
