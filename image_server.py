#!/usr/bin/env python3
"""
Image Server for Seeed EE02 E-Ink Display

Serves pre-processed 4bpp packed binary images for the 13.3" Spectra 6 display.
The ESP32 firmware fetches from /image_packed to get display-ready data.

Endpoints:
    /image_packed - Returns 960KB packed binary (4bpp, 1600x1200)
    /hash - Returns 16-char MD5 hash for change detection
    /image - Returns transformed JPEG for preview
    /imagejpg - Returns random front page image

Usage:
    python image_server.py
    # Server runs on http://0.0.0.0:5000
"""

from flask import Flask, send_file, Response, jsonify, request, redirect
import requests
import wand.image
from io import BytesIO
import random
import os
import hashlib
import json
from datetime import datetime
from html import escape
from urllib.parse import quote_plus

# Try to import PIL for image processing
try:
    from PIL import Image, ImageOps, ImageEnhance
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("Warning: PIL not available. /image_packed endpoint will not work.")

# Try to import pillow-heif for HEIC support
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_SUPPORT = True
except ImportError:
    HEIC_SUPPORT = False
    print("Warning: pillow-heif not installed. HEIC files will not be supported.")

# Optional: import frontpage URLs if available
try:
    from frontpageurls import urls
except ImportError:
    urls = []

app = Flask(__name__)

user_agent = "Mozilla/5.0 (Wayland; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
headers = {'User-Agent': user_agent}

# Display configuration
# Note: Buffer is 1600x1200 to match firmware expectations
# Image is rotated 90° CCW for portrait-mounted display
FRAME_WIDTH = 1600
FRAME_HEIGHT = 1200
BUFFER_SIZE = 960000  # (1600 * 1200) / 2 bytes

# The Spectra 6 Color Palette (RGB)
PALETTE_RGB = [
    (0, 0, 0),       # Black
    (255, 255, 255), # White
    (255, 255, 0),   # Yellow
    (255, 0, 0),     # Red
    (0, 0, 255),     # Blue
    (41, 204, 20)    # Green
]

# Map palette index to hardware 4-bit codes
HARDWARE_MAP = {
    0: 0x00,  # Black
    1: 0x01,  # White
    2: 0x02,  # Yellow
    3: 0x03,  # Red
    4: 0x05,  # Blue
    5: 0x06   # Green
}

# Image to display - change this path to your desired image
DEFAULT_IMAGE_PATH = "image.jpg"

# Image rotation configuration
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(SCRIPT_DIR, "images")
STATE_FILE = os.path.join(SCRIPT_DIR, ".eink_rotation_state.json")
DEVICE_CONFIG_FILENAME = "device_config.json"
GLOBAL_DEVICE_CONFIG_PATH = os.path.join(SCRIPT_DIR, DEVICE_CONFIG_FILENAME)
SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.heic', '.webp'}
DEFAULT_DEVICE_ID = "default"
GLOBAL_SCHEDULE_TARGET = "global"
SCHEDULE_KEYS = (
    'refresh_interval_minutes',
    'active_start_hour',
    'active_end_hour',
    'timezone_offset_minutes',
)

# Image enhancement settings
DEFAULT_CONTRAST = 1.2
DEFAULT_BRIGHTNESS = 1.0
DEFAULT_SATURATION = 1.2

# Cache for processed image data
_image_cache = {
    'data': None,
    'hash': None,
    'source_path': None,   # Path to the source image
    'source_mtime': None,  # Modification time of source image
}


def normalize_mac(mac_str: str) -> str:
    """Convert MAC address to lowercase, no separators."""
    return mac_str.lower().replace(':', '').replace('-', '').replace(' ', '')


def load_schedule_config(path: str) -> dict | None:
    """Load and validate an optional schedule config JSON file."""
    if not os.path.exists(path):
        return None

    try:
        with open(path, 'r') as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error loading device config {path}: {e}")
        return None

    if not isinstance(raw, dict):
        print(f"Device config {path} must contain a JSON object")
        return None

    config = {}

    if 'active_start_hour' in raw:
        value = raw['active_start_hour']
        if isinstance(value, int) and 0 <= value <= 23:
            config['active_start_hour'] = value
        else:
            print(f"Ignoring invalid active_start_hour in {path}: {value}")

    if 'active_end_hour' in raw:
        value = raw['active_end_hour']
        if isinstance(value, int) and 0 <= value <= 23:
            config['active_end_hour'] = value
        else:
            print(f"Ignoring invalid active_end_hour in {path}: {value}")

    if 'timezone_offset_minutes' in raw:
        value = raw['timezone_offset_minutes']
        if isinstance(value, int) and -720 <= value <= 840:
            config['timezone_offset_minutes'] = value
        else:
            print(f"Ignoring invalid timezone_offset_minutes in {path}: {value}")

    if 'refresh_interval_minutes' in raw:
        value = raw['refresh_interval_minutes']
        if isinstance(value, int) and 1 <= value <= 1440:
            config['refresh_interval_minutes'] = value
        else:
            print(f"Ignoring invalid refresh_interval_minutes in {path}: {value}")

    return config


