-- ===========================================================================
-- 009 — De « l'agence » à « la sous-cible » : généralisation de place_id
-- ===========================================================================
--
-- LE PROBLÈME QUE CETTE MIGRATION DÉBLOQUE
--   Le repère de collecte incrémentale est calculé par (entreprise, source) :
--
--       SELECT company, source, MAX(created_at) ... GROUP BY company, source
--
--   Tant qu'une filiale n'avait qu'UNE cible par source — une fiche Google
--   Maps, une application — c'était correct. Ça ne l'est plus.
--
--   Depuis que Google Maps visite plusieurs AGENCES et que les boutiques
--   d'applications suivent plusieurs APPS par filiale, ce repère unique est
--   destructeur. Exemple concret :
--
--       Vodacom South Africa, google_maps, dernier avis connu : 2026-08-01
--       -> repère = 2026-07-30 (marge de 2 jours)
--       Nouvelle agence découverte, avis de 2026-04 à 2026-07-28
--       -> TOUS écartés comme « déjà en base », alors qu'aucun n'y est.
--
--   La densification se sabordait donc elle-même : plus on découvre de
--   sous-cibles, plus on en jette le contenu. Sans erreur, sans trace.
--
-- POURQUOI UN RENOMMAGE PLUTÔT QU'UNE COLONNE DE PLUS
--   `place_id` a été introduit la veille pour Google Maps. Ajouter à côté un
--   `app_id` pour les boutiques, puis un troisième identifiant pour la source
--   suivante, multiplierait les colonnes disant toutes la même chose : « de
--   quelle sous-cible cet avis provient-il ». Une agence et une application
--   sont la MÊME notion vue de deux sources.
--
--   Le renommage coûte peu aujourd'hui (une quarantaine de lignes portent un
--   `place_id`) et beaucoup plus tard.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'reviews' AND column_name = 'place_id') THEN
        ALTER TABLE reviews RENAME COLUMN place_id   TO target_id;
        ALTER TABLE reviews RENAME COLUMN place_name TO target_name;
    END IF;
END $$;

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS target_id   TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS target_name TEXT;

COMMENT ON COLUMN reviews.target_id IS
    'Identifiant de la SOUS-CIBLE d''où vient l''avis : fiche Google Maps '
    '(0xHEX:0xHEX), identifiant d''application App Store, nom de paquet Play '
    'Store. NULL pour les sources sans sous-cible (presse). Entre dans le '
    'repère de collecte incrémentale ET dans le checksum de déduplication.';
COMMENT ON COLUMN reviews.target_name IS
    'Nom lisible de la sous-cible : « Vodacom Shop Rosebank Mall », '
    '« MTN MoMo SA ». Sert au contrôle d''attribution.';

DROP INDEX IF EXISTS idx_reviews_place;
CREATE INDEX IF NOT EXISTS idx_reviews_target ON reviews(target_id)
    WHERE target_id IS NOT NULL;

-- Le repère incrémental interroge (company, source, target_id) : cet index
-- sert exactement ce GROUP BY, sur une table qui grossit vite.
CREATE INDEX IF NOT EXISTS idx_reviews_watermark
    ON reviews(company, source, target_id, created_at DESC);


-- ---------------------------------------------------------------------------
-- VUE — couverture en sous-cibles, tous types confondus
--   Remplace v_place_coverage, qui ne parlait que d'agences. La colonne
--   `source` distingue désormais ce qu'on couvre : des agences pour Google
--   Maps, des applications pour les boutiques.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_place_coverage CASCADE;
DROP VIEW IF EXISTS v_target_coverage CASCADE;
CREATE VIEW v_target_coverage AS
SELECT
    sub.subsidiary_id,
    sub.name                                   AS subsidiary,
    op.name                                    AS operator,
    co.name                                    AS country,
    co.iso2,
    r.source,
    COUNT(DISTINCT r.target_id)                AS cibles_couvertes,
    COUNT(*)                                   AS avis,
    ROUND(COUNT(*)::numeric
          / NULLIF(COUNT(DISTINCT r.target_id), 0), 1)
                                               AS avis_par_cible,
    MAX(r.collected_at)                        AS derniere_collecte
FROM reviews r
JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
JOIN dim_country    co  ON co.country_id     = sub.country_id
WHERE r.target_id IS NOT NULL
GROUP BY sub.subsidiary_id, sub.name, op.name, co.name, co.iso2, r.source;


-- ---------------------------------------------------------------------------
-- CONTRÔLE — sous-cible dont le nom ne contient pas celui de l'opérateur.
--   Doit rester vide. Toute ligne est un avis attribué à la mauvaise enseigne
--   (agence d'un revendeur tiers, application d'un autre éditeur).
--
--   SELECT DISTINCT op.name AS operateur, r.source, r.target_name
--   FROM reviews r
--   JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
--   JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
--   WHERE r.target_name IS NOT NULL
--     AND position(lower(op.name) in lower(r.target_name)) = 0;
-- ---------------------------------------------------------------------------
