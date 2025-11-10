import os
import time
import psutil
import subprocess
import threading
import random
import socket
import requests
from collections import deque
from gpiozero import Button, RotaryEncoder
from board import SCL, SDA
import busio
from adafruit_ssd1306 import SSD1306_I2C
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime

# -------------------------------
# Display setup
# -------------------------------
WIDTH, HEIGHT = 128, 64
i2c = busio.I2C(SCL, SDA)
display = SSD1306_I2C(WIDTH, HEIGHT, i2c, addr=0x3C)

# -------------------------------
# Fonts
# -------------------------------
# Menu uses the default small bitmap font for crisp list text
font = ImageFont.load_default()
# Dashboard uses DejaVu for thicker glyphs
font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 9)
font_mid   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 10)
font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 22)
font_temp  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 28)

# -------------------------------
# Inputs
# -------------------------------
encoder = RotaryEncoder(a=27, b=22, max_steps=999)
btn_confirm = Button(5)
btn_back    = Button(23)
btn_knob    = Button(17)

# -------------------------------
# Menu items
# -------------------------------
menu_items = [
    "Show Clock",
    "Show Uptime",
    "Load Average",
    "Disk Space",
    "SMART Status",
    "Animation",
    "Game of Life",
    "Space Invaders",
    "Restart Jellyfin",
    "Restart Samba",
    "PowerOff",
    "Reboot",
]

VISIBLE_LINES = 7
selected_index = 0
menu_offset = 0  # first visible index
submenu_thread = None
stop_event = threading.Event()

# -------------------------------
# Background dashboard state
# -------------------------------
NET_IFACE = "eth0"   # change if needed
ZIP = "32084"
LAT, LON = 29.8922, -81.3139
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_UPDATE_INTERVAL = 600  # seconds
last_weather_update = 0
cached_weather = {"temp_f": 0, "condition": "sun", "forecast": []}

# Disk activity
last_disk_io = psutil.disk_io_counters(perdisk=True).get("sda", None)
last_reads, last_writes = (last_disk_io.read_count, last_disk_io.write_count) if last_disk_io else (0, 0)
disk_active = False
blink_timer = 0

# Network throughput
last_net = psutil.net_io_counters(pernic=True).get(NET_IFACE, None)
last_bytes = (last_net.bytes_sent + last_net.bytes_recv) if last_net else 0

# Load graph buffer
load_history = deque(maxlen=50)

# View management
current_view = "menu"  # "menu" or "background"
show_weather = False
background_cycle_start = 0.0

# Idle logic
IDLE_TIMEOUT = 30.0   # seconds on main menu
last_user_activity = time.time()

# Fade timing
FADE_DURATION = 0.5   # seconds to fade back to menu

# -------------------------------
# Helpers: GPIO gating for menu rotation callbacks
# -------------------------------
def disable_menu_rotation():
    try:
        encoder.when_rotated = lambda: None
    except Exception:
        pass

def enable_menu_rotation():
    try:
        encoder.when_rotated = on_rotate
    except Exception:
        pass

# -------------------------------
# Utility helpers
# -------------------------------
def now_ts():
    return time.time()

def mark_activity():
    global last_user_activity
    last_user_activity = now_ts()

def get_ip_last_octet():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip.split(".")[-1]
    except:
        return "--"

def get_cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return float(f.read()) / 1000
    except:
        return 0.0

def get_net_activity_bytes():
    global last_bytes
    now_net = psutil.net_io_counters(pernic=True).get(NET_IFACE, None)
    if not now_net:
        return 0
    now_total = now_net.bytes_sent + now_net.bytes_recv
    diff = max(0, now_total - last_bytes)
    last_bytes = now_total
    return diff  # bytes since last call

