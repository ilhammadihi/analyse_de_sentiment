# Déploiement sur le serveur de test (digiwise-4)

Ce guide documente le déploiement réel effectué le 27 août 2026 sur `digiwise-4`
(`ssh -p 2288 admin@197.230.47.54`), un serveur **mutualisé** hébergeant de
nombreux autres projets (radmetal, finance, odoo-marketplace, n8n, ifusionx...).
Tout ce qui suit a été vérifié en conditions réelles, pas deviné.

## Architecture

Deux dossiers séparés côté serveur, comme en local :

| Dossier serveur | Contenu | Géré par |
|---|---|---|
| `/home/admin/Projects/sentiment-backend/` | Ce dépôt (clone git) | Docker Compose (3 conteneurs) |
| `/home/admin/Projects/FaceMonitoring/` | Dépôt FaceMonitoring (pas le nôtre) | PM2 (backend) + build statique (frontend) |

Le frontend (`src/components/stats/`) est intégré **dans le dépôt
FaceMonitoring**, pas dans celui-ci — voir la section Frontend plus bas.

## Particularités connues de digiwise-4

**Serveur mutualisé : vérifier les ports avant de démarrer quoi que ce soit.**
`ss -tlnp` (ou `netstat -tlnp`) avant de fixer un port. Le 27/08/2026, `8000`
et `5432` (nos ports par défaut) étaient déjà pris par d'autres projets.
`docker-compose.yaml` expose donc `DB_HOST_PORT` et `API_HOST_PORT` (défauts
5432/8000, à surcharger dans `.env`) — utilisés cette fois : `DB_HOST_PORT=5440`,
`API_HOST_PORT=8200`.

**DNS/MTU cassé à l'intérieur des conteneurs Docker.** Vérifié :
`docker run --rm alpine nslookup pypi.org` → `connection timed out`. Ça casse
`docker build` (téléchargement de l'image de base, `pip install`) ET certains
appels applicatifs (RSS/presse vers `news.google.com`, timeouts fréquents).
`/etc/docker/daemon.json` déclare `"mtu": 1442` (non standard) — probablement
la cause. **Signalé à l'équipe/admin, pas corrigé par nous** (config Docker
partagée par tous les projets du serveur, hors de notre périmètre).

**Contournement utilisé pour le build** (tant que le DNS des conteneurs n'est
pas réparé) :
```bash
# En local, où le réseau fonctionne :
docker compose build
docker save sentiment-app:latest | gzip -1 > sentiment-app.tar.gz
scp -P 2288 sentiment-app.tar.gz admin@197.230.47.54:/home/admin/Projects/sentiment-backend/

# Sur le serveur :
docker load -i sentiment-app.tar.gz
docker compose up -d   # ne rebuild pas, utilise l'image déjà chargée
```
C'est pour ça que `docker-compose.yaml` fixe `image: sentiment-app:latest`
pour `api`/`worker` (au lieu de laisser Compose dériver un nom du dossier
parent, qui diffère entre `analyse_de_sentiment` en local et
`sentiment-backend` sur le serveur).

**Image Postgres à réutiliser, pas à télécharger.** Même souci DNS : `docker
images | grep postgres` pour voir ce qui est déjà en cache sur le serveur
avant de forcer un `postgres:15-alpine` qui n'existe peut-être pas. Paramétrable
via `POSTGRES_VERSION` dans `.env` (ex. `POSTGRES_VERSION=16-alpine`).

## Backend — étapes

1. `git clone https://github.com/ilhammadihi/analyse_de_sentiment sentiment-backend`
   **depuis `/home/admin/Projects/`** (pas depuis l'intérieur d'un autre
   dépôt — piège vécu : cloné par erreur dans `FaceMonitoring/`).
2. Transférer le `.env` (jamais via `git`, jamais dans une archive générique —
   voir mémoire `feedback_env_exclusion`) :
   ```
   scp -P 2288 .env admin@197.230.47.54:/home/admin/Projects/sentiment-backend/.env
   ```
