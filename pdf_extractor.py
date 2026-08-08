# pdf_extractor.py
import os
import io
import sys
import cv2
import numpy as np
import zipfile
from pypdf import PdfReader
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename
import json

# NOTE: UPLOAD_FOLDER used to be a bare relative path ('uploads'), created
# eagerly at import time with os.makedirs(). That resolves against the
# process's current working directory, which for a frozen macOS .app
# launched via `open`/Finder/LaunchServices is often "/" or some other
# read-only location -- NOT the folder next to the executable. That caused:
#   OSError: [Errno 30] Read-only file system: 'uploads'
# config.py already computes the correct, writable path
# (os.path.join(BASE_DIR, 'uploads'), anchored to the real executable
# location via sys.executable when frozen -- see config.py). Reuse that
# single source of truth instead of maintaining a second, wrong one here.
from config import UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── Cascade loader — mirrors app.py's _find_cascade() pattern ────────────────
def _resource_path(rel):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

def _find_cascade():
    # 1. Bundled next to exe / script
    p1 = _resource_path('haarcascade_frontalface_default.xml')
    if os.path.exists(p1):
        return p1
    # 2. OpenCV's own built-in data folder (fallback)
    try:
        p2 = os.path.join(os.path.dirname(cv2.__file__), 'data',
                          'haarcascade_frontalface_default.xml')
        if os.path.exists(p2):
            return p2
    except Exception:
        pass
    return p1  # CascadeClassifier handles missing gracefully

_CASCADE_PATH = _find_cascade()
_face_cascade = cv2.CascadeClassifier(_CASCADE_PATH)   # loaded once at import
# ─────────────────────────────────────────────────────────────────────────────

def parse_page_range(range_str, total_pages):
    """Parse '1-3,5' into sorted list of 0-based page indices"""
    pages = set()
    parts = [p.strip() for p in range_str.split(',') if p.strip()]
    for part in parts:
        if '-' in part:
            start, end = map(int, part.split('-'))
        else:
            start = end = int(part)
        start -= 1
        end -= 1
        if start < 0 or end >= total_pages:
            raise ValueError(f"Page number out of range. PDF has {total_pages} pages.")
        for i in range(start, end + 1):
            pages.add(i)
    return sorted(pages)


def crop_by_face(image_path):
    """
    Crop the passport data page from a passport photo using face-height proportions.
    Highly robust against mobile phone photos, bright lighting, and fingers.
    """
    # ── Load via PIL — handles EXIF rotation, matches app.py ─────────────────
    try:
        pil_img = Image.open(image_path)
        pil_img = ImageOps.exif_transpose(pil_img).convert('RGB')
    except Exception:
        return None

    img  = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h_img, w_img = img.shape[:2]

    # ── Face detection — identical params to app.py ───────────────────────────
    faces = _face_cascade.detectMultiScale(
        gray,
        scaleFactor=1.05,
        minNeighbors=3,
        minSize=(40, 40),
    )

    if len(faces) == 0:
        return None

    # Largest face = passport photo
    fx, fy, fw, fh = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)[0]

    col_l = w_img // 4
    col_r = 3 * w_img // 4

    # ── Detect image type ─────────────────────────────────────────────────────
    is_precropped = (fy / h_img) < 0.4

    # ── TOP ───────────────────────────────────────────────────────────────────
    if is_precropped:
        top = 0
    else:
        # TIGHTER FALLBACK: A standard passport face is ~1.5 to 1.7 times its height from the top edge.
        # This cuts out the top page perfectly even if the shadow isn't detected.
        data_page_top = max(0, fy - int(fh * 1.7))
        
        # Scan upward for the spine/shadow, but limit the scan distance 
        # so we don't bleed into the top page text.
        scan_limit = max(0, fy - int(fh * 2.0))
        for y in range(max(0, fy - int(fh * 0.3)), scan_limit, -1):
            # Increased threshold to 165 for brighter mobile photos
            if np.mean(gray[y, col_l:col_r]) < 165:
                data_page_top = y + 5
                break
        
        # Add a much smaller margin so we don't accidentally pull in the top page
        top = max(0, data_page_top - int(fh * 0.15))

    # ── BOTTOM — last MRZ row ─────────────────────────────────────────────────
    mrz_search_start = min(fy + fh + int(fh * 0.5), h_img - 1)

    edges       = cv2.Canny(gray, 50, 150)
    row_density = np.sum(edges, axis=1)

    # Dynamic density threshold based on image width (~12% of the row width being edge pixels)
    # This prevents resolution differences from breaking the MRZ detection.
    DENSE      = int(w_img * 255 * 0.12)
    
    # Limit the downward search proportionally so we don't grab desk/fingers
    MAX_SEARCH = min(h_img, mrz_search_start + int(fh * 5))

    mrz_bottom = mrz_search_start   
    for y in range(MAX_SEARCH - 1, mrz_search_start, -1):
        if row_density[y] > DENSE:
            mrz_bottom = y
            break

    # Dynamic padding instead of hardcoded 80px
    bottom = min(h_img, mrz_bottom + int(fh * 0.3))

    # ── Sanity check ─────────────────────────────────────────────────────────
    if (bottom - top) < int(fh * 2.0):
        top    = max(0,     fy - int(fh * 1.5))
        bottom = min(h_img, fy + int(fh * 4.0))

    return img[top:bottom, 0:w_img].copy()