def get_device_schedule_config(device_id: str) -> tuple[dict, str]:
    """Resolve schedule config using device-specific, default, then global fallback."""
    candidate_paths = []

    if device_id != DEFAULT_DEVICE_ID:
        candidate_paths.append(
            (os.path.join(IMAGES_DIR, device_id, DEVICE_CONFIG_FILENAME), f"images/{device_id}/{DEVICE_CONFIG_FILENAME}")
        )

    candidate_paths.append(
        (os.path.join(IMAGES_DIR, DEFAULT_DEVICE_ID, DEVICE_CONFIG_FILENAME),
         f"images/{DEFAULT_DEVICE_ID}/{DEVICE_CONFIG_FILENAME}")
    )
    candidate_paths.append((GLOBAL_DEVICE_CONFIG_PATH, DEVICE_CONFIG_FILENAME))

    for path, label in candidate_paths:
        config = load_schedule_config(path)
        if config is not None:
            return config, label

    return {}, "none"


def normalize_schedule_target(target: str | None) -> str:
    """Normalize schedule target identifiers used by the editor UI."""
    if not target:
        return GLOBAL_SCHEDULE_TARGET

    target = target.strip().lower()
    if target in {GLOBAL_SCHEDULE_TARGET, DEFAULT_DEVICE_ID}:
        return target

    return normalize_mac(target)


def get_schedule_config_path(target: str) -> str:
    """Return the exact JSON path for a given schedule target."""
    if target == GLOBAL_SCHEDULE_TARGET:
        return GLOBAL_DEVICE_CONFIG_PATH

    if target == DEFAULT_DEVICE_ID:
        return os.path.join(IMAGES_DIR, DEFAULT_DEVICE_ID, DEVICE_CONFIG_FILENAME)

    return os.path.join(IMAGES_DIR, target, DEVICE_CONFIG_FILENAME)


def describe_schedule_target(target: str) -> str:
    """Human-readable label for schedule targets."""
    if target == GLOBAL_SCHEDULE_TARGET:
        return "Global fallback"
    if target == DEFAULT_DEVICE_ID:
        return "Default device schedule"
    return f"Device {target}"


def get_schedule_editor_state(target: str) -> dict:
    """Collect exact and effective schedule config state for the editor."""
    target = normalize_schedule_target(target)
    exact_path = get_schedule_config_path(target)
    exact_config = load_schedule_config(exact_path) or {}

    if target == GLOBAL_SCHEDULE_TARGET:
        effective_config = exact_config
        effective_source = DEVICE_CONFIG_FILENAME if exact_config else "none"
    else:
        effective_config, effective_source = get_device_schedule_config(target)

    form_values = {}
    for key in SCHEDULE_KEYS:
        value = exact_config.get(key, effective_config.get(key, ''))
        form_values[key] = value

    return {
        'target': target,
        'label': describe_schedule_target(target),
        'exact_path': exact_path,
        'exact_config': exact_config,
        'effective_config': effective_config,
        'effective_source': effective_source,
        'form_values': form_values,
        'has_override': os.path.exists(exact_path),
    }


