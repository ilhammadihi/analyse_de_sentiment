-- ===========================================================================
-- Migration 002 — Modèle dimensionnel (pays / opérateur / filiale)
--
-- POURQUOI
--   La colonne reviews.company est une chaîne libre ("Moov Africa Benin") qui
--   mélange trois notions : l'opérateur, le pays et la filiale. Impossible donc
--   d'agréger « par pays » ou « par opérateur » sans découper du texte, et
--   ingérable dès qu'on ajoutera Orange / MTN / Airtel sur d'autres pays
--   (variantes d'écriture, fautes de frappe, aucun endroit pour les attributs).
--
--   On passe à un modèle en étoile : chaque avis pointe vers une FILIALE, qui
--   porte elle-même l'opérateur ET le pays. Les trois axes de dashboard
--   deviennent alors de simples jointures, et ajouter un opérateur dans un
--   nouveau pays ne coûte que 3 INSERT — aucune modification de code ni de
--   requête.
--
-- NON DESTRUCTIF
--   Les colonnes reviews.company et reviews.source sont CONSERVÉES : l'API
--   actuelle continue de fonctionner pendant la transition. Les nouvelles
--   colonnes sont nullables et remplies par report (backfill) en fin de script.
--
-- Idempotent : IF NOT EXISTS / ON CONFLICT DO NOTHING partout.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- DIMENSION : Pays
--   iso2 est la clé métier : elle garantit qu'on ne confondra jamais
--   "Côte d'Ivoire", "Cote d'Ivoire" et "CI".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_country (
    country_id  SERIAL PRIMARY KEY,
    iso2        CHAR(2)      NOT NULL UNIQUE,     -- 'BJ'
    iso3        CHAR(3)      UNIQUE,              -- 'BEN'
    name        VARCHAR(100) NOT NULL,            -- 'Bénin'
    region      VARCHAR(50),                      -- 'Afrique de l'Ouest'
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- DIMENSION : Opérateur
--   former_names sert à rattacher l'historique : Moov Africa s'appelait
--   Etisalat/Telecel, et la collecte Google Maps remonte encore des agences
--   nommées "ETISALAT BENIN".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_operator (
    operator_id   SERIAL PRIMARY KEY,
    code          VARCHAR(30)  NOT NULL UNIQUE,   -- 'moov_africa'
    name          VARCHAR(100) NOT NULL,          -- 'Moov Africa'
    parent_group  VARCHAR(100),                   -- 'Maroc Telecom'
    former_names  TEXT[]       NOT NULL DEFAULT '{}',
    created_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- DIMENSION : Filiale  (le croisement opérateur × pays)
--   C'est LA table à laquelle chaque avis se rattache. Un couple
--   (opérateur, pays) est unique : "Moov Africa Bénin" ne peut exister 2 fois.
--
--   aliases contient toutes les façons dont la filiale est nommée par les
--   sources (et les anciennes valeurs de reviews.company). C'est ce qui permet
--   le rattachement automatique sans jamais parser de texte à la volée.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_subsidiary (
    subsidiary_id SERIAL PRIMARY KEY,
    operator_id   INTEGER      NOT NULL REFERENCES dim_operator(operator_id),
    country_id    INTEGER      NOT NULL REFERENCES dim_country(country_id),
    name          VARCHAR(150) NOT NULL,          -- 'Moov Africa Bénin'
    aliases       TEXT[]       NOT NULL DEFAULT '{}',
    active        BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_subsidiary_operator_country UNIQUE (operator_id, country_id)
);

-- ---------------------------------------------------------------------------
-- DIMENSION : Source
--   `kind` sépare les AVIS CLIENTS de la PRESSE. Indispensable : les ~629
--   articles RSS écrasent numériquement les 156 avis clients notés et
--   fausseraient toute moyenne de satisfaction. Les vues plus bas s'appuient
--   dessus pour ne calculer la satisfaction que sur les avis clients.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_source (
    source_id   SERIAL PRIMARY KEY,
    code        VARCHAR(50)  NOT NULL UNIQUE,     -- = valeurs de SourceEnum
    name        VARCHAR(100) NOT NULL,
    kind        VARCHAR(20)  NOT NULL,            -- 'customer_review' | 'press'
    has_rating  BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_source_kind CHECK (kind IN ('customer_review', 'press'))
);


-- ---------------------------------------------------------------------------
-- TABLE DE FAITS : on greffe les clés dimensionnelles sur `reviews`
--   Nullables volontairement : on ne casse aucune ligne existante. Le passage
--   en NOT NULL se fera dans une migration ultérieure, une fois le backfill
--   vérifié et les collecteurs adaptés.
-- ---------------------------------------------------------------------------
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS subsidiary_id INTEGER
    REFERENCES dim_subsidiary(subsidiary_id);
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS source_id INTEGER
    REFERENCES dim_source(source_id);


-- ===========================================================================
-- DONNÉES DE RÉFÉRENCE (seed)
-- ===========================================================================

-- Pays — périmètre actuel. Ajouter un pays = 1 ligne ici, rien d'autre.
INSERT INTO dim_country (iso2, iso3, name, region) VALUES
    ('BJ', 'BEN', 'Bénin',                     'Afrique de l''Ouest'),
    ('BF', 'BFA', 'Burkina Faso',              'Afrique de l''Ouest'),
    ('ML', 'MLI', 'Mali',                      'Afrique de l''Ouest'),
    ('CF', 'CAF', 'République centrafricaine', 'Afrique centrale')
ON CONFLICT (iso2) DO NOTHING;

-- Opérateurs — périmètre actuel. Les suivants (Orange, MTN, Airtel, Vodacom…)
-- s'ajoutent ici sans toucher au reste.
INSERT INTO dim_operator (code, name, parent_group, former_names) VALUES
    ('moov_africa', 'Moov Africa', 'Maroc Telecom',
     ARRAY['Etisalat', 'Telecel']::text[])
ON CONFLICT (code) DO NOTHING;

-- Filiales — le croisement. `aliases` reprend les valeurs actuelles de
-- reviews.company pour permettre le rattachement automatique plus bas.
INSERT INTO dim_subsidiary (operator_id, country_id, name, aliases)
SELECT o.operator_id, c.country_id, v.name, v.aliases
FROM (VALUES
        ('BJ', 'Moov Africa Bénin',
            ARRAY['Moov Africa Benin', 'Agence Moov Bénin', 'Etisalat Bénin']::text[]),
        ('BF', 'Moov Africa Burkina',
            ARRAY['Moov Africa Burkina']::text[]),
        ('ML', 'Moov Africa Mali',
            ARRAY['Moov Africa Mali']::text[]),
        ('CF', 'Moov Africa Centrafrique',
            ARRAY['Moov Africa Centrafrique']::text[])
     ) AS v(iso2, name, aliases)
JOIN dim_country  c ON c.iso2 = v.iso2
JOIN dim_operator o ON o.code = 'moov_africa'
ON CONFLICT (operator_id, country_id) DO NOTHING;

-- Sources — `code` doit correspondre EXACTEMENT à SourceEnum côté Python.
INSERT INTO dim_source (code, name, kind, has_rating) VALUES
    ('google_play', 'Google Play Store', 'customer_review', TRUE),
    ('app_store',   'Apple App Store',   'customer_review', TRUE),
    ('google_maps', 'Google Maps',       'customer_review', TRUE),
    ('trustpilot',  'Trustpilot',        'customer_review', TRUE),
    ('rss_feed',    'Google News (RSS)', 'press',           FALSE)
ON CONFLICT (code) DO NOTHING;


-- ===========================================================================
-- BACKFILL — rattacher les lignes existantes aux dimensions
-- ===========================================================================

-- Chaque avis est rattaché à sa filiale via les alias déclarés plus haut.
UPDATE reviews r
SET    subsidiary_id = s.subsidiary_id
FROM   dim_subsidiary s
WHERE  r.subsidiary_id IS NULL
  AND  r.company = ANY (s.aliases);

UPDATE reviews r
SET    source_id = s.source_id
FROM   dim_source s
WHERE  r.source_id IS NULL
  AND  r.source = s.code;


-- ===========================================================================
-- INDEX
-- ===========================================================================
CREATE INDEX IF NOT EXISTS idx_reviews_subsidiary  ON reviews(subsidiary_id);
CREATE INDEX IF NOT EXISTS idx_reviews_source_id   ON reviews(source_id);
CREATE INDEX IF NOT EXISTS idx_subsidiary_operator ON dim_subsidiary(operator_id);
CREATE INDEX IF NOT EXISTS idx_subsidiary_country  ON dim_subsidiary(country_id);
-- Recherche d'une filiale par alias (rattachement à l'insertion)
CREATE INDEX IF NOT EXISTS idx_subsidiary_aliases  ON dim_subsidiary USING GIN (aliases);


-- ===========================================================================
-- VUES D'AGRÉGATS
--   Règle appliquée partout : la SATISFACTION (sentiment, note) ne se calcule
--   que sur kind='customer_review'. La presse est comptée à part, jamais
--   mélangée aux moyennes.
-- ===========================================================================

-- Vue de base : un avis "enrichi" de tous ses axes d'analyse.
DROP VIEW IF EXISTS v_reviews_enriched CASCADE;
CREATE VIEW v_reviews_enriched AS
SELECT
    r.review_id, r.title, r.text, r.rating, r.sentiment, r.verified,
    r.created_at, r.collected_at, r.run_id,
    sub.subsidiary_id, sub.name AS subsidiary,
    op.operator_id,    op.name  AS operator,   op.parent_group,
    co.country_id,     co.iso2, co.name AS country, co.region,
    src.source_id,     src.code AS source_code, src.name AS source, src.kind AS source_kind
FROM reviews r
LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
LEFT JOIN dim_source     src ON src.source_id     = r.source_id;


-- Tendance quotidienne enrichie des axes dimensionnels.
--
-- IMPORTANT : on NE redéfinit PAS la vue `sentiment_daily` (propriété de la
-- migration 001). db.apply_schema() ré-exécute 001 à chaque démarrage via
-- `CREATE OR REPLACE VIEW sentiment_daily`, et PostgreSQL interdit à REPLACE de
-- retirer des colonnes d'une vue existante : redéfinir sentiment_daily ici avec
-- des colonnes différentes faisait planter le worker en boucle
-- (« cannot drop columns from view »). On crée donc une vue AU NOM DISTINCT ;
-- sentiment_daily reste tel que 001 le définit, et l'API continue de l'utiliser.
DROP VIEW IF EXISTS sentiment_daily_dim CASCADE;
CREATE VIEW sentiment_daily_dim AS
SELECT
    date_trunc('day', COALESCE(r.created_at, r.collected_at))::date AS day,
    COUNT(*)                                         AS total,
    COUNT(*) FILTER (WHERE r.sentiment = 'positive') AS positive,
    COUNT(*) FILTER (WHERE r.sentiment = 'neutral')  AS neutral,
    COUNT(*) FILTER (WHERE r.sentiment = 'negative') AS negative,
    AVG(r.rating)                                    AS avg_rating,
    sub.subsidiary_id, sub.name AS subsidiary,
    op.operator_id,    op.name  AS operator,
    co.country_id,     co.iso2, co.name AS country,
    src.kind AS source_kind
FROM reviews r
LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
LEFT JOIN dim_source     src ON src.source_id     = r.source_id
GROUP BY 1, sub.subsidiary_id, sub.name, op.operator_id, op.name,
         co.country_id, co.iso2, co.name, src.kind;


-- Axe 1 — PAR PAYS (tous opérateurs confondus)
DROP VIEW IF EXISTS v_stats_by_country CASCADE;
CREATE VIEW v_stats_by_country AS
SELECT
    co.country_id, co.iso2, co.name AS country, co.region,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review') AS avis_clients,
    COUNT(*) FILTER (WHERE src.kind = 'press')           AS articles_presse,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review'
                       AND r.sentiment = 'positive')     AS positifs,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review'
                       AND r.sentiment = 'neutral')      AS neutres,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review'
                       AND r.sentiment = 'negative')     AS negatifs,
    ROUND((AVG(r.rating) FILTER (WHERE src.kind = 'customer_review'))::numeric, 2)
                                                         AS note_moyenne
