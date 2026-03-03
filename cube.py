#!/usr/bin/env python3
"""Yeelight Cube Lite — per-pixel LED controller.

Grid: 5 rows × 20 columns = 100 RGB LEDs
Pixel 0 = bottom-left, linear ordering left→right, bottom→top.

Protocol (LAN, TCP port 55443):
  1. set_power on
  2. activate_fx_mode {"mode": "direct"}
  3. update_leds <base64-per-pixel concatenated string>
Display persists after disconnect.

Animation notes:
  - activate_fx_mode must be refreshed periodically (~every frame is safest)
  - update_leds does not return a response in FX mode
  - On Android/Termux, hold termux-wake-lock to prevent WiFi sleep
  - On a dedicated server (HA/Pi), no wake lock needed
"""

import socket
import json
import time
import select
import base64
import math
import signal

signal.signal(signal.SIGPIPE, signal.SIG_IGN)

CUBE_IP = "192.168.0.83"
CUBE_PORT = 55443
ROWS = 5
COLS = 20
NUM_PIXELS = ROWS * COLS


# ── pixel encoding ───────────────────────────────────────────────
def encode_pixel(r, g, b):
    """Encode a single RGB pixel as base64 (Yeelight protocol)."""
    return base64.b64encode(bytes([r, g, b])).decode("ascii")


def rgb_from_hsv(h, s, v):
    """Convert HSV (0-1 floats) to RGB (0-255 ints)."""
    if s == 0:
        r = g = b = int(v * 255)
        return r, g, b
    i = int(h * 6)
    f = (h * 6) - i
    p = int(v * (1 - s) * 255)
    q = int(v * (1 - s * f) * 255)
    t = int(v * (1 - s * (1 - f)) * 255)
    v = int(v * 255)
    i %= 6
    if i == 0: return v, t, p
    if i == 1: return q, v, p
    if i == 2: return p, v, t
    if i == 3: return p, q, v
    if i == 4: return t, p, v
    return v, p, q


# ── grid helpers ─────────────────────────────────────────────────
def pixel_index(row, col):
    """Convert (row, col) to linear pixel index. Row 0 = bottom."""
    return row * COLS + col


def make_grid(r=0, g=0, b=0):
    """Create a blank grid filled with one color."""
    return [(r, g, b)] * NUM_PIXELS


def set_pixel(grid, row, col, r, g, b):
    """Set a single pixel in the grid."""
    if 0 <= row < ROWS and 0 <= col < COLS:
        grid[pixel_index(row, col)] = (r, g, b)


def grid_to_payload(grid):
    """Convert grid list of (r,g,b) tuples to protocol payload string."""
    return "".join(encode_pixel(*rgb) for rgb in grid)


# ── cube communication ───────────────────────────────────────────
class CubeConnection:
    """Persistent connection to Yeelight Cube for animations."""

    def __init__(self, ip=CUBE_IP, port=CUBE_PORT):
        self.ip = ip
        self.port = port
        self.sock = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect((self.ip, self.port))
        self._cmd("set_power", ["on"], 1)
        self._cmd("set_bright", [100], 2)
        self._cmd("activate_fx_mode", [{"mode": "direct"}], 3)
        self._last_fx = time.time()
        self._fx_interval = 10  # refresh FX every 10 seconds

    def _cmd(self, method, params, cid=1):
        msg = json.dumps({"id": cid, "method": method, "params": params}) + "\r\n"
        self.sock.send(msg.encode())
        time.sleep(0.2)
        while select.select([self.sock], [], [], 0.1)[0]:
            self.sock.recv(4096)

    def send_frame(self, grid):
        """Send a single frame, refreshing FX mode periodically."""
        now = time.time()
        if now - self._last_fx > self._fx_interval:
            # Wait for FX response (this is the key — pacing the connection)
            msg = json.dumps({"id": 3, "method": "activate_fx_mode", "params": [{"mode": "direct"}]}) + "\r\n"
            self.sock.send(msg.encode())
            deadline = time.time() + 3
            while time.time() < deadline:
                if select.select([self.sock], [], [], 0.5)[0]:
                    self.sock.recv(4096)
                    break
            self._last_fx = time.time()
        payload = grid_to_payload(grid)
        self.sock.send((json.dumps({
            "id": 10, "method": "update_leds", "params": [payload]
        }) + "\r\n").encode())
        while select.select([self.sock], [], [], 0)[0]:
            self.sock.recv(4096)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except:
                pass

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()


def send_grid(grid, ip=CUBE_IP, port=CUBE_PORT):
    """Connect to cube, activate FX mode, and send pixel data."""
    with CubeConnection(ip, port) as cube:
        cube.send_frame(grid)


