"""The Claude Code Stop hook: speak the response that just finished.

Reads the hook event JSON from stdin, finds the newest assistant message in
the session transcript that has not been spoken yet, cleans it up, and sends
it to the daemon. Registered in ~/.claude/settings.json by
`claude-speak install-hook`.
"""

import json
import sys
import time

from . import client, config
from .textclean import clean_for_speech, truncate_at_sentence


def last_assistant_text(transcript_path: str) -> tuple[str, str]:
    """Return (uuid, text) of the last assistant message in the transcript."""
    result = ("", "")
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                content = entry.get("message", {}).get("content", [])
                if isinstance(content, str):
                    text = content
                else:
                    text = "\n".join(
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                if text.strip():
                    result = (entry.get("uuid", ""), text)
    except OSError:
        return ("", "")
    return result


def fresh_assistant_text(transcript_path: str) -> str:
    """Return the newest assistant text that we have NOT spoken before.

    The Stop hook can fire before the final message is flushed to the
    transcript, so the newest entry at that instant may be the previous
    response. Poll until an entry appears whose uuid differs from the one we
    last spoke for this transcript, give it a moment to settle (the final
    message can land right after an intermediate one), then speak the newest.
    """
    try:
        state = json.loads(config.STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    last_spoken = state.get(transcript_path, "")

    deadline = time.monotonic() + 4.0
    uuid, text = last_assistant_text(transcript_path)
    while uuid == last_spoken and time.monotonic() < deadline:
        time.sleep(0.25)
        uuid, text = last_assistant_text(transcript_path)
    if not uuid or uuid == last_spoken:
        return ""
    # settle: a newer (final) entry may arrive just after this one
    time.sleep(0.4)
    newer_uuid, newer_text = last_assistant_text(transcript_path)
    if newer_uuid:
        uuid, text = newer_uuid, newer_text

    state[transcript_path] = uuid
    if len(state) > 50:
        state = dict(list(state.items())[-50:])
    try:
        config.ensure_runtime_dir()
        config.STATE_PATH.write_text(json.dumps(state))
    except OSError:
        pass
    return text


def run() -> None:
    if config.OFF_FLAG.exists():
        return
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    transcript_path = hook_input.get("transcript_path", "")
    if not transcript_path:
        return
    text = clean_for_speech(fresh_assistant_text(transcript_path))
    if not text:
        return
    client.speak(truncate_at_sentence(text, config.MAX_CHARS))
