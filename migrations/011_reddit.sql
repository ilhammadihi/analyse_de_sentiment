-- ===========================================================================
-- 011 — Reddit : la parole spontanée des abonnés
-- ===========================================================================
--
-- POURQUOI CETTE MIGRATION EST OBLIGATOIRE
--   `reviews.source` est un VARCHAR sans contrainte : un avis d'une source
--   inconnue s'insère SANS ERREUR. Mais `reviews.source_id` reste alors NULL,
--   et toutes les vues du modèle dimensionnel joignent `dim_source`. L'avis
--   est donc en base, invisible du dashboard, et rien ne le signale.
--   Ajouter une valeur à SourceEnum sans l'ajouter ici, c'est collecter pour
--   rien — la leçon de la migration 006.
--
-- `kind = 'customer_review'` ET NON 'press'
--   Un fil de forum est de la voix client : première personne, registre
--   familier, récit d'expérience. Le classer 'press' l'aurait sorti des
--   verbatims, des motifs et de la couche sémantique — qui filtrent tous sur
--   kind='customer_review' — c'est-à-dire précisément des trois écrans où un
--   témoignage détaillé vaut quelque chose.
--
-- `comparable = FALSE` — LA DÉCISION IMPORTANTE DE CETTE MIGRATION
--   Deux raisons INDÉPENDANTES, chacune suffisante.
--
--   1. LA COUVERTURE EST TRÈS INÉGALE, et c'est structurel, pas corrigeable.
--      Reddit est massif en Afrique du Sud, au Nigeria et au Kenya, marginal
--      ou absent ailleurs — mesuré à la mise en place : r/southafrica,
--      r/Nigeria et r/Kenya saturent le plafond de 25 fils par requête quand
--      la majorité des subreddits pays du périmètre n'existent pas ou ne
--      produisent aucun fil télécom sur un an.
--
--      Faire entrer Reddit dans un taux comparé reviendrait donc à comparer
--      une filiale mesurée sur des centaines de fils à une filiale mesurée sur
--      zéro. Ce n'est pas un écart de satisfaction, c'est un écart de
--      pénétration d'un réseau social — et rien à l'écran ne l'aurait laissé
--      voir.
--
--   2. LE BIAIS DE RECRUTEMENT est celui d'HelloPeter, pour lequel la
--      migration 007 a créé cet axe : on ouvre un fil quand ça ne marche pas,
--      pas quand tout va bien.
--
--   Ces avis ne sont PAS perdus. Ils restent en base, alimentent les
--   verbatims, les motifs et la couche sémantique, et leur volume est rendu
--   explicitement par `avis_hors_comparaison` — la mesure créée en 007 pour
--   qu'une donnée retirée d'un calcul ne disparaisse jamais en silence.
--
-- `has_rating = FALSE`
--   Un fil de forum n'a pas de note. Le collecteur écrit donc `rating = NULL`,
--   ce qui est cohérent avec les deux sources de presse.
-- ---------------------------------------------------------------------------

INSERT INTO dim_source (code, name, kind, has_rating) VALUES
    ('reddit', 'Reddit (forums pays)', 'customer_review', FALSE)
ON CONFLICT (code) DO NOTHING;

-- Hors comparaison, pour les deux raisons exposées ci-dessus. Le défaut de la
-- colonne est TRUE : sans cette ligne, Reddit entrerait silencieusement dans
-- tous les taux de satisfaction du dashboard.
UPDATE dim_source SET comparable = FALSE WHERE code = 'reddit';


-- Rattachement des lignes déjà insérées, au cas où des avis auraient été
-- collectés avant l'application de cette migration (source_id resté NULL).
UPDATE reviews r
SET    source_id = s.source_id
FROM   dim_source s
WHERE  r.source_id IS NULL
  AND  r.source = s.code;


-- ---------------------------------------------------------------------------
-- CONTRÔLES
--
--   1. Aucun avis orphelin de dimension (doit renvoyer 0 ligne) :
--      SELECT DISTINCT source FROM reviews WHERE source_id IS NULL;
--
--   2. Reddit est bien hors comparaison :
--      SELECT code, kind, comparable FROM dim_source ORDER BY comparable, code;
--      -- attendu : hellopeter ET reddit à FALSE, tous les autres à TRUE
--
--   3. Les taux de satisfaction ne doivent PAS bouger après le premier run
--      Reddit. S'ils bougent, c'est que `comparable` n'a pas été appliqué et
--      que le point 2 ci-dessus dira pourquoi.
-- ---------------------------------------------------------------------------
