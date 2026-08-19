"""
Service Vision — 100% open source, sans API externe payante.

Constat sur les documents réels du projet : ce sont des PDF générés
numériquement (texte tapé), pas des scans. Les "tampons" (DGFP, DIRSOLDE,
DPB, CF, DP) sont du texte réel inséré dans des encadrés en pointillés au
moment où l'étape correspondante du circuit est franchie — ils sont donc
directement lisibles dans le texte du PDF, sans avoir besoin d'un LLM de
vision. Seuls la signature manuscrite et le cachet du Ministre sont de
véritables éléments graphiques (image incrustée dans le PDF), détectés ici
par la simple présence d'une image sur la page — pas besoin d'IA non plus.

Si un jour un document est un vrai scan/photo (sans couche de texte
exploitable), un repli optionnel vers un LLM de vision (Gemini) est prévu,
mais uniquement si GEMINI_API_KEY est configuré — sinon le résultat indique
clairement qu'une vérification manuelle est nécessaire, plutôt que d'inventer
un résultat.
"""

import base64
import logging
import re

from app.config import get_settings

logger = logging.getLogger(__name__)

REPONSE_PAR_DEFAUT = {
    "numero_acte_haut": False,
    "tampon_DGFP": False,
    "tampon_DIRSOLDE": False,
    "tampon_DPB": False,
    "tampon_CF": False,
    "tampon_DP": False,
    "signature_cachet_ministre_bas": False,
    "details": "",
}

# Codes tampons -> clé de résultat correspondante
CODES_TAMPONS = {
    "DGFP": "tampon_DGFP",
    "DIRSOLDE": "tampon_DIRSOLDE",
    "DS": "tampon_DIRSOLDE",  # alias parfois utilisé pour Dir. Solde
    "DPB": "tampon_DPB",
    "CF": "tampon_CF",
    "DP": "tampon_DP",
}

# Un numéro d'acte réel contient des chiffres après "N°" ou "N".
# "N° …" (points de suspension) = acte pas encore numéroté = absent.
_RE_NUMERO_ACTE = re.compile(r"N\s*°?\s*(\d[\d\s/\.\-]*\d)")


def extraire_pages_pdf(pdf_bytes: bytes) -> list[str]:
    """Extrait la 1ère et dernière page en base64 (PNG) — conservé pour
    compatibilité avec le repli optionnel vers un LLM de vision."""
    import fitz

    images_b64 = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    if len(doc) >= 1:
        pix1 = doc[0].get_pixmap(dpi=150)
        images_b64.append(base64.b64encode(pix1.tobytes("png")).decode("utf-8"))

    if len(doc) >= 2:
        pix_last = doc[-1].get_pixmap(dpi=150)
        images_b64.append(base64.b64encode(pix_last.tobytes("png")).decode("utf-8"))

    doc.close()
    return images_b64


def _detecter_tampons_par_texte(texte: str) -> dict:
    """Cherche chaque code de tampon comme mot isolé dans le texte du PDF."""
    resultat = {}
    for code, cle in CODES_TAMPONS.items():
        # \b = limite de mot, pour ne pas matcher "DGFP" à l'intérieur d'un
        # autre mot plus long par accident.
        pattern = r"\b" + re.escape(code) + r"\b"
        resultat[cle] = bool(re.search(pattern, texte))
    return resultat


def _detecter_numero_acte(texte: str) -> bool:
    """Un numéro d'acte est présent seulement s'il contient de vrais chiffres
    (pas juste 'N° …' avec des points de suspension, qui signale un acte
    pas encore numéroté)."""
    debut_texte = texte[:400]  # le numéro est toujours en haut de la 1ère page
    return bool(_RE_NUMERO_ACTE.search(debut_texte))


