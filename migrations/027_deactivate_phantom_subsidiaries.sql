-- ===========================================================================
-- 027 — désactivation des 4 filiales fantômes (MTN RDC, MTN Guinée-Conakry,
-- MTN Guinée-Bissau, Orange Niger)
-- ===========================================================================
--
-- CE QUI A ÉTÉ VÉRIFIÉ, PAS SUPPOSÉ — recherche du 24 août 2026
-- (`tools/backfill_press_operator_data_2026.py`), corroborée le 26 août 2026
-- par une seconde recherche indépendante :
--
--   - MTN RDC : MTN N'EXPLOITE AUCUN RÉSEAU LICENCIÉ EN RDC. Le régulateur
--     (ARPTC) accuse même MTN (13 février 2026) d'un débordement de signal
--     ILLÉGAL près de Goma/Rutshuru depuis le Rwanda voisin — ce n'est pas une
--     filiale, c'est une interférence transfrontalière. Les opérateurs agréés
--     en RDC sont Orange, Vodacom, Africell et Airtel.
--     Source : developingtelecoms.com, 16/02/2026.
--
--   - MTN Guinée-Conakry : vendue à l'État guinéen, cession finalisée le
--     30/12/2024 (confirmé par le CEO du groupe MTN, Ralph Mupita, dans le
--     cadre de la stratégie de simplification du portefeuille).
--     Source : connectingafrica.com ; datacenterdynamics.com.
--
--   - MTN Guinée-Bissau : vendue à Telecel Group Mobile, cession finalisée le
--     07/08/2024, approuvée par le régulateur national (ARN).
--     Source : communiqué conjoint MTN Group / Telecel Group, mtn.com
--     (source primaire — l'opérateur lui-même).
--
--   - Orange Niger : cédée par le groupe Orange à des actionnaires
--     minoritaires (Zamani Com) en novembre 2019, rebaptisée Zamani Telecom
--     depuis décembre 2020 — près de six ans avant cette recherche.
--     Source : developingtelecoms.com ; telecompaper.com.
--
-- POURQUOI `active = false` ET NON UNE SUPPRESSION
--   Chaque filiale porte déjà des avis réellement collectés (37 à 92 selon la
--   filiale) — ce sont des mesures faites, pas une erreur de saisie à effacer.
--   Les supprimer romprait irréversiblement la traçabilité de ce qui a été
--   collecté et pourquoi. `dim_subsidiary.active` existe depuis la migration
--   002 précisément pour ce cas : la ligne reste, l'historique reste
--   interrogeable, mais la filiale cesse d'apparaître dans les agrégats
--   affichés au dashboard (voir la vue `v_reviews_enriched` ci-dessous) et
--   dans les exigences de couverture de l'agent qualité (déjà filtrées sur
--   `sub.active`, migrations antérieures).
--
--   Les sources de collecte correspondantes sont retirées de
--   `config/operators.json` dans le même changement : sans entrée, aucun
--   collecteur ne redemande plus jamais d'avis pour ces quatre filiales.
--
-- POURQUOI LE FILTRE EST AJOUTÉ DANS `v_reviews_enriched`, PAS SEULEMENT DANS
-- `v_stats_by_subsidiary`
--   `v_stats_by_country` et `v_stats_by_operator` agrègent DIRECTEMENT depuis
--   `v_reviews_enriched`, pas depuis `v_stats_by_subsidiary` — un filtre posé
--   seulement sur la vue « par filiale » aurait laissé les 211 avis peser sur
--   les tableaux RD Congo, Guinée, Guinée-Bissau et Niger, et sur les totaux
--   MTN/Orange groupe. Filtrer à la source commune (`v_reviews_enriched`)
--   propage la correction aux trois échelles à la fois — c'est le même
--   principe qui a justifié cette vue en premier lieu (une seule vérité,
--   jamais deux chiffres pour le même fait).
--
--   `sub.active IS NOT FALSE`, PAS `sub.active` : la jointure est un LEFT JOIN
--   et un avis ORPHELIN (`r.subsidiary_id` NULL, `sub` donc NULL) doit rester
--   visible — c'est tout l'enjeu de l'Agent 3 (migration 022) que de les
--   rendre visibles, pas de les faire disparaître une seconde fois derrière
--   ce filtre.
-- ===========================================================================

