#!/usr/bin/env python3
"""Yeelight Cube HTTP API server for Home Assistant integration.

Run this on the same network as your cube. HA calls it via rest_command.

Usage:
  python3 server.py [--port 8008] [--cube-ip 192.168.0.83]

HA configuration.yaml example:
  rest_command:
    cube_text:
      url: "http://<SERVER_IP>:8008/text"
      method: POST
      content_type: "application/json"
      payload: '{"message": "{{ message }}", "fg": {{ fg | default("[0,255,200]") }}}'
    cube_sign:
      url: "http://<SERVER_IP>:8008/sign"
      method: POST
      content_type: "application/json"
      payload: '{"message": "{{ message }}"}'
    cube_gauge:
      url: "http://<SERVER_IP>:8008/gauge"
      method: POST
      content_type: "application/json"
      payload: '{"value": {{ value }}, "max": {{ max | default(100) }}, "label": "{{ label }}"}'
    cube_weather:
      url: "http://<SERVER_IP>:8008/weather"
      method: POST
      content_type: "application/json"
      payload: '{"temp": {{ temp }}, "condition": "{{ condition }}"}'
    cube_status:
      url: "http://<SERVER_IP>:8008/status"
      method: POST
      content_type: "application/json"
      payload: '{"items": {{ items }}}'
    cube_off:
      url: "http://<SERVER_IP>:8008/off"
      method: POST

HA automation example:
  - alias: "Show battery on cube"
    trigger:
      - platform: state
        entity_id: sensor.phone_battery_level
    action:
      - service: rest_command.cube_gauge
        data:
          value: "{{ states('sensor.phone_battery_level') }}"
          label: "BAT"
          max: 100
"""

import json
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from cube import (
    ROWS, COLS, NUM_PIXELS,
    make_grid, set_pixel, send_grid, rgb_from_hsv,
    render_text, render_text_with_bg, load_image,
    anim_scroll_text, encode_pixel, grid_to_payload,
    FONT_5X3, CubeConnection,
)
import threading
import math


# ── widget renderers ─────────────────────────────────────────────

