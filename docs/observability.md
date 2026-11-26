# Observabilité — bot-modmail

Ajout propre à ce fork SoTFR. Tout tient dans `core/telemetry.py`, plus quelques appels de
compteurs dans le code existant : l'objectif est de garder la divergence avec
[modmail-dev/modmail](https://github.com/modmail-dev/modmail) aussi faible que possible.

Contrairement aux bots du monorepo `bots` et à l'api, ce bot n'embarque **pas** le SDK
OpenTelemetry. Il expose des métriques Prometheus sur un port local, scrapées par l'Alloy du
stack, qui les transmet à la plateforme. Une seule dépendance (`prometheus-client`) au lieu d'une
dizaine de paquets OTel Python, pour un fork qu'on veut pouvoir resynchroniser.

## Serveur de santé et de métriques

Jamais publié publiquement. Port réglé par `HEALTH_PORT` (défaut `8080`).

| Route      | Sens                                                                |
| ---------- | ------------------------------------------------------------------- |
| `/health`  | Liveness : le process répond. Ne dépend d'aucune dépendance.         |
| `/ready`   | Readiness : 200 si Discord, Mongo et S3 sont prêts, 503 sinon        |
| `/metrics` | Métriques Prometheus                                                 |

Le `HEALTHCHECK` du Dockerfile interroge `/ready`. C'était le dernier service de la flotte sans
aucun signal de santé : la plateforme ne pouvait pas valider son déploiement, et rien ne le
redémarrait s'il se figeait.

Le serveur démarre dans `Bot.run()`, juste après le client S3, et s'arrête avec le bot.

## Logs

Le bot sait déjà écrire du JSON : il suffit de régler `stream_log_format` sur `json`, ce que fait
la stack via la variable d'environnement `STREAM_LOG_FORMAT`
(`ConfigManager.populate_cache` lit toute variable dont le nom correspond à une clé connue).
Sans ça, Loki reçoit des lignes de texte non analysables.

`LOG_LEVEL` règle le niveau, `DISCORD_LOG_LEVEL` celui de discord.py.

Le bot écrit aussi un fichier rotatif dans le conteneur (`logs/modmail.log`), perdu à chaque
redéploiement : c'est le comportement de l'amont, les logs utiles sont ceux de stdout.

## Métriques

| Métrique                            | Type      | Labels       |
| ----------------------------------- | --------- | ------------ |
| `modmail_info`                      | jauge     | `version`    |
| `modmail_dependency_up`             | jauge     | `dependency` |
| `modmail_gateway_latency_seconds`   | jauge     | —            |
| `modmail_threads_open`              | jauge     | —            |
| `modmail_threads_created_total`     | compteur  | `origin`     |
| `modmail_threads_closed_total`      | compteur  | `reason`     |
| `modmail_thread_duration_seconds`   | histo     | —            |
| `modmail_messages_relayed_total`    | compteur  | `direction`  |
| `modmail_uploads_total`             | compteur  | `result`     |
| `modmail_dashboard_events_total`    | compteur  | `event`      |

- `origin` vaut `staff` (ticket ouvert via `?contact`) ou `member` (message privé reçu).
- `reason` vaut `manual` ou `scheduled` (fermeture programmée).
- `direction` vaut `to_member` (réponse de l'équipe) ou `to_team` (message du membre). Les notes
  internes ne sont pas comptées.
- `result` des uploads : `ok`, `failed` (échec S3) ou `download_failed` (CDN Discord injoignable).
- `event` du dashboard : `connected`, `disconnected`, `connect_failed` — de quoi voir une liaison
  websocket instable avec l'api.
- `modmail_dependency_up` reprend les vérifications de `/ready` : c'est la métrique sur laquelle
  alerter.

Les jauges sont relevées au moment du scrape, pas en continu.

## Ajouter une métrique

Déclarer l'instrument dans `core/telemetry.py`, dans **les deux** branches du `if AVAILABLE` :
la branche `else` donne un `_NoopMetric`, pour que le bot démarre même sans `prometheus_client`.
Puis l'importer et l'appeler là où l'événement se produit.

Garder les labels à faible cardinalité : un motif ou un résultat conviennent, un identifiant de
membre ou de ticket non.
