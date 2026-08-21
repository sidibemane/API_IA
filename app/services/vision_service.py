"""
Service Vision — plusieurs backends possibles, choisis via VISION_BACKEND dans .env :

  - VISION_BACKEND=gemini (externe, rapide, quota gratuit limité)
  - VISION_BACKEND=local (Moondream2/Qwen2-VL-2B via llama-server — rapide
    mais peu fiable sur ce type de document, testé et abandonné)
  - VISION_BACKEND=local_transformers (Qwen2.5-VL-7B via la bibliothèque
    transformers, exactement comme sur Google Colab — 100% open source,
    aucun coût, mais lent sur CPU sans carte graphique. Le modèle est
    chargé UNE SEULE FOIS en mémoire au premier appel (~110s), puis reste
    prêt pour tous les appels suivants tant que l'API tourne.)
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
    """Extrait la 1ère et dernière page en base64 (PNG), haute résolution."""
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


def analyser_visuel_acte(pdf_bytes: bytes) -> dict:
    """Point d'entrée : redirige selon VISION_BACKEND dans .env"""
    settings = get_settings()
    backend = getattr(settings, "vision_backend", "gemini")

    if backend == "local_transformers":
        return _analyser_via_transformers(pdf_bytes)
    if backend == "local":
        return _analyser_via_local_llamacpp(pdf_bytes, settings)
    return _analyser_via_gemini(pdf_bytes, settings)


def _extraire_codes_depuis_texte(texte_complet: str) -> dict:
    """Cherche les codes de tampons/éléments dans une transcription libre."""
    import difflib

    resultat = dict(REPONSE_PAR_DEFAUT)
    texte_maj = texte_complet.upper()

    def contient_approximativement(code: str) -> bool:
        if code in texte_maj:
            return True
        mots = re.findall(r"[A-Z]{2,}", texte_maj)
        for mot in mots:
            if difflib.SequenceMatcher(None, mot, code).ratio() >= 0.75:
                return True
        return False

    resultat["tampon_DGFP"] = contient_approximativement("DGFP")
    resultat["tampon_DIRSOLDE"] = contient_approximativement("DIRSOLDE")
    resultat["tampon_DPB"] = contient_approximativement("DPB")
    resultat["tampon_CF"] = contient_approximativement("CF")
    resultat["tampon_DP"] = contient_approximativement("DP") and not resultat["tampon_DPB"]
    resultat["numero_acte_haut"] = bool(re.search(r"\bN[°o]?\s*\d{2,}", texte_maj))
    resultat["signature_cachet_ministre_bas"] = any(
        m in texte_maj for m in ["SIGNATURE MANUSCRITE", "CACHET ROND", "CACHET OFFICIEL", "PARAPHE"]
    )
    return resultat


PROMPT_TRANSCRIPTION = (
    "Décris précisément et en détail tout ce que tu vois d'écrit sur cette "
    "image : le numéro en haut de page (recopie-le exactement, ou dis "
    "'aucun numéro' si tu vois seulement des points de suspension), chaque "
    "tampon ou étiquette (même en pointillés — recopie les lettres que tu "
    "distingues).\n"
    "Attention : le texte imprimé standard mentionne toujours 'LE MINISTRE "
    "DE LA FONCTION PUBLIQUE...' — ce n'est PAS une signature, ignore ce "
    "texte pour cette question. Regarde uniquement tout en bas de la page : "
    "y a-t-il une marque manuscrite (écriture à la main, pas du texte "
    "imprimé) ou un cachet/tampon rond ? Si oui, écris explicitement "
    "'SIGNATURE MANUSCRITE PRESENTE' ou 'CACHET ROND PRESENT'. Si le bas de "
    "la page est vide ou ne contient que du texte imprimé, écris "
    "'RIEN EN BAS DE PAGE'."
)


# ═══════════════════════════════════════════════════════════
#  BACKEND TRANSFORMERS (Qwen2.5-VL-7B, comme sur Colab)
# ═══════════════════════════════════════════════════════════

_modele_transformers = None
_processeur_transformers = None


def _charger_modele_transformers():
    global _modele_transformers, _processeur_transformers
    if _modele_transformers is not None:
        return _modele_transformers, _processeur_transformers

    logger.info("⏳ Chargement de Qwen2.5-VL-7B en mémoire (première fois, ~110s)...")
    t0 = time.time()
    import torch
    from transformers import Qwen2_5_VLForConditionalGeneration, AutoProcessor

    _modele_transformers = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        "Qwen/Qwen2.5-VL-7B-Instruct",
        torch_dtype=torch.bfloat16,
        device_map="cpu",
        low_cpu_mem_usage=True,
    )
    _processeur_transformers = AutoProcessor.from_pretrained("Qwen/Qwen2.5-VL-7B-Instruct")
    logger.info(f"✅ Qwen2.5-VL-7B chargé en {time.time()-t0:.1f}s")
    return _modele_transformers, _processeur_transformers