def send_native(method, params=[], ip=CUBE_IP, port=CUBE_PORT):
    """Send a raw command to the cube (non-FX mode)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect((ip, port))
    time.sleep(0.2)
    msg = json.dumps({"id": 1, "method": method, "params": params}) + "\r\n"
    s.send(msg.encode())
    time.sleep(0.3)
    try:
        resp = s.recv(2048).decode().strip()
    except:
        resp = ""
    s.close()
    return resp


# Color flow presets (count=0 means loop, action=1 means stay on last)
CF_PRESETS = {
    "candle": (0, 1, "200,1,16744448,80,300,1,16729600,60,150,1,16755200,90,"
               "250,1,16740352,70,200,1,16748544,85,350,1,16724736,50"),
    "police": (0, 1, "150,1,16711680,100,150,1,255,100"),
    "breathe_r": (0, 1, "2000,1,16711680,100,2000,1,16711680,1"),
    "breathe_g": (0, 1, "2000,1,65280,100,2000,1,65280,1"),
    "breathe_b": (0, 1, "2000,1,255,100,2000,1,255,1"),
    "breathe_c": (0, 1, "2000,1,65535,100,2000,1,65535,1"),
    "alarm": (0, 1, "100,1,16711680,100,100,1,16711680,1"),
    "rainbow": (0, 1, "1000,1,16711680,100,1000,1,65280,100,1000,1,255,100,"
                "1000,1,16776960,100,1000,1,16711935,100,1000,1,65535,100"),
    "disco": (0, 1, "200,1,16711680,100,200,1,65280,100,200,1,255,100,"
              "200,1,16776960,100,200,1,16711935,100,200,1,65535,100"),
    "sunset_flow": (0, 1, "3000,1,4915330,100,3000,1,14364480,100,"
                    "3000,1,16727040,100,3000,1,16744960,100"),
    "night": (0, 1, "5000,2,3000,5,5000,2,2700,3"),
}


def effect_start(name):
    """Start a native color flow effect."""
    if name not in CF_PRESETS:
        print(f"Unknown effect: {name}")
        print(f"Available: {', '.join(CF_PRESETS.keys())}")
        return
    count, action, flow = CF_PRESETS[name]
    send_native("set_power", ["on", "smooth", 300])
    time.sleep(0.3)
    send_native("start_cf", [count, action, flow])


def effect_stop():
    """Stop any running color flow."""
    send_native("stop_cf")


def alert_then_text(text, flashes=3, alert_color=(255, 0, 0),
                    fg=None, bg=(0, 0, 0), palette=None):
    """Flash an alert color, then display text."""
    # Flash phase using native color flow (smooth + fast)
    r, g, b = alert_color
    color_val = (r << 16) + (g << 8) + b
    flow = f"100,1,{color_val},100,100,1,{color_val},1"
    send_native("set_power", ["on", "smooth", 100])
    time.sleep(0.2)
    send_native("start_cf", [flashes, 1, flow])
    time.sleep(flashes * 0.2 + 0.3)
    # Text phase
    if palette:
        pal = NAMED_PALETTES.get(palette)
        colors = pal if pal else rainbow_palette(len(text.replace(' ', '')))
        grid = render_text_multi(text, colors=colors)
    elif fg:
        grid = render_text(text, fg=fg)
    else:
        grid = render_text(text, fg=(255, 255, 255))
    if grid:
        send_grid(grid)


# ── pattern generators ───────────────────────────────────────────
def rainbow_wave():
    """Smooth rainbow gradient across the grid."""
    grid = make_grid()
    for row in range(ROWS):
        for col in range(COLS):
            hue = ((col / COLS) + (row / ROWS) * 0.15) % 1.0
            r, g, b = rgb_from_hsv(hue, 1.0, 1.0)
            set_pixel(grid, row, col, r, g, b)
    return grid


def sunset():
    """Warm sunset gradient — deep purple at bottom to gold at top."""
    palette = [
        (75, 0, 130),    # deep purple
        (148, 0, 115),   # magenta
        (220, 40, 60),   # crimson
        (255, 140, 0),   # orange
        (255, 215, 80),  # gold
    ]
    grid = make_grid()
    for row in range(ROWS):
        base = palette[row]
        for col in range(COLS):
            # Add subtle horizontal shimmer
            shimmer = math.sin(col * 0.5) * 15
            r = max(0, min(255, int(base[0] + shimmer)))
            g = max(0, min(255, int(base[1] + shimmer * 0.5)))
            b = max(0, min(255, int(base[2] - shimmer * 0.3)))
            set_pixel(grid, row, col, r, g, b)
    return grid


def aurora():
    """Northern lights — flowing greens, teals, and purples."""
    grid = make_grid()
    for row in range(ROWS):
        for col in range(COLS):
            wave1 = math.sin(col * 0.4 + row * 0.8) * 0.5 + 0.5
            wave2 = math.sin(col * 0.25 - row * 1.2) * 0.5 + 0.5
            r = int(40 + 100 * wave2 * (1 - wave1))
            g = int(120 + 135 * wave1)
            b = int(80 + 175 * wave2 * wave1)
            # Fade bottom rows darker (like the ground)
            brightness = 0.3 + 0.7 * (row / (ROWS - 1))
            r = int(r * brightness)
            g = int(g * brightness)
            b = int(b * brightness)
            set_pixel(grid, row, col, r, g, b)
    return grid


def starfield():
    """Deep space with scattered stars."""
    import random
    random.seed(42)
    grid = make_grid(2, 2, 8)  # very dark blue
    for _ in range(20):
        row = random.randint(0, ROWS - 1)
        col = random.randint(0, COLS - 1)
        brightness = random.choice([80, 140, 200, 255])
        tint = random.choice([(255, 255, 255), (200, 220, 255), (255, 240, 200)])
        r = int(tint[0] * brightness / 255)
        g = int(tint[1] * brightness / 255)
        b = int(tint[2] * brightness / 255)
        set_pixel(grid, row, col, r, g, b)
    return grid


# ── text rendering ───────────────────────────────────────────────
FONT_5X3 = {
    'A': ["XXX","X.X","XXX","X.X","X.X"],
    'B': ["XXX.","X..X","XXX.","X..X","XXX."],
    'C': ["XXX","X..","X..","X..","XXX"],
    'D': ["XXX.","X..X","X..X","X..X","XXX."],
    'E': ["XXX","X..","XX.","X..","XXX"],
    'F': ["XXX","X..","XX.","X..","X.."],
    'G': ["XXXX","X...","X.XX","X..X","XXXX"],
    'H': ["X.X","X.X","XXX","X.X","X.X"],
    'I': ["X","X","X","X","X"],
    'J': ["..X","..X","..X","X.X","XXX"],
    'K': ["X.X","X.X","XX.","X.X","X.X"],
    'L': ["X..","X..","X..","X..","XXX"],
    'M': ["X..X","XXXX","X..X","X..X","X..X"],
    'N': ["X..X","XX.X","X.XX","X..X","X..X"],
    'O': ["XXX","X.X","X.X","X.X","XXX"],
    'P': ["XXX","X.X","XXX","X..","X.."],
    'Q': ["XXX","X.X","X.X","XXX","..X"],
    'R': ["XXX.","X..X","XXX.","X.X.","X..X"],
    'S': ["XXX","X..","XXX","..X","XXX"],
    'T': ["XXX",".X.",".X.",".X.",".X."],
    'U': ["X.X","X.X","X.X","X.X","XXX"],
    'V': ["X.X","X.X","X.X","X.X",".X."],
    'W': ["X..X","X..X","X..X","XXXX","X..X"],
    'X': ["X.X","X.X",".X.","X.X","X.X"],
    'Y': ["X.X","X.X","XXX",".X.",".X."],
    'Z': ["XXX","..X",".X.","X..","XXX"],
    '0': ["XXX","X.X","X.X","X.X","XXX"],
    '1': ["X","X","X","X","X"],
    '2': ["XXX","..X","XXX","X..","XXX"],
    '3': ["XXX","..X","XXX","..X","XXX"],
    '4': ["X.X","X.X","XXX","..X","..X"],
    '5': ["XXX","X..","XXX","..X","XXX"],
    '6': ["XXX","X..","XXX","X.X","XXX"],
    '7': ["XXX","..X","..X","..X","..X"],
    '8': ["XXX","X.X","XXX","X.X","XXX"],
    '9': ["XXX","X.X","XXX","..X","XXX"],
    ' ': ["...","...","...","...","..."],
    '!': ["X","X","X",".","X"],
    '?': ["XXX","..X",".X.","...",".X."],
    '.': [".",".",".",".","."],
    ':': [".","X",".","X","."],
    '-': ["...","...","XXX","...","..."],
    '<': ["..X",".X.","X..",".X.","..X"],
    '>': ["X..",".X.","..X",".X.","X.."],
}


def text_layout(text):
    """Compute column positions for each char. Variable-width glyphs, auto-fit spaces."""
    text = text.upper()
    # First pass: measure chars without spaces
    char_widths = []
    for ch in text:
        if ch == ' ':
            char_widths.append(0)
        else:
            glyph = FONT_5X3.get(ch, FONT_5X3[' '])
            char_widths.append(len(glyph[0]))
    # Total width of non-space chars + 1px gaps between adjacent non-space chars
    non_space = [i for i, ch in enumerate(text) if ch != ' ']
    letters_w = sum(char_widths)
    gaps = sum(1 for i in range(len(text) - 1) if text[i] != ' ' and text[i+1] != ' ')
    base_w = letters_w + gaps
    # Distribute remaining space to word gaps
    n_spaces = text.count(' ')
    if n_spaces > 0:
        avail = COLS - base_w
        space_w = max(2, avail // n_spaces) if avail > 0 else 2
    else:
        space_w = 2
    # Second pass: build positions
    positions = []
    col = 0
    for i, ch in enumerate(text):
        positions.append((ch, col))
        if ch == ' ':
            col += space_w
        else:
            col += char_widths[i]
            # 1px gap only if next char is non-space
            if i + 1 < len(text) and text[i + 1] != ' ':
                col += 1
    return text, positions, col


def render_text(text, fg=(0, 255, 200), bg=(0, 0, 0)):
    """Render text onto a 5×20 grid. Auto-scrolls if too wide. Returns grid or 'scroll' flag."""
    text, positions, total_w = text_layout(text)
    grid = make_grid(*bg)

    if total_w <= COLS:
        start_col = (COLS - total_w) // 2
        for ch, col in positions:
            if ch == ' ':
                continue
            glyph = FONT_5X3.get(ch, FONT_5X3[' '])
            for text_row in range(5):
                grid_row = ROWS - 1 - text_row
                for dx, c in enumerate(glyph[text_row]):
                    if c == 'X' and 0 <= start_col + col + dx < COLS:
                        set_pixel(grid, grid_row, start_col + col + dx, *fg)
        return grid
    else:
        return None


def rainbow_palette(n):
    """Generate n evenly-spaced rainbow colors."""
    colors = []
    for i in range(n):
        h = i / max(n, 1)
        # HSV to RGB (s=1, v=1)
        r, g, b = 0, 0, 0
        sector = int(h * 6) % 6
        f = h * 6 - int(h * 6)
        if sector == 0:   r, g, b = 255, int(255*f), 0
        elif sector == 1: r, g, b = int(255*(1-f)), 255, 0
        elif sector == 2: r, g, b = 0, 255, int(255*f)
        elif sector == 3: r, g, b = 0, int(255*(1-f)), 255
        elif sector == 4: r, g, b = int(255*f), 0, 255
        elif sector == 5: r, g, b = 255, 0, int(255*(1-f))
        colors.append((r, g, b))
    return colors


NAMED_PALETTES = {
    "rainbow": None,  # auto-generated
    "fire":    [(255, 0, 0), (255, 100, 0), (255, 200, 0), (255, 255, 100)],
    "ocean":   [(0, 20, 80), (0, 80, 200), (0, 180, 255), (100, 220, 255)],
    "neon":    [(255, 0, 255), (0, 255, 255), (255, 255, 0), (0, 255, 100)],
    "pastel":  [(255, 180, 200), (180, 220, 255), (200, 255, 200), (255, 255, 180)],
    "xmas":    [(255, 0, 0), (0, 200, 0), (255, 215, 0)],
    "ice":     [(100, 180, 255), (200, 230, 255), (255, 255, 255)],
    "matrix":  [(0, 255, 0), (0, 200, 50), (50, 255, 50), (0, 180, 80), (100, 255, 0)],
}


def render_text_multi(text, colors=None, bg=(0, 0, 0)):
    """Render text with each letter a different color."""
    text, positions, total_w = text_layout(text)
    if colors is None:
        colors = rainbow_palette(len(text.replace(' ', '')))
    grid = make_grid(*bg)
    if total_w > COLS:
        return None
    start_col = (COLS - total_w) // 2
    ci = 0
    for ch, col in positions:
        if ch == ' ':
            continue
        fg = colors[ci % len(colors)]
        glyph = FONT_5X3.get(ch, FONT_5X3[' '])
        for text_row in range(5):
            grid_row = ROWS - 1 - text_row
            for dx, c in enumerate(glyph[text_row]):
                if c == 'X' and 0 <= start_col + col + dx < COLS:
                    set_pixel(grid, grid_row, start_col + col + dx, *fg)
        ci += 1
    return grid


def render_sign_multi(text, colors=None, bg=(0, 0, 0)):
    """Render sign-style text with each letter a different color and solid bg."""
    text, positions, total_w = text_layout(text)
    if colors is None:
        colors = rainbow_palette(len(text.replace(' ', '')))
    grid = make_grid(*bg)
    start_col = max(0, (COLS - total_w) // 2)
    ci = 0
    for ch, col in positions:
        if ch == ' ':
            continue
        fg = colors[ci % len(colors)]
        glyph = FONT_5X3.get(ch, FONT_5X3[' '])
        for text_row in range(5):
            grid_row = ROWS - 1 - text_row
            for dx, c in enumerate(glyph[text_row]):
                if c == 'X' and 0 <= start_col + col + dx < COLS:
                    set_pixel(grid, grid_row, start_col + col + dx, *fg)
        ci += 1
    return grid


def render_text_with_bg(text, fg=(255, 255, 255), bg=(0, 0, 1)):
    """Render text with a solid background color (e.g., ON AIR style)."""
    text, positions, total_w = text_layout(text)
    grid = make_grid(*bg)
    start_col = max(0, (COLS - total_w) // 2)
    for ch, col in positions:
        if ch == ' ':
            continue
        glyph = FONT_5X3.get(ch, FONT_5X3[' '])
        for text_row in range(5):
            grid_row = ROWS - 1 - text_row
            for dx, c in enumerate(glyph[text_row]):
                if c == 'X' and 0 <= start_col + col + dx < COLS:
                    set_pixel(grid, grid_row, start_col + col + dx, *fg)
    return grid


def text_bitmap(text):
    """Build a full-width bitmap for scrolling text. Spaces are compressed."""
    text, positions, total_width = text_layout(text)
    total_width += 1  # include trailing pixel
    bitmap = [[False] * total_width for _ in range(5)]
    for ch, col in positions:
        if ch == ' ':
            continue
        glyph = FONT_5X3.get(ch, FONT_5X3[' '])
        for row in range(5):
            for dx, c in enumerate(glyph[row]):
                if c == 'X' and col + dx < total_width:
                    bitmap[row][col + dx] = True
    return bitmap, total_width


# ── image support ────────────────────────────────────────────────
def load_image(path):
    """Load an image file, resize to 5×20, return as grid."""
    from PIL import Image
    img = Image.open(path).convert("RGB")
    img = img.resize((COLS, ROWS), Image.LANCZOS)
    grid = make_grid()
    for row in range(ROWS):
        for col in range(COLS):
            r, g, b = img.getpixel((col, ROWS - 1 - row))  # flip Y
            set_pixel(grid, row, col, r, g, b)
    return grid


def load_gif_frames(path):
    """Load all frames from a GIF, resize to 5×20, return list of grids."""
    from PIL import Image
    img = Image.open(path)
    frames = []
    durations = []
    try:
        while True:
            frame = img.convert("RGB").resize((COLS, ROWS), Image.LANCZOS)
            grid = make_grid()
            for row in range(ROWS):
                for col in range(COLS):
                    r, g, b = frame.getpixel((col, ROWS - 1 - row))
                    set_pixel(grid, row, col, r, g, b)
            frames.append(grid)
            dur = img.info.get("duration", 100) / 1000.0  # ms to seconds
            durations.append(max(dur, 0.5))  # min 0.5s per frame
            img.seek(img.tell() + 1)
    except EOFError:
        pass
    return frames, durations


# ── animation generators ─────────────────────────────────────────
def anim_rainbow(duration=60, fps=1):
    """Animated rainbow scroll."""
    with CubeConnection() as cube:
        start = time.time()
        frame = 0
        while time.time() - start < duration:
            t = time.time() - start
            grid = make_grid()
            for row in range(ROWS):
                for col in range(COLS):
                    hue = ((col / COLS) + (row / ROWS) * 0.15 + t * 0.1) % 1.0
                    set_pixel(grid, row, col, *rgb_from_hsv(hue, 1.0, 1.0))
            cube.send_frame(grid)
            frame += 1
            time.sleep(1.0 / fps)
        print(f"{frame} frames in {time.time()-start:.0f}s")


def anim_aurora(duration=60, fps=1):
    """Animated northern lights — flowing waves of green/teal/purple."""
    with CubeConnection() as cube:
        start = time.time()
        frame = 0
        while time.time() - start < duration:
            t = time.time() - start
            grid = make_grid()
            for row in range(ROWS):
                for col in range(COLS):
                    wave1 = math.sin(col * 0.4 + row * 0.8 + t * 0.5) * 0.5 + 0.5
                    wave2 = math.sin(col * 0.25 - row * 1.2 + t * 0.3) * 0.5 + 0.5
                    wave3 = math.sin(col * 0.6 + t * 0.7) * 0.5 + 0.5
                    r = int(40 + 100 * wave2 * (1 - wave1) + 60 * wave3)
                    g = int(120 + 135 * wave1)
                    b = int(80 + 175 * wave2 * wave1)
                    brightness = 0.3 + 0.7 * (row / (ROWS - 1))
                    r = min(255, int(r * brightness))
                    g = min(255, int(g * brightness))
                    b = min(255, int(b * brightness))
                    set_pixel(grid, row, col, r, g, b)
            cube.send_frame(grid)
            frame += 1
            time.sleep(1.0 / fps)
        print(f"{frame} frames in {time.time()-start:.0f}s")


def anim_fire(duration=60, fps=1):
    """Animated fire effect — flickering reds, oranges, yellows."""
    import random
    with CubeConnection() as cube:
        # Heat map for the fire
        heat = [0.0] * NUM_PIXELS
        start = time.time()
        frame = 0
        while time.time() - start < duration:
            # Cool down
            for i in range(NUM_PIXELS):
                heat[i] = max(0, heat[i] - random.uniform(0.05, 0.15))
            # Ignite bottom row
            for col in range(COLS):
                heat[col] = min(1.0, heat[col] + random.uniform(0.3, 1.0))
            # Propagate upward
            for row in range(ROWS - 1, 0, -1):
                for col in range(COLS):
                    below = heat[pixel_index(row - 1, col)]
                    left = heat[pixel_index(row - 1, max(0, col - 1))]
                    right = heat[pixel_index(row - 1, min(COLS - 1, col + 1))]
                    heat[pixel_index(row, col)] = (below + left + right) / 3.2
            # Render
            grid = make_grid()
            for row in range(ROWS):
                for col in range(COLS):
                    h = heat[pixel_index(row, col)]
                    r = min(255, int(h * 255))
                    g = min(255, int(h * h * 180))
                    b = min(255, int(h * h * h * 60))
                    set_pixel(grid, row, col, r, g, b)
            cube.send_frame(grid)
            frame += 1
            time.sleep(1.0 / fps)
        print(f"{frame} frames in {time.time()-start:.0f}s")


def anim_breathe(duration=60, fps=1, r=0, g=255, b=200):
    """Gentle breathing/pulsing effect in one color."""
    with CubeConnection() as cube:
        start = time.time()
        frame = 0
        while time.time() - start < duration:
            t = time.time() - start
            brightness = (math.sin(t * 0.8) * 0.5 + 0.5) ** 1.5
            grid = make_grid(
                int(r * brightness),
                int(g * brightness),
                int(b * brightness),
            )
            cube.send_frame(grid)
            frame += 1
            time.sleep(1.0 / fps)
        print(f"{frame} frames in {time.time()-start:.0f}s")


def anim_scroll_text(text, duration=60, fps=1, fg=(0, 255, 200), bg=(0, 0, 0)):
    """Scroll text across the display."""
    bitmap, total_width = text_bitmap(text)

    with CubeConnection() as cube:
        start = time.time()
        frame = 0
        while time.time() - start < duration:
            offset = frame % (total_width + COLS)
            grid = make_grid(*bg)
            for row in range(ROWS):
                grid_row = ROWS - 1 - row
                for col in range(COLS):
                    src_col = col + offset - COLS
                    if 0 <= src_col < total_width and bitmap[row][src_col]:
                        set_pixel(grid, grid_row, col, *fg)
            cube.send_frame(grid)
            frame += 1
            time.sleep(1.0 / fps)
        print(f"{frame} frames in {time.time()-start:.0f}s")


def anim_scroll_text_multi(text, duration=60, fps=1, colors=None, bg=(0, 0, 0)):
    """Scroll multicolor text — each letter gets its own color."""
    text = text.upper()
    if colors is None:
        colors = rainbow_palette(len(text))
    # Build per-column color map
    char_width = 4
    total_width = len(text) * char_width
    col_colors = []
    for ci in range(len(text)):
        c = colors[ci % len(colors)]
        for _ in range(char_width):
            col_colors.append(c)

    bitmap, _ = text_bitmap(text)

    with CubeConnection() as cube:
        start = time.time()
        frame = 0
        while time.time() - start < duration:
            offset = frame % (total_width + COLS)
            grid = make_grid(*bg)
            for row in range(ROWS):
                grid_row = ROWS - 1 - row
                for col in range(COLS):
                    src_col = col + offset - COLS
                    if 0 <= src_col < total_width and bitmap[row][src_col]:
                        fg = col_colors[src_col]
                        set_pixel(grid, grid_row, col, *fg)
            cube.send_frame(grid)
            frame += 1
            time.sleep(1.0 / fps)
        print(f"{frame} frames in {time.time()-start:.0f}s")


def anim_gif(path, duration=60, loops=0):
    """Play an animated GIF on the cube."""
    frames, durations = load_gif_frames(path)
    if not frames:
        print("No frames found in GIF")
        return
    print(f"Loaded {len(frames)} frames from {path}")

    with CubeConnection() as cube:
        start = time.time()
        frame_idx = 0
        loop_count = 0
        while time.time() - start < duration:
            cube.send_frame(frames[frame_idx])
            time.sleep(durations[frame_idx])
            frame_idx += 1
            if frame_idx >= len(frames):
                frame_idx = 0
                loop_count += 1
                if loops > 0 and loop_count >= loops:
                    break
        print(f"{loop_count} loops in {time.time()-start:.0f}s")


# ── main ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    usage = """Usage: python cube.py <command> [args]

