-- ===========================================================================
-- 007 — Sources comparables et sources non comparables
-- ===========================================================================
--
-- LE PROBLÈME, MESURÉ
--   HelloPeter est une plateforme de PLAINTES : on n'y va que quand ça a mal
--   tourné. Résultat mesuré sur le corpus réel : 97,7 % d'avis négatifs, contre
--   60,7 % pour l'App Store, 37,0 % pour Google Play et 31,4 % pour Google Maps.
--   Ce n'est pas un défaut de collecte, c'est la nature de la plateforme.
--
--   Mélangée aux autres, elle déplace la part de négatifs des filiales
--   sud-africaines de +1,5 point (Telkom) à +11,7 points (Vodacom). Le décalage
--   n'est PAS constant : il dépend du rapport entre le volume HelloPeter et le
--   reste, donc il bouge à chaque run. Aucun coefficient correcteur fixe ne peut
--   le neutraliser — il corrigerait Vodacom en aggravant Telkom.
--
--   Effet observé dès le premier run : deux alertes ERROR « pic de
--   mécontentement » sur MTN South Africa et Cell C. La règle d'alerte est
--   correcte ; ce qui avait changé n'était pas la satisfaction, mais la
--   composition des sources.
--
-- POURQUOI PAS UN TROISIÈME `kind`
--   `kind` vaut 'customer_review' ou 'press', et il commande DEUX choses à la
--   fois : ce qui entre dans la satisfaction, et ce qui entre dans les
--   verbatims, les motifs et la couche sémantique (reviews/llm/semantic.py
--   filtre sur kind='customer_review'). Un kind='complaint_platform' aurait
--   sorti HelloPeter des deux côtés — or c'est précisément la source la plus
--   riche du corpus pour les motifs : 886 caractères en moyenne contre 59 sur
--   Google Play, 82 % d'avis substantiels contre 6 %.
--
--   D'où un axe SÉPARÉ. `kind` continue de dire CE QUE C'EST (de la voix
--   client, et ça l'est), `comparable` dit SI ÇA ENTRE DANS UNE COMPARAISON
--   entre filiales. Les deux questions étaient confondues, elles ne le sont
--   plus.
-- ---------------------------------------------------------------------------

ALTER TABLE dim_source
    ADD COLUMN IF NOT EXISTS comparable BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN dim_source.comparable IS
    'Cette source entre-t-elle dans les agrégats de satisfaction comparés '
    'entre filiales ? FALSE pour une source dont le biais de recrutement rend '
    'le taux incomparable à celui des autres (plateforme de plaintes). Les '
    'avis restent collectés, stockés, et servent aux verbatims et aux motifs.';

-- Le défaut est TRUE : toute source existante et toute source future entre dans
-- les comparaisons, sauf décision explicite ici. C'est le bon défaut — une
-- source oubliée doit être visible, pas silencieusement écartée.
UPDATE dim_source SET comparable = FALSE WHERE code = 'hellopeter';


-- ---------------------------------------------------------------------------
-- VUES — reconstruites pour exposer `source_comparable`
--   Reprises de la migration 005, à l'identique, plus la colonne. Le DROP
--   CASCADE emporte v_review_terms et v_review_aspects : elles sont donc
--   reconstruites plus bas, sans quoi les onglets Motifs et Aspects tomberaient
--   au premier redémarrage.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_reviews_enriched CASCADE;
CREATE VIEW v_reviews_enriched AS
SELECT
    r.review_id, r.title, r.text, r.rating, r.verified,
    r.created_at, r.collected_at, r.run_id,

    COALESCE(r.llm_sentiment, r.sentiment)      AS sentiment,
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
    src.kind AS source_kind,
    -- COALESCE : un avis dont le source_id est NULL (source jamais déclarée
    -- dans dim_source) doit rester comparable par défaut, sinon il
    -- disparaîtrait des agrégats en plus d'être déjà orphelin de dimension.
    COALESCE(src.comparable, TRUE)              AS source_comparable
FROM reviews r
LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
LEFT JOIN dim_source     src ON src.source_id     = r.source_id;


-- --- v_review_terms — reprise de la 005, emportée par le CASCADE ------------
--   VOLONTAIREMENT SANS FILTRE `comparable` : l'onglet Motifs doit voir
--   HelloPeter. C'est là que se trouve tout l'intérêt de cette source.
DROP VIEW IF EXISTS v_review_terms CASCADE;
CREATE VIEW v_review_terms AS
SELECT
    r.review_id, t.term, 'negative'::text AS polarity, r.sentiment_score,
    COALESCE(r.created_at, r.collected_at) AS occurred_at,
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
    r.review_id, t.term, 'positive'::text AS polarity, r.sentiment_score,
    COALESCE(r.created_at, r.collected_at) AS occurred_at,
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


-- --- v_review_aspects — reprise de la 005, emportée par le CASCADE ----------
--   Sans filtre `comparable` non plus, et pour la même raison.
DROP VIEW IF EXISTS v_review_aspects CASCADE;
CREATE VIEW v_review_aspects AS
SELECT
    r.review_id, a.aspect, 'negative'::text AS polarity, r.sentiment_score,
    COALESCE(r.created_at, r.collected_at) AS occurred_at,
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
    r.review_id, a.aspect, 'positive'::text AS polarity, r.sentiment_score,
    COALESCE(r.created_at, r.collected_at) AS occurred_at,
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


-- --- v_subsidiary_volume — le seuil de fiabilité doit compter pareil --------
--   `min_subsidiary_reviews` écarte les filiales à trop peu d'avis CLIENTS.
--   Si ce décompte incluait HelloPeter alors que les taux l'excluent, une
--   filiale pourrait franchir le seuil grâce à des avis qui n'entrent dans
--   aucun de ses taux : elle apparaîtrait dans un classement avec un
--   dénominateur bien plus petit qu'annoncé.
DROP VIEW IF EXISTS v_subsidiary_volume CASCADE;
CREATE VIEW v_subsidiary_volume AS
SELECT
    sub.subsidiary_id,
    COUNT(r.review_id) FILTER (
        WHERE src.kind = 'customer_review' AND src.comparable
    )                                                              AS avis_clients,
    COUNT(r.review_id) FILTER (
        WHERE src.kind = 'customer_review' AND NOT src.comparable
    )                                                   AS avis_hors_comparaison,
    COUNT(r.review_id) FILTER (WHERE src.kind = 'press')           AS articles_presse,
    COUNT(r.review_id)                                             AS total
FROM dim_subsidiary sub
LEFT JOIN reviews    r   ON r.subsidiary_id = sub.subsidiary_id
LEFT JOIN dim_source src ON src.source_id   = r.source_id
GROUP BY sub.subsidiary_id;


-- --- v_stats_by_* — même définition d'`avis_clients` partout ---------------
--   Ces trois vues servent les endpoints /stats/by-country, /by-operator et
--   /by-subsidiary. Les laisser sur l'ancienne règle ferait dire DEUX choses au
--   même mot : la barre de filtres annoncerait 336 avis clients pour Vodacom
--   pendant que le classement calculerait ses taux sur 281. C'est exactement
--   l'incohérence que le bloc `_MEASURES` avait été créé pour supprimer.
DROP VIEW IF EXISTS v_stats_by_country CASCADE;
CREATE VIEW v_stats_by_country AS
SELECT
    co.country_id, co.iso2, co.name AS country, co.region,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND src.comparable)
                                                         AS avis_clients,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND NOT src.comparable)
                                                         AS avis_hors_comparaison,
    COUNT(*) FILTER (WHERE src.kind = 'press')           AS articles_presse,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND src.comparable
                       AND r.sentiment = 'positive')     AS positifs,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND src.comparable
                       AND r.sentiment = 'neutral')      AS neutres,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND src.comparable
                       AND r.sentiment = 'negative')     AS negatifs,
    ROUND((AVG(r.rating) FILTER (
        WHERE src.kind = 'customer_review' AND src.comparable))::numeric, 2)
                                                         AS note_moyenne
