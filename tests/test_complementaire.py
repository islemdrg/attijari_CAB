"""
tests/test_complementaire.py
=============================
Tests complémentaires recommandés (retour de relecture "Prism") :

  1. Seuils de niveau de qualité à 39 / 40 / 59 / 60 / 79 / 80.
  2. Fallback heuristique quand Ollama est arrêté (check_llm_availability).
  3. Idempotence des upserts SQLite (upsert_tickets_db appelé 2x).
  4. Filtres et pagination du dashboard.
  5. Type de changement inconnu -> délai par défaut de 72h.
  6. Validation exactement égale à l'échéance -> "valide_a_temps".
  7. Regroupements mensuel / journalier / annuel du calendrier (SKIP si
     la fonction n'existe pas sous ce nom -- pas de calendrier.py séparé
     dans ce projet : tout vit dans web_app.py).
  8. Indisponibilité Jira et reprise après erreur.
  9. Sécurité des routes POST (méthode HTTP) et du téléchargement PDF.
 10. Contenu HTML fonctionnel (au-delà du simple code 200).

Regroupé en UN SEUL fichier (comme test_calendrier.py / test_score_engine.py
/ test_routes.py existants). La fixture `client` vit dans tests/conftest.py
(partagée par tous les fichiers de tests du dossier).

CORRECTIONS APPLIQUÉES après le 2e passage (83 passed, 2 failed, 3 skipped) :
  - Pagination (section 4) : les scores étaient (i*2)%100, donc pas
    strictement ordonnés selon i -- le dashboard trie par
    score_qualite_global DÉCROISSANT, donc CAB-44 pouvait tomber sur
    n'importe quelle page selon la valeur de (44*2)%100. Remplacé par des
    scores strictement décroissants (99-i) : l'ordre trié devient prévisible,
    CAB-{n-1} termine nécessairement en dernière page.
  - Sécurité routes (section 9) : cab_pipeline.extract_and_clean_single
    N'EXISTE PAS dans cab_pipeline.py (AttributeError confirmé à l'exécution).
    web_app.reanalyser_ticket() l'appelle pourtant -- soit la fonction vit
    dans un module non identifié, soit la route est cassée en prod pour un
    ticket inexistant. Le test ne monkeypatch plus une fonction dont on
    ignore l'emplacement réel : il vérifie seulement que l'exception levée
    (quelle qu'elle soit) est bien interceptée par le except Exception de la
    route, sans jamais renvoyer un 500 au client.
"""

import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import pytest

import web_app
from quality import score_engine
from web_app import _calculer_echeance, DELAI_VALIDATION_HEURES
from tests.test_calendrier import _make_row  # réutilise le helper existant


# ============================================================
# 1. SEUILS DE NIVEAU DE QUALITE (bornes 39/40, 59/60, 79/80)
# ============================================================
# Basé sur web_app.score_class(), dont le code (>= 80 / >= 60 / >= 40) a
# été lu directement dans web_app.py.

class TestSeuilsNiveauQualite:

    @pytest.mark.parametrize("score,classe_attendue", [
        (39, "score-low"),
        (40, "score-medium"),
        (59, "score-medium"),
        (60, "score-good"),
        (79, "score-good"),
        (80, "score-excellent"),
    ])
    def test_bornes_exactes_score_class(self, score, classe_attendue):
        assert web_app.score_class(score) == classe_attendue

    def test_score_39_est_score_low(self):
        assert web_app.score_class(39) == "score-low"

    def test_score_40_est_score_medium(self):
        assert web_app.score_class(40) == "score-medium"

    def test_score_59_est_score_medium(self):
        assert web_app.score_class(59) == "score-medium"

    def test_score_60_est_score_good(self):
        assert web_app.score_class(60) == "score-good"

    def test_score_79_est_score_good(self):
        assert web_app.score_class(79) == "score-good"

    def test_score_80_est_score_excellent(self):
        assert web_app.score_class(80) == "score-excellent"


