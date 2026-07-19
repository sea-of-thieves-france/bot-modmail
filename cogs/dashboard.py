"""
Dashboard websocket bridge
==========================

Exposes a small websocket server so an external moderation dashboard
(``web-dashboard``) can:

* receive lightweight "something changed" signals for threads (targeted refetch),
* send moderator replies that are relayed to the recipient's DM and mirrored into
  the thread channel through the normal :meth:`Thread.reply` pipeline.

Topology
--------
This socket is a **server-to-server** surface. The only intended client is the
``web-dashboard`` **backend API**, never a browser:

    browser <-(OAuth session, WS/SSE)-> web-dashboard API <-(this socket)-> bot

* Moderator actions go browser -> API -> bot; the API injects the trusted ``mod_id``.
* Live "something changed" signals go bot -> API, and the API refetches the thread
  from Mongo/Logviewer and pushes the fresh view to browsers. The bot is never on the
  read path.
* A browser cannot connect here even if it wanted to: the connection is authenticated
  with an ``Authorization`` header, which the browser WebSocket API cannot set. Only a
  backend holding ``DASHBOARD_WS_SECRET`` can complete the handshake.

Trust model
-----------
The dashboard authenticates its Discord moderators itself (Discord OAuth against its
own API); the bot never sees those tokens.  Instead:

* the websocket *connection* is authenticated with a shared secret
  (``DASHBOARD_WS_SECRET``) sent as ``Authorization: Bearer <secret>``;
* every reply frame carries the acting moderator's Discord ``mod_id``; the bot
  resolves the guild member and enforces the **existing** ``PermissionLevel`` for the
  ``reply`` command before sending anything;
* replies may include an opaque ``request_id`` that the bot echoes back on the
  matching ``reply_ack`` so the API can correlate acks across multiplexed moderators.

The whole feature is opt-in: if ``DASHBOARD_WS_SECRET`` is not set, the cog loads but
starts no server.

Configuration (environment / .env)
----------------------------------
    DASHBOARD_WS_SECRET          shared secret; feature disabled when unset
    DASHBOARD_WS_HOST            bind host (default 0.0.0.0)
    DASHBOARD_WS_PORT            bind port (default 8765)
    DASHBOARD_MAX_ATTACHMENT_MB  per-file size cap for relayed attachments (default 8)
"""

import asyncio
import hmac
import json
import os
from types import SimpleNamespace

import discord
from aiohttp import web, WSMsgType
from discord.ext import commands

from core import checks
from core.models import getLogger

logger = getLogger(__name__)

MAX_ATTACHMENTS = 10


class _DashboardAttachment:
    """Duck-typed stand-in for :class:`discord.Attachment`.

    Exposes only what the reply pipeline reads: ``url``/``filename`` (used by
    ``S3StorageClient.upload_attachment`` to download+re-upload) plus ``id``/``size``
    (persisted by ``append_log``).
    """

    def __init__(self, url: str, filename: str, size: int = 0):
        self.url = url
        self.filename = filename
        self.size = size
        self.id = discord.utils.time_snowflake(discord.utils.utcnow())
        self.content_type = None
        self.spoiler = False

    def is_spoiler(self) -> bool:
        return False


class _DashboardMessage:
    """Minimal :class:`discord.Message` look-alike for a dashboard-originated reply.

    Only the attributes touched by ``Thread.reply`` / ``Thread.send`` /
    ``ApiClient.append_log`` are provided; mutating methods are no-ops (mirrors
    :class:`core.models.DummyMessage`).
    """

    def __init__(self, *, author, content, channel, attachments=None):
        self.author = author
        self.content = content or ""
        self.channel = channel
        self.attachments = attachments or []
        self.stickers = []
        self.embeds = []
        self.message_snapshots = []
        self.reference = None
        self.mentions = []
        self.role_mentions = []
        self.mention_everyone = False
        self.created_at = discord.utils.utcnow()
        self.id = discord.utils.time_snowflake(self.created_at)
        self.type = discord.MessageType.default

    @property
    def jump_url(self) -> str:
        return ""

    async def delete(self, *, delay=None):
        return

    async def edit(self, **fields):
        return self

    async def add_reaction(self, emoji):
        return

    async def remove_reaction(self, emoji, member=None):
        return

    async def clear_reaction(self, emoji):
        return

    async def clear_reactions(self):
        return

    async def pin(self, *, reason=None):
        return

    async def unpin(self, *, reason=None):
        return


