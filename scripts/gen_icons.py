"""Generate PWA icons (storm cloud + lightning) with Pillow. Usage: python scripts/gen_icons.py frontend/public/icons"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

BG, CLOUD, BOLT = (11, 16, 32, 255), (148, 163, 184, 255), (250, 204, 21, 255)


def draw(size: int, pad: float = 0.0, bg=BG, mono=False) -> Image.Image:
    img = Image.new("RGBA", (size, size), bg if not mono else (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size * (1 - 2 * pad)
    o = size * pad
    P = lambda x, y: (o + x * s, o + y * s)  # noqa: E731
    cloud = (255, 255, 255, 255) if mono else CLOUD
    for cx, cy, r in [(0.35, 0.42, 0.17), (0.55, 0.35, 0.21), (0.72, 0.47, 0.14)]:
        x, y = P(cx, cy)
        d.ellipse([x - r * s, y - r * s, x + r * s, y + r * s], fill=cloud)
    d.rounded_rectangle([P(0.2, 0.42), P(0.86, 0.6)], radius=int(0.09 * s), fill=cloud)
    bolt = [P(0.55, 0.5), P(0.38, 0.75), P(0.5, 0.75), P(0.42, 0.95), P(0.66, 0.66), P(0.53, 0.66), P(0.62, 0.5)]
    d.polygon(bolt, fill=(255, 255, 255, 255) if mono else BOLT)
    return img


def main(out: str):
    p = Path(out)
    p.mkdir(parents=True, exist_ok=True)
    draw(192).save(p / "icon-192.png")
    draw(512).save(p / "icon-512.png")
    draw(512, pad=0.12).save(p / "icon-maskable-512.png")
    draw(180, pad=0.06).save(p / "apple-touch-icon.png")
    draw(192, bg=(220, 38, 38, 255)).save(p / "storm-alert.png")
    draw(96, mono=True).save(p / "badge.png")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "frontend/public/icons")
