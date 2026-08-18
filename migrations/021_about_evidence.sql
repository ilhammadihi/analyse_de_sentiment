-- ===========================================================================
-- 021 — Savoir, ou présumer : `about_source` sur les vues de motifs
-- ===========================================================================
--
-- LA RÈGLE À TENIR
--   « Un avis purement sur l'application ne doit pas apparaître dans un pic ni
--   dans les agents. »
--
-- CE QUI L'EN EMPÊCHAIT ENCORE
--   La migration 019 classe un avis sur ses ASPECTS quand elle en a, et sur le
--   défaut de sa SOURCE quand elle n'en a pas. Le second cas n'est pas une
--   connaissance, c'est une présomption — et c'est par là que la règle fuit.
--
--   Mesuré au 16 août sur les 10 093 avis du côté opérateur :
--
--       classés sur leur TEXTE      8 182   (aspects)
--       classés par PRÉSOMPTION     1 911   (google_maps 1 872, reddit 30,
--                                            hellopeter 9)
--
--   Sur ces 1 911, treize contiennent du vocabulaire applicatif et deux sont de
--   purs avis d'application : « In any case, this app is great! » et
--   « Convinced of the application », tous deux remontés par Google Maps. Deux
--   sur dix mille — mais ce sont précisément ceux qu'une notification cite.
--
-- LE PARTAGE RETENU, ET POURQUOI IL N'EST PAS LE MÊME DES DEUX CÔTÉS
--   COMPTER garde la présomption. Les 1 872 avis Google Maps sont des notes
--   d'AGENCES PHYSIQUES : on y juge un guichet, pas un logiciel. Les écarter
--   retirerait 19 % du côté opérateur — et le socle du signal pour les 130
--   filiales que Google Maps est seul à couvrir — pour rattraper deux lignes.
--
--   CITER exige la preuve. Une citation est ce que le lecteur retient : un seul
--   « this app is great! » sous un « pic de mécontentement » discrédite toute
--   l'alerte, alors qu'il ne pèse rien dans le taux. Le coût est nul — une
--   alerte cite deux avis et le côté opérateur en offre 8 182 vérifiés sur
--   texte.
--
--   D'où `about_strict`, côté Python, qui exige désormais les DEUX propriétés :
--   l'avis ne parle que de ce côté-ci, ET on le sait de son texte.
--
-- POURQUOI LA COLONNE VA AUSSI SUR LES VUES DE MOTIFS
--   `v_reviews_enriched` l'expose depuis la 019. Les deux vues de motifs, non.
--   L'invariant de `reviews/storage/filters.py` veut que TOUT axe filtrable soit
--   présent sur TOUTE vue filtrable : un axe manquant ne lève pas d'erreur, il
--   rend un filtre silencieusement ignoré — ou, pour un prédicat d'égalité sur
--   une colonne absente remplacée par NULL, un écran vide sans cause visible.
--
-- NON DESTRUCTIF — une colonne de plus sur deux vues, calculée depuis ce que
-- les vues calculaient déjà. Aucune donnée touchée.
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS v_review_terms CASCADE;
CREATE VIEW v_review_terms AS
SELECT
    r.review_id, t.term, 'negative'::text AS polarity, r.sentiment_score,
    COALESCE(r.created_at, r.collected_at) AS occurred_at,
    sub.subsidiary_id, sub.name AS subsidiary,
    op.operator_id,    op.name  AS operator,
    co.country_id,     co.iso2, co.name AS country, co.region,
    src.kind AS source_kind, src.code AS source_code,
    CASE
        WHEN p.dit_app AND p.dit_operateur THEN 'both'
        WHEN p.dit_app                     THEN 'app'
        WHEN p.dit_operateur               THEN 'operator'
        ELSE COALESCE(src.default_about, 'operator')
    END AS about,
    CASE WHEN p.dit_app OR p.dit_operateur THEN 'aspects' ELSE 'source' END
        AS about_source
FROM reviews r
CROSS JOIN LATERAL unnest(r.neg_terms) AS t(term)
LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
LEFT JOIN dim_source     src ON src.source_id     = r.source_id
CROSS JOIN LATERAL (
    SELECT (r.neg_aspects || r.pos_aspects)
               && (SELECT array_agg(aspect) FROM dim_aspect WHERE scope = 'app')
                                                AS dit_app,
           (r.neg_aspects || r.pos_aspects)
               && (SELECT array_agg(aspect) FROM dim_aspect WHERE scope = 'operator')
                                                AS dit_operateur
) p

UNION ALL

SELECT
    r.review_id, t.term, 'positive'::text AS polarity, r.sentiment_score,
    COALESCE(r.created_at, r.collected_at) AS occurred_at,
    sub.subsidiary_id, sub.name AS subsidiary,
    op.operator_id,    op.name  AS operator,
    co.country_id,     co.iso2, co.name AS country, co.region,
    src.kind AS source_kind, src.code AS source_code,
    CASE
        WHEN p.dit_app AND p.dit_operateur THEN 'both'
        WHEN p.dit_app                     THEN 'app'
        WHEN p.dit_operateur               THEN 'operator'
        ELSE COALESCE(src.default_about, 'operator')
    END AS about,
    CASE WHEN p.dit_app OR p.dit_operateur THEN 'aspects' ELSE 'source' END
        AS about_source
