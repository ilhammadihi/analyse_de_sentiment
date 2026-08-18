-- ===========================================================================
-- 017 — campagnes : nom, problème identifié, et identifiant du message envoyé
-- ===========================================================================
--
-- POURQUOI UN NOM EN COLONNE ET NON DANS LE PAYLOAD
--   Une campagne se DÉSIGNE par son nom dans une conversation d'équipe
--   (« où en est Data Boost ? »), pas par son numéro. Le payload sert à
--   conserver ce qui a fondé la décision ; le nom, lui, est une clé d'usage : il
--   s'affiche dans la liste, se cherche, et doit rester lisible sans déplier un
--   objet JSON.
--
-- POURQUOI LE PROBLÈME IDENTIFIÉ EST STOCKÉ SÉPARÉMENT DU MESSAGE
--   Ce sont deux textes qui ne s'adressent pas aux mêmes personnes. Le
--   « problème identifié » est INTERNE : il dit à l'équipe pourquoi cette
--   campagne existe, en reprenant les mesures. Le message est EXTERNE : il
--   s'adresse au client et ne doit surtout pas lui exposer nos taux de
--   satisfaction. Les fondre en un seul champ finirait par faire fuiter l'un
--   dans l'autre.
--
-- L'IDENTIFIANT DE MESSAGE TELEGRAM — UNE LEÇON DÉJÀ PAYÉE DEUX FOIS
--   La migration 015 a ajouté `alerts.telegram_message_id` après que quatre
--   alertes fausses soient restées sous les yeux de l'équipe, faute d'avoir
--   gardé l'identifiant renvoyé par l'API. La même faute a été reproduite ici :
--   la campagne n°1, envoyée puis rejetée le 13 août 2026, n'a pas pu être
--   retirée du groupe.
--
--   `send_text` renvoyait un booléen et jetait la réponse de l'API. Retirer un
--   message exige `deleteMessage(chat_id, message_id)` : sans cet identifiant,
--   une proposition erronée reste affichée pour toujours. Telegram n'autorise le
--   retrait que pendant 48 heures — d'où l'intérêt de le stocker AVANT d'en
--   avoir besoin.

ALTER TABLE campaigns
    --: Nom commercial de la campagne (« Data Boost »). Rédigé par le modèle
    --: quand il est disponible, sinon composé depuis l'objectif et le motif.
    --: Jamais NULL : une campagne sans nom ne se cite pas en réunion.
    ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT '',

    --: Le problème mesuré qui justifie la campagne, en une ou deux phrases.
    --: CALCULÉ, jamais rédigé librement : il ne contient que des chiffres issus
    --: des agrégats, et c'est ce qui permet de le relire six semaines plus tard
    --: pour juger si la campagne visait juste.
    ADD COLUMN IF NOT EXISTS problem TEXT NOT NULL DEFAULT '',

    --: Identifiant du message Telegram portant la proposition. NULL quand
    --: aucun canal n'était configuré, ou quand l'API n'a pas rendu
    --: d'identifiant lisible — perdre l'identifiant ne doit jamais faire
    --: échouer un envoi qui a abouti.
    ADD COLUMN IF NOT EXISTS telegram_message_id BIGINT;

--: Recherche par nom, pour la commande de suivi. `lower()` parce que personne
--: ne retape la casse exacte d'un nom de campagne.
CREATE INDEX IF NOT EXISTS idx_campaigns_name ON campaigns (lower(name));
