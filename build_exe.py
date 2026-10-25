"""Build script: converts icon to .ico and runs PyInstaller."""
import subprocess
import sys
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))

# We'll create a simple .ico from a BMP approach, or just build without icon
# if Pillow is not available
ICO_PATH = os.path.join(HERE, "mad60_icon.ico")
SCRIPT = os.path.join(HERE, "mad60_autoswitch_gui.py")

def create_simple_ico():
    """Create a minimal 32x32 .ico file with a red keyboard key on dark bg."""
    # 32x32 RGBA icon
    size = 32
    pixels = []
    for y in range(size):
        for x in range(size):
            # Dark background with red/coral center key shape
            cx, cy = size // 2, size // 2
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if 4 <= x <= 27 and 4 <= y <= 27:
                # Red key area
                if 6 <= x <= 25 and 6 <= y <= 25:
                    # Inner key with arrows hint
                    r, g, b, a = 233, 69, 96, 255
                else:
                    # Key border
                    r, g, b, a = 200, 50, 80, 255
            elif dist <= 15:
                r, g, b, a = 26, 26, 46, 255
            else:
                r, g, b, a = 26, 26, 46, 255

            pixels.append((b, g, r, a))  # BGRA for BMP

    # Build ICO file
    width = size
    height = size

    # BMP info header (BITMAPINFOHEADER) - height is 2x for AND mask
    bmp_header = struct.pack('<IiiHHIIiiII',
        40,          # biSize
        width,       # biWidth
        height * 2,  # biHeight (2x for XOR + AND)
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression
        0,           # biSizeImage
        0, 0,        # biXPelsPerMeter, biYPelsPerMeter
        0,           # biClrUsed
        0,           # biClrImportant
    )

    # Pixel data (bottom-up)
    pixel_data = b""
    for y in range(height - 1, -1, -1):
        for x in range(width):
            b, g, r, a = pixels[y * width + x]
            pixel_data += struct.pack('BBBB', b, g, r, a)

    # AND mask (all transparent = all zeros for 32-bit BGRA)
    and_mask = b"\x00" * (((width + 31) // 32) * 4 * height)

    image_data = bmp_header + pixel_data + and_mask
    image_size = len(image_data)

    # ICO header
    ico_header = struct.pack('<HHH', 0, 1, 1)  # reserved, type=icon, count=1

    # ICO directory entry
    ico_entry = struct.pack('<BBBBHHII',
        width if width < 256 else 0,
        height if height < 256 else 0,
        0,          # color palette
        0,          # reserved
        1,          # color planes
        32,         # bits per pixel
        image_size, # size of image data
        22,         # offset (6 header + 16 entry = 22)
    )

    with open(ICO_PATH, 'wb') as f:
        f.write(ico_header + ico_entry + image_data)

    print(f"Created {ICO_PATH}")


def build():
    # Create icon
    try:
        # Try using Pillow if available
        from PIL import Image
        img_path = None
        # Find the generated image
        brain_dir = os.path.join(os.environ.get("USERPROFILE", ""), ".gemini", "antigravity-ide", "brain")
        for root_dir, dirs, files in os.walk(brain_dir):
            for f in files:
                if "mad60_icon" in f and f.endswith((".jpg", ".png")):
                    img_path = os.path.join(root_dir, f)
                    break
            if img_path:
                break

        if img_path:
            img = Image.open(img_path)
            img = img.resize((256, 256), Image.LANCZOS)
            img.save(ICO_PATH, format="ICO", sizes=[(256, 256), (48, 48), (32, 32), (16, 16)])
            print(f"Created {ICO_PATH} from {img_path}")
        else:
            create_simple_ico()
    except ImportError:
        print("Pillow not available, creating simple icon...")
        create_simple_ico()

    # Build with PyInstaller
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconsole",
        "--name", "MAD60AutoSwitch",
        "--icon", ICO_PATH,
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", HERE,
        SCRIPT,
    ]
    print(f"\nRunning: {' '.join(cmd)}\n")
    subprocess.run(cmd, cwd=HERE)


if __name__ == "__main__":
    build()