FROM reviews r
CROSS JOIN LATERAL unnest(r.pos_terms) AS t(term)
LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
LEFT JOIN dim_source     src ON src.source_id     = r.source_id
CROSS JOIN LATERAL (
    SELECT (r.neg_aspects || r.pos_aspects)
               && (SELECT array_agg(aspect) FROM dim_aspect WHERE scope = 'app')
                                                AS dit_app,
           (r.neg_aspects || r.pos_aspects)
               && (SELECT array_agg(aspect) FROM dim_aspect WHERE scope = 'operator')
                                                AS dit_operateur
) p;


-- --- v_review_aspects — reprise de la 020, plus la colonne -------------------
--   ATTENTION À UNE ÉVIDENCE FAUSSE : on croit volontiers qu'ici `about_source`
--   vaut toujours 'aspects', puisqu'une ligne n'existe dans cette vue que parce
--   qu'un aspect a été reconnu. La base dit le contraire (5 795 lignes à
--   'source'), et la raison tient en un mot : `autre`.
--
--   `autre` est le REPLI de la taxonomie, de portée 'none' (migration 019). Un
--   avis dont c'est le seul aspect produit bien une ligne ici, mais ne fait
--   basculer ni `dit_app` ni `dit_operateur` : son côté reste présumé de sa
--   source. La colonne est donc bel et bien nécessaire sur cette vue, et pas
--   seulement pour respecter l'invariant du module de filtres.
DROP VIEW IF EXISTS v_review_aspects CASCADE;
CREATE VIEW v_review_aspects AS
SELECT
    r.review_id, a.aspect, 'negative'::text AS polarity, r.sentiment_score,
    r.llm_confidence,
    COALESCE(r.created_at, r.collected_at) AS occurred_at,
    sub.subsidiary_id, sub.name AS subsidiary,
    op.operator_id,    op.name  AS operator,
    co.country_id,     co.iso2, co.name AS country, co.region,
    src.kind AS source_kind, src.code AS source_code,
    CASE
        WHEN p.dit_app AND p.dit_operateur THEN 'both'
        WHEN p.dit_app                     THEN 'app'
        WHEN p.dit_operateur               THEN 'operator'
        ELSE COALESCE(src.default_about, 'operator')
    END AS about,
    CASE WHEN p.dit_app OR p.dit_operateur THEN 'aspects' ELSE 'source' END
        AS about_source,
    COALESCE(d.scope, 'none') AS aspect_scope
FROM reviews r
CROSS JOIN LATERAL unnest(r.neg_aspects) AS a(aspect)
LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
LEFT JOIN dim_source     src ON src.source_id     = r.source_id
LEFT JOIN dim_aspect     d   ON d.aspect          = a.aspect
CROSS JOIN LATERAL (
    SELECT (r.neg_aspects || r.pos_aspects)
               && (SELECT array_agg(aspect) FROM dim_aspect WHERE scope = 'app')
                                                AS dit_app,
           (r.neg_aspects || r.pos_aspects)
               && (SELECT array_agg(aspect) FROM dim_aspect WHERE scope = 'operator')
                                                AS dit_operateur
) p

UNION ALL

SELECT
    r.review_id, a.aspect, 'positive'::text AS polarity, r.sentiment_score,
    r.llm_confidence,
    COALESCE(r.created_at, r.collected_at) AS occurred_at,
    sub.subsidiary_id, sub.name AS subsidiary,
    op.operator_id,    op.name  AS operator,
    co.country_id,     co.iso2, co.name AS country, co.region,
    src.kind AS source_kind, src.code AS source_code,
    CASE
        WHEN p.dit_app AND p.dit_operateur THEN 'both'
        WHEN p.dit_app                     THEN 'app'
        WHEN p.dit_operateur               THEN 'operator'
        ELSE COALESCE(src.default_about, 'operator')
    END AS about,
    CASE WHEN p.dit_app OR p.dit_operateur THEN 'aspects' ELSE 'source' END
        AS about_source,
    COALESCE(d.scope, 'none') AS aspect_scope
FROM reviews r
CROSS JOIN LATERAL unnest(r.pos_aspects) AS a(aspect)
LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
LEFT JOIN dim_source     src ON src.source_id     = r.source_id
LEFT JOIN dim_aspect     d   ON d.aspect          = a.aspect
CROSS JOIN LATERAL (
    SELECT (r.neg_aspects || r.pos_aspects)
               && (SELECT array_agg(aspect) FROM dim_aspect WHERE scope = 'app')
                                                AS dit_app,
           (r.neg_aspects || r.pos_aspects)
               && (SELECT array_agg(aspect) FROM dim_aspect WHERE scope = 'operator')
                                                AS dit_operateur
) p;


-- ===========================================================================
-- CONTRÔLES POST-MIGRATION (à exécuter à la main, ne modifient rien)
--
--   -- 1. Ce que le mode « citation » a le droit de reprendre. Doit exclure
--   --    toute présomption.
--   SELECT about_source, COUNT(*) FROM v_reviews_enriched
--    WHERE source_kind='customer_review' AND about='operator' GROUP BY 1;
--
--   -- 2. La présomption doit RECULER à mesure du rattrapage sémantique. Si
--   --    elle stagne, c'est la couche sémantique qui est à l'arrêt.
--   SELECT source_code, COUNT(*) FROM v_reviews_enriched
--    WHERE source_kind='customer_review' AND about_source='source'
--    GROUP BY 1 ORDER BY 2 DESC;
--
--   -- 3. Les lignes de v_review_aspects encore présumées portent forcément
--   --    l'aspect de repli. DOIT RENDRE UNE SEULE LIGNE (« autre »).
--   SELECT DISTINCT aspect FROM v_review_aspects WHERE about_source = 'source';
-- ===========================================================================
