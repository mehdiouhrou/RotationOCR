"""
Service de détection et correction de rotation pour PDF scannés.
Remplace l'OCR complet par un pipeline simplifié de correction de rotation.
"""

import os
import tempfile
import logging
import fitz  # PyMuPDF
from pdf2image import convert_from_path
import pytesseract
from PIL import Image
import subprocess
import shutil

# Configuration du logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def detect_pdf_rotation(pdf_path):
    """
    Détecte l'orientation d'un PDF scanné en analysant le texte avec Tesseract.
    
    Args:
        pdf_path: Chemin vers le fichier PDF
        
    Returns:
        int: Angle de rotation nécessaire (0, 90, 180, 270)
    """
    try:
        logger.info(f"Détection de rotation pour: {pdf_path}")
        
        # Méthode 1: Essayer d'extraire le texte avec PyMuPDF
        doc = fitz.open(pdf_path)
        text = ""
        for page_num in range(min(3, len(doc))):  # Analyser max 3 premières pages
            page = doc[page_num]
            text += page.get_text()
        
        doc.close()
        
        # Si on a du texte, essayer de détecter l'orientation avec Tesseract
        if text.strip():
            # Créer une image temporaire de la première page pour analyse
            images = convert_from_path(pdf_path, first_page=1, last_page=1)
            if images:
                # Convertir en niveaux de gris pour meilleure détection
                img = images[0].convert('L')
                
                # Utiliser pytesseract pour détecter l'orientation
                try:
                    osd = pytesseract.image_to_osd(img)
                    # Parser le résultat OSD
                    angle = 0
                    for line in osd.split('\n'):
                        if 'Rotate' in line:
                            angle = int(line.split(':')[1].strip())
                            break
                    
                    logger.info(f"Angle détecté par Tesseract: {angle}°")
                    return angle
                except Exception as e:
                    logger.warning(f"Tesseract OSD échoué: {e}")
        
        # Méthode 2: Analyse heuristique basée sur les dimensions
        # Si le PDF semble être en portrait mais a des dimensions paysage, il est probablement tourné
        doc = fitz.open(pdf_path)
        page = doc[0]
        width, height = page.rect.width, page.rect.height
        doc.close()
        
        logger.info(f"Dimensions PDF: {width:.0f}x{height:.0f}")
        
        # Ratio largeur/hauteur
        ratio = width / height if height > 0 else 1
        
        # Si ratio > 1.3 (paysage) mais le contenu texte suggère portrait, rotation probable
        if ratio > 1.3:
            # Probablement en paysage, mais pourrait être un portrait tourné à 90°
            # On retourne 0 par défaut, l'utilisateur pourra corriger manuellement si besoin
            logger.info("PDF en orientation paysage détectée")
            return 0
        elif ratio < 0.77:
            logger.info("PDF en orientation portrait détectée")
            return 0
        else:
            logger.info("Ratio carré, orientation indéterminée")
            return 0
            
    except Exception as e:
        logger.error(f"Erreur lors de la détection de rotation: {e}")
        return 0

def rotate_and_compress_pdf(input_path, output_path, rotation_angle=0):
    """
    Tourne et compresse un PDF.
    
    Args:
        input_path: Chemin du PDF d'entrée
        output_path: Chemin du PDF de sortie
        rotation_angle: Angle de rotation (0, 90, 180, 270)
        
    Returns:
        bool: True si succès, False sinon
    """
    try:
        logger.info(f"Rotation de {rotation_angle}° pour: {input_path}")
        
        # Ouvrir le PDF source
        doc = fitz.open(input_path)
        
        # Créer un nouveau document
        new_doc = fitz.open()
        
        # Pour chaque page, appliquer la rotation
        for page_num in range(len(doc)):
            page = doc[page_num]
            
            # Créer une nouvelle page avec les mêmes dimensions
            rect = page.rect
            
            # Si rotation de 90° ou 270°, inverser largeur/hauteur
            if rotation_angle in [90, 270]:
                new_rect = fitz.Rect(0, 0, rect.height, rect.width)
            else:
                new_rect = fitz.Rect(0, 0, rect.width, rect.height)
            
            new_page = new_doc.new_page(width=new_rect.width, height=new_rect.height)
            
            # Copier le contenu de la page originale
            new_page.show_pdf_page(
                new_rect,
                doc,
                page_num,
                rotate=rotation_angle
            )
        
        # Sauvegarder le PDF tourné
        new_doc.save(output_path)
        new_doc.close()
        doc.close()
        
        logger.info(f"PDF tourné sauvegardé: {output_path}")
        
        # Compression avec Ghostscript si disponible
        if shutil.which("gs"):
            try:
                temp_output = output_path + ".compressed"
                cmd = [
                    "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
                    "-dPDFSETTINGS=/ebook", "-dNOPAUSE", "-dQUIET", "-dBATCH",
                    f"-sOutputFile={temp_output}", output_path
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    # Remplacer le fichier original par le compressé
                    os.replace(temp_output, output_path)
                    logger.info("Compression Ghostscript réussie")
                else:
                    logger.warning(f"Échec compression Ghostscript: {result.stderr}")
                    if os.path.exists(temp_output):
                        os.remove(temp_output)
            except Exception as e:
                logger.warning(f"Erreur lors de la compression: {e}")
        
        return True
        
    except Exception as e:
        logger.error(f"Erreur lors de la rotation/compression: {e}")
        return False

def process_pdf_rotation(input_path, output_dir):
    """
    Traite un PDF complet: détection + rotation + compression.
    
    Args:
        input_path: Chemin du PDF d'entrée
        output_dir: Répertoire de sortie
        
    Returns:
        tuple: (chemin_sortie, angle_détecté, succès)
    """
    try:
        # Nom du fichier de sortie
        filename = os.path.basename(input_path)
        output_path = os.path.join(output_dir, filename)
        
        # Détecter l'angle de rotation
        rotation_angle = detect_pdf_rotation(input_path)
        
        # Si angle non nul, appliquer la rotation
        if rotation_angle != 0:
            success = rotate_and_compress_pdf(input_path, output_path, rotation_angle)
            if success:
                logger.info(f"PDF traité avec rotation de {rotation_angle}°: {output_path}")
                return output_path, rotation_angle, True
            else:
                logger.error(f"Échec de la rotation pour: {input_path}")
                return None, rotation_angle, False
        else:
            # Pas de rotation nécessaire, juste copier et compresser
            success = rotate_and_compress_pdf(input_path, output_path, 0)
            if success:
                logger.info(f"PDF copié sans rotation: {output_path}")
                return output_path, 0, True
            else:
                # Fallback: simple copie
                shutil.copy2(input_path, output_path)
                logger.info(f"PDF copié (fallback): {output_path}")
                return output_path, 0, True
                
    except Exception as e:
        logger.error(f"Erreur lors du traitement PDF: {e}")
        return None, 0, False

if __name__ == "__main__":
    # Test du module
    import sys
    if len(sys.argv) > 1:
        pdf_path = sys.argv[1]
        angle = detect_pdf_rotation(pdf_path)
        print(f"Angle détecté: {angle}°")
        
        if len(sys.argv) > 2:
            output_path = sys.argv[2]
            rotate_and_compress_pdf(pdf_path, output_path, angle)
            print(f"PDF traité: {output_path}")
    else:
        print("Usage: python rotation_service.py <input.pdf> [output.pdf]")