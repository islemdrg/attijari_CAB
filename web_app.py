import calendar
from pathlib import Path
from datetime import datetime, timedelta
import ast
import json
import sqlite3
import threading
import time
import traceback
import re

import pandas as pd
from flask import Flask, flash, render_template, request, redirect, send_file, url_for

import cab_pipeline
import report_pdf
from quality import score_engine


app = Flask(__name__)
app.secret_key = "cab-quality-dev-secret"  # dev only : à remplacer par une vraie clé en prod

BASE_DIR = Path(__file__).resolve().parent

# SQLite (pas CSV) : upsert transactionnel par "Clé de ticket", voir
# score_engine.read_checkpoint_db / upsert_tickets_db pour le détail.
DATA_PATH = BASE_DIR / "cab_quality.db"

# Taille d'un sous-lot noté par appel à /refresh. Avec le LLM (un appel
# Ollama par ticket, ~4s/ticket) on garde des lots courts pour que l'état
# d'avancement affiché bouge souvent ; sans LLM, le repli heuristique est
# quasi instantané donc le lot peut être bien plus grand.
REFRESH_BATCH_SIZE_LLM = 30
REFRESH_BATCH_SIZE_NO_LLM = 500

# Nombre MAXIMUM de tickets notés par clic sur "Synchroniser" : la synchro
# enchaîne les sous-lots toute seule en arrière-plan jusqu'à épuiser le
# retard ou atteindre cette limite de sécurité.
REFRESH_MAX_TICKETS_PER_RUN_LLM = 300
REFRESH_MAX_TICKETS_PER_RUN_NO_LLM = 2000

# Auto-sync : le dashboard peut relancer une synchro tout seul si la
# dernière remonte à plus de AUTO_SYNC_MIN_INTERVAL_SECONDS. Ne pas
# marteler l'API Jira à chaque F5.
AUTO_SYNC_MIN_INTERVAL_SECONDS = 120
# NOTE : fonctionnalité actuellement INACTIVE — l'appel maybe_auto_sync()
# dans dashboard() est commenté. Ce flag ne fait donc rien tant que cet
# appel n'est pas réactivé (à trancher : réactive-t-on l'auto-sync, ou
# retire-t-on ce bloc et maybe_auto_sync() ?).
AUTO_SYNC_ENABLED = False

# ============================================================
# ETAT DE SYNCHRONISATION EN ARRIERE-PLAN
# ============================================================
# /refresh lance l'extraction Jira + la notation dans un thread séparé (pas
# dans la requête HTTP, qui timeout sur un gros portefeuille) et répond
# immédiatement. /refresh/status permet au dashboard de poller l'avancement.

_sync_lock = threading.Lock()
_sync_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "message": "",
    "error": "",
    "step": "",
}


def _extract_taches_jira(log_prefix):
    """Extraction Jira + filtre sur les tickets de type "Tâche", partagée
    entre _run_sync et _run_sync_force_all (voir score_engine.evaluate_tickets
    pour le détail sur le choix de ce filtre)."""

    print(f"[{log_prefix}] Extraction Jira en cours (peut prendre du temps sur un gros projet)...")

    df = cab_pipeline.extract_and_clean_cab(verbose=True)

    if "Type de ticket" in df.columns:
        df = df[
            df["Type de ticket"]
            .astype(str)
            .str.strip()
            .eq("Tâche")
        ].copy()

    print(f"[{log_prefix}] {len(df)} tickets extraits (type = Tâche).")

    return df


def _rattraper_metadonnees_jira(df, log_prefix):
    """Met à jour les métadonnées Jira (État, dates, assigné...) des tickets
    déjà notés, sans toucher aux scores déjà calculés — un ticket noté lors
    d'une synchro précédente n'est jamais réévalué par le checkpoint, donc
    sans ce rattrapage sa colonne "État" resterait vide en base."""

    try:
        meta_updated = score_engine.upsert_metadata_db(df, DATA_PATH)
        print(
            f"[{log_prefix}] Métadonnées Jira rafraîchies pour {meta_updated} "
            "ticket(s) déjà en base (État, dates, assigné, parent)."
        )
    except Exception as meta_exc:
        print(f"[{log_prefix}] Rattrapage des métadonnées ignoré : {meta_exc}")


def _run_sync():
    started = time.time()
    try:
        _sync_state["step"] = "Connexion à l'API Jira et extraction des tickets..."

        df = _extract_taches_jira("sync")

        _sync_state["step"] = f"{len(df)} tickets extraits. Notation en cours..."

        llm_available = score_engine.check_llm_availability()
        batch_size = (
            REFRESH_BATCH_SIZE_LLM if llm_available else REFRESH_BATCH_SIZE_NO_LLM
        )

        before = len(score_engine.read_checkpoint_db(DATA_PATH))
        max_per_run = (
            REFRESH_MAX_TICKETS_PER_RUN_LLM
            if llm_available
            else REFRESH_MAX_TICKETS_PER_RUN_NO_LLM
        )

        # Enchaînement de sous-lots : on rappelle la notation tant qu'elle
        # fait progresser le nombre de tickets en base, sans dépasser
        # max_per_run. Ça rattrape le retard en une seule synchro au lieu
        # d'un clic manuel répété.
        result_df = score_engine.read_checkpoint_db(DATA_PATH)
        processed = 0

        while processed < max_per_run:

            count_before_batch = len(result_df)

            result_df = score_engine.evaluate_tickets_checkpointed(
                df,
                output_path=str(DATA_PATH),
                ticket_types=["Tâche"],
                max_tickets=min(batch_size, max_per_run - processed),
            )

            gained = len(result_df) - count_before_batch

            if gained <= 0:
                # plus aucun ticket à noter : le retard est rattrapé
                break

            processed += gained

            _sync_state["step"] = (
                f"Notation en cours : {processed} ticket(s) noté(s) "
                f"pendant cette synchronisation, "
                f"{len(result_df)} au total en base..."
            )

        _rattraper_metadonnees_jira(df, "sync")

        after = len(result_df)
        added = max(0, after - before)

        methode = "LLM (analyse sémantique)" if llm_available else "heuristique (Ollama indisponible)"
        elapsed = round(time.time() - started, 1)

        _sync_state["message"] = (
            f"{added} ticket(s) noté(s)/re-noté(s) via méthode {methode} "
            f"en {elapsed}s. Portefeuille total : {after} tickets notés."
        )
        _sync_state["error"] = ""
        _sync_state["step"] = ""

    except Exception as exc:
        traceback.print_exc()
        _sync_state["error"] = str(exc)
        _sync_state["step"] = ""

    finally:
        _sync_state["running"] = False
        _sync_state["finished_at"] = time.time()


