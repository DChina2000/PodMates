"""生成程序图标（托盘、桌面、手机主屏幕）。"""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets"
S = 1024


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / S
        gd.line([(0, y), (S, y)], fill=(int(20 + 30 * t), int(150 - 60 * t), int(220 - 40 * t), 255))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=230, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    # 对话气泡
    d.rounded_rectangle([200, 250, 824, 690], radius=170, fill=(255, 255, 255, 255))
    d.polygon([(330, 650), (290, 830), (480, 680)], fill=(255, 255, 255, 255))
    for i, x in enumerate((370, 512, 654)):
        d.ellipse([x - 52, 418, x + 52, 522], fill=(30, 120, 200, 255) if i != 1 else (255, 150, 90, 255))
    return img


if __name__ == "__main__":
    img = draw()
    img.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.resize((512, 512), Image.LANCZOS).save(OUT / "icon-512.png")
    img.resize((192, 192), Image.LANCZOS).save(OUT / "icon-192.png")
    img.resize((180, 180), Image.LANCZOS).save(OUT / "apple-touch-icon.png")
    print("ok")
