# claude-speak

Read Claude Code responses aloud with a fully local TTS pipeline on macOS.
When Claude finishes a response, it gets spoken through
[Kokoro](https://huggingface.co/mlx-community/Kokoro-82M-bf16) running on
Apple silicon via [mlx-audio](https://github.com/Blaizzy/mlx-audio). No cloud,
no API keys, no audio leaves your machine.

```
claude-speak   speed 1.3x   ♫ Silk Tone · Eden (ducked)
     ▂█▅▃▂
 ▁▃▅███████▅▃▂▁        ·  ·
▃███████████████▅▃▂▁▂▃▅▅▃▂▁▁

now playing
  ▶ [0:04 / 0:23] 14:32:07  Done. The bug was in the retry loop...

queue (1)
  1. 14:32:41  Tests pass now. Three files changed...

played (newest first, enter = replay)
  14:29:55  I found the problem: the config loader...
```

## What it does

- **Speaks every finished Claude Code response**, starting well under a second
  after the response completes (sentence-streamed synthesis: it starts talking
  while the rest is still being generated)
- **Message queue**: responses play in order; nothing gets cut off by the next one
- **Terminal UI** with a live spectrum analyzer, queue, and replay history
- **Playback controls**: skip, replay, pause, rewind/fast-forward, and
  pitch-preserving speed (WSOLA time-stretching, 0.5x to 3x)
- **Spotify ducking**: your music dips while Claude talks and comes back after
  (plus play/pause/next/previous from the TUI)
- **Multi-session aware**: hooks from several Claude Code sessions feed one queue

Everything runs in a small daemon that keeps the model warm in memory
(time-to-speech is ~1s warm), controlled over a Unix socket.

## Requirements

- macOS on Apple silicon (mlx is Apple-silicon-only)
- Python 3.11+
- [Claude Code](https://claude.com/claude-code)

## Install

With [uv](https://docs.astral.sh/uv/) (`brew install uv` if you don't have it):

```sh
# latest release:
uv tool install https://github.com/FlorianWenzel/claude-speak/releases/latest/download/claude_speak-0.1.2-py3-none-any.whl
# or straight from git (main):
uv tool install git+https://github.com/FlorianWenzel/claude-speak
```

Prefer pipx? `pipx install git+https://github.com/FlorianWenzel/claude-speak`
works the same way. Wheels and sdists for every version are on the
[releases page](https://github.com/FlorianWenzel/claude-speak/releases).

To update later: `uv tool upgrade claude-speak` (git installs) or re-run the
install command with the new release URL.

Then register the Stop hook (this edits `~/.claude/settings.json`):

```sh
claude-speak install-hook
```

Restart Claude Code (or open `/hooks` once in an existing session). Done: the
next finished response is spoken. The first ever playback downloads the
Kokoro model (~300MB) and takes a moment; after that the daemon starts on
demand and stays warm.

## The TUI

```sh
claude-speak        # or: claude-speak tui
```

| Key | Action |
| --- | --- |
| up/down + enter | select a queued or played message and play it now |
| left / right | rewind / fast-forward 5 seconds |
| `-` / `+` | playback speed down / up (pitch-preserving) |
| `s` | skip the current message |
| `c` | clear the queue |
| `p` | pause / resume speech |
| `m` | play / pause Spotify |
| `n` / `N` | Spotify next / previous track |
| `q` | quit the TUI (speech keeps running) |

## Other commands

```sh
claude-speak say "hello there"   # speak arbitrary text
claude-speak off                 # mute (hook stays registered)
claude-speak on                  # unmute
claude-speak quit                # stop the daemon
claude-speak uninstall-hook      # remove the hook from settings.json
```

## Configuration

Environment variables (set them where Claude Code and the daemon start, e.g.
your shell profile or the `env` block in `~/.claude/settings.json`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLAUDE_TTS_VOICE` | `af_heart` | Kokoro voice |
| `CLAUDE_TTS_SPEED` | `1.1` | base voice speed (baked into synthesis) |
| `CLAUDE_TTS_MODEL` | `mlx-community/Kokoro-82M-bf16` | TTS model |
| `CLAUDE_TTS_MAX_CHARS` | `6000` | cap per spoken message |
| `CLAUDE_TTS_SPOTIFY` | `duck` | `duck`, `pause`, or `off` |
| `CLAUDE_TTS_SPOTIFY_DUCK` | `12` | Spotify volume while speaking |
| `CLAUDE_TTS_DEVICE` | (system default) | pin the output device by name substring |
| `CLAUDE_SPEAK_DIR` | `~/.claude-speak` | socket, logs, state |

## How it works

```
Claude Code ──Stop hook──> claude-speak hook ──unix socket──> daemon
                                                              ├─ Kokoro (mlx)
                                                              ├─ seekable player
                                                              ├─ Spotify (osascript)
                                                              └─ spectrum FFT
                                        claude-speak tui <────┘
```

- The **hook** reads the session transcript, finds the newest assistant
  message it hasn't spoken yet (deduplicated by message uuid, so nothing is
  spoken twice or one response late), strips markdown into listenable
  sentences, and enqueues it. Code blocks become "(code block)", tables are
  dropped, deep paths are shortened to their basename.
- The **daemon** synthesizes sentence by sentence and starts playing after the
  first one. Audio is kept in memory, so seeking backwards and re-stretching
  at a new speed are instant. If opening the output stream fails (default
  audio device changed), it reinitializes PortAudio and retries.
- **Spotify** is controlled via AppleScript; the first use asks for macOS
  automation permission.

## Troubleshooting

- **No audio, daemon log says "no audio output access"**: the daemon was
  spawned from a sandboxed process (it refuses to start deaf). Let the hook
  start it, or run `claude-speak daemon` from a regular terminal.
- **Nothing spoken in an existing Claude Code session**: hooks load at session
  start; open `/hooks` once or restart the session.
- **Spotify not ducking**: check System Settings, Privacy & Security,
  Automation, and allow your terminal/python to control Spotify.
- Daemon log: `~/.claude-speak/daemon.log`

## Releasing (maintainers)

Tag a version and push it; CI builds the package and creates a GitHub
release with the wheel and sdist attached:

```sh
git tag v0.2.0 && git push origin v0.2.0
```

PyPI publishing is wired up but off by default: add a
[trusted publisher](https://docs.pypi.org/trusted-publishers/) for this repo
and the `release.yml` workflow on pypi.org, then set the repository variable
`PUBLISH_PYPI` to `true`.

## License

MIT