def _run_sync_force_all():
    """Rattrapage complet : re-note TOUT le portefeuille (y compris les
    tickets déjà notés avec une version de moteur périmée), sans plafond
    par run. Utilisé après une mise à jour de ENGINE_VERSION pour que le
    nouveau prompt/schéma (ex: analyse_impact, barème corrigé) s'applique
    à tous les tickets existants, pas seulement aux nouveaux.

    Contrairement à _run_sync(), pas de limite REFRESH_MAX_TICKETS_PER_RUN :
    la boucle continue jusqu'à ce qu'il n'y ait plus AUCUN ticket à traiter,
    quel que soit le temps nécessaire. Tourne en arrière-plan (thread), donc
    aucun risque de timeout HTTP même sur plusieurs heures avec le LLM actif.
    """
    started = time.time()
    try:
        _sync_state["step"] = "Rattrapage complet : extraction Jira en cours..."

        df = _extract_taches_jira("force-all")

        llm_available = score_engine.check_llm_availability()
        batch_size = REFRESH_BATCH_SIZE_LLM if llm_available else REFRESH_BATCH_SIZE_NO_LLM

        before = len(score_engine.read_checkpoint_db(DATA_PATH))
        result_df = score_engine.read_checkpoint_db(DATA_PATH)
        processed = 0

        while True:
            # On interroge le nombre de tickets encore à traiter AVANT
            # d'appeler evaluate_tickets_checkpointed, pour connaître le
            # vrai volume restant indépendamment de la taille de la table
            # (qui ne bouge pas lors d'un upsert sur ticket déjà existant).
            current_db = score_engine.read_checkpoint_db(DATA_PATH)

            if "moteur_version" in current_db.columns and not current_db.empty:
                up_to_date_keys = set(
                    current_db.loc[
                        current_db["moteur_version"].astype(str) == str(score_engine.ENGINE_VERSION),
                        "Clé de ticket"
                    ].astype(str).str.strip()
                )
            else:
                up_to_date_keys = set()

            df_keys = set(df["Clé de ticket"].astype(str).str.strip())
            remaining = len(df_keys - up_to_date_keys)

            if remaining <= 0:
                # Plus aucun ticket à re-noter : le portefeuille entier
                # est aligné sur ENGINE_VERSION.
                break

            batch_before = len(up_to_date_keys)

            result_df = score_engine.evaluate_tickets_checkpointed(
                df,
                output_path=str(DATA_PATH),
                ticket_types=["Tâche"],
                max_tickets=batch_size,
            )

            after_db = score_engine.read_checkpoint_db(DATA_PATH)
            if "moteur_version" in after_db.columns:
                batch_after = (
                    after_db["moteur_version"].astype(str) == str(score_engine.ENGINE_VERSION)
                ).sum()
            else:
                batch_after = 0

            gained_this_batch = max(0, batch_after - batch_before)
            processed += gained_this_batch

            _sync_state["step"] = (
                f"Rattrapage complet en cours : {processed} ticket(s) "
                f"re-noté(s) avec le moteur v{score_engine.ENGINE_VERSION}, "
                f"{remaining} restant(s) avant ce lot..."
            )

            if gained_this_batch <= 0:
                # Le lot n'a fait progresser aucun ticket vers la version
                # courante -> on arrête pour éviter une boucle infinie
                # (ex: erreur systématique sur les tickets restants).
                print("[force-all] Aucune progression sur ce lot, arrêt du rattrapage.")
                break

        _rattraper_metadonnees_jira(df, "force-all")

        after = len(result_df)
        elapsed = round(time.time() - started, 1)
        methode = "LLM (analyse sémantique)" if llm_available else "heuristique"

        _sync_state["message"] = (
            f"Rattrapage complet terminé : {processed} ticket(s) re-noté(s) "
            f"via {methode} en {elapsed}s. Portefeuille total : {after} tickets, "
            f"tous alignés sur le moteur v{score_engine.ENGINE_VERSION}."
        )
        _sync_state["error"] = ""
        _sync_state["step"] = ""

    except Exception as exc:
        traceback.print_exc()
        _sync_state["error"] = str(exc)
        _sync_state["step"] = ""

    finally:
        _sync_state["running"] = False
        _sync_state["finished_at"] = time.time()


