"""
Interface en ligne de commande unifiée.

    python -m reviews init-db        # crée/vérifie le schéma
    python -m reviews run [--dry-run] # exécute le pipeline une fois
    python -m reviews serve           # lance l'API FastAPI
    python -m reviews schedule        # lance le pipeline en boucle (APScheduler)
    python -m reviews agent [--dry-run]  # un passage de l'agent de veille
    python -m reviews chat --ask "..."   # une question, réponse au terminal
    python -m reviews chat --listen      # écoute Telegram et répond
    python -m reviews retract-alert ID   # retire une alerte partie à tort
"""

import argparse
import logging
import os
import sys

from reviews.log_setup import setup_logging


def _cmd_init_db() -> int:
    from reviews.storage.db import get_database
    logging.getLogger("cli").info("Initialisation de la base de données…")
    get_database().apply_schema()
    print("✓ Base de données initialisée")
    return 0


def _cmd_run(dry_run: bool) -> int:
    from reviews.storage.db import get_database
    from reviews.pipeline.runner import build_pipeline
    from reviews.pipeline.reporting import print_summary

    if not dry_run:
        get_database().apply_schema()  # idempotent

    pipeline = build_pipeline()
    try:
        run = pipeline.run(dry_run=dry_run)
    finally:
        get_database().close_all()
    print_summary(run)
    return 0 if run.status == "success" else 1


def _cmd_serve(host: str | None, port: int | None) -> int:
    import uvicorn
    from reviews.config import get_settings
    settings = get_settings()
    uvicorn.run(
        "reviews.api.main:app",
        host=host or settings.api.host,
        port=port or settings.api.port,
    )
    return 0


def _cmd_schedule() -> int:
    from reviews.scheduling import run_scheduler
    run_scheduler()
    return 0


def _cmd_retract_alert(alert_ids: list[int]) -> int:
    """Retire une ou plusieurs alertes : message Telegram effacé, ligne supprimée.

    POURQUOI CETTE COMMANDE EXISTE. Quatre alertes de pic sont parties dans le
    groupe alors qu'elles reposaient sur des avis mal attribués. Les lignes ont
    pu être supprimées de la base ; les messages, eux, sont restés — le code
    n'avait jamais gardé leur identifiant. Une alerte fausse qu'on ne peut pas
    retirer reste sous les yeux de l'équipe, et c'est elle qu'on retiendra.

    Le retrait Telegram et la suppression en base sont INDÉPENDANTS : si le
    message est trop ancien (48 h, limite de l'API), la ligne est quand même
    supprimée et le refus journalisé. L'inverse serait pire — garder en base
    une alerte reconnue fausse parce qu'un message n'a pas pu être effacé.
    """
    from reviews.alerting.notifiers import TelegramNotifier
    from reviews.config import get_settings
    from reviews.storage.db import get_database

    settings = get_settings()
    db = get_database()
    cfg = settings.alerting
    notifier = (
        TelegramNotifier(cfg)
        if cfg.telegram_bot_token and cfg.telegram_chat_id
        else None
    )

    try:
        with db.cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT alert_id, company, title, telegram_message_id "
                "FROM alerts WHERE alert_id = ANY(%s)",
                (list(alert_ids),),
            )
            lignes = [dict(r) for r in cur.fetchall()]

        introuvables = set(alert_ids) - {l["alert_id"] for l in lignes}
        for i in sorted(introuvables):
            print(f"  · alerte {i} introuvable")

        for l in lignes:
            mid = l["telegram_message_id"]
            if mid is None:
                etat = "aucun identifiant Telegram (alerte antérieure au suivi)"
            elif notifier is None:
                etat = "Telegram non configuré"
            else:
                etat = "message retiré" if notifier.delete_message(mid) else "retrait refusé"
            print(f"  · {l['alert_id']} — {l['company']} : {etat}")

        with db.cursor() as cur:
            cur.execute("DELETE FROM alerts WHERE alert_id = ANY(%s)", (list(alert_ids),))
        print(f"✓ {len(lignes)} alerte(s) supprimée(s) de la base")
    finally:
        db.close_all()
    return 0


