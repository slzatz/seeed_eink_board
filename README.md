# Seeed EE02 E-Ink Display Project

Display images on a 13.3" Spectra 6 color e-ink display using custom firmware for the Seeed Studio XIAO ePaper Display Board (EE02).

This would not have happened without the esphome EE02 firmware display driver created by [esphome-bigink](https://github.com/acegallagher/esphome-bigink)

##  So what does this project do

Replaces the Seeed factoryi-installed firmware on the EE02 board with custom firmware that:

1. Connects to your WiFi network
2. Fetches images from a simple server that you can run locally or on a remote server
3. Displays the image on a spectra 6 eink screen
4. Goes to sleep to conserve battery (can vary sleep interval)
5. Can skip wakeups during configurable quiet hours
6. Wakes up periodically to check for new images and only refreshes the image if it has changed

---

## What's required

- Python 3.10 or newer
- `uv`
- A Seeed EE02 / XIAO ePaper board with a 13.3" Spectra 6 panel
- A USB-C data cable
- A 2.4 GHz WiFi network

---

## Step-by-Step Setup Guide

### Step 1: Install `uv`

If you do not already have `uv`, install it with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Step 2: Download This Project

```bash
git clone <repository-url>
cd seeed_eink_board
```

Or download and extract the ZIP file from the repository.

### Step 3: Install Project Dependencies

This installs Python packages and PlatformIO:

```bash
uv sync
```

This may take a few minutes the first time as it downloads PlatformIO and the ESP32 toolchain.

### Step 4: Configure Your WiFi Credentials

Copy the example config file and edit it with your credentials:

```bash
cd firmware/src
cp config.h.example config.h
```

Edit `config.h` and change these lines to match your WiFi network:

```cpp
#define WIFI_SSID "YourNetworkName"
#define WIFI_PASSWORD "YourPassword"
```

**Note:**
- The ESP32 only supports **2.4GHz WiFi** (not 5GHz)
- Make sure to keep the quotes around the values

### Step 5: Find Your Computer's IP Address

The board needs the IP address of the computer that will run `image_server.py`.

Linux:
```bash
hostname -I
```

macOS:
```bash
ipconfig getifaddr en0
```

You want the address on your local network, usually something like `192.168.x.x`.

### Step 6: Configure the Image Server Address

Assuming at least for testing purposes you are running the image server from this repository.  Of course you can run it from wherever you like.

Edit `firmware/src/config_manager.h`:

Find this line and change the IP address to your server's IP:

```cpp
#define DEFAULT_SERVER_HOST "192.168.86.33"  // Change this to your IP
```

The other settings should be fine:
- `DEFAULT_SERVER_PORT 5000` - The server runs on port 5000
- `DEFAULT_IMAGE_ENDPOINT "/image_packed"` - The URL path for images
- `DEFAULT_SLEEP_MINUTES 15` - minutes between wakeups during active hours
- `DEFAULT_ACTIVE_START_HOUR 8` - local hour when normal refreshes begin
- `DEFAULT_ACTIVE_END_HOUR 20` - local hour when quiet hours begin
- `DEFAULT_TIMEZONE_OFFSET_MINUTES 0` - minutes from UTC, for example `-300` for EST without DST

The device can also pull these schedule settings from the server later, so this default only has to be good enough to get you started.

### Step 7: Build the Firmware

```bash
cd firmware
uv run pio run
```

The first build takes several minutes as it downloads the ESP32 compiler and libraries. Subsequent builds are much faster.

You should see:
```
========================= [SUCCESS] Took XX.XX seconds =========================
```

### Step 8: Connect the EE02 Board

1. Connect the EE02 board to your computer using a USB-C cable
2. **Note:** If nothing seems to be happening, your cable might be charge-only.

Check if you can see the device:

Linux:
```bash
ls /dev/ttyACM*
```

macOS:
```bash
ls /dev/cu.usb*
```

You should see something like `/dev/ttyACM0` (Linux), `/dev/cu.usbmodem14101` (macOS), or `COM3` (Windows).

You'll probably have to Press the reset button (#4)on the board. The device does not seem to have a separate BOOT button.

### Step 9: Flash the Firmware

**Linux (adjust port if different):**
```bash
uv run pio run -t upload --upload-port /dev/ttyACM0
```

**macOS:**
```bash
uv run pio run -t upload --upload-port /dev/cu.usbmodem14101
```

You should see progress bars and finally:
```
========================= [SUCCESS] Took XX.XX seconds =========================
```

### Step 10: Prepare Images

Go back to the project root directory:
```bash
cd ..
```

Create the images directory and add some images:
```bash
mkdir -p images/default
cp your_photo.jpg images/default/
```

Images will be automatically resized and converted to the display's 6-color palette. You can add multiple images and they will rotate on each refresh.

JPEG and PNG are the safest choices. HEIC is supported if the required Python package is installed, but it can take longer for the server to process.

For device-specific images, see the "Support for multiple boards with different image collections" section below.

Optional: add a schedule override file so frames only wake during the hours you care about:

```bash
cp device_config.example.json images/default/device_config.json
```

The same file can also live in `images/<mac-address>/device_config.json` for a specific board.

If you prefer not to edit JSON manually, open `http://YOUR_SERVER_IP:5000/` after starting the server. The main page now includes embedded schedule editors for the global fallback, the default device schedule, and any devices that have already connected. The focused editor remains available at `http://YOUR_SERVER_IP:5000/schedule`.

### Step 11: Start the Image Server

```bash
uv run python image_server.py
```

You should see:
```
Starting E-Ink Image Server (Multi-Device)...
PIL available: True
HEIC support: True
Default image: image.jpg
Images directory: /path/to/seeed_eink_board/images
Display size: 1600x1200
Device directories found: default, d0cf1326f7e8
 * Serving Flask app 'image_server'
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:5000
 * Running on http://192.168.86.34:5000
```

Leave this running and open a new terminal for the next steps.

Open `http://YOUR_SERVER_IP:5000/` in a browser. That page shows:

- connected devices
- battery voltage reported by each device
- the current image directory for each device
- embedded schedule editors for global, default, and per-device overrides

### Step 12: Test the Display

Press the **reset button** on the EE02 board.

The display should:
1. Connect to WiFi (a few seconds)
2. Sync current time and any schedule overrides from the server
3. Download the image if needed
4. Refresh the display (usually 20-30 seconds of flickering)
5. Go to sleep, potentially until the next active window

Do not worry if the first image takes a while. The server may spend extra time resizing and converting a large image before it starts sending the 960 KB packed display buffer.

**Congratulations! (if that actually worked)** Your e-ink display is now showing your image!

---

## Monitoring what's happening (serial output)

The ESP32 sends debug information over USB. Mainly helpful for troubleshooting.

### Using `screen` (Linux/macOS) or whatever you'd prefer

```bash
screen /dev/ttyACM0 115200
```

Press reset on the board to see output.

### Using PlatformIO Monitor

```bash
cd firmware
uv run pio device monitor --port /dev/ttyACM0 --baud 115200
```

### Following logs across deep sleep

The USB serial device disappears when the board enters deep sleep, so a single `pio device monitor` session usually stops after the first sleep cycle.

This loop reattaches each time the board wakes up:

```bash
cd firmware
while true; do
  uv run pio device monitor --port /dev/ttyACM0 --baud 115200
  sleep 1
done
```

To save that output to a file at the same time:

```bash
script -f /tmp/ee02-monitor.log -c 'bash -lc "cd /home/slzatz/seeed_eink_board/firmware; while true; do uv run pio device monitor --port /dev/ttyACM0 --baud 115200; sleep 1; done"'
```

### What You'll See

Normal operation looks like this:
```
========================================
Seeed EE02 E-Ink Display Firmware
========================================
Boot count: 1

========================================
NORMAL OPERATION MODE
========================================

Battery: ADC=2413, voltage=4.21V
Connecting to WiFi: YourNetwork
.
Connected! IP: 192....
Checking image hash at: http://192.168.86.34:5000/hash
Sending X-Device-MAC: d0cf1326f7e8
Last known hash: (none)
Server hash: 942d3cfc05c8fa41
Image changed - will download new image
Fetching image from: http://192.168.86.34:5000/image_packed
Content length: 960000 bytes
Downloaded 960000 bytes in 10395 ms
Spectra6: Starting display refresh...
Spectra6: Refresh complete in 28432 ms
WiFi disconnected
Entering deep sleep for 15 minutes...
Going to sleep now...
```

When the image hasn't changed:
```
Checking image hash at: http://192.168.86.34:5000/hash
Last known hash: 942d3cfc05c8fa41
Server hash: 942d3cfc05c8fa41
Image unchanged - skipping download
Image unchanged - going back to sleep
WiFi disconnected
Entering deep sleep for 15 minutes...
```

---

## Changing Settings Without Reflashing

You can change the server address, sleep interval, and other settings without reflashing the firmware!

### Entering Configuration Mode

1. Hold Button 1 (GPIO2 - the button closest to the USB connector)
2. While holding Button 1, press and release the reset button (Button 4 next to on/off switch)
3. Continue holding Button 1 for an additional second
4. Release Button 1 - the device will enter configuration mode

### Using the Web Configuration Interface

1. Connect to your WiFi network
2. The serial monitor will show the device's IP address
3. Open a web browser and go to that IP address (e.g., `http://192.168.86.24`)
4. You'll see a configuration page where you can change:
   - **Server Host:** The IP address of your image server
   - **Server Port:** Usually 5000
   - **Image Endpoint:** Usually `/image_packed`
   - **Refresh Interval:** How often to check for new images during active hours (1-1440 minutes)
   - **Active Start / End Hour:** Local wall-clock active window
   - **Timezone Offset:** Minutes from UTC for local scheduling
5. Click **Save Configuration**
6. Click **Reboot Device**

### If WiFi Connection Fails

If the device can't connect to your WiFi in config mode:
1. It will create its own WiFi network called **"EInk-Setup"**
2. Connect your phone or computer to "EInk-Setup"
3. Open a browser to `http://192.168.4.1`
4. Configure the settings

---

## Support for multiple boards with different image collections

You can run multiple EE02 boards from a single image server, each displaying different content. Each board is identified by its MAC address.

### Directory Structure

```
seeed_eink_board/
└── images/
    ├── default/          # Fallback for unknown devices
    │   ├── image1.jpg
    │   └── image2.png
    ├── d0cf1326f7e8/     # First board (MAC without separators)
    │   ├── photo1.jpg
    │   └── photo2.heic
    └── aabbccddeeff/     # Second board
        └── artwork.png
```

### Finding Your Board's MAC Address

1. Enter configuration mode (hold Button 1 during reset)
2. Connect to the configuration page
3. The **Device Info** section shows the MAC address and IP
4. Use the MAC address (lowercase, no colons) as the directory name

### How It Works

1. Each board sends its MAC address with every request via the `X-Device-MAC` header
2. The server looks for `images/<mac-address>/` directory
3. If not found, falls back to `images/default/`
4. Each board maintains its own rotation state independently

### Example Setup for Two Boards

```bash
# Create directories
mkdir -p images/default
mkdir -p images/d0cf1326f7e8    # Kitchen display
mkdir -p images/a1b2c3d4e5f6    # Living room display

# Add images for each
cp kitchen_photos/*.jpg images/d0cf1326f7e8/
cp artwork/*.png images/a1b2c3d4e5f6/
cp fallback.jpg images/default/
```

Each board will cycle through its own set of images independently.

---

## Troubleshooting

### "No such file or directory: /dev/ttyACM0"

The device isn't detected. Try:
1. **Different USB cable** - This is the most common issue! Many cables are charge-only.
2. **Press the reset button** - The device may be in deep sleep
3. **Check the port name** - Run `ls /dev/ttyACM*` (Linux) or `ls /dev/cu.usb*` (macOS)

### WiFi won't connect

- Make sure your network is **2.4GHz**
- Make sure you copied `config.h.example` to `config.h`
- Double-check the SSID and password in your `firmware/src/config.h`
- Rebuild and reflash after changing: `uv run pio run -t upload`

### "HTTP GET failed, code: -1"

The device can't reach the image server:
1. Make sure the image server is running (`uv run python image_server.py`)
2. Check that the server IP address is correct
3. Make sure your firewall allows connections on port 5000
4. Test from another device: `curl http://YOUR_SERVER_IP:5000/hash`

### The server is reachable, but image updates feel slow

- This display is inherently slow to refresh. A full refresh often takes 20-30 seconds.
- The server may also need extra time to resize and quantize a source image before it can send `/image_packed`.
- HEIC images are usually slower to process than JPEG or PNG.
- Watch the server terminal and the firmware log together if you need to separate server processing time from panel refresh time.

### Image is rotated incorrectly

The display is designed for portrait orientation with the board at the bottom. If your image appears rotated, you can edit `image_server.py` and change the rotation value (line with `img.rotate(270,`)

---

## File Structure

```
seeed_eink_board/
├── README.md              # This file
├── image_server.py        # Python server that serves images to the display
├── image.jpg              # Fallback image (optional)
├── images/                # Multi-device image directories
│   ├── default/           # Fallback for unknown devices
│   └── d0cf1326f7e8/      # Device-specific (MAC address)
├── firmware/              # ESP32 firmware
│   ├── platformio.ini     # Build configuration
│   ├── README.md          # Detailed firmware documentation
│   └── src/
│       ├── config.h.example  # WiFi config template (copy to config.h)
│       ├── config_manager.h  # Default server settings
│       └── ...            # Other source files
└── pyproject.toml         # Python project configuration
```

---

## How It Works

```
┌─────────────────┐         ┌─────────────────┐
│  Your Computer  │         │   EE02 Board    │
│                 │         │                 │
│  image_server.py│◄────────│  ESP32 Firmware │
│       :5000     │  WiFi   │                 │
│                 │         │                 │
│   image.jpg     │         │  E-Ink Display  │
└─────────────────┘         └─────────────────┘

1. ESP32 wakes from deep sleep
2. Reads battery voltage via on-board ADC
3. Connects to WiFi
4. Requests `/device_config` from server to sync time and optional schedule overrides
5. If local time is outside the active window: go back to deep sleep until the next start hour
6. Requests `/hash` from server (small request to check if image changed)
   - Sends X-Device-MAC and X-Battery-Voltage headers
7. If hash matches previous: go back to sleep (saves battery!)
8. If hash is different: download `/image_packed` (960KB)
9. Send data to e-ink display
10. Display refreshes
11. ESP32 enters deep sleep for the configured interval or until the next active window
12. Repeat from step 1
```
---

## Battery Monitoring

The firmware reads battery voltage on each wake cycle and sends it to the image server via the `X-Battery-Voltage` HTTP header. The server logs voltage levels and displays them on the status page at `http://your-server:5000/`.

### Voltage Levels

| Voltage | Capacity | Status |
|---------|----------|--------|
| 4.2V+   | Full (or on USB) | GOOD |
| 3.7V    | ~50% | GOOD |
| 3.3V    | ~10% | LOW |
| 3.0V    | Empty (cutoff) | LOW |

Battery status is visible on the server index page and in the `/current` JSON endpoint.

---

## Credits

- Firmware display driver based on [esphome-bigink](https://github.com/acegallagher/esphome-bigink)