def start_sync_force_all():
    with _sync_lock:
        if _sync_state["running"]:
            return False
        _sync_state["running"] = True
        _sync_state["started_at"] = time.time()
        _sync_state["message"] = ""
        _sync_state["error"] = ""

    thread = threading.Thread(target=_run_sync_force_all, daemon=True)
    thread.start()
    return True


@app.route("/refresh/force-all", methods=["POST"])
def refresh_force_all():
    """Rattrapage complet : re-note tout le portefeuille sans plafond,
    utile après une mise à jour du moteur (ENGINE_VERSION incrémenté)."""

    started = start_sync_force_all()

    if started:
        flash(
            "Rattrapage complet lancé en arrière-plan : tous les tickets "
            "vont être re-notés avec le moteur à jour. Cela peut prendre "
            "du temps sur un gros portefeuille — la page se mettra à jour "
            "automatiquement.",
            "success",
        )
    else:
        flash(
            "Une synchronisation est déjà en cours, patiente qu'elle se termine.",
            "error",
        )

    return redirect(url_for("dashboard"))

def start_sync():
    with _sync_lock:
        if _sync_state["running"]:
            return False
        _sync_state["running"] = True
        _sync_state["started_at"] = time.time()
        _sync_state["message"] = ""
        _sync_state["error"] = ""

    thread = threading.Thread(target=_run_sync, daemon=True)
    thread.start()
    return True


def maybe_auto_sync():
    """Déclenche une synchro toute seule à l'ouverture du dashboard si la
    précédente remonte à plus de AUTO_SYNC_MIN_INTERVAL_SECONDS (ou n'a
    jamais eu lieu). Désactivable via AUTO_SYNC_ENABLED (ex: pendant un
    rattrapage manuel /refresh/force-all)."""

    if not AUTO_SYNC_ENABLED:
        return

    with _sync_lock:
        if _sync_state["running"]:
            return

        last = _sync_state["finished_at"] or _sync_state["started_at"]
        if last is not None and (time.time() - last) < AUTO_SYNC_MIN_INTERVAL_SECONDS:
            return

    start_sync()


# ============================================================
# HELPERS
# ============================================================

def safe_list(value):
    if isinstance(value, list):
        return value

    if value is None:
        return []

    try:
        if pd.isna(value):
            return []
    except (TypeError, ValueError):
        pass

    if isinstance(value, str):
        text = value.strip()

        if not text:
            return []

        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass

        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass

    return []


def score_class(score):
    try:
        if score is None or pd.isna(score):
            return "score-neutral"

        value = float(score)

    except (TypeError, ValueError):
        return "score-neutral"

    if value >= 80:
        return "score-excellent"

    if value >= 60:
        return "score-good"

    if value >= 40:
        return "score-medium"

    return "score-low"


def level_class(level):
    value = str(level).strip().lower()

    if "excellent" in value:
        return "level-excellent"

    if "bon" in value:
        return "level-good"

    if "moyen" in value:
        return "level-medium"

    if "faible" in value:
        return "level-low"

    if "hors" in value:
        return "level-neutral"

    return "level-neutral"


def status_class(status):
    value = str(status).strip().lower()

    if "ancienne" in value or "reference" in value:
        return "status-neutral"

    if "absente" in value or "manqu" in value:
        return "status-danger"

    if "heuristique" in value:
        return "status-warning"

    if "ok_llm" in value or value == "ok":
        return "status-success"

    return "status-warning"