FROM reviews r
JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
JOIN dim_country    co  ON co.country_id     = sub.country_id
JOIN dim_source     src ON src.source_id     = r.source_id
GROUP BY co.country_id, co.iso2, co.name, co.region;

DROP VIEW IF EXISTS v_stats_by_operator CASCADE;
CREATE VIEW v_stats_by_operator AS
SELECT
    op.operator_id, op.name AS operator, op.parent_group,
    COUNT(DISTINCT sub.country_id)                       AS nb_pays,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND src.comparable)
                                                         AS avis_clients,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND NOT src.comparable)
                                                         AS avis_hors_comparaison,
    COUNT(*) FILTER (WHERE src.kind = 'press')           AS articles_presse,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND src.comparable
                       AND r.sentiment = 'positive')     AS positifs,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND src.comparable
                       AND r.sentiment = 'neutral')      AS neutres,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND src.comparable
                       AND r.sentiment = 'negative')     AS negatifs,
    ROUND((AVG(r.rating) FILTER (
        WHERE src.kind = 'customer_review' AND src.comparable))::numeric, 2)
                                                         AS note_moyenne
FROM reviews r
JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
JOIN dim_source     src ON src.source_id     = r.source_id
GROUP BY op.operator_id, op.name, op.parent_group;

DROP VIEW IF EXISTS v_stats_by_subsidiary CASCADE;
CREATE VIEW v_stats_by_subsidiary AS
SELECT
    sub.subsidiary_id, sub.name AS subsidiary,
    op.name AS operator, co.name AS country, co.iso2,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND src.comparable)
                                                         AS avis_clients,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND NOT src.comparable)
                                                         AS avis_hors_comparaison,
    COUNT(*) FILTER (WHERE src.kind = 'press')           AS articles_presse,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND src.comparable
                       AND r.sentiment = 'positive')     AS positifs,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND src.comparable
                       AND r.sentiment = 'neutral')      AS neutres,
    COUNT(*) FILTER (WHERE src.kind = 'customer_review' AND src.comparable
                       AND r.sentiment = 'negative')     AS negatifs,
    ROUND((AVG(r.rating) FILTER (
        WHERE src.kind = 'customer_review' AND src.comparable))::numeric, 2)
                                                         AS note_moyenne
FROM reviews r
JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
JOIN dim_country    co  ON co.country_id     = sub.country_id
JOIN dim_source     src ON src.source_id     = r.source_id
GROUP BY sub.subsidiary_id, sub.name, op.name, co.name, co.iso2;


-- ---------------------------------------------------------------------------
-- CONTRÔLE
--   SELECT code, kind, comparable FROM dim_source ORDER BY comparable, code;
--   -- attendu : hellopeter seul à FALSE
--
--   Après application, la part de négatifs des filiales sud-africaines doit
--   retrouver ses valeurs d'avant HelloPeter : Vodacom 24,6 %, Cell C 72,1 %,
--   MTN 74,6 %, Telkom 87,9 %.
-- ---------------------------------------------------------------------------
