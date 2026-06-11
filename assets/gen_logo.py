"""Generate CC logo as PNG for GitHub README and social preview."""
from PIL import Image, ImageDraw, ImageFont
import math, os

W, H = 800, 300
img = Image.new("RGBA", (W, H), (13, 17, 23, 255))
draw = ImageDraw.Draw(img)
accent = (57, 255, 20, 255)
accent_mid = (57, 255, 20, 120)
accent_glow = (57, 255, 20, 60)

cx, cy = W // 2, 120
r = 70
offset = 55
pw = 6


def draw_c_arc(d, ccx, ccy, radius, stroke_w, color, flip=False):
    bbox = [ccx - radius, ccy - radius, ccx + radius, ccy + radius]
    if flip:
        d.arc(bbox, start=180, end=60, fill=color, width=stroke_w)
    else:
        d.arc(bbox, start=60, end=300, fill=color, width=stroke_w)


def draw_nodes(d, ccx, ccy, radius, color, flip=False):
    ends = [300, 180] if flip else [60, 300]
    for a in ends:
        rad = math.radians(a)
        x = ccx + radius * math.cos(rad)
        y = ccy - radius * math.sin(rad)
        nr = 6
        d.ellipse([x - nr, y - nr, x + nr, y + nr], fill=color)


# Glow layer
draw_c_arc(draw, cx - offset, cy, r + 4, pw + 6, accent_glow)
draw_c_arc(draw, cx + offset, cy, r + 4, pw + 6, accent_glow, flip=True)

# Mid-opacity layer
draw_c_arc(draw, cx - offset, cy, r + 2, pw + 3, accent_mid)
draw_c_arc(draw, cx + offset, cy, r + 2, pw + 3, accent_mid, flip=True)

# Main arcs
draw_c_arc(draw, cx - offset, cy, r, pw, accent)
draw_c_arc(draw, cx + offset, cy, r, pw, accent, flip=True)

# Nodes
draw_nodes(draw, cx - offset, cy, r, accent)
draw_nodes(draw, cx + offset, cy, r, accent, flip=True)

# Text
try:
    font = ImageFont.truetype("consola.ttf", 32)
except Exception:
    font = ImageFont.load_default()
text = "CYBER CONTROLLER"
bbox = draw.textbbox((0, 0), text, font=font)
tw = bbox[2] - bbox[0]
draw.text(((W - tw) // 2, 220), text, fill=accent, font=font)

out = os.path.join(os.path.dirname(__file__), "cc-logo.png")
img.save(out)
print(f"Saved {out}")