def number(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def etat_class(etat):
    """Classe CSS du badge d'état Jira (workflow), pas du statut de notation.

    On reste tolérant sur les libellés : un workflow Jira peut être
    personnalisé ("Validé", "En attente CAB", "Mis en production"...).
    """

    value = str(etat or "").strip().lower()

    if not value:
        return "status-neutral"

    if any(
        word in value
        for word in ("termin", "fait", "clos", "ferm", "prod", "done", "valid")
    ):
        return "status-success"

    if any(
        word in value
        for word in ("cours", "progress", "attente", "pending", "revue", "review")
    ):
        return "status-warning"

    if any(
        word in value
        for word in ("rejet", "annul", "refus", "bloqu", "cancel")
    ):
        return "status-danger"

    return "status-neutral"


def extract_year(value):
    """
    Extrait l'année d'une date Jira.
    Gère notamment :
    - 2026-05-01 16:43:00
    - 01/05/2026
    - 01/05/2026 16:43
    - 2026-05-01
    """
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    text = str(value).strip()

    if not text:
        return None

    # Cas très fréquent Jira : YYYY-MM-DD...
    match = re.match(r"^(\d{4})[-/]", text)

    if match:
        return int(match.group(1))

    # Cas DD/MM/YYYY...
    match = re.match(r"^\d{1,2}/\d{1,2}/(\d{4})", text)

    if match:
        return int(match.group(1))

    # Dernier recours
    parsed = pd.to_datetime(
        text,
        errors="coerce",
        dayfirst=True,
    )

    if pd.isna(parsed):
        return None

    return int(parsed.year)


# ============================================================
# DATA
# ============================================================

def load_data():
    if not DATA_PATH.exists():
        return pd.DataFrame()

    # read_checkpoint_db (pas de CSV) : SQLite gère nativement l'absence de
    # corruption possible, cf. commentaire en tête de score_engine.py.
    df = score_engine.read_checkpoint_db(DATA_PATH)

    if df.empty:
        return df

    list_columns = [
        "preuves",
        "recommandations",
    ]

    for column in list_columns:
        if column in df.columns:
            df[column] = df[column].apply(safe_list)

    # NB : "score_description" et "score_bonus" ont été retirés :
    # score_description n'a jamais existé côté score_engine.py (confusion
    # avec score_format) et le barème bonus a été supprimé à la demande du
    # métier. ticket.html/dashboard.html ont été corrigés en conséquence.
    numeric_columns = [
        "score_qualite_global",
        "score_completude",
        "score_format",
        "score_pertinence",
        "score_impact",
        "score_coherence",
    ]

    for column in numeric_columns:
        if column in df.columns:
            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

    if "score_qualite_global" not in df.columns:
        df["score_qualite_global"] = 0

    if "niveau_qualite" not in df.columns:
        df["niveau_qualite"] = "Non évalué"

    if "statut_evaluation" not in df.columns:
        df["statut_evaluation"] = "NON_EVALUE"

    # --------------------------------------------------------
    # État Jira du ticket (workflow) : distinct de statut_evaluation
    # --------------------------------------------------------
    # statut_evaluation = résultat de NOTRE moteur de notation.
    # "État" = colonne Jira du workflow (À faire / En cours / Terminé...),
    # c'est celle demandée pour le filtre.
    etat_column = next(
        (
            candidate
            for candidate in ("État", "Etat", "Statut", "Status")
            if candidate in df.columns
        ),
        None,
    )

    if etat_column:
        df["etat_jira"] = (
            df[etat_column]
            .fillna("")
            .astype(str)
            .str.strip()
        )
    else:
        df["etat_jira"] = ""

    if "Date de CAB" in df.columns:
        df["annee_cab"] = df["Date de CAB"].apply(extract_year)
    else:
        df["annee_cab"] = None

    return df

def _filtrer_tickets(df, search="", level="", status="", year="", etat=""):
    work = df.copy()

    if search:
        query = search.lower()
        key_match = work["Clé de ticket"].astype(str).str.lower().str.contains(query, na=False)
        summary_match = work["Résumé"].astype(str).str.lower().str.contains(query, na=False)
        work = work[key_match | summary_match]

    if level:
        work = work[work["niveau_qualite"].astype(str).eq(level)]

    if status:
        work = work[work["statut_evaluation"].astype(str).eq(status)]

    if year == "__none__":
        work = work[work["annee_cab"].isna()]
    elif year:
        work = work[
            work["annee_cab"].apply(lambda v: str(int(v)) if pd.notna(v) else "").eq(year)
        ]

    if etat:
        work = work[
            work["etat_jira"].astype(str).str.strip().str.casefold().eq(etat.casefold())
        ]

    work = work.sort_values("score_qualite_global", ascending=False)

    return work
# ============================================================
# DASHBOARD
# ============================================================

@app.route("/")
def dashboard():

    #maybe_auto_sync()

    df = load_data()

    if df.empty:
        return render_template(
            "dashboard.html",
            rows=[],
            total_tickets=0,
            avg_score=0,
            excellent=0,
            good=0,
            medium=0,
            weak=0,
            hors_perimetre=0,
            page=1,
            total_pages=1,
            total_results=0,
            page_start=0,
            page_end=0,
            search="",
            level="",
            status="",
            year="",
            etat="",
            levels=[],
            statuses=[],
            years=[],
            etats=[],
        )

    search = request.args.get("search", "").strip()
    level = request.args.get("level", "").strip()
    status = request.args.get("status", "").strip()
    year = request.args.get("year", "").strip()
    etat = request.args.get("etat", "").strip()

    work = _filtrer_tickets(df, search=search, level=level, status=status, year=year, etat=etat)

    # --------------------------------------------------------
    # STATISTIQUES
    # --------------------------------------------------------

    evaluated_work = work[
        work["statut_evaluation"]
        .astype(str)
        .ne("ANCIENNE_REFERENCE")
    ].copy()

    scores = evaluated_work["score_qualite_global"].dropna()

    total = len(evaluated_work)
    avg_score = round(float(scores.mean()), 1) if not scores.empty else 0

    excellent = int((scores >= 80).sum())
    good = int(((scores >= 60) & (scores < 80)).sum())
    medium = int(((scores >= 40) & (scores < 60)).sum())
    weak = int((scores < 40).sum())

    hors_perimetre = int(
        work["statut_evaluation"]
        .astype(str)
        .eq("ANCIENNE_REFERENCE")
        .sum()
    )

    # --------------------------------------------------------
    # PAGINATION
    # --------------------------------------------------------

    PER_PAGE = 20

    try:
        page = int(
            request.args.get(
                "page",
                1,
            )
        )
    except (TypeError, ValueError):
        page = 1

    page = max(1, page)

    total_results = len(work)

    total_pages = max(
        1,
        (total_results + PER_PAGE - 1) // PER_PAGE,
    )

    page = min(page, total_pages)

    start_index = (page - 1) * PER_PAGE
    end_index = start_index + PER_PAGE

    page_work = work.iloc[
        start_index:end_index
    ].copy()

    page_start = start_index + 1 if total_results else 0
    page_end = min(end_index, total_results)

    # --------------------------------------------------------
    # ROWS DE LA PAGE COURANTE
    # --------------------------------------------------------

    rows = []

    for _, row in page_work.iterrows():

        rows.append({
            "key": row.get("Clé de ticket", ""),
            "summary": row.get("Résumé", ""),
            "score": number(row.get("score_qualite_global")),
            "completude": number(row.get("score_completude")),
            "format": number(row.get("score_format")),
            "pertinence": number(row.get("score_pertinence")),
            "impact": number(row.get("score_impact")),
            "coherence": number(row.get("score_coherence")),
            "level": row.get("niveau_qualite", "Non évalué"),
            "methode": row.get("methode_analyse", ""),
            "source_description": row.get("source_description", ""),
            "status": row.get("statut_evaluation", ""),
            "etat": row.get("etat_jira", "") or "—",
            "etat_class": etat_class(row.get("etat_jira", "")),
            "score_class": score_class(
                row.get("score_qualite_global")
            ),
            "status_class": status_class(
                row.get("statut_evaluation")
            ),
            "level_class": level_class(
                row.get("niveau_qualite")
            ),
        })

    return render_template(
        "dashboard.html",
        rows=rows,
        total_tickets=total,
        avg_score=avg_score,
        excellent=excellent,
        good=good,
        medium=medium,
        weak=weak,
        hors_perimetre=hors_perimetre,
        page=page,
        total_pages=total_pages,
        total_results=total_results,
        page_start=page_start,
        page_end=page_end,
        search=search,
        level=level,
        status=status,
        levels=sorted(
            df["niveau_qualite"]
            .dropna()
            .astype(str)
            .unique()
        ),
        statuses=sorted(
            df["statut_evaluation"]
            .dropna()
            .astype(str)
            .unique()
        ),
        years=sorted(
            {
                str(int(v))
                for v in df["annee_cab"]
                if pd.notna(v)
            },
            reverse=True,
        ),
        year=year,
        etat=etat,
        etats=sorted(
            {
                value.strip()
                for value in df["etat_jira"].astype(str)
                if value.strip()
            }
        ),
    )

@app.route("/dashboard/export")
def export_dashboard_pdf():

    df = load_data()

    if df.empty:
        flash("Aucune donnée disponible pour générer un rapport.", "error")
        return redirect(url_for("dashboard"))

    search = request.args.get("search", "").strip()
    level = request.args.get("level", "").strip()
    status = request.args.get("status", "").strip()
    year = request.args.get("year", "").strip()
    etat = request.args.get("etat", "").strip()

    work = _filtrer_tickets(df, search=search, level=level, status=status, year=year, etat=etat)

    evaluated_work = work[
        work["statut_evaluation"].astype(str).ne("ANCIENNE_REFERENCE")
    ].copy()

    scores = evaluated_work["score_qualite_global"].dropna()

    stats = {
        "total": len(evaluated_work),
        "avg_score": round(float(scores.mean()), 1) if not scores.empty else 0,
        "excellent": int((scores >= 80).sum()),
        "good": int(((scores >= 60) & (scores < 80)).sum()),
        "medium": int(((scores >= 40) & (scores < 60)).sum()),
        "weak": int((scores < 40).sum()),
        "hors_perimetre": int(
            work["statut_evaluation"].astype(str).eq("ANCIENNE_REFERENCE").sum()
        ),
    }

    filtres = {"search": search, "level": level, "status": status, "year": year, "etat": etat}

    rows = work.to_dict(orient="records")

    try:
        pdf_buffer = report_pdf.build_portfolio_pdf(rows, filtres, stats)
    except Exception as exc:
        traceback.print_exc()
        flash(f"Échec de la génération du rapport de synthèse : {exc}", "error")
        return redirect(url_for("dashboard", **filtres))

    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="rapport_synthese_portefeuille.pdf",
    )
