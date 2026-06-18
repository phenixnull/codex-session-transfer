from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "assets"
STATIC_DIR = ROOT / "static"
SIZE = 1024


def rgba(hex_value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    value = hex_value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha


def gradient(size: int, top: str, bottom: str) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    top_rgba = rgba(top)
    bottom_rgba = rgba(bottom)
    draw = ImageDraw.Draw(image)
    for y in range(size):
        t = y / (size - 1)
        color = tuple(round(top_rgba[i] * (1 - t) + bottom_rgba[i] * t) for i in range(4))
        draw.line([(0, y), (size, y)], fill=color)
    return image


def rounded_mask(box: tuple[int, int, int, int], radius: int) -> Image.Image:
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    return mask


def draw_panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], accent: str) -> None:
    draw.rounded_rectangle(box, radius=58, fill=rgba("#25211e", 235), outline=rgba("#f8f4e8", 42), width=4)
    x0, y0, x1, y1 = box
    draw.rounded_rectangle((x0 + 42, y0 + 50, x0 + 132, y0 + 92), radius=20, fill=rgba(accent, 210))
    for index, width in enumerate((170, 218, 138)):
        y = y0 + 142 + index * 72
        draw.rounded_rectangle((x0 + 46, y, x0 + 46 + width, y + 22), radius=11, fill=rgba("#efe9dc", 108))
    draw.rounded_rectangle((x0 + 46, y1 - 92, x1 - 46, y1 - 54), radius=19, fill=rgba("#ffffff", 28))


def build_icon() -> Image.Image:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    card_box = (72, 72, SIZE - 72, SIZE - 72)
    card_mask = rounded_mask(card_box, 194)

    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    shadow_alpha = card_mask.filter(ImageFilter.GaussianBlur(34))
    shadow.paste(rgba("#000000", 126), (0, 28), shadow_alpha)
    canvas.alpha_composite(shadow)

    card = gradient(SIZE, "#302b25", "#11100f")
    canvas.paste(card, (0, 0), card_mask)

    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rounded_rectangle(card_box, radius=194, outline=rgba("#f7f1df", 44), width=6)
    draw.rounded_rectangle((128, 128, 896, 896), radius=144, outline=rgba("#5ee3a1", 32), width=3)

    draw_panel(draw, (218, 302, 494, 698), "#5ee3a1")
    draw_panel(draw, (530, 302, 806, 698), "#f0b45a")

    draw.line((476, 512, 554, 512), fill=rgba("#5ee3a1", 245), width=26)
    draw.polygon([(554, 512), (512, 468), (512, 556)], fill=rgba("#5ee3a1", 245))
    draw.arc((382, 202, 642, 462), 210, 330, fill=rgba("#efe9dc", 150), width=18)
    draw.arc((382, 562, 642, 822), 30, 150, fill=rgba("#efe9dc", 120), width=18)

    draw.rounded_rectangle((332, 754, 692, 808), radius=27, fill=rgba("#ffffff", 30), outline=rgba("#ffffff", 48), width=3)
    draw.rounded_rectangle((382, 774, 642, 790), radius=8, fill=rgba("#efe9dc", 120))
    return canvas


def save_resized(icon: Image.Image, size: int) -> None:
    icon.resize((size, size), Image.Resampling.LANCZOS).save(ASSET_DIR / f"icon-{size}.png")


def main() -> int:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    icon = build_icon()
    icon.save(ASSET_DIR / "icon.png")
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        save_resized(icon, size)
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon.save(ASSET_DIR / "icon.ico", sizes=ico_sizes)
    icns_sizes = [(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)]
    icon.save(ASSET_DIR / "icon.icns", format="ICNS", sizes=icns_sizes)
    icon.resize((64, 64), Image.Resampling.LANCZOS).save(STATIC_DIR / "favicon.png")
    print(f"Generated icons in {ASSET_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
