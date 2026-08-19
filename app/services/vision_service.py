"""
Service Vision — Analyse multimodale des tampons, signature et cachet du
Ministre sur le PDF de l'acte.

Utilise l'API Gemini (Google) plutôt que Hugging Face : contrairement aux
modèles servis via HF Inference Providers (instables — modèles retirés ou
mal routés selon les fournisseurs disponibles à un instant donné), Gemini
est appelé directement chez son éditeur, avec une sortie JSON garantie par
schéma strict (responseSchema) : plus besoin de deviner/parser un texte
libre en espérant qu'il contienne du JSON valide.
"""

import base64
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Schéma strict : Gemini est contraint de renvoyer EXACTEMENT cette forme,
# il ne peut pas "oublier" un champ ou répondre en texte libre.
SCHEMA_ANALYSE_VISUELLE = {
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
    "required": [
        "numero_acte_haut", "tampon_DGFP", "tampon_DIRSOLDE", "tampon_DPB",
        "tampon_CF", "tampon_DP", "signature_cachet_ministre_bas", "details",
    ],
}

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


def extraire_pages_pdf(pdf_bytes: bytes) -> list[str]:
    """Extrait la 1ère et dernière page en base64 (PNG)."""
    import fitz

    images_b64 = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    if len(doc) >= 1:
        pix1 = doc[0].get_pixmap(dpi=150)
        images_b64.append(
            base64.b64encode(pix1.tobytes("png")).decode("utf-8")
        )

    if len(doc) >= 2:
        pix_last = doc[-1].get_pixmap(dpi=150)
        images_b64.append(
            base64.b64encode(pix_last.tobytes("png")).decode("utf-8")
        )

    doc.close()
    return images_b64


def analyser_visuel_acte(pdf_bytes: bytes) -> dict:
    """Analyse visuelle du PDF (tampons, signature, cachet) via Gemini."""
    settings = get_settings()

    if settings.llm_mode == "cpu_local":
        logger.warning("⚠️ Vision désactivée en mode CPU local")
        return {**REPONSE_PAR_DEFAUT, "erreur": "Vision non disponible sur CPU",
                "details": "Vision désactivée en mode CPU. Vérification manuelle requise."}

    if not settings.gemini_api_key:
        logger.error("GEMINI_API_KEY manquant dans .env — vision désactivée.")
        return {**REPONSE_PAR_DEFAUT, "erreur": "GEMINI_API_KEY manquant",
                "details": "Ajoute GEMINI_API_KEY dans le fichier .env du serveur."}

    images_b64 = extraire_pages_pdf(pdf_bytes)
    if not images_b64:
        return {**REPONSE_PAR_DEFAUT, "erreur": "Impossible d'extraire les images du PDF."}

    nb_pages = len(images_b64)
    prompt_visuel = (
        f"Voici {nb_pages} page(s) d'un document administratif de la Fonction "
        "Publique du Sénégal (première page, puis dernière page si le document "
        "en a plusieurs). Examine TOUTES les pages fournies avant de répondre — "
        "les tampons et signatures se trouvent souvent en fin de document. "
        "Cherche précisément sur l'ensemble des pages :\n"
        "- le numéro de l'acte en haut de la première page (numero_acte_haut)\n"
        "- les tampons officiels apposés sur le document, sur n'importe quelle "
        "page fournie : DGFP, DIRSOLDE, DPB, CF, DP (un tampon par direction, "
        "généralement circulaire ou rectangulaire, avec le nom de la direction "
        "lisible dessus)\n"
        "- la signature manuscrite ET le cachet du Ministre, généralement en "
        "bas de la dernière page (signature_cachet_ministre_bas)\n"
        "Réponds true uniquement si tu es raisonnablement certain de la "
        "présence de l'élément (visible, lisible) sur au moins une des pages "
        "fournies. Réponds false si absent de toutes les pages, illisible, ou "
        "en cas de doute. Dans 'details', précise sur quelle page tu as trouvé "
        "chaque élément."
    )

    headers = {
        "x-goog-api-key": settings.gemini_api_key,
        "Content-Type": "application/json",
    }

    parts = [{"text": prompt_visuel}]
    for img_b64 in images_b64:
        parts.append({"inline_data": {"mime_type": "image/png", "data": img_b64}})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA_ANALYSE_VISUELLE,
            "temperature": 0.0,
        },
    }

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_vision_model}:generateContent"
    )

    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()

            candidats = result.get("candidates", [])
            if not candidats:
                bloque = result.get("promptFeedback", {}).get("blockReason")
                logger.warning(f"Aucune réponse Gemini (candidates vide). Bloqué : {bloque}")
                return {**REPONSE_PAR_DEFAUT, "erreur": f"Réponse vide (blockReason={bloque})"}

            texte_json = candidats[0]["content"]["parts"][0]["text"]
            import json
            resultat = json.loads(texte_json)
            logger.info(f"Réponse Gemini vision (pages envoyées={nb_pages}) : {resultat}")
            return resultat

    except httpx.HTTPStatusError as e:
        logger.error(f"Erreur API Gemini vision : {e.response.status_code} — {e.response.text}")
        return {**REPONSE_PAR_DEFAUT, "erreur": str(e)}

    except Exception as e:
        logger.error(f"Erreur vision : {e}")
        return {**REPONSE_PAR_DEFAUT, "erreur": str(e)}