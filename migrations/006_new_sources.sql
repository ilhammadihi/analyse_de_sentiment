-- ===========================================================================
-- 006 — Trois nouvelles sources : HelloPeter, GDELT, presse africaine
-- ===========================================================================
--
-- POURQUOI CETTE MIGRATION EST OBLIGATOIRE
--   `reviews.source` est un VARCHAR sans contrainte : un avis d'une source
--   inconnue s'insère SANS ERREUR. Mais `reviews.source_id` reste alors NULL,
--   et toutes les vues du modèle dimensionnel joignent `dim_source`. L'avis
--   est donc en base, invisible du dashboard, et rien ne le signale.
--   Ajouter une valeur à SourceEnum sans l'ajouter ici, c'est collecter pour
--   rien.
--
-- LE CHAMP `kind` N'EST PAS DÉCORATIF
--   Il sépare les AVIS CLIENTS de la PRESSE, et les vues de satisfaction ne
--   calculent que sur 'customer_review'. Deux des trois sources ajoutées ici
--   sont du journalisme : mal les classer ferait entrer des titres d'articles
--   dans le taux de satisfaction des filiales.
-- ---------------------------------------------------------------------------

INSERT INTO dim_source (code, name, kind, has_rating) VALUES
    -- Avis clients notés 1-5, Afrique du Sud. La source la plus volumineuse
    -- du projet : voir HELLOPETER_MAX_PAGES pour le garde-fou de volume.
    ('hellopeter',  'HelloPeter (Afrique du Sud)', 'customer_review', TRUE),
    -- Presse mondiale multilingue. Pas de note : GDELT ne rend qu'un titre,
    -- un pays-source et une langue.
    ('gdelt',       'GDELT (presse mondiale)',     'press',           FALSE),
    -- Presse tech africaine spécialisée (TechCabal, TechCentral, ITWeb
    -- Africa, Jeune Afrique…). Pas de note non plus.
    ('press_feed',  'Presse tech africaine',       'press',           FALSE)
ON CONFLICT (code) DO NOTHING;


-- Rattachement des lignes déjà insérées, au cas où des avis auraient été
-- collectés avant l'application de cette migration (source_id resté NULL).
UPDATE reviews r
SET    source_id = s.source_id
FROM   dim_source s
WHERE  r.source_id IS NULL
  AND  r.source = s.code;


-- ---------------------------------------------------------------------------
-- CONTRÔLE — doit renvoyer 0 ligne.
--   Toute ligne remontée ici est un avis orphelin de dimension, donc absent
--   du dashboard : c'est le symptôme exact que cette migration prévient.
--
--   SELECT DISTINCT source FROM reviews WHERE source_id IS NULL;
-- ---------------------------------------------------------------------------
