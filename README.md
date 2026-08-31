# Attijari CAB — Plateforme de pilotage des changements

Plateforme interne de gestion des changements **CAB (Change Advisory Board)**, développée dans le cadre d'un stage d'été chez **Attijari Bank**, synchronisée avec le serveur Jira interne de gestion des changements.

## Contexte et problématique

Le serveur Jira interne de gestion des changements souffre de plusieurs limites opérationnelles :

- des informations **manquantes ou mal organisées** sur les tickets, difficiles à exploiter au quotidien ;
- une interface **peu adaptée au suivi rapide** (« daily check ») de l'état des projets et des validations ;
- **aucune visibilité consolidée** sur les échéances de validation, exposant l'organisation à des retards non détectés ;
- **aucune analyse qualitative** du contenu des tickets (complétude, pertinence, cohérence de la description).

Cette plateforme a été développée pour répondre à ces quatre limites, en s'appuyant sur les données réelles extraites de Jira sans en modifier le fonctionnement natif.

## Fonctionnalités

### 1. Tableau de bord centralisé
Vue consolidée de tous les tickets CAB avec leurs scores de qualité, statuts de validation, niveau (Excellent / Bon / Moyen / Faible) et état Jira. Filtrage par recherche libre, niveau, statut d'évaluation, année et état du workflow. Export d'un **rapport PDF de synthèse** portant exactement sur le sous-ensemble de tickets affiché (filtres appliqués compris), avec statistiques par sous-score, champs manquants les plus fréquents et recommandations récurrentes.

### 2. Calendrier des échéances
Calcul automatique de l'échéance de validation prévue pour chaque ticket (délai de 48h pour les changements urgents, 72h pour les changements normaux/standards, à partir de la date de passage en CAB), avec classification en cinq statuts : à venir, échu, validé à temps, validé en retard, ou clos sans date. Vue calendaire mensuelle et blocs dédiés aux échéances dépassées et aux tickets en attente de validation, regroupés par année.

### 3. Notation qualité par intelligence artificielle
Moteur d'analyse combinant :
- une **analyse sémantique par LLM local** (Ollama, modèle `qwen2.5:3b`), qui évalue la pertinence de la description, la qualité de l'impact et la cohérence du ticket comme le ferait un relecteur humain expérimenté, en générant une analyse rédigée, des preuves factuelles et des recommandations d'amélioration spécifiques à chaque ticket ;
- un **repli heuristique déterministe** (expressions régulières), activé automatiquement si le LLM est indisponible, pour garantir qu'aucun ticket ne reste sans score.

Barème officiel sur 100 points : Complétude (25), Format (15), Pertinence (25), Impact (15), Cohérence (20).

### 4. Fiches détaillées et rapports PDF
Fiche complète par ticket (description, impact, sous-tâches liées, détail du score par catégorie, explications, preuves et recommandations), avec export PDF individuel au thème de la banque.

## Architecture technique

```
├── web_app.py            # Application Flask : routes, dashboard, calendrier, synchronisation
├── quality/
│   └── score_engine.py   # Moteur de notation qualité (LLM + heuristique)
├── cab_pipeline.py        # Extraction et nettoyage des données depuis l'API Jira Cloud
├── report_pdf.py           # Génération des rapports PDF (ticket individuel + synthèse portefeuille)
├── templates/              # Gabarits HTML (Jinja2) : dashboard, fiche ticket, calendrier
├── static/                  # Feuilles de style et logo
├── tests/                   # Suite de tests automatisés (pytest)
└── run_web.ps1              # Script de lancement rapide (Windows / PowerShell)
```

### Stack technique

| Composant | Technologie |
|---|---|
| Backend web | Python 3.13, Flask |
| Extraction de données | API Jira Cloud (REST v3) |
| Stockage | SQLite (upsert transactionnel par clé de ticket) |
| Analyse IA | Ollama (LLM local, `qwen2.5:3b`) |
| Génération PDF | ReportLab |
| Tests | pytest |

### Flux de données

