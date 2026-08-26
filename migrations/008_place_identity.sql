-- ===========================================================================
-- 008 — Identité du point de vente (Google Maps)
-- ===========================================================================
--
-- LE PROBLÈME
--   Le collecteur Google Maps rattachait tous ses avis à `company =
--   subsidiary_name`, sans jamais dire DE QUELLE AGENCE ils venaient. Trois
--   conséquences, toutes vérifiées :
--
--   1. Impossible de savoir combien d'agences distinctes sont réellement
--      couvertes. Mesuré à la main : « Agence MTN Lagos » et « Agence MTN
--      Nigeria » renvoient LA MÊME fiche en premier résultat — la
--      densification par ville était donc en partie illusoire, sans qu'aucune
--      donnée ne permette de s'en apercevoir.
--
--   2. Impossible de détecter une mauvaise attribution. « Agence Vodacom
--      Johannesburg » remonte « Cellucity - Bedfordview » en premier résultat,
--      un revendeur tiers dont les avis étaient enregistrés comme des avis
--      Vodacom.
--
--   3. Aucune granularité par agence dans le dashboard, alors que c'est
--      exactement ce qu'un directeur régional veut voir.
--
-- CE QUE CETTE MIGRATION AJOUTE
--   Deux colonnes nullables. Nullables parce que six sources sur huit n'ont
--   aucune notion de lieu : un avis d'application n'a pas d'agence, et exiger
--   la colonne les rendrait ininsérables.
-- ---------------------------------------------------------------------------

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS place_id   TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS place_name TEXT;

COMMENT ON COLUMN reviews.place_id IS
    'Identifiant Google du point de vente (0xHEX:0xHEX extrait de l''URL de la '
    'fiche). NULL pour toute source sans notion de lieu.';
COMMENT ON COLUMN reviews.place_name IS
    'Nom du point de vente tel que Google l''affiche (« Vodacom Shop Rosebank '
    'Mall »). Sert au contrôle : un nom qui ne contient pas celui de '
    'l''opérateur signale une mauvaise attribution.';

-- Recherche des avis d'une agence, et décompte des agences couvertes.
CREATE INDEX IF NOT EXISTS idx_reviews_place ON reviews(place_id)
    WHERE place_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- VUE — couverture réelle en agences, par filiale
--   C'est l'instrument qui manquait : sans lui, on ne peut pas répondre à
--   « combien d'agences suit-on vraiment ? », ni voir que deux requêtes
--   différentes tapent sur la même fiche.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_place_coverage CASCADE;
CREATE VIEW v_place_coverage AS
SELECT
    sub.subsidiary_id,
    sub.name                                   AS subsidiary,
    op.name                                    AS operator,
    co.name                                    AS country,
    co.iso2,
    COUNT(DISTINCT r.place_id)                 AS agences_couvertes,
    COUNT(*)                                   AS avis,
    ROUND(COUNT(*)::numeric
          / NULLIF(COUNT(DISTINCT r.place_id), 0), 1)
                                               AS avis_par_agence,
    MAX(r.collected_at)                        AS derniere_collecte
FROM reviews r
JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
JOIN dim_country    co  ON co.country_id     = sub.country_id
WHERE r.place_id IS NOT NULL
GROUP BY sub.subsidiary_id, sub.name, op.name, co.name, co.iso2;


-- ---------------------------------------------------------------------------
-- CONTRÔLE — agences dont le nom ne contient pas celui de l'opérateur.
--   Doit rester vide une fois le filtre du collecteur en place. Toute ligne ici
--   est un avis attribué à la mauvaise enseigne.
--
--   SELECT DISTINCT op.name AS operateur, r.place_name
--   FROM reviews r
--   JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
--   JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
--   WHERE r.place_name IS NOT NULL
--     AND position(lower(op.name) in lower(r.place_name)) = 0;
-- ---------------------------------------------------------------------------
