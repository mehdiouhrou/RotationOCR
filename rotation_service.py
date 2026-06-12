"""
Service de détection et correction de rotation pour PDF scannés.
Pipeline : pdf2image (dpi=150) → Tesseract (angle + texte, filtre photos)
         → PIL rotation → reconstruction PDF → Ghostscript (dpi=200).
Le texte OCR est extrait AVANT Ghostscript pour préserver la couche texte.
"""

import io
import os
import logging
import shutil
import subprocess

import fitz  # PyMuPDF
from pdf2image import convert_from_path
import pytesseract
from pytesseract import Output

logger = logging.getLogger(__name__)

_TESSERACT_LANGS = os.environ.get("TESSERACT_LANGS", "fra+ara+eng")
_OCR_DPI        = 100   # Rapide, suffisant pour extraction texte
_ROT_DPI        = 200   # Qualité pour archivage
_MAX_OCR_PAGES  = 5     # Pages analysées pour le texte OCR
_MIN_TEXT_WORDS = 5     # Seuil mots fiables pour distinguer page-texte / photo


# ---------------------------------------------------------------------------
# Helpers privés
# ---------------------------------------------------------------------------

def _images_from_pdf(pdf_path: str, dpi: int, max_pages: int | None = None):
    """Convertit les premières pages d'un PDF en liste d'images PIL."""
    kwargs: dict = {"dpi": dpi, "first_page": 1}
    if max_pages:
        kwargs["last_page"] = max_pages
    return convert_from_path(pdf_path, **kwargs)


def _run_tesseract(images) -> tuple[int, str]:
    """
    Depuis une liste d'images PIL, en un seul appel Tesseract par page :
      - Détecte l'angle de rotation via OSD (page 1 uniquement)
      - Extrait le texte OCR via image_to_data (--psm 3)
        et ignore les pages-photos (< _MIN_TEXT_WORDS mots conf > 30)

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

        # Un seul appel : détection photo + extraction texte fusionnés
        try:
            data = pytesseract.image_to_data(
                gray,
                lang=_TESSERACT_LANGS,
                output_type=Output.DICT,
                config="--psm 3",
            )
            words = [
                txt for txt, conf in zip(data["text"], data["conf"])
                if txt.strip() and int(conf) > 30
            ]
            if len(words) < _MIN_TEXT_WORDS:
                logger.debug("Page %d ignorée (photo ou vide, %d mots)", i + 1, len(words))
                continue
            text_parts.append(" ".join(words))
        except Exception as exc:
            logger.warning("Tesseract image_to_data page %d: %s", i + 1, exc)

    return rotation_angle, "\n".join(text_parts)


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

            # Rotation PIL — sens contraire au sens Tesseract pour corriger
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
      1. Conversion → images PIL à 150 dpi (rapide)
      2. Tesseract : angle OSD (page 1) + texte des pages-texte (filtre photos)
         — un seul appel image_to_data par page
      3. Rotation PIL + compression Ghostscript à 200 dpi

    Retourne (chemin_sortie, angle_détecté, succès, ocr_text).
    """
    try:
        filename    = os.path.basename(input_path)
        output_path = os.path.join(output_dir, filename)

        # 1. Images basse résolution pour OCR (rapide)
        images = _images_from_pdf(input_path, dpi=_OCR_DPI, max_pages=_MAX_OCR_PAGES)

        # 2. Détection angle + extraction texte (un appel Tesseract/page, filtre photos)
        rotation_angle, ocr_text = _run_tesseract(images)
        logger.info(
            "OCR terminé: %d caractères extraits, angle=%s°",
            len(ocr_text), rotation_angle,
        )

        # 3. Rotation PIL + compression GS (texte déjà extrait avant cette étape)
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
