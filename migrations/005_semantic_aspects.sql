-- ===========================================================================
-- Migration 005 — Analyse sémantique : aspects métier, et cache des synthèses
--
-- POURQUOI
--   L'onglet « Motifs » agrège les TERMES DU LEXIQUE qui se sont déclenchés
--   (migration 004). Mesuré sur 90 jours, il affiche en tête des motifs
--   d'insatisfaction :
--
--       can't (100) · bad (92) · useless (75) · doesn't (69) · don't (60) ·
--       better (59) · sim (58) · worst (57) · login (53) · without (52) ·
--       ever (51) · its (51) · جدا (42, « très »)
--
--   Ce sont, pour l'essentiel, des mots-outils et des négations. Ils sont
--   statistiquement corrélés au mécontentement — d'où leur présence dans le
--   lexique appris — mais ils ne nomment AUCUN motif : un décideur qui lit
--   « can't » n'apprend rien qu'il puisse corriger.
--
--   Le défaut n'est pas un réglage à affiner, c'est un défaut de nature. Un sac
--   de mots ne peut extraire QUE des mots ; « le réseau tombe tous les soirs
--   depuis la mise à jour » ne contient aucun terme de lexique et ne produira
--   jamais le motif « coupures réseau ». Il faut une couche qui lise la PHRASE
--   et la rattache à un aspect métier. C'est l'objet de cette migration.
--
-- CE QUI EST STOCKÉ, ET CE QUI NE L'EST PAS
--   On stocke le RÉSULTAT de l'analyse sémantique, jamais l'appel : les aspects
--   détectés, le sentiment jugé par le modèle, et la version de la taxonomie
--   qui les a produits. Le texte de l'avis reste la seule source de vérité ;
--   tout peut être recalculé en repassant les lignes.
--
-- LE LEXIQUE N'EST PAS SUPPRIMÉ, ET C'EST DÉLIBÉRÉ
--   Il tourne à l'insertion, sans réseau ni clé d'API, et couvre donc 100 % des
--   avis immédiatement. La couche sémantique passe APRÈS, par lots, et peut
--   échouer (quota gratuit épuisé, serveur de test sans accès sortant). La vue
--   applique donc COALESCE(llm, lexique) : le meilleur jugement disponible pour
--   chaque ligne, et jamais d'écran vide parce qu'un fournisseur est indisponible.
--
--   CONTREPARTIE ASSUMÉE, à connaître avant de lire un taux : tant que la
--   couverture n'est pas complète, un même indicateur agrège deux classifieurs.
--   La vue expose `sentiment_source` et `lexicon_sentiment` pour que ce mélange
--   soit mesurable au lieu d'être subi — voir les contrôles en fin de fichier.
--
-- NON DESTRUCTIF — colonnes ajoutées, nullables ou avec défaut vide.
-- Idempotent : IF NOT EXISTS partout.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- Colonnes de la table de faits
-- ---------------------------------------------------------------------------

-- Aspects MÉTIER détectés, séparés par la polarité qu'ils ont DANS L'AVIS.
--
-- Un même avis peut louer la couverture réseau et dénoncer la facturation :
-- deux aspects, deux polarités, une seule ligne. Les rassembler dans une seule
-- colonne obligerait à redemander au texte de quel côté chacun penche.
--
-- Les valeurs appartiennent à la taxonomie fermée de reviews/domain/aspects.py.
-- Fermée, et non ouverte : une taxonomie libre redonnerait un nuage de termes,
-- c'est-à-dire le défaut qu'on corrige ici.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS neg_aspects TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS pos_aspects TEXT[] NOT NULL DEFAULT '{}';

-- Sentiment jugé par le modèle sémantique. Distinct de `reviews.sentiment`,
-- qui reste la sortie du lexique : les écraser interdirait de mesurer l'apport
-- de la couche sémantique, et rendrait tout retour en arrière impossible.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS llm_sentiment VARCHAR(20);

-- Confiance déclarée par le modèle, sur [0, 1]. Sert à écarter les jugements
-- incertains d'un agrégat, et à repérer les avis qu'il faudra relire.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS llm_confidence REAL;

