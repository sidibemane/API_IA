"""
Service Vision — deux backends possibles, choisis via VISION_BACKEND dans .env :

  - VISION_BACKEND=gemini (par défaut) : utilise l'API Gemini (Google).
    Fiable et rapide, mais externe et soumis à un quota gratuit limité.

  - VISION_BACKEND=local : utilise Moondream2 auto-hébergé via llama-server
    (100% open source, gratuit, aucune limite). Stratégie : une question
    simple OUI/NON à la fois par élément à vérifier (plutôt qu'un JSON à 8
    champs d'un coup, que ce petit modèle ne suit pas bien), en réutilisant
    le cache du serveur pour ne payer le coût d'analyse de l'image qu'une
    seule fois par page.

Constat important (validé sur les vrais documents) : les tampons DGFP,
DIRSOLDE, DPB, CF, DP sont dessinés en pointillés (contours vectoriels
formant les lettres), PAS comme du texte extractible ni comme une image
incrustée classique — d'où le besoin d'une vraie analyse visuelle.
"""

import base64
import json
import logging
import re
import time

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
    """Point d'entrée : redirige vers Gemini ou le modèle local selon .env"""
    settings = get_settings()
    backend = getattr(settings, "vision_backend", "gemini")

    if backend == "local":
        return _analyser_via_local(pdf_bytes, settings)
    return _analyser_via_gemini(pdf_bytes, settings)


# ═══════════════════════════════════════════════════════════
#  BACKEND LOCAL (Moondream2 / llama-server) — question par question
# ═══════════════════════════════════════════════════════════

QUESTIONS_LOCAL = [
    ("tampon_DGFP", "Vois-tu les lettres en pointillés 'DGFP' quelque part sur cette image ?"),
    ("tampon_DIRSOLDE", "Vois-tu les lettres en pointillés 'DIRSOLDE' quelque part sur cette image ?"),
    ("tampon_DPB", "Vois-tu les lettres en pointillés 'DPB' quelque part sur cette image ?"),
    ("tampon_CF", "Vois-tu les lettres en pointillés 'CF' quelque part sur cette image ?"),
    ("tampon_DP", "Vois-tu les lettres en pointillés 'DP' (seulement DP, pas DPB) quelque part sur cette image ?"),
    ("numero_acte_haut", "En haut de l'image, vois-tu un numéro avec de vrais chiffres (pas juste des points de suspension) ?"),
    ("signature_cachet_ministre_bas", "En bas de l'image, vois-tu une signature manuscrite ou un cachet rond ?"),
]

PREFIXE_LOCAL = "<__media__>\nDocument administratif sénégalais.\n"


def _analyser_via_local(pdf_bytes: bytes, settings) -> dict:
    images_b64 = extraire_pages_pdf(pdf_bytes)
    if not images_b64:
        return {**REPONSE_PAR_DEFAUT, "erreur": "Impossible d'extraire les images du PDF."}

    resultat = dict(REPONSE_PAR_DEFAUT)
    details = []
    url = settings.local_vlm_url.replace("/v1/chat/completions", "/completion")

    # Page 1 : tampons + numéro
    t0 = time.time()
    for cle, question in QUESTIONS_LOCAL:
        if cle == "signature_cachet_ministre_bas" and len(images_b64) > 1:
            continue  # on la posera sur la dernière page si elle existe
        reponse, erreur = _poser_question_locale(url, images_b64[0], question)
        if erreur:
            return {**REPONSE_PAR_DEFAUT, "erreur": erreur}
        resultat[cle] = reponse
        details.append(f"{cle}={reponse}")

    # Dernière page (si différente) : signature/cachet
    if len(images_b64) > 1:
        cle, question = "signature_cachet_ministre_bas", QUESTIONS_LOCAL[-1][1]
        reponse, erreur = _poser_question_locale(url, images_b64[-1], question)
        if erreur:
            return {**REPONSE_PAR_DEFAUT, "erreur": erreur}
        resultat[cle] = reponse
        details.append(f"{cle}={reponse}")

    duree = time.time() - t0
    resultat["details"] = f"Analyse locale (Moondream2), {duree:.1f}s — " + ", ".join(details)
    logger.info(f"Résultat analyse visuelle (local) : {resultat}")
    return resultat


