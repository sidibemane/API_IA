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
    quand GIRAFE n'envoie pas les informations de l'agent avec l'acte.

Ce fichier lit/écrit DIRECTEMENT les mêmes fichiers JSON que l'API
(app/data/parametrage_verifications.json et app/data/base_agents.json).
L'API relit ces fichiers à chaque vérification : toute modification faite
ici est donc appliquée immédiatement, sans redémarrage.

Lancement :
    cd rag_rh_api
    streamlit run admin_app.py --server.port 8501
"""

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

# ═══════════════════════════════════════════════════════════
#  CHEMINS — mêmes fichiers que ceux lus par l'API (app/config.py)
# ═══════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "app" / "data"
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
        <h1>Administration — Vérification IA des actes RH</h1>
        <p>République du Sénégal · Ministère de la Fonction Publique, du Travail et de la Réforme du Service Public</p>
      </div>
    </div>
    """, unsafe_allow_html=True)

st.caption(
    " Dépose ton logo officiel dans `admin_assets/logo_ministere.png` pour qu'il "
    "s'affiche automatiquement ici (redémarre Streamlit après)."
)

# ═══════════════════════════════════════════════════════════
#  UTILITAIRES DE CHARGEMENT / SAUVEGARDE
# ═══════════════════════════════════════════════════════════

def charger_parametrage() -> dict:
    if not CHEMIN_PARAMETRAGE.exists():
        st.error(f"Fichier introuvable : {CHEMIN_PARAMETRAGE}")
        return {}
    with open(CHEMIN_PARAMETRAGE, "r", encoding="utf-8") as f:
        return json.load(f)


def sauvegarder_parametrage(config: dict):
    with open(CHEMIN_PARAMETRAGE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def charger_base_agents() -> list:
    if not CHEMIN_BASE_AGENTS.exists():
        return []
    with open(CHEMIN_BASE_AGENTS, "r", encoding="utf-8") as f:
        contenu = json.load(f)
    return contenu if isinstance(contenu, list) else []


def sauvegarder_base_agents(agents: list):
    with open(CHEMIN_BASE_AGENTS, "w", encoding="utf-8") as f:
        json.dump(agents, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════
#  NAVIGATION
# ═══════════════════════════════════════════════════════════

page = st.sidebar.radio(
    "Navigation",
    [" Circuits & vérifications", "👤 Base agents (secours)", " Aperçu JSON brut"],
)

st.sidebar.markdown("---")
st.sidebar.caption(
    "Toute modification enregistrée ici s'applique **immédiatement** sur "
    "la prochaine vérification d'acte dans l'API — aucun redémarrage requis."
)

# ═══════════════════════════════════════════════════════════
#  PAGE 1 — CIRCUITS & VÉRIFICATIONS
# ═══════════════════════════════════════════════════════════

if page == " Circuits & vérifications":
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

            if st.button(f"💾 Enregistrer les modifications — {NOMS_CIRCUITS.get(circuit, circuit)}", key=f"save_{circuit}"):
                config[circuit] = etapes
                sauvegarder_parametrage(config)
                st.success(
                    f"Configuration du circuit {circuit} enregistrée — "
                    "elle est déjà active sur la prochaine vérification d'acte."
                )

# ═══════════════════════════════════════════════════════════
#  PAGE 2 — BASE AGENTS DE SECOURS
# ═══════════════════════════════════════════════════════════

elif page == " Base agents (secours)":
    st.info(
        "ℹ Cette base **n'est utilisée qu'en secours**, quand GIRAFE n'envoie pas "
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
        use_container_width=True,
        key="editeur_agents",
    )

    if st.button("💾 Enregistrer la base agents"):
        nouveaux_agents = df_edite.fillna("").to_dict(orient="records")
        # On retire les lignes totalement vides ajoutées par erreur
        nouveaux_agents = [a for a in nouveaux_agents if any(str(v).strip() for v in a.values())]
        sauvegarder_base_agents(nouveaux_agents)
        st.success(f"Base agents enregistrée ({len(nouveaux_agents)} agent(s)).")

# ═══════════════════════════════════════════════════════════
#  PAGE 3 — APERÇU JSON BRUT (pour debug / vérification)
# ═══════════════════════════════════════════════════════════

else:
    st.subheader("Aperçu du fichier de paramétrage")
    config = charger_parametrage()
    st.json(config)
    st.download_button(
        "⬇️ Télécharger parametrage_verifications.json",
        data=json.dumps(config, ensure_ascii=False, indent=2),
        file_name="parametrage_verifications.json",
        mime="application/json",
    )