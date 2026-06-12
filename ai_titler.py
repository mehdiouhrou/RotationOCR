import json
import logging
import os
import re
import time
import unicodedata

import requests

logger = logging.getLogger(__name__)

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

_SYSTEM_INSTRUCTION = (
    "Tu es un expert en archivage de documents administratifs marocains. "
    "Tu analyses le texte OCR d'un document et tu génères un titre de fichier "
    "structuré et unique selon ces règles STRICTES :\n\n"
    "STRUCTURE : [Type]_[Sujet-principal]\n"
    "- Type : un seul mot parmi : Budget, Note, Rapport, Compte-rendu, Lettre, "
    "Fiche, Plan, Annexe, Decision, Circulaire, Convention, Contrat, "
    "Proces-verbal, Demande, Attestation, Autre\n"
    "- Sujet : 2 à 5 mots MAX décrivant le contenu spécifique, "
    "séparés par des tirets\n"
    "- Ne jamais répéter le type dans le sujet\n"
    "- Maximum 60 caractères au total (type + sujet)\n"
    "- Pas d'articles (le, la, les, de, du, des, un, une)\n"
    "- Exemples corrects :\n"
    "  Budget_Fonctionnement-Mars-2023\n"
    "  Budget_Investissement-Fiches-Assainissement\n"
    "  Compte-rendu_Reunion-Transport-Aerien\n"
    "  Note_Presentation-Budget-2023\n"
    "  Plan_Pluriannuel-Eau-Electricite-2027\n\n"
    "Réponds UNIQUEMENT avec ce JSON valide sans markdown :\n"
    '{\n'
    '  "titre": "Type_Sujet-principal",\n'
    '  "date": "YYYY-MM-DD ou null",\n'
    '  "confiance": 0-100,\n'
    '  "statut": "ok" ou "a_verifier"\n'
    '}'
)

_PROMPT_TEMPLATE = (
    "Voici le texte OCR d'un document administratif marocain.\n\n"
    "{ocr_text}\n\n"
    "Génère un titre de fichier structuré selon le format [Type]_[Sujet-principal]. "
    "Le sujet doit différencier ce document des autres documents du même type. "
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

    gemini_result = None
    for attempt in range(2):
        if attempt > 0:
            time.sleep(2)
        try:
            resp = requests.post(
                _GEMINI_URL,
                params={"key": api_key},
                json=payload,
                timeout=15,
            )
            if resp.status_code == 429:
                logger.warning(
                    "ai_titler: 429 Too Many Requests (tentative %d/2)",
                    attempt + 1,
                )
                if attempt == 1:
                    return fallback()
                continue
            resp.raise_for_status()
            raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            gemini_result = json.loads(raw_text)
            break
        except Exception as exc:
            logger.warning("ai_titler: erreur API ou JSON invalide: %s", exc)
            return fallback()

    if gemini_result is None:
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
