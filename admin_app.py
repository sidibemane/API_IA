"""
Interface d'administration — API de vérification des actes RH
================================================================
Permet de configurer DYNAMIQUEMENT, sans toucher au code ni redémarrer
l'API :
  - pour chaque circuit (APAE / APAG / RET) et chaque étape/profil,
    quelles vérifications sont actives (en-tête, timbre, identité agent,
    visa, délai d'avancement) ;
  - quels tampons sont attendus à chaque étape, et si une signature ou un
    numéro d'acte sont requis ;
  - la base agents de secours (base_agents.json), utilisée uniquement
    quand GIRAFE n'envoie pas les informations de l'agent avec l'acte ;
  - la BASE DE RÉFÉRENCE (dossier app/data) : ajout, modification et
    suppression de lignes dans chaque table CSV (corps, classes, échelons,
    durées d'avancement, type d'acte → circuit), des références légales
    de visa par corps et du texte des règles métier — avec contrôle de
    cohérence avant enregistrement, archivage automatique de chaque
    version, journal des modifications et restauration en un clic.

Ce fichier lit/écrit DIRECTEMENT les mêmes fichiers que l'API (dossier
app/data). L'API vérifie avant chaque vérification d'acte si un fichier a
changé et le recharge : toute modification faite ici est donc appliquée
immédiatement, sans modifier le code ni redémarrer l'API.

Lancement :
    cd rag_rh_api
    streamlit run admin_app.py --server.port 8501
"""

import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.services import referentiel_service as ref  # noqa: E402

# ═══════════════════════════════════════════════════════════
#  CHEMINS — mêmes fichiers que ceux lus par l'API (app/config.py)
# ═══════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = ref.dossier_data()  # même dossier que l'API (DATA_DIR du .env)
CHEMIN_PARAMETRAGE = DATA_DIR / "parametrage_verifications.json"
CHEMIN_BASE_AGENTS = DATA_DIR / "base_agents.json"
CHEMIN_LOGO = BASE_DIR / "admin_assets" / "logo_from_site_mfp (1).png"

NOMS_CIRCUITS = {
    "APAE": "Avancement d'échelon (APAE — 9 étapes)",
    "APAG": "Autre type d'acte (APAG — 12 étapes)",
    "RET": "Retraite fonctionnaire (RET — 13 étapes)",
}
TOUS_TAMPONS = ["DGFP", "DS", "DPB", "CF", "DP"]
VERIFICATIONS_LABELS = {
    "en_tete": "En-tête officiel",
    "timbre": "Timbre (signataire)",
    "identite_agent": "Identité agent (matricule/nom/prénom)",
    "visa": "Visa (loi/décret)",
    "delai_avancement": "Délai réglementaire d'avancement",
}

# Pleine largeur pour tableaux : `width="stretch"` sur les versions récentes
# de Streamlit, `use_container_width` (déprécié depuis) sur les anciennes.
try:
    _VERSION_ST = tuple(int(x) for x in st.__version__.split(".")[:2])
except ValueError:
    _VERSION_ST = (1, 0)
LARGEUR = {"width": "stretch"} if _VERSION_ST >= (1, 49) else {"use_container_width": True}

st.set_page_config(
    page_title="Admin — Vérification IA des actes RH",
    page_icon="🇸🇳",
    layout="wide",
)

# ═══════════════════════════════════════════════════════════
#  THEME — couleurs du drapeau sénégalais (vert dominant, jaune, rouge)
# ═══════════════════════════════════════════════════════════

VERT = "#00853F"
VERT_FONCE = "#00612D"
VERT_CLAIR = "#E8F5EC"
JAUNE = "#FDEF42"
ROUGE = "#E31B23"