def draw_cut_corner_box(draw, x1, y1, x2, y2, title):
    draw.rectangle((x1, y1, x2, y2), outline=255, fill=0)
    corner_w = 24
    draw.polygon(
        [(x1, y1), (x1 + corner_w, y1), (x1 + corner_w - 4, y1 + 8), (x1, y1 + 8)],
        outline=255, fill=255
    )
    draw.text((x1 + 3, y1 - 1), title, font=font_small, fill=0)

def get_weather():
    global last_weather_update, cached_weather
    if now_ts() - last_weather_update < WEATHER_UPDATE_INTERVAL:
        return cached_weather
    params = {
        "latitude": LAT,
        "longitude": LON,
        "current_weather": "true",
        "hourly": "temperature_2m",
        "timezone": "America/New_York"
    }
    try:
        r = requests.get(WEATHER_URL, params=params, timeout=5)
        data = r.json()
        cur = data["current_weather"]
        temp_c = cur["temperature"]
        temp_f = temp_c * 9 / 5 + 32
        code = cur.get("weathercode", 0)
        if code in [61, 63, 65, 80, 81]:      cond = "rain"
        elif code in [71, 73, 75, 77, 85, 86]: cond = "snow"
        elif code in [0, 1, 2, 3]:             cond = "sun"
        elif code in [95, 96, 99]:             cond = "storm"
        else:                                  cond = "cloud"
        hourly = data.get("hourly", {})
        temps = hourly.get("temperature_2m", [temp_c])[:4]
        temp_fs = [t * 9/5 + 32 for t in temps]
        cached_weather = {"temp_f": temp_f, "condition": cond, "forecast": temp_fs}
        last_weather_update = now_ts()
    except Exception:
        pass
    return cached_weather

