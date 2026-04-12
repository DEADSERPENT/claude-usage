"""
tui.py - Interactive Terminal User Interface for claude-usage.

A persistent, updating terminal display (like htop) with keyboard navigation.
Uses ANSI escape codes for cross-platform support.
"""

import sys
import time
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from config import DB_PATH, DAILY_LIMIT_USD, calc_cost


def _enable_ansi():
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass


def _get_terminal_size():
    try:
        import shutil
        cols, rows = shutil.get_terminal_size()
        return cols, rows
    except Exception:
        return 80, 24


def _kbhit():
    """Non-blocking keyboard check."""
    if sys.platform == "win32":
        import msvcrt
        return msvcrt.kbhit()
    else:
        import select
        return select.select([sys.stdin], [], [], 0)[0] != []


def _getch():
    """Read a single character."""
    if sys.platform == "win32":
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b'\x00', b'\xe0'):
            ch2 = msvcrt.getch()
            if ch2 == b'H': return 'UP'
            if ch2 == b'P': return 'DOWN'
            if ch2 == b'K': return 'LEFT'
            if ch2 == b'M': return 'RIGHT'
            return ''
        return ch.decode('utf-8', errors='replace')
    else:
        import tty, termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                ch2 = sys.stdin.read(1)
                if ch2 == '[':
                    ch3 = sys.stdin.read(1)
                    if ch3 == 'A': return 'UP'
                    if ch3 == 'B': return 'DOWN'
                    if ch3 == 'C': return 'RIGHT'
                    if ch3 == 'D': return 'LEFT'
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


ESC = '\033'
CLEAR = f'{ESC}[2J{ESC}[H'
BOLD = f'{ESC}[1m'
DIM = f'{ESC}[2m'
RST = f'{ESC}[0m'
RED = f'{ESC}[31m'
GREEN = f'{ESC}[32m'
YELLOW = f'{ESC}[33m'
BLUE = f'{ESC}[34m'
CYAN = f'{ESC}[36m'
WHITE = f'{ESC}[37m'
BG_SELECT = f'{ESC}[48;5;236m'

BRAILLE_CHARS = ' ⣀⣤⣶⣿'


