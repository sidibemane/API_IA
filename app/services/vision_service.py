"""
Service Vision — Adapté CPU.
En mode api_hf : appelle l'API HF Inference avec l'image.
En mode cpu_local : vision DÉSACTIVÉE (trop lourd pour CPU).
"""

import base64
import json
import logging
import re
import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def extraire_pages_pdf(pdf_bytes: bytes) -> list[str]:
    """Extrait la 1ère et dernière page en base64."""
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
    """Analyse visuelle adaptée CPU."""
    settings = get_settings()

    if settings.llm_mode == "cpu_local":
        logger.warning("⚠️ Vision désactivée en mode CPU local")
        return {
            "numero_acte_haut": False,
            "tampon_DGFP": False,
            "tampon_DIRSOLDE": False,
            "tampon_DPB": False,
            "tampon_CF": False,
            "tampon_DP": False,
            "signature_cachet_ministre_bas": False,
            "details": "Vision désactivée en mode CPU. Vérification manuelle requise.",
            "erreur": "Vision non disponible sur CPU",
        }

    # Mode API HF
    images_b64 = extraire_pages_pdf(pdf_bytes)
    if not images_b64:
        return {"erreur": "Impossible d'extraire les images du PDF."}

    prompt_visuel = (
        "Analyse cette image d'un document administratif sénégalais. "
        "Vérifie : numéro d'acte en haut, tampons (DGFP, DIRSOLDE, DPB, CF), "
        "signature du ministre en bas. "
        "Réponds en JSON : {\"numero_acte_haut\": bool, \"tampon_DGFP\": bool, "
        "\"tampon_DIRSOLDE\": bool, \"tampon_DPB\": bool, \"tampon_CF\": bool, "
        "\"tampon_DP\": bool, \"signature_cachet_ministre_bas\": bool, "
        "\"details\": \"...\"}"
    )

    headers = {
        "Authorization": f"Bearer {settings.hf_token}",
    }

    # Envoi de la première image
    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                settings.hf_api_vision_url,
                headers=headers,
                data={"inputs": prompt_visuel},
                files={"image": base64.b64decode(images_b64[0])},
            )
            response.raise_for_status()
            result = response.json()

            # Tenter de parser le JSON
            texte = str(result)
            match = re.search(r'\{.*\}', texte, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass

            return {
                "numero_acte_haut": False,
                "tampon_DGFP": False,
                "tampon_DIRSOLDE": False,
                "tampon_DPB": False,
                "tampon_CF": False,
                "tampon_DP": False,
                "signature_cachet_ministre_bas": False,
                "details": texte[:500],
            }

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 503:
            logger.warning("Modèle vision HF en cold start...")
            return {
                "erreur": "Modèle vision en chargement, réessayez dans 30s",
                "numero_acte_haut": False,
                "tampon_DGFP": False,
                "tampon_DIRSOLDE": False,
                "tampon_DPB": False,
                "tampon_CF": False,
                "tampon_DP": False,
                "signature_cachet_ministre_bas": False,
                "details": "Cold start du modèle vision",
            }
        logger.error(f"Erreur API vision : {e}")
        return {"erreur": str(e)}

    except Exception as e:
        logger.error(f"Erreur vision : {e}")
        return {"erreur": str(e)}