3. Ajuster le `.env` pour cet environnement :
   - `DB_HOST_PORT`, `API_HOST_PORT`, `POSTGRES_VERSION` — voir ci-dessus.
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — **laissés vides** pour ce
     serveur de test. Un même bot utilisé par deux workers actifs en même
     temps (local + serveur) posterait les mêmes alertes/veilles en double
     dans le groupe (chaque base a son propre journal de non-répétition, sans
     savoir que l'autre a déjà parlé).
   - `LLM_API_KEY` / `LLM3_API_KEY` — **clés séparées** du dev local
     recommandées. Le cloisonnement de quota (`llm_usage.profil`) n'est PAS
     configurable par `.env` (volontaire, voir `reviews/config.py` — un profil
     réglable par l'exploitant permettrait un reset accidentel du compteur) :
     la seule vraie séparation est une clé API différente par environnement.
4. `docker compose build` (ou le contournement local + transfert ci-dessus si
   le DNS des conteneurs est toujours cassé).
5. `docker compose up -d`.

### Piège de migration résolu

Sur un volume Postgres **réellement neuf**, le worker plantait en boucle :
```
Migration en échec : 009_target_identity.sql
column "target_id" of relation "reviews" already exists
```
Cause : `docker-compose.yaml` montait `./migrations` en
`/docker-entrypoint-initdb.d` — Postgres exécute alors tous les `.sql` en
aveugle au premier démarrage, sans rien enregistrer dans la table
`schema_migrations` que `apply_schema()` (`reviews/storage/db.py`) tient de
son côté. Le worker démarre ensuite, croit repartir de zéro, rejoue tout —
et plante sur une migration non idempotente (`RENAME COLUMN`).

**Corrigé dans le code** (commit `4abf9a7`) : le montage
`docker-entrypoint-initdb.d` a été retiré. `apply_schema()` suffit, tourne à
chaque démarrage du worker, et sait déjà ne jouer chaque migration qu'une
fois. Ce piège ne devrait plus se reproduire — mais si un déploiement futur
utilise un `docker-compose.yaml` antérieur à ce commit, il refera surface.

## Frontend — étapes

Le dashboard (`src/components/stats/` de ce dépôt) doit être copié dans le
**vrai** dépôt FaceMonitoring, qui évolue indépendamment de la copie de
référence locale (`D:\facemonitoring_ref_perso\...`, utilisée pour le
développement hors-ligne). **Toujours relire l'état actuel des fichiers
FaceMonitoring avant de patcher** — le 27/08/2026, `App.tsx` avait déjà
divergé significativement (gestion de rôle, `messengerReply`,
`handlePublishReply`...) entre le moment où on l'a lu et celui où on a
préparé le patch, à cause d'un travail en cours côté équipe FaceMonitoring.

1. Copier `src/components/stats/`, `src/data/africa.geo.json`,
   `scripts/build-africa-geo.mjs` dans `FaceMonitoring/frontend/`.
2. `npm install recharts @tanstack/react-query d3-geo topojson-client
   world-atlas` + `npm install --save-dev @types/d3-geo`.
3. Patcher `App.tsx` — **toujours par remplacements ciblés (ancre de texte
   exacte, échec bruyant si l'ancre ne correspond pas), jamais en réécrivant
   le fichier entier.** Un script Python avec `content.count(old) == 1` avant
   chaque `replace()` a servi de garde-fou après une première tentative qui
   avait écrasé un `onPublishReply` ajouté entre-temps (restauré depuis une
   sauvegarde `.bak` faite juste avant).
   Trois changements, tous additifs :
   - import de `SentimentDashboard`
   - le rendu du `<Header>` entouré d'une condition (`activeTab === "stats"`
     affiche un en-tête simplifié à la place)
   - une branche `activeTab === "stats" ? <SentimentDashboard /> : ...` dans
     la chaîne de rendu du contenu
4. `Sidebar.tsx` n'a besoin d'aucune modification — l'entrée `"stats"`
   ("Statistiques", `BarChart3`) existe déjà. Vérifier quand même qu'aucun
   filtrage par rôle ne la masque involontairement.
5. `npm run build`.
6. Déployer : `rsync -a --delete dist/ /var/www/apps/facemonitoring/`
   (dossier possédé par `admin`, pas de `sudo` nécessaire — mais son PARENT
   `/var/www/apps/` ne l'est pas, donc pas de sauvegarde possible avec un
   simple `cp -r` à ce niveau. Nos changements sur `App.tsx` étant purement
   additifs, un `npm run build` depuis l'état courant du dépôt suffit à
   reproduire n'importe quelle version antérieure en cas de souci).

## Nginx — dernière étape, celle qui touche une ressource partagée

Config trouvée via :
```bash
sudo grep -rl "facemonitoring" /etc/nginx/
```
→ `/etc/nginx/conf.d/locations/dev/facemonitoring.location.conf`, inclus par
`/etc/nginx/conf.d/dev.digiwise.io.conf` (domaine réel : **dev.digiwise.io**).

**Toujours, dans cet ordre :**
1. Sauvegarder le fichier (`cp fichier fichier.bak_$(date +%Y%m%d_%H%M%S)`).
2. Ajouter le nouveau bloc, **sans modifier ni supprimer les blocs
   existants** (`/facemonitoring/api/`, `/socket.io/`, `/webhook/` — ceux de
   FaceMonitoring, intouchés) :
   ```nginx
   location /facemonitoring/sentiment/ {
       proxy_pass http://127.0.0.1:8200/;
       proxy_http_version 1.1;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto https;
   }
   ```
3. `sudo nginx -t` — tester AVANT de recharger. Des avertissements sur des
   noms de domaine en conflit ailleurs dans la config globale sont normaux et
   sans rapport (vus le 27/08 : `auth.digiwise.io`, etc.).
4. `sudo systemctl reload nginx` (jamais `restart` — un `reload` ne coupe
   aucune connexion active, contrairement à un `restart`, sur une machine qui
   sert des dizaines d'autres projets).

## Vérification de bout en bout

```bash
# 1. Le backend seul, en direct
curl -s http://127.0.0.1:8200/health

# 2. Le proxy nginx (nécessite le bon Host, sinon on tape un autre vhost)
curl -sk -H "Host: dev.digiwise.io" https://127.0.0.1/facemonitoring/sentiment/health

# 3. Dans un navigateur
# https://dev.digiwise.io/facemonitoring/ -> se connecter -> onglet "Statistiques"
```

## État attendu juste après déploiement

Sur un volume neuf, ne pas s'inquiéter d'un périmètre "petit" au début : la
collecte se remplit progressivement selon la cadence de chaque source (`6h`
pour la plupart, `12h` pour `gdelt`/`reddit`). L'onglet "Actualité" (qui
dépend de `gdelt`/`rss_feed`/`press_feed`) est le plus lent à se remplir.
Vérifier la progression :
```bash
docker exec sentiment-postgres psql -U telecom_user -d telecom_db -c \
  "SELECT source, count(*) FROM reviews GROUP BY source ORDER BY count(*) DESC;"
```

## Suivi

- Régénérer toute clé API collée en clair dans un chat/terminal une fois le
  déploiement validé.
- Le souci DNS/MTU de digiwise-4 reste à corriger côté admin — pas
  bloquant pour ce déploiement (contournements en place), mais peut affecter
  d'autres projets du serveur de façon moins visible.
