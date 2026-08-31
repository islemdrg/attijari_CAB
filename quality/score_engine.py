"""
score_engine.py
================
Moteur de notation qualité des tickets CAB (Change Advisory Board).
"""

import json
import re
import sqlite3
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
from ollama import Client

# ============================================================
# VERSION DU MOTEUR
# ============================================================
# Incrémenter cette valeur force le re-scoring de tout le portefeuille au
# prochain /refresh : evaluate_tickets_checkpointed() ignore normalement un
# ticket déjà présent en base, donc une mise à jour de la logique de scoring
# ne serait jamais appliquée aux tickets déjà notés sans ce mécanisme.
ENGINE_VERSION = "6"

# ============================================================
# CONFIGURATION OLLAMA
# ============================================================

OLLAMA_HOST = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen2.5:3b"

# ============================================================
# STOCKAGE SQLITE
# ============================================================
# Écriture toujours transactionnelle, upsert par clé primaire ("Clé de
# ticket") sans doublon, schéma évolutif (ALTER TABLE ADD COLUMN).

CHECKPOINT_TABLE = "ticket_scores"


def read_checkpoint_db(db_path):
    db_path = Path(db_path)

    if not db_path.exists():
        return pd.DataFrame()

    conn = sqlite3.connect(str(db_path))
    try:
        try:
            return pd.read_sql_query(
                f'SELECT * FROM "{CHECKPOINT_TABLE}"',
                conn,
            )
        except Exception:
            # table pas encore créée (première utilisation)
            return pd.DataFrame()
    finally:
        conn.close()


def upsert_tickets_db(df, db_path):
    """
    Insère ou met à jour les tickets dans SQLite selon « Clé de ticket ».

    Les listes (preuves, recommandations) et dictionnaires sont sérialisés
    en JSON afin d'être compatibles avec SQLite.
    """
    if df.empty:
        return

    def sqlite_value(value):
        if isinstance(value, (list, dict)):
            return json.dumps(
                value,
                ensure_ascii=False,
            )

        if value is None:
            return None

        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass

        return value

    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))

    try:
        cur = conn.cursor()

        cur.execute(
            f'CREATE TABLE IF NOT EXISTS "{CHECKPOINT_TABLE}" '
            '("Clé de ticket" TEXT PRIMARY KEY)'
        )

        existing_cols = {
            row[1]
            for row in cur.execute(
                f'PRAGMA table_info("{CHECKPOINT_TABLE}")'
            )
        }

        for col in df.columns:
            if col not in existing_cols:
                cur.execute(
                    f'ALTER TABLE "{CHECKPOINT_TABLE}" '
                    f'ADD COLUMN "{col}" TEXT'
                )

        columns = list(df.columns)
        quoted_cols = ", ".join(
            f'"{col}"'
            for col in columns
        )
        placeholders = ", ".join(
            "?"
            for _ in columns
        )

        rows = [
            tuple(
                sqlite_value(value)
                for value in record
            )
            for record in df[columns].itertuples(
                index=False,
                name=None,
            )
        ]

        cur.executemany(
            f'INSERT OR REPLACE INTO "{CHECKPOINT_TABLE}" '
            f'({quoted_cols}) VALUES ({placeholders})',
            rows,
        )

        conn.commit()

    finally:
        conn.close()


# ============================================================
# BARÈME OFFICIEL /100
# ============================================================

COMPLETENESS_POINTS = {
    "Description": 3,
    "Impact de l'action": 3,
    "Criticité": 2,
    "Risque": 2,
    "Nom de l'application": 2,
    "Type du changement": 2,
    "Type de la plateforme": 2,
    "Date de mise en production": 3,
    "Date de CAB": 3,
    "Date de validation": 3,
}
# Total = 25 pts (vérifié : sum(COMPLETENESS_POINTS.values()) == 25)

FORMAT_POINTS = {
    "non_vide": 3,
    "20_mots": 3,
    "50_mots": 3,
    "deux_phrases": 3,
    "lisible": 3,
}

DESCRIPTION_RELEVANCE_POINTS = {
    "contexte": 5,
    "objectif": 5,
    "action": 7,
    "informations_concretes": 4,
    "validation_rollback": 4,
}

IMPACT_POINTS = {
    "clair": 4,
    "perimetre": 4,
    "consequence": 4,
    "absence_impact_justifiee": 3,
}

COHERENCE_POINTS = {
    "criticite": 4,
    "risque": 4,
    "impact_description": 4,
    "type_changement": 3,
    "dates": 5,
}
assert sum(COMPLETENESS_POINTS.values()) == 25, (
    f"COMPLETENESS_POINTS doit totaliser 25 pts, trouvé {sum(COMPLETENESS_POINTS.values())}"
)
assert sum(FORMAT_POINTS.values()) == 15, "FORMAT_POINTS doit totaliser 15 pts"
assert sum(DESCRIPTION_RELEVANCE_POINTS.values()) == 25, "DESCRIPTION_RELEVANCE_POINTS doit totaliser 25 pts"
assert sum(IMPACT_POINTS.values()) == 15, "IMPACT_POINTS doit totaliser 15 pts"
assert sum(COHERENCE_POINTS.values()) == 20, "COHERENCE_POINTS doit totaliser 20 pts"
# Le barème "bonus" a été retiré à la demande du métier : les 100 points se
# répartissent uniquement sur complétude / format / pertinence / impact /
# cohérence.

# ============================================================
# CHAMPS DATE
# ============================================================

DATE_FIELDS = [
    "Date de CAB",
    "Date de validation",
    "Date de mise en production",
]

# ============================================================
# METADONNEES JIRA CONSERVEES A COTE DU SCORE
# ============================================================
# Recopiées explicitement dans la ligne enregistrée en base pour permettre
# le filtrage du dashboard par état/année/assigné (le score seul ne suffit
# pas). Certains libellés de champ varient selon la config Jira : on
# accepte plusieurs alias et on normalise vers un nom canonique.
JIRA_META_ALIASES = {
    "État": ["État", "Etat", "Statut", "Status"],
    # "Catégorie d'état" = regroupement Jira (À faire / En cours / Terminé)
    # au-dessus de l'état précis du workflow : utile pour un filtre large.
    "Catégorie d'état": ["Catégorie d'état", "Catégorie d’état"],
    "Type de ticket": ["Type de ticket", "Type de demande", "Issue Type"],
    "Personne assignée": [
        "Personne assignée",
        "Responsable",
        "Assigné à",
        "Assignee",
    ],
    "Rapporteur": ["Rapporteur", "Reporter"],
    "Priorité": ["Priorité", "Priority"],
    "Création": ["Création", "Créé", "Created"],
    "Mise à jour": ["Mise à jour", "Updated"],
    "Clé parent": ["Clé parent", "Parent"],
    "Parent type": ["Parent type"],
    "Parent summary": ["Parent summary"],
    "Parent statut": ["Parent statut"],
    "Criticité": ["Criticité"],
    "Risque": ["Risque"],
    "Type du changement": ["Type du changement"],
    "Type de la plateforme": ["Type de la plateforme"],
    "Nom de l'application": ["Nom de l'application", "Nom de l’application"],
    # apostrophe typographique réellement utilisée par ce Jira : "Type de l’action"
    "Type de l'action": ["Type de l'action", "Type de l’action"],
    "Nature": ["Nature"],
    "Date de réception": ["Date de réception"],
    "Résolution": ["Résolution"],
    "Sous-tâches": ["Sous-tâches"],
    "Date de CAB": ["Date de CAB"],
    "Date de validation": ["Date de validation"],
    "Date de mise en production": ["Date de mise en production"],
}

