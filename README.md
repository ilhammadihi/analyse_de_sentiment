# Plateforme d'analyse de sentiment des avis clients — Socle (Phase 1)

Collecte multi-sources d'avis clients → analyse de sentiment (FR/EN) → stockage
PostgreSQL → **API temps réel** (REST + SSE) → **alerting** (log / e-mail / webhook).

> Phase 1 = le socle. Le **dashboard** n'est pas inclus (choix de techno réservé
> à l'équipe) : il se branchera sur l'API. La **phase 2** (agents IA marketing)
> consommera la même API.

## Démarrage rapide (Docker — recommandé pour l'environnement de test)

```bash
cp .env.example .env        # ajuster si besoin
docker compose up --build
```

Cela lance 3 services :

| Service    | Rôle                                             | Accès |
|------------|--------------------------------------------------|-------|
| `postgres` | Base de données (schéma appliqué au 1er démarrage) | localhost:5432 |
| `api`      | API FastAPI (REST + SSE)                          | http://localhost:8000/docs |
| `worker`   | Pipeline planifié (collecte + sentiment + alerting) | — |

- Documentation interactive de l'API : **http://localhost:8000/docs**
- Santé : **http://localhost:8000/health**
- Flux temps réel (SSE) : **http://localhost:8000/stream**

## Architecture

```
reviews/
├── domain/       modèles (Pydantic v2) + moteur de sentiment — purs, sans I/O
├── collectors/   1 classe par source ; collectent SEULEMENT (pas de BD)
├── processing/   résilience (retry/timeout)
├── storage/      db (pool) + repository (requêtes) + schema.sql
├── alerting/     règles + notifieurs (log/email/webhook) + manager
├── pipeline/     orchestrateur (injection de dépendances) + reporting
├── api/          FastAPI : routes stats/reviews/alerts/runs + SSE
├── scheduling.py planificateur APScheduler (service worker)
└── cli.py        point d'entrée : init-db | run | serve | schedule
```

Flux : `scheduler → collect → sentiment → dédup/persistance → alerting`,
l'API lit PostgreSQL et sert le dashboard (et, demain, les agents IA).

## Utilisation en ligne de commande

```bash
python -m reviews init-db          # créer/vérifier le schéma
python -m reviews run              # un run complet du pipeline
python -m reviews run --dry-run    # collecte + sentiment, sans écrire en BD
python -m reviews serve            # lancer l'API
python -m reviews schedule         # boucle planifiée (APScheduler)
```

## Principaux endpoints API

| Méthode | Route                     | Description |
|---------|---------------------------|-------------|
| GET | `/stats/overview`             | KPI globaux (volume, sentiment, note moyenne, 24h) |
| GET | `/stats/sentiment-trend`      | Tendance quotidienne du sentiment |
| GET | `/stats/by-company`           | Répartition par entreprise |
| GET | `/reviews`                    | Derniers avis (filtres company / sentiment) |
| GET | `/alerts`                     | Alertes récentes |
| GET | `/runs`, `/runs/{id}`         | Historique des runs |
| GET | `/stream`                     | Flux SSE temps réel (snapshot périodique) |

## Configuration

Toute la configuration passe par des variables d'environnement — voir
[.env.example](.env.example) (sources activées, fréquence de collecte, seuils
d'alerte, SMTP, webhook, API).

## Tests

```bash
pip install -r requirements.txt
python -m pytest        # 25 tests, sans base de données requise
```

Les tests tournent sans PostgreSQL : le pipeline est testé par injection de
faux repositories, ce qui illustre le découpage en couches.
