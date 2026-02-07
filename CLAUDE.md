Seeed studios makes a development board called the XIAO ePaper Display Board - EE02 that is designed to drive an e ink spectra 6 13.3 inch display.  The board is based on the ESP32-s3 chip and supports WiFi and Bluetooth connectivity.

The board is relatively new and there is limited documentation and community support available for it. However, Seeed Studio recently published this documentation: https://wiki.seeedstudio.com/getting_started_with_ee02/#getting-started-with-arduino

There is also a github repository that is trying to do the same thing we are in terms of directly addressing the EE02 XIAO ePaper Display Board at https://github.com/acegallagher/esphome-bigink.

Note that in this repository there is also documentation of the 13.3 inch spectra 6 driver: 13_3_E6_eInk_Display_module_Datasheet.pdf

Seeed provides a web app called the SenseCraft HMI platform to communicate with the e ink display. However. we don't want to go through the WebApp to display images, we want to directly hit the api endpoints that the custom firmware that we build in this repository supports.  If you look at the ~/eink repository on this computer, you will see that I have done something similar with the GooDisplay e ink driver board. We used the GooDisplay web app to reverse engineer the api endpoints and then wrote a python script to hit those endpoints directly but the GooDisplay web app was pretty simple.

This repository now contains custom firmware for the ESP32 on the EEO2 board that runs a web client that generates http requests to our custom image server to display images on the 13.3 inch spectra 6 display.  We also have the capability to put the ESP32 to sleep and have it wake up at intervals to update the display.  The ESP32 wakes up, connects to WiFi, makes a request to the image server to get the image to display, displays the image, and then goes back to sleep.  The image server rotates through the various images in folders that use the MAC address of the EEO@ board.  We can manage the images that each screen displays by adding or deleting images from that screen's image folder (named for its MAC address)

## Custom Firmware Implementation

We have implemented custom Arduino/PlatformIO firmware in the `firmware/` directory:

### Architecture

```
[Home Server]                    [EE02 Board]
image_server.py                  Arduino Firmware
      │                                │
      │ GET /image_packed              │
      │◄──────────────────────────────│ (wake from deep sleep)
      │                                │
      │ Returns packed binary          │
      │ (960KB, pre-dithered)          │
      │──────────────────────────────►│
      │                                │
      │                                │ Display image
      │                                │ (dual-controller SPI)
      │                                │
      │                                │ Deep sleep (15 min default)
      │                                ▼
```

### Display Hardware Details

The 13.3" Spectra 6 display uses dual UC8179 controllers in master/slave configuration:

- **Master (CS=GPIO44)**: Top 600 pixel rows (0-599)
- **Slave (CS=GPIO41)**: Bottom 600 pixel rows (600-1199)
- Both controllers share CLK (GPIO7) and MOSI (GPIO9)

**Other GPIO pins:**
- DC: GPIO10
- Reset: GPIO38
- Busy: GPIO4 (HIGH when busy)
- Power: GPIO43

**Battery monitoring pins (same circuit as EE04 board):**
- Battery ADC: GPIO1 (A0) - voltage divider output
- ADC Enable: GPIO6 (A5) - set HIGH to enable voltage divider, LOW to save power

**Data Format:**
- 4-bit per pixel (2 pixels per byte)
- Total buffer: 960,000 bytes
- Data is transposed during transfer: buffer columns become output rows

### Files

- `firmware/platformio.ini` - PlatformIO project configuration
- `firmware/src/config.h` - WiFi credentials and pin definitions
- `firmware/src/config_manager.h/.cpp` - Persistent configuration storage (NVS)
- `firmware/src/config_server.h/.cpp` - Web-based configuration interface
- `firmware/src/display.h/.cpp` - Spectra 6 display driver (ported from esphome-bigink)
- `firmware/src/main.cpp` - Main loop: WiFi, fetch, display, deep sleep, config mode
- `image_server.py` - Flask server with image rotation and `/image_packed` endpoint
- `.eink_rotation_state.json` - Persisted rotation state (auto-generated, gitignored)

### Runtime Configuration

The server endpoint is configurable at runtime without reflashing:

