"""Socket client for the daemon, plus on-demand daemon spawning."""

import json
import socket
import subprocess
import sys
import time

from . import config


def request(obj, timeout=2.0):
    """Send one JSON request, return the parsed JSON reply (or None)."""
    try:
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(timeout)
        conn.connect(str(config.SOCKET_PATH))
        conn.sendall(json.dumps(obj).encode("utf-8"))
        conn.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        conn.close()
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def spawn_daemon() -> None:
    config.ensure_runtime_dir()
    with open(config.LOG_PATH, "a") as log:
        subprocess.Popen(
            [sys.executable, "-m", "claude_speak", "daemon"],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def speak(text, voice=None, speed=None) -> None:
    """Enqueue text; if the daemon is down, start it and retry detached so
    the caller (a Claude Code hook) returns immediately."""
    message = {
        "cmd": "speak",
        "text": text,
        "voice": voice or config.VOICE,
        "speed": config.SPEED if speed is None else speed,
    }
    resp = request(message)
    if resp and resp.get("ok"):
        return
    payload = json.dumps(message)
    spawn_daemon()
    subprocess.Popen(
        [sys.executable, "-m", "claude_speak", "deliver", payload],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def deliver_with_retry(payload: str, deadline_seconds=60) -> None:
    """Detached child: wait for the daemon to come up, then deliver."""
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        resp = request(obj)
        if resp and resp.get("ok"):
            return
        time.sleep(0.5)
