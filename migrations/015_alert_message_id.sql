-- ===========================================================================
-- 015 — alerts.telegram_message_id : pouvoir retirer une alerte partie à tort
-- ===========================================================================
--
-- LE PROBLÈME, RENCONTRÉ EN CONDITIONS RÉELLES
--   Quatre alertes de pic sont parties sur Telegram alors qu'elles reposaient
--   sur des avis mal attribués — 316 avis d'applications de cryptomonnaie
--   rattachés à BTC Botswana, 202 avis indonésiens rattachés à Ooredoo
--   Algérie. Les lignes fautives ont été supprimées de la base et les alertes
--   avec elles, mais les MESSAGES, eux, sont restés dans le groupe.
--
--   Impossible de les retirer : `TelegramNotifier` postait le message et ne
--   gardait que le code HTTP. L'identifiant rendu par l'API — le seul moyen de
--   demander une suppression — était jeté à la ligne suivante.
--
-- CE QUE CETTE COLONNE PERMET
--   Retirer un message précis, et seulement celui-là. Sans identifiant stocké,
--   la seule alternative aurait été de deviner des identifiants voisins, ce
--   qui reviendrait à supprimer les messages d'autres personnes dans le
--   groupe. Une donnée manquante vaut mieux qu'une suppression au jugé.
--
-- POURQUOI UNE COLONNE ET NON UNE CLÉ DANS `notified`
--   `notified` est une liste de noms de canaux, lue telle quelle. Y glisser un
--   objet structuré obligerait chaque lecteur présent et futur à traiter deux
--   formes. Une colonne dédiée se lit, s'indexe et se laisse vide sans
--   ambiguïté quand le canal n'est pas Telegram.
--
-- NULLABLE, ET C'EST NORMAL
--   Les alertes antérieures à cette migration n'en ont pas — elles ne sont
--   plus retirables, c'est la conséquence assumée du défaut d'origine. Une
--   alerte filtrée par gravité ou par type n'est jamais partie et n'en aura
--   pas non plus.

ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS telegram_message_id BIGINT;

--: Retrouver l'alerte à retirer se fait par son identifiant, pas par le
--: message ; l'index sert au cas inverse — savoir si un message donné
--: correspond encore à une alerte vivante.
CREATE INDEX IF NOT EXISTS idx_alerts_telegram_message
    ON alerts (telegram_message_id)
    WHERE telegram_message_id IS NOT NULL;
