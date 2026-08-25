"""API RAG RH — Version CPU."""

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.models import RAGQuestionRequest, RAGQuestionResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(" Démarrage API RAG RH (mode CPU)...")

    from app.services.rag_service import initialiser_rag
    initialiser_rag()

    os.makedirs(get_settings().upload_dir, exist_ok=True)
    os.makedirs(get_settings().logs_dir, exist_ok=True)

    mode = get_settings().llm_mode
    logger.info(f" API prête ! Mode LLM : {mode}")
    yield


app = FastAPI(
    title="API RAG RH — Fonction Publique Sénégal (CPU)",
    version="2.0.0-cpu",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check():
    settings = get_settings()
    return {
        "status": "healthy",
        "service": "RAG RH API (CPU)",
        "llm_mode": settings.llm_mode,
        "vision_disponible": settings.llm_mode == "api_hf",
    }


@app.post("/rag/question", response_model=RAGQuestionResponse)
def poser_question_rag(req: RAGQuestionRequest):
    from app.services.rag_service import generer_reponse_rag
    try:
        return generer_reponse_rag(req.question, top_k=req.top_k)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/workflow/init")
def initialiser_workflow_api(acte_id: str = Form(...), acte_text: str = Form(...)):
    from app.services.workflow_service import get_moteur
    moteur = get_moteur(acte_id)
    return moteur.initialiser_workflow(acte_text)


async def _traiter_une_verification(
    etape: int, acte_id: str, acte_text: str, fichier: UploadFile, agent_info: str,
) -> dict:
    """Logique de vérification pour UN acte, réutilisée par l'endpoint
    unitaire (/workflow/valider) et l'endpoint en masse
    (/workflow/valider-masse). Chaque acte_id a son propre espace isolé
    (voir workflow_service.get_moteur), donc cette fonction peut être
    appelée pour plusieurs actes différents sans risque de mélange."""
    import json as _json
    from app.services.workflow_service import get_moteur
    from app.services.extraction_service import extraire_texte_fichier

    moteur = get_moteur(acte_id)

    # Fichier accepté : PDF ou Word (.docx/.doc). La vision (tampons/
    # signature) n'est possible que sur un PDF — un Word n'a pas de rendu
    # visuel exploitable.
    est_pdf = bool(fichier and fichier.filename and fichier.filename.lower().endswith(".pdf"))

    if moteur.workflow_actuel is None:
        if acte_text:
            moteur.initialiser_workflow(acte_text)
        elif fichier:
            contenu = await fichier.read()
            acte_text = extraire_texte_fichier(contenu, fichier.filename)
            moteur.initialiser_workflow(acte_text)
        else:
            raise HTTPException(400, "Fournissez acte_text ou fichier")

    fichier_bytes = None
    if fichier:
        fichier_bytes = await fichier.read()

    if not acte_text and fichier_bytes:
        acte_text = extraire_texte_fichier(fichier_bytes, fichier.filename or "acte")

    # La vision n'est transmise que si c'est vraiment un PDF, sinon le
    # moteur ignorera simplement les vérifications visuelles pour cette
    # étape (les vérifications textuelles/identité restent actives).
    pdf_bytes = fichier_bytes if est_pdf else None

    agents_externes = None
    if agent_info:
        try:
            parsed = _json.loads(agent_info)
            agents_externes = parsed if isinstance(parsed, list) else [parsed]
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"agent_info n'est pas un JSON valide pour l'acte {acte_id} : {e}")

    return moteur.valider_etape(acte_text, pdf_bytes, etape, acte_id, agents_externes)


@app.post("/workflow/valider")
async def valider_etape_api(
    etape: int = Form(...),
    acte_id: str = Form(...),
    acte_text: str = Form(None),
    fichier: UploadFile = File(None),
    agent_info: str = Form(
        None,
        description=(
            "JSON (fourni par GIRAFE) décrivant le/les agent(s) concerné(s) "
            "par cet acte — objet unique ou liste d'objets, avec les champs : "
            "matricule, nom, prenom, date_naissance, corps, grade, hierarchie. "
            "Si absent, l'API se rabat sur sa base de test locale (usage "
            "développement uniquement)."
        ),
    ),
):
    try:
        return await _traiter_une_verification(etape, acte_id, acte_text, fichier, agent_info)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/workflow/valider-masse")
