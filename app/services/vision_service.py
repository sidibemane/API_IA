"""
Service Vision — 100% open source, auto-hébergé, sans API externe.

Utilise Moondream2 (licence Apache 2.0, ~1 milliard de paramètres, conçu
pour tourner efficacement sur CPU) servi localement via llama-server
(llama.cpp), qui expose une API HTTP compatible OpenAI sur le serveur
lui-même — aucune connexion internet requise après le téléchargement
initial du modèle, aucun coût, aucune limite de requêtes.

Prérequis sur le serveur (voir instructions de déploiement) :
  1. llama.cpp installé (binaire llama-server)
  2. Le service llama-server tourne en arrière-plan (idéalement via
     systemd), servant le modèle sur LOCAL_VLM_URL (par défaut
     http://127.0.0.1:8081)

Ce fichier ne fait AUCUN appel réseau externe — tout reste sur la machine.
"""

import base64
import json
import logging
import re

import httpx

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

PROMPT_VISUEL = (
    "Voici une page d'un document administratif de la Fonction Publique du "
    "Sénégal.\n\n"
    "IMPORTANT sur la forme des tampons : sur ces documents, les tampons "
    "DGFP, DIRSOLDE, DPB, CF, DP sont dessinés comme de GRANDES LETTRES "
    "FORMÉES DE POINTILLÉS (un contour en pointillés dessinant chaque "
    "lettre du nom de la direction). Ce style en pointillés EST la façon "
    "normale dont un tampon apposé apparaît sur ces documents — ce n'est "
    "PAS un espace vide à ignorer.\n\n"
    "Réponds UNIQUEMENT avec un objet JSON, sans aucun texte avant ou "
    "après, au format exact :\n"
    '{"numero_acte_haut": bool, "tampon_DGFP": bool, "tampon_DIRSOLDE": bool, '
    '"tampon_DPB": bool, "tampon_CF": bool, "tampon_DP": bool, '
    '"signature_cachet_ministre_bas": bool, "details": "..."}\n\n'
    "numero_acte_haut : true seulement si un vrai numéro (des chiffres) "
    "apparaît en haut de page — false si tu vois seulement 'N° …' avec des "
    "points de suspension.\n"
    "tampon_XXX : true dès que tu distingues les lettres en pointillés "
    "formant ce nom de direction, peu importe leur netteté.\n"
    "signature_cachet_ministre_bas : true si une signature manuscrite et/ou "
    "un cachet rond sont visibles en bas de page.\n"
    "details : précise brièvement ce que tu as trouvé et où."
)


def extraire_pages_pdf(pdf_bytes: bytes) -> list[str]:
    """Extrait la 1ère et dernière page en base64 (PNG), haute résolution
    pour que les tampons en pointillés (fins) restent lisibles."""
    import fitz

    images_b64 = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    if len(doc) >= 1:
        pix1 = doc[0].get_pixmap(dpi=200)
        images_b64.append(base64.b64encode(pix1.tobytes("png")).decode("utf-8"))

    if len(doc) >= 2:
        pix_last = doc[-1].get_pixmap(dpi=200)
        images_b64.append(base64.b64encode(pix_last.tobytes("png")).decode("utf-8"))

    doc.close()
    return images_b64


def analyser_visuel_acte(pdf_bytes: bytes) -> dict:
    """Analyse le PDF via le modèle de vision local (Moondream2 / llama-server)."""
    settings = get_settings()

    images_b64 = extraire_pages_pdf(pdf_bytes)
    if not images_b64:
        return {**REPONSE_PAR_DEFAUT, "erreur": "Impossible d'extraire les images du PDF."}

    # On analyse la première page (numéro) — pour la signature/cachet en fin
    # de document, on analyse aussi la dernière page si elle est différente.
    resultat_global = dict(REPONSE_PAR_DEFAUT)
    details_par_page = []

    for i, img_b64 in enumerate(images_b64):
        resultat_page = _analyser_une_page(settings, img_b64)
        if "erreur" in resultat_page:
            return resultat_page  # erreur réseau/serveur : on arrête là

        # Fusion : un booléen devient True dès qu'une page le confirme
        for cle in REPONSE_PAR_DEFAUT:
            if cle == "details":
                continue
            if resultat_page.get(cle):
                resultat_global[cle] = True

        details_par_page.append(f"Page {i + 1}: {resultat_page.get('details', '')}")

    resultat_global["details"] = " | ".join(details_par_page)
    logger.info(f"Résultat analyse visuelle (Moondream2 local) : {resultat_global}")
    return resultat_global


def _analyser_une_page(settings, img_b64: str) -> dict:
    payload = {
        "model": "moondream2",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT_VISUEL},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                ],
            }
        ],
        "max_tokens": 400,
        "temperature": 0.0,
    }

    try:
        with httpx.Client(timeout=180.0) as client:
            response = client.post(settings.local_vlm_url, json=payload)
            response.raise_for_status()
            result = response.json()

            texte = result["choices"][0]["message"]["content"]
            match = re.search(r'\{.*\}', texte, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass

            logger.warning(f"Réponse Moondream2 non-JSON, contenu brut : {texte[:300]}")
            return {**REPONSE_PAR_DEFAUT, "details": texte[:500]}

    except httpx.ConnectError as e:
        logger.error(
            f"Impossible de joindre le serveur de vision local ({settings.local_vlm_url}). "
            f"Vérifie que llama-server tourne bien : {e}"
        )
        return {**REPONSE_PAR_DEFAUT, "erreur": "Serveur de vision local injoignable — vérifie que llama-server tourne."}

    except Exception as e:
        logger.error(f"Erreur vision (Moondream2 local) : {e}")
        return {**REPONSE_PAR_DEFAUT, "erreur": str(e)}