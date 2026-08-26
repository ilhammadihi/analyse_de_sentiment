# Plateforme d'analyse de sentiment des avis clients — opérateurs télécoms africains

Collecte multi-sources d'avis clients (boutiques d'applications, agences
physiques, plateformes d'avis, presse, réseaux sociaux) → sentiment (lexique
+ LLM optionnel) → taxonomie d'aspects → stockage PostgreSQL (modèle
dimensionnel) → **API temps réel** (REST + SSE) → **dashboard** intégré à
FaceMonitoring → **alerting Telegram** → **3 agents IA** (veille, campagne,
qualité) + un **assistant conversationnel** Telegram.

> Le dashboard n'est **pas** dans ce dépôt : il vit comme module natif dans le
> frontend FaceMonitoring (`src/components/stats/`, onglet « Statistiques »),
> et consomme l'API de ce dépôt en HTTP. Voir [docs/GUIDE_UTILISATEUR.md](docs/GUIDE_UTILISATEUR.md)
> pour ce que ce dashboard montre.

## Démarrage rapide (Docker — recommandé pour l'environnement de test)

```bash
cp .env.example .env        # ajuster si besoin (clé LLM, jetons Telegram…)
docker compose up --build
```

Cela lance 3 services :

| Service    | Rôle                                                | Accès |
|------------|------------------------------------------------------|-------|
| `postgres` | Base de données (schéma appliqué au 1er démarrage) | localhost:5432 |
| `api`      | API FastAPI (REST + SSE)                            | http://localhost:8000/docs |
| `worker`   | Pipeline planifié — UN job APScheduler par source, par régulateur et par agent | — |

- Documentation interactive de l'API : **http://localhost:8000/docs**
- Santé : **http://localhost:8000/health**
- Flux temps réel (SSE) : **http://localhost:8000/stream**

## Architecture

```
reviews/
├── domain/         modèles (Pydantic v2) + moteur de sentiment (lexique FR/EN) — purs, sans I/O
├── collectors/      1 classe par source, collecte SEULEMENT (pas de BD) :
│                     avis clients   — appstore, playstore, google_maps, trustpilot,
│                                       hellopeter, rss_feed, reddit, gdelt, press_feed
│                     marché (pays)  — market_data.py (Banque Mondiale / UIT)
│                     marché (opér.) — ncc_nigeria, anrt_maroc, arcep_benin, nca_ghana
│                                       (régulateurs nationaux, seuls à descendre au
│                                       niveau opérateur avec une cadence infra-annuelle)
├── llm/             couche sémantique optionnelle (taxonomie d'aspects, profils
│                     LLM cloisonnés — désactivable sans rien casser)
├── agents/           insight_agent    (Agent 1 — veille satisfaction quotidienne)
│                     campaign_agent   (Agent 2 — assistant de campagne)
│                     quality/         (Agent 3 — couverture, diagnostic, score de
│                                       confiance, découverte de sources, orphelins)
│                     chat_agent.py    assistant conversationnel Telegram (long polling)
├── processing/      résilience (retry/timeout)
├── storage/          db (pool) + repositories (1 par domaine) + migrations/*.sql
├── alerting/        règles + notifieurs (log / e-mail / webhook / Telegram) + manager
├── pipeline/         orchestrateur (injection de dépendances) + reporting
├── api/              FastAPI : routes stats/reviews/alerts/runs/quality/insights/campaigns + SSE
├── scheduling.py     planificateur : UN JOB PAR SOURCE (jamais un cycle global qui
│                     les enchaîne — une source lente ne doit plus retenir les autres
│                     ni l'alerting derrière elle)
└── cli.py           point d'entrée unique, voir `python -m reviews.cli --help`
```

Flux : `scheduler → collect → sentiment → sémantique (optionnel) →
dédup/persistance → alerting`, les agents lisent la même base et écrivent
leurs propres constats (jamais les avis), l'API sert le dashboard.

## Périmètre couvert

Config-driven (`config/operators.json`) : **96 filiales déclarées / 35
opérateurs / 38 pays** d'Afrique. Chaque filiale porte ses sources déclarées
(quelle app, quelle recherche Google Maps, quel flux RSS…) — ajouter un pays
ou un opérateur ne touche jamais le code des collecteurs.

