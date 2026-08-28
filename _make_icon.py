# -*- coding: utf-8 -*-
"""生成 P2PChat 应用图标：蓝紫渐变圆角方形 + 白色聊天气泡 + 笑脸。"""
from PIL import Image, ImageDraw
import os

size = 256

# 1) 对角渐变背景（Discord blurple -> 亮紫）
grad = Image.new("RGBA", (size, size))
px = grad.load()
c1 = (88, 101, 242)   # #5865F2
c2 = (146, 95, 255)   # 亮紫
for y in range(size):
    for x in range(size):
        t = (x + y) / (2 * (size - 1))
        px[x, y] = (int(c1[0] * (1 - t) + c2[0] * t),
                    int(c1[1] * (1 - t) + c2[1] * t),
                    int(c1[2] * (1 - t) + c2[2] * t), 255)

# 圆角方形遮罩（四角透明，现代 App 图标风格）
mask = Image.new("L", (size, size), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, size, size], radius=size // 5, fill=255)
icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
icon.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(icon)

# 2) 白色聊天气泡 + 小尾巴
d.rounded_rectangle([44, 56, size - 44, size - 86], radius=30, fill=(255, 255, 255, 255))
d.polygon([(80, size - 88), (62, size - 54), (116, size - 88)], fill=(255, 255, 255, 255))

# 3) 笑脸（眼睛 + 微笑弧）
eye = (88, 101, 242, 255)
d.ellipse([96, 108, 124, 136], fill=eye)
d.ellipse([136, 108, 164, 136], fill=eye)
d.arc([106, 120, 154, 166], start=15, end=165, fill=eye, width=10)

base = "C:/Users/36087/AppData/Roaming/@opensquilla/desktop-electron/opensquilla/workspace/p2p-chat"
sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
icon.save(os.path.join(base, "P2PChat.ico"), format="ICO", sizes=sizes)
icon.resize((512, 512), Image.LANCZOS).save(os.path.join(base, "_icon_preview.png"), "PNG")
print("ico saved:", os.path.getsize(os.path.join(base, "P2PChat.ico")), "bytes")
