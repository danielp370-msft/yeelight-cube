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
    'B': ["XX.","X.X","XX.","X.X","XX."],
    'C': ["XXX","X..","X..","X..","XXX"],
    'D': ["XX.","X.X","X.X","X.X","XX."],
    'E': ["XXX","X..","XX.","X..","XXX"],
    'F': ["XXX","X..","XX.","X..","X.."],
    'G': ["XXX","X..","X.X","X.X","XXX"],
    'H': ["X.X","X.X","XXX","X.X","X.X"],
    'I': ["XXX",".X.",".X.",".X.","XXX"],
    'J': ["..X","..X","..X","X.X","XXX"],
    'K': ["X.X","X.X","XX.","X.X","X.X"],
    'L': ["X..","X..","X..","X..","XXX"],
    'M': ["X.X","XXX","XXX","X.X","X.X"],
    'N': ["X.X","XXX","XXX","X.X","X.X"],
    'O': ["XXX","X.X","X.X","X.X","XXX"],
    'P': ["XXX","X.X","XXX","X..","X.."],
    'Q': ["XXX","X.X","X.X","XXX","..X"],
    'R': ["XXX","X.X","XX.","X.X","X.X"],
    'S': ["XXX","X..","XXX","..X","XXX"],
    'T': ["XXX",".X.",".X.",".X.",".X."],
    'U': ["X.X","X.X","X.X","X.X","XXX"],
    'V': ["X.X","X.X","X.X","X.X",".X."],
    'W': ["X.X","X.X","XXX","XXX","X.X"],
    'X': ["X.X","X.X",".X.","X.X","X.X"],
    'Y': ["X.X","X.X","XXX",".X.",".X."],
    'Z': ["XXX","..X",".X.","X..","XXX"],
    '0': ["XXX","X.X","X.X","X.X","XXX"],
    '1': [".X.",".X.",".X.",".X.",".X."],
    '2': ["XXX","..X","XXX","X..","XXX"],
    '3': ["XXX","..X","XXX","..X","XXX"],
    '4': ["X.X","X.X","XXX","..X","..X"],
    '5': ["XXX","X..","XXX","..X","XXX"],
    '6': ["XXX","X..","XXX","X.X","XXX"],
    '7': ["XXX","..X","..X","..X","..X"],
    '8': ["XXX","X.X","XXX","X.X","XXX"],
    '9': ["XXX","X.X","XXX","..X","XXX"],
    ' ': ["...",".?.","...",".?.","..."],
    '!': [".X.",".X.",".X.","...",".X."],
    '?': ["XXX","..X",".X.","...",".X."],
    '.': ["...","...","...","...",".X."],
    ':': ["...",".X.","...",".X.","..."],
    '-': ["...","...","XXX","...","..."],
    '<': ["..X",".X.","X..",".X.","..X"],  # heart left half
    '>': ["X..",".X.","..X",".X.","X.."],  # heart right half
}
FONT_5X3[' '] = ["...","...","...","...","..."]


def render_text(text, fg=(0, 255, 200), bg=(0, 0, 0)):
    """Render text onto a 5×20 grid. Auto-scrolls if too wide. Returns grid or 'scroll' flag."""
    text = text.upper()
    total_w = len(text) * 4 - 1  # 3px per char + 1px gap
    grid = make_grid(*bg)

    if total_w <= COLS:
        # Fits — center it
        start_col = (COLS - total_w) // 2
        col = start_col
        for ch in text:
            glyph = FONT_5X3.get(ch, FONT_5X3[' '])
            for text_row in range(5):
                grid_row = ROWS - 1 - text_row
                for dx, c in enumerate(glyph[text_row]):
                    if c == 'X' and 0 <= col + dx < COLS:
                        set_pixel(grid, grid_row, col + dx, *fg)
            col += 4
        return grid
    else:
        # Too wide — return None to signal caller to use scroll
        return None


def render_text_with_bg(text, fg=(255, 255, 255), bg=(30, 0, 0)):
    """Render text with a solid background color (e.g., ON AIR style)."""
    text = text.upper()
    total_w = len(text) * 4 - 1
    grid = make_grid(*bg)  # fill entire grid with bg
    start_col = max(0, (COLS - total_w) // 2)
    col = start_col
    for ch in text:
        glyph = FONT_5X3.get(ch, FONT_5X3[' '])
        for text_row in range(5):
            grid_row = ROWS - 1 - text_row
            for dx, c in enumerate(glyph[text_row]):
                if c == 'X' and 0 <= col + dx < COLS:
                    set_pixel(grid, grid_row, col + dx, *fg)
        col += 4
    return grid


def text_bitmap(text):
    """Build a full-width bitmap for scrolling text."""
    text = text.upper()
    char_width = 4
    total_width = len(text) * char_width
    bitmap = [[False] * total_width for _ in range(5)]
    for ci, ch in enumerate(text):
        glyph = FONT_5X3.get(ch, FONT_5X3[' '])
        for row in range(5):
            for dx, c in enumerate(glyph[row]):
                if c == 'X':
                    bitmap[row][ci * char_width + dx] = True
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
  anim gif <path> [duration]                 — Play animated GIF

Examples:
  python cube.py text "HI" 0 255 200
  python cube.py text "ON AIR"                  # auto-scrolls
  python cube.py sign "LIVE" 255 255 255 255 0 0  # white on red
  python cube.py image photo.png
  python cube.py anim fire 120 1
  python cube.py anim scroll "HELLO WORLD" 60 1
  python cube.py anim gif nyan.gif 60
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
            bg = (255, 0, 0)
        else:
            fg = (255, 255, 255)
            bg = (255, 0, 0)
        grid = render_text_with_bg(msg, fg=fg, bg=bg)
        send_grid(grid)
        print(f'Sign: "{msg}"')

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

    else:
        print(usage)