def extract_images_from_pdf_pages(pdf_path, page_indices):
    """Extract embedded images from specific PDF pages"""
    reader = PdfReader(pdf_path)
    image_paths = []
    for idx in page_indices:
        if idx >= len(reader.pages):
            continue
        page = reader.pages[idx]
        try:
            xobj = page.get('/Resources', {}).get('/XObject')
            if not xobj:
                continue
            xobj = xobj.get_object()
            for obj_name in xobj:
                obj = xobj[obj_name]
                if obj.get('/Subtype') == '/Image':
                    data = obj.get_data()
                    img = Image.open(io.BytesIO(data))
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    temp_path = os.path.join(UPLOAD_FOLDER, f"extracted_p{idx}_i{len(image_paths)}.jpg")
                    img.save(temp_path, 'JPEG')
                    image_paths.append(temp_path)
        except Exception:
            continue
    return image_paths

def process_pdf_upload(file, page_range, upload_folder=UPLOAD_FOLDER):
    """
    Main PDF processing function.
    Returns: (success: bool, result: zip_buffer or error_message)
    """
    filename = secure_filename(file.filename)
    pdf_path = os.path.join(upload_folder, filename)
    file.save(pdf_path)

    try:
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
        page_indices = parse_page_range(page_range, total_pages)
        extracted_image_paths = extract_images_from_pdf_pages(pdf_path, page_indices)

        if not extracted_image_paths:
            return False, "No embedded images found in the selected pages."

        cropped_images = []
        for img_path in extracted_image_paths:
            cropped = crop_by_face(img_path)
            if cropped is not None:
                cropped_images.append(cropped)
            if os.path.exists(img_path):
                os.remove(img_path)

        if not cropped_images:
            return False, "No face detected in any extracted image."

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w') as zipf:
            for idx, cropped_img in enumerate(cropped_images):
                success, encoded_img = cv2.imencode('.jpg', cropped_img)
                if success:
                    zipf.writestr(f"cropped_passport_{idx+1}.jpg", encoded_img.tobytes())

        zip_buffer.seek(0)
        return True, zip_buffer

    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Error: {str(e)}"
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

def process_image_upload(files, upload_folder=UPLOAD_FOLDER):
    """
    Process single or bulk image uploads.
    Returns: (success: bool, result: zip_buffer or error_msg, failed_files: list)
    """
    if not files or all(f.filename == '' for f in files):
        return False, "No files provided.", []

    cropped_images = []
    failed_files = []

    for file in files:
        if not file.filename:
            continue
            
        filename = secure_filename(file.filename)
        temp_path = os.path.join(upload_folder, filename)
        file.save(temp_path)

        try:
            # Reusing the existing crop_by_face logic
            cropped = crop_by_face(temp_path) 
            if cropped is not None:
                cropped_images.append((filename, cropped))
            else:
                failed_files.append(filename)
        except Exception as e:
            failed_files.append(filename)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    if not cropped_images:
        return False, "No faces detected in any of the uploaded images.", failed_files

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as zipf:
        for orig_name, cropped_img in cropped_images:
            success, encoded_img = cv2.imencode('.jpg', cropped_img)
            if success:
                base_name = os.path.splitext(orig_name)[0]
                zipf.writestr(f"{base_name}_cropped.jpg", encoded_img.tobytes())

    zip_buffer.seek(0)
    return True, zip_buffer, failed_files
