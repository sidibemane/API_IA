"""
Service LLM — 3 modes :
  1. api_hf     → Appelle l'API Hugging Face Inference (recommandé CPU)
  2. cpu_local  → Charge un petit modèle local (Qwen2.5-3B)
  3. llama_cpp  → Utilise llama.cpp avec un GGUF quantifié
"""

import json
import logging
import httpx
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

# Cache pour le mode local
_MODELE_LOCAL = None
_TOKENIZER_LOCAL = None


# ═══════════════════════════════════════════════════════════
#  MODE 1 : API HUGGING FACE INFERENCE (recommandé)
# ═══════════════════════════════════════════════════════════

def _generer_via_api_hf(
    prompt: str,
    max_tokens: int = 512,
    temperature: float = 0.01,
) -> str:
    """Appelle l'API Hugging Face Inference (chat completions) pour la génération.

    Utilise le endpoint "router" compatible OpenAI, seul format fiable
    aujourd'hui pour un modèle instruct/chat comme Qwen2.5-14B-Instruct.
    L'ancien format text-generation legacy ({"inputs": prompt}) envoyé
    directement au modèle est déprécié pour ce type de modèle et pouvait
    renvoyer une erreur ou un texte non exploitable.
    """
    settings = get_settings()

    headers = {
        "Authorization": f"Bearer {settings.hf_token}",
        "Content-Type": "application/json",
    }

    # Le nom du modèle est déduit de l'URL configurée (HF_API_LLM_URL),
    # pour rester compatible avec la configuration existante (.env).
    modele = settings.hf_api_llm_url.rstrip("/").split("/models/")[-1]

    payload = {
        "model": modele,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": max(temperature, 0.01),
    }

    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                "https://router.huggingface.co/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"].strip()

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 503:
            # Modèle en cours de chargement (cold start)
            logger.warning("Modèle HF en cold start, retry dans 20s...")
            import time
            time.sleep(20)
            return _generer_via_api_hf(prompt, max_tokens, temperature)
        logger.error(f"Erreur API HF : {e.response.status_code} — {e.response.text[:300]}")
        raise
    except Exception as e:
        logger.error(f"Erreur appel API HF : {e}")
        raise


# ═══════════════════════════════════════════════════════════
#  MODE 2 : CPU LOCAL (petit modèle Qwen2.5-3B)
# ═══════════════════════════════════════════════════════════

def _charger_modele_local():
    """Charge un petit modèle sur CPU."""
    global _MODELE_LOCAL, _TOKENIZER_LOCAL

    if _MODELE_LOCAL is not None:
        return _MODELE_LOCAL, _TOKENIZER_LOCAL

    from transformers import AutoTokenizer, AutoModelForCausalLM
    import torch

    settings = get_settings()
    logger.info(f"Chargement modèle local CPU : {settings.model_llm_local}")

    _TOKENIZER_LOCAL = AutoTokenizer.from_pretrained(
        settings.model_llm_local,
        trust_remote_code=True,
    )

    _MODELE_LOCAL = AutoModelForCausalLM.from_pretrained(
        settings.model_llm_local,
        torch_dtype=torch.float32,  # CPU = float32
        device_map="cpu",
        low_cpu_mem_usage=True,
        trust_remote_code=True,
    )
    _MODELE_LOCAL.eval()

    logger.info("✅ Modèle local CPU chargé")
    return _MODELE_LOCAL, _TOKENIZER_LOCAL


def _generer_via_local(
    prompt: str,
    max_tokens: int = 512,
    temperature: float = 0.01,
) -> str:
    """Génère via un modèle local sur CPU."""
    import torch

    modele, tokenizer = _charger_modele_local()

    entrees = tokenizer(
        prompt,
        return_tensors="pt",
        truncation=True,
        max_length=2048,
    )

    with torch.no_grad():
        sorties = modele.generate(
            **entrees,
            max_new_tokens=max_tokens,
            temperature=max(temperature, 0.01),
            do_sample=temperature > 0,
            pad_token_id=tokenizer.eos_token_id,
        )

    return tokenizer.decode(
        sorties[0][entrees["input_ids"].shape[1]:],
        skip_special_tokens=True,
    ).strip()


# ═══════════════════════════════════════════════════════════
#  FONCTION PRINCIPALE — route automatiquement
# ═══════════════════════════════════════════════════════════

def generer_texte(
    prompt: str,
    max_tokens: int = 512,
    temperature: float = 0.01,
) -> str:
    """
    Génère une réponse texte. Route automatiquement selon LLM_MODE :
      - api_hf    → API Hugging Face (rapide, gratuit avec limites)
      - cpu_local → Petit modèle local (lent mais autonome)
    """
    settings = get_settings()

    if settings.llm_mode == "api_hf":
        return _generer_via_api_hf(prompt, max_tokens, temperature)
    elif settings.llm_mode == "cpu_local":
        return _generer_via_local(prompt, max_tokens, temperature)
    else:
        raise ValueError(f"Mode LLM inconnu : {settings.llm_mode}")


def verification_semantique(acte_text: str) -> dict:
    """Vérification sémantique d'un acte (adaptée CPU)."""
    prompt = (
        "Tu es un juriste expert en actes administratifs de la Fonction Publique du Sénégal.\n"
        "Analyse UNIQUEMENT la cohérence sémantique interne du texte ci-dessous.\n\n"
        "Réponds STRICTEMENT sous ce format, une ligne par point :\n"
        "COHERENCE_OBJET: CONFORME ou NON CONFORME\n"
        "CONTRADICTIONS: AUCUNE ou <description courte>\n"
        "VISAS: CONFORME ou A_VERIFIER\n\n"
        f"Texte de l'acte :\n{acte_text[:2000]}\n"
    )

    try:
        reponse = generer_texte(prompt, max_tokens=150, temperature=0.01)
        resultats = {}
        for ligne in reponse.split("\n"):
            if ":" in ligne:
                cle, _, valeur = ligne.partition(":")
                resultats[cle.strip().upper()] = valeur.strip()
        return {
            "coherence_objet": resultats.get("COHERENCE_OBJET", "INCONNU"),
            "contradictions": resultats.get("CONTRADICTIONS", "AUCUNE"),
            "visas": resultats.get("VISAS", "INCONNU"),
            "reponse_brute": reponse,
        }
    except Exception as e:
        logger.error(f"Erreur vérification sémantique : {e}")
        return {
            "coherence_objet": "INCONNU",
            "contradictions": "ERREUR",
            "visas": "INCONNU",
            "erreur": str(e),
        }