# Colonnes canoniques réellement écrites en base à côté des scores.
JIRA_META_FIELDS = list(JIRA_META_ALIASES.keys())


def extract_jira_meta(ticket):
    """Renvoie les métadonnées Jira d'un ticket sous leurs noms canoniques.

    Un champ absent du ticket n'est PAS ajouté au résultat (on évite ainsi
    d'écraser en base une valeur déjà connue par un None).
    """

    ticket = dict(ticket or {})
    meta = {}

    for canonical, aliases in JIRA_META_ALIASES.items():
        for alias in aliases:
            if alias in ticket and not _empty(ticket.get(alias)):
                meta[canonical] = _text(ticket.get(alias))
                break

    return meta


def upsert_metadata_db(df, db_path, fields=None):
    """Met à jour UNIQUEMENT les métadonnées Jira des tickets déjà notés.

    Indispensable pour le rattrapage : les tickets notés avant l'ajout des
    métadonnées existent déjà en base et ne seront jamais réévalués (le
    checkpoint les ignore). On fait donc un UPDATE ciblé colonne par colonne
    plutôt qu'un INSERT OR REPLACE, qui remettrait à NULL tous les scores
    déjà calculés.

    Retourne le nombre de tickets mis à jour.
    """

    if df is None or df.empty or "Clé de ticket" not in df.columns:
        return 0

    db_path = Path(db_path)

    if not db_path.exists():
        return 0

    if fields is None:
        fields = JIRA_META_FIELDS

    # On ne garde que les colonnes réellement présentes dans l'extraction.
    present = [
        field
        for field in fields
        if field in df.columns
    ]

    if not present:
        return 0

    conn = sqlite3.connect(str(db_path))

    try:
        cur = conn.cursor()

        try:
            existing_cols = {
                row[1]
                for row in cur.execute(
                    f'PRAGMA table_info("{CHECKPOINT_TABLE}")'
                )
            }
        except Exception:
            return 0

        if not existing_cols:
            return 0

        for field in present:
            if field not in existing_cols:
                cur.execute(
                    f'ALTER TABLE "{CHECKPOINT_TABLE}" '
                    f'ADD COLUMN "{field}" TEXT'
                )

        set_clause = ", ".join(
            f'"{field}" = ?'
            for field in present
        )

        rows = []

        for _, row in df.iterrows():

            key = str(row.get("Clé de ticket", "")).strip()

            if not key:
                continue

            values = []

            for field in present:
                value = row.get(field)

                try:
                    if pd.isna(value):
                        value = None
                except (TypeError, ValueError):
                    pass

                values.append(
                    None
                    if value is None
                    else str(value)
                )

            rows.append(tuple(values) + (key,))

        if not rows:
            return 0

        cur.executemany(
            f'UPDATE "{CHECKPOINT_TABLE}" SET {set_clause} '
            'WHERE TRIM("Clé de ticket") = ?',
            rows,
        )

        updated = cur.rowcount

        conn.commit()

        return max(0, updated)

    finally:
        conn.close()


# ============================================================
# OUTILS GENERAUX
# ============================================================

def _empty(value):
    if value is None:
        return True

    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass

    return str(value).strip().lower() in {
        "",
        "nan",
        "none",
        "null",
        "n/a",
        "na",
        "-",
        "non renseigné",
        "non renseigne",
    }


def _text(value):
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass

    return str(value).strip()


def _word_count(text):
    return len(re.findall(r"\b[\wÀ-ÿ'-]+\b", _text(text)))


def _sentence_count(text):
    text = _text(text)

    if not text:
        return 0

    sentences = re.split(r"[.!?]+", text)

    return len([
        sentence
        for sentence in sentences
        if len(sentence.strip()) >= 10
    ])