def _cmd_market_data(limit_countries: int | None) -> int:
    """Collecte les indicateurs de marché (Banque Mondiale / UIT).

    Les pays viennent de `dim_country` : on ne collecte que ce que le modèle
    dimensionnel connaît, sinon on remplirait la base d'économies qu'aucun
    écran ne montre.
    """
    from reviews.collectors.market_data import MarketDataCollector
    from reviews.storage.db import get_database
    from reviews.storage.market_repository import MarketRepository

    db = get_database()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT iso3, country_id FROM dim_country "
                "WHERE iso3 IS NOT NULL ORDER BY name"
            )
            pays = {r[0]: r[1] for r in cur.fetchall()}
        if limit_countries:
            pays = dict(list(pays.items())[:limit_countries])

        print(f"Collecte des indicateurs de marché sur {len(pays)} pays…")
        lignes, erreurs = MarketDataCollector().collect(pays)
        ecrites = MarketRepository(db).upsert(lignes)

        print(f"✓ {ecrites} mesure(s) enregistrée(s)")
        if erreurs:
            # Les erreurs sont RÉSUMÉES, pas listées : 342 appels produisent
            # parfois des dizaines d'échecs identiques, et un mur de lignes
            # cacherait celle qui compte.
            print(f"⚠ {len(erreurs)} appel(s) en échec — premiers :")
            for e in erreurs[:5]:
                print(f"   {e}")
    finally:
        db.close_all()
    return 0


def _cmd_operator_regulateur(nom: str, collecteur_cls) -> int:
    """Collecte d'UN régulateur national par opérateur, à la main.

    Partagée par `ncc-nigeria`, `anrt-maroc` et `arcep-benin` : les trois
    régulateurs suivent exactement le même contrat
    (`.collect() -> (lignes, erreurs)` puis `OperatorMarketRepository.upsert`)
    — voir `_safe_operator_regulateur` dans `scheduling.py` pour le même
    regroupement côté planificateur.
    """
    from reviews.storage.db import get_database
    from reviews.storage.operator_market_repository import OperatorMarketRepository

    db = get_database()
    try:
        print(f"Collecte {nom} (abonnés par opérateur)…")
        lignes, erreurs = collecteur_cls().collect()
        ecrites = OperatorMarketRepository(db).upsert(lignes)

        print(f"✓ {ecrites} mesure(s) enregistrée(s)")
        if erreurs:
            print(f"⚠ {len(erreurs)} erreur(s) :")
            for e in erreurs[:5]:
                print(f"   {e}")
    finally:
        db.close_all()
    return 0


def _cmd_agent(dry_run: bool) -> int:
    """Un passage de l'agent de veille, à la main.

    `--dry-run` affiche ce que l'agent AURAIT dit sans appeler le modèle, sans
    rien envoyer et sans rien journaliser. C'est le mode à utiliser pour régler
    les seuils d'arbitrage : le faire en conditions réelles consommerait du
    quota et réveillerait le groupe Telegram à chaque essai.
    """
    from reviews.agents.insight_agent import build_agent
    from reviews.config import get_settings
    from reviews.storage.db import get_database

    db = get_database()
    try:
        passage = build_agent(db, get_settings()).run(dry_run=dry_run)
    finally:
        db.close_all()

    print(f"\n{'— PASSAGE À BLANC —' if dry_run else '— PASSAGE RÉEL —'}")
    print(f"{passage.resume()}\n")

    for i, s in enumerate(passage.signales, 1):
        print(f"{i}. {s['entite']}  (score {s['score']} · {s['raison']})")
        for ligne in s["texte"].splitlines():
            print(f"   {ligne}")
        print()

    if passage.tus:
        print("Volontairement tus :")
        for t in passage.tus:
            print(f"  · {t['entite']} — {t['raison']}")
        print()

    # Les écartés ne sont montrés qu'en mode verbeux : ils sont nombreux et
    # leur intérêt est de comprendre POURQUOI un seuil coupe, pas de les lire
    # tous les jours.
    ecartes = [c for c in passage.candidats if c.ecarte_parce_que]
    if ecartes and logging.getLogger().isEnabledFor(logging.DEBUG):
        print("Écartés par l'arbitrage :")
        for c in ecartes[:15]:
            print(f"  · {c.label} — {c.ecarte_parce_que}")
        print()

    if passage.raison_silence:
        print(f"Silence : {passage.raison_silence}")
    elif not dry_run:
        print("Briefing envoyé." if passage.envoye else "Briefing NON envoyé (canal absent).")
    return 0


