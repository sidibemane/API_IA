from enum import Enum

class NiveauCriticite(Enum):
    BLOQUANT = "Bloquant"
    IMPORTANT = "Important"
    INFORMATION = "Information"

class StatutEtape(Enum):
    EN_ATTENTE = "En attente"
    EN_COURS = "En cours"
    VALIDE = "Validé"
    REJETE = "Rejeté"
    ANOMALIES = "Anomalies détectées"

class TypeActe(Enum):
    RETRAITE_FONCTIONNAIRE = "retraite_fonctionnaire"
    AVANCEMENT_ECHELON = "avancement_echelon"
    AUTRE = "autre"


"""Modèles Pydantic pour l'API RAG RH."""

from typing import List
from pydantic import BaseModel, Field


class RAGQuestionRequest(BaseModel):
    question: str
    top_k: int = 10


class ChunkUtilise(BaseModel):
    texte: str
    source: str
    score: float
    score_rerank: float = 0.0


class RAGQuestionResponse(BaseModel):
    question: str
    reponse: str
    chunks_utilises: List[ChunkUtilise] = Field(default_factory=list)
    temps_generation_ms: float