1. `cab_pipeline.py` interroge l'API Jira (requête JQL sur le projet CAB), nettoie les champs texte riches (format ADF/wiki), et élimine les colonnes vides, constantes ou techniques.
2. `quality/score_engine.py` évalue chaque ticket : résolution de la description (ticket ou ticket parent en cas de vote individuel), calcul des cinq sous-scores, appel au LLM ou repli heuristique, puis enregistrement en base SQLite avec un mécanisme d'upsert incrémental (seuls les nouveaux tickets ou ceux notés avec une version obsolète du moteur sont retraités).
3. `web_app.py` lit la base SQLite pour alimenter le dashboard, le calendrier et les fiches ticket, et orchestre la synchronisation en arrière-plan (thread dédié, sans bloquer l'interface).
4. `report_pdf.py` génère les documents PDF à la demande, à partir des données déjà calculées.

## Installation

### Prérequis
- Python 3.11 ou supérieur
- [Ollama](https://ollama.com) installé localement (moteur d'exécution du LLM — voir section dédiée ci-dessous)
- Un accès à l'API Jira Cloud (token d'API) sur le projet CAB

### Installation du LLM local (Ollama)

Le moteur de notation qualité s'appuie sur un modèle de langage exécuté **localement**, sans dépendance à une API cloud externe (obligatoire dans un contexte bancaire). Ollama est le logiciel qui fait tourner ce modèle sur la machine ; il est **entièrement séparé** des paquets Python installés via `pip`, il faut l'installer à part.

**1. Installer le logiciel Ollama**

Télécharger et installer l'exécutable depuis [ollama.com/download](https://ollama.com/download) (disponible pour Windows, macOS et Linux). L'installation ajoute la commande `ollama` au terminal.

**2. Télécharger le modèle `qwen2.5:3b`**

Dans un terminal (PowerShell sous Windows) :

```bash
ollama pull qwen2.5:3b
```

Cette commande télécharge le modèle une seule fois (environ 2 Go) et le stocke localement. Choix de ce modèle : suffisamment léger pour tourner sur un poste de travail standard sans GPU dédié, tout en offrant une capacité d'analyse sémantique suffisante pour la lecture critique de tickets CAB.

**3. Démarrer le serveur Ollama**

Avant de lancer l'application Flask, démarrer le serveur Ollama dans un terminal dédié (à laisser ouvert en arrière-plan) :

```bash
ollama serve
```

Le serveur écoute par défaut sur `http://127.0.0.1:11434`, l'adresse configurée dans `quality/score_engine.py` (constante `OLLAMA_HOST`).

**Fonctionnement en l'absence du LLM** : si `ollama serve` n'est pas lancé, ou si le modèle n'a pas été téléchargé, l'application reste pleinement fonctionnelle — `score_engine.py` détecte l'indisponibilité du LLM (vérification en moins de 2 secondes) et bascule automatiquement sur un **analyseur heuristique** basé sur des expressions régulières, qui reste différencié par ticket mais moins fin qu'une lecture sémantique complète. Aucune interruption de service, aucune configuration supplémentaire nécessaire.

### Mise en place de l'application

```bash
git clone https://github.com/islemdrg/attijari_CAB.git
cd attijari_CAB

python -m venv env
env\Scripts\activate          # Windows
# source env/bin/activate     # Linux/macOS

pip install -r requirements.txt
```

### Configuration de la connexion Jira

Le fichier `.env` n'est **pas inclus dans le dépôt** (secrets d'authentification). Créer un fichier nommé `.env` à la racine du projet, avec le contenu suivant, en remplaçant chaque valeur par les identifiants réels :

```
BASE_URL=https://votre-domaine.atlassian.net
EMAIL=votre.email@attijaribank.com.tn
TOKEN=votre_token_api_jira
PROJECT_KEY=CAB
```

| Variable | Description |
|---|---|
| `BASE_URL` | URL racine de l'instance Jira Cloud (sans slash final) |
| `EMAIL` | Adresse email associée au compte Jira utilisé pour l'authentification |
| `TOKEN` | Jeton d'API Jira (généré depuis les paramètres du compte Atlassian) |
| `PROJECT_KEY` | Clé du projet Jira ciblé (`CAB` pour ce projet) |

### Lancement

Démarrer Ollama (voir section précédente), puis l'application :

```bash
ollama serve
python web_app.py
```

Sous Windows, le script `run_web.ps1` permet d'automatiser le lancement :

```powershell
.\run_web.ps1
```

L'application est accessible sur `http://localhost:5000`.

## Tests

```bash
python -m pytest tests -v
```

La suite couvre le calcul des échéances (délais, classification des statuts, cas limites), le moteur de notation (barème, complétude, cohérence des dates, résolution description/parent), et les routes principales de l'application.

## Qualité de code

Un script d'audit statique (`audit_qualite_code.py`) permet de détecter les lignes excessives, blocs de commentaires trop longs, fonctions surdimensionnées, imports inutilisés et duplications de code avant chaque livraison :

```bash
python audit_qualite_code.py web_app.py quality/score_engine.py cab_pipeline.py report_pdf.py
```

## Auteur

Projet réalisé dans le cadre d'un stage d'été en 2026 au sein du département Technologie et Transformation Digital d'**Attijari Bank**.