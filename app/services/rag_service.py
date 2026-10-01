"""
Service RAG — 100% CPU.
FAISS + SentenceTransformer + CrossEncoder tournent bien sur CPU.
Seule la génération finale utilise le LLM (via API ou local).
"""

import os
import time
import logging
import numpy as np
from sentence_transformers import SentenceTransformer, CrossEncoder
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import get_settings
from app.services.llm_service import generer_texte

logger = logging.getLogger(__name__)

_MODELE_EMBED = None
_RERANKER = None
_INDEX_FAISS = None
_CHUNKS = []
_TEXTES_CHUNKS = []
_SOURCES_CHUNKS = []

_FICHIERS_RAG = [
    ("regles_metier_actes_RH_v7.txt", "regles_metier"),
    ("corps_references_RAG.txt", "corps_references"),
]
_SIGNATURE_RAG = None


def _signature_rag():
    from app.services.referentiel_service import signature_fichiers
    return signature_fichiers([n for n, _ in _FICHIERS_RAG], get_settings().data_dir)


def initialiser_rag():
    """Initialise le pipeline RAG (tout en CPU)."""
    global _MODELE_EMBED, _RERANKER
    settings = get_settings()

    _construire_index_rag()

    # 5. Re-ranker (CPU)
    if _RERANKER is None:
        logger.info(f"Chargement re-ranker : {settings.model_reranker}")
        _RERANKER = CrossEncoder(settings.model_reranker)

    logger.info("✅ Pipeline RAG initialisé (CPU)")


def _construire_index_rag():
    """Découpe les textes et (re)calcule l'index FAISS. Les modèles déjà
    chargés sont réutilisés : appelée à nouveau automatiquement quand un
    texte est modifié depuis l'interface admin."""
    global _MODELE_EMBED, _INDEX_FAISS, _SIGNATURE_RAG
    global _CHUNKS, _TEXTES_CHUNKS, _SOURCES_CHUNKS

    settings = get_settings()
    data_dir = settings.data_dir
    signature = _signature_rag()

    # 1. Lecture des fichiers
    textes_bruts = []
    for nom_fichier, source in _FICHIERS_RAG:
        chemin = os.path.join(data_dir, nom_fichier)
        if os.path.exists(chemin):
            with open(chemin, "r", encoding="utf-8") as f:
                texte = f.read()
            textes_bruts.append({"texte": texte, "source": source})
            logger.info(f"  Lu : {source} ({len(texte):,} caractères)")

    # 2. Découpage en chunks
    splitter = RecursiveCharacterTextSplitter(
        separators=["\n====", "\nRÈGLE", "\nREGLE", "\n\n", "\n", ". ", " "],
        chunk_size=600,
        chunk_overlap=80,
        length_function=len,
    )

    chunks = []
    for item in textes_bruts:
        morceaux = splitter.split_text(item["texte"])
        for morceau in morceaux:
            if len(morceau.strip()) >= 50:
                chunks.append({
                    "texte": morceau.strip(),
                    "source": item["source"],
                    "index": len(chunks),
                })

    textes_chunks = [c["texte"] for c in chunks]
    sources_chunks = [c["source"] for c in chunks]
    logger.info(f"  Total chunks : {len(chunks)}")

    # 3. Embeddings (CPU — ~2-5 min la première fois)
    if _MODELE_EMBED is None:
        logger.info(f"Chargement embeddings : {settings.model_embed}")
        _MODELE_EMBED = SentenceTransformer(settings.model_embed)

    logger.info(f"Calcul embeddings pour {len(textes_chunks)} chunks...")
    embeddings = _MODELE_EMBED.encode(
        textes_chunks,
        batch_size=16,       # Plus petit batch pour CPU
        show_progress_bar=True,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )

    # 4. Index FAISS (CPU)
    import faiss
    dimension = embeddings.shape[1]
    index = faiss.IndexFlatIP(dimension)
    index.add(embeddings.astype("float32"))
    logger.info(f"  Index FAISS : {index.ntotal} vecteurs")

    # Publication d'un coup (une recherche en cours garde l'ancien index)
    _CHUNKS, _TEXTES_CHUNKS, _SOURCES_CHUNKS, _INDEX_FAISS = chunks, textes_chunks, sources_chunks, index
    _SIGNATURE_RAG = signature


def rechercher_chunks(question: str, top_k: int = 10) -> list[dict]:
    """Recherche FAISS + CrossEncoder (CPU)."""
    if _INDEX_FAISS is None or _RERANKER is None:
        initialiser_rag()
    elif _signature_rag() != _SIGNATURE_RAG:
        # Un texte (règles métier / références) a été modifié depuis
        # l'interface admin : on recalcule l'index avant de répondre.
        logger.info("🔄 Textes du RAG modifiés — recalcul de l'index...")
        _construire_index_rag()

    recall_k = min(top_k * 3, len(_TEXTES_CHUNKS))
    vecteur_question = _MODELE_EMBED.encode(
        [question], normalize_embeddings=True
    ).astype("float32")

    scores, indices = _INDEX_FAISS.search(vecteur_question, recall_k)

    candidats = []
    for score, idx in zip(scores[0], indices[0]):
        if idx != -1 and float(score) >= 0.15:
            candidats.append({
                "texte": _TEXTES_CHUNKS[idx],
                "source": _SOURCES_CHUNKS[idx],
                "score": float(score),
                "idx": int(idx),
            })

    if not candidats:
        return []

    # Re-ranking (CPU — quelques secondes)
    paires = [(question, c["texte"]) for c in candidats]
    scores_rerank = _RERANKER.predict(paires)
    for c, s in zip(candidats, scores_rerank):
        c["score_rerank"] = float(s)

    return sorted(candidats, key=lambda x: x["score_rerank"], reverse=True)[:top_k]


def generer_reponse_rag(question: str, top_k: int = 10) -> dict:
    """Pipeline RAG complet (CPU + API pour la génération)."""
    start = time.time()

    chunks = rechercher_chunks(question, top_k=top_k)

    contexte_joint = "\n\n".join(
        f"[Source: {c['source']}]\n{c['texte']}" for c in chunks
    )

    prompt = f"""Tu es un expert juridique de la Fonction Publique du Sénégal.

RÈGLE ABSOLUE : Réponds UNIQUEMENT en te basant sur le CONTEXTE ci-dessous.
N'utilise JAMAIS tes connaissances générales.
Si l'information est absente, réponds : "Information non disponible dans le contexte fourni."
Cite la règle applicable (ex: RÈGLE AE-02) si elle apparaît dans le contexte.

CONTEXTE :
{contexte_joint[:3500]}

QUESTION : {question}

RÉPONSE :"""

    reponse = generer_texte(prompt, max_tokens=500)
    temps_ms = (time.time() - start) * 1000

    return {
        "question": question,
        "reponse": reponse,
        "chunks_utilises": [
            {
                "texte": c["texte"][:200],
                "source": c["source"],
                "score": c["score"],
                "score_rerank": c.get("score_rerank", 0),
            }
            for c in chunks[:5]
        ],
        "temps_generation_ms": round(temps_ms, 1),
    }