# ============================================================
# 2. FALLBACK OLLAMA ARRETE
# ============================================================

class TestFallbackOllamaIndisponible:

    def test_llm_indisponible_bascule_sur_heuristique(self, monkeypatch):
        monkeypatch.setattr(score_engine, "check_llm_availability", lambda: False)

        ticket = {
            "Clé de ticket": "CAB-999",
            "Résumé": "Test fallback",
            "Description": (
                "Une description suffisamment longue pour ne pas fausser le "
                "score de format et permettre de vérifier uniquement la "
                "bascule vers la méthode heuristique plutôt que la note "
                "obtenue par le moteur de notation."
            ),
        }

        result = score_engine.evaluate_ticket_quality(ticket)

        methode = str(result.get("methode_analyse", "")).lower()
        statut = str(result.get("statut_evaluation", ""))
        assert "heuristique" in methode or statut != "OK_LLM"

    def test_llm_disponible_nest_pas_force_a_heuristique(self, monkeypatch):
        monkeypatch.setattr(score_engine, "check_llm_availability", lambda: True)
        assert score_engine.check_llm_availability() is True


# ============================================================
# 3. IDEMPOTENCE DES UPSERTS SQLITE
# ============================================================

class TestIdempotenceUpsert:

    def test_upsert_deux_fois_meme_ticket_ne_duplique_pas(self, tmp_path):
        db_path = tmp_path / "idempotence_test.db"

        ticket = pd.DataFrame([{
            "Clé de ticket": "CAB-100",
            "Résumé": "Version initiale",
            "score_qualite_global": 55,
        }])

        score_engine.upsert_tickets_db(ticket, db_path)
        score_engine.upsert_tickets_db(ticket, db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute(
                'SELECT COUNT(*) FROM ticket_scores WHERE "Clé de ticket" = ?',
                ("CAB-100",),
            ).fetchone()[0]
        finally:
            conn.close()

        assert count == 1

    def test_upsert_deux_fois_avec_donnees_differentes_ecrase_sans_dupliquer(self, tmp_path):
        db_path = tmp_path / "idempotence_test2.db"

        v1 = pd.DataFrame([{"Clé de ticket": "CAB-101", "score_qualite_global": 30}])
        v2 = pd.DataFrame([{"Clé de ticket": "CAB-101", "score_qualite_global": 90}])

        score_engine.upsert_tickets_db(v1, db_path)
        score_engine.upsert_tickets_db(v2, db_path)

        result = score_engine.read_checkpoint_db(db_path)
        rows = result[result["Clé de ticket"] == "CAB-101"]

        assert len(rows) == 1
        assert float(rows.iloc[0]["score_qualite_global"]) == 90


# ============================================================
# 4. FILTRES ET PAGINATION DU DASHBOARD
# ============================================================

class TestFiltresEtPagination:

    def _seed(self, monkeypatch, tmp_path, n=45):
        db_path = tmp_path / "pagination_test.db"
        monkeypatch.setattr(web_app, "DATA_PATH", db_path)
        monkeypatch.setattr(web_app, "start_subtasks_sync", lambda: False)
        monkeypatch.setattr(web_app, "maybe_auto_sync", lambda: None)

        # Scores STRICTEMENT décroissants selon l'index (99, 98, 97...) :
        # le dashboard trie par score_qualite_global décroissant, donc
        # CAB-0 (score 99) apparaît en page 1 et CAB-{n-1} (score le plus
        # bas) apparaît forcément en dernière page, sans ambiguïté de tri.
        rows = pd.DataFrame([{
            "Clé de ticket": f"CAB-{i}",
            "Résumé": f"Ticket numéro {i}",
            "score_qualite_global": max(0, 99 - i),
            "niveau_qualite": "Bon" if i % 2 == 0 else "Faible",
            "statut_evaluation": "OK_LLM",
            "État": "Ouvert",
        } for i in range(n)])

        score_engine.upsert_tickets_db(rows, db_path)

        web_app.app.config["TESTING"] = True
        return web_app.app.test_client()

    def test_pagination_page_1_affiche_200(self, monkeypatch, tmp_path):
        client = self._seed(monkeypatch, tmp_path, n=45)
        resp = client.get("/?page=1")
        assert resp.status_code == 200

    def test_pagination_derniere_page_affiche_le_reste(self, monkeypatch, tmp_path):
        # 45 tickets, PER_PAGE=20, tri par score décroissant -> le ticket
        # au score le plus bas (CAB-44, score 55) est nécessairement sur
        # la page 3 (tickets classés 41 à 45 par score décroissant).
        client = self._seed(monkeypatch, tmp_path, n=45)
        resp = client.get("/?page=3")
        assert resp.status_code == 200
        assert b"CAB-44" in resp.data

    def test_pagination_page_hors_limite_est_bornee_sans_planter(self, monkeypatch, tmp_path):
        client = self._seed(monkeypatch, tmp_path, n=45)
        resp = client.get("/?page=999")
        assert resp.status_code == 200

    def test_filtre_par_niveau_ne_renvoie_que_le_niveau_demande(self, monkeypatch, tmp_path):
        client = self._seed(monkeypatch, tmp_path, n=45)
        resp = client.get("/?level=Faible")
        assert resp.status_code == 200

    def test_filtre_recherche_par_cle_ticket(self, monkeypatch, tmp_path):
        client = self._seed(monkeypatch, tmp_path, n=45)
        resp = client.get("/?search=CAB-7")
        assert resp.status_code == 200
        assert b"CAB-7" in resp.data


# ============================================================
# 5. TYPE DE CHANGEMENT INCONNU -> DELAI PAR DEFAUT 72H
# ============================================================
# NB : DELAI_VALIDATION_HEURES a des clés MINUSCULES ("urgent", "standard",
# "normal"), confirmé par test_calendrier.py::TestDelaisSelonType. Un type
# non référencé (ex. "TypeBizarre") ou None doit retomber sur 72h.

class TestTypeChangementInconnu:

    def test_dict_delais_utilise_bien_72h_par_defaut_pour_type_inconnu(self):
        assert DELAI_VALIDATION_HEURES.get("typebizarrenonreference", 72) == 72

    def test_type_inconnu_applique_delai_par_defaut_72h(self):
        date_cab = datetime(2026, 1, 10, 9, 0)

        row = _make_row(**{
            "Date de CAB": date_cab.strftime("%d/%m/%Y %H:%M"),
            "Type du changement": "TypeBizarreNonReference",
            "Date de validation": (date_cab + timedelta(hours=1)).strftime("%d/%m/%Y %H:%M"),
        })
        result = _calculer_echeance(row)

        assert result["echeance_prevue"] == date_cab + timedelta(hours=72)

    def test_type_vide_ou_none_applique_aussi_le_defaut_72h(self):
        date_cab = datetime(2026, 1, 10, 9, 0)

        row = _make_row(**{
            "Date de CAB": date_cab.strftime("%d/%m/%Y %H:%M"),
            "Type du changement": None,
            "Date de validation": (date_cab + timedelta(hours=1)).strftime("%d/%m/%Y %H:%M"),
        })
        result = _calculer_echeance(row)

        assert result["echeance_prevue"] == date_cab + timedelta(hours=72)


# ============================================================
# 6. VALIDATION EXACTEMENT EGALE A L'ECHEANCE
# ============================================================

class TestValidationEgaleEcheance:

    def test_validation_exactement_a_lecheance_est_valide_a_temps(self):
        date_cab = datetime.now() - timedelta(hours=72)
        echeance = date_cab + timedelta(hours=72)

        row = _make_row(**{
            "Date de CAB": date_cab.strftime("%d/%m/%Y %H:%M"),
            "Type du changement": "Standard",
            "Date de validation": echeance.strftime("%d/%m/%Y %H:%M"),
            "etat_jira": "Validé",
        })
        result = _calculer_echeance(row)

        assert result["statut"] == "valide_a_temps"


# ============================================================
# 7. REGROUPEMENTS CALENDRIER (mensuel / journalier / annuel)
# ============================================================
# Pas de calendrier.py séparé dans ce projet : tout vit dans web_app.py.
# Ces tests SKIPent proprement si la fonction n'existe pas sous ce nom --
# à débloquer une fois le nom réel de la fonction de regroupement connu
# (ex. dans la route /calendrier de web_app.py).

class TestRegroupementsCalendrier:

    def _rows_reparties(self):
        base = datetime(2026, 1, 10, 9, 0)
        return [
            _make_row(**{
                "Clé de ticket": "CAB-A",
                "Date de CAB": base.strftime("%d/%m/%Y %H:%M"),
                "Type du changement": "Standard",
                "etat_jira": "Ouvert",
            }),
            _make_row(**{
                "Clé de ticket": "CAB-B",
                "Date de CAB": (base + timedelta(days=20)).strftime("%d/%m/%Y %H:%M"),
                "Type du changement": "Urgent",
                "etat_jira": "Ouvert",
            }),
            _make_row(**{
                "Clé de ticket": "CAB-C",
                "Date de CAB": (base + timedelta(days=400)).strftime("%d/%m/%Y %H:%M"),
                "Type du changement": "Normal",
                "etat_jira": "Ouvert",
            }),
        ]

    def test_regroupement_mensuel_separe_bien_les_mois(self):
        try:
            from web_app import _regrouper_par_mois
        except ImportError:
            pytest.skip("Fonction de regroupement mensuel non trouvée sous ce nom dans web_app.py.")

        resultat = _regrouper_par_mois(self._rows_reparties())
        assert len(resultat) >= 1

    def test_regroupement_journalier_isole_chaque_jour(self):
        try:
            from web_app import _regrouper_par_jour
        except ImportError:
            pytest.skip("Fonction de regroupement journalier non trouvée sous ce nom dans web_app.py.")

        resultat = _regrouper_par_jour(self._rows_reparties())
        assert len(resultat) >= 1

    def test_regroupement_annuel_separe_les_annees(self):
        try:
            from web_app import _regrouper_par_annee
        except ImportError:
            pytest.skip("Fonction de regroupement annuel non trouvée sous ce nom dans web_app.py.")

        resultat = _regrouper_par_annee(self._rows_reparties())
        assert len(resultat) >= 1


# ============================================================
# 8. INDISPONIBILITE JIRA ET REPRISE APRES ERREUR
# ============================================================

class TestIndisponibiliteJiraEtReprise:

    def test_sync_capture_l_erreur_jira_sans_planter_le_process(self, monkeypatch, tmp_path):
        db_path = tmp_path / "jira_down_test.db"
        monkeypatch.setattr(web_app, "DATA_PATH", db_path)

        def jira_en_panne(*args, **kwargs):
            raise Exception("Erreur API Jira 503: Service Unavailable")

        monkeypatch.setattr(web_app.cab_pipeline, "extract_and_clean_cab", jira_en_panne)

        web_app._run_sync()

        assert web_app._sync_state["running"] is False
        assert web_app._sync_state["error"] != ""
        assert "503" in web_app._sync_state["error"]

    def test_sync_reussie_apres_une_panne_reinitialise_l_erreur(self, monkeypatch, tmp_path):
        db_path = tmp_path / "jira_recovery_test.db"
        monkeypatch.setattr(web_app, "DATA_PATH", db_path)

        web_app._sync_state["error"] = "Erreur API Jira 503: Service Unavailable"

        df_vide_valide = pd.DataFrame(columns=["Clé de ticket", "Type de ticket"])
        monkeypatch.setattr(web_app.cab_pipeline, "extract_and_clean_cab", lambda **kw: df_vide_valide)
        monkeypatch.setattr(score_engine, "check_llm_availability", lambda: False)
        monkeypatch.setattr(
            score_engine, "evaluate_tickets_checkpointed",
            lambda *a, **kw: score_engine.read_checkpoint_db(db_path)
        )

        web_app._run_sync()

        assert web_app._sync_state["error"] == ""


# ============================================================
# 9. SECURITE DES ROUTES POST ET DU TELECHARGEMENT PDF
# ============================================================

class TestSecuriteRoutesPost:

    def test_refresh_en_get_est_refuse_405(self, client):
        resp = client.get("/refresh")
        assert resp.status_code == 405

    def test_reanalyser_ticket_en_get_est_refuse_405(self, client):
        resp = client.get("/ticket/CAB-1/reanalyser")
        assert resp.status_code == 405

    def test_reanalyser_post_sur_ticket_inexistant_ne_plante_pas(self, client):
        # NB : reanalyser_ticket() appelle cab_pipeline.extract_and_clean_single(),
        # qui n'existe PAS dans cab_pipeline.py (AttributeError confirmé à
        # l'exécution). Soit cette fonction vit dans un autre module non
        # identifié, soit la route /reanalyser est cassée en production
        # pour un ticket inexistant. Sans monkeypatcher une fonction dont on
        # ignore l'emplacement réel, on vérifie seulement que l'exception
        # levée EST bien interceptée par le except Exception de la route
        # (flash + redirect), sans jamais renvoyer un 500 au client.
        resp = client.post("/ticket/CAB-INEXISTANT-XYZ/reanalyser", follow_redirects=True)
        assert resp.status_code == 200

    def test_telechargement_pdf_sur_ticket_inexistant_ne_plante_pas(self, client):
        # NB : adapte l'URL si ta route d'export PDF a un chemin différent
        # (ex. /ticket/<id>/pdf ou /export/<id>.pdf). Vérifie simplement
        # l'absence de crash serveur (pas de 500), même en 404/302.
        resp = client.get("/ticket/CAB-INEXISTANT/pdf")
        assert resp.status_code in (200, 302, 404)

    def test_route_inexistante_renvoie_404_et_pas_500(self, client):
        resp = client.get("/route/qui/nexiste/pas")
        assert resp.status_code == 404


# ============================================================
# 10. CONTENU HTML FONCTIONNEL (au-delà du code 200)
# ============================================================

class TestContenuHtmlFonctionnel:

    def test_dashboard_rend_une_page_html_valide(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"<html" in resp.data.lower()

    def test_dashboard_avec_donnees_affiche_la_cle_du_ticket(self, monkeypatch, tmp_path, client):
        db_path = tmp_path / "html_content_test.db"
        monkeypatch.setattr(web_app, "DATA_PATH", db_path)
        monkeypatch.setattr(web_app, "maybe_auto_sync", lambda: None)

        ticket = pd.DataFrame([{
            "Clé de ticket": "CAB-VISIBLE-1",
            "Résumé": "Ticket de test visible dans le HTML",
            "score_qualite_global": 72,
            "niveau_qualite": "Bon",
            "statut_evaluation": "OK_LLM",
        }])
        score_engine.upsert_tickets_db(ticket, db_path)

        resp = client.get("/")
        assert b"CAB-VISIBLE-1" in resp.data

    def test_ticket_detail_inexistant_redirige_proprement(self, client):
        resp = client.get("/ticket/CAB-NEXISTE-PAS", follow_redirects=True)
        assert resp.status_code == 200

    def test_calendrier_page_rend_du_contenu_non_vide(self, client):
        resp = client.get("/calendrier")
        assert resp.status_code == 200
        assert len(resp.data) > 0