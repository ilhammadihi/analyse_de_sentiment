-- ===========================================================================
-- 020 — Le côté de l'ASPECT, à côté du côté de l'AVIS
-- ===========================================================================
--
-- CE QUE LA MIGRATION 019 A LAISSÉ OUVERT
--   Elle a donné à chaque AVIS un objet (`about`). Elle a délibérément gardé
--   les avis mixtes des deux côtés : un avis qui dénonce une recharge perdue ET
--   un bug de connexion est un client mécontent du service, l'écarter du
--   dénominateur sous-estimerait le mécontentement. Ce choix reste bon POUR
--   COMPTER.
--
--   Il ne l'est plus POUR NOMMER. Mesuré après la 019, le classement des motifs
--   de service affichait :
--
--       Service client            1 925
--       Facturation & prix        1 455
--       Recharge & paiement       1 066
--       Bugs de l'application     1 030   <-- ici
--       Agence & boutique           879
--
--   Ces 1 030 avis sont bien dans le périmètre opérateur — ils y ont chacun une
--   plainte de service. Mais l'ASPECT cité, lui, parle du logiciel. L'Agent 1
--   écrivait donc « Les plaintes portent surtout sur : service client,
--   facturation, bugs de l'application » sous un titre annonçant une
--   dégradation du SERVICE. Chaque chiffre était exact et la phrase trompeuse.
--
--   Il fallait donc distinguer deux questions que `about` confondait :
--       « cet AVIS concerne-t-il le service ? »   -> about (migration 019)
--       « cet ASPECT concerne-t-il le service ? » -> aspect_scope (ici)
--
-- NON DESTRUCTIF — une colonne de plus sur une vue, rien d'autre. La colonne
-- vient de `dim_aspect`, déjà seedée par la 019 : aucune liste nouvelle à tenir.
-- ---------------------------------------------------------------------------

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
    -- Côté de l'AVIS (migration 019) : ce qu'il faut pour compter.
    CASE
        WHEN p.dit_app AND p.dit_operateur THEN 'both'
        WHEN p.dit_app                     THEN 'app'
        WHEN p.dit_operateur               THEN 'operator'
        ELSE COALESCE(src.default_about, 'operator')
    END AS about,
    -- Côté de l'ASPECT : ce qu'il faut pour nommer.
    --
    -- COALESCE 'none' plutôt que NULL : un aspect absent de dim_aspect ne doit
    -- être écarté d'AUCUN côté. Il serait sinon invisible partout — la panne la
    -- plus silencieuse possible pour un motif nouvellement ajouté.
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
--   -- 1. Motifs du SERVICE — plus aucun aspect d'application ne doit y figurer.
--   SELECT aspect, COUNT(*) FROM v_review_aspects
--    WHERE polarity='negative' AND source_kind='customer_review'
--      AND about <> 'app' AND aspect_scope <> 'app'
--    GROUP BY 1 ORDER BY 2 DESC LIMIT 8;
--
--   -- 2. Aucun aspect ne doit tomber en 'none' hors « autre » : toute autre
--   --    ligne est un motif ajouté à la taxonomie Python et jamais déclaré
--   --    dans dim_aspect. DOIT RENDRE UNE SEULE LIGNE (« autre »).
--   SELECT DISTINCT aspect FROM v_review_aspects WHERE aspect_scope = 'none';
-- ===========================================================================
