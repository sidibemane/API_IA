"""Service d'extraction de texte PDF/DOCX."""

import logging

logger = logging.getLogger(__name__)


def extraire_texte_pdf(pdf_bytes: bytes) -> str:
    """Extrait le texte d'un PDF depuis les bytes."""
    import fitz

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        texte = "\n".join(page.get_text("text") for page in doc)
        doc.close()
        return texte
    except Exception as e:
        logger.error(f"Erreur extraction PDF : {e}")
        return f"[ERREUR: {e}]"


def extraire_texte_docx(docx_bytes: bytes) -> str:
    """Extrait le texte d'un DOCX depuis les bytes."""
    import io
    from docx import Document

    try:
        doc = Document(io.BytesIO(docx_bytes))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as e:
        logger.error(f"Erreur extraction DOCX : {e}")
        return f"[ERREUR: {e}]"


def extraire_texte_fichier(fichier_bytes: bytes, nom_fichier: str) -> str:
    """Extrait le texte selon l'extension du fichier."""
    ext = nom_fichier.lower().split(".")[-1]
    if ext == "pdf":
        return extraire_texte_pdf(fichier_bytes)
    elif ext in ("docx", "doc"):
        return extraire_texte_docx(fichier_bytes)
    else:
        return fichier_bytes.decode("utf-8", errors="ignore")