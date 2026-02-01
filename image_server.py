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

from flask import Flask, send_file, Response, jsonify, request
import requests
import wand.image
from io import BytesIO
import random
import os
import hashlib
import json

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
SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.heic', '.webp'}
DEFAULT_DEVICE_ID = "default"

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

    def get_next_image(self, device_id: str = DEFAULT_DEVICE_ID) -> str | None:
        """Get path to next image in rotation for a specific device."""
        images = self._scan_directory(device_id)
        if not images:
            return None

        state = self._get_device_state(device_id)
        device_dir = self._get_device_dir(device_id)

        # Handle case where image list changed (files added/removed)
        if state['current_index'] >= len(images):
            state['current_index'] = 0

        image_name = images[state['current_index']]
        state['last_returned'] = image_name
        state['current_index'] = (state['current_index'] + 1) % len(images)
        self._save_state()

        return os.path.join(device_dir, image_name)

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
    print(f"Hash request from device: {device_id}")

    image_path = get_current_image_path(device_id)
    if not image_path:
        return "No image", 404

    try:
        _, hash_value = get_cached_image_data(image_path)
        if hash_value is None:
            return "No image", 404
        return hash_value
    except Exception as e:
        print(f"Error getting image hash: {e}")
        return f"Error: {e}", 500


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
    print(f"Image request from device: {device_id}")

    # Get next image (advances rotation)
    image_path = get_next_image_path(device_id)
    if not image_path:
        return "No images available", 404

    try:
        packed_data, image_hash = get_cached_image_data(image_path)
        if packed_data is None:
            return "Failed to process image", 500

        return Response(
            packed_data,
            mimetype='application/octet-stream',
            headers={
                'Content-Length': str(len(packed_data)),
                'Content-Disposition': 'attachment; filename=image.bin',
                'X-Image-Hash': image_hash,
                'X-Image-Name': os.path.basename(image_path),
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

        return jsonify({
            'device_id': device_id,
            'current_image': os.path.basename(current_path) if current_path else None,
            'current_path': current_path,
            'rotation': status,
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
            devices_status[dev_id] = {
                'current_image': os.path.basename(current_path) if current_path else None,
                'current_path': current_path,
                'rotation': status
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

    # Build device status table
    device_rows = ""
    for dev_id in all_devices:
        status = _rotator.get_status(dev_id)
        current_path = get_current_image_path(dev_id)
        current_name = os.path.basename(current_path) if current_path else "None"
        device_rows += f"""
        <tr>
            <td><code>{dev_id}</code></td>
            <td><code>{current_name}</code></td>
            <td>{status['total_images']}</td>
            <td><code>{status['images_dir']}</code></td>
        </tr>"""

    if not device_rows:
        device_rows = "<tr><td colspan='4'>No devices have connected yet</td></tr>"

    return f"""
    <h1>E-Ink Image Server (Multi-Device)</h1>
    <h2>Endpoints</h2>
    <ul>
        <li><a href="/image_packed">/image_packed</a> - Packed binary for ESP32 (960KB, advances rotation)</li>
        <li><a href="/hash">/hash</a> - Image hash for change detection (16 chars)</li>
        <li><a href="/current">/current</a> - Current rotation status (JSON)</li>
        <li><a href="/image">/image</a> - Transformed JPEG preview</li>
        <li><a href="/imagejpg">/imagejpg</a> - Random front page image</li>
    </ul>
    <p><em>Endpoints accept <code>X-Device-MAC</code> header for device identification.</em></p>

    <h2>Device Status</h2>
    <table border="1" cellpadding="8" cellspacing="0">
        <tr>
            <th>Device ID (MAC)</th>
            <th>Current Image</th>
            <th>Total Images</th>
            <th>Images Directory</th>
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
│   └── image2.png
├── d0cf1326f7e8/     # Device-specific (MAC without separators)
│   └── photo.jpg
└── aabbccddeeff/     # Another device
    └── ...
    </pre>
    <p>Create a directory named after the device's MAC address (lowercase, no separators) to serve device-specific images.</p>
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
