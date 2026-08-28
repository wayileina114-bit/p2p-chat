# -*- coding: utf-8 -*-
"""生成 P2PChat 应用图标：二次元 Q 版少女（粉渐变 + 大眼睛 + 腮红 + 水手服领 + 蝴蝶结）。"""
from PIL import Image, ImageDraw
import os

S = 256

# 1) 粉色对角渐变背景
grad = Image.new("RGBA", (S, S))
px = grad.load()
c1 = (255, 158, 199)  # #ff9ec7
c2 = (255, 217, 232)  # #ffd9e8
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * (S - 1))
        px[x, y] = (int(c1[0] * (1 - t) + c2[0] * t),
                    int(c1[1] * (1 - t) + c2[1] * t),
                    int(c1[2] * (1 - t) + c2[2] * t), 255)

mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S, S], radius=S // 5, fill=255)
icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
icon.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(icon)

SKIN = (255, 228, 214, 255)
HAIR = (90, 52, 48, 255)      # 深棕发
HAIR2 = (130, 78, 68, 255)    # 发梢亮色
EYE = (255, 111, 165, 255)    # 粉瞳
WHITE = (255, 255, 255, 255)
NAVY = (70, 86, 150, 255)     # 水手服深蓝

# 2) 头发（大圆 + 两侧发帘，Q 版大头）
d.ellipse([44, 40, S - 44, S - 40], fill=HAIR)
d.ellipse([52, 48, S - 52, S - 48], fill=HAIR)

# 3) 脸（小圆，下部）
d.ellipse([70, 96, S - 70, S - 18], fill=SKIN)

# 4) 刘海（两撮）
d.pieslice([44, 40, S - 44, S - 40], 180, 300, fill=HAIR)
d.pieslice([44, 40, S - 44, S - 40], 240, 360, fill=HAIR)

# 5) 大眼睛（Q 版：大瞳孔 + 高光）
for cx in (92, S - 92):
    d.ellipse([cx - 34, 120, cx + 34, 178], fill=WHITE)
    d.ellipse([cx - 24, 132, cx + 24, 170], fill=EYE)
    d.ellipse([cx - 20, 136, cx - 4, 152], fill=WHITE)   # 高光
    d.ellipse([cx + 8, 148, cx + 20, 160], fill=WHITE)   # 小高光
    d.arc([cx - 34, 116, cx + 34, 148], start=200, end=340, fill=(40, 30, 35, 255), width=6)  # 上睫毛

# 6) 腮红
d.ellipse([58, 156, 92, 178], fill=(255, 150, 170, 140))
d.ellipse([S - 92, 156, S - 58, 178], fill=(255, 150, 170, 140))

# 7) 小嘴
d.arc([110, 152, S - 110, 178], start=20, end=160, fill=(200, 90, 90, 255), width=6)

# 8) 头顶蝴蝶结（红）
bow = [(150, 30), (128, 8), (122, 34), (112, 8), (150, 46), (188, 8), (182, 34), (176, 8)]
d.polygon(bow, fill=(245, 80, 110, 255))
d.ellipse([144, 32, 158, 46], fill=(255, 140, 160, 255))

# 9) 水手服领（白色三角领 + 深蓝边）
d.polygon([(96, S - 26), (S // 2, 210), (S - 96, S - 26)], fill=WHITE)
d.polygon([(96, S - 26), (S // 2, 210), (130, S - 26)], fill=NAVY)
d.polygon([(S - 96, S - 26), (S // 2, 210), (S - 130, S - 26)], fill=NAVY)

base = "C:/Users/36087/AppData/Roaming/@opensquilla/desktop-electron/opensquilla/workspace/p2p-chat"
sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
icon.save(os.path.join(base, "P2PChat.ico"), format="ICO", sizes=sizes)
icon.resize((512, 512), Image.LANCZOS).save(os.path.join(base, "_icon_preview.png"), "PNG")
print("ico saved:", os.path.getsize(os.path.join(base, "P2PChat.ico")), "bytes")
