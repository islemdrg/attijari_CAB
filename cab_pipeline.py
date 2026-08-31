"""
cab_pipeline.py
================
Module partagé : extraction API Jira + nettoyage complet du projet CAB.

Utilisé par :
- 1_extraction_nettoyage.ipynb : pour vérifier/explorer le résultat du nettoyage.
- 2_analyse_qualite.ipynb      : pour récupérer un DataFrame propre et calculer le score de qualité.

Aucune donnée n'est mise en cache sur disque (pas de CSV) : chaque appel à
extract_and_clean_cab() refait une extraction fraîche via l'API Jira.
"""

import os
import re
import json
import ast
from collections import Counter

import requests
import pandas as pd
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------

def _load_config():
    load_dotenv(override=True)
    jira_url = os.environ.get("BASE_URL")
    email = os.environ.get("EMAIL")
    api_token = os.environ.get("TOKEN")
    project_key = os.environ.get("PROJECT_KEY")

    assert jira_url and email and api_token and project_key, (
        "Vérifie ton fichier .env (BASE_URL, EMAIL, TOKEN, PROJECT_KEY requis)."
    )
    return jira_url, email, api_token, project_key


# ---------------------------------------------------------------------------
# 2. Extraction API
# ---------------------------------------------------------------------------

def get_all_issues(jira_url, email, api_token, jql, batch_size=100, limit=None):
    auth = HTTPBasicAuth(email, api_token)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    url = f"{jira_url}/rest/api/3/search/jql"

    all_issues = []
    next_page_token = None

    while True:
        if limit is not None:
            remaining = max(0, limit - len(all_issues))
            if remaining <= 0:
                break
            payload_max = min(batch_size, remaining)
        else:
            payload_max = batch_size

        payload = {"jql": jql, "maxResults": payload_max, "fields": ["*all"]}
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        response = requests.post(url, headers=headers, json=payload, auth=auth)
        if response.status_code != 200:
            raise Exception(f"Erreur API Jira {response.status_code}: {response.text}")

        data = response.json()
        issues = data.get("issues", [])
        if not issues:
            break

        all_issues.extend(issues)
        if limit is not None and len(all_issues) >= limit:
            break

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return all_issues[:limit] if limit is not None else all_issues


def get_field_mapping(jira_url, email, api_token):
    auth = HTTPBasicAuth(email, api_token)
    headers = {"Accept": "application/json"}
    url = f"{jira_url}/rest/api/3/field"
    response = requests.get(url, headers=headers, auth=auth)
    if response.status_code != 200:
        raise Exception(f"Erreur API Jira {response.status_code}: {response.text}")
    fields = response.json()
    return {f["id"]: f["name"] for f in fields}


def issues_to_dataframe(issues, field_mapping):
    rows = []
    for issue in issues:
        row = {"Clé de ticket": issue.get("key"), "ID de ticket": issue.get("id")}
        fields = issue.get("fields", {})
        for field_id, value in fields.items():
            col_name = field_mapping.get(field_id, field_id)
            if isinstance(value, dict):
                value = value.get("name") or value.get("displayName") or value.get("value") or str(value)
            elif isinstance(value, list):
                value = ", ".join(
                    str(v.get("name") if isinstance(v, dict) else v) for v in value
                )
            if col_name in row and (value is None or value == "") and row[col_name] not in (None, ""):
                continue
            row[col_name] = value
        rows.append(row)
    return pd.DataFrame(rows)


def extract_parent_columns(df, issues):
    parent_par_cle = {}
    for issue in issues:
        key = issue.get("key")
        parent = issue.get("fields", {}).get("parent")
        parent_par_cle[key] = parent

    def get_parent_attr(cle, extractor):
        p = parent_par_cle.get(cle)
        if not isinstance(p, dict):
            return None
        try:
            return extractor(p)
        except Exception:
            return None

    df["Clé parent"] = df["Clé de ticket"].apply(lambda c: get_parent_attr(c, lambda p: p.get("key")))
    df["Parent summary"] = df["Clé de ticket"].apply(lambda c: get_parent_attr(c, lambda p: p.get("fields", {}).get("summary")))
    df["Parent statut"] = df["Clé de ticket"].apply(lambda c: get_parent_attr(c, lambda p: p.get("fields", {}).get("status", {}).get("name")))
    df["Parent type"] = df["Clé de ticket"].apply(lambda c: get_parent_attr(c, lambda p: p.get("fields", {}).get("issuetype", {}).get("name")))
    return df