def save_schedule_config(path: str, config: dict) -> None:
    """Persist a schedule override JSON file."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with open(path, 'w') as f:
        json.dump(config, f, indent=2)
        f.write('\n')


def delete_schedule_config(path: str) -> bool:
    """Delete a schedule override file if it exists."""
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def parse_schedule_form(form) -> tuple[dict | None, str | None]:
    """Validate schedule editor form input."""
    try:
        refresh_interval = int(form.get('refresh_interval_minutes', ''))
        active_start = int(form.get('active_start_hour', ''))
        active_end = int(form.get('active_end_hour', ''))
        timezone_offset = int(form.get('timezone_offset_minutes', ''))
    except ValueError:
        return None, "All schedule fields must be integers."

    if not 1 <= refresh_interval <= 1440:
        return None, "Refresh interval must be between 1 and 1440 minutes."
    if not 0 <= active_start <= 23:
        return None, "Active start hour must be between 0 and 23."
    if not 0 <= active_end <= 23:
        return None, "Active end hour must be between 0 and 23."
    if not -720 <= timezone_offset <= 840:
        return None, "Timezone offset must be between -720 and 840 minutes."

    return {
        'refresh_interval_minutes': refresh_interval,
        'active_start_hour': active_start,
        'active_end_hour': active_end,
        'timezone_offset_minutes': timezone_offset,
    }, None


def get_schedule_targets() -> list[str]:
    """Return editor shortcut targets."""
    targets = {GLOBAL_SCHEDULE_TARGET, DEFAULT_DEVICE_ID}
    targets.update(_rotator.get_all_devices())
    targets.discard('')
    return sorted(targets, key=lambda value: (value not in {GLOBAL_SCHEDULE_TARGET, DEFAULT_DEVICE_ID}, value))


def render_schedule_form_card(target: str, include_target_picker: bool = False,
                              redirect_to: str = "/schedule") -> str:
    """Render a schedule override form card for a specific target."""
    state = get_schedule_editor_state(target)
    exact_json = escape(json.dumps(state['exact_config'], indent=2)) if state['exact_config'] else '{}'
    effective_json = escape(json.dumps(state['effective_config'], indent=2)) if state['effective_config'] else '{}'
    network_info_html = ""

    if target not in {GLOBAL_SCHEDULE_TARGET, DEFAULT_DEVICE_ID}:
        network_info = _device_network_status.get(target)
        if network_info:
            network_info_html = (
                f'<p><strong>Last IP:</strong> <code>{escape(network_info["ip"])}</code><br>'
                f'<span class="hint">Last seen: {escape(network_info["timestamp"])}</span></p>'
            )
        else:
            network_info_html = '<p><strong>Last IP:</strong> <span class="hint">Not seen yet</span></p>'

    target_picker_html = ""
    if include_target_picker:
        target_picker_html = f"""
        <form action="/schedule" method="GET" style="margin-top: 12px;">
          <div class="row">
            <label for="target">Edit target</label>
            <input id="target" type="text" name="target" value="{escape(state['target'])}" placeholder="global, default, or device MAC">
            <div class="hint">Use <code>global</code>, <code>default</code>, or a MAC like <code>d0cf1326f7e8</code>.</div>
          </div>
          <button type="submit">Open Target</button>
        </form>
        """

    return f"""
      <div class="card">
        <h2>{escape(state['label'])}</h2>
        <p><strong>Override file:</strong> <code>{escape(state['exact_path'])}</code></p>
        <p><strong>Effective source:</strong> <code>{escape(state['effective_source'])}</code></p>
        {network_info_html}
        {target_picker_html}
        <form action="/schedule/save" method="POST">
          <input type="hidden" name="target" value="{escape(state['target'])}">
          <input type="hidden" name="redirect_to" value="{escape(redirect_to)}">
          <div class="row">
            <label>Refresh Interval (minutes)</label>
            <input type="number" name="refresh_interval_minutes" min="1" max="1440" value="{escape(str(state['form_values']['refresh_interval_minutes']))}" required>
          </div>
          <div class="row">
            <label>Active Start Hour</label>
            <input type="number" name="active_start_hour" min="0" max="23" value="{escape(str(state['form_values']['active_start_hour']))}" required>
          </div>
          <div class="row">
            <label>Active End Hour</label>
            <input type="number" name="active_end_hour" min="0" max="23" value="{escape(str(state['form_values']['active_end_hour']))}" required>
          </div>
          <div class="row">
            <label>Timezone Offset (minutes from UTC)</label>
            <input type="number" name="timezone_offset_minutes" min="-720" max="840" value="{escape(str(state['form_values']['timezone_offset_minutes']))}" required>
          </div>
          <button type="submit">Save Override</button>
        </form>
        <form action="/schedule/clear" method="POST" style="margin-top:12px;">
          <input type="hidden" name="target" value="{escape(state['target'])}">
          <input type="hidden" name="redirect_to" value="{escape(redirect_to)}">
          <button type="submit" class="danger">Clear Override</button>
          <span class="hint">Deletes only the exact file for this target.</span>
        </form>
        <div style="margin-top:16px;">
          <strong>Exact Override JSON</strong>
          <pre>{exact_json}</pre>
          <strong>Effective Schedule JSON</strong>
          <pre>{effective_json}</pre>
        </div>
      </div>
    """


def render_schedule_editor(target: str, message: str = "", error: str = "") -> str:
    """Render a simple HTML editor for schedule overrides."""
    shortcuts = ''.join(
        f'<li><a href="/schedule?target={escape(schedule_target)}">{escape(describe_schedule_target(schedule_target))}</a></li>'
        for schedule_target in get_schedule_targets()
    )
    message_html = f'<div class="message success">{escape(message)}</div>' if message else ''
    error_html = f'<div class="message error">{escape(error)}</div>' if error else ''

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Schedule Editor</title>
      <style>
        body {{ font-family: Arial, sans-serif; max-width: 720px; margin: 32px auto; padding: 0 16px 48px; background: #f6f7f9; color: #222; }}
        h1, h2 {{ margin-bottom: 0.4rem; }}
        .card {{ background: white; border: 1px solid #ddd; border-radius: 8px; padding: 18px; margin-bottom: 16px; }}
        .row {{ margin-bottom: 14px; }}
        label {{ display: block; font-weight: bold; margin-bottom: 6px; }}
        input[type="text"], input[type="number"] {{ width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #ccc; border-radius: 4px; }}
        button {{ background: #0b67d0; color: white; border: none; padding: 10px 16px; border-radius: 4px; cursor: pointer; margin-right: 8px; }}
        button.danger {{ background: #c43d31; }}
        .message {{ padding: 12px 14px; border-radius: 6px; margin-bottom: 16px; }}
        .success {{ background: #e7f6ea; border: 1px solid #9bd0a7; }}
        .error {{ background: #fdecec; border: 1px solid #e2a4a4; }}
        code, pre {{ background: #eef1f4; border-radius: 4px; }}
        code {{ padding: 2px 5px; }}
        pre {{ padding: 12px; overflow-x: auto; }}
        ul {{ margin-top: 8px; }}
        .hint {{ color: #555; font-size: 0.95em; }}
      </style>
    </head>
    <body>
      <h1>Schedule Editor</h1>
      <p><a href="/">Back to server status</a></p>
      {message_html}
      {error_html}

      {render_schedule_form_card(target, include_target_picker=True)}

      <div class="card">
        <h2>Shortcuts</h2>
        <ul>{shortcuts}</ul>
      </div>
    </body>
    </html>
    """


