"""SNS で共有されたときの画像 public/og.png (1200x630) を作る。アイコン (icon-master.png) と英語のタイトル。"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
W, H = 1200, 630
img = Image.new("RGBA", (W, H), (33, 37, 41, 255))
icon = Image.open(os.path.join(HERE, "icon-master.png")).convert("RGBA").resize((340, 340), Image.LANCZOS)
img.alpha_composite(icon, (70, (H - 340) // 2))
d = ImageDraw.Draw(img)
x = 470
d.text((x, 175), "Bitcoin's Proof of Work,", font=ImageFont.truetype(BOLD, 52), fill=(248, 249, 250))
d.text((x, 245), "written in CSS", font=ImageFont.truetype(BOLD, 78), fill=(247, 147, 26))
d.text((x, 360), "No JavaScript. Double SHA-256 in pure CSS.", font=ImageFont.truetype(BOLD, 28), fill=(222, 226, 230))
d.text((x, 410), "bitcoin-pow-in-css.activetk.jp", font=ImageFont.truetype(BOLD, 28), fill=(173, 181, 189))
img.convert("RGB").save(os.path.join(HERE, "..", "public", "og.png"), optimize=True)
print("ok")