def _cmd_orphelins(appliquer: bool, inclure_probables: bool) -> int:
    """Réattribution des avis orphelins — analyse, puis application explicite.

    L'ANALYSE EST LE DÉFAUT, l'écriture demande `--appliquer`. Cette commande
    modifie `reviews`, la seule table que tout le reste de l'Agent 3 s'interdit
    de toucher : elle ne doit jamais s'exécuter par inadvertance.

    `--inclure-probables` étend l'écriture aux correspondances obtenues après
    repli d'accents. Séparé de `--appliquer` parce que ce n'est pas le même
    engagement : l'une rejoue une égalité stricte, l'autre valide une règle de
    normalisation.
    """
    from reviews.agents.quality.orphelins import ResolveurOrphelins
    from reviews.storage.db import get_database
    from reviews.storage.quality_repository import QualityRepository

    db = get_database()
    try:
        depot = QualityRepository(db)
        resolveur = ResolveurOrphelins(db)
        propositions, rapport = resolveur.analyser()
        depot.enregistrer_propositions([p.as_dict() for p in propositions])

        if appliquer:
            rapport.appliques = resolveur.appliquer(
                propositions, inclure_haute_confiance=inclure_probables
            )
    finally:
        db.close_all()

    print(f"\n{'— APPLICATION —' if appliquer else '— ANALYSE SEULE —'}")
    print(f"{rapport.resume()}\n")

    print("Par méthode :")
    for methode, n in sorted(rapport.par_methode.items(), key=lambda x: -x[1]):
        print(f"  {n:>5}  {methode}")

    if not appliquer and (rapport.auto_safe or rapport.haute_confiance):
        print(
            f"\nRien n'a été écrit. Pour appliquer les {rapport.auto_safe} "
            "correspondance(s) strictement déterministe(s) :"
        )
        print("  python -m reviews orphelins --appliquer")
        if rapport.haute_confiance:
            print(
                f"Pour inclure aussi les {rapport.haute_confiance} "
                "correspondance(s) obtenue(s) après normalisation :"
            )
            print("  python -m reviews orphelins --appliquer --inclure-probables")
    return 0


def _cmd_quality(dry_run: bool, top: int) -> int:
    """Un passage de l'Agent 3 — gardien de la qualité des données.

    `--dry-run` analyse tout — couverture, mapping, diagnostic, contrôles,
    score — SANS rien écrire en base, sans appeler le modèle, sans sonder les
    URL candidates et sans notifier. C'est le mode de mise au point : un
    passage réel écrit des instantanés que les Agents 1 et 2 vont lire, et
    réveille le groupe Telegram.
    """
    from reviews.agents.quality.guardian import build_quality_agent
    from reviews.config import get_settings
    from reviews.storage.db import get_database

    db = get_database()
    try:
        passage = build_quality_agent(db, get_settings()).run(dry_run=dry_run)
    finally:
        db.close_all()

    print(f"\n{'— PASSAGE À BLANC —' if dry_run else '— PASSAGE RÉEL —'}")
    print(f"{passage.resume()}\n")

    if passage.diagnostics:
        print("Diagnostic des filiales :")
        # Trié par effectif décroissant : on veut voir d'abord ce qui domine le
        # périmètre, pas l'ordre alphabétique des cas.
        for cas, n in sorted(passage.diagnostics.items(), key=lambda x: -x[1]):
            print(f"  {n:>4}  {cas}")
        print()

    if passage.signales:
        print("Filiales sous le seuil de confiance :")
        for i, s in enumerate(passage.signales[:top], 1):
            print(f"{i}. {s['entite']}  ({s['score']} % · {s['statut']} · {s['raison']})")
            for ligne in s["texte"].splitlines():
                print(f"   {ligne}")
            print()

    if passage.tus:
        print("Volontairement tus :")
        for t in passage.tus:
            print(f"  · {t['entite']} — {t['raison']}")
        print()

    print(
        f"Constats : {passage.constats} (dont {passage.valides} instruits par le "
        f"modèle) · candidates : {passage.candidates} "
        f"({passage.sources_annoncees} nouvelle(s) annoncée(s)) · affirmations : "
        f"{passage.affirmations} dont {passage.non_corrobores} non corroborée(s)"
    )

    # Les erreurs de sous-étape sont affichées mais ne font pas échouer la
    # commande : le passage a produit ce qu'il pouvait, et masquer le reste
    # serait pire. Même politique que les jobs du planificateur.
    for erreur in passage.erreurs:
        print(f"  ⚠ {erreur}")

    if passage.raison_silence:
        print(f"Silence : {passage.raison_silence}")
    elif not dry_run:
        print("Alerte envoyée." if passage.envoye else "Alerte NON envoyée (canal absent).")
    return 0