class ImageRotator:
    """Manages rotation through images in a directory, with per-device state."""

    def __init__(self, images_dir: str, state_file: str):
        self.images_dir = images_dir
        self.state_file = state_file
        self._device_states = {}  # Per-device state: {device_id: {current_index, last_returned}}
        self._load_state()

    def _load_state(self):
        """Load rotation state from JSON file (per-device format)."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                # Check if it's the old single-device format and migrate
                if 'current_index' in state and 'last_returned' in state:
                    # Old format - migrate to new per-device format under 'default'
                    print("Migrating old state file format to per-device format")
                    self._device_states = {
                        DEFAULT_DEVICE_ID: {
                            'current_index': state.get('current_index', 0),
                            'last_returned': state.get('last_returned', None)
                        }
                    }
                    self._save_state()
                else:
                    # New per-device format
                    self._device_states = state
                print(f"Loaded rotation state for {len(self._device_states)} device(s)")
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error loading state file: {e}")
                self._device_states = {}
        else:
            self._device_states = {}

    def _save_state(self):
        """Save rotation state to JSON file (per-device format)."""
        try:
            with open(self.state_file, 'w') as f:
                json.dump(self._device_states, f, indent=2)
        except IOError as e:
            print(f"Error saving state file: {e}")

    def _get_device_state(self, device_id: str) -> dict:
        """Get or create state for a specific device."""
        if device_id not in self._device_states:
            self._device_states[device_id] = {
                'current_index': 0,
                'last_returned': None
            }
        return self._device_states[device_id]

    def _get_device_dir(self, device_id: str) -> str:
        """
        Get the images directory for a specific device.

        Falls back to 'default' directory if device-specific directory doesn't exist.
        """
        device_dir = os.path.join(self.images_dir, device_id)
        if os.path.isdir(device_dir):
            return device_dir

        # Fall back to default directory
        default_dir = os.path.join(self.images_dir, DEFAULT_DEVICE_ID)
        if os.path.isdir(default_dir):
            return default_dir

        # Last resort: use the base images directory (for backward compatibility)
        return self.images_dir

    def _scan_directory(self, device_id: str) -> list[str]:
        """Scan directory and return sorted list of image filenames."""
        device_dir = self._get_device_dir(device_id)
        if not os.path.isdir(device_dir):
            return []

        images = []
        try:
            for entry in os.scandir(device_dir):
                real_path = os.path.realpath(entry.path)
                if not os.path.isfile(real_path):
                    continue
                _, ext = os.path.splitext(entry.name.lower())
                if ext not in SUPPORTED_EXTENSIONS:
                    continue
                images.append(entry.name)
        except OSError as e:
            print(f"Error scanning directory {device_dir}: {e}")
            return []

        images.sort()
        return images

    def peek_next_image(self, device_id: str = DEFAULT_DEVICE_ID) -> str | None:
        """Get path to the next image in rotation without advancing state."""
        images = self._scan_directory(device_id)
        if not images:
            return None

        state = self._get_device_state(device_id)
        device_dir = self._get_device_dir(device_id)

        if state['current_index'] >= len(images):
            state['current_index'] = 0

        image_name = images[state['current_index']]
        return os.path.join(device_dir, image_name)

    def mark_image_served(self, device_id: str = DEFAULT_DEVICE_ID) -> str | None:
        """Advance rotation state after successfully serving the next image."""
        images = self._scan_directory(device_id)
        if not images:
            return None

        state = self._get_device_state(device_id)
        if state['current_index'] >= len(images):
            state['current_index'] = 0

        image_name = images[state['current_index']]
        state['last_returned'] = image_name
        state['current_index'] = (state['current_index'] + 1) % len(images)
        self._save_state()

        return image_name

    def get_next_image(self, device_id: str = DEFAULT_DEVICE_ID) -> str | None:
        """Get path to next image in rotation for a specific device and advance state."""
        image_path = self.peek_next_image(device_id)
        if not image_path:
            return None

        self.mark_image_served(device_id)
        return image_path

    def get_current_image(self, device_id: str = DEFAULT_DEVICE_ID) -> str | None:
        """
        Get the image that was last returned (without advancing).

        Returns:
            str | None: Full path to the current image, or None if none returned yet
        """
        state = self._get_device_state(device_id)
        device_dir = self._get_device_dir(device_id)

        if state['last_returned'] and os.path.exists(os.path.join(device_dir, state['last_returned'])):
            return os.path.join(device_dir, state['last_returned'])
        return None

    def get_status(self, device_id: str = DEFAULT_DEVICE_ID) -> dict:
        """Get current rotation status for a specific device."""
        state = self._get_device_state(device_id)
        images = self._scan_directory(device_id)
        device_dir = self._get_device_dir(device_id)
        return {
            'device_id': device_id,
            'current_image': state['last_returned'],
            'current_index': state['current_index'],
            'total_images': len(images),
            'images_dir': device_dir,
            'image_list': images
        }

    def get_all_devices(self) -> list[str]:
        """Get list of all devices with saved state."""
        return list(self._device_states.keys())


# Initialize the image rotator
_rotator = ImageRotator(IMAGES_DIR, STATE_FILE)

# Battery voltage tracking per device: {device_id: {voltage, timestamp}}
_battery_status = {}

# Last-seen device network info: {device_id: {ip, timestamp}}
_device_network_status = {}


def record_device_request(device_id: str):
    """Track the last IP address and timestamp seen for a device."""
    if device_id == DEFAULT_DEVICE_ID:
        return

    _device_network_status[device_id] = {
        'ip': request.remote_addr or 'unknown',
        'timestamp': datetime.now().isoformat(timespec='seconds')
    }


def log_battery_status(device_id: str):
    """Extract battery voltage from request header and log it."""
    record_device_request(device_id)

    voltage_str = request.headers.get('X-Battery-Voltage')
    if voltage_str:
        try:
            voltage = float(voltage_str)
            _battery_status[device_id] = {
                'voltage': voltage,
                'timestamp': datetime.now().isoformat(timespec='seconds')
            }
            level = "LOW" if voltage < 3.3 else "OK" if voltage < 3.7 else "GOOD"
            print(f"Battery [{device_id}]: {voltage:.2f}V ({level})")
        except ValueError:
            pass


def create_palette_image():
    """Create a palette image for PIL quantization."""
    palette_img = Image.new('P', (1, 1))
    palette_data = []
    for r, g, b in PALETTE_RGB:
        palette_data.extend([r, g, b])
    # Pad to 256 colors (PIL requirement)
    palette_data.extend([0, 0, 0] * (256 - len(PALETTE_RGB)))
    palette_img.putpalette(palette_data)
    return palette_img


def process_image_to_packed(image_path, contrast=DEFAULT_CONTRAST,
                            brightness=DEFAULT_BRIGHTNESS,
                            saturation=DEFAULT_SATURATION):
    """
    Process an image file to packed 4bpp binary data for the Spectra 6 display.

    Args:
        image_path: Path to the source image
        contrast: Contrast enhancement factor (1.0 = original)
        brightness: Brightness enhancement factor (1.0 = original)
        saturation: Saturation enhancement factor (1.0 = original)

    Returns:
        bytes: Packed binary data (960,000 bytes)
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("PIL not available")

    # Open and prepare image
    img = Image.open(image_path)
    img = ImageOps.exif_transpose(img)  # Handle EXIF orientation (camera rotation)
    img = img.convert("RGB")

    # For portrait-mounted display: fit to portrait dimensions first,
    # then rotate to match the 1600x1200 buffer layout expected by firmware
    img = ImageOps.fit(img, (FRAME_HEIGHT, FRAME_WIDTH),  # 1200x1600 portrait
                       method=Image.Resampling.LANCZOS,
                       centering=(0.5, 0.0))

    # Rotate 270° (90° clockwise) to convert portrait image to landscape buffer
    # and match the physical display orientation with board attached at bottom
    img = img.rotate(270, expand=True)

    # Apply enhancements
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if saturation != 1.0:
        img = ImageEnhance.Color(img).enhance(saturation)

    # Quantize to 6-color palette with Floyd-Steinberg dithering
    palette_img = create_palette_image()
    dithered = img.quantize(
        colors=len(PALETTE_RGB),
        palette=palette_img,
        dither=Image.Dither.FLOYDSTEINBERG
    )

    # Pack bits (2 pixels per byte)
    pixels = list(dithered.get_flattened_data())
    packed_data = bytearray()

    for i in range(0, len(pixels), 2):
        p1_idx = pixels[i]
        p2_idx = pixels[i+1] if i+1 < len(pixels) else 0

        val1 = HARDWARE_MAP.get(p1_idx, 0x01)
        val2 = HARDWARE_MAP.get(p2_idx, 0x01)

        byte_val = (val1 << 4) | val2
        packed_data.append(byte_val)

    return bytes(packed_data)


