# Yeelight Cube Lite — Per-Pixel LED Controller

Control individual LEDs on a Yeelight Cube Lite (5×20 matrix, 100 RGB LEDs) via the LAN protocol.

## Setup

- Yeelight Cube Lite on your local network
- LAN Control enabled (check port 55443: `nc -zv <CUBE_IP> 55443`)
- Python 3 (no dependencies needed)

Edit `CUBE_IP` in `cube.py` to match your device.

## Usage

```bash
# Display text (max ~5 characters)
python3 cube.py text "HI"
python3 cube.py text "HELLO" 255 100 0    # custom RGB color

# Patterns
python3 cube.py aurora      # northern lights
python3 cube.py sunset      # warm sunset gradient
python3 cube.py rainbow     # rainbow wave
python3 cube.py stars       # starfield

# Solid color / off
python3 cube.py color 255 0 100
python3 cube.py off
```

## Grid Layout

```
Row 4 (top):    pixels 80-99   ← left to right
Row 3:          pixels 60-79
Row 2:          pixels 40-59
Row 1:          pixels 20-39
Row 0 (bottom): pixels  0-19   ← pixel 0 = bottom-left
```

## Protocol

1. TCP connect to port 55443
2. `set_power` → on
3. `activate_fx_mode` → `{"mode": "direct"}`
4. `update_leds` → base64-encoded RGB per pixel (concatenated)

Display persists after disconnect. No app required (if LAN Control is already enabled).