def _parse_date(value):
    if _empty(value):
        return None

    value = str(value).strip()

    formats = [
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            pass

    try:
        parsed = pd.to_datetime(
            value,
            dayfirst=True,
            errors="coerce"
        )
        if pd.notna(parsed):
            return parsed.to_pydatetime()

    except Exception:
        pass

    return None


# ============================================================
# 1. COMPLETUDE
# ============================================================

def calculate_completeness(ticket):
    score = 0
    missing = []
    details = {}

    for field, points in COMPLETENESS_POINTS.items():

        present = not _empty(ticket.get(field))

        if present:
            score += points
            details[field] = {
                "present": True,
                "points": points,
            }
        else:
            missing.append(field)

            details[field] = {
                "present": False,
                "points": 0,
            }

    return score, missing, details


# ============================================================
# 2. FORMAT DESCRIPTION
# ============================================================

def calculate_description_format(ticket):

    description = _text(ticket.get("Description"))

    result = {
        "non_vide": False,
        "20_mots": False,
        "50_mots": False,
        "deux_phrases": False,
        "lisible": False,
    }

    if not description:
        return 0, result

    result["non_vide"] = True

    words = _word_count(description)

    if words >= 20:
        result["20_mots"] = True

    if words >= 50:
        result["50_mots"] = True

    if _sentence_count(description) >= 2:
        result["deux_phrases"] = True

    lower = description.lower()

    vague_values = {
        "test",
        "ras",
        "néant",
        "aucun",
        "n/a",
        "na",
        "aucune",
        "ok",
    }

    is_json = (
        description.startswith("{")
        and description.endswith("}")
    )

    is_attachment = (
        "pièce jointe" in lower
        or "piece jointe" in lower
        or "attachment" in lower
    )

    is_title_only = (
        words < 8
        and _sentence_count(description) <= 1
    )

    is_vague = lower.strip() in vague_values

    result["lisible"] = not (
        is_json
        or is_attachment
        or is_title_only
        or is_vague
    )

    score = sum(
        FORMAT_POINTS[key]
        for key, value in result.items()
        if value
    )

    return score, result


# ============================================================
# 3. PAYLOAD ENVOYÉ AU LLM
# ============================================================

def _ticket_payload(ticket):

    fields = [
        "Clé de ticket",
        "Résumé",
        "Description",
        "Impact de l'action",
        "Criticité",
        "Risque",
        "Nom de l'application",
        "Type du changement",
        "Type de la plateforme",
        "Date de mise en production",
        "Date de CAB",
        "Date de validation",
    ]

    payload = {}

    for field in fields:
        payload[field] = (
            _text(ticket.get(field))
            if not _empty(ticket.get(field))
            else "Non renseigné"
        )

    return payload


# ============================================================
# 4. SCHEMA LLM
# ============================================================
# NOUVEAU : ajout de "analyse_impact", distinct de "analyse". "analyse"
# reste "type": "string" (la structuration en 2 phrases est imposée par le
# PROMPT ci-dessous, pas par le schéma JSON qui ne valide qu'un type).

LLM_SCHEMA = {

    "type": "object",

    "properties": {

        "pertinence_description": {

            "type": "object",

            "properties": {

                "contexte": {"type": "boolean"},
                "objectif": {"type": "boolean"},
                "action": {"type": "boolean"},
                "informations_concretes": {"type": "boolean"},
                "validation_rollback": {"type": "boolean"},

            },

            "required": [
                "contexte",
                "objectif",
                "action",
                "informations_concretes",
                "validation_rollback",
            ],

            "additionalProperties": False,
        },

        "qualite_impact": {

            "type": "object",

            "properties": {

                "clair": {"type": "boolean"},
                "perimetre": {"type": "boolean"},
                "consequence": {"type": "boolean"},
                "absence_impact_justifiee": {"type": "boolean"},

            },

            "required": [
                "clair",
                "perimetre",
                "consequence",
                "absence_impact_justifiee",
            ],

            "additionalProperties": False,
        },

        "coherence": {

            "type": "object",

            "properties": {

                "criticite": {
                    "type": "boolean"
                },

                "risque": {
                    "type": "boolean"
                },

                "impact_description": {
                    "type": "boolean"
                },

                "type_changement": {
                    "type": "boolean"
                },

            },

            "required": [
                "criticite",
                "risque",
                "impact_description",
                "type_changement",
            ],

            "additionalProperties": False,
        },

        "analyse": {
            "type": "string"
        },

        
        "preuves": {
            "type": "array",
            "items": {
                "type": "string"
            },
            "maxItems": 5,
        },

        "recommandations": {
            "type": "array",
            "items": {
                "type": "string"
            },
            "maxItems": 5,
        },
    },

    "required": [
        "pertinence_description",
        "qualite_impact",
        "coherence",
        "analyse",
        "preuves",
        "recommandations",
    ],

    "additionalProperties": False,
}


# ============================================================
# 5. PROMPT SEMANTIQUE
# ============================================================
# NOUVEAU : la section finale du prompt a été réécrite pour (1) forcer une
# structure en deux phrases NON contradictoires dans "analyse" (fini le
# "Le ticket décrit précisément X... Cependant, il ne fournit pas de
# détails sur X" qui se contredit lui-même), et (2) ajouter une vraie
# section dédiée à l'analyse d'impact, distincte de l'analyse générale.
PROMPT = """
Tu es un analyste senior en Change Management bancaire, avec une expertise
en lecture critique de documentation technique. Ta mission n'est PAS de
repérer des mots-clés : c'est de LIRE et COMPRENDRE le contenu du ticket
comme le ferait un relecteur humain expérimenté, puis de porter un jugement
qualitatif sur ce texte précis.

============================================================
METHODE DE LECTURE OBLIGATOIRE (avant toute évaluation)
============================================================

Avant de répondre à un seul critère, effectue mentalement ces 3 étapes :

ETAPE 1 — LECTURE INTEGRALE
Lis la Description et l'Impact comme un texte continu, du début à la fin,
sans t'arrêter au premier mot-clé rencontré. Une information peut être
exprimée n'importe où dans le texte, de façon détournée, implicite, ou
répartie sur plusieurs phrases. Une seule phrase peut contenir plusieurs
informations à la fois (contexte + action + information technique
ensemble, par exemple) — ne juge jamais un critère en isolant une phrase
du reste du texte.

ETAPE 2 — ANALYSE SEMANTIQUE (pas lexicale)
Pour chaque critère, demande-toi : "Est-ce qu'un collègue qui n'a JAMAIS vu
ce ticket pourrait comprendre, uniquement à partir de ce texte, QUOI va
changer, POURQUOI, et COMMENT on vérifie que ça a fonctionné ?" Un critère
est vrai seulement si la réponse est oui pour CE critère précis — jamais
parce qu'un mot particulier apparaît dans le texte. Inversement, l'absence
d'un mot attendu ne signifie pas automatiquement que le critère est faux :
le sens peut être présent sous une autre formulation, une métaphore
technique, ou une tournure implicite.

ETAPE 3 — JUGEMENT DE QUALITE REDACTIONNELLE
Au-delà du simple oui/non par critère, évalue la qualité rédactionnelle
réelle du texte : est-il précis ou vague, structuré ou décousu, technique
et vérifiable ou approximatif ? Un ticket peut cocher tous les critères
booléens tout en restant rédigé de façon minimaliste ou floue — cette
nuance doit apparaître explicitement dans le texte de l'analyse (voir plus
bas), pas seulement dans les booléens bruts.

============================================================
REGLES GENERALES
============================================================

- Analyse uniquement le ticket fourni, jamais en comparaison avec d'autres.
- Ne donne jamais un score numérique.
- Ne déduis jamais qu'un critère est présent simplement parce qu'un mot-clé
  apparaît : le critère doit être réellement satisfait par le SENS du
  contenu, indépendamment des mots exacts employés.
- Une information vague, générique ou creuse ("mise à jour de
  l'application", sans autre précision) ne suffit pas, même si elle
  mentionne le bon sujet en apparence.
- Si les informations ne permettent pas de conclure avec certitude, réponds
  false plutôt que de supposer.
- Utilise uniquement les informations présentes dans le ticket ; ne
  complète jamais une information manquante avec tes connaissances
  générales, même si elle serait plausible pour ce type de changement.
- Réponds exclusivement avec le JSON demandé.

============================================================
PERTINENCE DE LA DESCRIPTION — 25 POINTS
============================================================

contexte :
true uniquement si, après lecture complète, on comprend POURQUOI ce
changement est nécessaire maintenant (incident, alerte, fin de support,
demande métier, audit, dette technique...) — peu importe la formulation
utilisée pour l'exprimer, tant que le sens est réellement présent.

objectif :
true uniquement si l'on comprend clairement CE QUE le changement doit
accomplir, le résultat concret recherché — même si le mot "objectif"
n'apparaît jamais littéralement dans le texte.

action :
true uniquement si l'action technique ou fonctionnelle réalisée est décrite
avec assez de précision pour qu'un tiers comprenne concrètement ce qui va
être modifié (pas juste "mise à jour" ou "correction" sans préciser quoi
exactement ni comment).

informations_concretes :
true si la description contient des éléments vérifiables et spécifiques :
nom exact d'application/serveur/composant, numéro de version, volumétrie,
environnement précis, paramètre technique, flux de données identifié —
pas une mention vague du type "l'application" sans nom.

validation_rollback :
true si un test, une validation, un contrôle post-déploiement, une
vérification, un rollback, un plan de reprise ou une procédure équivalente
est réellement décrit dans le texte (pas juste évoqué en un mot isolé sans
aucun détail sur comment il serait mené).

============================================================
QUALITE DE L'IMPACT — 15 POINTS
============================================================

clair :
true si le texte de l'impact permet de comprendre, sans ambiguïté, ce qui
va concrètement être affecté par le changement.

perimetre :
true si les entités concrètement concernées (services, applications,
utilisateurs, agences, systèmes, processus) sont identifiées nommément ou
de façon suffisamment précise pour être délimitées, pas juste évoquées en
termes vagues.

consequence :
true si une conséquence concrète est décrite en cas de problème ou de
changement d'état : indisponibilité, ralentissement, blocage, dégradation,
gain, perte, etc. — pas une simple affirmation qu'il y a "un impact" sans
préciser lequel.

absence_impact_justifiee :
true UNIQUEMENT si le ticket indique explicitement qu'il n'y a pas
d'impact ET donne une justification qui tient logiquement compte du
contexte du changement décrit (pas une affirmation creuse type "aucun
impact" sans aucune explication).

============================================================
COHERENCE — 15 POINTS SEMANTIQUES
============================================================

criticite :
true si la criticité indiquée est cohérente avec la gravité réelle du
changement telle que décrite dans la description et l'impact (un
changement mineur de configuration déclaré "Critique" sans justification,
ou l'inverse, est une incohérence à signaler par false).

risque :
true si le niveau de risque indiqué est cohérent avec la nature réelle du
changement décrit dans le contenu du ticket.

impact_description :
true si l'impact décrit correspond réellement et logiquement à la
description et à l'action du changement (pas de contradiction entre ce qui
est fait et ce qui est annoncé comme conséquence).

type_changement :
true si Standard/Urgent/Normal/etc. est cohérent avec le contenu réel décrit
dans le ticket (un changement présenté comme urgent sans aucun contexte
d'urgence explicite est incohérent).

============================================================
REGLE IMPORTANTE
============================================================

Un champ présent n'est PAS automatiquement de bonne qualité.

Exemple :

Description = "Déploiement application X"

Le champ est présent pour la complétude, mais cela ne suffit pas pour
valider : contexte, objectif, action précise, informations concrètes ou
validation.

Analyse donc chaque critère indépendamment, sur la base du sens réel du
texte et non de sa simple présence.

============================================================
ANALYSE — TEXTE LIBRE, REDACTION EXIGEANTE ET NON CONTRADICTOIRE
============================================================

Le champ "analyse" DOIT être une VRAIE analyse rédigée, pas un résumé
mécanique des critères validés. Rédige exactement deux phrases denses,
séparées par un retour à la ligne :

Phrase 1 (ce qui fonctionne réellement) :
Cite un élément CONCRET et VÉRIFIABLE tiré du ticket (nom exact
d'application, version, action précise, composant, criticité déclarée) qui
prouve ce qui est réellement bien renseigné. N'affirme jamais qu'un critère
est "indiqué" sans citer l'élément exact qui le prouve — montre-le PAR
L'EXEMPLE.

Phrase 2 (ce qui manque réellement, avec impact concret) :
Cite le critère précis qui manque (contexte, objectif, informations
concrètes, validation/rollback...) et explique CONCRÈTEMENT en quoi son
absence complique la lecture, la compréhension ou la validation du
changement pour quelqu'un qui découvre ce ticket. Ne répète jamais dans
cette phrase un critère déjà validé dans la phrase 1 — relis-toi avant de
répondre pour vérifier qu'il n'y a aucune contradiction entre les deux
phrases (un même critère ne peut pas être "acquis" en phrase 1 et
"manquant" en phrase 2).

Interdiction absolue :
- de répondre par true, false, un score, ou un mot seul ;
- de réutiliser une formulation générique interchangeable d'un ticket à
  l'autre (si la phrase pourrait être copiée-collée sur un autre ticket
  sans la changer, elle est trop générique — réécris-la en citant du
  contenu réel de CE ticket) ;
- de mentionner un critère sans donner l'élément textuel qui le prouve ou
  qui explique concrètement son absence.

Exemple de forme ATTENDUE :
"Le ticket précise que la mise à niveau du Firewall Frontal Forcepoint 2105
vers la version 6.10.18 est motivée par une fin de support du firmware
actuel, avec un plan de rollback décrit en cas d'échec de la mise à jour.
En revanche, aucune information n'indique quelles agences ou quels flux
réseau seraient temporairement affectés pendant la fenêtre de maintenance,
ce qui rend difficile l'évaluation du périmètre réel de l'interruption."

============================================================
RECOMMANDATIONS — SPECIFIQUES A CE TICKET, JAMAIS GENERIQUES
============================================================

Les recommandations (liste de 3 à 5 phrases) doivent être des actions
d'écriture concrètes, adressées à l'émetteur DE CE ticket précis. Chaque
recommandation doit :

1. Nommer explicitement CE QUI est absent ou insuffisant dans CE ticket
   (jamais une catégorie générique comme "préciser le contexte" appliquée
   uniformément à tous les tickets qui manquent de contexte).
2. Suggérer CONCRÈTEMENT quoi ajouter, en t'appuyant sur les éléments déjà
   présents dans le ticket (nom d'application, type de changement,
   criticité déclarée) pour rendre la suggestion pertinente à CE cas
   précis.
3. Être formulée comme un conseil direct à la personne qui a rédigé ce
   ticket précis, jamais un conseil de bonnes pratiques générales valable
   pour n'importe quel autre ticket.

Exemple de recommandation INTERDITE (trop générique) :
"Préciser le contexte du changement."

Exemple de recommandation ATTENDUE (spécifique au ticket) :
"Préciser depuis quand le firmware actuel du Forcepoint 2105 n'est plus
supporté par l'éditeur, information qui justifierait la criticité 'Élevée'
actuellement déclarée sans qu'aucune urgence ne soit expliquée dans le
texte."

============================================================
PREUVES — CITATIONS FACTUELLES UNIQUEMENT
============================================================

Chaque preuve (liste de 3 à 5 phrases courtes) doit citer un élément
FACTUEL et SPÉCIFIQUE trouvé dans le texte du ticket, jamais une catégorie
générique du type "informations concrètes mentionnées". Exemple : "Le
serveur SRV-cdp-gateway-01 est identifié comme composant impacté par la
mise à jour."

============================================================
"""

# ============================================================
# 6. APPEL QWEN
# ============================================================

def _call_llm(ticket, client_instance=None, retries=2):

    client = client_instance or Client(
        host=OLLAMA_HOST,
        timeout=180
    )

    last_exc = None

    # Retries : quand le LLM local (Ollama) est momentanément indisponible
    # ou renvoie un timeout, l'ancien code abandonnait immédiatement et le
    # ticket retombait sur un score sémantique à 0 partout -> beaucoup de
    # tickets finissaient avec des scores identiques. On retente 2 fois
    # avant d'abandonner pour de vrai.
    for attempt in range(retries + 1):

        try:

            response = client.chat(

                model=OLLAMA_MODEL,

                messages=[
                    {
                        "role": "system",
                        "content": PROMPT,
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            _ticket_payload(ticket),
                            ensure_ascii=False
                        ),
                    },
                ],

                format=LLM_SCHEMA,

                options={
                    "temperature": 0,
                    "seed": 42,
                    "num_ctx": 4096,
                    "num_predict": 800,
                },
            )

            return json.loads(
                response["message"]["content"]
            )

        except Exception as exc:

            last_exc = exc

    raise last_exc


