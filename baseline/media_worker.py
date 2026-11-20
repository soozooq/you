"""Image hash entry point for a deployment-isolated media worker."""
import sys


def image_hash(path):
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 16_000_000
    with Image.open(path, formats=("PNG", "JPEG", "GIF", "WEBP")) as image:
        if image.width * image.height > 16_000_000 or image.n_frames != 1:
            raise ValueError("Image exceeds worker limits")
        pixels = list(image.convert("L").resize((8, 8)).getdata())
    average = sum(pixels) / len(pixels)
    return sum((value > average) << index for index, value in enumerate(pixels))


if __name__ == "__main__":
    try:
        print(f"{image_hash(sys.argv[1]):016x}")
    except Exception:
        sys.exit(1)
