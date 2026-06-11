import json
import logging
import os
import re
import unicodedata

import requests

logger = logging.getLogger(__name__)

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

_SYSTEM_INSTRUCTION = (
    "Tu extrais le titre exact de documents administratifs. "
    'Réponds UNIQUEMENT avec ce JSON : {"titre": "titre exact du document", '
    '"date": "YYYY-MM-DD ou null", "confiance": 0-100, "statut": "ok" ou "a_verifier"}'
)

_PROMPT_TEMPLATE = (
    "Voici le texte OCR d'un document administratif marocain.\n\n"
    "{ocr_text}\n\n"
    "Extrait le titre exact tel qu'il apparaît dans le document "
    "(première ligne ou en-tête principal). "
    "Réponds UNIQUEMENT en JSON valide, sans balises markdown."
)

_FORBIDDEN_CHARS_RE = re.compile(r'[/\\:*?"<>|]')
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _sanitize_title(titre: str) -> str:
    titre = unicodedata.normalize("NFKD", titre).encode("ascii", "ignore").decode()
    titre = titre.replace(" ", "-")
    titre = titre.capitalize()
    titre = _FORBIDDEN_CHARS_RE.sub("", titre)
    return titre[:120]


def _build_filename(titre: str, date: str | None, confiance: int) -> tuple[str, str]:
    """Return (nom_fichier, statut)."""
    if confiance >= 50:
        if date:
            return f"{titre}_{date}.pdf", "ok"
        return f"{titre}.pdf", "ok"
    return f"A-verifier_{titre}.pdf", "a_verifier"


def extract_title(ocr_text: str, original_filename: str) -> dict:
    """
    Appelle Gemini 2.5 Flash pour extraire le titre d'un document administratif.

    Retourne un dict avec les clés :
        titre, date, confiance, statut, nom_fichier
    """
    original_stem = os.path.splitext(original_filename)[0]

    def fallback() -> dict:
        return {
            "titre": original_stem,
            "date": None,
            "confiance": 0,
            "statut": "a_verifier",
            "nom_fichier": "A-verifier_" + original_filename,
        }

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.warning("ai_titler: GEMINI_API_KEY absent — fallback")
        return fallback()

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": _PROMPT_TEMPLATE.format(ocr_text=ocr_text[:3000])}
                ]
            }
        ],
        "generationConfig": {"responseMimeType": "application/json"},
        "systemInstruction": {"parts": [{"text": _SYSTEM_INSTRUCTION}]},
    }

    try:
        resp = requests.post(
            _GEMINI_URL,
            params={"key": api_key},
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        gemini_result = json.loads(raw_text)
    except Exception as exc:
        logger.warning("ai_titler: erreur API ou JSON invalide: %s", exc)
        return fallback()

    try:
        titre_raw = str(gemini_result.get("titre") or original_stem)
        date_raw = gemini_result.get("date") or None
        confiance = int(gemini_result.get("confiance", 0))

        if date_raw and not _DATE_RE.match(str(date_raw)):
            date_raw = None

        titre = _sanitize_title(titre_raw)
        if not titre:
            titre = _sanitize_title(original_stem) or "document"

        nom_fichier, statut = _build_filename(titre, date_raw, confiance)
    except Exception as exc:
        logger.warning("ai_titler: erreur post-traitement: %s", exc)
        return fallback()

    return {
        "titre": titre,
        "date": date_raw,
        "confiance": confiance,
        "statut": statut,
        "nom_fichier": nom_fichier,
    }
