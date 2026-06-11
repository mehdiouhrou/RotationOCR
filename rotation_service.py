"""
Service de détection et correction de rotation pour PDF scannés.
Pipeline : pdf2image → Tesseract (angle + texte) → PyMuPDF rotation → Ghostscript compression.
Le texte OCR est extrait AVANT Ghostscript pour préserver la couche texte.
"""

import os
import logging
import shutil
import subprocess

import fitz  # PyMuPDF
from pdf2image import convert_from_path
import pytesseract

logger = logging.getLogger(__name__)

_TESSERACT_LANGS = os.environ.get("TESSERACT_LANGS", "fra+ara+eng")
_MAX_OCR_PAGES   = 3   # Nombre de pages converties pour extraction texte (3000 chars suffisent)


# ---------------------------------------------------------------------------
# Helpers privés
# ---------------------------------------------------------------------------

def _images_from_pdf(pdf_path: str, max_pages: int = _MAX_OCR_PAGES):
    """Convertit les premières pages d'un PDF en liste d'images PIL (300 dpi)."""
    return convert_from_path(pdf_path, dpi=300, first_page=1, last_page=max_pages)


def _run_tesseract(images) -> tuple[int, str]:
    """
    Depuis une liste d'images PIL :
      - Détecte l'angle de rotation via OSD (page 1 uniquement)
      - Extrait le texte OCR via image_to_string (toutes les pages)

    Retourne (rotation_angle, ocr_text).
    """
    rotation_angle = 0
    text_parts: list[str] = []

    for i, img in enumerate(images):
        gray = img.convert("L")

        # OSD sur la première page seulement
        if i == 0:
            try:
                osd = pytesseract.image_to_osd(gray)
                for line in osd.split("\n"):
                    if "Rotate" in line:
                        rotation_angle = int(line.split(":")[1].strip())
                        break
                logger.info("Tesseract OSD: angle=%s°", rotation_angle)
            except Exception as exc:
                logger.warning("Tesseract OSD échoué (page 1): %s", exc)

        # Extraction texte
        try:
            text = pytesseract.image_to_string(gray, lang=_TESSERACT_LANGS)
            if text.strip():
                text_parts.append(text)
        except Exception as exc:
            logger.warning("Tesseract image_to_string page %d: %s", i + 1, exc)

    return rotation_angle, "\n".join(text_parts)


# ---------------------------------------------------------------------------
# Rotation + compression
# ---------------------------------------------------------------------------

def rotate_and_compress_pdf(input_path: str, output_path: str, rotation_angle: int = 0) -> bool:
    """
    Applique la rotation et compresse un PDF avec Ghostscript.
    Retourne True si succès.
    """
    try:
        doc     = fitz.open(input_path)
        new_doc = fitz.open()

        for page_num in range(len(doc)):
            page = doc[page_num]
            rect = page.rect

            if rotation_angle in (90, 270):
                new_rect = fitz.Rect(0, 0, rect.height, rect.width)
            else:
                new_rect = fitz.Rect(0, 0, rect.width, rect.height)

            new_page = new_doc.new_page(width=new_rect.width, height=new_rect.height)
            new_page.show_pdf_page(new_rect, doc, page_num, rotate=rotation_angle)

        new_doc.save(output_path)
        new_doc.close()
        doc.close()
        logger.info("PDF tourné (%s°) sauvegardé: %s", rotation_angle, output_path)

        # Compression Ghostscript (facultative)
        if shutil.which("gs"):
            temp = output_path + ".compressed"
            cmd  = [
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
    Traite un PDF complet : images → Tesseract (texte + angle) → rotation → compression.

    Retourne (chemin_sortie, angle_détecté, succès, ocr_text).
    Le texte OCR est extrait AVANT Ghostscript pour garantir sa disponibilité.
    """
    try:
        filename    = os.path.basename(input_path)
        output_path = os.path.join(output_dir, filename)

        # 1. Conversion PDF → images PIL (avant tout traitement)
        images = _images_from_pdf(input_path)

        # 2. Tesseract : angle de rotation + texte OCR en un seul passage
        rotation_angle, ocr_text = _run_tesseract(images)
        logger.info("OCR terminé: %d caractères extraits, angle=%s°",
                    len(ocr_text), rotation_angle)

        # 3. Rotation + compression (Ghostscript ici — ne touche pas au texte déjà extrait)
        success = rotate_and_compress_pdf(input_path, output_path, rotation_angle)

        if success:
            return output_path, rotation_angle, True, ocr_text

        # Fallback : copie brute si rotate_and_compress_pdf échoue
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

    pdf   = sys.argv[1]
    odir  = sys.argv[2] if len(sys.argv) > 2 else os.path.dirname(pdf) or "."
    out, angle, ok, text = process_pdf_rotation(pdf, odir)
    print(f"Sortie : {out}  angle={angle}°  succès={ok}")
    print(f"Texte OCR ({len(text)} chars) :\n{text[:500]}")