# ============================================================
# TICKET DETAIL + EXPORT PDF
# ============================================================

def _prepare_ticket(ticket_id):
    """Partagé entre /ticket/<id> et /ticket/<id>/export pour éviter de
    dupliquer la préparation des données (parsing preuves/recommandations,
    classes CSS)."""

    df = load_data()

    if df.empty:
        return None

    match = df[
        df["Clé de ticket"]
        .astype(str)
        .eq(str(ticket_id))
    ]

    if match.empty:
        return None

    ticket = match.iloc[0].to_dict()

    ticket["preuves"] = safe_list(
        ticket.get("preuves")
    )

    ticket["recommandations"] = safe_list(
        ticket.get("recommandations")
    )

    ticket["score_class"] = score_class(
        ticket.get("score_qualite_global")
    )

    ticket["status_class"] = status_class(
        ticket.get("statut_evaluation")
    )

    # Sous-tâches : récupérées à la demande depuis Jira (pas stockées en
    # base qualité, elles ne sont pas notées). Échec silencieux si l'API
    # est indisponible : la page ticket doit rester consultable même sans
    # sous-tâches chargées.
    try:
        ticket["sous_taches"] = cab_pipeline.get_subtasks(ticket_id, verbose=False)
    except Exception as exc:
        print(f"[ticket_detail] Sous-tâches non chargées pour {ticket_id}: {exc}")
        ticket["sous_taches"] = []

    for sous_tache in ticket["sous_taches"]:
        sous_tache["etat_class"] = etat_class(sous_tache.get("etat", ""))

    return ticket


@app.route("/ticket/<ticket_id>")
def ticket_detail(ticket_id):

    ticket = _prepare_ticket(ticket_id)

    if ticket is None:
        return redirect(url_for("dashboard"))

    return render_template(
        "ticket.html",
        ticket=ticket,
        number=number,
    )