-- Version de la TAXONOMIE + du prompt ayant produit les colonnes ci-dessus.
-- Même rôle que lexicon_version : sans ce numéro, on ne saurait pas quelles
-- lignes rejouer après une évolution de la taxonomie, et un graphique
-- mélangerait des aspects issus de deux nomenclatures différentes.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS aspect_version SMALLINT;

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS llm_analyzed_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_llm_confidence_range'
    ) THEN
        ALTER TABLE reviews ADD CONSTRAINT chk_llm_confidence_range
            CHECK (llm_confidence IS NULL
                   OR (llm_confidence >= 0 AND llm_confidence <= 1))
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_llm_sentiment_values'
    ) THEN
        ALTER TABLE reviews ADD CONSTRAINT chk_llm_sentiment_values
            CHECK (llm_sentiment IS NULL
                   OR llm_sentiment IN ('positive', 'neutral', 'negative'))
            NOT VALID;
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- INDEX
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_reviews_neg_aspects ON reviews USING GIN (neg_aspects);
CREATE INDEX IF NOT EXISTS idx_reviews_pos_aspects ON reviews USING GIN (pos_aspects);

-- File d'attente de l'analyse sémantique : les lignes pas encore traitées.
-- Index PARTIEL — il ne contient que le reste à faire, donc il rétrécit à
-- mesure que le backfill avance, jusqu'à ne plus rien coûter.
CREATE INDEX IF NOT EXISTS idx_reviews_aspect_pending
    ON reviews (COALESCE(created_at, collected_at) DESC)
    WHERE aspect_version IS NULL;


-- ===========================================================================
-- TABLE — synthèses en langage naturel, mises en cache
--
--   Une synthèse coûte un appel à un fournisseur dont le quota gratuit est
--   limité et peut être réduit sans préavis. Elle est donc calculée UNE FOIS
--   par question posée, puis relue.
--
--   La clé est l'empreinte du périmètre + de la question (`scope_hash`), pas un
--   identifiant d'entité : deux lecteurs qui ouvrent le même écart entre deux
--   filiales sur la même période doivent recevoir la même phrase, sans second
--   appel.
--
--   `payload` conserve les CHIFFRES envoyés au modèle. C'est ce qui rend la
--   synthèse vérifiable : sans eux, une phrase affirmant « +12 points » ne
--   pourrait plus être rapprochée de rien six semaines plus tard, et une
--   soutenance ne peut pas défendre un texte dont l'entrée a disparu.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS llm_insights (
    insight_id   BIGSERIAL PRIMARY KEY,

    -- Nature de la question : 'spike' (pic de négatifs), 'comparison' (écart
    -- entre entités), 'scope' (synthèse d'un périmètre).
    kind         VARCHAR(32)  NOT NULL,

    -- Empreinte stable du périmètre + de la question. Calculée côté Python
    -- (reviews/llm/insights.py) pour que la même question produise toujours la
    -- même clé, quel que soit l'ordre des filtres dans l'URL.
    scope_hash   CHAR(64)     NOT NULL,

    -- Périmètre lisible (ce que le filtre valait), pour l'audit.
    scope        JSONB        NOT NULL DEFAULT '{}'::jsonb,

    -- Chiffres et extraits transmis au modèle.
    payload      JSONB        NOT NULL DEFAULT '{}'::jsonb,

    -- La synthèse elle-même, deux à trois phrases.
    text         TEXT         NOT NULL,

    model        VARCHAR(120),
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_llm_insights_scope UNIQUE (kind, scope_hash)
);

CREATE INDEX IF NOT EXISTS idx_llm_insights_created
    ON llm_insights (created_at DESC);


