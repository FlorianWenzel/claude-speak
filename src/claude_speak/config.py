"""Paths and settings. Everything is overridable via environment variables."""

import os
from pathlib import Path

RUNTIME_DIR = Path(
    os.environ.get("CLAUDE_SPEAK_DIR", str(Path.home() / ".claude-speak"))
)
SOCKET_PATH = RUNTIME_DIR / "daemon.sock"
LOCK_PATH = RUNTIME_DIR / "daemon.lock"
LOG_PATH = RUNTIME_DIR / "daemon.log"
STATE_PATH = RUNTIME_DIR / "spoken.json"  # last spoken message per transcript
OFF_FLAG = RUNTIME_DIR / "off"  # exists -> hook stays silent

MODEL = os.environ.get("CLAUDE_TTS_MODEL", "mlx-community/Kokoro-82M-bf16")
# pin the output device by name substring (e.g. "MacBook Pro Speakers");
# empty = follow the system default
DEVICE = os.environ.get("CLAUDE_TTS_DEVICE", "")
VOICE = os.environ.get("CLAUDE_TTS_VOICE", "af_heart")
SPEED = float(os.environ.get("CLAUDE_TTS_SPEED", "1.1"))
MAX_CHARS = int(os.environ.get("CLAUDE_TTS_MAX_CHARS", "6000"))

# Spotify ducking: "duck" lowers the volume while speaking, "pause" pauses
# the music, "off" disables the integration entirely.
SPOTIFY_MODE = os.environ.get("CLAUDE_TTS_SPOTIFY", "duck")
# <= 1: fraction of the current volume to duck to (0.6 = keep 60%, music
# stays audible in the background); > 1: absolute Spotify volume (0-100)
SPOTIFY_DUCK = float(os.environ.get("CLAUDE_TTS_SPOTIFY_DUCK", "0.6"))

MAX_QUEUE = 20
MAX_HISTORY = 20
MIN_BUFFER_SECONDS = 0.4  # buffered audio before playback starts
MIN_RATE, MAX_RATE = 0.5, 3.0


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