def render_gauge(value, max_val=100, label="", color=None, bg=(0, 0, 0)):
    """Render a horizontal gauge bar with optional label.
    
    Layout (5 rows x 20 cols):
      Row 4 (top):  label text (3 chars max)
      Row 3:        value as number
      Row 2:        [gauge bar fills left to right]
      Row 1:        [gauge bar fills left to right]
      Row 0:        [gauge bar fills left to right]
    """
    pct = max(0, min(1, value / max_val))
    filled = int(pct * COLS)

    # Auto color: green > yellow > red
    if color is None:
        if pct > 0.5:
            color = (0, 255, 0)
        elif pct > 0.2:
            color = (255, 200, 0)
        else:
            color = (255, 0, 0)

    grid = make_grid(*bg)

    # Gauge bar (rows 0-2)
    for row in range(3):
        for col in range(COLS):
            if col < filled:
                # Slight gradient - brighter in middle row
                mult = 1.0 if row == 1 else 0.6
                r = min(255, int(color[0] * mult))
                g = min(255, int(color[1] * mult))
                b = min(255, int(color[2] * mult))
                set_pixel(grid, row, col, r, g, b)
            else:
                set_pixel(grid, row, col, 15, 15, 15)  # dim unfilled

    # Number (row 3-4) — show percentage
    num_text = f"{int(pct * 100)}"
    if label:
        display = f"{label} {num_text}"
    else:
        display = num_text

    display = display.upper()[:5]  # max 5 chars for top rows
    char_w = 4
    total_w = len(display) * char_w - 1
    start_col = max(0, (COLS - total_w) // 2)

    col = start_col
    for ch in display:
        glyph = FONT_5X3.get(ch, FONT_5X3.get(' '))
        if glyph is None:
            col += char_w
            continue
        # Only render top 2 rows of the 5-row font into grid rows 3-4
        for text_row in range(5):
            grid_row = ROWS - 1 - text_row
            if grid_row < 3:
                continue  # skip — that's the gauge bar area
            for dx, c in enumerate(glyph[text_row]):
                if c == 'X' and 0 <= col + dx < COLS:
                    set_pixel(grid, grid_row, col + dx, *color)
        col += char_w

    return grid


# Weather icons (5x5 pixel art, placed on left side)
WEATHER_ICONS = {
    "sunny": [
        "..X..............",
        ".XXX.............",
        "XXXXX............",
        ".XXX.............",
        "..X..............",
    ],
    "cloudy": [
        "..XXX............",
        ".XXXXX...........",
        "XXXXXXX..........",
        ".XXXXX...........",
        ".................",
    ],
    "rainy": [
        "..XXX............",
        ".XXXXX...........",
        "XXXXXXX..........",
        ".X.X.X...........",
        "X.X.X............",
    ],
    "snowy": [
        "..XXX............",
        ".XXXXX...........",
        "XXXXXXX..........",
        ".X.X.X...........",
        "..X.X............",
    ],
    "stormy": [
        "..XXX............",
        ".XXXXX...........",
        "XXXXXXX..........",
        "...XX............",
        "..X..............",
    ],
}

WEATHER_COLORS = {
    "sunny": (255, 200, 0),
    "cloudy": (150, 150, 150),
    "rainy": (80, 80, 255),
    "snowy": (200, 200, 255),
    "stormy": (255, 255, 0),
}


def render_weather(temp, condition="sunny", unit="C"):
    """Render weather: icon on left, temperature on right."""
    grid = make_grid(0, 0, 0)

    # Draw weather icon (left 7 cols)
    icon = WEATHER_ICONS.get(condition, WEATHER_ICONS["sunny"])
    icon_color = WEATHER_COLORS.get(condition, (255, 200, 0))
    for text_row, row_str in enumerate(icon):
        grid_row = ROWS - 1 - text_row
        for col, c in enumerate(row_str):
            if c == 'X' and col < 7:
                set_pixel(grid, grid_row, col, *icon_color)

    # Draw temperature number (right side, starting col 9)
    temp_str = f"{int(temp)}"
    # Color based on temp
    if temp >= 30:
        temp_color = (255, 60, 0)
    elif temp >= 20:
        temp_color = (255, 180, 0)
    elif temp >= 10:
        temp_color = (0, 255, 100)
    elif temp >= 0:
        temp_color = (0, 180, 255)
    else:
        temp_color = (100, 100, 255)

    char_w = 4
    col = 9
    for ch in temp_str:
        glyph = FONT_5X3.get(ch)
        if glyph:
            for text_row in range(5):
                grid_row = ROWS - 1 - text_row
                for dx, c in enumerate(glyph[text_row]):
                    if c == 'X' and 0 <= col + dx < COLS:
                        set_pixel(grid, grid_row, col + dx, *temp_color)
        col += char_w

    # Degree symbol and unit
    if col + 3 < COLS:
        set_pixel(grid, 4, col, *temp_color)  # tiny dot for °
        col += 2
        glyph = FONT_5X3.get(unit.upper())
        if glyph:
            for text_row in range(5):
                grid_row = ROWS - 1 - text_row
                for dx, c in enumerate(glyph[text_row]):
                    if c == 'X' and 0 <= col + dx < COLS:
                        set_pixel(grid, grid_row, col + dx, *temp_color)

    return grid


def render_status(items):
    """Render status dots — list of {name, color, state}.
    
    Each item gets a colored dot + 2-char label, spaced across the grid.
    items: [{"label": "TV", "color": [0,255,0]}, {"label": "AC", "color": [255,0,0]}, ...]
    """
    grid = make_grid(0, 0, 0)
    n = len(items)
    if n == 0:
        return grid

    spacing = COLS // min(n, 5)

    for i, item in enumerate(items[:5]):  # max 5 items
        cx = i * spacing + spacing // 2
        color = tuple(item.get("color", [0, 255, 0]))
        label = item.get("label", "")[:2].upper()

        # Draw dot (rows 3-4)
        for row in [3, 4]:
            for dx in [-1, 0, 1]:
                if 0 <= cx + dx < COLS:
                    set_pixel(grid, row, cx + dx, *color)

        # Draw label (rows 0-1, abbreviated)
        if label:
            lx = cx - 1
            glyph = FONT_5X3.get(label[0])
            if glyph:
                for text_row in range(2):  # only bottom 2 rows of font
                    grid_row = 1 - text_row
                    for dx, c in enumerate(glyph[text_row + 3]):
                        if c == 'X' and 0 <= lx + dx < COLS:
                            set_pixel(grid, grid_row, lx + dx, *color)

    return grid


# ── HTTP server ──────────────────────────────────────────────────

class CubeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len else b"{}"
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        path = self.path.rstrip("/")
        result = "ok"

        try:
            if path == "/text":
                msg = data.get("message", "HI")
                fg = tuple(data.get("fg", [0, 255, 200]))
                bg = tuple(data.get("bg", [0, 0, 0]))
                grid = render_text(msg, fg=fg, bg=bg)
                if grid is None:
                    anim_scroll_text(msg, duration=len(msg) * 4, fps=1, fg=fg, bg=bg)
                else:
                    send_grid(grid)

            elif path == "/sign":
                msg = data.get("message", "LIVE")
                fg = tuple(data.get("fg", [255, 255, 255]))
                bg = tuple(data.get("bg", [30, 0, 0]))
                grid = render_text_with_bg(msg, fg=fg, bg=bg)
                send_grid(grid)

            elif path == "/gauge":
                value = float(data.get("value", 50))
                max_val = float(data.get("max", 100))
                label = data.get("label", "")
                color = tuple(data.get("color")) if data.get("color") else None
                grid = render_gauge(value, max_val, label, color)
                send_grid(grid)

            elif path == "/weather":
                temp = float(data.get("temp", 20))
                condition = data.get("condition", "sunny")
                unit = data.get("unit", "C")
                grid = render_weather(temp, condition, unit)
                send_grid(grid)

            elif path == "/status":
                items = data.get("items", [])
                grid = render_status(items)
                send_grid(grid)

            elif path == "/color":
                r = int(data.get("r", 0))
                g = int(data.get("g", 0))
                b = int(data.get("b", 0))
                send_grid(make_grid(r, g, b))

            elif path == "/off":
                send_grid(make_grid(0, 0, 0))

            else:
                result = f"unknown endpoint: {path}"
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": result}).encode())
                return

        except Exception as e:
            result = str(e)
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": result}).encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": result}).encode())

    def log_message(self, format, *args):
        print(f"[cube] {args[0]}")


def main():
    parser = argparse.ArgumentParser(description="Yeelight Cube HTTP API")
    parser.add_argument("--port", type=int, default=8008)
    parser.add_argument("--cube-ip", default="192.168.0.83")
    args = parser.parse_args()

    import cube
    cube.CUBE_IP = args.cube_ip

    print(f"Yeelight Cube API server on port {args.port}")
    print(f"Cube IP: {args.cube_ip}")
    print(f"\nEndpoints:")
    print(f"  POST /text     - display text")
    print(f"  POST /sign     - text with background")
    print(f"  POST /gauge    - battery/sensor gauge")
    print(f"  POST /weather  - weather display")
    print(f"  POST /status   - status dots")
    print(f"  POST /color    - solid color")
    print(f"  POST /off      - turn off")

    server = HTTPServer(("0.0.0.0", args.port), CubeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
        server.server_close()


if __name__ == "__main__":
    main()
