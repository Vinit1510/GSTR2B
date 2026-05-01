"""Advanced multi-pass CAPTCHA solver using dual-model ddddocr engines.

Strategies:
1. BETA Model (High Intelligence)
2. STANDARD Model (Strict Number Style)
3. THICK-DIGIT Pass (Binarization + Erosion)
4. Multi-Scale passes (2x, 3x)
"""
from __future__ import annotations

import logging
import re
import io
import cv2
import numpy as np
from PIL import Image

log = logging.getLogger("gstr2b.captcha")

_ocr_beta = None
_ocr_std = None

def _get_ocr_beta():
    global _ocr_beta
    if _ocr_beta is None:
        import ddddocr  # type: ignore
        log.info("Initialising ddddocr reader (BETA MODEL)...")
        _ocr_beta = ddddocr.DdddOcr(show_ad=False, beta=True)
    return _ocr_beta

def _get_ocr_std():
    global _ocr_std
    if _ocr_std is None:
        import ddddocr  # type: ignore
        log.info("Initialising ddddocr reader (STANDARD MODEL - Numbers Only)...")
        _ocr_std = ddddocr.DdddOcr(show_ad=False, beta=False)
        _ocr_std.set_ranges(0)  # Number-only style
    return _ocr_std

def solve_captcha(image_bytes: bytes) -> str | None:
    """Try multiple preprocessing and model strategies to solve the CAPTCHA."""
    try:
        ocr_beta = _get_ocr_beta()
        ocr_std = _get_ocr_std()
        
        # 1. Prepare multiple versions of the image
        img_np = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        
        # Version A: 2.5x Cubic Scale + CLAHE (Good for thin digits)
        v_scale = cv2.resize(img_np, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(v_scale, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
        v_clahe = clahe.apply(gray)
        _, v_clahe_bytes = cv2.imencode(".png", v_clahe)
        
        # Version B: Red Line Removal + 2x Lanczos
        hsv = cv2.cvtColor(img_np, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array([0, 100, 100]), np.array([10, 255, 255])) # Red mask
        cleaned = img_np.copy()
        cleaned[mask > 0] = [200, 200, 200] # Neutralize red to light grey
        v_no_red = cv2.resize(cleaned, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_LANCZOS4)
        _, v_no_red_bytes = cv2.imencode(".png", v_no_red)

        # Version C: THICK-DIGIT (Binarization + Erosion)
        # This helps when digits are very faint or broken
        gray_orig = cv2.cvtColor(img_np, cv2.COLOR_BGR2GRAY)
        thresh = cv2.threshold(gray_orig, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        kernel = np.ones((2,2), np.uint8)
        eroded = cv2.erode(thresh, kernel, iterations=1)
        # Invert back to black-on-white for ddddocr
        thick = cv2.bitwise_not(eroded)
        v_thick = cv2.resize(thick, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        _, v_thick_bytes = cv2.imencode(".png", v_thick)

        # 2. RUN PASSES
        # PASS 1: Intelligence (BETA)
        for b in [v_clahe_bytes.tobytes(), v_no_red_bytes.tobytes(), image_bytes, v_thick_bytes.tobytes()]:
            res = "".join(re.findall(r"\d", ocr_beta.classification(b)))
            if len(res) == 6: return res
            
        # PASS 2: Numbers-Only Style (STANDARD)
        for b in [v_clahe_bytes.tobytes(), v_thick_bytes.tobytes(), v_no_red_bytes.tobytes(), image_bytes]:
            res = "".join(re.findall(r"\d", ocr_std.classification(b)))
            if len(res) == 6: return res

        return None
    except Exception as exc:
        log.error("Captcha solver error: %s", exc)
        return None