-- ===========================================================================
-- TABLE — consommation quotidienne du fournisseur
--
--   Le service tourne sans surveillance sur un environnement de test, avec un
--   quota gratuit dont Google ne publie plus la valeur et qu'il a déjà réduit
--   sans préavis (décembre 2025). Un compteur PERSISTANT est donc nécessaire :
--   un compteur en mémoire repartirait de zéro à chaque redémarrage du worker,
--   c'est-à-dire précisément quand le quota vient d'être épuisé.
--
--   Il sert aussi de journal : « combien d'appels ce projet a-t-il coûté ? »
--   est une question à laquelle il faut pouvoir répondre.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS llm_usage (
    day          DATE     PRIMARY KEY,
    calls        INTEGER  NOT NULL DEFAULT 0,
    tokens_in    BIGINT   NOT NULL DEFAULT 0,
    tokens_out   BIGINT   NOT NULL DEFAULT 0,
    -- Échecs (quota, réseau, réponse illisible). Un chiffre qui monte alors que
    -- `calls` stagne est la signature d'un quota épuisé.
    errors       INTEGER  NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ===========================================================================
-- VUE — v_reviews_enriched, étendue aux aspects et au sentiment sémantique
--
--   PROPRIÉTÉ DE CETTE MIGRATION à partir d'ici (la 004 l'était avant elle).
--   Database.apply_schema() rejoue les migrations dans l'ordre à chaque
--   démarrage : c'est donc cette définition qui subsiste. Toute colonne à
--   ajouter à la vue doit l'être ICI.
--
--   LE POINT IMPORTANT : `sentiment` devient COALESCE(llm_sentiment, sentiment).
--   Aucun agrégat n'a besoin d'être réécrit — _MEASURES, les classements, la
--   matrice, les alertes lisent tous `v.sentiment` et bénéficient donc de la
--   couche sémantique dès qu'elle a traité la ligne, sans une seule requête
--   modifiée. Le jugement du lexique reste lisible sous `lexicon_sentiment`.
-- ===========================================================================
DROP VIEW IF EXISTS v_reviews_enriched CASCADE;
CREATE VIEW v_reviews_enriched AS
SELECT
    r.review_id, r.title, r.text, r.rating, r.verified,
    r.created_at, r.collected_at, r.run_id,

    -- Meilleur jugement disponible pour cette ligne.
    COALESCE(r.llm_sentiment, r.sentiment)      AS sentiment,
    -- D'où il vient. Sans cette colonne, la part de lignes encore classées par
    -- le lexique serait invisible, donc impossible à annoncer au lecteur.
    CASE WHEN r.llm_sentiment IS NOT NULL THEN 'llm' ELSE 'lexicon' END
                                                AS sentiment_source,
    r.sentiment                                 AS lexicon_sentiment,
    r.llm_sentiment, r.llm_confidence, r.aspect_version, r.llm_analyzed_at,

    r.sentiment_score, r.neg_terms, r.pos_terms, r.lexicon_version,
    r.neg_aspects, r.pos_aspects,

    sub.subsidiary_id, sub.name AS subsidiary,
    op.operator_id,    op.name  AS operator,   op.parent_group,
    co.country_id,     co.iso2, co.name AS country, co.region,
    src.source_id,     src.code AS source_code, src.name AS source,
    src.kind AS source_kind
FROM reviews r
LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
LEFT JOIN dim_source     src ON src.source_id     = r.source_id;


