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

from flask import Flask, send_file, Response, jsonify
import requests
import wand.image
from io import BytesIO
import random
import os
import hashlib
import json
import time

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
IMAGES_DIR = os.path.expanduser("~/images")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".eink_rotation_state.json")
SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.heic', '.webp'}

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


class ImageRotator:
    """Manages rotation through images in a directory."""

    def __init__(self, images_dir: str, state_file: str):
        self.images_dir = images_dir
        self.state_file = state_file
        self.last_scan_time = 0.0
        self.image_list = []       # Sorted list of image filenames
        self.current_index = 0
        self.last_returned = None  # Track the last returned image
        self._load_state()

    def _load_state(self):
        """Load rotation state from JSON file."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                self.current_index = state.get('current_index', 0)
                self.last_scan_time = state.get('last_scan_time', 0.0)
                self.image_list = state.get('image_list', [])
                self.last_returned = state.get('last_returned', None)
                print(f"Loaded rotation state: index={self.current_index}, images={len(self.image_list)}")
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error loading state file: {e}")
                self._reset_state()
        else:
            self._reset_state()

    def _save_state(self):
        """Save rotation state to JSON file."""
        state = {
            'current_index': self.current_index,
            'last_scan_time': self.last_scan_time,
            'image_list': self.image_list,
            'last_returned': self.last_returned
        }
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except IOError as e:
            print(f"Error saving state file: {e}")

    def _reset_state(self):
        """Reset to initial state."""
        self.current_index = 0
        self.last_scan_time = 0.0
        self.image_list = []
        self.last_returned = None

    def _scan_directory(self) -> tuple[list[str], list[str]]:
        """
        Scan directory for images.

        Returns:
            tuple: (all_images, new_images) - sorted lists of filenames
        """
        if not os.path.isdir(self.images_dir):
            return [], []

        all_images = []
        new_images = []

        try:
            for entry in os.scandir(self.images_dir):
                # Resolve symlinks
                real_path = os.path.realpath(entry.path)

                if not os.path.isfile(real_path):
                    continue

                # Check extension
                _, ext = os.path.splitext(entry.name.lower())
                if ext not in SUPPORTED_EXTENSIONS:
                    continue

                all_images.append(entry.name)

                # Check if this is a new image (added since last scan)
                try:
                    mtime = os.path.getmtime(real_path)
                    if mtime > self.last_scan_time:
                        new_images.append(entry.name)
                except OSError:
                    continue

        except OSError as e:
            print(f"Error scanning directory {self.images_dir}: {e}")
            return [], []

        # Sort alphabetically for consistent order
        all_images.sort()
        new_images.sort()

        return all_images, new_images

    def get_next_image(self) -> str | None:
        """
        Get path to next image, prioritizing newly added images.

        Returns:
            str | None: Full path to the next image, or None if no images available
        """
        all_images, new_images = self._scan_directory()

        if not all_images:
            return None

        # Update our image list
        self.image_list = all_images

        # If new images detected, return the first new one
        if new_images:
            print(f"New images detected: {new_images}")
            self.last_scan_time = time.time()
            image_name = new_images[0]
            self.last_returned = image_name
            self._save_state()
            return os.path.join(self.images_dir, image_name)

        # Normal rotation - wrap around if needed
        if self.current_index >= len(self.image_list):
            self.current_index = 0

        image_name = self.image_list[self.current_index]
        self.last_returned = image_name
        self.current_index += 1
        self.last_scan_time = time.time()
        self._save_state()

        return os.path.join(self.images_dir, image_name)

    def get_current_image(self) -> str | None:
        """
        Get the image that was last returned (without advancing).

        Returns:
            str | None: Full path to the current image, or None if none returned yet
        """
        if self.last_returned and os.path.exists(os.path.join(self.images_dir, self.last_returned)):
            return os.path.join(self.images_dir, self.last_returned)
        return None

    def get_status(self) -> dict:
        """Get current rotation status."""
        all_images, _ = self._scan_directory()
        return {
            'current_image': self.last_returned,
            'current_index': self.current_index,
            'total_images': len(all_images),
            'images_dir': self.images_dir,
            'image_list': all_images
        }


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
    pixels = list(dithered.getdata())
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


def get_current_image_path() -> str | None:
    """
    Get the path to the current image to display.

    Uses image rotation if ~/images exists with images,
    otherwise falls back to DEFAULT_IMAGE_PATH.

    Returns:
        str | None: Path to the image file, or None if no image available
    """
    # Try rotation first
    current = _rotator.get_current_image()
    if current and os.path.exists(current):
        return current

    # Fall back to default
    if os.path.exists(DEFAULT_IMAGE_PATH):
        return DEFAULT_IMAGE_PATH

    return None


def get_next_image_path() -> str | None:
    """
    Get the path to the next image to display (advances rotation).

    Uses image rotation if ~/images exists with images,
    otherwise falls back to DEFAULT_IMAGE_PATH.

    Returns:
        str | None: Path to the image file, or None if no image available
    """
    # Try rotation first
    next_image = _rotator.get_next_image()
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
    """
    if not PIL_AVAILABLE:
        return "PIL not available", 500

    image_path = get_current_image_path()
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
    """
    if not PIL_AVAILABLE:
        return "PIL not available", 500

    # Get next image (advances rotation)
    image_path = get_next_image_path()
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
                'X-Image-Name': os.path.basename(image_path)
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
    """Return JSON with current rotation status."""
    status = _rotator.get_status()
    current_path = get_current_image_path()

    return jsonify({
        'current_image': os.path.basename(current_path) if current_path else None,
        'current_path': current_path,
        'rotation': status,
        'heic_support': HEIC_SUPPORT,
        'images_dir': IMAGES_DIR,
        'fallback_image': DEFAULT_IMAGE_PATH if os.path.exists(DEFAULT_IMAGE_PATH) else None
    })


@app.route("/")
def index():
    """Show available endpoints."""
    status = _rotator.get_status()
    current_path = get_current_image_path()
    current_name = os.path.basename(current_path) if current_path else "None"

    return f"""
    <h1>E-Ink Image Server</h1>
    <h2>Endpoints</h2>
    <ul>
        <li><a href="/image_packed">/image_packed</a> - Packed binary for ESP32 (960KB, advances rotation)</li>
        <li><a href="/hash">/hash</a> - Image hash for change detection (16 chars)</li>
        <li><a href="/current">/current</a> - Current rotation status (JSON)</li>
        <li><a href="/image">/image</a> - Transformed JPEG preview</li>
        <li><a href="/imagejpg">/imagejpg</a> - Random front page image</li>
    </ul>
    <h2>Status</h2>
    <ul>
        <li>Current image: <code>{current_name}</code></li>
        <li>Images in rotation: {status['total_images']}</li>
        <li>Images directory: <code>{IMAGES_DIR}</code></li>
        <li>HEIC support: {'Yes' if HEIC_SUPPORT else 'No'}</li>
    </ul>
    <p>Place images in <code>~/images/</code> for rotation, or <code>image.jpg</code> as fallback.</p>
    """


if __name__ == "__main__":
    print("Starting E-Ink Image Server...")
    print(f"PIL available: {PIL_AVAILABLE}")
    print(f"HEIC support: {HEIC_SUPPORT}")
    print(f"Default image: {DEFAULT_IMAGE_PATH}")
    print(f"Images directory: {IMAGES_DIR}")
    print(f"Display size: {FRAME_WIDTH}x{FRAME_HEIGHT}")

    # Show rotation status
    status = _rotator.get_status()
    if status['total_images'] > 0:
        print(f"Images in rotation: {status['total_images']}")
    else:
        print(f"No images in {IMAGES_DIR}, will use fallback: {DEFAULT_IMAGE_PATH}")

    app.run(debug=True, host='0.0.0.0', port=5000)