Commands (static):
  text <message> [r g b]        — Display text (auto-scrolls if too long)
  sign <message> [fgR fgG fgB bgR bgG bgB] — Text with background (ON AIR style)
  multi <message> [palette]     — Multicolor text (each letter different color)
  msign <message> [palette] [bgR bgG bgB] — Multicolor sign with background
  image <path>                  — Display a PNG/JPG image
  rainbow                       — Rainbow wave gradient
  sunset                        — Warm sunset gradient
  aurora                        — Northern lights effect
  stars                         — Starfield
  off                           — Turn all LEDs off
  color <r> <g> <b>             — Fill all LEDs with one color

Commands (animated):
  anim rainbow [duration] [fps]              — Animated rainbow scroll
  anim aurora [duration] [fps]               — Animated northern lights
  anim fire [duration] [fps]                 — Flickering fire
  anim breathe [duration] [fps] [r g b]      — Breathing pulse
  anim scroll <text> [duration] [fps]        — Scrolling text
  anim mscroll <text> [palette] [dur] [fps]  — Multicolor scrolling text
  anim gif <path> [duration]                 — Play animated GIF

Palettes: rainbow, fire, ocean, neon, pastel, xmas, ice

Examples:
  python cube.py text "HI" 0 255 200
  python cube.py multi "HELLO"                  # rainbow letters
  python cube.py multi "COOL" neon              # neon palette
  python cube.py msign "LIVE" fire              # fire-colored on dark bg
  python cube.py msign "XMAS" xmas 10 20 5     # xmas colors on dark green
  python cube.py sign "LIVE" 255 255 255 255 0 0
  python cube.py anim mscroll "MERRY CHRISTMAS" xmas 60 1
  python cube.py anim scroll "HELLO WORLD" 60 1
  python cube.py anim gif nyan.gif 60

