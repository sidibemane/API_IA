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
def initialiser_workflow_api(acte_text: str = Form(...)):
    from app.services.workflow_service import get_moteur
    moteur = get_moteur()
    return moteur.initialiser_workflow(acte_text)


@app.post("/workflow/valider")
async def valider_etape_api(
    etape: int = Form(...),
    acte_id: str = Form(...),
    acte_text: str = Form(None),
    fichier: UploadFile = File(None),
):
    from app.services.workflow_service import get_moteur
    from app.services.extraction_service import extraire_texte_fichier

    moteur = get_moteur()

    if moteur.workflow_actuel is None:
        if acte_text:
            moteur.initialiser_workflow(acte_text)
        elif fichier:
            contenu = await fichier.read()
            acte_text = extraire_texte_fichier(contenu, fichier.filename)
            moteur.initialiser_workflow(acte_text)
        else:
            raise HTTPException(400, "Fournissez acte_text ou fichier")

    pdf_bytes = None
    if fichier:
        pdf_bytes = await fichier.read()

    if not acte_text and pdf_bytes:
        acte_text = extraire_texte_fichier(pdf_bytes, fichier.filename or "acte.pdf")

    try:
        return moteur.valider_etape(acte_text, pdf_bytes, etape, acte_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/workflow/cloture")
async def cloture_workflow_api(
    acte_id: str = Form(...),
    fichier: UploadFile = File(...),
):
    from app.services.workflow_service import get_moteur
    moteur = get_moteur()
    pdf_bytes = await fichier.read()
    try:
        return moteur.verification_finale(pdf_bytes, acte_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/workflow/reset")
def reset_workflow():
    from app.services.workflow_service import reset_moteur
    reset_moteur()
    return {"message": "Workflow réinitialisé"}


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