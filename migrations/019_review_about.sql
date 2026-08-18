-- ===========================================================================
-- 019 — De quoi l'avis parle-t-il : de l'APPLICATION, ou de l'OPÉRATEUR ?
-- ===========================================================================
--
-- LE PROBLÈME, MESURÉ
--   Les boutiques d'applications pèsent 26 196 avis sur 31 619 avis clients,
--   soit 83 % du corpus. Or on n'y parle pas de la même chose qu'ailleurs :
--   c'est l'ÉDITEUR DE L'APPLICATION qu'on y interpelle, pas l'opérateur.
--
--   Comptés sur les aspects déjà reconnus par la couche sémantique
--   (migration 005), les trois premiers motifs négatifs des boutiques sont :
--
--       app_bugs        3 598 avis   (1 816 Play + 1 782 App Store)
--       app_connexion   2 381 avis
--       app_ergonomie   1 715 avis
--
--   Ces 7 694 avis décrivent un logiciel qui plante, un code OTP qui n'arrive
--   pas, une refonte d'interface mal accueillie. Aucun n'est un jugement sur la
--   couverture réseau, la facturation ou l'accueil en boutique — c'est-à-dire
--   sur ce que le tableau de bord prétend mesurer quand il classe les filiales.
--
--   CONSÉQUENCE DIRECTE, ET C'EST L'OBJET DE CETTE MIGRATION : une mise à jour
--   ratée de l'application fait monter la part de négatifs d'une filiale, la
--   règle `negative_spike` tire un « pic de mécontentement », et l'Agent 1 en
--   cherche ensuite la cause du côté du service — un réseau qui n'a pas bougé,
--   une facturation qui n'a pas changé. Le signal est juste, son étiquette est
--   fausse, et tout ce qui se branche dessus hérite de l'erreur.
--
-- POURQUOI PAS SIMPLEMENT ÉCARTER LES BOUTIQUES D'APPLICATIONS
--   Parce que la mesure l'interdit. Sur les avis de boutiques déjà analysés,
--   4 114 ne parlent QUE de l'opérateur :
--
--       recharge_paiement   1 067 avis     (argent non crédité, mobile money)
--       facturation_prix    1 050 avis
--       service_client      1 010 avis
--       forfaits_data         873 avis
--
--   Un client dont la recharge a disparu écrit là où il a l'habitude d'écrire,
--   et c'est de plus en plus l'application. Écarter la source entière jetterait
--   ces 4 114 plaintes — les plus coûteuses de toutes pour un opérateur.
--
--   La séparation ne peut donc PAS se faire par source. Elle se fait AVIS PAR
--   AVIS, sur ce que l'avis dit réellement.
--
-- POURQUOI PAS UN QUATRIÈME `kind`, NI UN SECOND `comparable`
--   `kind` dit CE QUE C'EST (voix client ou presse). `comparable` (migration
--   007) dit SI ÇA ENTRE DANS UNE COMPARAISON entre filiales. Ces deux axes
--   sont des propriétés de la SOURCE : tout HelloPeter est hors comparaison,
--   tout flux RSS est de la presse.
--
--   Ici la propriété n'appartient pas à la source mais à L'AVIS : deux avis du
--   même Play Store, le même jour, sur la même filiale, ne parlent pas du même
--   objet. D'où un troisième axe, `about`, porté par la ligne de fait et non
--   par la dimension.
--
-- CE QUE L'AXE VAUT
--   'operator'  l'avis nomme un grief de service : réseau, facturation,
--               recharge, service client, boutique, SIM, roaming…
--   'app'       l'avis nomme un grief applicatif : bug, connexion, ergonomie.
--   'both'      l'avis nomme les deux. Mesuré : 2 006 avis, dont 93,0 % de
--               négatifs — de très loin la population la plus mécontente du
--               corpus. Ces avis comptent DES DEUX CÔTÉS, et c'est voulu : ils
--               contiennent réellement un grief de service, l'écarter reviendrait
--               à perdre la plainte la mieux argumentée du corpus.
--
--   Aucune valeur 'inconnu'. Un avis sans aspect exploitable — « très bien »,
--   « smooth », « good 😊 », 25 à 31 caractères en moyenne — reçoit le défaut
--   de sa source (voir `dim_source.default_about`). Une quatrième valeur aurait
--   forcé chaque agrégat à trancher son sort en silence, à un endroit différent
--   dans chaque requête ; le défaut le tranche UNE fois, ici, et le dit.
--
-- CE QUE ÇA CHANGE, MESURÉ AVANT APPLICATION
--   Répartition des 30 857 avis clients comparables :
--
--       app        20 107 avis   43,2 % de négatifs
--       operator    8 744 avis   55,4 % de négatifs
--       both        2 006 avis   93,0 % de négatifs
--
--   La satisfaction OPÉRATEUR se calculera donc désormais sur 10 750 avis au
--   lieu de 30 857. C'est une perte de volume assumée : les 20 107 avis retirés
--   ne portaient aucun jugement sur le service.
--
--   Sur les 133 filiales ayant des avis, 101 dépassaient le seuil de fiabilité
--   de 30 avis ; 73 le dépassent après séparation. Les 28 filiales qui en
--   sortent ne perdent pas de l'information : elles apprennent que leur volume
--   apparent était surtout constitué de notes d'application.
--
--   AUCUNE filiale ne tombe à zéro. Google Maps en couvre 130 sur 133, et
--   Google Maps parle d'agences — donc de l'opérateur.
--
-- NON DESTRUCTIF — aucune colonne de fait n'est modifiée. `about` est CALCULÉ
-- dans la vue, jamais stocké : il dérive des aspects, qui changent quand la
-- couche sémantique repasse une ligne. Stocké, il se serait périmé en silence
-- à chaque nouvelle analyse — exactement le défaut que `sentiment =
-- COALESCE(llm_sentiment, sentiment)` évite depuis la migration 005.
-- ---------------------------------------------------------------------------