Commands (native effects — smooth hardware transitions):
  effect <name>                 — Start a native color flow effect
  effect stop                   — Stop current effect
  alert <message> [palette]     — Flash alert then show text
  bright <1-100> [smooth|sudden] — Set brightness
  night                         — Warm nightlight mode

Effects: candle, police, alarm, breathe_r, breathe_g, breathe_b,
         breathe_c, rainbow, disco, sunset_flow, night

Examples (native):
  python cube.py effect candle
  python cube.py effect police
  python cube.py alert "DOOR" neon
  python cube.py bright 30 smooth
  python cube.py night
"""

    if len(sys.argv) < 2:
        print(usage)
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "text":
        msg = sys.argv[2] if len(sys.argv) > 2 else "HI"
        if len(sys.argv) >= 6:
            fg = (int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]))
        else:
            fg = (0, 255, 200)
        grid = render_text(msg, fg=fg)
        if grid is None:
            # Too wide — auto-scroll
            print(f'"{msg}" too wide, scrolling...')
            anim_scroll_text(msg, duration=len(msg) * 4, fps=1, fg=fg)
        else:
            send_grid(grid)
            print(f'Displayed: "{msg}"')

    elif command == "sign":
        msg = sys.argv[2] if len(sys.argv) > 2 else "LIVE"
        if len(sys.argv) >= 9:
            fg = (int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]))
            bg = (int(sys.argv[6]), int(sys.argv[7]), int(sys.argv[8]))
        elif len(sys.argv) >= 6:
            fg = (int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]))
            bg = (0, 0, 1)
        else:
            fg = (255, 255, 255)
            bg = (0, 0, 1)
        grid = render_text_with_bg(msg, fg=fg, bg=bg)
        send_grid(grid)
        print(f'Sign: "{msg}"')

    elif command == "multi":
        msg = sys.argv[2] if len(sys.argv) > 2 else "HELLO"
        palette_name = sys.argv[3] if len(sys.argv) > 3 else "rainbow"
        pal = NAMED_PALETTES.get(palette_name)
        colors = pal if pal else rainbow_palette(len(msg))
        grid = render_text_multi(msg, colors=colors)
        if grid is None:
            print(f'"{msg}" too wide, scrolling multicolor...')
            anim_scroll_text_multi(msg, duration=len(msg) * 4, fps=1, colors=colors)
        else:
            send_grid(grid)
            print(f'Multicolor: "{msg}" ({palette_name})')

    elif command == "msign":
        msg = sys.argv[2] if len(sys.argv) > 2 else "LIVE"
        palette_name = sys.argv[3] if len(sys.argv) > 3 else "rainbow"
        if len(sys.argv) >= 7:
            bg = (int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6]))
        else:
            bg = (30, 0, 0)
        pal = NAMED_PALETTES.get(palette_name)
        colors = pal if pal else rainbow_palette(len(msg))
        grid = render_sign_multi(msg, colors=colors, bg=bg)
        send_grid(grid)
        print(f'Multicolor sign: "{msg}" ({palette_name})')

    elif command == "image":
        path = sys.argv[2]
        grid = load_image(path)
        send_grid(grid)
        print(f'Displayed image: {path}')

    elif command == "rainbow":
        send_grid(rainbow_wave())
        print("Rainbow wave!")

    elif command == "sunset":
        send_grid(sunset())
        print("Sunset gradient!")

    elif command == "aurora":
        send_grid(aurora())
        print("Aurora borealis!")

    elif command == "stars":
        send_grid(starfield())
        print("Starfield!")

    elif command == "off":
        send_grid(make_grid(0, 0, 0))
        print("LEDs off.")

    elif command == "color":
        r, g, b = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
        send_grid(make_grid(r, g, b))
        print(f"Solid color ({r}, {g}, {b})")

    elif command == "anim":
        anim_type = sys.argv[2].lower() if len(sys.argv) > 2 else "rainbow"

        if anim_type == "scroll":
            text = sys.argv[3] if len(sys.argv) > 3 else "HELLO"
            dur = int(sys.argv[4]) if len(sys.argv) > 4 else 60
            fps = float(sys.argv[5]) if len(sys.argv) > 5 else 1
            anim_scroll_text(text, dur, fps)
        elif anim_type == "mscroll":
            text = sys.argv[3] if len(sys.argv) > 3 else "HELLO"
            palette_name = sys.argv[4] if len(sys.argv) > 4 else "rainbow"
            dur = int(sys.argv[5]) if len(sys.argv) > 5 else 60
            fps = float(sys.argv[6]) if len(sys.argv) > 6 else 1
            pal = NAMED_PALETTES.get(palette_name)
            colors = pal if pal else rainbow_palette(len(text))
            anim_scroll_text_multi(text, dur, fps, colors=colors)
        elif anim_type == "gif":
            path = sys.argv[3] if len(sys.argv) > 3 else "anim.gif"
            dur = int(sys.argv[4]) if len(sys.argv) > 4 else 60
            anim_gif(path, dur)
        else:
            dur = int(sys.argv[3]) if len(sys.argv) > 3 else 60
            fps = float(sys.argv[4]) if len(sys.argv) > 4 else 1

            if anim_type == "rainbow":
                anim_rainbow(dur, fps)
            elif anim_type == "aurora":
                anim_aurora(dur, fps)
            elif anim_type == "fire":
                anim_fire(dur, fps)
            elif anim_type == "breathe":
                r = int(sys.argv[5]) if len(sys.argv) > 5 else 0
                g = int(sys.argv[6]) if len(sys.argv) > 6 else 255
                b = int(sys.argv[7]) if len(sys.argv) > 7 else 200
                anim_breathe(dur, fps, r, g, b)
            else:
                print(f"Unknown animation: {anim_type}")

    elif command == "effect":
        name = sys.argv[2].lower() if len(sys.argv) > 2 else "candle"
        if name == "stop":
            effect_stop()
            print("Effect stopped.")
        else:
            effect_start(name)
            print(f"Effect: {name}")

    elif command == "alert":
        msg = sys.argv[2] if len(sys.argv) > 2 else "ALERT"
        palette = sys.argv[3] if len(sys.argv) > 3 else None
        alert_then_text(msg, palette=palette)
        print(f'Alert: "{msg}"')

    elif command == "bright":
        level = int(sys.argv[2]) if len(sys.argv) > 2 else 100
        mode = sys.argv[3] if len(sys.argv) > 3 else "smooth"
        dur = 500 if mode == "smooth" else 0
        send_native("set_bright", [level, mode, dur])
        print(f"Brightness: {level}% ({mode})")

    elif command == "night":
        send_native("set_power", ["on", "smooth", 500])
        time.sleep(0.3)
        send_native("set_ct_abx", [2700, "smooth", 1000])
        time.sleep(0.5)
        send_native("set_bright", [3, "smooth", 1000])
        print("Nightlight mode (warm 2700K, 3%)")

    else:
        print(usage)