# ============================================================
# 7. SCORE DES BLOCS SEMANTIQUES
# ============================================================

def _points(block, weights):

    return sum(
        points
        for criterion, points in weights.items()
        if block.get(criterion) is True
    )


# ============================================================
# 8. COHERENCE DES DATES
# ============================================================

def calculate_date_coherence(ticket):

    debut = _parse_date(
        ticket.get("Date de CAB")
    )

    fin = _parse_date(
        ticket.get("Date de validation")
    )

    production = _parse_date(
        ticket.get("Date de mise en production")
    )

    details = []

    # Les 5 points sont attribués uniquement si les dates
    # disponibles permettent de vérifier une chronologie cohérente.

    if not debut or not fin:
        return 0, [
            "Date de CAB ou date de validation manquante."
        ]

    if debut > fin:
        return 0, [
            "Incohérence : la date de CAB est postérieure à la date de validation."
        ]

    score = 5

    details.append(
        "Date de CAB antérieure ou égale à la date de validation."
    )

    if production:

        if production < debut:
            score = 0
            details.append(
                "Incohérence : mise en production avant la date de CAB."
            )

        elif production > fin:
            score = 0
            details.append(
                "Incohérence : mise en production après la date de validation."
            )

        else:
            details.append(
                "Mise en production comprise dans la période du changement."
            )

    return score, details


# ============================================================
# 8bis. RESOLUTION DE LA SOURCE DE DESCRIPTION (ticket vs parent)
# ============================================================
# Un ticket "Tâche" de type vote individuel a souvent sa propre Description
# vide, alors que l'info existe sur son ticket parent. Règle appliquée
# systématiquement : Description du ticket si non vide, sinon celle du
# parent, sinon absente. La source utilisée est tracée en sortie
# ("source_description") pour rester vérifiable.