def _spark(values, width=20):
    """Generate a spark line from values."""
    if not values:
        return ' ' * width
    mx = max(values) or 1
    chars = '▁▂▃▄▅▆▇█'
    result = ''
    step = max(1, len(values) // width)
    sampled = values[::step][:width]
    for v in sampled:
        idx = int((v / mx) * (len(chars) - 1))
        result += chars[idx]
    return result.ljust(width)


def _bar(value, maximum, width=30, fill_char='█', empty_char='░'):
    if maximum <= 0:
        return empty_char * width
    pct = min(1.0, value / maximum)
    filled = int(pct * width)
    return fill_char * filled + empty_char * (width - filled)


def _fmt(n):
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(n)


def _load_data(db_path):
    """Load all data needed for TUI display."""
    if not db_path.exists():
        return None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    today_str = date.today().isoformat()

    today_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
               COUNT(*) as turns, COUNT(DISTINCT session_id) as sessions
        FROM turns WHERE substr(timestamp, 1, 10) = ?
        GROUP BY model ORDER BY inp + out DESC
    """, (today_str,)).fetchall()

    sessions = conn.execute("""
        SELECT session_id, project_name, git_branch, model, turn_count,
               total_input_tokens, total_output_tokens,
               total_cache_read, total_cache_creation,
               first_timestamp, last_timestamp
        FROM sessions ORDER BY last_timestamp DESC LIMIT 50
    """).fetchall()

    hourly = conn.execute("""
        SELECT substr(timestamp, 12, 2) as hour,
               SUM(input_tokens + output_tokens) as tokens
        FROM turns WHERE substr(timestamp, 1, 10) = ?
        GROUP BY hour ORDER BY hour
    """, (today_str,)).fetchall()

    cut15 = (datetime.utcnow() - timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%S')
    burn_row = conn.execute(
        "SELECT SUM(input_tokens + output_tokens) as t FROM turns WHERE timestamp >= ?",
        (cut15,)
    ).fetchone()

    conn.close()

    return {
        "today_rows": [dict(r) for r in today_rows],
        "sessions": [dict(r) for r in sessions],
        "hourly": [dict(r) for r in hourly],
        "burn_15m": (burn_row["t"] or 0) if burn_row else 0,
        "today_str": today_str,
    }


class TUI:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.running = True
        self.view = 'overview'  # overview, sessions, hourly
        self.selected = 0
        self.scroll_offset = 0
        self.data = None
        self.last_refresh = 0

    def run(self):
        _enable_ansi()
        print(CLEAR, end='', flush=True)

        try:
            while self.running:
                now = time.time()
                if now - self.last_refresh > 2:
                    self.data = _load_data(self.db_path)
                    self.last_refresh = now

                self._render()

                start = time.time()
                while time.time() - start < 0.5:
                    if _kbhit():
                        key = _getch()
                        self._handle_key(key)
                    time.sleep(0.05)

        except KeyboardInterrupt:
            pass
        finally:
            print(f'{ESC}[?25h', end='')  # show cursor
            print(RST)
            print("\n  TUI stopped.\n")

    def _handle_key(self, key):
        if key in ('q', 'Q', '\x1b'):
            self.running = False
        elif key in ('r', 'R'):
            self.data = _load_data(self.db_path)
            self.last_refresh = time.time()
        elif key in ('1',):
            self.view = 'overview'
            self.selected = 0
        elif key in ('2',):
            self.view = 'sessions'
            self.selected = 0
            self.scroll_offset = 0
        elif key in ('3',):
            self.view = 'hourly'
        elif key == 'UP':
            if self.selected > 0:
                self.selected -= 1
                if self.selected < self.scroll_offset:
                    self.scroll_offset = self.selected
        elif key == 'DOWN':
            max_items = len(self.data["sessions"]) if self.data else 0
            if self.selected < max_items - 1:
                self.selected += 1
                cols, rows = _get_terminal_size()
                visible = rows - 10
                if self.selected >= self.scroll_offset + visible:
                    self.scroll_offset = self.selected - visible + 1
        elif key in ('t', 'T', '\t'):
            views = ['overview', 'sessions', 'hourly']
            idx = views.index(self.view)
            self.view = views[(idx + 1) % len(views)]
            self.selected = 0

    def _render(self):
        cols, rows = _get_terminal_size()
        lines = []

        # Header
        now_str = datetime.now().strftime('%H:%M:%S')
        header = f" {BOLD}{CYAN}Claude Code Usage TUI{RST}  {DIM}{now_str}{RST}"
        view_tabs = f" {self._tab('1:Overview', 'overview')} {self._tab('2:Sessions', 'sessions')} {self._tab('3:Hourly', 'hourly')}  {DIM}q:quit  r:refresh  tab:switch{RST}"
        lines.append(header)
        lines.append(view_tabs)
        lines.append(f" {'─' * (cols - 2)}")

        if not self.data:
            lines.append(f" {YELLOW}No data. Run: python cli.py scan{RST}")
        elif self.view == 'overview':
            lines.extend(self._render_overview(cols, rows))
        elif self.view == 'sessions':
            lines.extend(self._render_sessions(cols, rows))
        elif self.view == 'hourly':
            lines.extend(self._render_hourly(cols, rows))

        # Pad to fill screen
        while len(lines) < rows - 1:
            lines.append('')

        # Status bar
        status = f" {DIM}Refreshing every 2s │ Today: {self.data['today_str'] if self.data else '?'}{RST}"
        lines.append(status)

        output = CLEAR + '\n'.join(line[:cols] for line in lines[:rows])
        sys.stdout.write(output)
        sys.stdout.flush()

    def _tab(self, label, view_name):
        if self.view == view_name:
            return f'{BG_SELECT}{BOLD} {label} {RST}'
        return f'{DIM} {label} {RST}'

    def _render_overview(self, cols, rows):
        lines = []
        d = self.data

        t_inp = t_out = t_cr = t_cc = t_turns = t_sess = 0
        t_cost = 0.0
        for r in d["today_rows"]:
            inp, out = r["inp"] or 0, r["out"] or 0
            cr, cc = r["cr"] or 0, r["cc"] or 0
            t_inp += inp; t_out += out; t_cr += cr; t_cc += cc
            t_turns += r["turns"]; t_sess += r["sessions"]
            t_cost += calc_cost(r["model"], inp, out, cr, cc)

        burn_pm = d["burn_15m"] / 15

        lines.append(f"")
        lines.append(f" {BOLD}Today's Usage{RST}")
        lines.append(f" {'─' * 50}")
        lines.append(f" Sessions: {CYAN}{t_sess}{RST}   Turns: {CYAN}{t_turns}{RST}   Cost: {GREEN}${t_cost:.4f}{RST}")
        lines.append(f" Input: {_fmt(t_inp)}   Output: {_fmt(t_out)}   Cache R: {_fmt(t_cr)}   Cache W: {_fmt(t_cc)}")
        lines.append(f" Burn Rate: {YELLOW}{_fmt(int(burn_pm))}/min{RST}")

        if DAILY_LIMIT_USD > 0:
            pct = min(100, (t_cost / DAILY_LIMIT_USD) * 100)
            bar = _bar(t_cost, DAILY_LIMIT_USD, 30)
            color = RED if pct > 80 else YELLOW if pct > 50 else GREEN
            lines.append(f"")
            lines.append(f" Daily Budget: {color}{bar}{RST} {pct:.1f}% of ${DAILY_LIMIT_USD:.2f}")

        lines.append(f"")
        lines.append(f" {BOLD}By Model{RST}")
        lines.append(f" {'─' * 50}")
        lines.append(f" {'MODEL':<30} {'INPUT':>9} {'OUTPUT':>9} {'COST':>10}")

        for r in d["today_rows"]:
            inp, out = r["inp"] or 0, r["out"] or 0
            cost = calc_cost(r["model"], inp, out, r["cr"] or 0, r["cc"] or 0)
            lines.append(f" {r['model'][:28]:<30} {_fmt(inp):>9} {_fmt(out):>9} {GREEN}${cost:>9.4f}{RST}")

        # Hourly sparkline
        if d["hourly"]:
            lines.append(f"")
            lines.append(f" {BOLD}Hourly Activity{RST}")
            tokens = [h["tokens"] or 0 for h in d["hourly"]]
            hours = [h["hour"] for h in d["hourly"]]
            spark = _spark(tokens, min(40, cols - 10))
            lines.append(f" {CYAN}{spark}{RST}")
            if hours:
                lines.append(f" {DIM}{hours[0]}:00{'':>{len(spark)-8}}{hours[-1]}:00{RST}")

        return lines

    def _render_sessions(self, cols, rows):
        lines = []
        sessions = self.data["sessions"]
        visible_rows = rows - 8

        lines.append(f"")
        lines.append(f" {BOLD}Sessions{RST} ({len(sessions)} total)  {DIM}↑↓ navigate{RST}")
        lines.append(f" {'─' * (cols - 2)}")
        lines.append(f" {'SESSION':<10} {'PROJECT':<25} {'MODEL':<22} {'TURNS':>6} {'TOKENS':>10} {'COST':>10}")

        visible = sessions[self.scroll_offset:self.scroll_offset + visible_rows]
        for i, s in enumerate(visible):
            idx = i + self.scroll_offset
            tokens = (s["total_input_tokens"] or 0) + (s["total_output_tokens"] or 0)
            cost = calc_cost(s["model"], s["total_input_tokens"] or 0,
                           s["total_output_tokens"] or 0,
                           s["total_cache_read"] or 0,
                           s["total_cache_creation"] or 0)

            sid = (s["session_id"] or "")[:8]
            proj = (s["project_name"] or "unknown")[:23]
            model = (s["model"] or "unknown")[:20]

            prefix = f'{BG_SELECT}' if idx == self.selected else ''
            suffix = RST if idx == self.selected else ''

            lines.append(f" {prefix}{sid:<10} {proj:<25} {model:<22} {s['turn_count'] or 0:>6} {_fmt(tokens):>10} ${cost:>9.4f}{suffix}")

        if self.selected < len(sessions):
            s = sessions[self.selected]
            lines.append(f" {'─' * (cols - 2)}")
            lines.append(f" {DIM}Selected: {s['session_id'][:16]}  Branch: {s.get('git_branch') or 'none'}{RST}")

        return lines

    def _render_hourly(self, cols, rows):
        lines = []
        hourly = self.data["hourly"]

        lines.append(f"")
        lines.append(f" {BOLD}Hourly Token Distribution{RST}")
        lines.append(f" {'─' * (cols - 2)}")

        if not hourly:
            lines.append(f" {DIM}No hourly data today.{RST}")
            return lines

        max_tokens = max((h["tokens"] or 0) for h in hourly) or 1
        bar_width = cols - 20

        for h in hourly:
            tokens = h["tokens"] or 0
            hour_label = f" {h['hour']}:00"
            bar_len = int((tokens / max_tokens) * bar_width)
            bar = '█' * bar_len
            color = GREEN if tokens < max_tokens * 0.5 else YELLOW if tokens < max_tokens * 0.8 else RED
            lines.append(f"{hour_label} {color}{bar}{RST} {_fmt(tokens)}")

        return lines


def run_tui(db_path=DB_PATH):
    tui = TUI(db_path)
    tui.run()