-- ===========================================================================
-- VUE — v_review_terms, reconstruite (CASCADE l'a supprimée avec la précédente)
--
--   Reprise à l'identique de la migration 004. Elle est redéclarée ici parce
--   que le DROP ... CASCADE ci-dessus l'emporte : sans cette reconstruction,
--   l'onglet Motifs tomberait au premier redémarrage.
-- ===========================================================================
DROP VIEW IF EXISTS v_review_terms CASCADE;
CREATE VIEW v_review_terms AS
SELECT
    r.review_id,
    t.term,
    'negative'::text                            AS polarity,
    r.sentiment_score,
    COALESCE(r.created_at, r.collected_at)      AS occurred_at,
    sub.subsidiary_id, sub.name AS subsidiary,
    op.operator_id,    op.name  AS operator,
    co.country_id,     co.iso2, co.name AS country, co.region,
    src.kind AS source_kind, src.code AS source_code
FROM reviews r
CROSS JOIN LATERAL unnest(r.neg_terms) AS t(term)
LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
LEFT JOIN dim_source     src ON src.source_id     = r.source_id

UNION ALL

SELECT
    r.review_id,
    t.term,
    'positive'::text                            AS polarity,
    r.sentiment_score,
    COALESCE(r.created_at, r.collected_at)      AS occurred_at,
    sub.subsidiary_id, sub.name AS subsidiary,
    op.operator_id,    op.name  AS operator,
    co.country_id,     co.iso2, co.name AS country, co.region,
    src.kind AS source_kind, src.code AS source_code
FROM reviews r
CROSS JOIN LATERAL unnest(r.pos_terms) AS t(term)
LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
LEFT JOIN dim_source     src ON src.source_id     = r.source_id;


-- ===========================================================================
-- VUE — un ASPECT par ligne, porteur de tous les axes de filtrage
--
--   Jumelle exacte de v_review_terms, colonne pour colonne : mêmes noms, même
--   ordre, `aspect` là où l'autre a `term`. Ce n'est pas de la symétrie
--   décorative — c'est ce qui permet au constructeur de filtre Python
--   (reviews/storage/filters.py, correspondance ASPECTS) de s'appliquer ici
--   SANS aucun cas particulier, et à la même méthode de repository de servir
--   les deux dimensions.
--
--   L'INVARIANT DU MODULE DE FILTRES S'APPLIQUE : tout axe ajouté à
--   v_reviews_enriched doit l'être ici aussi. Un axe manquant ne provoque pas
--   d'erreur — il produit un filtre SILENCIEUSEMENT IGNORÉ sur cet écran, donc
--   deux écrans qui affichent des chiffres contradictoires sans rien signaler.
-- ===========================================================================
DROP VIEW IF EXISTS v_review_aspects CASCADE;
CREATE VIEW v_review_aspects AS
SELECT
    r.review_id,
    a.aspect,
    'negative'::text                            AS polarity,
    r.sentiment_score,
    r.llm_confidence,
    COALESCE(r.created_at, r.collected_at)      AS occurred_at,
    sub.subsidiary_id, sub.name AS subsidiary,
    op.operator_id,    op.name  AS operator,
    co.country_id,     co.iso2, co.name AS country, co.region,
    src.kind AS source_kind, src.code AS source_code
FROM reviews r
CROSS JOIN LATERAL unnest(r.neg_aspects) AS a(aspect)
LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
LEFT JOIN dim_source     src ON src.source_id     = r.source_id

UNION ALL

SELECT
    r.review_id,
    a.aspect,
    'positive'::text                            AS polarity,
    r.sentiment_score,
    r.llm_confidence,
    COALESCE(r.created_at, r.collected_at)      AS occurred_at,
    sub.subsidiary_id, sub.name AS subsidiary,
    op.operator_id,    op.name  AS operator,
    co.country_id,     co.iso2, co.name AS country, co.region,
    src.kind AS source_kind, src.code AS source_code
FROM reviews r
CROSS JOIN LATERAL unnest(r.pos_aspects) AS a(aspect)
LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
LEFT JOIN dim_source     src ON src.source_id     = r.source_id;


-- ===========================================================================
-- VUE — volume par filiale (reconstruite : le CASCADE ci-dessus l'a emportée)
-- ===========================================================================
DROP VIEW IF EXISTS v_subsidiary_volume CASCADE;
CREATE VIEW v_subsidiary_volume AS
SELECT
    sub.subsidiary_id,
    COUNT(r.review_id) FILTER (WHERE src.kind = 'customer_review') AS avis_clients,
    COUNT(r.review_id) FILTER (WHERE src.kind = 'press')           AS articles_presse,
    COUNT(r.review_id)                                             AS total
FROM dim_subsidiary sub
LEFT JOIN reviews    r   ON r.subsidiary_id = sub.subsidiary_id
LEFT JOIN dim_source src ON src.source_id   = r.source_id
GROUP BY sub.subsidiary_id;


-- ===========================================================================
-- CONTRÔLES POST-MIGRATION (à exécuter à la main, ne modifient rien)
--
--   -- Couverture de l'analyse sémantique (doit monter vers 100 %) :
--   SELECT COUNT(*) FILTER (WHERE aspect_version IS NOT NULL) AS analyses,
--          COUNT(*) AS total
--     FROM reviews r JOIN dim_source s ON s.source_id = r.source_id
--    WHERE s.kind = 'customer_review';
--
--   -- Accord entre le lexique et la couche sémantique. Un accord très élevé
--   -- signifierait que la couche n'apporte rien ; très bas, qu'il faut relire
--   -- des exemples avant de lui faire confiance.
--   SELECT lexicon_sentiment, llm_sentiment, COUNT(*)
--     FROM v_reviews_enriched
--    WHERE llm_sentiment IS NOT NULL
--    GROUP BY 1, 2 ORDER BY 3 DESC;
--
--   -- Principaux motifs d'insatisfaction, enfin nommés :
--   SELECT aspect, COUNT(*) FROM v_review_aspects
--    WHERE polarity = 'negative' AND source_kind = 'customer_review'
--    GROUP BY aspect ORDER BY 2 DESC;
--
--   -- Consommation du fournisseur :
--   SELECT * FROM llm_usage ORDER BY day DESC LIMIT 14;
--
--   -- Une fois la couverture complète, verrouiller les contraintes :
--   ALTER TABLE reviews VALIDATE CONSTRAINT chk_llm_confidence_range;
--   ALTER TABLE reviews VALIDATE CONSTRAINT chk_llm_sentiment_values;
-- ===========================================================================
