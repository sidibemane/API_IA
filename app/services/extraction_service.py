"""Service d'extraction de texte PDF/DOCX."""

import logging
import re

logger = logging.getLogger(__name__)

_RE_MATRICULE_MOT = re.compile(r'\A(\d{6,9}[A-Z])\Z')

MARQUEUR_TABLEAU_RECONSTRUIT = "[TABLEAU_AVANCEMENT_RECONSTRUIT_PAR_POSITION]"


def _detecter_et_reconstruire_tableau_avancement(doc) -> str:
    """Reconstruit, à partir de la position (x, y) réelle de chaque mot sur
    la page, une version PROPRE et non ambiguë des tableaux de progression
    grade/échelon à plusieurs agents (mise en page "Prénom(s) Nom /
    matricule de solde | Grade | Date d'effet | Grade | Date d'effet").

    POURQUOI : l'extraction de texte linéaire standard (page.get_text
    ("text")) lit les blocs dans un ordre qui peut mélanger les données de
    deux agents voisins dès qu'une cellule s'étale sur plusieurs lignes
    (ex: un agent bénéficiant d'un double avancement de grade dans la même
    ligne du tableau décale la lecture des lignes suivantes). Résultat
    observé en production : une partie des données d'un agent se retrouve
    attribuée à tort à son voisin ("Tableau de progression non
    exploitable" alors que le tableau est parfaitement lisible visuellement).

    Cette fonction regroupe les mots PAR COLONNE (bande de x, déduite de la
    position des en-têtes "Grade"/"Date" eux-mêmes — donc valable quelle
    que soit la largeur exacte du tableau) et PAR LIGNE DE TABLEAU (bande
    de y, ancrée sur la position de chaque matricule trouvé dans la
    colonne de gauche), ce qui reconstitue fidèlement quelle donnée
    appartient à quel agent, indépendamment de l'ordre dans lequel la
    bibliothèque d'extraction linéarise le texte.

    Ne s'applique que si un en-tête de tableau contenant au moins 2
    occurrences du mot "Grade" ET 2 occurrences du mot "Date" est détecté
    sur une page — sinon ne renvoie rien pour cette page (aucun impact sur
    les actes à un seul agent ou sans ce type de tableau)."""
    lignes_reconstruites = []
    for page in doc:
        try:
            mots = page.get_text("words")  # (x0, y0, x1, y1, texte, bloc, ligne, mot)
        except Exception:
            continue
        if not mots:
            continue

        mots_grade = [m for m in mots if m[4].strip().rstrip(":").lower() == "grade"]
        if len(mots_grade) < 2:
            continue
        y_entete = max(m[1] for m in mots_grade)
        grade_x = sorted(m[0] for m in mots_grade if abs(m[1] - y_entete) < 3)
        if len(grade_x) < 2:
            continue

        mots_date = [m for m in mots if m[4].strip().lower() == "date" and abs(m[1] - y_entete) < 3]
        date_x = sorted(m[0] for m in mots_date)
        if len(date_x) < 2:
            continue

        # Bornes de colonnes : 5 bandes (nom | grade1 | date1 | grade2 | date2),
        # déduites des positions x des en-têtes eux-mêmes.
        refs = [0.0, grade_x[0], date_x[0], grade_x[1], date_x[1]]
        bandes = []
        for i in range(5):
            gauche = -1e9 if i == 0 else (refs[i - 1] + refs[i]) / 2
            droite = 1e9 if i == 4 else (refs[i] + refs[i + 1]) / 2
            bandes.append((gauche, droite))

        # Bornes verticales du tableau : du bas de l'en-tête jusqu'au
        # prochain "Article" (fin du tableau), ou bas de page à défaut.
        y_bas_entete = y_entete + 5
        mots_apres_entete = [m for m in mots if m[1] > y_bas_entete]
        y_fin_tableau = 1e9
        for m in mots_apres_entete:
            if m[4].strip().lower().startswith("article"):
                y_fin_tableau = min(y_fin_tableau, m[1])
        corps_tableau = [m for m in mots_apres_entete if m[1] < y_fin_tableau]
        if not corps_tableau:
            continue

        # Ancrage des LIGNES du tableau sur la position de chaque matricule
        # trouvé dans la colonne de gauche (bande 0).
        matricules = sorted(
            (m for m in corps_tableau if bandes[0][0] <= m[0] < bandes[0][1] and _RE_MATRICULE_MOT.match(m[4].strip())),
            key=lambda m: m[1],
        )
        if not matricules:
            continue

        y_matricules = [m[1] for m in matricules]
        bornes_lignes = []
        for i, y in enumerate(y_matricules):
            haut = y_bas_entete if i == 0 else (y_matricules[i - 1] + y) / 2
            bas = y_fin_tableau if i == len(y_matricules) - 1 else (y + y_matricules[i + 1]) / 2
            bornes_lignes.append((haut, bas))

        for _matricule_mot, (haut, bas) in zip(matricules, bornes_lignes):
            mots_ligne = [m for m in corps_tableau if haut <= m[1] < bas]
            nom_mots = sorted((m for m in mots_ligne if bandes[0][0] <= m[0] < bandes[0][1]), key=lambda m: (m[1], m[0]))
            nom_txt = " ".join(m[4] for m in nom_mots)
            cellules_progression = []
            for gauche, droite in bandes[1:]:
                mots_bande = sorted((m for m in mots_ligne if gauche <= m[0] < droite), key=lambda m: (m[1], m[0]))
                cellules_progression.append(" ".join(m[4] for m in mots_bande))
            # Format "entrelacé" OU "en blocs" (grade grade date date) selon
            # le nombre d'étapes de la ligne — les deux sont gérés par
            # _extraire_paires_brutes côté agents_service.py.
            ligne_reconstruite = f"{nom_txt} " + " ".join(cellules_progression)
            lignes_reconstruites.append(" ".join(ligne_reconstruite.split()))

    if not lignes_reconstruites:
        return ""
    return "\n\n" + MARQUEUR_TABLEAU_RECONSTRUIT + "\n" + "\n".join(lignes_reconstruites)


def extraire_texte_pdf(pdf_bytes: bytes) -> str:
    """Extrait le texte d'un PDF depuis les bytes."""
    import fitz

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        texte = "\n".join(page.get_text("text") for page in doc)
        try:
            appendice = _detecter_et_reconstruire_tableau_avancement(doc)
            if appendice:
                texte += appendice
        except Exception as e:
            logger.warning(f"Reconstruction du tableau d'avancement par position impossible ({e}) — le texte linéaire standard reste utilisé seul.")
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