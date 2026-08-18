-- ===========================================================================
-- 024 — Agent 3 : notifier UNE FOIS une source nouvellement vérifiée
-- ===========================================================================
--
-- LE BESOIN, TEL QUE FORMULÉ PAR LE MÉTIER
--   « Envoie un message COURT uniquement lorsqu'une nouvelle source
--   intéressante est trouvée. » — et rien de plus dans ce message : ni score,
--   ni code HTTP, ni jargon de diagnostic. Une phrase, un volume estimé, une
--   proposition.
--
-- POURQUOI UNE COLONNE DE PLUS PLUTÔT QUE `should_report()`
--   `agent_reports` porte déjà la règle de non-répétition de l'Agent 1, mais
--   elle raisonne sur un SCORE qui peut remonter (« aggravation », donc
--   reparler). Une source candidate n'a pas cette dynamique : une fois VÉRIFIÉE
--   et proposée, il n'y a rien de plus à dire tant que personne n'a statué. Le
--   signal recherché est binaire — « déjà annoncée » ou non — et une colonne
--   sur la ligne elle-même le porte plus simplement qu'un journal séparé.
--
-- POURQUOI `avis_estimes` EXISTE, ET POURQUOI IL RESTE UNE ESTIMATION
--   « 86 avis publics disponibles » est ce qui rend une proposition actionnable
--   pour une équipe qui doit arbitrer si un connecteur vaut la peine d'être
--   écrit. Ce nombre est lu au mieux sur la page sondée (un motif
--   « 86 avis » / « 86 reviews » dans le texte), jamais recalculé par une
--   collecte : l'Agent 3 ne devient pas un scraper pour l'obtenir. Nullable —
--   une page qui ne l'affiche pas ne doit pas produire un zéro inventé.
--
-- NON DESTRUCTIF — deux colonnes nullables sur une table déjà existante.
-- ---------------------------------------------------------------------------

ALTER TABLE source_candidates
    ADD COLUMN IF NOT EXISTS avis_estimes INTEGER,
    ADD COLUMN IF NOT EXISTS notified_at  TIMESTAMPTZ;

COMMENT ON COLUMN source_candidates.avis_estimes IS
    'Volume d''avis lu au mieux sur la page sondée (motif "N avis"/"N '
    'reviews"). Estimation, jamais une collecte : NULL si illisible.';
COMMENT ON COLUMN source_candidates.notified_at IS
    'Date d''envoi du message Telegram court annonçant cette source. NULL '
    'tant que rien n''a été envoyé. Empêche de ré-annoncer une source déjà '
    'portée à la connaissance de l''équipe.';

--: Requête chaude de la notification : les candidates VÉRIFIÉES jamais
--: annoncées.
CREATE INDEX IF NOT EXISTS idx_source_candidates_a_notifier
    ON source_candidates (status)
    WHERE notified_at IS NULL;


-- ===========================================================================
-- CONTRÔLE POST-MIGRATION (à exécuter à la main, ne modifie rien)
--
--   SELECT source_name, subsidiary_id, avis_estimes, notified_at
--     FROM source_candidates
--    WHERE status = 'VERIFIED' AND notified_at IS NULL;
-- ===========================================================================