@app.route("/ticket/<ticket_id>/export")
def export_ticket_pdf(ticket_id):
    """Génère et télécharge un rapport PDF détaillé pour ce ticket :
    résumé, description, impact, analyse, analyse d'impact, détail du
    score /100, explications, recommandations, preuves."""

    ticket = _prepare_ticket(ticket_id)

    if ticket is None:
        flash(f"Ticket {ticket_id} introuvable.", "error")
        return redirect(url_for("dashboard"))

    try:
        pdf_buffer = report_pdf.build_ticket_pdf(ticket)
    except Exception as exc:
        traceback.print_exc()
        flash(f"Échec de la génération du rapport PDF : {exc}", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"rapport_{ticket_id}.pdf",
    )


# ============================================================
# SYNC JIRA (résout : "ça ne ramène pas les tickets de l'API Jira" +
# "ça reste trop longtemps actualiser et ça affiche rien")
# ============================================================
# Avant : le dashboard lisait uniquement le CSV statique
# resultats_cab_checkpoint_final.csv, généré une fois dans un notebook.
# Rien dans web_app.py ne rappelait jamais l'API Jira -> un ticket créé
# après la génération du CSV n'apparaissait jamais.
#
# /refresh lance maintenant la synchro dans un thread d'arrière-plan
# (voir start_sync) : le clic répond tout de suite, et /refresh/status
# permet au dashboard de savoir quand c'est terminé (le JS de la page
# poll cette route toutes les 3s pendant que "en cours" est vrai).

@app.route("/refresh", methods=["POST"])
def refresh():

    started = start_sync()

    if started:
        flash(
            "Synchronisation Jira lancée en arrière-plan (extraction API + "
            "notation). Cette page se mettra à jour automatiquement.",
            "success",
        )
    else:
        flash(
            "Une synchronisation est déjà en cours, patiente qu'elle se termine.",
            "error",
        )

    return redirect(url_for("dashboard"))


@app.route("/refresh/status")
def refresh_status():
    return {
        "running": _sync_state["running"],
        "message": _sync_state["message"],
        "error": _sync_state["error"],
        "step": _sync_state["step"],
    }


@app.route("/ticket/<ticket_id>/reanalyser", methods=["POST"])
def reanalyser_ticket(ticket_id):
    """Renote un seul ticket (utile quand erreur_llm est renseigné pour ce
    ticket, ex. Ollama temporairement indisponible lors du dernier passage).

    Utilise extract_and_clean_single() (1-2 appels API ciblés) au lieu de
    extract_and_clean_cab() qui retirait tout le projet pour un seul
    ticket."""

    try:
        match = cab_pipeline.extract_and_clean_single(ticket_id, verbose=False)

        result = score_engine.evaluate_ticket_quality(match.iloc[0].to_dict())

        # upsert_tickets_db : remplace directement la ligne par clé
        # primaire "Clé de ticket", aucun risque de doublon ni de fichier
        # à moitié écrit.
        score_engine.upsert_tickets_db(pd.DataFrame([result]), DATA_PATH)

        flash(f"Ticket {ticket_id} réanalysé avec succès.", "success")

    except Exception as exc:
        traceback.print_exc()
        flash(f"Échec de la réanalyse du ticket {ticket_id} : {exc}", "error")

    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.route("/sous-taches")