def get_cached_image_data(image_path: str):
    """
    Get processed image data, using cache if source hasn't changed.

    Args:
        image_path: Path to the image file to process

    Returns:
        tuple: (packed_data, hash) or (None, None) if error
    """
    global _image_cache

    if not os.path.exists(image_path):
        return None, None

    # Resolve symlinks for consistent path comparison
    real_path = os.path.realpath(image_path)

    # Check if source image has changed
    current_mtime = os.path.getmtime(real_path)

    if (_image_cache['data'] is not None and
        _image_cache['source_path'] == real_path and
        _image_cache['source_mtime'] == current_mtime):
        # Cache is valid
        return _image_cache['data'], _image_cache['hash']

    # Process the image
    print(f"Processing image: {image_path}")
    packed_data = process_image_to_packed(image_path)

    # Compute hash (using first 16 chars of MD5)
    image_hash = hashlib.md5(packed_data).hexdigest()[:16]

    # Update cache
    _image_cache['data'] = packed_data
    _image_cache['hash'] = image_hash
    _image_cache['source_path'] = real_path
    _image_cache['source_mtime'] = current_mtime

    print(f"Image processed, hash: {image_hash}")
    return packed_data, image_hash


def get_current_image_path(device_id: str = DEFAULT_DEVICE_ID) -> str | None:
    """
    Get the path to the current image to display for a device.

    Uses image rotation if images directory exists with images,
    otherwise falls back to DEFAULT_IMAGE_PATH.

    Args:
        device_id: Device identifier (normalized MAC address)

    Returns:
        str | None: Path to the image file, or None if no image available
    """
    # Try rotation first
    current = _rotator.get_current_image(device_id)
    if current and os.path.exists(current):
        return current

    # Fall back to default
    if os.path.exists(DEFAULT_IMAGE_PATH):
        return DEFAULT_IMAGE_PATH

    return None


def get_next_image_path(device_id: str = DEFAULT_DEVICE_ID) -> str | None:
    """
    Get the path to the next image to display (advances rotation) for a device.

    Uses image rotation if images directory exists with images,
    otherwise falls back to DEFAULT_IMAGE_PATH.

    Args:
        device_id: Device identifier (normalized MAC address)

    Returns:
        str | None: Path to the image file, or None if no image available
    """
    # Try rotation first
    next_image = _rotator.get_next_image(device_id)
    if next_image and os.path.exists(next_image):
        return next_image

    # Fall back to default
    if os.path.exists(DEFAULT_IMAGE_PATH):
        return DEFAULT_IMAGE_PATH

    return None


def get_pending_image_path(device_id: str = DEFAULT_DEVICE_ID) -> str | None:
    """
    Get the next image that would be served to a device without advancing rotation.

    This is used so /hash and /image_packed describe the same image.
    """
    next_image = _rotator.peek_next_image(device_id)
    if next_image and os.path.exists(next_image):
        return next_image

    if os.path.exists(DEFAULT_IMAGE_PATH):
        return DEFAULT_IMAGE_PATH

    return None


