import RPi.GPIO as GPIO
import time
import psutil
import os

# --- LED Setup ---
LED_PINS = [20, 21, 13, 26]
GPIO.setmode(GPIO.BCM)
GPIO.setup(LED_PINS, GPIO.OUT)

BLINK_DELAY = 0.15  # Light pattern speed
IDLE_DELAY = 0.5    # Delay during idle pulse
ACTIVITY_THRESHOLD = 1024 * 10  # bytes/sec threshold to trigger show

def all_off():
    for p in LED_PINS:
        GPIO.output(p, GPIO.LOW)

def circular_spin(repeats=1):
    for _ in range(repeats):
        for p in LED_PINS:
            GPIO.output(p, GPIO.HIGH)
            time.sleep(BLINK_DELAY)
            GPIO.output(p, GPIO.LOW)

def idle_pulse():
    all_off()

def get_mount_device(mount_point="/srv/nas"):
    """Find the device (like /dev/sda1) that backs this mount."""
    for part in psutil.disk_partitions(all=False):
        if part.mountpoint == mount_point:
            return os.path.basename(part.device)
    return None

def get_io_counters(dev_name):
    """Return read/write bytes for given device name (e.g. sda1)."""
    counters = psutil.disk_io_counters(perdisk=True)
    return counters.get(dev_name, None)

print("Starting NAS Activity Light Show. Press Ctrl+C to stop.")

device = get_mount_device("/srv/nas")
if not device:
    print("Error: Could not find device for /srv/nas.")
    GPIO.cleanup()
    exit(1)

print(f"Monitoring device: {device}")

try:
    last_io = get_io_counters(device)
    if not last_io:
        raise RuntimeError("No IO stats found for device.")

    while True:
        time.sleep(0.5)
        current_io = get_io_counters(device)
        if not current_io:
            continue

        read_diff = current_io.read_bytes - last_io.read_bytes
        write_diff = current_io.write_bytes - last_io.write_bytes
        activity = read_diff + write_diff
        last_io = current_io

        if activity > ACTIVITY_THRESHOLD:
            print(f"Disk activity detected: {activity} bytes/s → light show!")
            circular_spin(2)
        else:
            idle_pulse()

except KeyboardInterrupt:
    print("\nExiting...")

finally:
    all_off()
    GPIO.cleanup()

