"""
Generate PodTidy application icon.
Creates a multi-resolution .ico file with a podcast/audio theme.
"""
from PIL import Image, ImageDraw

ACCENT = (107, 105, 214)  # #6B69D6 — app accent purple
WHITE = (255, 255, 255)
SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw_icon(size: int) -> Image.Image:
    """Draw a podcast-themed icon at the given size (square)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = max(1, size * 0.08)  # proportional margin
    r = max(2, size * 0.20)        # corner radius

    # --- Rounded-square background ---
    # Use a mask for clean anti-aliased rounding
    mask = Image.new("L", (size * 4, size * 4), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        [0, 0, size * 4, size * 4],
        radius=r * 4,
        fill=255,
    )
    mask = mask.resize((size, size), Image.LANCZOS)

    bg = Image.new("RGBA", (size, size), ACCENT + (255,))
    img.paste(bg, (0, 0), mask)

    # --- Audio waveform bars ---
    # Draw 4 vertical bars centered, varying heights
    bar_area_left = size * 0.22
    bar_area_right = size * 0.78
    bar_area_width = bar_area_right - bar_area_left
    bar_count = 4
    bar_gap_ratio = 0.28  # gap as fraction of total bar width
    total_bar_w = bar_area_width / (bar_count + (bar_count - 1) * bar_gap_ratio)
    gap_w = total_bar_w * bar_gap_ratio
    bar_w = total_bar_w

    # Bar heights: low, high, highest, medium (classic waveform)
    heights = [0.38, 0.70, 1.0, 0.55]
    center_y = size / 2
    max_bar_half = (size - 2 * margin) / 2 * 0.85

    for i, h_ratio in enumerate(heights):
        x0 = bar_area_left + i * (bar_w + gap_w)
        x1 = x0 + bar_w
        half_h = max_bar_half * h_ratio
        y0 = center_y - half_h
        y1 = center_y + half_h

        # Rounded tops
        bar_r = max(1, bar_w * 0.42)
        draw.rounded_rectangle(
            [x0, y0, x1, y1],
            radius=bar_r,
            fill=WHITE,
        )

    return img


def main():
    ico_path = "app_icon.ico"
    ico_sizes = [(s, s) for s in SIZES]

    # Generate the largest icon (256px) and let Pillow downscale
    # for clean anti-aliased results at all resolutions
    master = draw_icon(256)
    master.save(
        ico_path,
        format="ICO",
        sizes=ico_sizes,
    )
    print(f"  [OK] {ico_path} ({', '.join(str(s) for s in SIZES)} px)")

    # Also save a 256px PNG for reference / other uses
    png_path = "app_icon.png"
    master.save(png_path, format="PNG")
    print(f"  [OK] {png_path} (256px PNG)")


if __name__ == "__main__":
    main()
