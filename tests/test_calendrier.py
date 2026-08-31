"""
tests/test_calendrier.py
========================
Tests de la logique de calcul des échéances CAB (fonction
_calculer_echeance dans web_app.py) : délais selon le type de
changement, et classification en 5 statuts (a_venir, echu,
valide_a_temps, valide_en_retard, clos_sans_date).

Exécution : python -m pytest tests/test_calendrier.py -v
"""

import sys
import os
from datetime import datetime, timedelta

import pandas as pd
import pytest

# Permet d'importer web_app.py depuis la racine du projet, quelle que
# soit la façon dont pytest est lancé (racine ou dossier tests/).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from web_app import _calculer_echeance, DELAI_VALIDATION_HEURES


# ------------------------------------------------------------
# Fixtures : construction de lignes de ticket factices (dict-like,
# comme le seraient les lignes d'un DataFrame pandas via row.get(...)).
# ------------------------------------------------------------

def _make_row(**overrides):
    """Construit une ligne de ticket minimale avec des valeurs par
    défaut neutres, surchargées par les kwargs fournis."""
    base = {
        "Clé de ticket": "CAB-TEST-1",
        "Résumé": "Ticket de test",
        "Date de CAB": None,
        "Date de validation": None,
        "Type du changement": "Standard",
        "etat_jira": "",
        "État": "",
        "score_qualite_global": 50,
    }
    base.update(overrides)
    return pd.Series(base)


# ------------------------------------------------------------
# 1. Délais selon le type de changement
# ------------------------------------------------------------

class TestDelaisSelonType:

    def test_urgent_delai_48h(self):
        assert DELAI_VALIDATION_HEURES.get("urgent") == 48

    def test_standard_delai_72h(self):
        assert DELAI_VALIDATION_HEURES.get("standard") == 72

    def test_normal_delai_72h(self):
        assert DELAI_VALIDATION_HEURES.get("normal") == 72

    def test_echeance_urgent_est_cab_plus_48h(self):
        date_cab = datetime(2026, 1, 10, 9, 0)
        row = _make_row(
            **{
                "Date de CAB": date_cab.strftime("%d/%m/%Y %H:%M"),
                "Type du changement": "Urgent",
                "Date de validation": (date_cab + timedelta(hours=1)).strftime("%d/%m/%Y %H:%M"),
            }
        )
        result = _calculer_echeance(row)
        assert result is not None
        assert result["echeance_prevue"] == date_cab + timedelta(hours=48)

    def test_echeance_normal_ou_standard_est_cab_plus_72h(self):
        date_cab = datetime(2026, 1, 10, 9, 0)
        for type_libelle in ("Normal", "Standard"):
            row = _make_row(
                **{
                    "Date de CAB": date_cab.strftime("%d/%m/%Y %H:%M"),
                    "Type du changement": type_libelle,
                    "Date de validation": (date_cab + timedelta(hours=1)).strftime("%d/%m/%Y %H:%M"),
                }
            )
            result = _calculer_echeance(row)
            assert result["echeance_prevue"] == date_cab + timedelta(hours=72), (
                f"Échec pour Type du changement = {type_libelle!r}"
            )


# ------------------------------------------------------------
# 2. Classification des statuts
# ------------------------------------------------------------

class TestStatutsEcheance:

    def test_validation_avant_echeance_est_valide_a_temps(self):
        date_cab = datetime.now() - timedelta(days=10)
        date_validation = date_cab + timedelta(hours=10)  # bien avant les 72h

        row = _make_row(
            **{
                "Date de CAB": date_cab.strftime("%d/%m/%Y %H:%M"),
                "Type du changement": "Standard",
                "Date de validation": date_validation.strftime("%d/%m/%Y %H:%M"),
            }
        )
        result = _calculer_echeance(row)
        assert result["statut"] == "valide_a_temps"

    def test_validation_apres_echeance_est_valide_en_retard(self):
        date_cab = datetime.now() - timedelta(days=10)
        date_validation = date_cab + timedelta(hours=100)  # après les 72h

        row = _make_row(
            **{
                "Date de CAB": date_cab.strftime("%d/%m/%Y %H:%M"),
                "Type du changement": "Standard",
                "Date de validation": date_validation.strftime("%d/%m/%Y %H:%M"),
            }
        )
        result = _calculer_echeance(row)
        assert result["statut"] == "valide_en_retard"

    def test_aucune_validation_et_delai_depasse_est_echu(self):
        # Date de CAB il y a 10 jours, type Standard (72h) : l'échéance
        # est largement dépassée aujourd'hui, et le ticket est toujours
        # ouvert dans Jira (etat_jira neutre, ni terminal ni "rejeté").
        date_cab = datetime.now() - timedelta(days=10)

        row = _make_row(
            **{
                "Date de CAB": date_cab.strftime("%d/%m/%Y %H:%M"),
                "Type du changement": "Standard",
                "Date de validation": None,
                "etat_jira": "En cours",
            }
        )
        result = _calculer_echeance(row)
        assert result["statut"] == "echu"

    def test_aucune_validation_et_delai_futur_est_a_venir(self):
        # Date de CAB dans 2 heures : l'échéance (CAB + 72h) est encore
        # loin dans le futur.
        date_cab = datetime.now() + timedelta(hours=2)

        row = _make_row(
            **{
                "Date de CAB": date_cab.strftime("%d/%m/%Y %H:%M"),
                "Type du changement": "Standard",
                "Date de validation": None,
                "etat_jira": "En cours",
            }
        )
        result = _calculer_echeance(row)
        assert result["statut"] == "a_venir"

    def test_ticket_refuse_sans_validation_est_clos_sans_date(self):
        date_cab = datetime.now() - timedelta(days=10)

        row = _make_row(
            **{
                "Date de CAB": date_cab.strftime("%d/%m/%Y %H:%M"),
                "Type du changement": "Standard",
                "Date de validation": None,
                "etat_jira": "Refusé",
            }
        )
        result = _calculer_echeance(row)
        assert result["statut"] == "clos_sans_date"

    @pytest.mark.parametrize("etat", ["Refusé", "Clôturé", "Abandonné"])
    def test_etats_terminaux_sans_validation_sont_clos_sans_date(self, etat):
        date_cab = datetime.now() - timedelta(days=10)

        row = _make_row(
            **{
                "Date de CAB": date_cab.strftime("%d/%m/%Y %H:%M"),
                "Type du changement": "Standard",
                "Date de validation": None,
                "etat_jira": etat,
            }
        )
        result = _calculer_echeance(row)
        assert result["statut"] == "clos_sans_date", f"Échec pour état Jira = {etat!r}"

    @pytest.mark.parametrize("etat", ["Ouvert", "En attente", "Validé sous réserve"])
    def test_etats_non_terminaux_sans_validation_et_delai_depasse_sont_echu(self, etat):
        date_cab = datetime.now() - timedelta(days=10)

        row = _make_row(
            **{
                "Date de CAB": date_cab.strftime("%d/%m/%Y %H:%M"),
                "Type du changement": "Standard",
                "Date de validation": None,
                "etat_jira": etat,
            }
        )
        result = _calculer_echeance(row)
        assert result["statut"] == "echu", f"Échec pour état Jira = {etat!r}"

    def test_pas_de_date_de_cab_retourne_none(self):
        row = _make_row(**{"Date de CAB": None})
        result = _calculer_echeance(row)
        assert result is None
