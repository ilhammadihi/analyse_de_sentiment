# Guide utilisateur — Dashboard « Statistiques »

Ce guide s'adresse aux managers et responsables qui consultent le dashboard
de satisfaction client, dans FaceMonitoring (onglet **Statistiques**). Il ne
couvre pas l'installation ni le code — voir [README.md](../README.md) pour la
partie technique.

## Ce que mesure ce dashboard

Chaque écran s'appuie sur des avis clients réellement collectés (boutiques
d'applications, agences physiques via Google Maps, plateformes d'avis,
presse, réseaux sociaux) sur les filiales télécoms suivies à travers le
continent. Aucun chiffre n'est estimé par un modèle : les moyennes, volumes
et tendances sont calculés directement sur les avis en base.

## La barre de filtres — un seul périmètre pour tout le dashboard

Au-dessus des onglets **Actualité**, **Comparer** et **Données**, une barre
de filtres unique définit la tranche de données observée : elle s'applique
à tous les graphiques de ces trois onglets à la fois, pour que deux écrans
consultés à la suite parlent toujours du même périmètre.

- **Période** : 7 jours, 30 jours, 90 jours, 12 mois, ou tout l'historique.
- **Pays / Région / Opérateur / Filiale** : peuvent se combiner — par
  exemple un opérateur + un pays isolent une seule filiale.
- **Seuil de fiabilité** (nombre minimal d'avis) : « Toutes » inclut les
  filiales peu documentées (bruit statistique compris) ; **≥ 10 avis** est
  la coupe recommandée (81 filiales sur 96) ; **≥ 30 avis** ne garde que les
  chiffres les plus sûrs (41 filiales).

Les onglets **Marché** et **Qualité** n'affichent pas cette barre : ils
répondent à une question qui n'a pas de « période » (le marché est annuel et
national, la qualité des données porte sur aujourd'hui, pas sur une plage
passée).

## Les cinq onglets

### 1. Actualité — que dit-on de nous en ce moment

L'écran d'entrée. Quatre indicateurs (volume d'avis, part de négatifs, note
moyenne, évolution sur 24 h), leur tendance dans le temps, la répartition
par source, et deux blocs à lire en premier :

- **À retenir** — les faits marquants de la période, composés à partir des
  mesures déjà calculées (aucun texte inventé).
- **Filiales à surveiller** — celles dont le signal a bougé le plus fort sur
  les 7 derniers jours, fenêtre fixe et indépendante du filtre de période
  choisi plus haut (pour ne jamais faire remonter un pic vieux de plusieurs
  mois).

Volontairement sans détail de collecte (sources, doublons, runs) — ce
niveau-là est dans **Données** et **Qualité**.

### 2. Comparer — cette entité fait-elle mieux ou moins bien qu'une autre

Trois façons de répondre à la même question, sélectionnables en haut de
l'écran :

- **Courbes** — deux à cinq entités choisies, suivies dans le temps.
- **Carte** — la même comparaison, lue géographiquement.
- **Matrice** — le croisement opérateur × pays (une case = une filiale).

Changer de vue ne change jamais le périmètre observé (toujours celui de la
barre de filtres), seulement la façon de le regarder.

### 3. Marché — le fait mesurable, à côté de l'opinion

Le contexte de marché **par opérateur** : abonnés déclarés par les
régulateurs nationaux (NCC Nigeria, ANRT Maroc, ARCEP Bénin, NCA Ghana) et
par des rapports d'entreprise ou de presse sourcés pour d'autres pays. Placé
**après** Comparer à dessein : le marché sert à vérifier si un volume
d'abonnés explique un niveau de satisfaction, jamais à le présumer.

Seules les filiales dont la dernière mesure date de **l'année en cours**
sont montrées — un chiffre 2024 n'apparaît jamais à côté d'un chiffre 2026.
La couverture est donc partielle par construction : c'est le prix d'un écran
qui ne mélange jamais deux fraîcheurs de données. Chaque ligne indique sa
source et sa période exacte (mensuelle, trimestrielle ou annuelle selon le
régulateur).

Pas de barre de filtres ici : un seul sélecteur, la recherche par pays.

### 4. Données — d'où viennent mes avis, combien, et est-ce que ça bouge

L'état des lieux des sources : volume par source, évolution, composition.
Répond à une question de photographie, pas de diagnostic — pour le
diagnostic, voir **Qualité**.

### 5. Qualité — puis-je me fier à ce que je vois

Deux questions, toutes deux actionnables par un manager :

1. **Quels pays suivis n'ont produit aucun avis récent ?** — un signal pour
   relancer une agence ou vérifier qu'une boutique n'a pas fermé.
2. **Quelles sources externes ont été vérifiées, et pourraient être
   intégrées ?** — uniquement des candidates dont le contenu a été confirmé
   par une sonde réelle (jamais une source seulement citée sans preuve).

Placé en dernier délibérément : c'est l'écran qu'on consulte **après** avoir
vu quelque chose d'étonnant ailleurs, pas en premier — pour ne pas faire
douter de chiffres qui, pour l'écrasante majorité des filiales, sont
parfaitement fiables.

## Les alertes

Les pics de mécontentement significatifs ne sont pas affichés dans un onglet
dédié du dashboard : ils sont poussés directement sur le canal **Telegram**
de veille dès leur détection, avec le contexte nécessaire pour agir. C'est
un choix délibéré — dupliquer ce même signal dans un écran consulté après
coup n'apporterait rien de plus.

## Ce que ce dashboard ne montre pas, et pourquoi

- **La santé technique de la collecte** (collecteurs en échec, doublons,
  durée des runs) — c'est un écran d'exploitation utile à qui maintient la
  plateforme, pas à qui la consulte ; un message comme « 96 % de doublons »
  y décrirait un mécanisme interne qui fonctionne comme prévu, mais serait
  mal interprété comme un défaut des chiffres affichés.
- **Un chiffre de marché par pays entier** (Banque Mondiale / UIT) — retiré
  le 24 août 2026 : cette source s'arrête à 2023-2024 et mélangeait des
  fraîcheurs différentes avec les données plus récentes par opérateur.
