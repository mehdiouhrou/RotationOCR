"""
Service de détection et correction de rotation pour PDF scannés.
Pipeline : pdf2image (dpi=100) → PaddleOCR (angle + texte)
         → PIL rotation → reconstruction PDF → Ghostscript (dpi=200).
Le texte OCR est extrait AVANT Ghostscript pour préserver la couche texte.
"""

import io
import os
import logging
import shutil
import subprocess

import fitz  # PyMuPDF
import numpy as np
from pdf2image import convert_from_path
from paddleocr import PaddleOCR

logger = logging.getLogger(__name__)

_ocr = PaddleOCR(use_angle_cls=True, lang='fr', use_gpu=False, show_log=False)
_OCR_DPI        = 100   # Rapide, suffisant pour extraction texte
_ROT_DPI        = 200   # Qualité pour archivage
_MAX_OCR_PAGES  = 5     # Pages analysées pour le texte OCR


# ---------------------------------------------------------------------------
# Helpers privés
# ---------------------------------------------------------------------------

def _cls_is_upright(cls_result) -> bool:
    """True = 0°, False = 180°."""
    if not cls_result:
        return True
    entry = cls_result[0] if isinstance(cls_result, list) else cls_result
    if isinstance(entry, (list, tuple)) and entry:
        label = entry[0]
    else:
        label = entry
    return str(label) == "0"


def _text_from_ocr_result(result) -> str:
    if not result or not result[0]:
        return ""
    parts: list[str] = []
    for line in result[0]:
        if line and len(line) >= 2 and line[1]:
            parts.append(line[1][0])
    return " ".join(parts)


def _extract_text_and_angle(pdf_path: str) -> tuple[int, str]:
    """
    Convertit les 5 premières pages en images (100 dpi), exécute PaddleOCR
    sur chaque page, déduit l'angle global (majorité 180°) et concatène le texte.
    """
    images = convert_from_path(
        pdf_path, dpi=_OCR_DPI, first_page=1, last_page=_MAX_OCR_PAGES,
    )
    text_parts: list[str] = []
    cls_flags: list[bool] = []

    for i, img in enumerate(images):
        img_array = np.array(img.convert("RGB"))
        try:
            cls_result = _ocr.ocr(img_array, det=False, rec=False, cls=True)
            cls_flags.append(_cls_is_upright(cls_result))

            ocr_result = _ocr.ocr(img_array, cls=True)
            page_text = _text_from_ocr_result(ocr_result)
            if page_text.strip():
                text_parts.append(page_text)
        except Exception as exc:
            logger.warning("PaddleOCR page %d: %s", i + 1, exc)
            cls_flags.append(True)

    upside_down = sum(1 for upright in cls_flags if not upright)
    angle = 180 if cls_flags and upside_down > len(cls_flags) / 2 else 0
    return angle, "\n".join(text_parts)


# ---------------------------------------------------------------------------
# Rotation + compression
# ---------------------------------------------------------------------------

def rotate_and_compress_pdf(input_path: str, output_path: str, rotation_angle: int = 0) -> bool:
    """
    Applique la rotation via PIL (expand=True) et compresse avec Ghostscript.
    Si rotation_angle == 0 : copie directe puis compression.
    Retourne True si succès.
    """
    try:
        if rotation_angle != 0:
            # Conversion pages → images PIL à _ROT_DPI
            images = convert_from_path(input_path, dpi=_ROT_DPI)

            # Rotation PIL — sens contraire pour corriger l'orientation détectée
            rotated = [img.rotate(-rotation_angle, expand=True) for img in images]

            # Reconstruction PDF PyMuPDF depuis images PIL
            new_doc = fitz.open()
            for img in rotated:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                img_data = buf.getvalue()

                # Points = pixels × (72 pt/inch) / dpi
                w_pt = img.width  * 72 / _ROT_DPI
                h_pt = img.height * 72 / _ROT_DPI

                page = new_doc.new_page(width=w_pt, height=h_pt)
                page.insert_image(page.rect, stream=img_data)

            new_doc.save(output_path)
            new_doc.close()
            logger.info("PDF tourné (%s°) reconstruit depuis PIL: %s", rotation_angle, output_path)
        else:
            shutil.copy2(input_path, output_path)

        # Compression Ghostscript
        if shutil.which("gs"):
            temp = output_path + ".compressed"
            cmd = [
                "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
                "-dPDFSETTINGS=/ebook", "-dNOPAUSE", "-dQUIET", "-dBATCH",
                f"-sOutputFile={temp}", output_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                os.replace(temp, output_path)
                logger.info("Compression Ghostscript réussie")
            else:
                logger.warning("Ghostscript échoué: %s", result.stderr[:200])
                if os.path.exists(temp):
                    os.remove(temp)

        return True

    except Exception as exc:
        logger.error("rotate_and_compress_pdf: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Point d'entrée principal
# ---------------------------------------------------------------------------

def process_pdf_rotation(input_path: str, output_dir: str) -> tuple[str | None, int, bool, str]:
    """
    Traite un PDF complet :
      1. PaddleOCR sur les 5 premières pages (100 dpi) : angle + texte
      2. Rotation PIL + compression Ghostscript à 200 dpi

    Retourne (chemin_sortie, angle_détecté, succès, ocr_text).
    """
    try:
        filename    = os.path.basename(input_path)
        output_path = os.path.join(output_dir, filename)

        rotation_angle, ocr_text = _extract_text_and_angle(input_path)
        logger.info(
            "OCR terminé: %d caractères extraits, angle=%s°",
            len(ocr_text), rotation_angle,
        )

        success = rotate_and_compress_pdf(input_path, output_path, rotation_angle)

        if success:
            return output_path, rotation_angle, True, ocr_text

        shutil.copy2(input_path, output_path)
        logger.warning("Fallback copie brute: %s", output_path)
        return output_path, rotation_angle, True, ocr_text

    except Exception as exc:
        logger.error("process_pdf_rotation: %s", exc)
        return None, 0, False, ""


# ---------------------------------------------------------------------------
# CLI de test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python rotation_service.py <input.pdf> [output_dir]")
        sys.exit(1)

    pdf  = sys.argv[1]
    odir = sys.argv[2] if len(sys.argv) > 2 else os.path.dirname(pdf) or "."
    out, angle, ok, text = process_pdf_rotation(pdf, odir)
    print(f"Sortie : {out}  angle={angle}°  succès={ok}")
    print(f"Texte OCR ({len(text)} chars) :\n{text[:500]}")