def display_image(uri, w=None, h=None):
    """Fetch and process an image from a URL."""
    print(uri)
    try:
        response = requests.get(uri, timeout=5.0, headers=headers)
    except (requests.exceptions.ConnectionError,
            requests.exceptions.TooManyRedirects,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ReadTimeout) as e:
        print(f"requests.get({uri}) generated exception:\n{e}")
        return False

    if response.status_code != 200:
        print(f"status code = {response.status_code}")
        return False

    if response.encoding or response.content.isascii():
        print(f"{uri} returned ascii text and not an image")
        return False

    try:
        img = wand.image.Image(file=BytesIO(response.content))
    except Exception as e:
        print(f"wand.image.Image(file=BytesIO(response.content)) "
              f"generated exception from {uri} {e}")
        return False

    img.transform(resize='825x1600>')

    if img.format == 'JPEG':
        img.save(filename="fp.jpg")
        img.close()
    else:
        print("format is not JPEG")
        return False


@app.route("/hash")
def image_hash():
    """
    Return just the hash of the current image.

    Used by the ESP32 to check if the image has changed before downloading
    the full 960KB. This saves bandwidth and battery when the image hasn't changed.

    Accepts X-Device-MAC header to identify the device.
    """
    if not PIL_AVAILABLE:
        return "PIL not available", 500

    # Get device ID from header
    device_mac = request.headers.get('X-Device-MAC', DEFAULT_DEVICE_ID)
    device_id = normalize_mac(device_mac) if device_mac != DEFAULT_DEVICE_ID else DEFAULT_DEVICE_ID
    log_battery_status(device_id)
    print(f"Hash request from device: {device_id}")

    image_path = get_pending_image_path(device_id)
    if not image_path:
        print(f"Hash request: no pending image for device {device_id}")
        return "No image", 404

    try:
        _, hash_value = get_cached_image_data(image_path)
        if hash_value is None:
            return "No image", 404
        print(f"Hash response for {device_id}: next_image={os.path.basename(image_path)} hash={hash_value}")
        return hash_value
    except Exception as e:
        print(f"Error getting image hash: {e}")
        return f"Error: {e}", 500


@app.route("/device_config")
def device_config():
    """
    Return current server time plus optional per-device schedule overrides.

    The firmware persists any provided values locally and uses its own RTC-backed
    clock plus active window logic to decide how long to sleep.
    """
    device_mac = request.headers.get('X-Device-MAC', DEFAULT_DEVICE_ID)
    device_id = normalize_mac(device_mac) if device_mac != DEFAULT_DEVICE_ID else DEFAULT_DEVICE_ID
    log_battery_status(device_id)

    schedule_config, config_source = get_device_schedule_config(device_id)

    payload = {
        'device_id': device_id,
        'server_time_epoch': int(datetime.now().timestamp()),
        'config_source': config_source,
    }
    payload.update(schedule_config)

    return jsonify(payload)


@app.route("/schedule")
def schedule_editor():
    """Small web UI for editing schedule overrides."""
    target = normalize_schedule_target(request.args.get('target'))
    message = request.args.get('message', '')
    error = request.args.get('error', '')
    return render_schedule_editor(target, message=message, error=error)


@app.route("/schedule/save", methods=["POST"])
def schedule_save():
    """Save a schedule override JSON file."""
    target = normalize_schedule_target(request.form.get('target'))
    redirect_to = request.form.get('redirect_to', '/schedule') or '/schedule'
    config, error = parse_schedule_form(request.form)
    if error:
        if redirect_to == '/':
            return redirect(f"/?error={quote_plus(error)}")
        return render_schedule_editor(target, error=error)

    path = get_schedule_config_path(target)
    save_schedule_config(path, config)
    if redirect_to == '/':
        return redirect(f"/?message={quote_plus('Schedule override saved')}")
    return redirect(f"/schedule?target={target}&message={quote_plus('Schedule override saved')}")


@app.route("/schedule/clear", methods=["POST"])
def schedule_clear():
    """Delete the exact schedule override file for a target."""
    target = normalize_schedule_target(request.form.get('target'))
    redirect_to = request.form.get('redirect_to', '/schedule') or '/schedule'
    path = get_schedule_config_path(target)
    deleted = delete_schedule_config(path)
    message = "Schedule override cleared" if deleted else "No override file existed for this target"
    if redirect_to == '/':
        return redirect(f"/?message={quote_plus(message)}")
    return redirect(f"/schedule?target={target}&message={quote_plus(message)}")


@app.route("/image_packed")
def image_packed():
    """
    Serve pre-processed packed binary data for the E-Ink display.

    Returns 960,000 bytes of 4bpp packed image data that the
    ESP32 firmware can directly load into its display buffer.
    Uses caching to avoid reprocessing unchanged images.

    Each request advances to the next image in rotation.
    Accepts X-Device-MAC header to identify the device and serve device-specific images.
    """
    if not PIL_AVAILABLE:
        return "PIL not available", 500

    # Get device ID from header
    device_mac = request.headers.get('X-Device-MAC', DEFAULT_DEVICE_ID)
    device_id = normalize_mac(device_mac) if device_mac != DEFAULT_DEVICE_ID else DEFAULT_DEVICE_ID
    log_battery_status(device_id)
    print(f"Image request from device: {device_id}")

    # Resolve the next image without advancing so /hash and /image_packed stay in sync.
    image_path = get_pending_image_path(device_id)
    if not image_path:
        return "No images available", 404

    try:
        packed_data, image_hash = get_cached_image_data(image_path)
        if packed_data is None:
            return "Failed to process image", 500

        image_name = os.path.basename(image_path)
        _rotator.mark_image_served(device_id)
        print(
            f"Image response for {device_id}: "
            f"image={image_name} hash={image_hash} bytes={len(packed_data)}"
        )

        return Response(
            packed_data,
            mimetype='application/octet-stream',
            headers={
                'Content-Length': str(len(packed_data)),
                'Content-Disposition': 'attachment; filename=image.bin',
                'X-Image-Hash': image_hash,
                'X-Image-Name': image_name,
                'X-Device-ID': device_id
            }
        )
    except Exception as e:
        print(f"Error processing image: {e}")
        return f"Error: {e}", 500


