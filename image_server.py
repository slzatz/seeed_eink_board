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

# Try to import PIL for image processing
try:
    from PIL import Image, ImageOps, ImageEnhance
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("Warning: PIL not available. /image_packed endpoint will not work.")

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

# Image enhancement settings
DEFAULT_CONTRAST = 1.2
DEFAULT_BRIGHTNESS = 1.0
DEFAULT_SATURATION = 1.2

# Cache for processed image data
_image_cache = {
    'data': None,
    'hash': None,
    'source_mtime': None,  # Modification time of source image
}


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


def get_cached_image_data():
    """
    Get processed image data, using cache if source hasn't changed.

    Returns:
        tuple: (packed_data, hash) or (None, None) if error
    """
    global _image_cache

    if not os.path.exists(DEFAULT_IMAGE_PATH):
        return None, None

    # Check if source image has changed
    current_mtime = os.path.getmtime(DEFAULT_IMAGE_PATH)

    if (_image_cache['data'] is not None and
        _image_cache['source_mtime'] == current_mtime):
        # Cache is valid
        return _image_cache['data'], _image_cache['hash']

    # Process the image
    print(f"Processing image: {DEFAULT_IMAGE_PATH}")
    packed_data = process_image_to_packed(DEFAULT_IMAGE_PATH)

    # Compute hash (using first 8 chars of MD5 for simplicity)
    image_hash = hashlib.md5(packed_data).hexdigest()[:16]

    # Update cache
    _image_cache['data'] = packed_data
    _image_cache['hash'] = image_hash
    _image_cache['source_mtime'] = current_mtime

    print(f"Image processed, hash: {image_hash}")
    return packed_data, image_hash


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

    if not os.path.exists(DEFAULT_IMAGE_PATH):
        return "No image", 404

    try:
        _, hash_value = get_cached_image_data()
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
    """
    if not PIL_AVAILABLE:
        return "PIL not available", 500

    if not os.path.exists(DEFAULT_IMAGE_PATH):
        return f"Image not found: {DEFAULT_IMAGE_PATH}", 404

    try:
        packed_data, image_hash = get_cached_image_data()
        if packed_data is None:
            return "Failed to process image", 500

        return Response(
            packed_data,
            mimetype='application/octet-stream',
            headers={
                'Content-Length': str(len(packed_data)),
                'Content-Disposition': 'attachment; filename=image.bin',
                'X-Image-Hash': image_hash
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


@app.route("/")
def index():
    """Show available endpoints."""
    return """
    <h1>E-Ink Image Server</h1>
    <ul>
        <li><a href="/image_packed">/image_packed</a> - Packed binary for ESP32 (960KB)</li>
        <li><a href="/hash">/hash</a> - Image hash for change detection (16 chars)</li>
        <li><a href="/image">/image</a> - Transformed JPEG preview</li>
        <li><a href="/imagejpg">/imagejpg</a> - Random front page image</li>
    </ul>
    <p>Place your image as <code>image.jpg</code> in the server directory.</p>
    """


if __name__ == "__main__":
    print("Starting E-Ink Image Server...")
    print(f"PIL available: {PIL_AVAILABLE}")
    print(f"Default image: {DEFAULT_IMAGE_PATH}")
    print(f"Display size: {FRAME_WIDTH}x{FRAME_HEIGHT}")

    app.run(debug=True, host='0.0.0.0', port=5000)
