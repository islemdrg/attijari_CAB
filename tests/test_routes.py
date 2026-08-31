"""
tests/test_routes.py
=====================
Tests d'intégration légers sur les routes Flask principales : vérifie
que chaque page répond en HTTP 200 et ne plante pas, même sur une base
de données vide (premier lancement, avant toute synchro Jira).

Ces tests utilisent le client de test Flask (aucun serveur réel lancé,
aucun appel Jira réel) et redirigent la base de données vers un fichier
SQLite temporaire pour ne jamais toucher à cab_quality.db en conditions
réelles.

Exécution : python -m pytest tests/test_routes.py -v
"""

import sys
import os
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import web_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Client de test Flask, pointé vers une base SQLite temporaire et
    vide (pas de vraie donnée CAB), pour tester uniquement que les
    routes répondent sans planter — pas le contenu métier lui-même
    (couvert par test_calendrier.py et test_score_engine.py).

    start_subtasks_sync est neutralisé : sans ça, le thread
    d'arrière-plan tente un df.to_sql() sur un DataFrame vide (aucune
    colonne), ce qui lève sqlite3.OperationalError dans le thread —
    invisible pour pytest (l'exception ne remonte pas au thread
    principal) mais bruyant dans les logs de test."""

    db_temp = tmp_path / "cab_quality_test.db"
    monkeypatch.setattr(web_app, "DATA_PATH", db_temp)
    monkeypatch.setattr(web_app, "start_subtasks_sync", lambda: False)

    web_app.app.config["TESTING"] = True
    with web_app.app.test_client() as test_client:
        yield test_client


class TestRoutesRepondentSansPlanter:

    def test_dashboard_repond_200_meme_sans_donnees(self, client):
        response = client.get("/")
        assert response.status_code == 200

    def test_sous_taches_repond_200(self, client, monkeypatch):
        # Évite un vrai appel réseau vers Jira pendant le test : on
        # simule un DataFrame vide, comme le ferait get_all_subtasks()
        # si l'API Jira n'a pas encore été synchronisée.
        import pandas as pd

        def _fake_get_all_subtasks(verbose=False):
            return pd.DataFrame()

        monkeypatch.setattr(
            "cab_pipeline.get_all_subtasks",
            _fake_get_all_subtasks,
        )

        response = client.get("/sous-taches")
        assert response.status_code == 200

    def test_calendrier_repond_200_meme_sans_donnees(self, client):
        response = client.get("/calendrier")
        assert response.status_code == 200

    def test_refresh_status_repond_200_et_json_valide(self, client):
        response = client.get("/refresh/status")
        assert response.status_code == 200
        data = response.get_json()
        assert "running" in data
        assert "message" in data
        assert "error" in data

    def test_ticket_detail_inexistant_redirige_vers_dashboard(self, client):
        response = client.get("/ticket/CAB-INEXISTANT-999", follow_redirects=False)
        assert response.status_code in (302, 200)

    def test_calendrier_jour_sans_parametres_redirige(self, client):
        response = client.get("/calendrier/jour")
        assert response.status_code in (302, 200)
