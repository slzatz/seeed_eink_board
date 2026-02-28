# EE02 E-Ink Display Firmware

Custom firmware for the Seeed Studio XIAO ePaper Display Board (EE02) driving a 13.3" Spectra 6 e-ink display.

## Features

- Fetches images from a configurable HTTP server
- **Hash-based change detection** - only downloads and refreshes when the image changes
- Deep sleep between refreshes for battery conservation
- Runtime configuration via web interface (no reflashing needed)
- Support for the 6-color Spectra 6 palette (Black, White, Yellow, Red, Blue, Green)

## Prerequisites

- [PlatformIO](https://platformio.org/) (CLI or VSCode extension)
- USB-C cable with data lines (not charge-only)
- Python 3.x with `uv` for the image server

## Quick Start

### 1. Configure WiFi Credentials

Copy the example config and edit with your credentials:

```bash
cp src/config.h.example src/config.h
```

Edit `src/config.h` and set your WiFi credentials:

```cpp
#define WIFI_SSID "YourNetworkName"
#define WIFI_PASSWORD "YourPassword"
```

### 2. Set Default Server Address (Optional)

Edit `src/config_manager.h` to set the default image server:

```cpp
#define DEFAULT_SERVER_HOST "192.168.86.34"  // Your server's IP
#define DEFAULT_SERVER_PORT 5000
#define DEFAULT_IMAGE_ENDPOINT "/image_packed"
#define DEFAULT_SLEEP_MINUTES 15
#define DEFAULT_ACTIVE_START_HOUR 8
#define DEFAULT_ACTIVE_END_HOUR 20
#define DEFAULT_TIMEZONE_OFFSET_MINUTES 0
```

### 3. Build the Firmware

```bash
cd firmware
uv run pio run
```

### 4. Flash the Firmware

Connect the EE02 board via USB. If the device is in deep sleep, press the reset button to wake it.

```bash
uv run pio run -t upload --upload-port /dev/ttyACM0
```

**Note:** The USB port may vary. On Linux it's typically `/dev/ttyACM0`, on macOS `/dev/cu.usbmodem*`, on Windows `COM3` or similar.

If the device isn't detected, try a different USB cable - many cables are charge-only and lack data lines.

### 5. Start the Image Server

In the repository root:

```bash
# Create a symlink to your image
ln -sf your_image.jpg image.jpg

# Start the server
uv run python image_server.py
```

The server runs on `http://0.0.0.0:5000` with these endpoints:
- `/device_config` - Current epoch time plus optional schedule overrides
- `/` - Status page with embedded schedule editors
- `/schedule` - Focused browser UI for editing schedule overrides
- `/image_packed` - 960KB binary data for the display
- `/hash` - 16-character hash for change detection
- `/image` - JPEG preview

### 6. Test

Press the reset button on the EE02 board. The display should:
1. Connect to WiFi
2. Sync current time and optional schedule overrides from `/device_config`
3. Skip work and go back to sleep if it is currently in quiet hours
4. Check the image hash
5. Download the image (if changed)
6. Refresh the display (takes 20-30 seconds)
7. Enter deep sleep

## Monitoring Serial Output

The firmware outputs debug information via USB serial at 115200 baud.

### Using `cat` (simplest)

```bash
# Set baud rate and read output
stty -F /dev/ttyACM0 115200 raw -echo
cat /dev/ttyACM0
```

### Using `screen`

```bash
screen /dev/ttyACM0 115200
# Press Ctrl+A then K to exit
```

### Using PlatformIO Monitor

```bash
uv run pio device monitor --port /dev/ttyACM0 --baud 115200
```

### Important: Deep Sleep Disconnects USB

When the ESP32-S3 enters deep sleep, the USB connection is lost. This is normal behavior. To see output:

1. Start your serial monitor
2. Press the reset button on the board
3. Output will appear as the device boots

If you want the monitor to reconnect automatically after each sleep cycle:

```bash
while true; do
  uv run pio device monitor --port /dev/ttyACM0 --baud 115200
  sleep 1
done
```

### Example Output

```
========================================
Seeed EE02 E-Ink Display Firmware
========================================
Boot count: 1
Wakeup was not from deep sleep (code: 0)
ConfigManager: Initialized
Current Configuration:
  Server: 192.168.86.34:5000
  Endpoint: /image_packed
  Full URL: http://192.168.86.34:5000/image_packed
  Refresh interval: 15 minutes
  Active window: 08:00-20:00
  Timezone offset: 0 minutes from UTC

========================================
NORMAL OPERATION MODE
========================================

Connecting to WiFi: YourNetwork
.
Connected! IP: 192.168.86.24
Fetching device config from: http://192.168.86.34:5000/device_config
Clock synchronized from server epoch: 1772290800
Clock status: utc=1772290800, local=08:00, active_window=yes
Checking image hash at: http://192.168.86.34:5000/hash
Last known hash: (none)
Server hash: 942d3cfc05c8fa41
Image changed - will download new image
Spectra6: Initializing display...
Spectra6: Buffer allocated in PSRAM (960000 bytes)
Fetching image from: http://192.168.86.34:5000/image_packed
Content length: 960000 bytes
Downloaded 960000 bytes in 10395 ms
Spectra6: Starting display refresh...
Spectra6: Data transfer complete in 3405 ms
Spectra6: Sending refresh command (this takes 20-30 seconds)...
Spectra6: Refresh complete in 28432 ms
WiFi disconnected
Entering deep sleep for 15 minutes 0 seconds...
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
Entering deep sleep for 15 minutes 0 seconds...
```

When the device wakes during quiet hours:
```
Fetching device config from: http://192.168.86.34:5000/device_config
Clock synchronized from server epoch: 1772337600
Clock status: utc=1772337600, local=21:00, active_window=no
Currently in quiet hours - skipping hash/image fetch
WiFi disconnected
Outside active window - sleeping until next active start in 39600 seconds
Entering deep sleep for 660 minutes 0 seconds...
```

## Changing Configuration at Runtime

The firmware supports runtime configuration without reflashing.

### Entering Configuration Mode

**Hold Button 1 during reset:**
1. Hold Button 1 (GPIO2)
2. While holding, press and release the reset button
3. Continue holding Button 1 for an additional second
4. Release Button 1

The device will enter configuration mode and either:
- **STA mode**: Connect to your WiFi and show its IP address
- **AP mode**: Create a WiFi network called "EInk-Setup" if WiFi fails

### Web Configuration Interface

1. Open a browser to the device's IP address (shown in serial output)
   - Or connect to "EInk-Setup" WiFi and go to `http://192.168.4.1`

2. Configure these settings:
   - **Server Host**: IP address or domain name (e.g., `192.168.86.34`)
   - **Server Port**: Usually `5000`
   - **Image Endpoint**: Path to the image (e.g., `/image_packed`)
   - **Refresh Interval**: Minutes between wakeups during active hours (1-1440)
   - **Active Start Hour**: Local hour when image checks begin (0-23)
   - **Active End Hour**: Local hour when quiet hours begin (0-23)
   - **Timezone Offset**: Minutes from UTC used for local wall-clock scheduling

3. Click "Save Configuration"

4. Click "Reboot Device" to start normal operation

### Configuration Persistence

Settings are stored in NVS (Non-Volatile Storage) and persist across:
- Reboots
- Deep sleep cycles
- Power loss

To reset to defaults, use the "Reset Defaults" button in the web interface.

### Remote Schedule Overrides

The image server can override the local schedule by serving `device_config.json`.

- Global override: `device_config.json` in the repository root
- Default device override: `images/default/device_config.json`
- Per-device override: `images/<mac-address>/device_config.json`

Example:

```json
{
  "refresh_interval_minutes": 60,
  "active_start_hour": 8,
  "active_end_hour": 20,
  "timezone_offset_minutes": -480
}
```

Only the keys you include are overridden; everything else stays on the device's locally stored configuration.

If you prefer not to edit JSON by hand, start `image_server.py` and open the main page at `http://your-server:5000/`. It includes embedded schedule editors for the global fallback, the default schedule, and each device that has already contacted the server.

## Troubleshooting

### Device not detected via USB

1. **Try a different USB cable** - Many cables are charge-only
2. Check if device appears: `ls /dev/ttyACM*` (Linux) or `ls /dev/cu.usb*` (macOS)
3. The device may be in deep sleep - press reset to wake it

### WiFi connection fails

- Verify SSID and password in your `src/config.h`
- Make sure you copied `config.h.example` to `config.h`
- Check that your network is 2.4GHz (ESP32 doesn't support 5GHz)
- Rebuild and reflash after changing credentials

### HTTP requests fail (code: -1)

- Verify the server is running: `curl http://your-server:5000/hash`
- Check the server IP address matches your configuration
- Ensure firewall allows connections on port 5000

### Display doesn't refresh

- Check serial output for errors
- Verify the image server returns valid data: `curl http://localhost:5000/hash`
- The refresh takes 20-30 seconds, and the server may need extra time to process a large image before the download starts
- HEIC files often take longer to process than JPEG or PNG

### Image appears rotated

The image orientation depends on how you position the display. You can:
- Rotate the source image before serving
- Or modify the image processing in `image_server.py`

## File Structure

```
firmware/
├── platformio.ini          # PlatformIO project configuration
├── README.md               # This file
└── src/
    ├── config.h.example    # WiFi config template (copy to config.h)
    ├── config_manager.h    # Default server settings
    ├── config_manager.cpp  # NVS-based configuration storage
    ├── config_server.h     # Web configuration interface
    ├── config_server.cpp   # HTTP server for configuration
    ├── display.h           # Display driver interface
    ├── display.cpp         # Spectra 6 display driver
    └── main.cpp            # Main application logic
```

## Hardware Reference

### Pin Configuration (EE02 Board)

| Function | GPIO | Notes |
|----------|------|-------|
| SPI CLK | 7 | Shared by both controllers |
| SPI MOSI | 9 | Shared by both controllers |
| CS Master | 44 | Top half of display (rows 0-599) |
| CS Slave | 41 | Bottom half of display (rows 600-1199) |
| DC | 10 | Data/Command select |
| Reset | 38 | Hardware reset |
| Busy | 4 | LOW when busy, HIGH when ready |
| Power | 43 | Display power control |

### Display Specifications

- Resolution: 1600 x 1200 pixels
- Colors: 6 (Black, White, Yellow, Red, Blue, Green)
- Data format: 4-bit per pixel (2 pixels per byte)
- Buffer size: 960,000 bytes
- Refresh time: 20-30 seconds

### Color Codes

| Color | Hardware Code |
|-------|---------------|
| Black | 0x00 |
| White | 0x01 |
| Yellow | 0x02 |
| Red | 0x03 |
| Blue | 0x05 |
| Green | 0x06 |

## Power Consumption

- **Active (WiFi + display refresh)**: ~150-200mA
- **Deep sleep**: ~10µA

For battery operation, increase the sleep interval to maximize battery life. At 15-minute intervals, the device is active for roughly 1 minute per hour.

## Credits

- Display driver based on [esphome-bigink](https://github.com/acegallagher/esphome-bigink)
- Image processing based on the GooDisplay project in `~/eink`
