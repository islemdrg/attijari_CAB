"""
tests/conftest.py
==================
Fixtures partagées entre TOUS les fichiers de tests du dossier tests/.
pytest charge ce fichier automatiquement (aucun import nécessaire) et rend
ses fixtures disponibles dans test_routes.py, test_complementaire.py, etc.

CETTE FIXTURE `client` A ÉTÉ DÉPLACÉE DEPUIS tests/test_routes.py :
copie-colle ici le corps EXACT de la fixture déjà définie là-bas (probablement
en haut du fichier, décorée @pytest.fixture), puis SUPPRIME-la de
test_routes.py pour éviter une définition dupliquée. Le squelette ci-dessous
reprend la logique déjà confirmée dans nos échanges précédents (redirection
de DATA_PATH vers une base temporaire + neutralisation du thread de sync des
sous-tâches) : adapte-le si ta version réelle diffère légèrement.
"""

import pytest

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
    invisible pour pytest mais bruyant dans les logs de test."""

    db_temp = tmp_path / "cab_quality_test.db"
    monkeypatch.setattr(web_app, "DATA_PATH", db_temp)
    monkeypatch.setattr(web_app, "start_subtasks_sync", lambda: False)

    web_app.app.config["TESTING"] = True
    with web_app.app.test_client() as test_client:
        yield test_client