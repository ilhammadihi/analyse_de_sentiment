-- ===========================================================================
-- 012 — collection_jobs : la collecte devient une file de travail
-- ===========================================================================
--
-- LE PROBLÈME QU'ELLE RÉSOUT, MESURÉ
--   Google Maps était UN collecteur monolithique : 405 recherches enchaînées
--   dans un seul appel, ~10 h 30, et une insertion en base UNIQUEMENT à la fin
--   (le pipeline appelle `collector.run()` puis `batch_insert`). Toute
--   interruption — redémarrage du conteneur, expiration du budget — perdait
--   l'intégralité du travail, pas la moitié. Entre le 4 et le 9 août 2026, ce
--   collecteur a tourné des heures chaque jour sans écrire un seul avis.
--
--   Une erreur sur la 200e recherche condamnait aussi les 205 suivantes, alors
--   que les 199 premières avaient réussi et qu'aucune ne dépend des autres.
--
-- LE MODÈLE
--   Une ligne = UNE unité de collecte indépendante et reprenable. Pour Google
--   Maps : une filiale × un lieu (« Agence Orange Casablanca »). L'état vit en
--   base et survit donc au processus, ce qui est tout l'intérêt : après un
--   redémarrage, la file dit exactement ce qui reste à faire.
--
--       Casablanca -> success
--       Rabat      -> success
--       Fès        -> failed      (réessayée seule, pas tout le Maroc)
--       Marrakech  -> pending
--
-- POURQUOI UNE TABLE PLUTÔT QU'UN FICHIER D'ÉTAT
--   `data/state/` existe déjà pour les scrapers, mais un fichier ne se
--   verrouille pas entre plusieurs exécutants et ne se lit pas depuis le
--   dashboard. `FOR UPDATE SKIP LOCKED` (voir JobRepository.claim) donne une
--   file concurrente correcte sans dépendance nouvelle — Postgres est déjà là.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS collection_jobs (
    job_id          BIGSERIAL PRIMARY KEY,

    source          VARCHAR(50)  NOT NULL,
    job_type        VARCHAR(50)  NOT NULL DEFAULT 'unit',

    -- Identité STABLE de l'unité, indépendante de son rang dans une liste.
    -- C'est elle qui rend la replanification idempotente : on réécrit le
    -- catalogue à chaque cycle sans dupliquer les lignes ni perdre l'état.
    -- Un index sur le rang aurait cassé dès qu'une filiale change d'ordre
    -- dans operators.json.
    job_key         TEXT         NOT NULL,

    -- Contexte métier, dupliqué ici volontairement : il rend la file lisible
    -- telle quelle (« quelles unités du Nigeria échouent ? ») sans jointure.
    company         VARCHAR(255),
    operator        VARCHAR(255),
    country         VARCHAR(2),
    location        TEXT,
    query           TEXT,

    status          VARCHAR(20)  NOT NULL DEFAULT 'pending',
    -- pending  : à faire
    -- running  : réservée par un exécutant (voir la reprise des bails)
    -- success  : terminée
    -- failed   : épuisée après plusieurs tentatives

    -- Petit = prioritaire. Sert à faire remonter ce qui a échoué et ce qui n'a
    -- jamais réussi : sans cela, une unité en fin de catalogue qui échoue
    -- systématiquement ne serait jamais retentée avant les autres.
    priority        INT          NOT NULL DEFAULT 100,

    scheduled_at    TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,

    attempts        INT          NOT NULL DEFAULT 0,
    items_found     INT          NOT NULL DEFAULT 0,
    items_inserted  INT          NOT NULL DEFAULT 0,
    error_message   TEXT,

    -- REPRISE EN COURS D'UNITÉ. Une recherche Google Maps ouvre jusqu'à cinq
    -- fiches ; interrompue à la troisième, elle reprend à la troisième et non
    -- à la première. JSONB plutôt qu'un entier : la forme du curseur appartient
    -- au collecteur (fiches vues, jeton de page, décalage) et changera d'une
    -- source à l'autre.
    cursor          JSONB,

    -- Dernier run qui a exécuté cette unité : fait le lien avec pipeline_runs
    -- et run_metrics, qui restent la vue « passages » du dashboard.
    run_id          TEXT,

    created_at      TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT collection_jobs_unique UNIQUE (source, job_key),
    CONSTRAINT collection_jobs_status_check
        CHECK (status IN ('pending', 'running', 'success', 'failed'))
);

-- Index de la requête de réservation : on cherche les unités d'une source
-- prêtes à partir, les plus prioritaires d'abord. Sans lui, chaque appel
-- parcourt les 405 lignes — supportable ici, mais la file est faite pour
-- grandir avec le périmètre.
CREATE INDEX IF NOT EXISTS idx_collection_jobs_a_faire
    ON collection_jobs (source, status, priority, scheduled_at);

-- Reprise des bails abandonnés : retrouver les 'running' trop vieux.
CREATE INDEX IF NOT EXISTS idx_collection_jobs_running
    ON collection_jobs (status, started_at)
    WHERE status = 'running';

-- Lecture du dashboard / diagnostic par filiale.
CREATE INDEX IF NOT EXISTS idx_collection_jobs_company
    ON collection_jobs (source, company);


-- ---------------------------------------------------------------------------
-- CONTRÔLES
--
--   1. État de la file, par source :
--      SELECT source, status, count(*) FROM collection_jobs
--      GROUP BY 1, 2 ORDER BY 1, 2;
--
--   2. Unités qui n'ont JAMAIS réussi (les seules réellement inquiétantes —
--      un échec isolé sur une unité déjà collectée ne perd rien) :
--      SELECT company, location, attempts, left(error_message, 80)
--      FROM collection_jobs
--      WHERE last_success_at IS NULL AND attempts > 0
--      ORDER BY attempts DESC;
--
--   3. Bails abandonnés (doivent être repris au démarrage suivant) :
--      SELECT count(*) FROM collection_jobs
--      WHERE status = 'running'
--        AND started_at < CURRENT_TIMESTAMP - INTERVAL '2 hours';
-- ---------------------------------------------------------------------------
