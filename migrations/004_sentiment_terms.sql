-- ===========================================================================
-- Migration 004 — Score de sentiment et termes déclencheurs
--
-- POURQUOI
--   Le dashboard sait aujourd'hui QUE la satisfaction baisse, jamais POURQUOI.
--   Or le moteur lexical (reviews/domain/sentiment.py) identifie déjà, pour
--   chaque avis, les mots exacts qui ont fait pencher le score — puis les jette :
--   seul le label 'positive'/'neutral'/'negative' est persisté.
--
--   On conserve donc le score continu et les termes déclenchés. Deux usages :
--     1. l'onglet « Motifs d'insatisfaction » : agréger les termes négatifs sur
--        un périmètre (pays, opérateur, filiale, période) répond à « de quoi se
--        plaignent les clients ici ? » sans aucun modèle supplémentaire ;
--     2. les agents IA de la phase 2 : un agent qui doit proposer une campagne a
--        besoin du motif, pas d'un score. C'est l'entrée de son prompt.
--
--   Le score continu, lui, rend les comparaisons plus fines que le label : la
--   moyenne d'un score sur [-1, 1] distingue deux filiales que le simple
--   « % de négatifs » écrase (un avis à -0,18 et un à -0,95 comptent pareil).
--
-- CHOIX DU STOCKAGE
--   Colonnes TEXT[] sur `reviews` plutôt qu'une table de liaison : le nombre de
--   termes par avis est petit (0 à ~10), la volumétrie totale est modeste, et
--   surtout on évite une clé étrangère de plus à maintenir en cohérence avec la
--   déduplication (ON CONFLICT DO NOTHING sur reviews). Un index GIN rend la
--   recherche « quels avis contiennent ce terme » aussi rapide qu'une jointure.
--
-- NON DESTRUCTIF — colonnes ajoutées, nullables ou avec défaut vide. Le
-- backfill est fait par tools/backfill_sentiment_terms.py (le lexique vit en
-- Python, il ne peut pas être rejoué en SQL).
--
-- Idempotent : IF NOT EXISTS partout.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- Colonnes de la table de faits
-- ---------------------------------------------------------------------------

-- Score « compound » normalisé sur [-1, 1], au sens VADER. NULL = avis pas
-- encore repassé par le moteur (le backfill le remplit).
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS sentiment_score REAL;

-- Termes du lexique effectivement déclenchés, après négation et
-- intensification. Un terme nié apparaît dans la polarité qu'il a PRISE, pas
-- celle qu'il porte au lexique : « pas rapide » alimente neg_terms.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS neg_terms TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS pos_terms TEXT[] NOT NULL DEFAULT '{}';

-- Marqueur de version du lexique ayant produit les colonnes ci-dessus.
-- Sans lui, impossible de savoir quelles lignes rejouer quand le lexique
-- s'enrichit : on ré-analyserait tout à chaque fois, ou pire on comparerait des
-- termes produits par deux versions différentes.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS lexicon_version SMALLINT;

-- PostgreSQL n'a pas de ADD CONSTRAINT IF NOT EXISTS : sans ce bloc, une
-- seconde exécution de la migration échouerait sur « constraint already
-- exists », et le worker qui rejoue les migrations au démarrage bouclerait.
--
-- NOT VALID : la contrainte ne bloque pas la migration sur d'éventuelles lignes
-- héritées hors bornes ; les insertions futures sont contrôlées. À verrouiller
-- avec VALIDATE CONSTRAINT après le backfill (voir la fin du fichier).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_sentiment_score_range'
    ) THEN
        ALTER TABLE reviews ADD CONSTRAINT chk_sentiment_score_range
            CHECK (sentiment_score IS NULL
                   OR (sentiment_score >= -1 AND sentiment_score <= 1))
            NOT VALID;
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- INDEX
-- ---------------------------------------------------------------------------

-- « Quels avis mentionnent ce terme ? » — utilisé par les verbatims d'exemple
-- de l'onglet Motifs. Sans GIN, chaque clic déclenche un scan des 20 k lignes.
CREATE INDEX IF NOT EXISTS idx_reviews_neg_terms ON reviews USING GIN (neg_terms);
CREATE INDEX IF NOT EXISTS idx_reviews_pos_terms ON reviews USING GIN (pos_terms);