@app.route("/image")
def image():
    """Serve a transformed JPEG image (for preview/testing)."""
    if not os.path.exists("image.jpg"):
        return "image.jpg not found", 404

    with wand.image.Image(filename='image.jpg') as img:
        img.rotate(90)
        img.transform(resize='825x1600^')
        img.crop(width=825, height=1600, gravity='center')
        img.save(filename="transformed_image.jpg")

    return send_file("transformed_image.jpg", mimetype="image/jpg")


@app.route("/imagejpg")
def imagejpg():
    """Serve a random front page image."""
    if not urls:
        return "No URLs configured", 404

    partial_url = random.choice(urls)
    f = display_image("https://www.frontpages.com" + partial_url, 800, 1200)
    if f is False:
        return "Failed to fetch image", 500
    return send_file("fp.jpg", mimetype="image/jpg")


@app.route("/current")
def current():
    """
    Return JSON with current rotation status.

    Accepts optional X-Device-MAC header or ?device= query param to get status for a specific device.
    Without a device identifier, returns status for all known devices.
    """
    # Get device ID from header or query param
    device_mac = request.headers.get('X-Device-MAC') or request.args.get('device')

    if device_mac:
        device_id = normalize_mac(device_mac)
        status = _rotator.get_status(device_id)
        current_path = get_current_image_path(device_id)
        schedule_config, config_source = get_device_schedule_config(device_id)

        return jsonify({
            'device_id': device_id,
            'current_image': os.path.basename(current_path) if current_path else None,
            'current_path': current_path,
            'rotation': status,
            'schedule_config': schedule_config,
            'config_source': config_source,
            'battery': _battery_status.get(device_id),
            'heic_support': HEIC_SUPPORT,
            'images_dir': IMAGES_DIR,
            'fallback_image': DEFAULT_IMAGE_PATH if os.path.exists(DEFAULT_IMAGE_PATH) else None
        })
    else:
        # Return status for all known devices
        all_devices = _rotator.get_all_devices()
        devices_status = {}
        for dev_id in all_devices:
            status = _rotator.get_status(dev_id)
            current_path = get_current_image_path(dev_id)
            schedule_config, config_source = get_device_schedule_config(dev_id)
            devices_status[dev_id] = {
                'current_image': os.path.basename(current_path) if current_path else None,
                'current_path': current_path,
                'rotation': status,
                'schedule_config': schedule_config,
                'config_source': config_source,
                'battery': _battery_status.get(dev_id)
            }

        return jsonify({
            'devices': devices_status,
            'total_devices': len(all_devices),
            'heic_support': HEIC_SUPPORT,
            'images_dir': IMAGES_DIR,
            'fallback_image': DEFAULT_IMAGE_PATH if os.path.exists(DEFAULT_IMAGE_PATH) else None
        })


