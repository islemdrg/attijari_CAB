"""
tests/test_score_engine.py
===========================
Tests du moteur de notation qualité (quality/score_engine.py) : calcul
des scores/barèmes, cohérence des dates, résolution description/impact
parent-enfant, et nettoyage des champs texte riches (ADF Jira).

Exécution : python -m pytest tests/test_score_engine.py -v
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from quality.score_engine import (
    COMPLETENESS_POINTS,
    FORMAT_POINTS,
    DESCRIPTION_RELEVANCE_POINTS,
    IMPACT_POINTS,
    COHERENCE_POINTS,
    calculate_completeness,
    calculate_description_format,
    calculate_date_coherence,
    _resolve_description,
    _resolve_impact,
)

try:
    from cab_pipeline import normalize_rich_text
    HAS_NORMALIZE_RICH_TEXT = True
except ImportError:
    HAS_NORMALIZE_RICH_TEXT = False


# ------------------------------------------------------------
# 1. Intégrité du barème /100
# ------------------------------------------------------------
# Ces tests répliquent les assert déjà présents dans score_engine.py
# (sum(...) == 25/15/25/15/20). Les garder ici aussi permet de les
# détecter dans la CI même si l'assert du module est un jour retiré,
# et de documenter explicitement la règle attendue.

class TestBaremeOfficiel:

    def test_completude_totalise_25_points(self):
        assert sum(COMPLETENESS_POINTS.values()) == 25

    def test_format_totalise_15_points(self):
        assert sum(FORMAT_POINTS.values()) == 15

    def test_pertinence_totalise_25_points(self):
        assert sum(DESCRIPTION_RELEVANCE_POINTS.values()) == 25

    def test_impact_totalise_15_points(self):
        assert sum(IMPACT_POINTS.values()) == 15

    def test_coherence_totalise_20_points(self):
        assert sum(COHERENCE_POINTS.values()) == 20

    def test_total_general_est_100_points(self):
        total = (
            sum(COMPLETENESS_POINTS.values())
            + sum(FORMAT_POINTS.values())
            + sum(DESCRIPTION_RELEVANCE_POINTS.values())
            + sum(IMPACT_POINTS.values())
            + sum(COHERENCE_POINTS.values())
        )
        assert total == 100, (
            f"Le barème total doit être 100 pts, trouvé {total} pts. "
            "Un score_qualite_global à 100/100 ne doit être atteignable "
            "que si le barème réel totalise bien 100 (pas plus)."
        )


# ------------------------------------------------------------
# 2. Calcul de la complétude
# ------------------------------------------------------------

class TestCompletude:

    def test_ticket_complet_obtient_le_score_max(self):
        ticket_complet = {
            "Description": "Texte de description.",
            "Impact de l'action": "Texte d'impact.",
            "Criticité": "Élevée",
            "Risque": "Moyen",
            "Nom de l'application": "AppTest",
            "Type du changement": "Standard",
            "Type de la plateforme": "Production",
            "Date de mise en production": "10/01/2026",
            "Date de CAB": "08/01/2026",
            "Date de validation": "09/01/2026",
        }
        score, missing, details = calculate_completeness(ticket_complet)
        assert score == sum(COMPLETENESS_POINTS.values())
        assert missing == []

    def test_ticket_vide_obtient_score_zero(self):
        score, missing, details = calculate_completeness({})
        assert score == 0
        assert set(missing) == set(COMPLETENESS_POINTS.keys())

    def test_champ_partiellement_rempli_est_compte_manquant(self):
        ticket = {"Description": "Non renseigné"}
        score, missing, details = calculate_completeness(ticket)
        assert "Description" in missing


# ------------------------------------------------------------
# 3. Format de la description
# ------------------------------------------------------------

class TestFormatDescription:

    def test_description_vide_score_zero(self):
        score, result = calculate_description_format({"Description": ""})
        assert score == 0
        assert result["non_vide"] is False

    def test_description_courte_ne_valide_pas_50_mots(self):
        ticket = {"Description": "Correctif rapide sur le serveur."}
        score, result = calculate_description_format(ticket)
        assert result["50_mots"] is False

    def test_description_longue_et_structuree_valide_tous_les_criteres(self):
        description = (
            "Suite à une alerte de saturation mémoire constatée sur le serveur "
            "SRV-app-01, une augmentation de la RAM de 8 à 16 Go est réalisée "
            "afin de stabiliser le service applicatif de paiement en ligne. "
            "Un test de charge sera effectué après le redémarrage pour valider "
            "la bonne prise en compte de la ressource supplémentaire."
        )
        score, result = calculate_description_format({"Description": description})
        assert result["non_vide"] is True
        assert result["20_mots"] is True
        assert result["50_mots"] is True
        assert result["deux_phrases"] is True
        assert result["lisible"] is True

    def test_description_reduite_a_mot_vague_est_illisible(self):
        for valeur_vague in ("RAS", "test", "N/A", "-"):
            score, result = calculate_description_format({"Description": valeur_vague})
            assert result["lisible"] is False, f"Échec pour valeur = {valeur_vague!r}"


# ------------------------------------------------------------
# 4. Cohérence des dates
# ------------------------------------------------------------

class TestCoherenceDates:

    def test_dates_manquantes_score_zero(self):
        score, details = calculate_date_coherence({})
        assert score == 0

    def test_cab_posterieure_a_validation_est_incoherente(self):
        ticket = {
            "Date de CAB": "15/01/2026",
            "Date de validation": "10/01/2026",
        }
        score, details = calculate_date_coherence(ticket)
        assert score == 0
        assert any("postérieure" in d for d in details)

    def test_chronologie_coherente_sans_production_score_max(self):
        ticket = {
            "Date de CAB": "10/01/2026",
            "Date de validation": "12/01/2026",
        }
        score, details = calculate_date_coherence(ticket)
        assert score == 5

    def test_production_avant_cab_est_incoherente(self):
        ticket = {
            "Date de CAB": "10/01/2026",
            "Date de validation": "12/01/2026",
            "Date de mise en production": "05/01/2026",
        }
        score, details = calculate_date_coherence(ticket)
        assert score == 0

    def test_production_apres_validation_est_incoherente(self):
        ticket = {
            "Date de CAB": "10/01/2026",
            "Date de validation": "12/01/2026",
            "Date de mise en production": "20/01/2026",
        }
        score, details = calculate_date_coherence(ticket)
        assert score == 0

    def test_production_dans_la_fenetre_est_coherente(self):
        ticket = {
            "Date de CAB": "10/01/2026",
            "Date de validation": "12/01/2026",
            "Date de mise en production": "11/01/2026",
        }
        score, details = calculate_date_coherence(ticket)
        assert score == 5


# ------------------------------------------------------------
# 5. Résolution description/impact (ticket -> parent -> absente)
# ------------------------------------------------------------

class TestResolutionDescriptionParent:

    def test_description_propre_au_ticket_est_prioritaire(self):
        ticket = {
            "Description": "Description propre du ticket.",
            "Parent description": "Description du parent.",
        }
        text, source = _resolve_description(ticket)
        assert text == "Description propre du ticket."
        assert source == "ticket"

    def test_description_parent_utilisee_si_propre_absente(self):
        ticket = {
            "Description": "",
            "Parent description": "Description du parent.",
        }
        text, source = _resolve_description(ticket)
        assert text == "Description du parent."
        assert source == "parent"

    def test_description_absente_si_ni_ticket_ni_parent(self):
        text, source = _resolve_description({})
        assert text == ""
        assert source == "absente"

    def test_impact_suit_la_meme_logique_que_description(self):
        ticket = {
            "Impact de l'action": "",
            "Parent impact": "Impact du parent.",
        }
        text, source = _resolve_impact(ticket)
        assert text == "Impact du parent."
        assert source == "parent"


# ------------------------------------------------------------
# 6. Nettoyage des champs texte riches (ADF Jira) — si disponible
# ------------------------------------------------------------

@pytest.mark.skipif(not HAS_NORMALIZE_RICH_TEXT, reason="normalize_rich_text absent de cab_pipeline")
class TestNettoyageTexteRiche:

    def test_texte_simple_reste_inchange(self):
        assert normalize_rich_text("Texte simple sans markup.") == "Texte simple sans markup."

    def test_markup_gras_est_retire(self):
        result = normalize_rich_text("*Texte en gras*")
        assert "*" not in result

    def test_valeur_vide_est_preservee(self):
        import pandas as pd
        assert pd.isna(normalize_rich_text(None)) or normalize_rich_text(None) is None
