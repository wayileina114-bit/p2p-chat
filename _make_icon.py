from PIL import Image, ImageDraw
import os

size = 256
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

r = 40
d.rounded_rectangle([8, 8, size-8, size-8], radius=r, fill=(37, 99, 235, 255))
d.polygon([(60, size-60), (40, size-20), (100, size-70)], fill=(37, 99, 235, 255))

cy = size//2
for cx in [size//2 - 60, size//2, size//2 + 60]:
    d.ellipse([cx-18, cy-18, cx+18, cy+18], fill="white")

sizes = [(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)]
out = "C:/Users/36087/AppData/Roaming/@opensquilla/desktop-electron/opensquilla/workspace/p2p-chat/P2PChat.ico"
img.save(out, format="ICO", sizes=sizes)
print("ico saved:", out, os.path.getsize(out), "bytes")