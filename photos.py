"""Template-measured photo frames with independent image crop controls."""
from functools import lru_cache
from io import BytesIO
import json
from pathlib import Path
import fitz
import numpy as np
from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parent
GEOMETRY = json.loads((ROOT / "assets/photo-geometry.json").read_text())
GEOMETRY["oval"] = {"rect": [130.0, 565.0, 467.0, 789.0]}
SLOTS = ("left", "center", "right", "small", "oval")

def cover(image, width, height, zoom=1.0, pan_x=0.0, pan_y=0.0):
    aspect = width / height
    crop_width = min(image.width, image.height * aspect) / zoom
    crop_height = crop_width / aspect
    x = (image.width - crop_width) * (pan_x + 1) / 2
    y = (image.height - crop_height) * (pan_y + 1) / 2
    return image.crop((x, y, x+crop_width, y+crop_height)).resize((width, height), Image.Resampling.LANCZOS)

def perspective_coefficients(destination, source):
    matrix, vector = [], []
    for (x, y), (u, v) in zip(destination, source):
        matrix.extend([[x, y, 1, 0, 0, 0, -u*x, -u*y], [0, 0, 0, x, y, 1, -v*x, -v*y]])
        vector.extend([u, v])
    return np.linalg.solve(np.asarray(matrix, float), np.asarray(vector, float)).tolist()

@lru_cache(maxsize=24)
def prepared_photo(key: str, data: bytes, zoom=1.0, pan_x=0.0, pan_y=0.0) -> bytes:
    if len(data) > 25 * 1024 * 1024:
        raise ValueError("Each photo must be smaller than 25 MB.")
    with Image.open(BytesIO(data)) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
    frame = GEOMETRY[key]
    x0, y0, x1, y1 = frame["rect"]
    if key == "oval":
        size = (round((x1-x0)*3), round((y1-y0)*3))
        mask = Image.new("L", size)
        ImageDraw.Draw(mask).ellipse((0, 0, size[0]-1, size[1]-1), fill=255)
    else:
        mask = Image.open(ROOT / f"assets/photo-{key}-mask.png").convert("L")
        size = mask.size
    if key in {"left", "center", "right"}:
        points = frame["corners"]
        source_width = round(np.linalg.norm(np.array(points[1])-np.array(points[0])) * 3)
        source_height = round(np.linalg.norm(np.array(points[3])-np.array(points[0])) * 3)
        source_image = cover(image, source_width, source_height,zoom=zoom,pan_x=pan_x,pan_y=pan_y)
        destination = [((x-x0)*3,(y-y0)*3) for x,y in points]
        coeffs = perspective_coefficients(destination,[(0,0),(1,0),(1,1),(0,1)])
        a,b,c,d,e,f,g,h = coeffs
        # Cover every pixel of the actual mask, including its curved edges.
        # Normalize the projected source extent instead of guessing overscan.
        yy,xx = np.where(np.asarray(mask)>0)
        denominator=g*xx+h*yy+1
        u=(a*xx+b*yy+c)/denominator
        v=(d*xx+e*yy+f)/denominator
        u0,u1=float(u.min())-.015,float(u.max())+.015
        v0,v1=float(v.min())-.015,float(v.max())+.015
        sx,sy=(source_width-1)/(u1-u0),(source_height-1)/(v1-v0)
        coeffs=[(a-u0*g)*sx,(b-u0*h)*sx,(c-u0)*sx,
                (d-v0*g)*sy,(e-v0*h)*sy,(f-v0)*sy,g,h]
        result = source_image.transform(size, Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BICUBIC)
    else:
        result = cover(image, *size, zoom=zoom, pan_x=pan_x, pan_y=pan_y)
    result = result.convert("RGBA")
    result.putalpha(mask)
    output = BytesIO()
    result.save(output, format="PNG")
    return output.getvalue()

def draw_photos(page: fitz.Page, uploads: dict[str, bytes] | None, front: bool, small_crop=None, photo_crops=None):
    uploads = uploads or {}
    crop = small_crop or {"zoom": 1.0, "pan_x": 0.0, "pan_y": 0.0}
    for key in SLOTS[:4] if front else ("oval",):
        if not uploads.get(key):
            continue
        kwargs = (photo_crops or {}).get(key,crop if key == "small" else {})
        page.insert_image(fitz.Rect(GEOMETRY[key]["rect"]), stream=prepared_photo(key, uploads[key], **kwargs))