FROM reviews r
JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
JOIN dim_country    co  ON co.country_id     = sub.country_id
JOIN dim_source     src ON src.source_id     = r.source_id
GROUP BY co.country_id, co.iso2, co.name, co.region;


-- Axe 2 — PAR OPÉRATEUR (toutes filiales confondues)
DROP VIEW IF EXISTS v_stats_by_operator CASCADE;
CREATE VIEW v_stats_by_operator AS
SELECT
    op.operator_id, op.name AS operator, op.parent_group,
    COUNT(DISTINCT sub.country_id)                       AS nb_pays,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review') AS avis_clients,
    COUNT(*) FILTER (WHERE src.kind = 'press')           AS articles_presse,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review'
                       AND r.sentiment = 'positive')     AS positifs,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review'
                       AND r.sentiment = 'neutral')      AS neutres,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review'
                       AND r.sentiment = 'negative')     AS negatifs,
    ROUND((AVG(r.rating) FILTER (WHERE src.kind = 'customer_review'))::numeric, 2)
                                                         AS note_moyenne
FROM reviews r
JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
JOIN dim_source     src ON src.source_id     = r.source_id
GROUP BY op.operator_id, op.name, op.parent_group;


-- Axe 3 — PAR FILIALE (le grain le plus fin : opérateur × pays)
DROP VIEW IF EXISTS v_stats_by_subsidiary CASCADE;
CREATE VIEW v_stats_by_subsidiary AS
SELECT
    sub.subsidiary_id, sub.name AS subsidiary,
    op.name AS operator, co.name AS country, co.iso2,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review') AS avis_clients,
    COUNT(*) FILTER (WHERE src.kind = 'press')           AS articles_presse,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review'
                       AND r.sentiment = 'positive')     AS positifs,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review'
                       AND r.sentiment = 'neutral')      AS neutres,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review'
                       AND r.sentiment = 'negative')     AS negatifs,
    ROUND((AVG(r.rating) FILTER (WHERE src.kind = 'customer_review'))::numeric, 2)
                                                         AS note_moyenne
FROM reviews r
JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
JOIN dim_country    co  ON co.country_id     = sub.country_id
JOIN dim_source     src ON src.source_id     = r.source_id
GROUP BY sub.subsidiary_id, sub.name, op.name, co.name, co.iso2;


-- ===========================================================================
-- CONTRÔLE POST-MIGRATION (à exécuter à la main, ne modifie rien)
--
--   -- Reste-t-il des avis non rattachés à une filiale ?
--   SELECT company, COUNT(*) FROM reviews
--    WHERE subsidiary_id IS NULL GROUP BY company;
--   -- Toute ligne qui remonte ici = un alias manquant dans dim_subsidiary.
--
--   SELECT * FROM v_stats_by_country;
--   SELECT * FROM v_stats_by_operator;
-- ===========================================================================