-- ===========================================================================
-- DIMENSION — de quel côté chaque aspect de la taxonomie tombe
--
--   La liste des aspects vit en Python (reviews/domain/aspects.py) : c'est elle
--   qui part dans le prompt. Cette table n'en est pas une copie concurrente,
--   c'est sa PROJECTION SQL : elle ne porte que le découpage app / opérateur,
--   dont le SQL a besoin et que le prompt ignore.
--
--   POURQUOI UNE TABLE PLUTÔT QU'UN ARRAY EN DUR DANS LA VUE
--     Le jour où la taxonomie gagne un `app_paiement`, un tableau littéral
--     écrit dans la vue continuerait de le ranger du côté opérateur. Sans
--     erreur : les griefs applicatifs se remettraient simplement à polluer la
--     satisfaction du service, et il faudrait re-mesurer pour s'en apercevoir.
--
--     Avec une table, l'oubli est RATTRAPABLE et RATTRAPÉ : le test
--     `test_aspect_scopes_couvrent_la_taxonomie` compare cette table à la
--     taxonomie Python et échoue sur tout aspect non classé.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS dim_aspect (
    aspect      TEXT PRIMARY KEY,
    -- 'app'      : ne concerne que le logiciel.
    -- 'operator' : concerne le service rendu par la filiale.
    -- 'none'     : ne tranche rien ('autre' est le repli de la taxonomie ;
    --              il ne doit surtout pas faire basculer un avis d'un côté).
    scope       VARCHAR(10) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_aspect_scope CHECK (scope IN ('app', 'operator', 'none'))
);

COMMENT ON TABLE dim_aspect IS
    'Découpage app / opérateur de la taxonomie d''aspects. Miroir SQL de '
    'reviews/domain/aspects.py — tenu synchrone par un test.';

INSERT INTO dim_aspect (aspect, scope) VALUES
    -- L'application comme produit logiciel. Trois aspects, 7 694 avis.
    ('app_bugs',              'app'),
    ('app_connexion',         'app'),
    ('app_ergonomie',         'app'),

    -- Le service rendu par la filiale. C'est ce que le dashboard compare.
    ('reseau_couverture',     'operator'),
    ('debit_lenteur',         'operator'),
    ('coupures_pannes',       'operator'),
    ('facturation_prix',      'operator'),
    ('forfaits_data',         'operator'),
    ('recharge_paiement',     'operator'),
    ('service_client',        'operator'),
    ('agence_boutique',       'operator'),
    ('sim_identification',    'operator'),
    ('roaming_international', 'operator'),
    ('promotions_offres',     'operator'),
    ('fibre_domicile',        'operator'),
    ('fraude_securite',       'operator'),

    -- Le repli de la taxonomie. Ne tranche rien, par construction.
    ('autre',                 'none')
ON CONFLICT (aspect) DO UPDATE SET scope = EXCLUDED.scope;


-- ===========================================================================
-- DIMENSION SOURCE — de quoi parle, par défaut, un avis qu'on n'a pas pu lire
--
--   MESURÉ, ET C'EST CE QUI JUSTIFIE LE DÉFAUT : parmi les avis de boutiques
--   dont la couche sémantique a tiré un aspect nommable, 67 % portent sur
--   l'application et 33 % sur l'opérateur. Un avis de boutique illisible est
--   donc, deux fois sur trois, un avis sur l'application.
--
--   Les avis concernés sont d'ailleurs reconnaissables : 25 à 31 caractères en
--   moyenne, contre 108 à 132 pour ceux qui nomment un aspect. « very good
--   app », « smooth », « good 😊 ». Ce sont des notes d'application, pas des
--   jugements sur un opérateur télécom.
--
--   Google Maps note des AGENCES, HelloPeter et Reddit parlent de service :
--   leur défaut reste 'operator', qui est celui de la colonne.
--
--   POURQUOI SUR LA DIMENSION ET NON DANS LA VUE
--     Le jour où un collecteur AppGallery ou Aptoide arrive, le défaut se règle
--     par un UPDATE d'une ligne. Écrit dans la vue, il aurait fallu y penser —
--     et l'oubli aurait été silencieux, la nouvelle source héritant du défaut
--     'operator' et reversant ses bugs d'application dans la satisfaction du
--     service.
-- ===========================================================================
ALTER TABLE dim_source
    ADD COLUMN IF NOT EXISTS default_about VARCHAR(20) NOT NULL DEFAULT 'operator';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'chk_source_default_about') THEN
        ALTER TABLE dim_source ADD CONSTRAINT chk_source_default_about
            CHECK (default_about IN ('app', 'operator'));
    END IF;
