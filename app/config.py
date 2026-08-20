"""Configuration centralisée — mode CPU / API HF."""

import os
from pathlib import Path
from functools import lru_cache
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    hf_token: str = os.getenv("HF_TOKEN", "")

    # Mode : "api_hf" | "cpu_local" | "llama_cpp"
    llm_mode: str = os.getenv("LLM_MODE", "api_hf")

    # API Hugging Face Inference
    hf_api_llm_url: str = os.getenv(
        "HF_API_LLM_URL",
        "https://api-inference.huggingface.co/models/Qwen/Qwen2.5-14B-Instruct"
    )
    hf_api_vision_url: str = os.getenv(
        "HF_API_VISION_URL",
        "https://api-inference.huggingface.co/models/Qwen/Qwen2.5-VL-7B-Instruct"
    )

    # Gemini (vision multimodale — tampons, signature, cachet)
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_vision_model: str = os.getenv("GEMINI_VISION_MODEL", "gemini-3.6-flash")

    # Backend vision : "gemini" (externe, rapide) ou "local" (Moondream2, open source)
    vision_backend: str = os.getenv("VISION_BACKEND", "gemini")

    # Modèle de vision local (Moondream2 via llama-server) — 100% open source
    local_vlm_url: str = os.getenv("LOCAL_VLM_URL", "http://127.0.0.1:8081/v1/chat/completions")

    # Modèles locaux (si mode cpu_local)
    model_llm_local: str = os.getenv("MODEL_LLM_LOCAL", "Qwen/Qwen2.5-3B-Instruct")
    model_vision_local: str = ""  # Désactivé en CPU

    # Embeddings (toujours local CPU)
    model_embed: str = os.getenv(
        "MODEL_EMBED",
        "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
    )
    model_reranker: str = os.getenv(
        "MODEL_RERANKER",
        "cross-encoder/ms-marco-MiniLM-L-6-v2"
    )

    # Chemins
    data_dir: str = os.getenv("DATA_DIR", str(BASE_DIR / "app" / "data"))
    upload_dir: str = os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads"))
    logs_dir: str = os.getenv("LOGS_DIR", str(BASE_DIR / "logs"))

    # API
    api_host: str = os.getenv("API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("API_PORT", "8000"))

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()