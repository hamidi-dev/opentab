"""A pure-Python ``curses`` for running OpenTab's TUI in the browser.

Pyodide ships no ``_curses``. This module implements the subset OpenTab uses on top
of a terminal emulator (xterm.js) reached through the ``otterm`` JS module:

    otterm.write(str)            ANSI output
    otterm.read(timeout_ms)      raw input received so far ('' on timeout)
    otterm.size()                [rows, cols]
    otterm.sleep(ms)             a real (Atomics.wait) sleep in the worker

One screen, one window. Output is diffed per refresh and painted with truecolor SGR,
so OpenTab's init_color palette shows exactly. Input is decoded from xterm's byte
stream: CSI/SS3 keys, SGR mouse reports (clicks and double-clicks synthesised the way
ncurses does with mouseinterval), and a private resize report.
"""

import time as _time
import unicodedata as _ud

import otterm as _js

# ---------------------------------------------------------------------- constants

class error(Exception):
    pass


A_NORMAL = 0
A_ATTRIBUTES = 0xFFFFFF00
A_CHARTEXT = 0xFF
A_COLOR = 0xFF00
A_STANDOUT = 0x10000
A_UNDERLINE = 0x20000
A_REVERSE = 0x40000
A_BLINK = 0x80000
A_DIM = 0x100000
A_BOLD = 0x200000
A_ALTCHARSET = 0x400000
A_INVIS = 0x800000
A_PROTECT = 0x1000000
A_ITALIC = 0x80000000

COLOR_BLACK, COLOR_RED, COLOR_GREEN, COLOR_YELLOW = 0, 1, 2, 3
COLOR_BLUE, COLOR_MAGENTA, COLOR_CYAN, COLOR_WHITE = 4, 5, 6, 7
COLORS = 256
COLOR_PAIRS = 256

KEY_MIN = 257
KEY_BREAK = 257
KEY_DOWN, KEY_UP, KEY_LEFT, KEY_RIGHT, KEY_HOME, KEY_BACKSPACE = 258, 259, 260, 261, 262, 263
KEY_F0 = 264
for _n in range(64):
    globals()[f"KEY_F{_n}"] = KEY_F0 + _n
KEY_DC, KEY_IC = 330, 331
KEY_NPAGE, KEY_PPAGE = 338, 339
KEY_ENTER = 343
KEY_BTAB = 353
KEY_END = 360
KEY_MOUSE = 409
KEY_RESIZE = 410
KEY_MAX = 511

# ncurses mouse version 2: five bits per button.
def _mb(button, bits):
    return bits << ((button - 1) * 5)

for _b in range(1, 6):
    globals()[f"BUTTON{_b}_RELEASED"] = _mb(_b, 0o1)
    globals()[f"BUTTON{_b}_PRESSED"] = _mb(_b, 0o2)
    globals()[f"BUTTON{_b}_CLICKED"] = _mb(_b, 0o4)
    globals()[f"BUTTON{_b}_DOUBLE_CLICKED"] = _mb(_b, 0o10)
    globals()[f"BUTTON{_b}_TRIPLE_CLICKED"] = _mb(_b, 0o20)
BUTTON_CTRL = _mb(6, 0o1)
BUTTON_SHIFT = _mb(6, 0o2)
BUTTON_ALT = _mb(6, 0o4)
REPORT_MOUSE_POSITION = _mb(6, 0o10)
ALL_MOUSE_EVENTS = REPORT_MOUSE_POSITION - 1

# VT100 alternate charset, as ncurses encodes it: A_ALTCHARSET | the ACS letter.
_ACS_GLYPH = {"l": "┌", "k": "┐", "m": "└", "j": "┘", "q": "─", "x": "│", "t": "├", "u": "┤", "w": "┬",
              "v": "┴", "n": "┼", "0": "█", "a": "▒", "h": "▒", "~": "·", "`": "◆", "f": "°",
              ",": "←", "+": "→", "-": "↑", ".": "↓"}
for _name, _letter in {"ULCORNER": "l", "URCORNER": "k", "LLCORNER": "m", "LRCORNER": "j", "HLINE": "q",
                       "VLINE": "x", "LTEE": "t", "RTEE": "u", "TTEE": "w", "BTEE": "v", "PLUS": "n",
                       "BLOCK": "0", "CKBOARD": "a", "BOARD": "h", "BULLET": "~", "DIAMOND": "`",
                       "DEGREE": "f", "LARROW": ",", "RARROW": "+", "UARROW": "-", "DARROW": "."}.items():
    globals()["ACS_" + _name] = A_ALTCHARSET | ord(_letter)

