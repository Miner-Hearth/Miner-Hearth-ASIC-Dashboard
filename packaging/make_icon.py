"""Build-time helper: renders packaging/miner_dashboard.ico (needs Pillow; not a runtime dependency)."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((16, 16, S - 16, S - 16), radius=110, fill=(16, 19, 23, 255), outline=(62, 224, 180, 255), width=18)
# ASIC chip
d.rounded_rectangle((136, 136, 376, 376), radius=28, fill=(35, 42, 53, 255), outline=(62, 224, 180, 255), width=12)
for i in range(5):
    o = 160 + i * 48
    for (x0, y0, x1, y1) in ((o - 8, 92, o + 8, 136), (o - 8, 376, o + 8, 420), (92, o - 8, 136, o + 8), (376, o - 8, 420, o + 8)):
        d.rectangle((x0, y0, x1, y1), fill=(62, 224, 180, 255))
# lightning bolt
d.polygon([(278, 160), (196, 272), (250, 272), (226, 352), (316, 232), (262, 232)], fill=(240, 193, 74, 255))
out = Path(__file__).resolve().parent / "miner_dashboard.ico"
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save(out.with_suffix(".png"))
img.save(out.with_name("miner_hearth_512.png"))  # v1.17.0: Umbrel / 5tratumOS app icon (512x512)
print("wrote", out)