END $$;

COMMENT ON COLUMN dim_source.default_about IS
    'Objet présumé d''un avis de cette source dont aucun aspect ne tranche. '
    '''app'' pour les boutiques d''applications, ''operator'' partout ailleurs. '
    'Ne s''applique JAMAIS à un avis dont les aspects parlent : ceux-là sont '
    'classés sur ce qu''ils disent, pas sur d''où ils viennent.';

UPDATE dim_source SET default_about = 'app'
 WHERE code IN ('google_play', 'app_store');


-- ===========================================================================
-- VUE — v_reviews_enriched, étendue de l'axe `about`
--
--   PROPRIÉTÉ DE CETTE MIGRATION à partir d'ici (la 007 l'était avant elle).
--   Toute colonne à ajouter à la vue doit désormais l'être ICI.
--
--   L'ORDRE DU `CASE` PORTE LA RÈGLE : ce que l'avis DIT prime toujours sur
--   d'où il VIENT. Le défaut de source n'intervient qu'en dernier recours,
--   quand la couche sémantique n'a rien pu nommer — soit parce qu'elle n'est
--   pas encore passée sur la ligne, soit parce qu'il n'y avait rien à nommer.
--
--   La conséquence est heureuse : la classification S'AMÉLIORE TOUTE SEULE à
--   mesure que le backfill sémantique avance. Aucune reprise à prévoir, aucune
--   colonne à recalculer — la vue lit les aspects du jour.
-- ===========================================================================
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
-- Les deux sous-requêtes sont NON CORRÉLÉES : PostgreSQL les évalue une fois
-- par requête (InitPlan), pas une fois par ligne. Le LATERAL, lui, évite de
-- réécrire trois fois la même intersection dans le CASE ci-dessus.
CROSS JOIN LATERAL (
    SELECT
        (r.neg_aspects || r.pos_aspects)
            && (SELECT array_agg(aspect) FROM dim_aspect WHERE scope = 'app')
                                                AS dit_app,
        (r.neg_aspects || r.pos_aspects)
            && (SELECT array_agg(aspect) FROM dim_aspect WHERE scope = 'operator')
                                                AS dit_operateur
) p;


-- ===========================================================================
-- VUES DE MOTIFS — le même axe, sinon un filtre s'appliquerait à moitié
--
--   L'INVARIANT DU MODULE DE FILTRES (reviews/storage/filters.py) : tout axe
--   exposé par v_reviews_enriched doit l'être aussi par v_review_terms et
--   v_review_aspects. Un axe manquant ne lève AUCUNE erreur — il produit un
--   filtre silencieusement ignoré sur cet écran, donc deux onglets qui
--   affichent des chiffres contradictoires sans que rien ne le signale.
--
--   Ces deux vues restent VOLONTAIREMENT SANS FILTRE sur `about` : l'onglet
--   Motifs doit pouvoir montrer les griefs applicatifs. Le but n'a jamais été
--   de les cacher, il est de ne plus les faire passer pour du mécontentement
--   envers l'opérateur.
-- ===========================================================================
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
    END AS about
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
    END AS about
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


-- --- v_review_aspects — jumelle de la précédente, colonne pour colonne -------
--   Ici `about` se lit d'ailleurs directement sur la ligne : un aspect de
--   scope 'app' appartient forcément à un avis qui parle de l'application. La
--   colonne reste néanmoins celle de l'AVIS et non celle de l'aspect — sans
--   quoi filtrer `about=operator` sur cet onglet ne montrerait plus les griefs
--   de service des avis mixtes, alors qu'ils en font partie.
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
    END AS about
FROM reviews r
CROSS JOIN LATERAL unnest(r.neg_aspects) AS a(aspect)
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
    END AS about
FROM reviews r
CROSS JOIN LATERAL unnest(r.pos_aspects) AS a(aspect)
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


-- ===========================================================================
-- VUE — volume par filiale, désormais séparé lui aussi
--
--   `min_subsidiary_reviews` écarte les filiales à trop peu d'avis clients. Ce
--   décompte DOIT compter comme les taux comptent : c'est l'invariant posé par
--   la migration 007. Si le seuil continuait d'inclure les avis d'application
--   quand les taux les excluent, une filiale franchirait le seuil grâce à des
--   avis qui n'entrent dans aucun de ses taux — et son classement s'appuierait
--   sur un dénominateur bien plus petit qu'annoncé.
--
--   `avis_app` est rendu explicitement, jamais passé sous silence : sans lui,
--   20 107 avis disparaîtraient de tous les écrans sans que rien ne l'indique.
-- ===========================================================================
DROP VIEW IF EXISTS v_subsidiary_volume CASCADE;
CREATE VIEW v_subsidiary_volume AS
SELECT
    sub.subsidiary_id,
    COUNT(v.review_id) FILTER (
        WHERE v.source_kind = 'customer_review' AND v.source_comparable
          AND v.about <> 'app'
    )                                                          AS avis_clients,
    COUNT(v.review_id) FILTER (
        WHERE v.source_kind = 'customer_review' AND v.source_comparable
          AND v.about <> 'operator'
    )                                                          AS avis_app,
    COUNT(v.review_id) FILTER (
        WHERE v.source_kind = 'customer_review' AND NOT v.source_comparable
    )                                                AS avis_hors_comparaison,
    COUNT(v.review_id) FILTER (WHERE v.source_kind = 'press')  AS articles_presse,
    COUNT(v.review_id)                                         AS total
FROM dim_subsidiary sub
LEFT JOIN v_reviews_enriched v ON v.subsidiary_id = sub.subsidiary_id
GROUP BY sub.subsidiary_id;


-- ===========================================================================
-- VUES v_stats_by_* — la même définition d'`avis_clients` partout
--
--   Reprises de la migration 007, plus la séparation. Les laisser sur
--   l'ancienne règle ferait dire DEUX choses au même mot d'un écran à l'autre :
--   c'est l'incohérence que le bloc `_MEASURES` a été créé pour supprimer.
-- ===========================================================================
DROP VIEW IF EXISTS v_stats_by_country CASCADE;
CREATE VIEW v_stats_by_country AS
SELECT
    co.country_id, co.iso2, co.name AS country, co.region,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'app')
                                                         AS avis_clients,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'operator')
                                                         AS avis_app,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND NOT v.source_comparable)      AS avis_hors_comparaison,
    COUNT(*) FILTER (WHERE v.source_kind = 'press')      AS articles_presse,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'app'
                       AND v.sentiment = 'positive')     AS positifs,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'app'
                       AND v.sentiment = 'neutral')      AS neutres,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'app'
                       AND v.sentiment = 'negative')     AS negatifs,
    ROUND((AVG(v.rating) FILTER (
        WHERE v.source_kind = 'customer_review'
          AND v.source_comparable AND v.about <> 'app'))::numeric, 2)
                                                         AS note_moyenne
FROM v_reviews_enriched v
JOIN dim_country co ON co.country_id = v.country_id
GROUP BY co.country_id, co.iso2, co.name, co.region;

DROP VIEW IF EXISTS v_stats_by_operator CASCADE;
CREATE VIEW v_stats_by_operator AS
SELECT
    op.operator_id, op.name AS operator, op.parent_group,
    COUNT(DISTINCT v.country_id)                         AS nb_pays,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'app')
                                                         AS avis_clients,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'operator')
                                                         AS avis_app,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND NOT v.source_comparable)      AS avis_hors_comparaison,
    COUNT(*) FILTER (WHERE v.source_kind = 'press')      AS articles_presse,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'app'
                       AND v.sentiment = 'positive')     AS positifs,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'app'
                       AND v.sentiment = 'neutral')      AS neutres,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'app'
                       AND v.sentiment = 'negative')     AS negatifs,
    ROUND((AVG(v.rating) FILTER (
        WHERE v.source_kind = 'customer_review'
          AND v.source_comparable AND v.about <> 'app'))::numeric, 2)
                                                         AS note_moyenne
FROM v_reviews_enriched v
JOIN dim_operator op ON op.operator_id = v.operator_id
GROUP BY op.operator_id, op.name, op.parent_group;

DROP VIEW IF EXISTS v_stats_by_subsidiary CASCADE;
CREATE VIEW v_stats_by_subsidiary AS
SELECT
    v.subsidiary_id, v.subsidiary,
    v.operator, v.country, v.iso2,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'app')
                                                         AS avis_clients,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'operator')
                                                         AS avis_app,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND NOT v.source_comparable)      AS avis_hors_comparaison,
    COUNT(*) FILTER (WHERE v.source_kind = 'press')      AS articles_presse,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'app'
                       AND v.sentiment = 'positive')     AS positifs,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'app'
                       AND v.sentiment = 'neutral')      AS neutres,
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND v.source_comparable AND v.about <> 'app'
                       AND v.sentiment = 'negative')     AS negatifs,
    ROUND((AVG(v.rating) FILTER (
        WHERE v.source_kind = 'customer_review'
          AND v.source_comparable AND v.about <> 'app'))::numeric, 2)
                                                         AS note_moyenne
FROM v_reviews_enriched v
WHERE v.subsidiary_id IS NOT NULL
GROUP BY v.subsidiary_id, v.subsidiary, v.operator, v.country, v.iso2;


-- ===========================================================================
-- VUE — les deux satisfactions côte à côte, par filiale
--
--   Ce que la séparation rend enfin lisible : « l'application de Vodacom SA va
--   mal, son service va bien » est une phrase que le corpus contenait déjà mais
--   qu'aucun écran ne pouvait former, les deux jugements étant moyennés dans un
--   seul taux.
--
--   C'est aussi le premier tri utile pour l'Agent 1 : une filiale dont l'écart
--   `ecart_points` est très négatif a un problème de LOGICIEL, qui ne se
--   traitera pas en envoyant du monde en agence.
-- ===========================================================================
DROP VIEW IF EXISTS v_app_vs_operator CASCADE;
CREATE VIEW v_app_vs_operator AS
SELECT
    v.subsidiary_id, v.subsidiary, v.operator, v.country, v.iso2,

    COUNT(*) FILTER (WHERE v.about <> 'app')                  AS avis_operateur,
    ROUND((100.0 * COUNT(*) FILTER (WHERE v.about <> 'app'
                                      AND v.sentiment = 'negative')
           / NULLIF(COUNT(*) FILTER (WHERE v.about <> 'app'), 0))::numeric, 1)
                                                              AS neg_operateur,

    COUNT(*) FILTER (WHERE v.about <> 'operator')             AS avis_app,
    ROUND((100.0 * COUNT(*) FILTER (WHERE v.about <> 'operator'
                                      AND v.sentiment = 'negative')
           / NULLIF(COUNT(*) FILTER (WHERE v.about <> 'operator'), 0))::numeric, 1)
                                                              AS neg_app,

    -- Positif : l'application est mieux jugée que le service. Négatif :
    -- l'inverse. C'est cet écart qui dit sur quoi agir.
    ROUND((
        100.0 * COUNT(*) FILTER (WHERE v.about <> 'app' AND v.sentiment = 'negative')
              / NULLIF(COUNT(*) FILTER (WHERE v.about <> 'app'), 0)
      - 100.0 * COUNT(*) FILTER (WHERE v.about <> 'operator' AND v.sentiment = 'negative')
              / NULLIF(COUNT(*) FILTER (WHERE v.about <> 'operator'), 0)
    )::numeric, 1)                                            AS ecart_points
FROM v_reviews_enriched v
WHERE v.source_kind = 'customer_review'
  AND v.source_comparable
  AND v.subsidiary_id IS NOT NULL
GROUP BY v.subsidiary_id, v.subsidiary, v.operator, v.country, v.iso2;


-- ===========================================================================
-- CONTRÔLES POST-MIGRATION (à exécuter à la main, ne modifient rien)
--
--   -- 1. Répartition attendue sur le corpus comparable :
--   --    app ~20 107 (43,2 % neg) · operator ~8 744 (55,4 %) · both ~2 006 (93,0 %)
--   SELECT about, COUNT(*), ROUND(100.0 * COUNT(*)
--            FILTER (WHERE sentiment='negative') / COUNT(*), 1) AS pct_neg
--     FROM v_reviews_enriched
--    WHERE source_kind='customer_review' AND source_comparable
--    GROUP BY 1 ORDER BY 2 DESC;
--
--   -- 2. Part du corpus classée par PRÉSOMPTION plutôt que sur son texte.
--   --    Doit BAISSER à mesure que le backfill sémantique avance. Si elle
--   --    stagne, c'est la couche sémantique qui est à l'arrêt, pas la
--   --    classification.
--   SELECT about_source, COUNT(*) FROM v_reviews_enriched
--    WHERE source_kind='customer_review' GROUP BY 1;
--
--   -- 3. CONTRÔLE DE FUITE — un aspect d'application classé côté opérateur.
--   --    DOIT RENDRE ZÉRO LIGNE. Toute ligne ici est un aspect ajouté à la
--   --    taxonomie Python et jamais déclaré dans dim_aspect : ses avis
--   --    retombent alors dans la satisfaction du service en silence.
--   SELECT DISTINCT a.aspect
--     FROM v_review_aspects a
--     LEFT JOIN dim_aspect d ON d.aspect = a.aspect
--    WHERE d.aspect IS NULL;
--
--   -- 4. Les deux satisfactions, là où elles divergent le plus :
--   SELECT * FROM v_app_vs_operator
--    WHERE avis_operateur >= 30 AND avis_app >= 30
--    ORDER BY ecart_points LIMIT 15;
--
--   -- 5. Filiales sorties du seuil de fiabilité par la séparation (~28) :
--   SELECT COUNT(*) FROM v_subsidiary_volume
--    WHERE avis_clients < 30 AND avis_clients + avis_app >= 30;
-- ===========================================================================
