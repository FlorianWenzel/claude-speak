"""The `claude-speak` command."""

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import config

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def _hook_command() -> str:
    exe = shutil.which("claude-speak")
    if exe:
        return f"{exe} hook"
    return f'"{sys.executable}" -m claude_speak hook'


def cmd_install_hook(_args) -> int:
    try:
        settings = json.loads(SETTINGS_PATH.read_text())
    except FileNotFoundError:
        settings = {}
    except json.JSONDecodeError:
        print(f"error: {SETTINGS_PATH} is not valid JSON, fix it first")
        return 1

    hooks = settings.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])
    for entry in stop:
        for h in entry.get("hooks", []):
            if "claude-speak" in h.get("command", "") or "claude_speak" in h.get(
                "command", ""
            ):
                print("hook already installed:", h["command"])
                return 0
    stop.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": _hook_command(),
                    "timeout": 15,
                    "statusMessage": "Speaking response",
                }
            ]
        }
    )
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"Stop hook registered in {SETTINGS_PATH}")
    print("Restart Claude Code (or open /hooks once) to load it.")
    return 0


def cmd_uninstall_hook(_args) -> int:
    try:
        settings = json.loads(SETTINGS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        print("nothing to remove")
        return 0
    stop = settings.get("hooks", {}).get("Stop", [])
    kept = []
    removed = 0
    for entry in stop:
        cmds = entry.get("hooks", [])
        if any(
            "claude-speak" in h.get("command", "") or "claude_speak" in h.get("command", "")
            for h in cmds
        ):
            removed += 1
        else:
            kept.append(entry)
    if removed:
        settings["hooks"]["Stop"] = kept
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"removed {removed} hook entr{'y' if removed == 1 else 'ies'}")
    return 0


def cmd_say(args) -> int:
    from . import client

    text = " ".join(args.text)
    if not text:
        print("usage: claude-speak say <text>")
        return 1
    client.speak(text)
    return 0


def cmd_off(_args) -> int:
    config.ensure_runtime_dir()
    config.OFF_FLAG.touch()
    print("muted (responses will not be spoken)")
    return 0


def cmd_on(_args) -> int:
    config.OFF_FLAG.unlink(missing_ok=True)
    print("unmuted")
    return 0


def cmd_quit(_args) -> int:
    from . import client

    resp = client.request({"cmd": "quit"})
    print("daemon stopped" if resp else "daemon was not running")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="claude-speak",
        description="Read Claude Code responses aloud with local Kokoro TTS.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("tui", help="open the terminal UI (default)")
    sub.add_parser("daemon", help="run the TTS daemon in the foreground")
    sub.add_parser("hook", help="Claude Code Stop hook entry point (stdin JSON)")
    deliver = sub.add_parser("deliver", help=argparse.SUPPRESS)
    deliver.add_argument("payload")
    say = sub.add_parser("say", help="speak arbitrary text through the daemon")
    say.add_argument("text", nargs="+")
    sub.add_parser("install-hook", help="register the Stop hook in ~/.claude/settings.json")
    sub.add_parser("uninstall-hook", help="remove the Stop hook again")
    sub.add_parser("off", help="mute (keep the hook, skip speaking)")
    sub.add_parser("on", help="unmute")
    sub.add_parser("quit", help="stop the daemon")
    args = parser.parse_args()

    command = args.command or "tui"
    if command == "tui":
        from . import tui

        tui.run()
        return 0
    if command == "daemon":
        from . import daemon

        daemon.run()
        return 0
    if command == "hook":
        from . import hook

        hook.run()
        return 0
    if command == "deliver":
        from . import client

        client.deliver_with_retry(args.payload)
        return 0
    return {
        "say": cmd_say,
        "install-hook": cmd_install_hook,
        "uninstall-hook": cmd_uninstall_hook,
        "off": cmd_off,
        "on": cmd_on,
        "quit": cmd_quit,
    }[command](args)


if __name__ == "__main__":
    sys.exit(main())