@app.route("/")
def index():
    """Show available endpoints and multi-device status."""
    all_devices = _rotator.get_all_devices()
    message = request.args.get('message', '')
    error = request.args.get('error', '')
    message_html = f'<div class="message success">{escape(message)}</div>' if message else ''
    error_html = f'<div class="message error">{escape(error)}</div>' if error else ''
    schedule_cards = [
        render_schedule_form_card(GLOBAL_SCHEDULE_TARGET, redirect_to="/"),
        render_schedule_form_card(DEFAULT_DEVICE_ID, redirect_to="/"),
    ]
    for dev_id in all_devices:
        schedule_cards.append(render_schedule_form_card(dev_id, redirect_to="/"))

    # Build device status table
    device_rows = ""
    for dev_id in all_devices:
        status = _rotator.get_status(dev_id)
        current_path = get_current_image_path(dev_id)
        current_name = os.path.basename(current_path) if current_path else "None"
        batt = _battery_status.get(dev_id)
        if batt:
            v = batt['voltage']
            color = '#c00' if v < 3.3 else '#c90' if v < 3.7 else '#090'
            batt_display = f'<span style="color:{color};font-weight:bold">{v:.2f}V</span><br><small>{batt["timestamp"]}</small>'
        else:
            batt_display = '<span style="color:#999">N/A</span>'
        schedule_config, config_source = get_device_schedule_config(dev_id)
        if schedule_config:
            schedule_summary = (
                f'{schedule_config.get("active_start_hour", "-")}:00-'
                f'{schedule_config.get("active_end_hour", "-")}:00 '
                f'@ {schedule_config.get("timezone_offset_minutes", "-")} min'
            )
        else:
            schedule_summary = 'No override'
        device_rows += f"""
        <tr>
            <td><code>{dev_id}</code></td>
            <td><code>{current_name}</code></td>
            <td>{status['total_images']}</td>
            <td>{batt_display}</td>
            <td><code>{escape(schedule_summary)}</code><br><small>{escape(config_source)}</small></td>
            <td><code>{status['images_dir']}</code></td>
            <td><a href="/schedule?target={escape(dev_id)}">Edit</a></td>
        </tr>"""

    if not device_rows:
        device_rows = "<tr><td colspan='7'>No devices have connected yet</td></tr>"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>E-Ink Image Server</title>
      <style>
        body {{ font-family: Arial, sans-serif; max-width: 1180px; margin: 32px auto; padding: 0 16px 48px; background: #f6f7f9; color: #222; }}
        h1, h2 {{ margin-bottom: 0.4rem; }}
        .card {{ background: white; border: 1px solid #ddd; border-radius: 8px; padding: 18px; margin-bottom: 16px; }}
        .row {{ margin-bottom: 14px; }}
        label {{ display: block; font-weight: bold; margin-bottom: 6px; }}
        input[type="text"], input[type="number"] {{ width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #ccc; border-radius: 4px; }}
        button {{ background: #0b67d0; color: white; border: none; padding: 10px 16px; border-radius: 4px; cursor: pointer; margin-right: 8px; }}
        button.danger {{ background: #c43d31; }}
        .message {{ padding: 12px 14px; border-radius: 6px; margin-bottom: 16px; }}
        .success {{ background: #e7f6ea; border: 1px solid #9bd0a7; }}
        .error {{ background: #fdecec; border: 1px solid #e2a4a4; }}
        .hint {{ color: #555; font-size: 0.95em; }}
        .schedule-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; align-items: start; }}
        table {{ width: 100%; background: white; border-collapse: collapse; }}
        th, td {{ border: 1px solid #d9d9d9; padding: 8px; vertical-align: top; text-align: left; }}
        code, pre {{ background: #eef1f4; border-radius: 4px; }}
        code {{ padding: 2px 5px; }}
        pre {{ padding: 12px; overflow-x: auto; }}
        ul {{ margin-top: 8px; }}
      </style>
    </head>
    <body>
    <h1>E-Ink Image Server (Multi-Device)</h1>
    {message_html}
    {error_html}
    <h2>Endpoints</h2>
    <ul>
        <li><a href="/image_packed">/image_packed</a> - Packed binary for ESP32 (960KB, advances rotation)</li>
        <li><a href="/hash">/hash</a> - Image hash for change detection (16 chars)</li>
        <li><a href="/device_config">/device_config</a> - Current epoch time plus optional schedule overrides</li>
        <li><a href="/schedule">/schedule</a> - Browser UI for editing schedule overrides</li>
        <li><a href="/current">/current</a> - Current rotation status (JSON)</li>
        <li><a href="/image">/image</a> - Transformed JPEG preview</li>
        <li><a href="/imagejpg">/imagejpg</a> - Random front page image</li>
    </ul>
    <p><em>Endpoints accept <code>X-Device-MAC</code> header for device identification.</em></p>

    <h2>Schedule Shortcuts</h2>
    <ul>
        <li><a href="/schedule?target=global">Edit global fallback schedule</a></li>
        <li><a href="/schedule?target=default">Edit default device schedule</a></li>
    </ul>

    <h2>Schedule Editor</h2>
    <div class="schedule-grid">
    {''.join(schedule_cards)}
    </div>

    <h2>Device Status</h2>
    <table border="1" cellpadding="8" cellspacing="0">
        <tr>
            <th>Device ID (MAC)</th>
            <th>Current Image</th>
            <th>Total Images</th>
            <th>Battery</th>
            <th>Effective Schedule</th>
            <th>Images Directory</th>
            <th>Schedule</th>
        </tr>
        {device_rows}
    </table>

    <h2>Configuration</h2>
    <ul>
        <li>Images directory: <code>{IMAGES_DIR}</code></li>
        <li>HEIC support: {'Yes' if HEIC_SUPPORT else 'No'}</li>
        <li>Fallback image: <code>{DEFAULT_IMAGE_PATH}</code></li>
    </ul>

    <h2>Directory Structure</h2>
    <pre>
images/
├── default/          # Fallback for unknown devices
│   ├── image1.jpg
│   ├── image2.png
│   └── device_config.json  # Optional default schedule override
├── d0cf1326f7e8/     # Device-specific (MAC without separators)
│   ├── photo.jpg
│   └── device_config.json  # Optional per-device override
└── aabbccddeeff/     # Another device
    └── ...
    </pre>
    <p>Create a directory named after the device's MAC address (lowercase, no separators) to serve device-specific images.</p>
    <p>Optional schedule overrides live in <code>device_config.json</code> and can set <code>active_start_hour</code>, <code>active_end_hour</code>, <code>timezone_offset_minutes</code>, and <code>refresh_interval_minutes</code>.</p>
    </body>
    </html>
    """


if __name__ == "__main__":
    print("Starting E-Ink Image Server (Multi-Device)...")
    print(f"PIL available: {PIL_AVAILABLE}")
    print(f"HEIC support: {HEIC_SUPPORT}")
    print(f"Default image: {DEFAULT_IMAGE_PATH}")
    print(f"Images directory: {IMAGES_DIR}")
    print(f"Display size: {FRAME_WIDTH}x{FRAME_HEIGHT}")

    # Check for device directories
    if os.path.isdir(IMAGES_DIR):
        subdirs = [d for d in os.listdir(IMAGES_DIR)
                   if os.path.isdir(os.path.join(IMAGES_DIR, d))]
        if subdirs:
            print(f"Device directories found: {', '.join(subdirs)}")
        else:
            print(f"No device directories in {IMAGES_DIR}")
            print(f"Create images/default/ or images/<mac-address>/ directories")
    else:
        print(f"Images directory not found: {IMAGES_DIR}")
        print(f"Create {IMAGES_DIR}/default/ directory and add images")

    # Show known devices from state
    known_devices = _rotator.get_all_devices()
    if known_devices:
        print(f"Known devices from state: {', '.join(known_devices)}")

    app.run(debug=True, host='0.0.0.0', port=5000)