UPDATE dim_subsidiary sub
SET active = false
FROM dim_operator o, dim_country c
WHERE sub.operator_id = o.operator_id
  AND sub.country_id  = c.country_id
  AND (o.code, c.iso2) IN (
      ('mtn', 'CD'),     -- MTN RDC
      ('mtn', 'GN'),     -- MTN Guinée-Conakry
      ('mtn', 'GW'),     -- MTN Guinée-Bissau
      ('orange', 'NE')   -- Orange Niger
  );

CREATE OR REPLACE VIEW v_reviews_enriched AS
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

    -- DE QUOI CET AVIS PARLE-T-IL.
    CASE
        WHEN p.dit_app AND p.dit_operateur THEN 'both'
        WHEN p.dit_app                     THEN 'app'
        WHEN p.dit_operateur               THEN 'operator'
        ELSE COALESCE(src.default_about, 'operator')
    END                                         AS about,

    -- COMMENT ON L'A SU. Même rôle que `sentiment_source` : sans cette colonne,
    -- un verdict lu dans le texte serait indiscernable d'un défaut de source, et
    -- la part du corpus classée par présomption resterait invisible — donc
    -- impossible à annoncer au lecteur, et impossible à surveiller.
    CASE WHEN p.dit_app OR p.dit_operateur THEN 'aspects' ELSE 'source' END
                                                AS about_source,

    sub.subsidiary_id, sub.name AS subsidiary,
    op.operator_id,    op.name  AS operator,   op.parent_group,
    co.country_id,     co.iso2, co.name AS country, co.region,
    src.source_id,     src.code AS source_code, src.name AS source,
    src.kind AS source_kind,
    COALESCE(src.comparable, TRUE)              AS source_comparable,
    COALESCE(src.default_about, 'operator')     AS source_default_about
FROM reviews r
LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
LEFT JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
LEFT JOIN dim_country    co  ON co.country_id     = sub.country_id
LEFT JOIN dim_source     src ON src.source_id     = r.source_id
CROSS JOIN LATERAL (
    SELECT
        (r.neg_aspects || r.pos_aspects)
            && (SELECT array_agg(aspect) FROM dim_aspect WHERE scope = 'app')
                                                AS dit_app,
        (r.neg_aspects || r.pos_aspects)
            && (SELECT array_agg(aspect) FROM dim_aspect WHERE scope = 'operator')
                                                AS dit_operateur
) p
-- NOUVEAU (027) : écarte les avis d'une filiale désactivée des agrégats
-- affichés, sans toucher aux avis orphelins (`sub` NULL, voir ci-dessus).
WHERE sub.active IS NOT FALSE;

-- v_review_terms et v_review_aspects NE LISENT PAS v_reviews_enriched — elles
-- repartent chacune de `reviews r`, comme la migration 019 l'a fait pour
-- éviter d'empiler une vue sur une vue à ce niveau de volumétrie. Le filtre
-- doit donc être répété ici, sur les QUATRE branches des deux UNION ALL, pour
-- que les nuages de mots et les aspects cessent eux aussi de citer des
-- filiales désactivées — même invariant qu'en tête de fichier : une seule
-- vérité, jamais deux chiffres pour le même fait selon l'écran consulté.
--
-- REPRISES DE LA 021 (PAS DE LA 019) : c'est elle qui porte la dernière
-- définition de ces deux vues (`about_source`, puis `aspect_scope` hérité de
-- la 020 pour `v_review_aspects`) — les reprendre depuis la 019 aurait fait
-- disparaître ces deux colonnes et cassé tout ce qui les lit
-- (`reviews/storage/filters.py`, l'Agent 1).
CREATE OR REPLACE VIEW v_review_terms AS
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
WHERE sub.active IS NOT FALSE

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
) p
WHERE sub.active IS NOT FALSE;

CREATE OR REPLACE VIEW v_review_aspects AS
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
WHERE sub.active IS NOT FALSE

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
) p
WHERE sub.active IS NOT FALSE;