def _resolve_description(ticket):

    own = _text(ticket.get("Description"))
    if own:
        return own, "ticket"

    parent_desc = _text(ticket.get("Parent description"))
    if parent_desc:
        return parent_desc, "parent"

    return "", "absente"


def _resolve_impact(ticket):

    own = _text(ticket.get("Impact de l'action"))
    if own:
        return own, "ticket"

    parent_impact = _text(ticket.get("Parent impact"))
    if parent_impact:
        return parent_impact, "parent"

    return "", "absente"


# ============================================================
# 8ter. ANALYSEUR HEURISTIQUE (utilisé quand le LLM est indisponible)
# ============================================================
# Repli déterministe : chaque ticket reste noté sur le contenu réel de SA
# description (au lieu d'un score à 0 partout) quand Ollama ne répond pas.
# Moins fin que l'analyse sémantique du LLM, mais toujours différencié.

_MARQUEURS_CONTEXTE = [
    r"suite à", r"suite aux", r"en raison de", r"à la suite de",
    r"suite au", r"constat", r"alerte", r"probl[eè]me", r"incident",
    r"actuellement", r"besoin de", r"demande de",
]

_MARQUEURS_OBJECTIF = [
    r"objectif", r"afin de", r"dans le but de", r"permettre de",
    r"vise à", r"but est de", r"pour améliorer", r"pour garantir",
]

_MARQUEURS_ACTION = [
    r"mettre à jour", r"mise à jour", r"déployer", r"déploiement",
    r"configurer", r"configuration", r"installer", r"installation",
    r"modifier", r"modification", r"corriger", r"correctif",
    r"augmenter", r"ajouter", r"ajout", r"supprimer", r"suppression",
    r"migrer", r"migration", r"activer", r"désactiver", r"restaurer",
    r"redémarrer", r"remplacer",
]

_MARQUEURS_VALIDATION = [
    r"rollback", r"plan de retour", r"plan de reprise", r"\btest\b",
    r"validation", r"vérification", r"recette", r"contrôle post",
    r"restauration", r"sauvegarde avant", r"backup avant", r"retour arrière",
]

_MARQUEURS_INFO_CONCRETE = [
    r"\d+\.\d+",  # version x.y
    r"\bv\d+\b",
    r"\d+\s?(go|mo|vcpu|cpu|%|ms)\b",
    r"\b[A-Z]{2,}-[A-Za-z0-9]+\b",  # ex: SRV-cdp-gateway-01
    r"\b\d{2,}\b",
]

_MARQUEURS_PERIMETRE = [
    r"application", r"service", r"utilisateur", r"agence", r"système",
    r"processus", r"client", r"serveur", r"module",
]

_MARQUEURS_CONSEQUENCE = [
    r"indisponibilit", r"ralentissement", r"blocage", r"dégrad",
    r"interruption", r"coupure", r"perte", r"gain", r"amélioration de",
]

_MARQUEURS_ABSENCE_JUSTIFIEE = [
    r"aucun impact.{0,40}(car|puisque|étant donné|en raison)",
    r"pas d'impact.{0,40}(car|puisque|étant donné|en raison)",
    r"sans impact.{0,40}(car|puisque|étant donné)",
]


def _match_any(patterns, text):
    return any(re.search(p, text) for p in patterns)


def heuristic_relevance(description):
    """Repli déterministe pour la pertinence (25 pts) quand le LLM ne
    répond pas. Basé sur le contenu réel du ticket, donc différencié
    ticket par ticket (contrairement à l'ancien fallback qui mettait tout
    le monde à False)."""

    text = description.lower()

    if not text:
        return {
            "contexte": False,
            "objectif": False,
            "action": False,
            "informations_concretes": False,
            "validation_rollback": False,
        }

    return {
        "contexte": _match_any(_MARQUEURS_CONTEXTE, text),
        "objectif": _match_any(_MARQUEURS_OBJECTIF, text),
        "action": _match_any(_MARQUEURS_ACTION, text),
        "informations_concretes": _match_any(_MARQUEURS_INFO_CONCRETE, text),
        "validation_rollback": _match_any(_MARQUEURS_VALIDATION, text),
    }


def heuristic_impact(impact_text, description_text):
    """Repli déterministe pour la qualité de l'impact (15 pts)."""

    impact_l = impact_text.lower()
    combined = (impact_text + " " + description_text).lower()

    if not impact_l:
        return {
            "clair": False,
            "perimetre": False,
            "consequence": False,
            "absence_impact_justifiee": False,
        }

    return {
        "clair": len(impact_l.strip()) >= 15,
        "perimetre": _match_any(_MARQUEURS_PERIMETRE, combined),
        "consequence": _match_any(_MARQUEURS_CONSEQUENCE, combined),
        "absence_impact_justifiee": _match_any(_MARQUEURS_ABSENCE_JUSTIFIEE, impact_l),
    }


# ============================================================
# 8quater. DISPONIBILITE DU LLM (health-check rapide)
# ============================================================
# Un seul health-check (< 2s) avant de lancer un lot, plutôt qu'un retry
# lent ticket par ticket : si Ollama ne répond pas, on bascule tout de
# suite sur l'analyseur heuristique pour tout le lot.