async def valider_etape_masse_api(
    etape: int = Form(
        ...,
        description="Numéro d'étape à vérifier — LE MÊME pour tous les actes de ce lot (un seul profil traite le lot).",
    ),
    acte_ids: list[str] = Form(
        ...,
        description="Un identifiant d'acte par fichier, dans le même ordre que 'fichiers'.",
    ),
    fichiers: list[UploadFile] = File(
        ...,
        description="Les fichiers PDF/Word des actes à vérifier, un par acte, dans le même ordre que 'acte_ids'.",
    ),
    agents_info: list[str] = Form(
        None,
        description=(
            "Un JSON agent_info par acte, dans le même ordre que 'fichiers'. "
            "Optionnel : mettez une chaîne vide '' pour un acte donné si aucune "
            "info agent n'est disponible pour lui précisément."
        ),
    ),
):
    """Vérifie PLUSIEURS actes en une seule requête, pour la même étape/le
    même profil — utile pour un traitement en masse (ex: un agent GIRAFE
    reçoit 20 actes à valider pour 'Dir. Solde' d'un coup).

    Chaque acte reste isolé des autres (voir /workflow/valider) : si l'un
    des fichiers échoue, les autres continuent d'être traités normalement —
    l'échec est simplement rapporté dans le résultat de CET acte précis.
    """
    nb = len(fichiers)
    if len(acte_ids) != nb:
        raise HTTPException(400, f"{nb} fichier(s) mais {len(acte_ids)} acte_id(s) — il en faut autant des deux côtés.")
    if agents_info and len(agents_info) not in (0, nb):
        raise HTTPException(400, f"{nb} fichier(s) mais {len(agents_info)} agent_info(s) — il en faut autant des deux côtés, ou aucun.")

    resultats = []
    nb_valides = 0
    nb_rejetes = 0
    nb_erreurs = 0

    for i in range(nb):
        acte_id = acte_ids[i]
        fichier = fichiers[i]
        agent_info = agents_info[i] if agents_info and agents_info[i] else None

        try:
            resultat = await _traiter_une_verification(etape, acte_id, None, fichier, agent_info)
            resultats.append(resultat)
            if resultat.get("statut") == "Validé":
                nb_valides += 1
            else:
                nb_rejetes += 1
        except HTTPException as e:
            nb_erreurs += 1
            resultats.append({
                "acte_id": acte_id, "etape": etape, "statut": "Erreur",
                "message_verdict": f" Erreur de traitement pour l'acte « {acte_id} » : {e.detail}",
                "erreur": e.detail,
            })
        except Exception as e:
            nb_erreurs += 1
            resultats.append({
                "acte_id": acte_id, "etape": etape, "statut": "Erreur",
                "message_verdict": f" Erreur inattendue pour l'acte « {acte_id} » : {e}",
                "erreur": str(e),
            })

    return {
        "nb_actes_traites": nb,
        "nb_valides": nb_valides,
        "nb_rejetes": nb_rejetes,
        "nb_erreurs": nb_erreurs,
        "resultats": resultats,
    }


@app.post("/workflow/cloture")
async def cloture_workflow_api(
    acte_id: str = Form(...),
    fichier: UploadFile = File(...),
):
    from app.services.workflow_service import get_moteur
    moteur = get_moteur(acte_id)
    pdf_bytes = await fichier.read()
    try:
        return moteur.verification_finale(pdf_bytes, acte_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/workflow/reset")
def reset_workflow(acte_id: str = Form(...)):
    from app.services.workflow_service import reset_moteur
    reset_moteur(acte_id)
    return {"message": f"Workflow réinitialisé pour l'acte {acte_id}"}


@app.post("/vision/analyser")
async def analyser_vision_api(fichier: UploadFile = File(...)):
    from app.services.vision_service import analyser_visuel_acte

    if not fichier.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Seuls les PDF sont acceptés")

    pdf_bytes = await fichier.read()
    try:
        return analyser_visuel_acte(pdf_bytes)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/extraire/texte")
async def extraire_texte_api(fichier: UploadFile = File(...)):
    from app.services.extraction_service import extraire_texte_fichier
    contenu = await fichier.read()
    texte = extraire_texte_fichier(contenu, fichier.filename)
    return {"nom_fichier": fichier.filename, "nb_caracteres": len(texte), "texte": texte}


@app.post("/regles/verifier")
def verifier_regles_api(acte_text: str = Form(...)):
    from app.services.regles_service import verifier_points_abc
    return verifier_points_abc(acte_text)


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.api_host, port=settings.api_port)