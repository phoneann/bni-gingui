"""把形象照原圖自動裁成臉部置中的正方形頭像。
用法：python3 tools/headshot.py <原圖> <輸出.jpg> [邊長px]
規則：OpenCV 抓最大人臉，裁切邊長＝臉高×2.6，臉水平置中、臉中心落在框高 42% 處。
"""
import sys, cv2, numpy as np
from PIL import Image, ImageOps
FACE_RATIO, FACE_CY = 2.6, 0.42
src, dst = sys.argv[1], sys.argv[2]
size = int(sys.argv[3]) if len(sys.argv) > 3 else 400
im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
W, H = im.size
sc = min(1.0, 1200 / max(W, H))
g = cv2.cvtColor(np.array(im.resize((int(W*sc), int(H*sc)))), cv2.COLOR_RGB2GRAY)
casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
faces = casc.detectMultiScale(g, 1.1, 4, minSize=(20, 20))
if len(faces) == 0:
    sys.exit(f"找不到人臉：{src}")
x, y, w, h = [v / sc for v in max(faces, key=lambda f: f[2]*f[3])]
cx, cy = x + w/2, y + h/2
side = min(h * FACE_RATIO, W, H)
left = max(0, min(cx - side/2, W - side))
top = max(0, min(cy - side*FACE_CY, H - side))
im.crop((int(left), int(top), int(left+side), int(top+side))).resize((size, size), Image.LANCZOS).save(dst, quality=92)
print("ok", dst, "face", (int(x), int(y), int(w), int(h)))