LINES, COLS = 24, 80

# ---------------------------------------------------------------------- colour state

def _xterm256():
    base = [
        (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
        (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
    ]
    steps = [0, 95, 135, 175, 215, 255]
    cube = [(steps[i // 36], steps[(i // 6) % 6], steps[i % 6]) for i in range(216)]
    grey = [(8 + 10 * i,) * 3 for i in range(24)]
    return base + cube + grey


_palette = _xterm256()
_pairs = {0: (-1, -1)}
_cursor_visible = 1
_escdelay_ms = 25
_mouse_mask = 0
_mouse_interval_ms = 166
_mouse_events = []
_key_queue = []  # ungetch / unget_wch / synthesised keys, consumed first
_inbuf = ""
_last_click = (0.0, -1, -1)
_idle_hook = None  # set by the bootstrap: runs one deferred "thread" job, True if it did
_screen = None


def _rgb(index):
    if index is None or index < 0:
        return None
    return _palette[index] if index < len(_palette) else _palette[index % 256]


def start_color():
    global COLORS, COLOR_PAIRS
    COLORS, COLOR_PAIRS = 256, 256


def has_colors():
    return True


def can_change_color():
    return True


def use_default_colors():
    _pairs[0] = (-1, -1)


def assume_default_colors(fg, bg):
    _pairs[0] = (fg, bg)


def init_color(n, r, g, b):
    if not 0 <= n < COLORS:
        raise ValueError("color number out of range")
    _palette[n] = tuple(max(0, min(255, round(c * 255 / 1000))) for c in (r, g, b))


def color_content(n):
    return tuple(round(c * 1000 / 255) for c in _rgb(n) or (0, 0, 0))


def init_pair(n, fg, bg):
    if not 1 <= n < COLOR_PAIRS:
        raise ValueError("pair number out of range")
    if fg >= COLORS or bg >= COLORS:
        raise ValueError("color out of range")
    _pairs[n] = (fg, bg)
    if _screen is not None:
        _screen._dirty_all = True  # live theme switch: repaint every cell


def pair_content(n):
    return _pairs.get(n, (-1, -1))


def color_pair(n):
    return (n << 8) & A_COLOR


def pair_number(attr):
    return (attr & A_COLOR) >> 8


# ---------------------------------------------------------------------- terminal-wide

def curs_set(visibility):
    global _cursor_visible
    old, _cursor_visible = _cursor_visible, visibility
    return old


def set_escdelay(ms):
    global _escdelay_ms
    _escdelay_ms = ms


def get_escdelay():
    return _escdelay_ms


def mousemask(mask):
    global _mouse_mask
    old, _mouse_mask = _mouse_mask, mask
    _js.write("\x1b[?1000h\x1b[?1006h" if mask else "\x1b[?1000l\x1b[?1006l")
    return mask, old


def mouseinterval(ms):
    global _mouse_interval_ms
    old, _mouse_interval_ms = _mouse_interval_ms, ms
    return old


def getmouse():
    if not _mouse_events:
        raise error("getmouse() returned ERR")
    return _mouse_events.pop(0)


def ungetmouse(id, x, y, z, bstate):
    _mouse_events.append((id, x, y, z, bstate))
    _key_queue.append(KEY_MOUSE)


def ungetch(ch):
    _key_queue.insert(0, ord(ch) if isinstance(ch, str) else ch)


def unget_wch(ch):
    _key_queue.insert(0, ch)


def napms(ms):
    _js.sleep(int(ms))
    return 0


def beep():
    pass


def flash():
    pass


def def_prog_mode():
    pass


def reset_prog_mode():
    if _screen is not None:
        _screen._dirty_all = True


def def_shell_mode():
    pass


def reset_shell_mode():
    pass


def noecho():
    pass


def echo():
    pass


def cbreak():
    pass


def nocbreak():
    pass


def raw():
    pass


def noraw():
    pass


def nl():
    pass


def nonl():
    pass


def meta(flag):
    pass


def doupdate():
    if _screen is not None:
        _screen.refresh()


def isendwin():
    return _screen is None


def setupterm(term=None, fd=-1):
    pass


def tigetnum(cap):
    return {"colors": 256, "pairs": 256, "cols": COLS, "lines": LINES}.get(cap, -2)


def tigetflag(cap):
    return 1 if cap in ("ccc", "am", "bce") else 0


def tigetstr(cap):
    return None


def termname():
    return b"xterm-256color"


def longname():
    return b"xterm with 256 colors"


def has_key(ch):
    return True


def update_lines_cols():
    global LINES, COLS
    LINES, COLS = _size()


def resizeterm(lines, cols):
    if _screen is not None:
        _screen._resize(lines, cols)


def resize_term(lines, cols):
    resizeterm(lines, cols)


def is_term_resized(lines, cols):
    return (lines, cols) != _size()


def _size():
    rows, cols = _js.size()
    return max(1, int(rows)), max(1, int(cols))


def initscr():
    global _screen
    update_lines_cols()
    _screen = window(LINES, COLS)
    _js.write("\x1b[?1049h\x1b[?25l\x1b[2J")
    return _screen


def endwin():
    global _screen
    mousemask(0)
    _js.write("\x1b[0m\x1b[?25h\x1b[2J\x1b[H\x1b[?1049l")
    _screen = None


def newwin(*args):
    raise error("OpenTab-in-the-browser supports a single screen")


def wrapper(func, *args, **kwargs):
    stdscr = initscr()
    try:
        stdscr.keypad(True)
        start_color()
        return func(stdscr, *args, **kwargs)
    finally:
        endwin()


# ---------------------------------------------------------------------- the window

def _width(ch):
    if not ch:
        return 0
    if _ud.combining(ch) or ch in "​‌‍︎️":
        return 0
    return 2 if _ud.east_asian_width(ch) in ("W", "F") else 1


def _split(ch_or_chtype, attr):
    # A chtype carries its attributes in the high bits; a str is just the glyph.
    if isinstance(ch_or_chtype, int):
        code, bits = ch_or_chtype & A_CHARTEXT, ch_or_chtype & ~A_CHARTEXT
        glyph = chr(code) if code else " "
        if bits & A_ALTCHARSET:
            glyph, bits = _ACS_GLYPH.get(glyph, glyph), bits & ~A_ALTCHARSET
        return glyph, attr | bits
    if isinstance(ch_or_chtype, bytes):
        ch_or_chtype = ch_or_chtype.decode("utf-8", "replace")
    return (ch_or_chtype or " ")[0], attr


class window:
    def __init__(self, rows, cols):
        self._rows, self._cols = rows, cols
        self._bg = (" ", 0)
        self._grid = [[(" ", 0)] * cols for _ in range(rows)]
        self._shown = None  # what the terminal currently displays
        self._dirty_all = True
        self._y = self._x = 0
        self._timeout = -1
        self._keypad = False

    # geometry
    def getmaxyx(self):
        return self._rows, self._cols

    def getyx(self):
        return self._y, self._x

    def getbegyx(self):
        return 0, 0

    def _resize(self, rows, cols):
        global LINES, COLS
        LINES, COLS = rows, cols
        self._rows, self._cols = rows, cols
        self._grid = [[self._bg] * cols for _ in range(rows)]
        self._shown = None
        self._dirty_all = True
        self._y = min(self._y, rows - 1)
        self._x = min(self._x, cols - 1)

    def resize(self, rows, cols):
        self._resize(rows, cols)

    # attributes
    def _merge(self, attr):
        # Text without a colour pair takes the window background's, as ncurses does.
        if not attr & A_COLOR:
            attr |= self._bg[1] & A_COLOR
        return attr | (self._bg[1] & A_ATTRIBUTES & ~A_COLOR)

    def bkgd(self, ch, attr=0):
        glyph, attr = _split(ch, attr)
        self._bg = (glyph, attr)

    def bkgdset(self, ch, attr=0):
        self.bkgd(ch, attr)

    def getbkgd(self):
        return ord(self._bg[0]) | self._bg[1]

    def attron(self, attr):
        pass

    def attroff(self, attr):
        pass

    def attrset(self, attr):
        pass

    # writing
    def move(self, y, x):
        if not (0 <= y < self._rows and 0 <= x < self._cols):
            raise error("wmove() returned ERR")
        self._y, self._x = y, x

    def _put(self, y, x, text, attr):
        if not (0 <= y < self._rows and 0 <= x < self._cols):
            raise error("addwstr() returned ERR")
        attr = self._merge(attr)
        row = self._grid[y]
        for ch in text:
            if ch == "\n":
                y += 1
                x = 0
                if y >= self._rows:
                    raise error("addwstr() returned ERR")
                row = self._grid[y]
                continue
            w = _width(ch)
            if w == 0:
                if x > 0:
                    prev = row[x - 1]
                    row[x - 1] = (prev[0] + ch, prev[1])
                continue
            if x + w > self._cols:
                break  # clip at the right edge
            row[x] = (ch, attr)
            if w == 2:
                row[x + 1] = ("", attr)  # continuation of a wide glyph
            x += w
        self._y, self._x = y, min(x, self._cols - 1)

    def addstr(self, *args):
        if len(args) >= 3 and isinstance(args[0], int) and isinstance(args[1], int):
            y, x, text = args[0], args[1], args[2]
            attr = args[3] if len(args) > 3 else 0
        else:
            y, x = self._y, self._x
            text = args[0]
            attr = args[1] if len(args) > 1 else 0
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        self._put(y, x, text, attr)

    def addnstr(self, *args):
        if len(args) >= 4 and isinstance(args[0], int) and isinstance(args[1], int):
            y, x, text, n = args[:4]
            attr = args[4] if len(args) > 4 else 0
        else:
            y, x = self._y, self._x
            text, n = args[0], args[1]
            attr = args[2] if len(args) > 2 else 0
        self._put(y, x, text[:n], attr)

    def insstr(self, *args):
        self.addstr(*args)

    def addch(self, *args):
        if len(args) >= 3:
            y, x, ch = args[:3]
            attr = args[3] if len(args) > 3 else 0
        else:
            y, x = self._y, self._x
            ch = args[0]
            attr = args[1] if len(args) > 1 else 0
        glyph, attr = _split(ch, attr)
        self._put(y, x, glyph, attr)

    def echochar(self, ch, attr=0):
        self.addch(ch, attr)
        self.refresh()

    def _line(self, args, down):
        if len(args) >= 4:
            y, x, ch, n = args[:4]
            attr = args[4] if len(args) > 4 else 0
        else:
            y, x = self._y, self._x
            ch, n = args[0], args[1]
            attr = args[2] if len(args) > 2 else 0
        if isinstance(ch, str) and len(ch.encode()) > 1:
            raise OverflowError("byte doesn't fit in chtype")
        glyph, attr = _split(ch, attr)
        attr = self._merge(attr)
        if not (0 <= y < self._rows and 0 <= x < self._cols):
            raise error("whline() returned ERR")
        for i in range(n):
            yy, xx = (y + i, x) if down else (y, x + i)
            if yy >= self._rows or xx >= self._cols:
                break
            self._grid[yy][xx] = (glyph, attr)

    def hline(self, *args):
        self._line(args, False)

    def vline(self, *args):
        self._line(args, True)

    def border(self, *args):
        rows, cols = self._rows, self._cols
        for x in range(1, cols - 1):
            self._grid[0][x] = self._grid[rows - 1][x] = ("─", self._bg[1])
        for y in range(1, rows - 1):
            self._grid[y][0] = self._grid[y][cols - 1] = ("│", self._bg[1])
        for (y, x), g in {(0, 0): "┌", (0, cols - 1): "┐", (rows - 1, 0): "└", (rows - 1, cols - 1): "┘"}.items():
            self._grid[y][x] = (g, self._bg[1])

    def box(self, *args):
        self.border()

    def inch(self, *args):
        y, x = (args[0], args[1]) if len(args) >= 2 else (self._y, self._x)
        if not (0 <= y < self._rows and 0 <= x < self._cols):
            raise error("winch() returned ERR")
        ch, attr = self._grid[y][x]
        code = ord(ch[0]) if ch else 32
        return (code if code <= 0xFF else 0) | attr

    def instr(self, *args):
        y, x = (args[0], args[1]) if len(args) >= 2 else (self._y, self._x)
        n = args[2] if len(args) >= 3 else self._cols - x
        return "".join(c for c, _ in self._grid[y][x : x + n]).encode()

    def erase(self):
        self._grid = [[self._bg] * self._cols for _ in range(self._rows)]
        self._y = self._x = 0

    def clear(self):
        self.erase()
        self._dirty_all = True

    def clrtoeol(self):
        row = self._grid[self._y]
        for x in range(self._x, self._cols):
            row[x] = self._bg

    def clrtobot(self):
        self.clrtoeol()
        for y in range(self._y + 1, self._rows):
            self._grid[y] = [self._bg] * self._cols

    def clearok(self, flag):
        if flag:
            self._dirty_all = True

    def idlok(self, flag):
        pass

    def leaveok(self, flag):
        pass

    def scrollok(self, flag):
        pass

    def immedok(self, flag):
        pass

    def touchwin(self):
        self._dirty_all = True

    def redrawwin(self):
        self._dirty_all = True

    def noutrefresh(self):
        pass

    # painting
    def _sgr(self, attr):
        fg, bg = _pairs.get(pair_number(attr), (-1, -1))
        fg_rgb, bg_rgb = _rgb(fg), _rgb(bg)
        if attr & (A_REVERSE | A_STANDOUT):
            fg_rgb, bg_rgb = bg_rgb or (26, 27, 38), fg_rgb or (192, 202, 245)
        parts = ["0"]
        if attr & A_BOLD:
            parts.append("1")
        if attr & A_DIM:
            parts.append("2")
        if attr & A_ITALIC:
            parts.append("3")
        if attr & A_UNDERLINE:
            parts.append("4")
        parts.append("38;2;%d;%d;%d" % fg_rgb if fg_rgb else "39")
        parts.append("48;2;%d;%d;%d" % bg_rgb if bg_rgb else "49")
        return "\x1b[" + ";".join(parts) + "m"

    def refresh(self):
        out = []
        full = self._dirty_all or self._shown is None
        shown = self._shown
        last_sgr = None
        for y, row in enumerate(self._grid):
            if not full and shown[y] == row:
                continue
            x = 0
            cols = self._cols
            while x < cols:
                if not full and shown[y][x] == row[x]:
                    x += 1
                    continue
                out.append("\x1b[%d;%dH" % (y + 1, x + 1))
                while x < cols and (full or shown[y][x] != row[x]):
                    ch, attr = row[x]
                    if ch == "":  # wide-glyph continuation, already drawn
                        x += 1
                        continue
                    sgr = self._sgr(attr)
                    if sgr != last_sgr:
                        out.append(sgr)
                        last_sgr = sgr
                    out.append(ch)
                    x += 2 if _width(ch[0]) == 2 else 1
        self._shown = [list(r) for r in self._grid]
        self._dirty_all = False
        if _cursor_visible:
            out.append("\x1b[%d;%dH\x1b[?25h" % (self._y + 1, self._x + 1))
        else:
            out.append("\x1b[?25l")
        if out:
            _js.write("".join(out))

    # input
    def keypad(self, flag):
        self._keypad = bool(flag)

    def nodelay(self, flag):
        self._timeout = 0 if flag else -1

    def timeout(self, ms):
        self._timeout = ms

    def notimeout(self, flag):
        pass

    def getch(self, *args):
        key = _next_key(self, self._timeout)
        if key is None:
            return -1
        return ord(key) if isinstance(key, str) else key

    def get_wch(self, *args):
        key = _next_key(self, self._timeout)
        if key is None:
            raise error("no input")
        return key

    def getkey(self, *args):
        key = self.get_wch()
        return key if isinstance(key, str) else _keyname(key)


def _keyname(code):
    for name, value in globals().items():
        if name.startswith("KEY_") and value == code:
            return name
    return str(code)


def keyname(code):
    return _keyname(code).encode()


# ---------------------------------------------------------------------- input decoding

_CSI_TILDE = {"1": KEY_HOME, "2": KEY_IC, "3": KEY_DC, "4": KEY_END, "5": KEY_PPAGE, "6": KEY_NPAGE, "7": KEY_HOME, "8": KEY_END,
              "11": KEY_F0 + 1, "12": KEY_F0 + 2, "13": KEY_F0 + 3, "14": KEY_F0 + 4,
              "15": KEY_F0 + 5, "17": KEY_F0 + 6, "18": KEY_F0 + 7, "19": KEY_F0 + 8, "20": KEY_F0 + 9,
              "21": KEY_F0 + 10, "23": KEY_F0 + 11, "24": KEY_F0 + 12}
_CSI_FINAL = {"A": KEY_UP, "B": KEY_DOWN, "C": KEY_RIGHT, "D": KEY_LEFT, "H": KEY_HOME, "F": KEY_END, "Z": KEY_BTAB,
              "P": KEY_F0 + 1, "Q": KEY_F0 + 2, "R": KEY_F0 + 3, "S": KEY_F0 + 4}


def _fill(timeout_ms):
    global _inbuf
    data = _js.read(int(timeout_ms))
    if data:
        _inbuf += str(data)
        return True
    return False


def _parse():
    """Decode one key from the buffer. Returns (key, consumed) or None for 'need more'."""
    buf = _inbuf
    c = buf[0]
    if c != "\x1b":
        if c == "\r":
            return "\n", 1
        if c == "\x7f":
            return KEY_BACKSPACE, 1
        return c, 1
    if len(buf) == 1:
        return None
    nxt = buf[1]
    if nxt == "O":  # SS3
        if len(buf) < 3:
            return None
        key = _CSI_FINAL.get(buf[2])
        return (key, 3) if key else ("\x1b", 1)
    if nxt != "[":
        return "\x1b", 1  # Alt+key: ESC first, then the key, as ncurses reports it
    # CSI: parameters then a final byte in @..~
    i = 2
    while i < len(buf) and not ("@" <= buf[i] <= "~"):
        i += 1
    if i >= len(buf):
        return None
    params, final, seq_len = buf[2:i], buf[i], i + 1
    if params.startswith("<") and final in "Mm":
        return ("mouse", params[1:], final), seq_len
    if final == "t" and params.startswith("8;"):
        return ("resize",), seq_len
    if final == "~":
        key = _CSI_TILDE.get(params.split(";")[0])
        if key:
            return key, seq_len
    elif final in _CSI_FINAL and (not params or params.startswith("1;") or params == "1"):
        return _CSI_FINAL[final], seq_len
    # Unknown sequence: hand back ESC and let the caller read the rest as characters
    # (OpenTab decodes its modified-Space encodings that way).
    return "\x1b", 1


def _mouse(params, final):
    global _last_click
    try:
        b, x, y = (int(v) for v in params.split(";"))
    except ValueError:
        return None
    x, y = x - 1, y - 1
    if b & 64:  # wheel
        state = BUTTON4_PRESSED if (b & 3) == 0 else BUTTON5_PRESSED  # noqa: F821 - defined via globals() above
        if (b & 3) > 1:
            return None
    elif (b & 3) == 0:
        if final == "M":
            return None  # press: wait for the release to report a click
        now = _time.monotonic()
        t, lx, ly = _last_click
        if (now - t) * 1000 <= max(_mouse_interval_ms, 300) and (lx, ly) == (x, y):
            state = BUTTON1_DOUBLE_CLICKED  # noqa: F821 - defined via globals() above
            _last_click = (0.0, -1, -1)
        else:
            state = BUTTON1_CLICKED  # noqa: F821 - defined via globals() above
            _last_click = (now, x, y)
    else:
        return None
    if not state & _mouse_mask:
        return None
    _mouse_events.append((0, x, y, 0, state))
    return KEY_MOUSE


def _next_key(win, timeout_ms):
    global _inbuf
    if _key_queue:
        return _key_queue.pop(0)
    deadline = None if timeout_ms is None or timeout_ms < 0 else _time.monotonic() + timeout_ms / 1000
    while True:
        while _inbuf:
            parsed = _parse()
            if parsed is None:  # an incomplete escape: wait escdelay for the rest
                if not _fill(_escdelay_ms):
                    parsed = ("\x1b", 1)
                else:
                    continue
            key, used = parsed
            _inbuf = _inbuf[used:]
            if isinstance(key, tuple):
                if key[0] == "mouse":
                    code = _mouse(key[1], key[2])
                    if code is not None:
                        return code
                    continue
                if key[0] == "resize":
                    rows, cols = _size()
                    win._resize(rows, cols)
                    return KEY_RESIZE
                continue
            return key
        # Nothing buffered: let one deferred background job run, then report an idle
        # wake so the app polls its workers and repaints, exactly like a timeout.
        if _idle_hook is not None and _idle_hook():
            if not _fill(0):
                return None
            continue
        if deadline is None:
            _fill(1000)
            continue
        remaining = (deadline - _time.monotonic()) * 1000
        if remaining <= 0:
            return None
        if not _fill(max(1, int(remaining))):
            return None


window.__module__ = "curses"