def sous_taches():

    search = request.args.get("search", "").strip()
    etat = request.args.get("etat", "").strip()

    df = _load_subtasks_from_cache()

    if df.empty:
        # Premier chargement, cache jamais initialisé : on lance le
        # rafraîchissement en arrière-plan et on affiche un message plutôt
        # que de bloquer la requête HTTP sur l'appel Jira complet.
        start_subtasks_sync()
        flash(
            "Chargement initial des sous-tâches en cours en arrière-plan, "
            "réessaie dans quelques instants.",
            "success",
        )

    if df.empty:
        return render_template(
            "sous_taches.html",
            groups=[],
            total_results=0,
            page=1,
            total_pages=1,
            page_start=0,
            page_end=0,
            search=search,
            etat=etat,
            etats=[],
        )

    work = df.copy()

    if search:
        query = search.lower()
        match_key = work["Clé de ticket"].astype(str).str.lower().str.contains(query, na=False)
        match_summary = work["Résumé"].astype(str).str.lower().str.contains(query, na=False)
        match_parent = work["Clé parent"].astype(str).str.lower().str.contains(query, na=False)
        match_parent_summary = work["Résumé parent"].astype(str).str.lower().str.contains(query, na=False)
        work = work[match_key | match_summary | match_parent | match_parent_summary]

    if etat:
        work = work[work["État"].astype(str).str.casefold().eq(etat.casefold())]

    work = work.sort_values(["Clé parent", "Clé de ticket"])

    # Pagination par TICKET PARENT (pas par ligne de sous-tâche), pour
    # que le regroupement affiché sur une page ne soit jamais coupé en
    # plein milieu d'un groupe.
    parent_keys = work["Clé parent"].replace("", "Sans parent").unique().tolist()

    PER_PAGE = 15  # nombre de groupes (tickets parent) par page

    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)

    total_pages = max(1, (len(parent_keys) + PER_PAGE - 1) // PER_PAGE)
    page = min(page, total_pages)

    start_index = (page - 1) * PER_PAGE
    end_index = start_index + PER_PAGE
    page_parent_keys = set(parent_keys[start_index:end_index])

    work["_parent_key_norm"] = work["Clé parent"].replace("", "Sans parent")
    page_work = work[work["_parent_key_norm"].isin(page_parent_keys)]

    groups = []
    for parent_key, group_df in page_work.groupby("_parent_key_norm", sort=False):
        parent_summary = group_df["Résumé parent"].iloc[0] or "—"

        subtasks = []
        for _, row in group_df.iterrows():
            subtasks.append({
                "key": row.get("Clé de ticket", ""),
                "summary": row.get("Résumé", ""),
                "etat": row.get("État", "—"),
                "etat_class": etat_class(row.get("État", "")),
                "assignee": row.get("Personne assignée", "Non assigné"),
                "priorite": row.get("Priorité", "—"),
                "updated": str(row.get("Mise à jour", ""))[:10],
            })

        groups.append({
            "parent_key": parent_key,
            "parent_summary": parent_summary,
            "subtasks": subtasks,
        })

    return render_template(
        "sous_taches.html",
        groups=groups,
        total_results=len(work),
        page=page,
        total_pages=total_pages,
        page_start=start_index + 1 if parent_keys else 0,
        page_end=min(end_index, len(parent_keys)),
        total_parents=len(parent_keys),
        search=search,
        etat=etat,
        etats=sorted({v.strip() for v in df["État"].astype(str) if v.strip()}),
    )
SUBTASKS_TABLE = "subtasks_cache"


def _init_subtasks_cache_from_jira():
    """Rafraîchit le cache SQLite des sous-tâches depuis Jira. Appelée
    en arrière-plan (thread), jamais dans le corps d'une requête HTTP —
    exactement le même principe que _run_sync() pour les tâches notées."""

    df = cab_pipeline.get_all_subtasks(verbose=True)

    conn = sqlite3.connect(str(DATA_PATH))
    try:
        df.to_sql(SUBTASKS_TABLE, conn, if_exists="replace", index=False)
        print(f"[subtasks-cache] {len(df)} sous-tâche(s) mise(s) en cache.")
    finally:
        conn.close()


def _load_subtasks_from_cache():
    """Lit le cache SQLite (quasi instantané), jamais Jira directement."""

    if not DATA_PATH.exists():
        return pd.DataFrame()

    conn = sqlite3.connect(str(DATA_PATH))
    try:
        try:
            return pd.read_sql_query(f'SELECT * FROM "{SUBTASKS_TABLE}"', conn)
        except Exception:
            return pd.DataFrame()
    finally:
        conn.close()


_subtasks_sync_lock = threading.Lock()
_subtasks_sync_state = {"running": False, "finished_at": None}


def start_subtasks_sync():
    with _subtasks_sync_lock:
        if _subtasks_sync_state["running"]:
            return False
        _subtasks_sync_state["running"] = True

    def _run():
        try:
            _init_subtasks_cache_from_jira()
        except Exception:
            traceback.print_exc()
        finally:
            _subtasks_sync_state["running"] = False
            _subtasks_sync_state["finished_at"] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return True


@app.route("/sous-taches/refresh", methods=["POST"])
def refresh_subtasks():
    started = start_subtasks_sync()
    if started:
        flash("Synchronisation des sous-tâches lancée en arrière-plan.", "success")
    else:
        flash("Une synchronisation des sous-tâches est déjà en cours.", "error")
    return redirect(url_for("sous_taches"))


# ============================================================
# CALENDRIER (échéances de validation prévues vs. réelles, alerte sur les
# tickets échus : délai dépassé sans validation)
# ============================================================

DELAI_VALIDATION_HEURES = {
    "urgent": 48,
    "normal": 72,
    "standard": 72,
}


def _parse_date_flexible(value):
    """Parsing tolérant, même logique que score_engine._parse_date."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    text = str(value).strip()
    if not text:
        return None

    formats = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y",
               "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"]
    for fmt in formats:
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            pass

    parsed = pd.to_datetime(text, dayfirst=True, errors="coerce")
    return parsed.to_pydatetime() if pd.notna(parsed) else None


def _calculer_echeance(row):
    """Calcule l'échéance de validation prévue et son statut réel, en
    croisant Date de validation avec l'état Jira du ticket — pas
    uniquement la présence d'une date, pour éviter de marquer "Échu"
    un ticket déjà clos pour une autre raison (rejeté, annulé...)."""

    date_cab = _parse_date_flexible(row.get("Date de CAB"))
    date_validation = _parse_date_flexible(row.get("Date de validation"))

    if date_cab is None:
        return None

    type_changement = str(row.get("Type du changement", "")).strip().lower()
    delai_heures = DELAI_VALIDATION_HEURES.get(type_changement, 72)

    echeance_prevue = date_cab + timedelta(hours=delai_heures)

    etat_jira_value = str(row.get("etat_jira", "") or row.get("État", "")).strip().lower()

    # Liste fermée des 7 états Jira possibles (workflow à choix
    # contraint, pas de saisie libre). Seuls "Refusé", "Clôturé" et
    # "Abandonné" sont considérés terminaux ici : "Validé" est géré
    # séparément via Date de validation, et "Validé sous réserve" reste
    # actif (une réserve implique une action encore attendue, donc le
    # ticket doit continuer à pouvoir passer "Échu" si le délai expire
    # sans résolution de la réserve).
    ETATS_JIRA_TERMINAUX = {"refusé", "clôturé", "abandonné"}

    est_clos = etat_jira_value in ETATS_JIRA_TERMINAUX

    delai_depasse = echeance_prevue < datetime.now()

    if date_validation is not None:
        # Une date de validation existe : on vérifie si elle a été
        # renseignée dans les délais ou après l'échéance prévue.
        if date_validation <= echeance_prevue:
            statut = "valide_a_temps"
        else:
            statut = "valide_en_retard"
    elif est_clos:
        # Pas de Date de validation, mais le ticket est déjà dans un
        # état terminal Jira (rejeté/annulé/clos autrement) : ce n'est
        # pas un cas "échu en attente de vote", juste un ticket clos
        # sans date de validation formelle renseignée.
        statut = "clos_sans_date"
    elif delai_depasse:
        # Toujours ouvert dans Jira, aucune validation, délai dépassé :
        # c'est le vrai cas "échu" qui doit alerter.
        statut = "echu"
    else:
        statut = "a_venir"

    return {
        "key": row.get("Clé de ticket", ""),
        "summary": row.get("Résumé", ""),
        "date_cab": date_cab,
        "echeance_prevue": echeance_prevue,
        "date_validation": date_validation,
        "type_changement": row.get("Type du changement", "—"),
        "delai_heures": delai_heures,
        "etat_jira": row.get("etat_jira", "") or row.get("État", "—"),
        "statut": statut,
        "score": row.get("score_qualite_global"),
    }

@app.route("/calendrier")
def calendrier():

    df = load_data()

    if df.empty:
        return render_template(
            "calendrier.html",
            echeances_par_jour={},
            echues_par_annee={},
            sans_validation_par_annee={},
            mois_courant=datetime.now().strftime("%Y-%m"),
            annee=datetime.now().year,
            mois=datetime.now().month,
        )

    try:
        annee = int(request.args.get("annee", datetime.now().year))
        mois = int(request.args.get("mois", datetime.now().month))
    except (TypeError, ValueError):
        annee, mois = datetime.now().year, datetime.now().month

    echeances = []
    for _, row in df.iterrows():
        result = _calculer_echeance(row)
        if result is not None:
            echeances.append(result)

    # Échéances en retard (échues, non validées) : mises en avant
    # indépendamment du mois affiché, car ce sont les plus urgentes à
    # traiter — un retard de la semaine dernière ne doit pas disparaître
    # juste parce qu'on regarde le calendrier du mois en cours.
    # Regroupées par année de l'échéance, triées décroissant (année la
    # plus récente en premier), pour éviter une liste plate trop longue.
    echues_liste = sorted(
        [e for e in echeances if e["statut"] == "echu"],
        key=lambda e: e["echeance_prevue"],
        reverse=True,
    )

    echues_par_annee = {}
    for e in echues_liste:
        annee_echeance = e["echeance_prevue"].year
        echues_par_annee.setdefault(annee_echeance, []).append(e)

    echues_par_annee = dict(
        sorted(echues_par_annee.items(), key=lambda kv: kv[0], reverse=True)
    )
     # Regroupement par année des échéances sans validation.
    sans_validation_liste = sorted(
        [e for e in echeances if e["date_validation"] is None and e["statut"] == "a_venir"],
        key=lambda e: e["echeance_prevue"],
    )

    sans_validation_par_annee = {}
    for e in sans_validation_liste:
        annee_echeance = e["echeance_prevue"].year
        sans_validation_par_annee.setdefault(annee_echeance, []).append(e)

    sans_validation_par_annee = dict(
        sorted(sans_validation_par_annee.items(), key=lambda kv: kv[0], reverse=True)
    )
    # Regroupement par jour, pour le mois demandé uniquement.
    echeances_par_jour = {}
    for e in echeances:
        d = e["echeance_prevue"]
        if d.year == annee and d.month == mois:
            jour = d.day
            echeances_par_jour.setdefault(jour, []).append(e)

    for jour in echeances_par_jour:
        echeances_par_jour[jour].sort(key=lambda e: e["echeance_prevue"])

    cal = calendar.Calendar(firstweekday=0)  # lundi
    semaines = cal.monthdayscalendar(annee, mois)

    mois_precedent = mois - 1 if mois > 1 else 12
    annee_mois_precedent = annee if mois > 1 else annee - 1
    mois_suivant = mois + 1 if mois < 12 else 1
    annee_mois_suivant = annee if mois < 12 else annee + 1

    return render_template(
        "calendrier.html",
        semaines=semaines,
        echeances_par_jour=echeances_par_jour,
        echues_par_annee=echues_par_annee,
        annee=annee,
        mois=mois,
        nom_mois=calendar.month_name[mois].capitalize(),
        mois_precedent=mois_precedent,
        annee_mois_precedent=annee_mois_precedent,
        mois_suivant=mois_suivant,
        annee_mois_suivant=annee_mois_suivant,
        sans_validation_par_annee=sans_validation_par_annee,
        aujourd_hui=datetime.now().day if (annee == datetime.now().year and mois == datetime.now().month) else None,
    )


@app.route("/calendrier/jour")
def calendrier_jour():

    try:
        annee = int(request.args.get("annee"))
        mois = int(request.args.get("mois"))
        jour = int(request.args.get("jour"))
    except (TypeError, ValueError):
        return redirect(url_for("calendrier"))

    df = load_data()

    if df.empty:
        echeances_du_jour = []
    else:
        echeances_du_jour = []
        for _, row in df.iterrows():
            result = _calculer_echeance(row)
            if result is not None:
                d = result["echeance_prevue"]
                if d.year == annee and d.month == mois and d.day == jour:
                    echeances_du_jour.append(result)

        echeances_du_jour.sort(key=lambda e: e["echeance_prevue"])

    return render_template(
        "calendrier_jour.html",
        echeances=echeances_du_jour,
        annee=annee,
        mois=mois,
        jour=jour,
    )
# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
    )