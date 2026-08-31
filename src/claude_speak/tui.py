"""Terminal UI: queue, history, playback controls, spectrum analyzer."""

import curses
import math
import time
from datetime import datetime

from .client import request

STATUS_POLL = 0.3
TICK_MS = 60  # spectrum poll + redraw cadence
SPECTRUM_ROWS = 6
BLOCKS = "▁▂▃▄▅▆▇█"


def preview(item, width):
    text = " ".join(item["text"].split())
    stamp = datetime.fromtimestamp(item["ts"]).strftime("%H:%M:%S")
    line = f"{stamp}  {text}"
    return line[: max(0, width - 1)]


class Analyzer:
    """Holds smoothed bar levels, peak markers, and an adaptive gain."""

    def __init__(self):
        self.levels = []
        self.peaks = []
        self.gain = 1.0

    def update(self, bands):
        n = len(bands) if bands else len(self.levels)
        if len(self.levels) != n:
            self.levels = [0.0] * n
            self.peaks = [0.0] * n
        if bands:
            loudest = max(bands)
            self.gain = max(self.gain * 0.995, loudest, 1e-4)
            raw = [math.log1p(b / self.gain * 20) / math.log1p(20) for b in bands]
        else:
            raw = [0.0] * n
        for i in range(n):
            self.levels[i] = max(raw[i], self.levels[i] * 0.72)
            self.peaks[i] = max(self.peaks[i] - 0.035, self.levels[i])

    def resize(self, n):
        if len(self.levels) != n:
            self.levels = [0.0] * n
            self.peaks = [0.0] * n


def color_for_row(r, rows):
    if not curses.has_colors():
        return 0
    frac = r / max(1, rows - 1)
    if frac > 0.78:
        return curses.color_pair(3)
    if frac > 0.5:
        return curses.color_pair(2)
    return curses.color_pair(1)


def draw_spectrum(stdscr, ana, y, width):
    rows = SPECTRUM_ROWS
    height, _ = stdscr.getmaxyx()
    for b, lvl in enumerate(ana.levels):
        x = 1 + b * 2
        if x >= width - 1:
            break
        cells = lvl * rows
        full = int(cells)
        frac = cells - full
        for r in range(rows):
            yy = y + rows - 1 - r
            if yy >= height:
                continue
            if r < full:
                ch = "█"
            elif r == full and frac > 0.06:
                ch = BLOCKS[min(7, int(frac * 8))]
            else:
                continue
            try:
                stdscr.addstr(yy, x, ch, color_for_row(r, rows))
            except curses.error:
                pass
        pk = int(ana.peaks[b] * rows)
        if pk > full and pk < rows:
            try:
                stdscr.addstr(y + rows - 1 - pk, x, "·", curses.A_DIM)
            except curses.error:
                pass