class Dashboard(commands.Cog):
    """Websocket bridge between the bot and the moderation dashboard."""

    def __init__(self, bot):
        self.bot = bot
        self.secret = (os.environ.get("DASHBOARD_WS_SECRET") or "").strip()
        self.host = (os.environ.get("DASHBOARD_WS_HOST") or "0.0.0.0").strip()
        try:
            self.port = int(os.environ.get("DASHBOARD_WS_PORT") or 8765)
        except ValueError:
            self.port = 8765
        try:
            self.max_attachment_bytes = int(
                float(os.environ.get("DASHBOARD_MAX_ATTACHMENT_MB") or 8) * 1024 * 1024
            )
        except ValueError:
            self.max_attachment_bytes = 8 * 1024 * 1024

        self._runner: web.AppRunner = None
        self._site: web.TCPSite = None
        self._clients: set = set()

    # ------------------------------------------------------------------ lifecycle
    async def cog_load(self):
        if not self.secret:
            logger.info("Dashboard websocket disabled (DASHBOARD_WS_SECRET is not set).")
            return
        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        try:
            await self._site.start()
        except OSError:
            logger.error(
                "Dashboard websocket failed to bind %s:%s.",
                self.host,
                self.port,
                exc_info=True,
            )
            self._site = None
            return
        logger.info("Dashboard websocket listening on ws://%s:%s/ws", self.host, self.port)

    async def cog_unload(self):
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass

    # --------------------------------------------------------------------- server
    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        # Authenticate the connection with a constant-time secret comparison.
        provided = request.headers.get("Authorization", "")
        prefix = "Bearer "
        token = provided[len(prefix) :] if provided.startswith(prefix) else ""
        if not (self.secret and token and hmac.compare_digest(token, self.secret)):
            logger.warning("Rejected dashboard websocket connection (bad secret).")
            return web.Response(status=401, text="unauthorized")

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        logger.info("Dashboard websocket client connected (%d total).", len(self._clients))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_text(ws, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("Dashboard websocket connection error: %s", ws.exception())
        finally:
            self._clients.discard(ws)
            logger.info(
                "Dashboard websocket client disconnected (%d remaining).",
                len(self._clients),
            )
        return ws

    async def _on_text(self, ws: web.WebSocketResponse, data: str):
        try:
            payload = json.loads(data)
        except (ValueError, TypeError):
            await self._send(ws, {"type": "error", "error": "invalid_json"})
            return

        action = payload.get("action")
        if action == "reply":
            await self._handle_reply(ws, payload)
        else:
            await self._send(
                ws,
                {
                    "type": "error",
                    "error": "unknown_action",
                    "request_id": payload.get("request_id") if isinstance(payload, dict) else None,
                },
            )

    async def _handle_reply(self, ws: web.WebSocketResponse, payload: dict):
        # Correlation id: the dashboard API multiplexes many moderators over the
        # single bot connection, so it must be able to match each ack/nack back to
        # the request (and thus the browser) that produced it. Echoed verbatim.
        request_id = payload.get("request_id")

        async def nack(error):
            await self._send(
                ws,
                {"type": "reply_ack", "ok": False, "error": error, "request_id": request_id},
            )

        await self.bot.wait_until_ready()

        content = (payload.get("content") or "").strip()
        raw_attachments = payload.get("attachments") or []
        if not content and not raw_attachments:
            return await nack("empty_message")

        # Resolve the thread from its channel.
        try:
            channel_id = int(payload.get("channel_id"))
            mod_id = int(payload.get("mod_id"))
        except (TypeError, ValueError):
            return await nack("bad_ids")

        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return await nack("thread_not_found")
        thread = await self.bot.threads.find(channel=channel)
        if thread is None:
            return await nack("thread_not_found")

        # Resolve the acting moderator and enforce PermissionLevel for `reply`.
        member = self.bot.guild.get_member(mod_id) if self.bot.guild else None
        if member is None:
            return await nack("mod_not_in_guild")
        fake_ctx = SimpleNamespace(
            bot=self.bot,
            author=member,
            channel=thread.channel,
            guild=self.bot.modmail_guild,
        )
        try:
            allowed = await checks.check_permissions(fake_ctx, "reply")
        except Exception:
            logger.warning("Permission check failed for dashboard reply.", exc_info=True)
            allowed = False
        if not allowed:
            return await nack("forbidden")

        # Build relayed attachments (fail-soft: a bad file is skipped, text still sends).
        attachments = await self._build_attachments(raw_attachments)

        message = _DashboardMessage(
            author=member, content=content, channel=thread.channel, attachments=attachments
        )
        try:
            await thread.reply(message, content)
        except Exception:
            logger.error("Dashboard reply failed to send.", exc_info=True)
            return await nack("send_failed")

        await self._send(ws, {"type": "reply_ack", "ok": True, "request_id": request_id})

    async def _build_attachments(self, raw_attachments: list) -> list:
        attachments = []
        for entry in raw_attachments[:MAX_ATTACHMENTS]:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            filename = entry.get("filename") or "attachment"
            if not url:
                continue
            size = 0
            try:
                # HEAD-style size probe; skip oversized files early.
                async with self.bot.session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning("Skipping dashboard attachment %s (HTTP %d).", url, resp.status)
                        continue
                    length = resp.headers.get("Content-Length")
                    if length is not None:
                        size = int(length)
                        if size > self.max_attachment_bytes:
                            logger.warning("Skipping oversized dashboard attachment %s.", url)
                            continue
            except Exception:
                logger.warning("Could not reach dashboard attachment %s; skipping.", url, exc_info=True)
                continue
            attachments.append(_DashboardAttachment(url, filename, size))
        return attachments

    # --------------------------------------------------------------- broadcasting
    async def _send(self, ws: web.WebSocketResponse, obj: dict):
        try:
            await ws.send_str(json.dumps(obj))
        except Exception:
            self._clients.discard(ws)

    async def _broadcast(self, obj: dict):
        if not self._clients:
            return
        data = json.dumps(obj)
        for ws in list(self._clients):
            try:
                await ws.send_str(data)
            except Exception:
                self._clients.discard(ws)

    def _signal(self, thread, **extra) -> dict:
        base = {
            "channel_id": str(thread.channel.id) if thread.channel else None,
            "ticket_id": getattr(thread, "log_key", None),
        }
        base.update(extra)
        return base

    # -------------------------------------------------------------------- events
    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        if not self._clients:
            return
        recipient_id = str(thread.id) if thread.id else None
        await self._broadcast(self._signal(thread, type="thread_create", recipient_id=recipient_id))

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod, message, anonymous, plain):
        if not self._clients:
            return
        await self._broadcast(
            self._signal(
                thread,
                type="thread_message",
                from_mod=bool(from_mod),
                ts=str(discord.utils.utcnow()),
            )
        )

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, silent, delete_channel, message, scheduled):
        if not self._clients:
            return
        await self._broadcast(self._signal(thread, type="thread_close"))


async def setup(bot):
    await bot.add_cog(Dashboard(bot))