def _cmd_chat(question: str | None, listen: bool) -> int:
    """L'agent conversationnel : une question au terminal, ou l'écoute Telegram.

    LES DEUX MODES SONT SÉPARÉS PAR UN DRAPEAU EXPLICITE, et c'est délibéré.
    `--ask` ne touche jamais à Telegram : la question est posée depuis le
    terminal, la réponse s'y affiche, et rien ne part dans le groupe. C'est le
    mode d'une mise au point ou d'une démonstration — on peut y répéter vingt
    fois la même question sans que personne ne reçoive vingt notifications.

    `--listen` ouvre la boucle d'écoute, et donc RÉPOND dans Telegram. Un seul
    processus peut le faire à la fois (contrainte du long polling) : le lancer
    pendant qu'un autre écoute ferait perdre une question sur deux.
    """
    from reviews.config import get_settings
    from reviews.storage.db import get_database

    settings = get_settings()
    db = get_database()
    try:
        if listen:
            from reviews.agents.telegram_chat import build_boucle

            boucle = build_boucle(db, settings)
            if boucle is None:
                print("✗ Configuration insuffisante — voir les journaux.")
                return 1
            print("À l'écoute de Telegram. Ctrl-C pour arrêter.")
            boucle.demarrer()
            return 0

        from reviews.agents.chat_agent import build_chat_agent

        reponse = build_chat_agent(db, settings).repondre(question or "")
    finally:
        db.close_all()

    print()
    if reponse.demande:
        # Les paramètres retenus AVANT la réponse : c'est ce qu'il faut lire en
        # premier quand une réponse surprend, et cela évite de rejouer l'appel
        # au modèle pour comprendre ce qu'il a compris.
        import json as _json

        print("Compris comme :", _json.dumps(reponse.demande.as_dict(),
                                             ensure_ascii=False, indent=2))
        print()
    print(reponse.texte)
    print(f"\n[{reponse.resume()}]")
    return 0