def analyser_visuel_acte(pdf_bytes: bytes) -> dict:
    """Analyse le PDF : tampons + numéro via le texte natif, signature/cachet
    via la présence d'une image incrustée sur le document."""
    import fitz

    settings = get_settings()

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        logger.error(f"PDF illisible : {e}")
        return {**REPONSE_PAR_DEFAUT, "erreur": f"PDF illisible : {e}"}

    texte_complet = ""
    nb_images_total = 0
    details_images = []

    for i, page in enumerate(doc):
        texte_complet += page.get_text() + "\n"
        images = page.get_images(full=True)
        nb_images_total += len(images)
        if images:
            details_images.append(f"page {i + 1} : {len(images)} image(s) incrustée(s)")

    nb_pages = len(doc)
    doc.close()

    texte_exploitable = len(texte_complet.strip()) > 30

    if not texte_exploitable:
        # Probablement un vrai scan/photo sans couche de texte. Repli
        # optionnel sur un LLM de vision si configuré, sinon vérification
        # manuelle demandée explicitement (pas de résultat inventé).
        logger.warning(
            "PDF sans texte exploitable (probablement un scan/image). "
            "Détection par texte impossible."
        )
        if settings.gemini_api_key:
            logger.info("Repli sur Gemini (GEMINI_API_KEY configuré) pour ce PDF scanné.")
            return _analyser_via_gemini_fallback(pdf_bytes, settings)

        return {
            **REPONSE_PAR_DEFAUT,
            "erreur": "PDF sans texte exploitable (scan/image) et aucun repli vision configuré.",
            "details": "Ce document semble être un scan/une image sans texte extractible. "
                       "Vérification manuelle requise, ou configurer GEMINI_API_KEY pour "
                       "activer le repli automatique sur un LLM de vision.",
        }

    resultat = _detecter_tampons_par_texte(texte_complet)
    resultat["numero_acte_haut"] = _detecter_numero_acte(texte_complet)
    # Une image incrustée sur le document = signature/cachet du Ministre
    # (c'est le seul élément graphique de ces documents, inséré uniquement
    # une fois l'acte réellement signé).
    resultat["signature_cachet_ministre_bas"] = nb_images_total > 0

    resultat["details"] = (
        f"Analyse par extraction native ({nb_pages} page(s), "
        f"{len(texte_complet)} caractères de texte). "
        + ("; ".join(details_images) if details_images else "Aucune image incrustée détectée.")
    )

    logger.info(f"Résultat analyse visuelle (extraction native) : {resultat}")
    return resultat


def _analyser_via_gemini_fallback(pdf_bytes: bytes, settings) -> dict:
    """Repli optionnel : uniquement appelé si le PDF n'a pas de texte
    exploitable (vrai scan) ET que GEMINI_API_KEY est configuré."""
    import httpx
    import json

    images_b64 = extraire_pages_pdf(pdf_bytes)
    if not images_b64:
        return {**REPONSE_PAR_DEFAUT, "erreur": "Impossible d'extraire les images du PDF."}

    schema = {
        "type": "OBJECT",
        "properties": {
            "numero_acte_haut": {"type": "BOOLEAN"},
            "tampon_DGFP": {"type": "BOOLEAN"},
            "tampon_DIRSOLDE": {"type": "BOOLEAN"},
            "tampon_DPB": {"type": "BOOLEAN"},
            "tampon_CF": {"type": "BOOLEAN"},
            "tampon_DP": {"type": "BOOLEAN"},
            "signature_cachet_ministre_bas": {"type": "BOOLEAN"},
            "details": {"type": "STRING"},
        },
        "required": list(REPONSE_PAR_DEFAUT.keys()),
    }

    prompt = (
        "Ce document scanné de la Fonction Publique du Sénégal doit être "
        "analysé : présence du numéro d'acte, des tampons DGFP, DIRSOLDE, "
        "DPB, CF, DP, et de la signature/cachet du Ministre."
    )

    parts = [{"text": prompt}]
    for img_b64 in images_b64:
        parts.append({"inline_data": {"mime_type": "image/png", "data": img_b64}})

    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{settings.gemini_vision_model}:generateContent",
                headers={"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"},
                json={
                    "contents": [{"parts": parts}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "responseSchema": schema,
                        "temperature": 0.0,
                    },
                },
            )
            response.raise_for_status()
            result = response.json()
            texte_json = result["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(texte_json)
    except Exception as e:
        logger.error(f"Erreur repli Gemini : {e}")
        return {**REPONSE_PAR_DEFAUT, "erreur": str(e)}