"""Turn a markdown assistant response into text that reads well aloud."""

import re


def clean_for_speech(text: str) -> str:
    """Strip markdown down to plain sentences that read well aloud."""
    # ellipses are pauses, not sentence ends; normalize "..." to a single
    # "…" so the sentence splitter below never breaks on them
    text = re.sub(r"\.{2,}", "…", text)
    # fenced code blocks -> short spoken placeholder
    text = re.sub(r"```.*?```", " (code block) ", text, flags=re.DOTALL)
    # tables: drop lines that are mostly pipes
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("|"))
    # inline code: keep the content, drop the backticks
    text = re.sub(r"`([^`]*)`", r"\1", text)
    # links/images: keep the label
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # headers, blockquotes, list markers
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    # bold/italic markers
    text = re.sub(r"(\*\*|__|\*|_)(?=\S)", "", text)
    text = re.sub(r"(?<=\S)(\*\*|__|\*|_)", "", text)
    # paths read horribly; shorten deep ones to their last component
    text = re.sub(r"(?:~|/)?(?:[\w.-]+/){2,}([\w.-]+)", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    # one sentence per line: Kokoro segments on newlines and starts playing
    # after the first segment, so this is what makes playback start early.
    # Split only when the next chunk looks like a sentence start (capital,
    # digit, or opening quote/paren), so "e.g. foo" and mid-sentence
    # punctuation don't produce fake segment gaps
    text = re.sub(r"(?<=[.!?;:])\s+(?=[\"'(\[]?[A-Z0-9])", "\n", text)
    return text


def truncate_at_sentence(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # prefer the last sentence end inside the window; say so either way,
    # so a cut never sounds like the intended ending
    m = list(re.finditer(r"[.!?](?:\s|$)", cut))
    if m and m[-1].end() > limit * 0.5:
        cut = cut[: m[-1].end()].strip()
    else:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "\nTruncated."
