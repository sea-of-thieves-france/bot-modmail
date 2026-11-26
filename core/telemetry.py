"""
Santé et métriques du bot.

Expose un petit serveur HTTP, jamais publié publiquement :
- ``GET /health``  : liveness, le process répond ;
- ``GET /ready``   : readiness, 200 si Discord, Mongo et S3 sont prêts, 503 sinon, détail en JSON ;
- ``GET /metrics`` : métriques Prometheus, scrapées par l'Alloy du stack.

Les métriques sont des no-op si ``prometheus_client`` n'est pas installé : le bot démarre
quand même, sans télémétrie.
"""

import json
import os
import typing

import discord
from aiohttp import web

from core.models import getLogger

logger = getLogger(__name__)

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False
    logger.warning("prometheus_client is not installed - metrics are disabled.")


DEFAULT_PORT = 8080


class _NoopMetric:
    """Remplace un instrument quand prometheus_client est absent."""

    def labels(self, *_args, **_kwargs) -> "_NoopMetric":
        return self

    def inc(self, *_args, **_kwargs) -> None:
        pass

    def dec(self, *_args, **_kwargs) -> None:
        pass

    def set(self, *_args, **_kwargs) -> None:
        pass

    def observe(self, *_args, **_kwargs) -> None:
        pass


if AVAILABLE:
    REGISTRY = CollectorRegistry()

    BOT_INFO = Gauge(
        "modmail_info",
        "Version du bot en cours d'execution",
        ["version"],
        registry=REGISTRY,
    )
    DEPENDENCY_UP = Gauge(
        "modmail_dependency_up",
        "Etat des dependances du bot (1 : prete, 0 : indisponible)",
        ["dependency"],
        registry=REGISTRY,
    )
    GATEWAY_LATENCY = Gauge(
        "modmail_gateway_latency_seconds",
        "Latence websocket avec la gateway Discord",
        registry=REGISTRY,
    )
    THREADS_OPEN = Gauge(
        "modmail_threads_open",
        "Tickets ouverts suivis en memoire",
        registry=REGISTRY,
    )
    THREADS_CREATED = Counter(
        "modmail_threads_created_total",
        "Tickets ouverts, par origine",
        ["origin"],
        registry=REGISTRY,
    )
    THREADS_CLOSED = Counter(
        "modmail_threads_closed_total",
        "Tickets fermes, par motif",
        ["reason"],
        registry=REGISTRY,
    )
    THREAD_DURATION = Histogram(
        "modmail_thread_duration_seconds",
        "Duree de vie d'un ticket, de l'ouverture a la fermeture",
        buckets=(300, 900, 3600, 10800, 43200, 86400, 259200, 604800),
        registry=REGISTRY,
    )
    MESSAGES_RELAYED = Counter(
        "modmail_messages_relayed_total",
        "Messages relayes entre le membre et l'equipe, par sens",
        ["direction"],
        registry=REGISTRY,
    )
    UPLOADS = Counter(
        "modmail_uploads_total",
        "Envois de pieces jointes vers S3, par resultat",
        ["result"],
        registry=REGISTRY,
    )
    DASHBOARD_EVENTS = Counter(
        "modmail_dashboard_events_total",
        "Evenements de la liaison websocket avec le dashboard",
        ["event"],
        registry=REGISTRY,
    )
else:  # pragma: no cover
    REGISTRY = None
    BOT_INFO = _NoopMetric()
    DEPENDENCY_UP = _NoopMetric()
    GATEWAY_LATENCY = _NoopMetric()
    THREADS_OPEN = _NoopMetric()
    THREADS_CREATED = _NoopMetric()
    THREADS_CLOSED = _NoopMetric()
    THREAD_DURATION = _NoopMetric()
    MESSAGES_RELAYED = _NoopMetric()
    UPLOADS = _NoopMetric()
    DASHBOARD_EVENTS = _NoopMetric()


class TelemetryServer:
    """Serveur de santé et de métriques, démarré avec le bot et arrêté avec lui."""

    def __init__(self, bot):
        self.bot = bot
        self._runner: typing.Optional[web.AppRunner] = None

    async def start(self, port: typing.Optional[int] = None) -> None:
        port = port or int(os.getenv("HEALTH_PORT", DEFAULT_PORT))
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/ready", self._ready)
        app.router.add_get("/metrics", self._metrics)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        self._runner = runner
        BOT_INFO.labels(version=str(self.bot.version)).set(1)
        logger.info("Health server listening on port %s.", port)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def checks(self) -> typing.Dict[str, bool]:
        """Chaque dépendance ; une vérification qui lève une erreur compte comme « pas prête »."""

        def safe(check) -> bool:
            try:
                return bool(check())
            except Exception:
                return False

        return {
            "discord": safe(lambda: self.bot.is_ready() and not self.bot.is_closed()),
            # La connexion Mongo est validée au démarrage ; ici on vérifie qu'elle est toujours là.
            "mongo": safe(lambda: self.bot.db is not None),
            "storage": safe(lambda: self.bot.storage is not None),
        }

    async def _health(self, _request: web.Request) -> web.Response:
        # `bot.uptime` est une chaine lisible : on recalcule des secondes pour la sonde.
        uptime = int((discord.utils.utcnow() - self.bot.start_time).total_seconds())
        return _json({"status": "ok", "uptime": uptime})

    async def _ready(self, _request: web.Request) -> web.Response:
        checks = self.checks()
        ready = all(checks.values())
        return _json(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status=200 if ready else 503,
        )

    async def _metrics(self, _request: web.Request) -> web.Response:
        if not AVAILABLE:
            return web.Response(status=501, text="prometheus_client is not installed\n")

        # Mesures relevées au moment du scrape, pas en continu.
        for dependency, ready in self.checks().items():
            DEPENDENCY_UP.labels(dependency=dependency).set(1 if ready else 0)
        if self.bot.is_ready():
            GATEWAY_LATENCY.set(self.bot.latency)
        THREADS_OPEN.set(len(self.bot.threads))

        return web.Response(body=generate_latest(REGISTRY), content_type="text/plain", charset="utf-8")


def _json(payload: dict, status: int = 200) -> web.Response:
    return web.Response(
        body=json.dumps(payload),
        status=status,
        content_type="application/json",
        headers={"cache-control": "no-store"},
    )