def _cmd_campaign(
    brief: str | None,
    dry_run: bool,
    rapport: int | None,
    lister: bool,
    fiche: int | None = None,
    contenus: int | None = None,
    revoir: int | None = None,
    consigne: str | None = None,
    option: str | None = None,
) -> int:
    """L'assistant de campagne : proposer, lister, ou faire le bilan d'une campagne.

    `--dry-run` mesure et décide TOUT — cible, segment, objectif, leviers — sans
    appeler le modèle pour la rédaction, sans rien enregistrer et sans rien
    envoyer. C'est le mode qui sert à régler les seuils : en conditions réelles,
    chaque essai remplirait la table de campagnes fictives et réveillerait le
    groupe Telegram.
    """
    from reviews.config import get_settings
    from reviews.storage.db import get_database

    settings = get_settings()
    db = get_database()
    try:
        from reviews.agents.campaign_agent import build_campaign_agent
        from reviews.storage.campaign_repository import CampaignRepository

        if lister:
            for c in CampaignRepository(db).lister(limit=20):
                print(
                    f"#{c['campaign_id']:<5} {c['status']:<9} "
                    f"{c['entity_label']:<28} {c['objective']:<13} "
                    f"{int(c['segment_size']):>5} avis  {c['hook'][:60]}"
                )
            return 0

        agent = build_campaign_agent(db, settings)

        for numero, methode in (
            (rapport, agent.rapport), (fiche, agent.fiche), (contenus, agent.contenus)
        ):
            if numero is not None:
                resultat = methode(numero)
                print()
                print(resultat.get("texte") or resultat.get("raison") or "Rien.")
                return 0 if resultat.get("available") else 1

        if revoir is not None:
            campagne = agent.reviser(revoir, consigne or "", strategie=option)
            print()
            print(campagne.texte())
            if campagne.campaign_id:
                print(
                    f"\nVersion n°{campagne.campaign_id}, révision de la "
                    f"n°{revoir} (ton : {campagne.ton})."
                )
            return 0 if not campagne.refus else 1

        campagne = agent.proposer(brief or "", dry_run=dry_run)
    finally:
        db.close_all()

    print(f"\n{'— PROPOSITION À BLANC —' if dry_run else '— PROPOSITION —'}\n")
    print(campagne.texte())
    print(f"\n[{campagne.resume()}]")

    if campagne.ecartees and logging.getLogger().isEnabledFor(logging.DEBUG):
        print("\nCibles écartées :")
        for e in campagne.ecartees[:15]:
            print(f"  · {e['entite']} — {e['raison']}")
    if campagne.campaign_id:
        print(
            f"\nEnregistrée sous le n°{campagne.campaign_id}"
            + (" et transmise pour validation." if campagne.transmise
               else " (aucun canal de validation configuré).")
        )
    return 0 if not campagne.refus else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reviews",
        description="Plateforme de collecte et d'analyse de sentiment des avis clients",
    )
    parser.add_argument("--verbose", action="store_true", help="Logs détaillés (DEBUG)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Créer/vérifier le schéma de la base")

    p_run = sub.add_parser("run", help="Exécuter le pipeline une fois")
    p_run.add_argument("--dry-run", action="store_true", help="Sans insertion en BD")

    p_serve = sub.add_parser("serve", help="Lancer l'API FastAPI")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)

    sub.add_parser("schedule", help="Lancer le pipeline en boucle (APScheduler)")

    p_retract = sub.add_parser(
        "retract-alert",
        help="Retirer une alerte : message Telegram effacé et ligne supprimée",
    )
    p_retract.add_argument("alert_ids", type=int, nargs="+", help="Identifiants d'alerte")

    p_market = sub.add_parser(
        "market-data", help="Collecter les indicateurs de marché (Banque Mondiale/UIT)"
    )
    p_market.add_argument(
        "--limit-countries", type=int, default=None,
        help="N'interroger que les N premiers pays (mise au point)",
    )

    sub.add_parser(
        "ncc-nigeria",
        help="Collecter les abonnés GSM par opérateur (NCC Nigeria)",
    )
    sub.add_parser(
        "anrt-maroc",
        help="Collecter les abonnés mobile par opérateur (ANRT Maroc)",
    )
    sub.add_parser(
        "arcep-benin",
        help="Collecter les abonnés mobile par opérateur (ARCEP Bénin)",
    )
    sub.add_parser(
        "nca-ghana",
        help="Collecter les abonnements voix mobile par opérateur (NCA Ghana)",
    )

    p_agent = sub.add_parser(
        "agent", help="Un passage de l'agent de veille satisfaction"
    )
    p_agent.add_argument(
        "--dry-run",
        action="store_true",
        help="Montre ce qui serait dit, sans appeler le modèle ni envoyer",
    )

    p_quality = sub.add_parser(
        "quality",
        help="Agent 3 : couverture, diagnostic, qualité et score de confiance",
    )
    p_quality.add_argument(
        "--dry-run", action="store_true",
        help="Analyse tout sans rien écrire, sans sonder, sans appeler le "
        "modèle ni notifier",
    )
    p_quality.add_argument(
        "--top", type=int, default=10, metavar="N",
        help="Nombre de filiales détaillées à l'écran (défaut : 10)",
    )

    p_orph = sub.add_parser(
        "orphelins",
        help="Réattribuer les avis sans filiale (analyse par défaut, "
        "écriture sur --appliquer)",
    )
    p_orph.add_argument(
        "--appliquer", action="store_true",
        help="Écrire réellement dans reviews. Sans ce drapeau, rien n'est modifié.",
    )
    p_orph.add_argument(
        "--inclure-probables", action="store_true", dest="inclure_probables",
        help="Étendre l'écriture aux correspondances obtenues après repli "
        "d'accents (HIGH_CONFIDENCE)",
    )

    p_chat = sub.add_parser(
        "chat", help="Agent conversationnel : poser une question ou écouter Telegram"
    )
    groupe = p_chat.add_mutually_exclusive_group(required=True)
    groupe.add_argument(
        "--ask", metavar="QUESTION",
        help="Poser une question depuis le terminal (n'écrit RIEN dans Telegram)",
    )
    groupe.add_argument(
        "--listen", action="store_true",
        help="Ouvrir la boucle d'écoute Telegram (répond dans la conversation)",
    )

    p_campaign = sub.add_parser(
        "campaign",
        help="Assistant de campagne : proposer une campagne, la lister, en faire "
        "le bilan",
    )
    p_campaign.add_argument(
        "--brief", metavar="DESCRIPTION", default=None,
        help="Description libre orientant la campagne (« pour Orange au Mali, "
        "plutôt rassurant, par SMS »). Sans elle, l'agent choisit sa cible.",
    )
    p_campaign.add_argument(
        "--dry-run", action="store_true",
        help="Décide tout sans appeler le modèle, sans enregistrer ni envoyer",
    )
    p_campaign.add_argument(
        "--report", type=int, metavar="ID", default=None,
        help="Bilan de la campagne n°ID : ce que la satisfaction du segment est "
        "devenue depuis son lancement",
    )
    p_campaign.add_argument(
        "--list", action="store_true", dest="list_campaigns",
        help="Lister les campagnes récentes et leur statut",
    )
    p_campaign.add_argument(
        "--fiche", type=int, metavar="ID", default=None,
        help="Dossier structuré de la campagne n°ID (CAMPAIGN REPORT)",
    )
    p_campaign.add_argument(
        "--contenus", type=int, metavar="ID", default=None,
        help="Décliner la campagne n°ID en SMS, e-mail, réseaux et annonce",
    )
    p_campaign.add_argument(
        "--revoir", type=int, metavar="ID", default=None,
        help="Réviser la campagne n°ID (voir --consigne et --option)",
    )
    p_campaign.add_argument(
        "--consigne", metavar="TEXTE", default=None,
        help="Ce qu'il faut changer : « plus agressif commercialement »",
    )
    p_campaign.add_argument(
        "--option", metavar="A|B|C", default=None,
        help="Rejouer la révision sous un autre angle stratégique",
    )

    args = parser.parse_args(argv)

    if args.verbose:
        os.environ["LOG_LEVEL"] = "DEBUG"
    setup_logging()

    try:
        if args.command == "init-db":
            return _cmd_init_db()
        if args.command == "run":
            return _cmd_run(args.dry_run)
        if args.command == "serve":
            return _cmd_serve(args.host, args.port)
        if args.command == "schedule":
            return _cmd_schedule()
        if args.command == "retract-alert":
            return _cmd_retract_alert(args.alert_ids)
        if args.command == "market-data":
            return _cmd_market_data(args.limit_countries)
        if args.command == "ncc-nigeria":
            from reviews.collectors.ncc_nigeria import NccNigeriaCollector
            return _cmd_operator_regulateur("NCC Nigeria", NccNigeriaCollector)
        if args.command == "anrt-maroc":
            from reviews.collectors.anrt_maroc import AnrtMarocCollector
            return _cmd_operator_regulateur("ANRT Maroc", AnrtMarocCollector)
        if args.command == "arcep-benin":
            from reviews.collectors.arcep_benin import ArcepBeninCollector
            return _cmd_operator_regulateur("ARCEP Bénin", ArcepBeninCollector)
        if args.command == "nca-ghana":
            from reviews.collectors.nca_ghana import NcaGhanaCollector
            return _cmd_operator_regulateur("NCA Ghana", NcaGhanaCollector)
        if args.command == "agent":
            return _cmd_agent(args.dry_run)
        if args.command == "quality":
            return _cmd_quality(args.dry_run, args.top)
        if args.command == "orphelins":
            return _cmd_orphelins(args.appliquer, args.inclure_probables)
        if args.command == "chat":
            return _cmd_chat(args.ask, args.listen)
        if args.command == "campaign":
            return _cmd_campaign(
                args.brief, args.dry_run, args.report, args.list_campaigns,
                fiche=args.fiche, contenus=args.contenus, revoir=args.revoir,
                consigne=args.consigne, option=args.option,
            )
    except KeyboardInterrupt:
        logging.getLogger("cli").info("Interrompu par l'utilisateur")
        return 130
    except Exception as e:  # noqa: BLE001
        logging.getLogger("cli").error("Erreur fatale : %s", e, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
