import sys
import os
import cv2
import numpy as np
from PIL import Image, ImageOps

# ======================================================
# FACE CROPPING  (EXIF-aware, zone-filtered)
# ======================================================

def _find_cascade_path():
    """
    Finds haarcascade_frontalface_default.xml from multiple locations:
    1. Bundled next to exe via PyInstaller (--add-data "haarcascade...;.")
    2. OpenCV built-in data folder (dev/script mode fallback)
    """
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        p = os.path.join(meipass, "haarcascade_frontalface_default.xml")
        if os.path.exists(p):
            return p
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "haarcascade_frontalface_default.xml")
    if os.path.exists(p):
        return p
    try:
        p = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        if os.path.exists(p):
            return p
    except Exception:
        pass
    return ""


def crop_passport_face(input_path, output_path, target_size=(400, 400)):
    """
    Crops the face photo from a passport image.

    1. PIL ImageOps.exif_transpose() applied BEFORE any OpenCV work so that
       phones which store images rotated (EXIF tag) are handled correctly.
    2. The EXIF-corrected PIL image is converted to a numpy array for OpenCV —
       no second raw cv2.imread() that would ignore EXIF again.
    3. Detected faces are filtered to the expected passport-photo zone
       (left 50%, top 85% of the page) to reject false positives from MRZ
       text, stamps, or watermarks.
    4. The crop is taken from the already-corrected PIL image so the saved
       thumbnail is always the right way up.
    5. Cascade XML found via _find_cascade_path() — works both inside
       PyInstaller exe and in normal script mode.
    """
    try:
        img_pil = Image.open(input_path)
        img_pil = ImageOps.exif_transpose(img_pil)
        img_pil = img_pil.convert("RGB")
    except Exception as e:
        raise ValueError(f"Cannot open image: {e}")

    img_w, img_h = img_pil.size

    img_cv = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
    gray   = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)

    cascade_path = _find_cascade_path()
    cascade = cv2.CascadeClassifier(cascade_path)
    faces = cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)
    )

    if len(faces) == 0:
        return False

    valid_faces = [
        (x, y, w, h) for (x, y, w, h) in faces
        if x < img_w * 0.50 and y < img_h * 0.85
    ]
    if not valid_faces:
        valid_faces = list(faces)

    x, y, w, h = max(valid_faces, key=lambda r: r[2] * r[3])
    pad = int(0.2 * w)
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(img_w, x + w + pad)
    y2 = min(img_h, y + h + pad)

    face_crop    = img_pil.crop((x1, y1, x2, y2))
    face_resized = face_crop.resize(target_size, Image.LANCZOS)
    face_resized.save(output_path, "JPEG", quality=95)
    return True