def draw(stdscr, state, selected, ana):
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    bold = curses.A_BOLD
    dim = curses.A_DIM

    def put(y, x, text, attr=0):
        if 0 <= y < height:
            try:
                stdscr.addnstr(y, x, text, max(0, width - x - 1), attr)
            except curses.error:
                pass

    if state is None:
        put(0, 0, "claude-speak", bold)
        put(2, 0, "daemon not running (it starts with the next response)", dim)
        put(4, 0, "[q] quit", dim)
        return []

    paused = " [PAUSED]" if state["paused"] else ""
    rate = state.get("rate", 1.0)
    header = f"claude-speak{paused}   speed {rate:.1f}x"
    if state.get("device"):
        header += f"   out: {state['device']}"
    spotify = state.get("spotify")
    if spotify and spotify.get("track"):
        tag = "ducked" if spotify.get("ducked") else spotify.get("state", "")
        header += f"   ♫ {spotify['track']} · {spotify['artist']} ({tag})"
    put(0, 0, header, bold)

    draw_spectrum(stdscr, ana, 1, width)
    row = 1 + SPECTRUM_ROWS + 1

    current = state["current"]
    put(row, 0, "now playing", bold)
    row += 1
    if current:
        pos = int(state.get("position", 0))
        dur = int(state.get("duration", 0))
        gen = "+" if state.get("generating") else ""
        clock = f"[{pos // 60}:{pos % 60:02d} / {dur // 60}:{dur % 60:02d}{gen}] "
        put(row, 2, "▶ " + clock + preview(current, width - 6 - len(clock)))
    else:
        put(row, 2, "(silence)", dim)
    row += 2

    # selectable rows: queued items, then history items
    selectable = []
    put(row, 0, f"queue ({len(state['queue'])})", bold)
    row += 1
    if not state["queue"]:
        put(row, 2, "(empty)", dim)
        row += 1
    for i, item in enumerate(state["queue"], 1):
        attr = curses.A_REVERSE if len(selectable) == selected else 0
        put(row, 2, f"{i}. " + preview(item, width - 6), attr)
        selectable.append(item["id"])
        row += 1
    row += 1

    put(row, 0, "played (newest first, enter = replay)", bold)
    row += 1
    if not state["history"]:
        put(row, 2, "(nothing yet)", dim)
        row += 1
    for item in state["history"]:
        if row >= height - 2:
            break
        attr = curses.A_REVERSE if len(selectable) == selected else 0
        put(row, 2, preview(item, width - 4), attr)
        selectable.append(item["id"])
        row += 1

    put(
        height - 1,
        0,
        "[↑↓ enter] play [←→] seek [-+] speed [s] skip [c] clear [p] pause [m] music [n/N] track [q] quit",
        dim,
    )
    return selectable


def _main(stdscr):
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
    stdscr.timeout(TICK_MS)
    selected = 0
    state = None
    last_status = 0.0
    ana = Analyzer()

    while True:
        now = time.monotonic()
        if now - last_status >= STATUS_POLL:
            state = request({"cmd": "status"})
            last_status = now

        _, width = stdscr.getmaxyx()
        n_bands = min(36, max(8, (width - 2) // 2))
        ana.resize(n_bands)
        if state is not None:
            resp = request({"cmd": "spectrum", "bands": n_bands}, timeout=0.5)
            ana.update(resp.get("bands") if resp else None)
        else:
            ana.update(None)

        selectable = draw(stdscr, state, selected, ana)
        if selectable:
            selected = max(0, min(selected, len(selectable) - 1))
        stdscr.refresh()

        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return
        if key == -1:
            continue
        if key in (ord("q"), 27):
            return
        if key == curses.KEY_UP:
            selected = max(0, selected - 1)
        elif key == curses.KEY_DOWN:
            selected += 1
        elif key == curses.KEY_LEFT:
            request({"cmd": "seek", "seconds": -5})
            last_status = 0
        elif key == curses.KEY_RIGHT:
            request({"cmd": "seek", "seconds": 5})
            last_status = 0
        elif key in (ord("+"), ord("=")):
            request({"cmd": "rate", "delta": 0.1})
            last_status = 0
        elif key in (ord("-"), ord("_")):
            request({"cmd": "rate", "delta": -0.1})
            last_status = 0
        elif key == ord("m"):
            request({"cmd": "spotify", "action": "playpause"})
            last_status = 0
        elif key == ord("n"):
            request({"cmd": "spotify", "action": "next"})
            last_status = 0
        elif key == ord("N"):
            request({"cmd": "spotify", "action": "previous"})
            last_status = 0
        elif key == ord("s"):
            request({"cmd": "skip"})
            last_status = 0
        elif key == ord("c"):
            request({"cmd": "clear"})
            last_status = 0
        elif key == ord("p"):
            if state:
                request({"cmd": "resume" if state["paused"] else "pause"})
            last_status = 0
        elif key in (curses.KEY_ENTER, 10, 13) and selectable:
            request({"cmd": "play_now", "id": selectable[selected]})
            selected = 0
            last_status = 0


def run():
    curses.wrapper(_main)