def check_llm_availability(timeout=2):
    try:
        import requests as _requests
        resp = _requests.get(f"{OLLAMA_HOST}/api/tags", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


# ============================================================
# 8quinquies. EXPLICATIONS LISIBLES (page détail ticket)
# ============================================================
# Libellés humains des critères booléens, utilisés pour composer les
# textes "explication_*" affichés sur la fiche ticket.

_CRITERION_LABELS = {
    # pertinence description
    "contexte": "Contexte / problème à l'origine du changement",
    "objectif": "Objectif du changement",
    "action": "Action technique/fonctionnelle précise",
    "informations_concretes": "Informations concrètes (applis, versions, flux...)",
    "validation_rollback": "Tests / validation / plan de rollback",
    # impact
    "clair": "Impact clairement renseigné",
    "perimetre": "Périmètre (applications/utilisateurs concernés)",
    "consequence": "Conséquence concrète décrite",
    "absence_impact_justifiee": "Absence d'impact justifiée",
    # coherence
    "criticite": "Criticité cohérente avec le contenu",
    "risque": "Risque cohérent avec le contenu",
    "impact_description": "Impact cohérent avec la description",
    "type_changement": "Type de changement cohérent",
    "dates": "Chronologie des dates cohérente",
    # format
    "non_vide": "Description non vide",
    "20_mots": "Au moins 20 mots",
    "50_mots": "Au moins 50 mots",
    "deux_phrases": "Au moins deux phrases",
    "lisible": "Contenu lisible (pas un simple mot-clé ou une pièce jointe)",
}


def _explain_block(block, points_map=None):
    """points_map : dict critère -> points, pour afficher le barème exact
    à côté de chaque critère (répond à "sur quelle base c'est évalué" —
    avant, seul le libellé était visible, jamais le poids en points)."""

    if not block:
        return "Non évalué."

    def _fmt(key):
        label = _CRITERION_LABELS.get(key, key)
        if points_map and key in points_map:
            return f"{label} (+{points_map[key]} pts)"
        return label

    ok = [_fmt(k) for k, v in block.items() if v]
    ko = [_fmt(k) for k, v in block.items() if not v]

    parts = []
    if ok:
        parts.append("Points obtenus : " + "; ".join(ok) + ".")
    if ko:
        parts.append("Points perdus : " + "; ".join(ko) + ".")

    return " ".join(parts) if parts else "Non évalué."


def _explain_completeness(missing, details):
    if not missing:
        return "Tous les champs obligatoires sont renseignés."
    return "Champs obligatoires manquants : " + ", ".join(missing) + "."


# ============================================================
# 9. SCORE COMPLET D'UN TICKET
# ============================================================

_RECOMMENDATION_TEXT = {
    "contexte": "Préciser le contexte : pourquoi ce changement est nécessaire (incident, alerte, demande métier...).",
    "objectif": "Indiquer clairement l'objectif du changement (ce qu'on cherche à obtenir).",
    "action": "Décrire précisément l'action technique/fonctionnelle réalisée.",
    "informations_concretes": "Ajouter des informations concrètes (versions, volumétrie, noms de serveurs/applications, %...).",
    "validation_rollback": "Décrire le plan de test/validation et le plan de rollback en cas d'échec.",
    "clair": "Renseigner un impact clair et non ambigu.",
    "perimetre": "Préciser le périmètre impacté (applications, services, utilisateurs concernés).",
    "consequence": "Décrire la conséquence concrète en cas de problème (indisponibilité, dégradation...).",
}


def _generate_recommendations(
    missing,
    format_details,
    relevance_block,
    impact_block,
    description_source,
):
    """Recommandations heuristiques PROPRES À CE TICKET : dépendent de ses
    champs manquants et de son contenu réel, donc diffèrent d'un ticket à
    l'autre (contrairement à l'ancien fallback qui renvoyait toujours
    ['Relancer l'analyse sémantique du ticket.'] pour tout le monde)."""

    recos = []

    if description_source == "absente":
        recos.append(
            "Renseigner une description sur ce ticket (ou sur son ticket "
            "parent) : aucune information exploitable n'est disponible "
            "actuellement pour l'évaluer."
        )
        return recos

    if description_source == "parent":
        recos.append(
            "Ce ticket n'a pas sa propre description : envisager d'en "
            "ajouter une directement dessus plutôt que de dépendre "
            "uniquement de celle du ticket parent."
        )

    for field in missing[:3]:
        recos.append(f"Renseigner le champ obligatoire manquant : {field}.")

    if format_details and not format_details.get("50_mots", True):
        recos.append(
            "Étoffer la description : viser au moins 50 mots pour qu'elle "
            "soit exploitable en revue."
        )

    for key in ("contexte", "objectif", "action", "informations_concretes", "validation_rollback"):
        if not relevance_block.get(key, True):
            recos.append(_RECOMMENDATION_TEXT[key])

    for key in ("clair", "perimetre", "consequence"):
        if not impact_block.get(key, True):
            recos.append(_RECOMMENDATION_TEXT[key])

    if not recos:
        recos.append(
            "Ticket globalement complet sur les critères vérifiables sans "
            "LLM : vérifier manuellement la cohérence entre criticité/"
            "risque/type de changement et le contenu décrit."
        )

    return recos[:6]


def _deduplicate_similar_phrases(items, similarity_threshold=0.72):
    """Filtre les phrases quasi-identiques d'une liste (recommandations,
    preuves) en ne gardant que la première occurrence de chaque idée.

    Un petit modèle (qwen2.5:3b) reformule parfois la même idée avec des
    mots différents (ex: "préciser les applications impactées" et
    "préciser les applications satellite impactées") malgré la consigne
    anti-répétition du PROMPT : filet de sécurité mécanique, indépendant de
    la qualité du prompt.
    """

    def _normalize(text):
        text = str(text).lower().strip()
        text = re.sub(r"[^\w\sàâäéèêëïîôöùûüç]", " ", text)
        return re.sub(r"\s+", " ", text)

    kept = []
    kept_normalized = []

    for item in items or []:
        text = str(item).strip()
        if not text:
            continue

        normalized = _normalize(text)
        is_duplicate = any(
            SequenceMatcher(None, normalized, existing).ratio() >= similarity_threshold
            for existing in kept_normalized
        )

        if not is_duplicate:
            kept.append(text)
            kept_normalized.append(normalized)

    return kept


def _clean_text_items(items):
    """Filtre/nettoie une liste destinée à preuves/recommandations : le LLM
    (petit modèle 3B) glisse parfois un booléen brut (true/false) dans ces
    listes malgré le schéma JSON qui impose des chaînes — même défaut que le
    bug "Analyse: false", mais au niveau d'un élément de liste plutôt que du
    champ entier. On élimine tout élément booléen ou qui se réduit à
    "true"/"false"/vide une fois converti en texte."""

    cleaned = []

    for item in items or []:

        if isinstance(item, bool):
            continue

        text = str(item).strip()

        if not text or text.lower() in {"true", "false", "none", "null"}:
            continue

        cleaned.append(text)

    return cleaned


def evaluate_ticket_quality(
    ticket,
    client_instance=None,
    llm_available=None,
):
    """
    llm_available :
        - None  -> vérifie la disponibilité d'Ollama pour CE ticket (usage
                   isolé").
        - True  -> tente le LLM (utilisé par evaluate_tickets*, qui a déjà
                   vérifié UNE fois pour tout le lot, pour ne pas perdre de
                   temps à re-vérifier ticket par ticket).
        - False -> saute directement à l'analyseur heuristique (rapide).
    """

    ticket = dict(ticket or {})

    key = _text(
        ticket.get("Clé de ticket")
    )

    summary = _text(
        ticket.get("Résumé")
    )

    # ancien ref
    summary_lower = summary.lower().strip()

    ancienne_reference_terms = {
        "ancien référence",
        "ancienne référence",
        "ancien reference",
        "ancienne reference",
        "old reference",
        "historique",
    }

    if summary_lower in ancienne_reference_terms:
        return {
            "Clé de ticket": key,
            "Résumé": summary,
            "score_completude": None,
            "score_format": None,
            "score_pertinence": None,
            "score_impact": None,
            "score_coherence": None,
            "score_qualite_global": None,
            "niveau_qualite": "Hors périmètre",
            "statut_evaluation": "ANCIENNE_REFERENCE",
            "moteur_version": ENGINE_VERSION,

            "explication_completude": (
                "Ticket identifié comme ancienne référence."
            ),
            "explication_format": (
                "Non évalué : ticket hors périmètre."
            ),
            "explication_description": (
                "Non évaluée : ticket hors périmètre."
            ),
            "explication_impact": (
                "Non évalué : ticket hors périmètre."
            ),
            "explication_coherence": (
                "Non évaluée : ticket hors périmètre."
            ),
            "analyse": (
                "Ancienne référence : ce ticket est exclu "
                "de l'évaluation de qualité CAB."
            ),
            "preuves": [],
            "recommandations": [],
            "erreur_llm": "",
            "methode_analyse": "hors_perimetre",
        }

    # --------------------------------------------------------
    # 0. RESOLUTION DE LA DESCRIPTION / IMPACT (ticket -> parent -> absente)
    # --------------------------------------------------------
    # Règle unique et systématique (cf. commentaire sur _resolve_description) :
    # évite l'incohérence "certains tickets notés sur le résumé, d'autres non".

    description_text, description_source = _resolve_description(ticket)
    impact_text, impact_source = _resolve_impact(ticket)

    ticket_effective = dict(ticket)
    ticket_effective["Description"] = description_text
    ticket_effective["Impact de l'action"] = impact_text

    # --------------------------------------------------------
    # A. COMPLETUDE /25 (sur la description/impact résolus)
    # --------------------------------------------------------

    completeness_score, missing, completeness_details = (
        calculate_completeness(ticket_effective)
    )

    # --------------------------------------------------------
    # B. FORMAT /15
    # --------------------------------------------------------

    format_score, format_details = (
        calculate_description_format(ticket_effective)
    )

    # --------------------------------------------------------
    # C. DATES
    # --------------------------------------------------------

    date_score, date_details = (
        calculate_date_coherence(ticket)
    )

    # --------------------------------------------------------
    # D. ANALYSE SEMANTIQUE (LLM en priorité, heuristique en repli)
    # --------------------------------------------------------

    if llm_available is None:
        llm_available = check_llm_availability()

    llm = None
    llm_error = ""

    if llm_available:
        try:
            llm = _call_llm(
                ticket_effective,
                client_instance
            )
        except Exception as exc:
            llm_error = str(exc)
    else:
        llm_error = (
            "Ollama indisponible (health-check négatif) : "
            "analyse heuristique utilisée à la place."
        )

    # --------------------------------------------------------
    # E. SCORES SEMANTIQUES
    # --------------------------------------------------------

    if llm:

        methode = "llm"

        relevance_block = llm.get(
            "pertinence_description",
            {}
        )

        impact_block = llm.get(
            "qualite_impact",
            {}
        )

        coherence_block = llm.get(
            "coherence",
            {}
        )

        relevance_score = _points(
            relevance_block,
            DESCRIPTION_RELEVANCE_POINTS
        )

        impact_score = _points(
            impact_block,
            IMPACT_POINTS
        )

        coherence_semantic_score = _points(
            coherence_block,
            COHERENCE_POINTS
        )

        coherence_score = (
            coherence_semantic_score
            + date_score
        )

        analysis = llm.get(
            "analyse",
            ""
        )


        evidence = llm.get(
            "preuves",
            []
        )

        recommendations = llm.get(
            "recommandations",
            []
        )

        # Garde-fou : le LLM (petit modèle 3B) répond parfois par un
        # booléen (true/false) au lieu du texte demandé pour "analyse",
        # malgré le schéma JSON qui impose un string — constaté en
        # production ("Analyse: false" affiché tel quel sur la fiche
        # ticket). On ne laisse jamais une valeur inexploitable partir
        # telle quelle : on reconstruit une synthèse lisible à partir des
        # critères déjà calculés, propre à CE ticket.
        if not isinstance(analysis, str) or len(analysis.strip()) < 10:

            validés = [
                _CRITERION_LABELS.get(k, k)
                for k, v in {**relevance_block, **impact_block}.items()
                if v
            ]

            manquants = [
                _CRITERION_LABELS.get(k, k)
                for k, v in {**relevance_block, **impact_block}.items()
                if not v
            ]

            morceaux = []
            if validés:
                morceaux.append("Points couverts : " + ", ".join(validés[:3]) + ".")
            if manquants:
                morceaux.append("Manque : " + ", ".join(manquants[:3]) + ".")

            analysis = (
                " ".join(morceaux)
                if morceaux
                else "Analyse LLM reçue dans un format inexploitable pour ce ticket."
            )

        if not isinstance(evidence, list) or not evidence:
            evidence = [
                _CRITERION_LABELS.get(k, k)
                for k, v in {**relevance_block, **impact_block}.items()
                if v
            ]

        if not isinstance(recommendations, list) or not recommendations:
            recommendations = []

    else:

        # Repli heuristique déterministe : chaque ticket est noté sur SA
        # propre description (ticket ou parent), donc les scores varient
        # réellement d'un ticket à l'autre au lieu de tomber à 0 partout.

        methode = "heuristique"

        relevance_block = heuristic_relevance(description_text)
        impact_block = heuristic_impact(impact_text, description_text)

        # La cohérence sémantique (criticité/risque/type cohérents avec le
        # contenu) demande une vraie compréhension du texte : on ne
        # l'invente pas sans LLM. Seule la cohérence des dates (calculable
        # sans ambiguïté) est comptée.
        coherence_block = {
            criterion: False
            for criterion in COHERENCE_POINTS
            if criterion != "dates"
        }

        relevance_score = _points(
            relevance_block,
            DESCRIPTION_RELEVANCE_POINTS
        )

        impact_score = _points(
            impact_block,
            IMPACT_POINTS
        )

        coherence_score = date_score

        n_relevance_ok = sum(1 for v in relevance_block.values() if v)

        if description_source == "absente":
            analysis = (
                "Analyse heuristique : aucune description exploitable "
                "(ni sur le ticket, ni sur son parent)."
            )
        else:
            source_label = (
                "la description du ticket"
                if description_source == "ticket"
                else "la description du ticket parent"
            )

            analysis = (
                f"Analyse heuristique (LLM indisponible) basée sur {source_label} : "
                f"{n_relevance_ok}/5 critères de pertinence détectés."
            )

        evidence = [
            _CRITERION_LABELS.get(k, k)
            for k, v in {**relevance_block, **impact_block}.items()
            if v
        ]

        recommendations = _generate_recommendations(
            missing,
            format_details,
            relevance_block,
            impact_block,
            description_source,
        )

    # --------------------------------------------------------
    # F. SCORE GLOBAL /100
    # --------------------------------------------------------

    total = (
        completeness_score
        + format_score
        + relevance_score
        + impact_score
        + coherence_score
    )

    # sécurité
    total = min(100, max(0, int(total)))

    # --------------------------------------------------------
    # G. NIVEAU
    # --------------------------------------------------------

    if total >= 80:
        level = "Excellent"

    elif total >= 60:
        level = "Bon"

    elif total >= 40:
        level = "Moyen"

    else:
        level = "Faible"

    # --------------------------------------------------------
    # H. RECOMMANDATIONS AUTOMATIQUES (complément, pas remplacement)
    # --------------------------------------------------------
    # Le complément générique n'intervient QUE si la liste est totalement
    # vide (LLM en échec total ou heuristique sans aucune piste) — jamais
    # pour compléter des recommandations déjà présentes, pour ne pas noyer
    # une recommandation spécifique du LLM sous du texte générique. Réutilise
    # _generate_recommendations (même logique que le repli heuristique) au
    # lieu de dupliquer les libellés par critère.

    recommendations = list(recommendations)

    if not recommendations:
        recommendations = _generate_recommendations(
            missing,
            format_details,
            relevance_block,
            impact_block,
            description_source,
        )

    # doublons stricts, puis doublons sémantiques (reformulations quasi-
    # identiques qu'un petit modèle produit parfois malgré la consigne)
    recommendations = list(
        dict.fromkeys(
            str(x).strip()
            for x in recommendations
            if str(x).strip()
        )
    )
    recommendations = _deduplicate_similar_phrases(recommendations)[:5]
    # --------------------------------------------------------
    # I. STATUT
    # --------------------------------------------------------
    # Distingue explicitement la méthode utilisée (LLM vs heuristique) au
    # lieu d'un statut d'erreur générique quand le LLM ne répond pas, alors
    # qu'un score heuristique valide existe bel et bien.

    if description_source == "absente":
        status = "DESCRIPTION_ABSENTE"
    elif methode == "llm":
        status = "OK_LLM"
    else:
        status = "OK_HEURISTIQUE"

    # --------------------------------------------------------
    # J. RESULTAT
    # --------------------------------------------------------
    # Noms de colonnes alignés sur ce qu'attendent web_app.py / dashboard.html
    # / ticket.html. "preuves"/"recommandations" sont stockées en JSON (liste),
    # pas en chaîne jointe. "source_description"/"source_impact" tracent
    # explicitement l'origine du texte utilisé (ticket / parent / absente).

    coherence_explanation = _explain_block(coherence_block, COHERENCE_POINTS)
    if methode == "heuristique":
        coherence_explanation += (
            " (Cohérence sémantique non vérifiable sans LLM — seule la "
            "cohérence des dates a été évaluée.)"
        )
    coherence_explanation += " " + " ".join(date_details)

    return {

        "Clé de ticket":
            key,

        "Résumé":
            summary,

        "Description":
            description_text,

        "Impact de l'action":
            impact_text,

        "source_description":
            description_source,

        "source_impact":
            impact_source,

        "methode_analyse":
            methode,

        "score_completude":
            int(completeness_score),

        "score_format":
            int(format_score),

        "score_pertinence":
            int(relevance_score),

        "score_impact":
            int(impact_score),

        "score_coherence":
            int(coherence_score),

        "score_qualite_global":
            total,

        "niveau_qualite":
            level,

        "statut_evaluation":
            status,

        "champs_manquants":
            ", ".join(missing),

        "detail_completude":
            json.dumps(
                completeness_details,
                ensure_ascii=False
            ),

        "detail_format":
            json.dumps(
                format_details,
                ensure_ascii=False
            ),

        "detail_pertinence":
            json.dumps(
                relevance_block,
                ensure_ascii=False
            ),

        "detail_impact":
            json.dumps(
                impact_block,
                ensure_ascii=False
            ),

        "detail_coherence":
            json.dumps(
                coherence_block,
                ensure_ascii=False
            ),

        "detail_dates":
            " | ".join(date_details),

        "explication_completude":
            _explain_completeness(missing, completeness_details),

        "explication_format":
            _explain_block(format_details, FORMAT_POINTS),

        "explication_description":
            _explain_block(relevance_block, DESCRIPTION_RELEVANCE_POINTS),

        "explication_impact":
            _explain_block(impact_block, IMPACT_POINTS),

        "explication_coherence":
            coherence_explanation,

        "analyse":
            analysis,


        "preuves":
            json.dumps(
                _clean_text_items(evidence)[:5],
                ensure_ascii=False
            ),

        "recommandations":
            json.dumps(
                _clean_text_items(recommendations)[:5],
                ensure_ascii=False
            ),

        "erreur_llm":
            llm_error,

        "moteur_version":
            ENGINE_VERSION,

        # Métadonnées Jira recopiées telles quelles (État, dates, assigné,
        # parent...) : elles alimentent les filtres du dashboard et la page
        # des sous-tâches. Placées en dernier pour ne jamais écraser une
        # colonne de score portant le même nom.
        **extract_jira_meta(ticket),
    }


# ============================================================
# 10. EVALUATION DE PLUSIEURS TICKETS
# ============================================================

def evaluate_tickets(
    df,
    ticket_types=None,
    limit=None
):
    """
    ticket_types : liste des valeurs de "Type de ticket" à noter (ex.
    ["Tâche", "Sous-tâche"]). None = pas de filtre, on note TOUS les
    types présents.

    Les tickets "Sous-tâche" ont un taux de remplissage de Description bien
    supérieur aux tickets "Tâche" sur ce projet : ne pas les exclure du
    filtre, c'est là que vit l'essentiel du contenu exploitable (et les
    tickets "parents" porteurs de la description substantielle).
    """

    work = df.copy()

    if (
        ticket_types
        and "Type de ticket" in work.columns
    ):

        work = work[
            work["Type de ticket"]
            .astype(str)
            .str.strip()
            .isin(ticket_types)
        ]

    if limit is not None:
        work = work.head(limit)

    # Un seul health-check pour tout le lot (pas un par ticket) : évite
    # d'attendre inutilement si Ollama est down, et permet de savoir tout de
    # suite quelle méthode va être utilisée sur l'ensemble du lot.
    llm_available = check_llm_availability()
    print(
        "LLM (Ollama) disponible : notation sémantique."
        if llm_available
        else "LLM (Ollama) indisponible : notation heuristique (repli)."
    )

    rows = []

    total = len(work)

    for index, (_, row) in enumerate(
        work.iterrows(),
        start=1
    ):

        result = evaluate_ticket_quality(
            row.to_dict(),
            llm_available=llm_available,
        )

        rows.append(result)

        print(
            f"[{index}/{total}] "
            f"{result['Clé de ticket']} "
            f"→ {result['score_qualite_global']}/100 "
            f"({result['niveau_qualite']}, {result['methode_analyse']})"
        )

    return pd.DataFrame(rows)


# ============================================================
# 11. EVALUATION PAR LOTS / CHECKPOINT
# ============================================================
def evaluate_tickets_checkpointed(
    df,
    output_path,
    ticket_types=None,
    max_tickets=15,
    force_reanalysis=False,
):
    """
    Évalue uniquement les tickets absents de SQLite.

    Important :
    - Un ticket déjà enregistré dans cab_quality.db est ignoré.
    - La synchronisation suivante reprend automatiquement au ticket suivant.
    """

    db_path = Path(output_path)

    previous = read_checkpoint_db(db_path)

    already_evaluated = set()

    if (
        not previous.empty
        and "Clé de ticket" in previous.columns
    ):
        if "moteur_version" in previous.columns:
            up_to_date = previous[
                previous["moteur_version"].astype(str) == str(ENGINE_VERSION)
            ]
        else:
            # colonne absente (base très ancienne) : aucun ticket n'est
            # considéré à jour -> tout le portefeuille repasse en notation.
            up_to_date = previous.iloc[0:0]

        already_evaluated = set(
            up_to_date["Clé de ticket"]
            .dropna()
            .astype(str)
            .str.strip()
        )

    work = df.copy()

    if (
        ticket_types is not None
        and "Type de ticket" in work.columns
    ):
        if isinstance(ticket_types, str):
            ticket_types = [ticket_types]

        work = work[
            work["Type de ticket"]
            .astype(str)
            .isin(ticket_types)
        ].copy()

    work["Clé de ticket"] = (
        work["Clé de ticket"]
        .astype(str)
        .str.strip()
    )

    if force_reanalysis:
        tickets_restants = work.copy()

        print(
            "Mode réévaluation forcée activé : "
            "les tickets déjà présents dans SQLite seront recalculés."
        )

    else:
        tickets_restants = work[
            ~work["Clé de ticket"]
            .isin(already_evaluated)
        ].copy()

    tickets_restants = tickets_restants.head(max_tickets)

    print(
        f"Tickets déjà évalués : {len(already_evaluated)} | "
        f"Tickets restants dans Jira : {len(work) - len(already_evaluated)} | "
        f"Tickets à traiter maintenant : {len(tickets_restants)}"
    )

    results = []

    for position, (_, row) in enumerate(
        tickets_restants.iterrows(),
        start=1,
    ):
        ticket = row.to_dict()

        result = evaluate_ticket_quality(ticket)

        results.append(result)

        print(
            f"{result['Clé de ticket']} "
            f"→ {result['score_qualite_global']}/100 "
            f"({result['statut_evaluation']})"
        )

    result_df = pd.DataFrame(results)

    if not result_df.empty:
        upsert_tickets_db(
            result_df,
            db_path,
        )

    return read_checkpoint_db(db_path)


# ============================================================
# 12. EXPORT
# ============================================================

def export_results(
    results,
    output_path="resultats_qualite_CAB.xlsx"
):

    path = Path(output_path)

    results.to_excel(
        path,
        index=False
    )

    return str(path)