**To enter configuration mode:**
1. Press the reset button twice quickly (within 2 seconds)
2. The device will either:
   - Connect to your WiFi and show its IP address (open in browser)
   - Or create a WiFi network called "EInk-Setup" (connect and go to http://192.168.4.1)

**Configurable settings:**
- Server host (IP or domain name, e.g., "192.168.86.100" or "myserver.linode.com")
- Server port
- Image endpoint path
- Sleep interval (how often to refresh)

Configuration is stored in NVS (Non-Volatile Storage) and persists across reboots.

### Building and Flashing

1. Install PlatformIO (VSCode extension or CLI)
2. Edit `firmware/src/config.h` with your WiFi credentials (WIFI_SSID and WIFI_PASSWORD)
3. Optionally edit `firmware/src/config_manager.h` to change default server settings
4. Connect EE02 board via USB
5. Build and upload:
   ```bash
   cd firmware
   pio run -t upload
   ```

### Running the Image Server

1. Install dependencies: `uv sync`
2. Create the images directory structure (see Multi-Device Support below)
3. Run: `uv run python image_server.py`
4. Server listens on http://0.0.0.0:5000

**Endpoints:**
- `/image_packed` - Returns 960KB of pre-processed 4bpp binary data (advances to next image)
- `/hash` - Returns 16-char MD5 hash for change detection
- `/current` - Returns JSON with current rotation status (all devices or specific device)
- `/` - Index page with multi-device status overview

All endpoints accept the `X-Device-MAC` header to identify which device is making the request.
The firmware also sends an `X-Battery-Voltage` header with the current battery voltage (e.g., "3.85").

### Multi-Device Support

The server supports multiple EE02 boards, each with their own image rotation. Devices are identified by their MAC address (sent via `X-Device-MAC` HTTP header).

**Directory Structure:**
```
seeed_eink_board/
├── images/
│   ├── default/          # Fallback for unknown devices
│   │   ├── image1.jpg
│   │   └── image2.png
│   ├── d0cf1326f7e8/     # Device-specific (MAC without separators)
│   │   ├── photo1.jpg
│   │   └── photo2.heic
│   └── aabbccddeeff/     # Another device
│       └── ...
├── image_server.py
└── .eink_rotation_state.json  # Tracks state per-device
```

**How it works:**
1. Each ESP32 sends its MAC address (lowercase, no separators) via the `X-Device-MAC` header
2. The server looks for a directory named `images/<mac-address>/`
3. If not found, it falls back to `images/default/`
4. Each device maintains its own rotation state (current index, last returned image)

**Finding your device's MAC:**
- Enter configuration mode on the EE02 (hold Button 1 during reset)
- The configuration page shows the device's MAC address and IP
- Use this MAC address (without colons, lowercase) as the directory name

**State File Format:**
```json
{
  "d0cf1326f7e8": {
    "current_index": 3,
    "last_returned": "image.jpg"
  },
  "default": {
    "current_index": 0,
    "last_returned": "fallback.png"
  }
}
```

### Image Rotation

The server rotates through images in device-specific or default directories:

- **Supported formats:** `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.heic`, `.webp`
- **HEIC support:** Enabled via `pillow-heif` library (handles iPhone photos directly)
- **Rotation order:** Alphabetical by filename
- **Persistence:** Rotation state (per-device) saved to `.eink_rotation_state.json`
- **Symlinks:** Supported - can link to images stored elsewhere
- **Fallback:** If device directory doesn't exist, uses `images/default/`; if that's empty, falls back to `image.jpg` in repository root
- **Dynamic updates:** Directory is scanned on each request, so adding/removing images takes effect immediately

Each request to `/image_packed` advances to the next image in rotation for that specific device.

### Battery Monitoring

The EE02 board has a voltage divider circuit (same as the EE04 board) that allows reading battery voltage via ADC:

- **GPIO1 (A0):** Battery voltage ADC input (through voltage divider)
- **GPIO6 (A5):** ADC enable pin - must be set HIGH before reading
- **Scaling factor:** 7.16 (voltage divider ratio, from EE04 reference)
- **Note:** GPIO1 is NOT a button despite earlier assumptions. The three physical keys on the board are on GPIO2, GPIO3, and GPIO5 (matching EE04 layout).

The firmware reads battery voltage once per boot (before WiFi to avoid ADC noise) and sends it to the server via the `X-Battery-Voltage` HTTP header. The server logs voltage levels and displays them on the status page with color coding:
- **RED:** < 3.3V (low, charge soon)
- **YELLOW:** 3.3V - 3.7V (OK)
- **GREEN:** > 3.7V (good)

Typical LiPo voltage range: 3.0V (empty) to 4.2V (full). Readings above 4.2V indicate USB power.

### Color Palette

The Spectra 6 supports 6 colors with these hardware codes:
- 0x00: Black
- 0x01: White
- 0x02: Yellow
- 0x03: Red
- 0x05: Blue
- 0x06: Green

### Reference
- Seeed documentation: https://wiki.seeedstudio.com/getting_started_with_ee02/#getting-started-with-arduino
- Seeed GFX library (cloned locally at ~/Seeed_GFX): Contains the official T133A01 display driver. Our init register values and sequences have been verified to match exactly. The library defines this board/display combo as `BOARD_SCREEN_COMBO 510` with `USE_XIAO_EPAPER_DISPLAY_BOARD_EE02`.
- Display driver based on: https://github.com/acegallagher/esphome-bigink
- Image processing based on: ~/eink/send_to_display.py (GooDisplay project)
- Battery ADC circuit based on EE04 documentation: https://wiki.seeedstudio.com/epaper_ee04/