def _analyser_via_transformers(pdf_bytes: bytes) -> dict:
    import fitz
    from qwen_vl_utils import process_vision_info

    try:
        model, processor = _charger_modele_transformers()
    except Exception as e:
        logger.error(f"Erreur chargement modèle transformers : {e}")
        return {**REPONSE_PAR_DEFAUT, "erreur": f"Erreur chargement modèle : {e}"}

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=100)
    chemin_image = "/tmp/_vision_page_tmp.png"
    pix.save(chemin_image)
    doc.close()

    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "image": chemin_image},
            {"type": "text", "text": PROMPT_TRANSCRIPTION},
        ],
    }]

    try:
        t0 = time.time()
        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        image_inputs, video_inputs = process_vision_info(messages)
        inputs = processor(text=[text], images=image_inputs, videos=video_inputs, padding=True, return_tensors="pt")

        generated_ids = model.generate(**inputs, max_new_tokens=150, do_sample=False)
        generated_ids_trimmed = [out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)]
        output_text = processor.batch_decode(generated_ids_trimmed, skip_special_tokens=True)
        texte = output_text[0].strip()
        duree = time.time() - t0

        logger.info(f"Transcription Qwen2.5-VL-7B ({duree:.1f}s) : {texte[:400]}")

        resultat = _extraire_codes_depuis_texte(texte)
        resultat["details"] = f"Analyse locale (Qwen2.5-VL-7B), {duree:.1f}s — {texte[:400]}"
        logger.info(f"Résultat analyse visuelle (transformers) : {resultat}")
        return resultat

    except Exception as e:
        logger.error(f"Erreur génération Qwen2.5-VL-7B : {e}")
        return {**REPONSE_PAR_DEFAUT, "erreur": str(e)}


# ═══════════════════════════════════════════════════════════
#  BACKEND LOCAL VIA LLAMA-SERVER (gardé pour référence/comparaison)
# ═══════════════════════════════════════════════════════════

def _analyser_via_local_llamacpp(pdf_bytes: bytes, settings) -> dict:
    images_b64 = extraire_pages_pdf(pdf_bytes)
    if not images_b64:
        return {**REPONSE_PAR_DEFAUT, "erreur": "Impossible d'extraire les images du PDF."}

    url = settings.local_vlm_url.replace("/v1/chat/completions", "/completion")
    prompt = "<__media__>\n" + PROMPT_TRANSCRIPTION + "\nDescription:"
    payload = {
        "prompt": prompt,
        "multimodal_data": [images_b64[0]],
        "n_predict": 200,
        "temperature": 0.2,
        "repeat_penalty": 1.3,
        "repeat_last_n": 64,
        "cache_prompt": True,
    }
    try:
        t0 = time.time()
        with httpx.Client(timeout=400.0) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            result = response.json()
            texte = result.get("content", "").strip()
            duree = time.time() - t0
            resultat = _extraire_codes_depuis_texte(texte)
            resultat["details"] = f"Analyse locale (llama-server), {duree:.1f}s — {texte[:400]}"
            logger.info(f"Résultat analyse visuelle (local) : {resultat}")
            return resultat
    except Exception as e:
        logger.error(f"Erreur vision locale : {e}")
        return {**REPONSE_PAR_DEFAUT, "erreur": str(e)}


# ═══════════════════════════════════════════════════════════
#  BACKEND GEMINI (externe, solution de secours)
# ═══════════════════════════════════════════════════════════

SCHEMA_ANALYSE_VISUELLE = {
    "type": "OBJECT",
    "properties": {k: {"type": "STRING" if k == "details" else "BOOLEAN"} for k in REPONSE_PAR_DEFAUT},
    "required": list(REPONSE_PAR_DEFAUT.keys()),
}


def _analyser_via_gemini(pdf_bytes: bytes, settings) -> dict:
    if not settings.gemini_api_key:
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
            time.sleep(delai)
            return _appeler_gemini_avec_retry(url, headers, payload, tentative + 1)
        logger.error(f"Erreur API Gemini vision : {e.response.status_code} — {e.response.text}")
        return {**REPONSE_PAR_DEFAUT, "erreur": str(e)}
    except Exception as e:
        logger.error(f"Erreur vision : {e}")
        return {**REPONSE_PAR_DEFAUT, "erreur": str(e)}