def draw_weather_icon(draw, x, y, cond):
    # simple monochrome icons
    if cond == "sun":
        draw.ellipse((x, y, x+20, y+20), outline=255, fill=255)
    elif cond == "rain":
        draw.ellipse((x, y, x+20, y+12), outline=255, fill=0)
        for i in range(3):
            draw.line((x+5+i*5, y+12, x+3+i*5, y+18), fill=255)
    elif cond == "snow":
        for i in range(6):
            draw.line((x+10, y+6, x+10+(i%3-1)*6, y+6+(i//3-1)*6), fill=255)
    elif cond == "storm":
        draw.polygon([(x+6, y+6), (x+10, y+6), (x+8, y+14), (x+12, y+14), (x+6, y+22)], fill=255)
    else:
        draw.ellipse((x+2, y+6, x+22, y+18), outline=255, fill=255)

# -------------------------------
# Background: system screen
# -------------------------------
def draw_system_screen():
    global last_reads, last_writes, disk_active, blink_timer, load_history

    image = Image.new("1", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)

    now = datetime.now()
    hour = now.strftime("%H")
    minute = now.strftime("%M")
    month = now.strftime("%b")
    day = now.strftime("%d")

    cpu_temp = get_cpu_temp()
    load_avg = psutil.getloadavg()[0]
    ram_used = psutil.virtual_memory().percent / 10
    ip_octet = get_ip_last_octet()
    load_history.append(load_avg)

    # Disk activity check
    disk_stats = psutil.disk_io_counters(perdisk=True).get("sda", None)
    if disk_stats:
        if disk_stats.read_count > last_reads or disk_stats.write_count > last_writes:
            disk_active = True
            blink_timer = 3
        last_reads, last_writes = disk_stats.read_count, disk_stats.write_count
    if blink_timer > 0:
        blink_timer -= 1
    else:
        disk_active = False

    # Time, big
    draw.text((0, -4), hour, font=font_large, fill=255)
    draw.text((0, 22), minute, font=font_large, fill=255)
    draw.rectangle((0, 46, 44, 64), outline=255, fill=255)
    draw.text((2, 48), month, font=font_mid, fill=0)
    draw.text((28, 48), day, font=font_mid, fill=0)

    # CPU box
    draw_cut_corner_box(draw, 46, 0, 127, 28, "CPU")
    draw.text((88, 2), f"IP {ip_octet}", font=font_small, fill=255)
    draw.text((50, 14), f"{cpu_temp:>4.1f}°", font=font_small, fill=255)

    # CPU load graph area
    base_x, base_y, max_h = 90, 27, 12
    for i, v in enumerate(list(load_history)[-36:]):
        h = int(min(v / 2.0 * max_h, max_h))
        draw.line((base_x + i, base_y, base_x + i, base_y - h), fill=255)

    # RAM box
    draw_cut_corner_box(draw, 46, 34, 127, 62, "RAM")
    draw.text((50, 48), f"{ram_used:>4.1f}", font=font_small, fill=255)

    # Ethernet activity bars (hollow boxes that fill)
    net_bytes = get_net_activity_bytes()
    kbps = net_bytes * 2 / 1024.0
    bars = 0
    if kbps > 50:  bars = 1
    if kbps > 150: bars = 2
    if kbps > 400: bars = 3

    base_x = 113  # shifted left 2 px
    base_y = 59   # shifted up 2 px
    for i in range(3):
        bx1 = base_x + (i * 4)
        bx2 = bx1 + 3
        by1 = base_y - (i + 1) * 2
        # hollow outline
        draw.rectangle((bx1, by1 - 2, bx2, base_y), outline=255, fill=255 if i < bars else 0)

    # Disk LED
    draw.rectangle((44, 60, 49, 63), outline=255, fill=255 if disk_active else 0)
    return image

# -------------------------------
# Background: weather screen
# -------------------------------
def draw_weather_screen():
    image = Image.new("1", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    weather = get_weather()
    cond = weather["condition"]
    temp_f = weather["temp_f"]
    forecast = weather["forecast"]

    draw.text((10, 5), f"{temp_f:.0f}°F", font=font_temp, fill=255)
    draw_weather_icon(draw, 90, 12, cond)

    fx = 10
    fy = 48
    for t in forecast:
        draw.text((fx, fy), f"{t:.0f}°", font=font_small, fill=255)
        fx += 30
    return image

# -------------------------------
# Transitions
# -------------------------------
def slide_transition(img_from, img_to, delay=0.01, step=8):
    for offset in range(0, WIDTH + 1, step):
        frame = Image.new("1", (WIDTH, HEIGHT))
        frame.paste(img_from, (-offset, 0))
        frame.paste(img_to, (WIDTH - offset, 0))
        display.image(frame)
        display.show()
        time.sleep(delay)

def fade_to_menu(from_img, to_menu_img, duration=FADE_DURATION, steps=10):
    # Dissolve style: progressively reveal menu rows
    delay = max(0.01, duration / steps)
    for k in range(1, steps + 1):
        frame = Image.new("1", (WIDTH, HEIGHT))
        frame.paste(from_img, (0, 0))
        # mask reveals rows where y % steps < k
        mask = Image.new("1", (WIDTH, HEIGHT), 0)
        md = ImageDraw.Draw(mask)
        for y in range(HEIGHT):
            if (y % steps) < k:
                md.line((0, y, WIDTH, y), fill=1)
        frame.paste(to_menu_img, (0, 0), mask)
        display.image(frame)
        display.show()
        time.sleep(delay)

# -------------------------------
# Menu rendering and helpers
# -------------------------------
def show_menu():
    global menu_offset
    image = Image.new("1", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)

    # Ensure selected item is visible
    if selected_index < menu_offset:
        menu_offset = selected_index
    elif selected_index >= menu_offset + VISIBLE_LINES:
        menu_offset = selected_index - VISIBLE_LINES + 1

    visible_items = menu_items[menu_offset:menu_offset + VISIBLE_LINES]

    total_height = len(visible_items) * 8
    top_margin = max(0, (HEIGHT - total_height) // 2)

    for i, item in enumerate(visible_items):
        y = top_margin + i * 8
        absolute_index = menu_offset + i
        if absolute_index == selected_index:
            draw.rectangle([0, y, WIDTH - 1, y + 8], fill=255)
            draw.text((2, y), item, font=font, fill=0)
        else:
            draw.text((2, y), item, font=font, fill=255)

    display.image(image)
    display.show()
    return image  # return the image so we can slide away from it

def system_call(cmd):
    try:
        subprocess.run(cmd, shell=True)
    except Exception as e:
        show_text([f"Error: {e}"])
        time.sleep(2)

def show_text(lines, center=False):
    image = Image.new("1", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines[:6]):
        w, _ = draw.textsize(line, font=font)
        x = (WIDTH - w) // 2 if center else 0
        draw.text((x, i * 10), line, font=font, fill=255)
    display.image(image)
    display.show()

# -------------------------------
# Submenu screens (from your code)
# -------------------------------
def screen_spaceinvaders():
    import time as _t
    width, height = WIDTH, HEIGHT
    player_y = height - 8
    alien_rows = 3
    alien_cols = 8
    alien_spacing_x = 12
    alien_spacing_y = 8
    alien_step_x = 1
    alien_dir = 1
    bullet_speed = 2
    frame_delay = 0.03
    fire_cooldown = 0.1
    fire_block_time = 0.1
    alien_move_interval = 0.12
    last_alien_move_time = 0
    MAX_STEP_DELTA = 3
    SHOW_DEBUG = False
    player_x = width // 2
    bullets = []
    aliens = [
        [10 + c * alien_spacing_x, 8 + r * alien_spacing_y]
        for r in range(alien_rows)
        for c in range(alien_cols)
    ]
    score = 0
    last_fire = 0
    last_steps = encoder.steps
    last_fire_block = 0
    step_delta = 0
    disable_menu_rotation()

    def fire_bullet():
        nonlocal last_fire, last_fire_block, bullets
        now = _t.time()
        if now - last_fire >= fire_cooldown:
            bullets.append([player_x + 2, player_y - 3])
            last_fire = now
            last_fire_block = now

    old_knob_handler = btn_knob.when_pressed
    btn_knob.when_pressed = fire_bullet

    try:
        while True:
            if stop_event.is_set() or btn_back.is_pressed:
                return

            now = _t.time()
            if now - last_fire_block > fire_block_time:
                step_delta = encoder.steps - last_steps
                if abs(step_delta) > MAX_STEP_DELTA:
                    last_steps = encoder.steps
                    step_delta = 0
                if step_delta:
                    player_x += step_delta * 3
                    player_x = max(0, min(width - 6, player_x))
                last_steps = encoder.steps

            if aliens:
                projected_out_of_bounds = any(
                    (ax + alien_dir * alien_step_x) < 0 or (ax + alien_dir * alien_step_x) > (width - 8)
                    for ax, _ in aliens
                )
                move_down = projected_out_of_bounds and (now - last_alien_move_time >= alien_move_interval)
                if move_down:
                    last_alien_move_time = now
                    alien_dir *= -1
                    aliens = [[ax, ay + 4] for ax, ay in aliens]
                else:
                    new_aliens = []
                    for ax, ay in aliens:
                        nx = ax + alien_dir * alien_step_x
                        nx = max(0, min(width - 8, nx))
                        new_aliens.append([nx, ay])
                    aliens = new_aliens

            bullets = [[bx, by - bullet_speed] for bx, by in bullets if by - bullet_speed > 0]

            for b in list(bullets):
                bx, by = b
                hit_idx = None
                for idx, (ax, ay) in enumerate(aliens):
                    if abs(bx - ax) < 4 and abs(by - ay) < 4:
                        hit_idx = idx
                        break
                if hit_idx is not None:
                    try:
                        bullets.remove(b)
                    except ValueError:
                        pass
                    del aliens[hit_idx]
                    score += 10

            if not aliens:
                alien_rows = min(alien_rows + 1, 5)
                aliens = [
                    [10 + c * alien_spacing_x, 8 + r * alien_spacing_y]
                    for r in range(alien_rows)
                    for c in range(alien_cols)
                ]
                alien_dir = 1

            if any(ay >= player_y - 2 for _, ay in aliens):
                image = Image.new("1", (width, height))
                draw = ImageDraw.Draw(image)
                draw.text((30, 24), "GAME OVER", font=font, fill=255)
                draw.text((35, 40), f"Score {score}", font=font, fill=255)
                display.image(image)
                display.show()
                _t.sleep(1.5)
                return

            image = Image.new("1", (width, height))
            draw = ImageDraw.Draw(image)

            draw.text((2, 0), f"SCORE {score}", font=font, fill=255)

            for ax, ay in aliens:
                draw.rectangle([ax, ay, ax + 3, ay + 2], fill=255)

            draw.rectangle([player_x, player_y, player_x + 5, player_y + 2], fill=255)

            for bx, by in bullets:
                draw.rectangle([bx, by, bx + 1, by + 2], fill=255)

            if SHOW_DEBUG:
                try:
                    draw.text((2, 54), f"enc:{encoder.steps} d:{step_delta} dir:{alien_dir}", font=font, fill=255)
                except Exception:
                    pass

            display.image(image)
            display.show()
            _t.sleep(frame_delay)
    finally:
        btn_knob.when_pressed = old_knob_handler
        enable_menu_rotation()

def screen_gameoflife():
    import random
    width, height = WIDTH, HEIGHT
    cell_size = 2
    cols = width // cell_size
    rows = height // cell_size
    frame_delay = 0.1
    regenerate_event = threading.Event()

    def randomize_grid():
        return [[random.randint(0, 1) for _ in range(cols)] for _ in range(rows)]

    def regenerate_pressed():
        regenerate_event.set()

    old_knob_handler = btn_knob.when_pressed
    btn_knob.when_pressed = regenerate_pressed
    grid = randomize_grid()

    def count_neighbors(x, y):
        return sum(
            grid[(y + dy) % rows][(x + dx) % cols]
            for dy in (-1, 0, 1)
            for dx in (-1, 0, 1)
            if not (dx == 0 and dy == 0)
        )

    def draw_grid(grid_data, alpha=1.0):
        image = Image.new("1", (width, height))
        draw = ImageDraw.Draw(image)
        for y in range(rows):
            for x in range(cols):
                if grid_data[y][x]:
                    if random.random() < alpha:
                        x0, y0 = x * cell_size, y * cell_size
                        draw.rectangle([x0, y0, x0 + cell_size - 1, y0 + cell_size - 1], fill=255)
        display.image(image)
        display.show()

    try:
        while True:
            if stop_event.is_set() or btn_back.is_pressed:
                return

            if regenerate_event.is_set():
                for alpha in reversed([i / 10 for i in range(11)]):
                    if stop_event.is_set() or btn_back.is_pressed:
                        return
                    draw_grid(grid, alpha)
                    time.sleep(0.03)
                grid = randomize_grid()
                for alpha in [i / 10 for i in range(11)]:
                    if stop_event.is_set() or btn_back.is_pressed:
                        return
                    draw_grid(grid, alpha)
                    time.sleep(0.03)
                regenerate_event.clear()

            new_grid = [[0 for _ in range(cols)] for _ in range(rows)]
            for y in range(rows):
                if stop_event.is_set() or btn_back.is_pressed:
                    return
                for x in range(cols):
                    n = count_neighbors(x, y)
                    if grid[y][x]:
                        new_grid[y][x] = 1 if n in (2, 3) else 0
                    else:
                        new_grid[y][x] = 1 if n == 3 else 0
            grid = new_grid
            draw_grid(grid)
            delay = random.uniform(0.08, 0.14)
            for _ in range(int(delay / 0.01)):
                if stop_event.is_set() or btn_back.is_pressed:
                    return
                time.sleep(0.01)
    finally:
        btn_knob.when_pressed = old_knob_handler

def screen_animation():
    sprite_size = 6
    x, y = 10, 20
    dx, dy = 2, 1
    frame_delay = 0.03
    while not stop_event.is_set():
        image = Image.new("1", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(image)
        draw.rectangle([x, y, x + sprite_size, y + sprite_size], fill=255)
        draw.rectangle([x+1, y+1, x+sprite_size, y+sprite_size], outline=0)
        display.image(image)
        display.show()
        x += dx
        y += dy
        if x <= 0 or x + sprite_size >= WIDTH:  dx = -dx
        if y <= 0 or y + sprite_size >= HEIGHT: dy = -dy
        for _ in range(int(frame_delay * 10)):
            if stop_event.is_set():
                return
            time.sleep(frame_delay)

def screen_clock():
    while not stop_event.is_set():
        now = datetime.now()
        sep = ":" if now.second % 2 == 0 else " "
        hour = now.strftime("%I").lstrip("0") or "12"
        minute = now.strftime("%M")
        second = now.strftime("%S")
        ampm = now.strftime("%p")
        time_str = f"{hour}{sep}{minute}{sep}{second} {ampm}"
        date_str = now.strftime("%b %d, %Y")
        image = Image.new("1", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(image)
        date_w, date_h = draw.textsize(date_str, font=font)
        time_w, time_h = draw.textsize(time_str, font=font)
        block_height = date_h + 4 + time_h
        top_y = (HEIGHT - block_height) // 2
        draw.text(((WIDTH - date_w) // 2, top_y), date_str, font=font, fill=255)
        draw.text(((WIDTH - time_w) // 2, top_y + date_h + 4), time_str, font=font, fill=255)
        display.image(image)
        display.show()
        for _ in range(10):
            if stop_event.is_set():
                return
            time.sleep(0.1)

def screen_uptime():
    while not stop_event.is_set():
        show_text(["System Uptime:", subprocess.getoutput("uptime -p")])
        for _ in range(100):
            if stop_event.is_set():
                return
            time.sleep(0.1)

def screen_loadavg():
    history = deque(maxlen=WIDTH)
    sample_interval = 5.0
    next_sample = time.time()
    while not stop_event.is_set():
        if time.time() >= next_sample:
            load1 = os.getloadavg()[0]
            history.append(load1)
            next_sample = time.time() + sample_interval
        image = Image.new("1", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(image)
        if history:
            latest = history[-1]
            header = f"1m Load Avg {latest:.2f}"
            draw.text((2, 0), header, font=font, fill=255)
            max_load = max(1.0, max(history))
            scale = 40 / max_load
            for x, val in enumerate(history):
                h = int(val * scale)
                y_top = HEIGHT - 1 - h
                draw.line((x, y_top, x, HEIGHT - 1), fill=255)
        else:
            draw.text((2, 0), "1m Load Avg ...", font=font, fill=255)
        display.image(image)
        display.show()
        for _ in range(int(sample_interval * 10)):
            if stop_event.is_set():
                return
            time.sleep(0.1)

def screen_diskspace():
    while not stop_event.is_set():
        partitions = []
        seen_devices = set()
        for p in psutil.disk_partitions(all=False):
            if ("loop" in p.device
                or not os.path.exists(p.mountpoint)
                or p.mountpoint.startswith("/boot/firmware")
                or p.mountpoint == "/boot"):
                continue
            dev = p.device
            if dev in seen_devices:
                continue
            seen_devices.add(dev)
            try:
                usage = psutil.disk_usage(p.mountpoint)
                partitions.append((dev, p.mountpoint, usage.percent))
            except PermissionError:
                continue
        image = Image.new("1", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(image)
        y = 0
        bar_height = 8
        spacing = 4
        for device, mount, percent in partitions[:3]:
            label = f"{device} ({mount})"
            draw.text((0, y), label[:20], font=font, fill=255)
            y += 9
            draw.rectangle([0, y, WIDTH - 1, y + bar_height], outline=255, fill=0)
            fill_width = int(WIDTH * (percent / 100.0))
            if fill_width > 0:
                draw.rectangle([0, y, fill_width, y + bar_height], fill=255)
            text = f"{percent:.0f}%"
            text_w, text_h = draw.textsize(text, font=font)
            text_x = (WIDTH - text_w) // 2
            text_y = y + (bar_height - text_h) // 2
            if text_x + text_w / 2 < fill_width:
                draw.text((text_x, text_y), text, font=font, fill=0)
            else:
                draw.text((text_x, text_y), text, font=font, fill=255)
            y += bar_height + spacing
            if y > HEIGHT - 8:
                break
        if not partitions:
            draw.text((10, 28), "No drives found", font=font, fill=255)
        display.image(image)
        display.show()
        for _ in range(100):
            if stop_event.is_set():
                return
            time.sleep(0.1)

def screen_smart():
    device = "/dev/sda"
    while not stop_event.is_set():
        try:
            output = subprocess.check_output(["sudo", "smartctl", "-a", device], stderr=subprocess.STDOUT).decode()
            health = "UNKNOWN"
            temp = "?"
            reallocated = "?"
            hours = "?"
            for line in output.splitlines():
                if "SMART overall-health self-assessment test result" in line:
                    health = "PASSED" if "PASSED" in line else ("FAILED" if "FAILED" in line else health)
                elif "Temperature_Celsius" in line or "Temperature_Internal" in line:
                    parts = line.split()
                    temp = parts[-1]
                elif "Reallocated_Sector_Ct" in line:
                    parts = line.split()
                    reallocated = parts[-1]
                elif "Power_On_Hours" in line:
                    parts = line.split()
                    hours = parts[-1]
            lines = [f"SMART Status: {health}"]
            if health == "PASSED":
                lines += [f"Temp: {temp} C", f"Hours: {hours}", f"Realloc: {reallocated}"]
            else:
                lines += ["Drive may be failing!"]
            show_text(lines, center=False)
        except subprocess.CalledProcessError as e:
            show_text(["SMART Read Error", str(e)], center=False)
        except FileNotFoundError:
            show_text(["smartctl not found"], center=False)
        for _ in range(300):
            if stop_event.is_set():
                return
            time.sleep(0.1)

# -------------------------------
# Thread orchestration
# -------------------------------
def start_submenu(target):
    global submenu_thread
    stop_event.clear()
    submenu_thread = threading.Thread(target=target, daemon=True)
    submenu_thread.start()

def stop_submenu():
    stop_event.set()
    if submenu_thread and submenu_thread.is_alive():
        submenu_thread.join(timeout=1)
    show_menu()

# -------------------------------
# Actions
# -------------------------------
def perform_action(index):
    actions = {
        0: screen_clock,
        1: screen_uptime,
        2: screen_loadavg,
        3: screen_diskspace,
        4: screen_smart,
        5: screen_animation,
        6: screen_gameoflife,
        7: screen_spaceinvaders,
        8: lambda: system_call("sudo systemctl restart jellyfin"),
        9: lambda: system_call("sudo systemctl restart smbd"),
        10: lambda: system_call("sudo poweroff"),
        11: lambda: system_call("sudo reboot"),
    }
    act = actions.get(index)
    if callable(act):
        start_submenu(act)

# -------------------------------
# Wake to menu from background
# -------------------------------
def wake_to_menu():
    global current_view
    if current_view == "background":
        # Build frames for fade: from current background to menu
        bg_img = draw_system_screen() if not show_weather else draw_weather_screen()
        menu_img = render_menu_image_only()
        fade_to_menu(bg_img, menu_img, duration=FADE_DURATION, steps=10)
        current_view = "menu"
        show_menu()

def render_menu_image_only():
    # helper to generate menu image without showing it immediately
    global menu_offset
    image = Image.new("1", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    if selected_index < menu_offset:
        menu_offset = selected_index
    elif selected_index >= menu_offset + VISIBLE_LINES:
        menu_offset = selected_index - VISIBLE_LINES + 1
    visible_items = menu_items[menu_offset:menu_offset + VISIBLE_LINES]
    total_height = len(visible_items) * 8
    top_margin = max(0, (HEIGHT - total_height) // 2)
    for i, item in enumerate(visible_items):
        y = top_margin + i * 8
        absolute_index = menu_offset + i
        if absolute_index == selected_index:
            ImageDraw.Draw(image).rectangle([0, y, WIDTH - 1, y + 8], fill=255)
            ImageDraw.Draw(image).text((2, y), item, font=font, fill=0)
        else:
            ImageDraw.Draw(image).text((2, y), item, font=font, fill=255)
    return image

# -------------------------------
# Buttons and encoder handlers
# -------------------------------
def confirm_pressed():
    mark_activity()
    if current_view == "background":
        wake_to_menu()
        return
    if submenu_thread and submenu_thread.is_alive():
        stop_event.set()
    else:
        perform_action(selected_index)

def back_pressed():
    mark_activity()
    if current_view == "background":
        wake_to_menu()
        return
    if submenu_thread and submenu_thread.is_alive():
        stop_event.set()
    else:
        show_menu()

def knob_pressed():
    confirm_pressed()

def on_rotate():
    global selected_index
    mark_activity()
    if current_view == "background":
        wake_to_menu()
        return
    if submenu_thread and submenu_thread.is_alive():
        return
    selected_index = encoder.steps % len(menu_items)
    show_menu()

# Wire events
encoder.when_rotated = on_rotate
btn_confirm.when_pressed = lambda: (
    confirm_pressed() if not (submenu_thread and submenu_thread.is_alive())
    else stop_submenu()
)
btn_back.when_pressed = lambda: (
    stop_submenu() if (submenu_thread and submenu_thread.is_alive())
    else back_pressed()
)
btn_knob.when_pressed = knob_pressed

# -------------------------------
# Main loop with idle-to-background logic
# -------------------------------
def show_background_loop_frame():
    global show_weather, background_cycle_start
    if background_cycle_start == 0:
        background_cycle_start = now_ts()
    elapsed = now_ts() - background_cycle_start
    if show_weather and elapsed > 15:
        # slide back to system
        w = draw_weather_screen()
        s = draw_system_screen()
        slide_transition(w, s)
        show_weather = False
        background_cycle_start = now_ts()
    elif (not show_weather) and elapsed > 15:
        # slide to weather
        s = draw_system_screen()
        w = draw_weather_screen()
        slide_transition(s, w)
        show_weather = True
        background_cycle_start = now_ts()
    else:
        # draw current background screen
        frame = draw_weather_screen() if show_weather else draw_system_screen()
        display.image(frame)
        display.show()

def main():
    global current_view, background_cycle_start, show_weather, last_user_activity
    # start on menu
    show_menu()
    last_user_activity = now_ts()
    display_idle_slid = False

    while True:
        time.sleep(0.05)

        # If in submenu, do nothing with idle timer
        if submenu_thread and submenu_thread.is_alive():
            continue

        if current_view == "menu":
            # check for idle timeout only while on the menu
            if now_ts() - last_user_activity >= IDLE_TIMEOUT:
                # build the slide from menu to background system screen
                sys_img = draw_system_screen()
                menu_img = render_menu_image_only()
                slide_transition(menu_img, sys_img, delay=0.01, step=8)
                current_view = "background"
                show_weather = False
                background_cycle_start = now_ts()
        else:
            # background visible: update dashboard, and keep checking for wake via handlers
            show_background_loop_frame()

# Run
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        display.fill(0)
        display.show()