st.markdown(f"""
<style>
    .stApp {{
        background-color: #FAFAF7;
    }}
    /* Bandeau d'en-tête */
    .header-ministere {{
        background: linear-gradient(90deg, {VERT_FONCE} 0%, {VERT} 55%, {VERT} 90%, {JAUNE} 100%);
        border-bottom: 5px solid {ROUGE};
        padding: 1.1rem 1.6rem;
        border-radius: 10px;
        display: flex;
        align-items: center;
        gap: 1rem;
        margin-bottom: 1.4rem;
        box-shadow: 0 2px 10px rgba(0,0,0,0.12);
    }}
    .header-ministere h1 {{
        color: white;
        font-size: 1.5rem;
        margin: 0;
        font-weight: 700;
    }}
    .header-ministere p {{
        color: #EAFBE9;
        margin: 0;
        font-size: 0.92rem;
    }}
    /* Boutons */
    .stButton>button {{
        background-color: {VERT};
        color: white;
        border: none;
        border-radius: 6px;
        font-weight: 600;
        padding: 0.5rem 1.2rem;
    }}
    .stButton>button:hover {{
        background-color: {VERT_FONCE};
        color: white;
    }}
    /* Onglets */
    .stTabs [data-baseweb="tab"] {{
        font-weight: 600;
    }}
    .stTabs [aria-selected="true"] {{
        color: {VERT_FONCE} !important;
        border-bottom-color: {VERT} !important;
    }}
    /* Cartes étape */
    .carte-etape {{
        background-color: white;
        border: 1px solid #E0E0DA;
        border-left: 5px solid {VERT};
        border-radius: 8px;
        padding: 0.9rem 1.1rem;
        margin-bottom: 0.8rem;
    }}
    div[data-testid="stMetric"] {{
        background-color: {VERT_CLAIR};
        border-radius: 8px;
        padding: 0.6rem;
        border: 1px solid {VERT};
    }}
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════
#  EN-TÊTE
# ═══════════════════════════════════════════════════════════

col_logo, col_titre = st.columns([1, 9])
with col_logo:
    if CHEMIN_LOGO.exists():
        st.image(str(CHEMIN_LOGO), width=70)
    else:
        st.markdown(
            f"<div style='font-size:2.6rem; text-align:center;'>🇸🇳</div>",
            unsafe_allow_html=True,
        )

with col_titre:
    st.markdown(f"""
    <div class="header-ministere" style="margin-left:-1rem;">
      <div>
        <h1>Administration  Vérification IA des actes RH</h1>
        <p>République du Sénégal · Ministère de la Fonction Publique, du Travail et de la Réforme du Service Public</p>
      </div>
    </div>
    """, unsafe_allow_html=True)



# ═══════════════════════════════════════════════════════════
#  UTILITAIRES DE CHARGEMENT / SAUVEGARDE
# ═══════════════════════════════════════════════════════════

def charger_parametrage() -> dict:
    if not CHEMIN_PARAMETRAGE.exists():
        st.error(f"Fichier introuvable : {CHEMIN_PARAMETRAGE}")
        return {}
    with open(CHEMIN_PARAMETRAGE, "r", encoding="utf-8") as f:
        return json.load(f)


def sauvegarder_parametrage(config: dict, circuit: str = ""):
    # Écriture atomique + archivage de la version précédente + journal
    ref.sauvegarder_texte(
        CHEMIN_PARAMETRAGE.name,
        json.dumps(config, ensure_ascii=False, indent=2),
        auteur=auteur_courant(),
        commentaire=f"Paramétrage du circuit {circuit}" if circuit else "",
        action="paramétrage circuit",
    )


def charger_base_agents() -> list:
    if not CHEMIN_BASE_AGENTS.exists():
        return []
    with open(CHEMIN_BASE_AGENTS, "r", encoding="utf-8") as f:
        contenu = json.load(f)
    return contenu if isinstance(contenu, list) else []


def sauvegarder_base_agents(agents: list):
    with open(CHEMIN_BASE_AGENTS, "w", encoding="utf-8") as f:
        json.dump(agents, f, ensure_ascii=False, indent=2)



def auteur_courant() -> str:
    return (st.session_state.get("auteur") or "").strip() or "admin"


def _url_api() -> str:
    return os.getenv("ADMIN_API_URL", f"http://localhost:{os.getenv('API_PORT', '8000')}").rstrip("/")


def etat_api():
    """Interroge l'API pour savoir ce qu'elle a réellement chargé. Retourne
    None si l'API n'est pas joignable (elle prendra quand même les
    modifications en compte à son prochain appel)."""
    try:
        with urllib.request.urlopen(f"{_url_api()}/admin/referentiel", timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def afficher_etat_api_sidebar():
    etat = etat_api()
    if etat is None:
        st.sidebar.caption(
            f"⚪ API non joignable ({_url_api()}). Les modifications seront "
            "prises en compte dès son prochain appel."
        )
    else:
        heure = (etat.get("date_dernier_chargement") or "")[11:19]
        st.sidebar.caption(
            f"🟢 API connectée — référentiel chargé à {heure} : "
            f"{etat.get('nb_corps', 0)} corps, {etat.get('nb_durees_avancement', 0)} durées, "
            f"{etat.get('nb_types_acte_parametres', 0)} types d'acte paramétrés."
        )


def _mtime(nom: str) -> int:
    try:
        return (DATA_DIR / nom).stat().st_mtime_ns
    except FileNotFoundError:
        return 0


@st.cache_data(show_spinner=False)
def _table_en_cache(nom: str, mtime: int) -> pd.DataFrame:
    return ref.charger_table(nom)


def charger_table_cache(nom: str) -> pd.DataFrame:
    # Le cache est indexé sur la date de modification : il se vide tout
    # seul dès que le fichier change (ici ou ailleurs).
    return _table_en_cache(nom, _mtime(nom)).copy()


@st.cache_data(show_spinner=False)
def _references_en_cache(mtime: int):
    return ref.charger_references_visa()


def _version(cle: str) -> int:
    return st.session_state.get(f"version_{cle}", 0)


def _nouvelle_version(cle: str):
    st.session_state[f"version_{cle}"] = _version(cle) + 1


def _flash(type_message: str, message: str):
    st.session_state["flash"] = (type_message, message)


def _resume_diff(d: dict) -> str:
    return (
        f"**{len(d['ajoutees'])}** ajoutée(s) · **{len(d['modifiees'])}** modifiée(s) · "
        f"**{len(d['supprimees'])}** supprimée(s)"
    )


def _fusionner_edition(complet: pd.DataFrame, affiche: pd.DataFrame, edite: pd.DataFrame) -> pd.DataFrame:
    """Réinjecte dans la table complète le sous-ensemble édité (après
    filtre). Lignes affichées absentes du résultat = supprimées ; lignes
    sans n° d'origine = ajoutées (placées en fin de table)."""
    base = complet.drop(index=affiche["_ligne"].astype(int).tolist(), errors="ignore")
    ed = edite.copy()
    lignes = pd.to_numeric(ed["_ligne"], errors="coerce")
    existantes = ed[lignes.notna()].copy()
    existantes.index = lignes[lignes.notna()].astype(int)
    nouvelles = ed[lignes.isna()]
    res = pd.concat([base, existantes.drop(columns="_ligne")]).sort_index()
    if len(nouvelles):
        res = pd.concat([res, nouvelles.drop(columns="_ligne")], ignore_index=True)
    return res.reset_index(drop=True)


def _config_colonnes(nom: str, df_affiche: pd.DataFrame, defauts: dict) -> dict:
    spec = ref.TABLES[nom]
    config = {"_ligne": None}  # colonne technique masquée
    for col in df_affiche.columns:
        if col == "_ligne":
            continue
        libelle = spec["libelles"].get(col, col)
        obligatoire = col in spec["obligatoires"]
        aide = "Obligatoire" if obligatoire else None
        if col == spec.get("auto_id"):
            config[col] = st.column_config.TextColumn(libelle, disabled=True, help="Attribué automatiquement à l'enregistrement")
            continue
        options = None
        if col in spec["choix"]:
            options = list(spec["choix"][col])
        elif col in spec["fk"]:
            table_cible, col_cible = spec["fk"][col]
            options = sorted(set(charger_table_cache(table_cible)[col_cible]))
        if options is not None:
            # On garde aussi les valeurs déjà présentes, même hors liste,
            # pour ne pas les effacer à l'affichage.
            options = list(dict.fromkeys(options + sorted(set(df_affiche[col]) - set(options) - {""})))
            config[col] = st.column_config.SelectboxColumn(
                libelle, options=options, required=obligatoire, help=aide, default=defauts.get(col),
            )
        else:
            config[col] = st.column_config.TextColumn(libelle, required=obligatoire, help=aide, default=defauts.get(col))
    return config


def _afficher_validation(erreurs: list, avertissements: list, cle: str) -> bool:
    """Affiche erreurs / avertissements. Retourne True si l'enregistrement
    est autorisé."""
    for e in erreurs:
        st.error(e)
    for a in avertissements:
        st.warning(a)
    if erreurs:
        st.caption("Corrigez les erreurs ci-dessus pour pouvoir enregistrer.")
        return False
    if avertissements:
        return st.checkbox("J'ai lu les avertissements — enregistrer quand même", key=f"confirm_{cle}")
    return True


# ═══════════════════════════════════════════════════════════
#  NAVIGATION
# ═══════════════════════════════════════════════════════════

PAGE_CIRCUITS = " Circuits & vérifications"
PAGE_TABLES = " Base de référence (tables)"
PAGE_VISA = " Références visa par corps"
PAGE_REGLES = " Règles métier (RAG)"
PAGE_HISTORIQUE = " Historique & restauration"
PAGE_AGENTS = " Base agents (secours)"
PAGE_JSON = " Aperçu JSON brut"

page = st.sidebar.radio(
    "Navigation",
    [PAGE_CIRCUITS, PAGE_TABLES, PAGE_VISA, PAGE_REGLES, PAGE_HISTORIQUE, PAGE_AGENTS, PAGE_JSON],
)

st.sidebar.markdown("---")
st.sidebar.text_input(
    "Votre nom (journal des modifications)",
    key="auteur",
    placeholder="ex : M. Diop — DGFP",
)
st.sidebar.caption(
    "Toute modification enregistrée ici s'applique **immédiatement** sur "
    "la prochaine vérification d'acte dans l'API — aucun redémarrage requis."
)
afficher_etat_api_sidebar()

# Message de confirmation conservé d'un rechargement de page à l'autre
if st.session_state.get("flash"):
    _type, _msg = st.session_state.pop("flash")
    getattr(st, _type)(_msg)

# ═══════════════════════════════════════════════════════════
#  PAGE 1 — CIRCUITS & VÉRIFICATIONS
# ═══════════════════════════════════════════════════════════

if page == PAGE_CIRCUITS:
    config = charger_parametrage()

    if not config:
        st.warning("Aucune configuration trouvée.")
        st.stop()

    onglets = st.tabs([NOMS_CIRCUITS.get(c, c) for c in config.keys()])

    for onglet, circuit in zip(onglets, config.keys()):
        with onglet:
            etapes = config[circuit]
            nb_actives_total = 0

            for num_etape in sorted(etapes.keys(), key=lambda x: int(x)):
                etape = etapes[num_etape]
                cle_base = f"{circuit}_{num_etape}"

                with st.container():
                    st.markdown('<div class="carte-etape">', unsafe_allow_html=True)
                    col_titre, col_reset = st.columns([6, 1])
                    with col_titre:
                        st.markdown(f"**Étape {num_etape} — {etape['nom']}** · _{etape['role']}_")

                    c1, c2 = st.columns([3, 2])

                    with c1:
                        st.caption("Vérifications actives à cette étape")
                        verifs = etape.get("verifications", {})
                        nb_cols = st.columns(3)
                        nouvelles_verifs = {}
                        for i, (cle, label) in enumerate(VERIFICATIONS_LABELS.items()):
                            with nb_cols[i % 3]:
                                nouvelles_verifs[cle] = st.checkbox(
                                    label,
                                    value=verifs.get(cle, True),
                                    key=f"verif_{cle_base}_{cle}",
                                )
                        etape["verifications"] = nouvelles_verifs

                    with c2:
                        st.caption("Tampons attendus à cette étape")
                        tampons_actuels = set(etape.get("tampons_requis", []))
                        cols_tampons = st.columns(len(TOUS_TAMPONS))
                        nouveaux_tampons = []
                        for i, tampon in enumerate(TOUS_TAMPONS):
                            with cols_tampons[i]:
                                coche = st.checkbox(
                                    tampon,
                                    value=tampon in tampons_actuels,
                                    key=f"tampon_{cle_base}_{tampon}",
                                )
                                if coche:
                                    nouveaux_tampons.append(tampon)
                        etape["tampons_requis"] = nouveaux_tampons

                        cc1, cc2 = st.columns(2)
                        with cc1:
                            etape["signature_requise"] = st.checkbox(
                                "Signature Ministre requise",
                                value=etape.get("signature_requise", False),
                                key=f"signature_{cle_base}",
                            )
                        with cc2:
                            etape["numero_acte_requis"] = st.checkbox(
                                "Numéro d'acte requis",
                                value=etape.get("numero_acte_requis", False),
                                key=f"numero_{cle_base}",
                            )

                    st.markdown("</div>", unsafe_allow_html=True)

            if st.button(f" Enregistrer les modifications — {NOMS_CIRCUITS.get(circuit, circuit)}", key=f"save_{circuit}"):
                config[circuit] = etapes
                sauvegarder_parametrage(config, circuit)
                st.success(
                    f"Configuration du circuit {circuit} enregistrée — "
                    "elle est déjà active sur la prochaine vérification d'acte."
                )

# ═══════════════════════════════════════════════════════════
#  PAGE 2 — BASE AGENTS DE SECOURS
# ═══════════════════════════════════════════════════════════

elif page == PAGE_AGENTS:
    st.info(
        " Cette base **n'est utilisée qu'en secours**, quand GIRAFE n'envoie pas "
        "les informations de l'agent (`agent_info`) avec l'acte lors de l'appel à "
        "l'API. En usage normal, c'est GIRAFE qui fournit ces données à chaque "
        "appel — cette base sert surtout pour les tests autonomes."
    )

    agents = charger_base_agents()
    df = pd.DataFrame(agents) if agents else pd.DataFrame(
        columns=["matricule", "nom", "prenom", "date_naissance", "corps", "hierarchie"]
    )

    st.markdown("**Édite le tableau directement** (double-clique une cellule), ajoute une ligne en bas, ou supprime une ligne avec la case à cocher à gauche puis la touche Suppr.")

    df_edite = st.data_editor(
        df,
        num_rows="dynamic",
        **LARGEUR,
        key="editeur_agents",
    )

    if st.button(" Enregistrer la base agents"):
        nouveaux_agents = df_edite.fillna("").to_dict(orient="records")
        # On retire les lignes totalement vides ajoutées par erreur
        nouveaux_agents = [a for a in nouveaux_agents if any(str(v).strip() for v in a.values())]
        sauvegarder_base_agents(nouveaux_agents)
        st.success(f"Base agents enregistrée ({len(nouveaux_agents)} agent(s)).")

# ═══════════════════════════════════════════════════════════
#  PAGE 3 — APERÇU JSON BRUT (pour debug / vérification)
# ═══════════════════════════════════════════════════════════

elif page == PAGE_JSON:
    st.subheader("Aperçu du fichier de paramétrage")
    config = charger_parametrage()
    st.json(config)
    st.download_button(
        "⬇ Télécharger parametrage_verifications.json",
        data=json.dumps(config, ensure_ascii=False, indent=2),
        file_name="parametrage_verifications.json",
        mime="application/json",
    )

# ═══════════════════════════════════════════════════════════
#  PAGE 4 — BASE DE RÉFÉRENCE : TABLES CSV
#
#  Chaque table décrite dans app/services/referentiel_service.py (TABLES)
#  apparaît ici automatiquement. L'administrateur ajoute, modifie ou
#  supprime des lignes ; avant l'enregistrement, les contrôles de
#  cohérence sont affichés (champs obligatoires, nombres, doublons,
#  codes inexistants dans les autres tables...). À l'enregistrement,
#  l'ancienne version est archivée et l'API la prend en compte à la
#  vérification d'acte suivante.
# ═══════════════════════════════════════════════════════════

elif page == PAGE_TABLES:
    st.subheader("Base de référence — tables de règles")
    st.markdown(
        "Quand le ministère publie une nouvelle règle (nouveau corps, nouvelle durée "
        "d'avancement, nouvelle classe, nouveau type d'acte…), saisissez-la ici : "
        "**aucune modification du code n'est nécessaire**. Chaque enregistrement est "
        "archivé et peut être annulé depuis la page *Historique & restauration*."
    )

    nom = st.selectbox(
        "Table à modifier",
        list(ref.TABLES.keys()),
        format_func=lambda n: f"{ref.TABLES[n]['titre']}  ({n})",
    )
    spec = ref.TABLES[nom]
    complet = charger_table_cache(nom)

    st.info(spec["description"])
    c1, c2, c3 = st.columns(3)
    c1.metric("Lignes", f"{len(complet):,}".replace(",", " "))
    journal_table = ref.lire_journal(limite=1, fichier=nom)
    if journal_table:
        derniere = journal_table[0]
        c2.metric("Dernière modification", derniere["date"].replace("T", " ")[:16])
        c3.metric("Par", derniere.get("auteur", "—"))
    elif (DATA_DIR / nom).exists():
        c2.metric("Dernière modification", datetime.fromtimestamp((DATA_DIR / nom).stat().st_mtime).strftime("%Y-%m-%d %H:%M"))
        c3.metric("Par", "—")
    else:
        c2.metric("Fichier", "pas encore créé")
        c3.metric("", "")

    onglet_modif, onglet_import, onglet_export = st.tabs(["Ajouter / modifier / supprimer", "Importer un fichier", "Exporter"])

    # ── Onglet 1 : édition directe ──
    with onglet_modif:
        filtre_corps = None
        defauts = dict(spec.get("valeurs_defaut", {}))
        if nom == "corps_classe_echelon.csv":
            corps_df = charger_table_cache("corps.csv")
            libelles_corps = dict(zip(corps_df["cps_code"], corps_df["cps_libelle"]))
            codes = sorted(set(complet["cce_cps_code"]) | set(libelles_corps))
            filtre_corps = st.selectbox(
                "Corps",
                ["(tous les corps)"] + codes,
                format_func=lambda c: c if c == "(tous les corps)" else f"{c} — {libelles_corps.get(c, '⚠ code absent de la table des corps')}",
                help="Choisissez un corps pour afficher et modifier ses durées. Les nouvelles lignes ajoutées reprendront ce code corps.",
            )
            if filtre_corps == "(tous les corps)":
                filtre_corps = None
            else:
                defauts["cce_cps_code"] = filtre_corps

        recherche = st.text_input("Rechercher (dans toutes les colonnes)", key=f"recherche_{nom}", placeholder="ex : INSTITUTEURS, 10026, 2_SG…")

        vue = complet.copy()
        vue.insert(0, "_ligne", vue.index)
        if filtre_corps:
            vue = vue[vue["cce_cps_code"] == filtre_corps]
        if recherche.strip():
            terme = recherche.strip().upper()
            masque = vue.drop(columns="_ligne").apply(lambda col: col.str.upper().str.contains(terme, regex=False)).any(axis=1)
            vue = vue[masque]

        LIMITE = 2000
        if len(vue) > LIMITE:
            st.caption(
                f"{len(vue):,} lignes correspondent — seules les {LIMITE:,} premières sont affichées. "
                "Affinez avec le filtre ou la recherche pour atteindre une ligne précise.".replace(",", " ")
            )
            vue = vue.head(LIMITE)
        else:
            st.caption(f"{len(vue)} ligne(s) affichée(s) sur {len(complet)}.")

        st.markdown(
            "Double-cliquez une cellule pour la **modifier** · ajoutez une ligne avec **+** en bas du "
            "tableau · pour **supprimer**, cochez la case à gauche de la ligne puis touche *Suppr* (ou l'icône corbeille)."
        )
        cle_editeur = f"edit_{nom}_{filtre_corps}_{recherche}_{_version(nom)}"
        edite = st.data_editor(
            vue,
            num_rows="dynamic",
            hide_index=True,
            **LARGEUR,
            column_config=_config_colonnes(nom, vue, defauts),
            key=cle_editeur,
        )

        fusion = ref.preparer_table(nom, _fusionner_edition(complet, vue, edite))
        d = ref.diff_tables(nom, complet, fusion)
        nb_changements = len(d["ajoutees"]) + len(d["modifiees"]) + len(d["supprimees"])

        if nb_changements == 0:
            st.caption("Aucune modification en attente.")
        else:
            st.markdown(f"#### Modifications en attente : {_resume_diff(d)}")
            with st.expander("Voir le détail"):
                cle_cols = spec["cle"]
                for titre, cles, source in (
                    ("Ajoutées", d["ajoutees"], fusion),
                    ("Modifiées (nouvelle valeur)", d["modifiees"], fusion),
                    ("Supprimées", d["supprimees"], complet),
                ):
                    if cles:
                        st.markdown(f"**{titre}**")
                        masque = source[cle_cols].apply(tuple, axis=1).isin(set(cles))
                        st.dataframe(source[masque], hide_index=True, **LARGEUR)

            erreurs, avertissements = ref.valider_table(nom, fusion, complet)
            autorise = _afficher_validation(erreurs, avertissements, f"{nom}_{_version(nom)}")
            commentaire = st.text_input(
                "Texte de référence / motif (facultatif, conservé dans le journal)",
                key=f"commentaire_{nom}_{_version(nom)}",
                placeholder="ex : Décret n°2026-123 du 02/02/2026 portant statut particulier…",
            )
            col_ok, col_annuler = st.columns([3, 1])
            with col_ok:
                if st.button(f"Enregistrer dans {nom}", disabled=not autorise, type="primary", key=f"save_{nom}"):
                    try:
                        res = ref.sauvegarder_table(nom, fusion, auteur=auteur_courant(), commentaire=commentaire)
                        _nouvelle_version(nom)
                        _flash("success",
                               f"{nom} enregistré — {res['ajoutees']} ajoutée(s), {res['modifiees']} modifiée(s), "
                               f"{res['supprimees']} supprimée(s). Les nouvelles règles sont actives dès la "
                               f"prochaine vérification d'acte (ancienne version archivée).")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
            with col_annuler:
                if st.button("Annuler les modifications", key=f"annuler_{nom}"):
                    _nouvelle_version(nom)
                    st.rerun()

    # ── Onglet 2 : import d'un fichier complet ──
    with onglet_import:
        st.markdown(
            "Pour intégrer un référentiel envoyé par le ministère (CSV, séparateur `,` ou `;`, "
            f"encodage UTF-8). Colonnes attendues : `{', '.join(spec['colonnes'])}`."
        )
        fichier = st.file_uploader("Fichier CSV", type=["csv", "txt"], key=f"import_{nom}_{_version(nom)}")
        if fichier is not None:
            try:
                importe = ref.lire_csv_televerse(fichier.getvalue())
            except Exception as e:
                st.error(f"Lecture du fichier impossible : {e}")
                importe = None
            if importe is not None:
                manquantes = [c for c in spec["obligatoires"] if c not in importe.columns]
                if manquantes:
                    st.error(f"Colonnes obligatoires absentes du fichier : {', '.join(manquantes)}")
                else:
                    en_trop = [c for c in importe.columns if c not in spec["colonnes"]]
                    if en_trop:
                        st.warning(f"Colonnes inconnues conservées telles quelles : {', '.join(en_trop)}")
                    mode = st.radio(
                        "Mode d'import",
                        ["Ajouter / mettre à jour (les lignes existantes non citées sont conservées)",
                         "Remplacer toute la table par ce fichier"],
                        key=f"mode_import_{nom}",
                    )
                    for col in spec["colonnes"]:
                        if col not in importe.columns:
                            importe[col] = ""
                    if mode.startswith("Remplacer"):
                        cible = importe
                    else:
                        cle_cols = spec["cle"]
                        combine = pd.concat([complet, importe], ignore_index=True)
                        avec_cle = combine[cle_cols].ne("").all(axis=1)
                        cible = pd.concat([combine[avec_cle].drop_duplicates(cle_cols, keep="last"), combine[~avec_cle]])
                    cible = ref.preparer_table(nom, cible)
                    d_imp = ref.diff_tables(nom, complet, cible)
                    st.markdown(f"Aperçu : {_resume_diff(d_imp)} — {len(cible)} lignes au total après import.")
                    st.dataframe(importe.head(50), hide_index=True, **LARGEUR)
                    erreurs, avertissements = ref.valider_table(nom, cible, complet)
                    autorise = _afficher_validation(erreurs, avertissements, f"import_{nom}_{_version(nom)}")
                    commentaire = st.text_input("Texte de référence / motif", key=f"commentaire_import_{nom}")
                    if st.button("Importer", disabled=not autorise, type="primary", key=f"btn_import_{nom}"):
                        try:
                            res = ref.sauvegarder_table(nom, cible, auteur=auteur_courant(),
                                                        commentaire=commentaire or f"Import de {fichier.name}",
                                                        action="import")
                            _nouvelle_version(nom)
                            _flash("success", f"Import terminé — {res['ajoutees']} ajoutée(s), {res['modifiees']} modifiée(s), {res['supprimees']} supprimée(s).")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

    # ── Onglet 3 : export ──
    with onglet_export:
        st.download_button(
            f"⬇ Télécharger {nom} (version en service)",
            data=ref.table_vers_csv(complet),
            file_name=nom, mime="text/csv",
        )
        st.download_button(
            "⬇ Télécharger un modèle vide (en-têtes seulement)",
            data=ref.table_vers_csv(pd.DataFrame(columns=spec["colonnes"])),
            file_name=f"modele_{nom}", mime="text/csv",
        )


# ═══════════════════════════════════════════════════════════
#  PAGE 5 — RÉFÉRENCES LÉGALES (VISA) PAR CORPS
# ═══════════════════════════════════════════════════════════

elif page == PAGE_VISA:
    st.subheader("Références légales attendues dans le visa, par corps")
    st.markdown(
        "Pour chaque corps, la liste des lois et décrets que l'acte **doit** citer. "
        "L'IA vérifie la présence de chaque **numéro de texte** (ex : `61-33`, `97-17`) dans l'acte. "
        "Un corps sans bloc ici est contrôlé avec la règle générale : Loi n°61-33 pour un "
        "fonctionnaire, Loi n°97-17 / Décret n°74-347 pour un non-fonctionnaire."
    )

    corps_refs, fin_ligne = _references_en_cache(_mtime(ref.FICHIER_REFERENCES_VISA))
    corps_refs = [dict(c, references=list(c["references"])) for c in corps_refs]
    st.caption(f"{len(corps_refs)} corps ont des références précises.")

    NOUVEAU = "__nouveau__"
    recherche = st.text_input("Rechercher un corps (code ou libellé)", placeholder="ex : JURISTES, 10027")
    candidats = [
        c for c in corps_refs
        if not recherche.strip()
        or recherche.strip().upper() in c["libelle"].upper()
        or recherche.strip() in c["code"]
    ]
    options = [NOUVEAU] + [c["code"] for c in candidats[:500]]
    par_code = {c["code"]: c for c in corps_refs}
    choix = st.selectbox(
        "Corps",
        options,
        index=1 if len(options) > 1 else 0,
        format_func=lambda o: "➕ Ajouter un nouveau corps" if o == NOUVEAU else f"{o} — {par_code[o]['libelle']}",
    )

    v = _version("visa")
    if choix == NOUVEAU:
        corps_df = charger_table_cache("corps.csv")
        sans_bloc = corps_df[~corps_df["cps_code"].isin(par_code.keys())]
        modele = st.selectbox(
            "Pré-remplir à partir de la table des corps (facultatif)",
            [""] + sans_bloc["cps_code"].tolist(),
            format_func=lambda c: "—" if not c else f"{c} — {dict(zip(sans_bloc['cps_code'], sans_bloc['cps_libelle'])).get(c, '')}",
            key=f"visa_modele_{v}",
        )
        if modele:
            ligne = sans_bloc[sans_bloc["cps_code"] == modele].iloc[0]
            courant = {"libelle": ligne["cps_libelle"], "code": modele,
                       "type": ligne["cps_typecorps_code"] if ligne["cps_typecorps_code"] in ref.TYPES_CORPS else "FONCT",
                       "age_retraite": ligne["cps_age_retraite"] or "60", "references": []}
        else:
            courant = {"libelle": "", "code": "", "type": "FONCT", "age_retraite": "60", "references": []}
        code_original = None
    else:
        courant = par_code[choix]
        code_original = choix

    cle_w = f"{choix}_{v}_{courant['code']}"
    c1, c2, c3, c4 = st.columns([4, 1.4, 1.6, 1.2])
    libelle = c1.text_input("Libellé du corps", value=courant["libelle"], key=f"visa_lib_{cle_w}")
    code = c2.text_input("Code", value=courant["code"], key=f"visa_code_{cle_w}")
    type_corps = c3.selectbox("Type", ref.TYPES_CORPS,
                              index=ref.TYPES_CORPS.index(courant["type"]) if courant["type"] in ref.TYPES_CORPS else 0,
                              key=f"visa_type_{cle_w}")
    age = c4.text_input("Âge retraite", value=str(courant.get("age_retraite") or ""), key=f"visa_age_{cle_w}")
    texte_refs = st.text_area(
        "Références juridiques — une par ligne",
        value="\n".join(courant["references"]),
        height=200,
        key=f"visa_refs_{cle_w}",
        help="Recopiez le texte tel qu'il figure dans le visa, avec son numéro au format n°XX-YY.",
    )
    nouveau = {
        "libelle": " ".join(libelle.split()), "code": code.strip(), "type": type_corps,
        "age_retraite": age.strip(),
        "references": [" ".join(l.split()) for l in texte_refs.splitlines() if l.strip()],
    }
    numeros = [n for n in (ref.numeros_reference(r) for r in nouveau["references"]) if n]
    st.markdown("Numéros qui seront contrôlés dans l'acte : " + (", ".join(f"`{n}`" for n in numeros) if numeros else "_aucun_"))

    vierge = code_original is None and not nouveau["libelle"] and not nouveau["code"]
    modifie = (code_original is None and not vierge) or (code_original is not None and nouveau != {k: courant[k] for k in nouveau})
    erreurs, avertissements = ref.valider_corps_reference(nouveau, corps_refs, code_original)
    if vierge:
        autorise = False
        st.caption("Renseignez le libellé, le code, le type et les références du nouveau corps.")
    elif modifie:
        autorise = _afficher_validation(erreurs, avertissements, f"visa_{cle_w}")
    else:
        autorise = False
        for a in avertissements:
            st.caption(f"ℹ {a}")
    commentaire = st.text_input("Texte de référence / motif (facultatif)", key=f"visa_com_{cle_w}")

    b1, b2 = st.columns([3, 2])
    with b1:
        if st.button("Enregistrer" if code_original else "Ajouter ce corps", type="primary",
                     disabled=not (modifie and autorise), key=f"visa_save_{cle_w}"):
            if code_original is None:
                liste = corps_refs + [nouveau]
                action = "ajout corps"
            else:
                liste = [nouveau if c["code"] == code_original else c for c in corps_refs]
                action = "modification corps"
            ref.sauvegarder_references_visa(liste, fin_ligne, auteur_courant(), commentaire, action, nouveau["code"])
            _nouvelle_version("visa")
            _flash("success", f"Références du corps {nouveau['code']} enregistrées — actives dès la prochaine vérification d'acte.")
            st.rerun()
    if code_original:
        with b2:
            confirmer = st.checkbox("Confirmer la suppression", key=f"visa_conf_del_{cle_w}")
            if st.button("Supprimer ce bloc", disabled=not confirmer, key=f"visa_del_{cle_w}"):
                liste = [c for c in corps_refs if c["code"] != code_original]
                ref.sauvegarder_references_visa(liste, fin_ligne, auteur_courant(), commentaire, "suppression corps", code_original)
                _nouvelle_version("visa")
                _flash("success", f"Bloc de références du corps {code_original} supprimé — ce corps sera contrôlé avec la règle générale FONCT / NON_FONCT.")
                st.rerun()


# ═══════════════════════════════════════════════════════════
#  PAGE 6 — RÈGLES MÉTIER (texte du RAG)
# ═══════════════════════════════════════════════════════════

elif page == PAGE_REGLES:
    st.subheader("Règles métier — base de connaissance de l'assistant")
    st.markdown(
        f"Texte interrogé par l'assistant questions/réponses (`/rag/question`). "
        f"À l'enregistrement, l'API **recalcule son index automatiquement** à la question suivante "
        f"(compter quelques dizaines de secondes sur CPU pour cette première réponse). "
        f"Conservez la forme `RÈGLE XX-00 : …` pour que l'assistant puisse citer la règle."
    )
    nom_regles = ref.FICHIER_REGLES_METIER
    texte_actuel = ref.charger_texte(nom_regles)
    v = _version("regles")
    nouveau_texte = st.text_area("Contenu", value=texte_actuel, height=560, key=f"regles_{v}")
    modifie = nouveau_texte != texte_actuel
    st.caption(f"{len(nouveau_texte):,} caractères".replace(",", " ") + (" — modifications non enregistrées" if modifie else ""))
    commentaire = st.text_input("Texte de référence / motif (facultatif)", key=f"regles_com_{v}")
    c1, c2 = st.columns([3, 1])
    with c1:
        if st.button("Enregistrer les règles métier", type="primary", disabled=not modifie):
            ref.sauvegarder_texte(nom_regles, nouveau_texte, auteur=auteur_courant(), commentaire=commentaire,
                                  details={"nb_caracteres": len(nouveau_texte)})
            _nouvelle_version("regles")
            _flash("success", "Règles métier enregistrées — l'index de l'assistant sera recalculé à la prochaine question.")
            st.rerun()
    with c2:
        if st.button("Annuler", disabled=not modifie):
            _nouvelle_version("regles")
            st.rerun()


# ═══════════════════════════════════════════════════════════
#  PAGE 7 — HISTORIQUE & RESTAURATION
# ═══════════════════════════════════════════════════════════

elif page == PAGE_HISTORIQUE:
    st.subheader("Historique des modifications")
    tous_fichiers = list(ref.TABLES.keys()) + list(ref.TEXTES.keys()) + [CHEMIN_PARAMETRAGE.name]
    filtre = st.selectbox("Fichier", ["(tous)"] + tous_fichiers)
    journal = ref.lire_journal(limite=500, fichier=None if filtre == "(tous)" else filtre)
    if not journal:
        st.caption("Aucune modification enregistrée depuis l'interface pour l'instant.")
    else:
        lignes = []
        for e in journal:
            if "ajoutees" in e:
                detail = f"+{e.get('ajoutees', 0)} / ~{e.get('modifiees', 0)} / −{e.get('supprimees', 0)}"
            elif e.get("corps"):
                detail = f"corps {e['corps']}"
            else:
                detail = ""
            exemples = ", ".join(e.get("exemples_ajoutees", []) + e.get("exemples_modifiees", []) + e.get("exemples_supprimees", []))
            lignes.append({
                "Date": e.get("date", "").replace("T", " "),
                "Auteur": e.get("auteur", ""),
                "Fichier": e.get("fichier", ""),
                "Action": e.get("action", ""),
                "Lignes (+ / ~ / −)": detail,
                "Exemples (clés)": exemples,
                "Motif": e.get("commentaire", ""),
            })
        st.dataframe(pd.DataFrame(lignes), hide_index=True, **LARGEUR)

    st.markdown("---")
    st.subheader("Restaurer une version précédente")
    st.caption(
        "Chaque enregistrement archive la version remplacée. Restaurer une version archive "
        "aussi la version actuelle : une restauration peut donc toujours être annulée."
    )
    avec_archives = [f for f in tous_fichiers if ref.lister_sauvegardes(f)]
    fichier_r = st.selectbox(
        "Fichier à restaurer", tous_fichiers, key="fichier_restauration",
        index=tous_fichiers.index(avec_archives[0]) if avec_archives else 0,
        format_func=lambda f: f + ("" if f in avec_archives else "  (aucune version archivée)"),
    )
    sauvegardes = ref.lister_sauvegardes(fichier_r)
    if not sauvegardes:
        st.caption("Aucune version archivée pour ce fichier.")
    else:
        choix_s = st.selectbox(
            "Version archivée",
            [s["fichier"] for s in sauvegardes],
            format_func=lambda f: next(
                f"Version en service jusqu'au {s['date'].strftime('%d/%m/%Y à %H:%M:%S')}  ({s['taille'] / 1024:.0f} Ko)"
                for s in sauvegardes if s["fichier"] == f
            ),
        )
        chemin_s = next(Path(s["chemin"]) for s in sauvegardes if s["fichier"] == choix_s)
        if fichier_r in ref.TABLES:
            try:
                d_r = ref.diff_tables(fichier_r, charger_table_cache(fichier_r), ref.lire_csv_televerse(chemin_s.read_bytes()))
                st.markdown(f"Effet de la restauration par rapport à la version en service : {_resume_diff(d_r)}")
            except Exception as e:
                st.caption(f"Comparaison impossible : {e}")
        c1, c2 = st.columns(2)
        with c1:
            st.download_button("⬇ Télécharger cette version", data=chemin_s.read_bytes(), file_name=f"{choix_s}")
        with c2:
            confirmer = st.checkbox("Confirmer la restauration", key=f"conf_rest_{choix_s}")
            if st.button("Restaurer cette version", type="primary", disabled=not confirmer):
                ref.restaurer_sauvegarde(fichier_r, choix_s, auteur=auteur_courant())
                for k in ("visa", "regles", fichier_r):
                    _nouvelle_version(k)
                _flash("success", f"{fichier_r} restauré — la version restaurée est active dès la prochaine vérification d'acte.")
                st.rerun()