def _poser_question_locale(url: str, img_b64: str, question: str) -> tuple:
    """Pose une question OUI/NON simple. Retourne (bool_reponse, erreur_ou_None)."""
    prompt = PREFIXE_LOCAL + question + "\nRéponds uniquement par OUI ou NON.\nRéponse:"
    payload = {
        "prompt": prompt,
        "multimodal_data": [img_b64],
        "n_predict": 6,
        "temperature": 0.0,
        "cache_prompt": True,
    }
    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            result = response.json()
            texte = result.get("content", "").strip().upper()
            cache_n = result.get("tokens_cached", "?")
            logger.info(f"  Q: {question[:40]}... -> '{texte}' (cache={cache_n})")
            return ("OUI" in texte or "YES" in texte), None
    except httpx.HTTPStatusError as e:
        logger.error(f"Erreur HTTP llama-server : {e.response.status_code} — {e.response.text}")
        return False, f"{e.response.status_code}: {e.response.text[:200]}"
    except httpx.ConnectError as e:
        logger.error(f"Serveur de vision local injoignable : {e}")
        return False, "Serveur de vision local injoignable — vérifie que llama-server tourne."
    except Exception as e:
        logger.error(f"Erreur vision locale : {e}")
        return False, str(e)


# ═══════════════════════════════════════════════════════════
#  BACKEND GEMINI (solution de secours, externe)
# ═══════════════════════════════════════════════════════════

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
    "required": list(REPONSE_PAR_DEFAUT.keys()),
}


def _analyser_via_gemini(pdf_bytes: bytes, settings) -> dict:
    if not settings.gemini_api_key:
        logger.error("GEMINI_API_KEY manquant dans .env — vision désactivée.")
        return {**REPONSE_PAR_DEFAUT, "erreur": "GEMINI_API_KEY manquant"}

    images_b64 = extraire_pages_pdf(pdf_bytes)
    if not images_b64:
        return {**REPONSE_PAR_DEFAUT, "erreur": "Impossible d'extraire les images du PDF."}

    nb_pages = len(images_b64)
    prompt_visuel = (
        f"Voici {nb_pages} page(s) d'un document administratif de la Fonction "
        "Publique du Sénégal. Examine TOUTES les pages fournies.\n\n"
        "IMPORTANT : les tampons DGFP, DIRSOLDE, DPB, CF, DP sont dessinés en "
        "GRANDES LETTRES POINTILLÉES — ce style compte comme un tampon présent.\n\n"
        "Cherche : le numéro d'acte en haut (numero_acte_haut, false si 'N° …' "
        "sans chiffres), les tampons DGFP/DIRSOLDE/DPB/CF/DP (lettres "
        "pointillées), et la signature/cachet du Ministre en bas de la "
        "dernière page. Dans 'details', précise où tu as trouvé chaque élément."
    )

    headers = {"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"}
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
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_vision_model}:generateContent"
    return _appeler_gemini_avec_retry(url, headers, payload)


def _appeler_gemini_avec_retry(url: str, headers: dict, payload: dict, tentative: int = 1) -> dict:
    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
            candidats = result.get("candidates", [])
            if not candidats:
                bloque = result.get("promptFeedback", {}).get("blockReason")
                return {**REPONSE_PAR_DEFAUT, "erreur": f"Réponse vide (blockReason={bloque})"}
            texte_json = candidats[0]["content"]["parts"][0]["text"]
            resultat = json.loads(texte_json)
            logger.info(f"Réponse Gemini vision : {resultat}")
            return resultat
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429 and tentative <= 3:
            corps = e.response.text
            match = re.search(r"retry in (\d+(?:\.\d+)?)s", corps)
            delai = float(match.group(1)) + 1 if match else 15.0
            logger.warning(f"Quota Gemini dépassé (429), tentative {tentative}/3 — attente {delai:.0f}s.")
            time.sleep(delai)
            return _appeler_gemini_avec_retry(url, headers, payload, tentative + 1)
        logger.error(f"Erreur API Gemini vision : {e.response.status_code} — {e.response.text}")
        return {**REPONSE_PAR_DEFAUT, "erreur": str(e)}
    except Exception as e:
        logger.error(f"Erreur vision : {e}")
        return {**REPONSE_PAR_DEFAUT, "erreur": str(e)}