Quatre filiales ont été désactivées le 26/08/2026 (`dim_subsidiary.active =
false`, migration 027) après vérification qu'elles ne correspondent plus à
une activité réelle : MTN RDC (aucun réseau licencié en RDC), MTN
Guinée-Conakry (vendue à l'État guinéen, déc. 2024), MTN Guinée-Bissau
(vendue à Telecel Group, août 2024), Orange Niger (cédée en 2019, devenue
Zamani Telecom). Leurs avis déjà collectés restent en base pour traçabilité
mais n'apparaissent plus dans les agrégats affichés — voir l'en-tête de la
migration 027 pour les sources.

## Utilisation en ligne de commande

```bash
python -m reviews.cli init-db          # créer/vérifier le schéma (applique les migrations manquantes)
python -m reviews.cli run              # un run complet du pipeline de collecte
python -m reviews.cli serve            # lancer l'API
python -m reviews.cli schedule         # boucle planifiée (APScheduler, service worker)

# Contexte marché
python -m reviews.cli market-data      # indicateurs pays (Banque Mondiale / UIT)
python -m reviews.cli ncc-nigeria      # abonnés GSM par opérateur (NCC Nigeria, mensuel)
python -m reviews.cli anrt-maroc       # abonnés mobile par opérateur (ANRT Maroc, trimestriel)
python -m reviews.cli arcep-benin      # abonnés mobile par opérateur (ARCEP Bénin, annuel)
python -m reviews.cli nca-ghana        # abonnements voix mobile par opérateur (NCA Ghana, trimestriel)

# Agents
python -m reviews.cli agent            # Agent 1 — un passage de veille satisfaction
python -m reviews.cli campaign         # Agent 2 — proposer / lister / faire le bilan d'une campagne
python -m reviews.cli quality          # Agent 3 — couverture, diagnostic, score de confiance
python -m reviews.cli orphelins        # réattribuer les avis sans filiale (écrit sur --appliquer)
python -m reviews.cli chat             # assistant conversationnel (question ponctuelle ou écoute Telegram)

# Alerting
python -m reviews.cli retract-alert <id>  # retirer une alerte (message Telegram effacé, ligne supprimée)
```

`--help` sur n'importe quelle sous-commande donne ses options.

## Principaux endpoints API

| Méthode | Route                          | Description |
|---------|--------------------------------|--------------|
| GET | `/stats/overview`                  | KPI globaux (volume, sentiment, note moyenne, période) |
| GET | `/stats/sentiment-trend`           | Tendance du sentiment dans le temps |
| GET | `/stats/ranking`                   | Classement par filiale/pays/opérateur (voir `granularity`) |
| GET | `/stats/movers`                    | Filiales dont le signal a le plus bougé récemment |
| GET | `/stats/market`, `/stats/market/operators` | Contexte marché — pays (Banque Mondiale/UIT) et par opérateur (régulateurs) |
| GET | `/stats/pipeline-health`           | Santé de la chaîne de collecte (écran d'exploitation) |
| GET | `/reviews`                         | Derniers avis, filtrables (voir `reviews/api/filter_params.py`) |
| GET | `/alerts`                          | Alertes récentes |
| GET | `/runs`, `/runs/{id}`              | Historique des runs de collecte |
| GET/POST | `/quality/*`                   | Agent 3 : couverture, diagnostic, candidates, score de confiance |
| GET/POST | `/insights/*`                  | Agent 1 : rapports de veille |
| GET/POST | `/campaigns/*`                 | Agent 2 : campagnes proposées, bilan |
| GET | `/stream`                          | Flux SSE temps réel (snapshot périodique) |

Liste complète et schémas : http://localhost:8000/docs (Swagger généré par FastAPI).

## Configuration

Toute la configuration passe par des variables d'environnement — voir
[.env.example](.env.example) (sources activées, fréquence de collecte par
source/régulateur, seuils d'alerte, Telegram, LLM, SMTP, webhook, API).

## Tests

```bash
pip install -r requirements.txt
python -m pytest        # ~740 tests, sans base de données requise
```

Les tests tournent sans PostgreSQL pour l'écrasante majorité : le pipeline et
les repositories sont testés par injection de faux curseurs/repositories, ce
qui illustre le découpage en couches. Un sous-ensemble (marqué explicitement)
suppose une base locale pour des vérifications d'intégration ponctuelles.

## Pour aller plus loin

- [docs/GUIDE_UTILISATEUR.md](docs/GUIDE_UTILISATEUR.md) — ce que montre le
  dashboard « Statistiques » côté FaceMonitoring, pour un manager/owner.
- `migrations/*.sql` — chaque fichier documente en tête POURQUOI le
  changement a été fait, pas seulement le DDL ; c'est la source la plus fiable
  sur l'évolution du modèle de données.
- Mémoire de session (hors dépôt) — historique des décisions et des
  arbitrages métier au fil du projet.