# ---------------------------------------------------------------------------
# 3. Nettoyage des champs texte riches (ADF / wiki markup -> texte lisible)
# ---------------------------------------------------------------------------

def _walk_to_text(obj):
    texts = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and "text" in node:
                texts.append(node["text"])
            if node.get("type") == "hardBreak":
                texts.append("\n")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)

    walk(obj)
    return " ".join(texts).strip()


def parse_adf_to_text(s):
    s = str(s)
    s = re.sub(r'^\{adf:[^}]*\}\s*', '', s)
    for parser in (json.loads, ast.literal_eval, lambda x: json.loads(x.replace("'", '"'))):
        try:
            return _walk_to_text(parser(s))
        except Exception:
            continue
    return s


def normalize_rich_text(val):
    if pd.isna(val):
        return val
    s = str(val).strip()
    if s.startswith("{") and '"type"' in s.replace("'", '"'):
        parsed = parse_adf_to_text(s)
        text = parsed if parsed else ""
    else:
        text = s

    text = re.sub(r'h[1-6]\.\s*', '', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'_([^_]+)_', r'\1', text)
    text = re.sub(r'\{adf:[^}]*\}', '', text)
    text = re.sub(r'[\u200b\xa0]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text if text else val


RICH_TEXT_COLS = ["Description", "Impact de l'action", "Actions", "Suivi temporel"]


def clean_rich_text_columns(df):
    for col in RICH_TEXT_COLS:
        if col in df.columns:
            df[col] = df[col].apply(normalize_rich_text)
    return df


# ---------------------------------------------------------------------------
# 4. Nettoyage structurel (colonnes vides, constantes, artefacts, doublons)
# ---------------------------------------------------------------------------

ARTEFACT_PATTERNS = [
    r"Attachment", r"Pièce jointe", r"Work log", r"Journal de travail",
    r"Comment", r"Commentaire", r"Link", r"Lien", r"Approvals",
    r"Original estimate", r"CHART", r"forms",
]

COLONNES_METIER_NON_UTILES = [
    "Progrès", "Σ Progrès",
    "Ratio du travail réel comparé à l'estimation",
    "Catégorie d'état modifiée",
    "Étiquettes",
    # Ajoutés : colonnes techniques qui passaient entre les mailles des filtres
    # (ni 100% vides, ni constantes, ni matchées par ARTEFACT_PATTERNS)
    "Classement",                 # LexoRank interne Jira (ordre de tri), aucune info métier
    "Dernier affichage",          # horodatage de dernière consultation (0.6% rempli)
    "Estimation restante",        # suivi de temps non utilisé dans ce process (0.1% rempli)
    "Σ Estimation restante",      # idem (0.1% rempli)
    "Temps consacré",             # idem (9.7% rempli)
    "Σ Temps consacré",           # idem (10.8% rempli)
]


def nettoyage_structurel(df):
    # colonnes 100% vides
    full_null_cols = df.columns[df.isna().mean() == 1.0].tolist()
    df = df.drop(columns=full_null_cols)

    # colonnes constantes (sauf Parent type, utile pour distinguer Tâche/Sous-tâche)
    constant_cols = [c for c in df.columns if df[c].nunique(dropna=True) <= 1 and c != "Parent type"]
    df = df.drop(columns=constant_cols)

    # artefacts techniques
    artefact_cols = [c for c in df.columns if any(re.search(p, str(c), re.IGNORECASE) for p in ARTEFACT_PATTERNS)]
    df = df.drop(columns=artefact_cols)

    # colonnes métier jugées non utiles
    cols_baseline = [c for c in df.columns if re.search(r"Baseline", c)]
    cols_to_drop_custom = sorted(set(cols_baseline + [c for c in COLONNES_METIER_NON_UTILES if c in df.columns]))
    df = df.drop(columns=cols_to_drop_custom)

    # doublons
    df = df.drop_duplicates().reset_index(drop=True)
    df = df.dropna(subset=["Clé de ticket"]).reset_index(drop=True)
    df = df.drop_duplicates(subset="Clé de ticket", keep="first").reset_index(drop=True)

    return df


def tronquer_si_trop_long(val, limite=32000):
    if isinstance(val, str) and len(val) > limite:
        return val[:limite] + " [...tronqué]"
    return val


# ---------------------------------------------------------------------------
# 5. Fonction principale : extraction + nettoyage complet
# ---------------------------------------------------------------------------

def extract_and_clean_cab(jql=None, verbose=True, max_tickets=None):
    """
    Extrait les tickets du projet CAB depuis l'API Jira Cloud et applique
    le nettoyage complet (champs texte riches + nettoyage structurel).

    Retourne un DataFrame propre, prêt à être filtré/analysé.
    Aucune écriture disque n'est faite : chaque appel refait une extraction fraîche.
    """
    jira_url, email, api_token, project_key = _load_config()

    if jql is None:
        jql = f"project = {project_key} ORDER BY created ASC"

    if verbose:
        print("Connexion :", jira_url)
        print("Requête JQL :", jql)

    issues = get_all_issues(jira_url, email, api_token, jql, limit=max_tickets)
    field_mapping = get_field_mapping(jira_url, email, api_token)

    if verbose:
        print(f"{len(issues)} tickets récupérés")
    assert len(issues) > 0, "Aucun ticket récupéré : vérifie PROJECT_KEY / la connexion."

    df = issues_to_dataframe(issues, field_mapping)
    df = extract_parent_columns(df, issues)
    df = clean_rich_text_columns(df)
    df = nettoyage_structurel(df)

    for col in df.select_dtypes(include=["object", "string"]).columns:
        df[col] = df[col].apply(tronquer_si_trop_long)

    if verbose:
        print("Nettoyage terminé. Dimensions finales :", df.shape)

    return df
def get_subtasks(ticket_key, verbose=False):
    """Récupère les sous-tâches directes d'un ticket via l'API Jira,
    avec leur état, assigné et dates — sans repasser par toute
    l'extraction/nettoyage du portefeuille (1 appel ciblé, léger).

    Retourne une liste de dicts (pas un DataFrame) : plus simple à
    itérer directement dans le template ticket.html.
    """
    jira_url, email, api_token, project_key = _load_config()

    jql = f'parent = "{ticket_key}" ORDER BY created ASC'
    issues = get_all_issues(jira_url, email, api_token, jql)

    if verbose:
        print(f"{len(issues)} sous-tâche(s) trouvée(s) pour {ticket_key}.")

    subtasks = []
    for issue in issues:
        fields = issue.get("fields", {})
        subtasks.append({
            "key": issue.get("key"),
            "summary": fields.get("summary", ""),
            "etat": (fields.get("status") or {}).get("name", "—"),
            "assignee": (fields.get("assignee") or {}).get("displayName", "Non assigné"),
            "priorite": (fields.get("priority") or {}).get("name", "—"),
            "created": fields.get("created", ""),
            "updated": fields.get("updated", ""),
        })

    return subtasks
def get_all_subtasks(verbose=True):
    """Récupère TOUTES les sous-tâches du projet CAB, avec leur ticket
    parent (clé + résumé), pour la page /sous-taches. Contrairement à
    get_subtasks() (utilisée dans la fiche ticket, ciblée sur UN parent),
    cette fonction ramène l'ensemble du portefeuille en un seul appel
    paginé — comme extract_and_clean_cab() pour les tâches.
    """
    jira_url, email, api_token, project_key = _load_config()

    jql = f'project = {project_key} AND issuetype = "Sous-tâche" ORDER BY created ASC'
    issues = get_all_issues(jira_url, email, api_token, jql)

    if verbose:
        print(f"{len(issues)} sous-tâche(s) récupérée(s).")

    rows = []
    for issue in issues:
        fields = issue.get("fields", {})
        parent = fields.get("parent") or {}
        parent_fields = parent.get("fields", {})

        rows.append({
            "Clé de ticket": issue.get("key"),
            "Résumé": fields.get("summary", ""),
            "État": (fields.get("status") or {}).get("name", "—"),
            "Personne assignée": (fields.get("assignee") or {}).get("displayName", "Non assigné"),
            "Priorité": (fields.get("priority") or {}).get("name", "—"),
            "Création": fields.get("created", ""),
            "Mise à jour": fields.get("updated", ""),
            "Clé parent": parent.get("key", ""),
            "Résumé parent": parent_fields.get("summary", ""),
        })

    return pd.DataFrame(rows)