-- Index de couverture des filtres du dashboard : toutes les requêtes de stats
-- sont bornées dans le temps PUIS découpées par filiale.
CREATE INDEX IF NOT EXISTS idx_reviews_occurred_subsidiary
    ON reviews (COALESCE(created_at, collected_at) DESC, subsidiary_id);

-- Lignes restant à ré-analyser après une évolution du lexique.
CREATE INDEX IF NOT EXISTS idx_reviews_lexicon_version
    ON reviews (lexicon_version)
    WHERE lexicon_version IS NULL;


-- ===========================================================================
-- VUE — v_reviews_enriched, étendue au score et aux termes
--
--   PROPRIÉTÉ DE CETTE MIGRATION à partir d'ici. La 002 la crée ; la 004 la
--   remplace pour y ajouter sentiment_score / neg_terms / pos_terms, dont les
--   agrégats du dashboard ont besoin (score moyen, motifs). Comme
--   Database.apply_schema() rejoue les migrations dans l'ordre à chaque
--   démarrage, c'est TOUJOURS cette définition-ci qui subsiste.
--
--   Conséquence à respecter : toute colonne ajoutée à la vue doit l'être ICI,
--   pas dans la 002 — sinon elle serait écrasée au démarrage suivant.
--
--   CASCADE est sans danger : les vues v_stats_by_* et sentiment_daily_dim de
--   la 002 lisent la table `reviews` directement, aucune ne dépend de celle-ci.
-- ===========================================================================
DROP VIEW IF EXISTS v_reviews_enriched CASCADE;
CREATE VIEW v_reviews_enriched AS
SELECT
    r.review_id, r.title, r.text, r.rating, r.sentiment, r.verified,
    r.created_at, r.collected_at, r.run_id,
    r.sentiment_score, r.neg_terms, r.pos_terms, r.lexicon_version,
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
-- VUE — un terme par ligne, porteur de tous les axes de filtrage
--
--   C'est la vue qu'interroge l'onglet « Motifs ». Elle expose exactement les
--   mêmes colonnes de filtrage que v_reviews_enriched (iso2, operator_id,
--   subsidiary_id, region, source_kind), afin que le constructeur de filtre
--   Python (reviews/storage/filters.py) s'applique ici SANS aucun cas
--   particulier. Toute colonne ajoutée d'un côté doit l'être de l'autre.
--
--   CROSS JOIN LATERAL unnest : un avis sans terme déclenché ne produit aucune
--   ligne, ce qui est voulu — un avis sans mot du lexique n'a rien à dire sur
--   les motifs.
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
-- CONTRÔLE POST-MIGRATION (à exécuter à la main, ne modifie rien)
--
--   -- Combien de lignes restent à analyser ? (doit tomber à 0 après backfill)
--   SELECT COUNT(*) FROM reviews WHERE lexicon_version IS NULL;
--
--   -- Une fois à 0, verrouiller la contrainte de bornes :
--   ALTER TABLE reviews VALIDATE CONSTRAINT chk_sentiment_score_range;
--
--   -- Top motifs de plainte, tous périmètres confondus :
--   SELECT term, COUNT(*) FROM v_review_terms
--    WHERE polarity = 'negative' AND source_kind = 'customer_review'
--    GROUP BY term ORDER BY 2 DESC LIMIT 20;
-- ===========================================================================


-- ===========================================================================
-- VUE — volume total par filiale, tous temps confondus
--
--   Support du seuil de fiabilité du dashboard : les filiales comptant très
--   peu d'avis clients encombrent les classements sans rien apprendre, et un
--   taux calculé sur trois avis n'est pas comparable à un taux calculé sur
--   quatre cents.
--
--   TOUS TEMPS CONFONDUS, délibérément. Un décompte borné à la fenêtre
--   affichée ferait entrer et sortir des filiales à chaque changement de
--   période : la composition du classement dépendrait du zoom, ce qui est
--   exactement ce qu'un lecteur ne peut pas anticiper. Le volume total est une
--   propriété stable de la filiale, pas de la vue.
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
