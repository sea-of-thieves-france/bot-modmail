"""
Dashboard realtime client
=========================

Dials the moderation dashboard's realtime hub so the bot can:

* send lightweight "something changed" signals for threads (targeted refetch),
* receive moderator replies (with a native socket.io **ack**) that are relayed to
  the recipient's DM and mirrored into the thread via :meth:`Thread.reply`.

Topology (inverted vs. the old design)
--------------------------------------
The **API hosts** the realtime hub (a secret-authenticated ``/modmail`` socket.io
namespace); the **bot dials in as an outbound client** — zero inbound ports::

    browser <-(socket.io default ns, JWT)-> web-dashboard API <-(/modmail ns, secret)-> bot

* Moderator replies go browser -> API -> bot; the API injects the trusted ``mod_id``
  and the bot enforces the existing ``PermissionLevel`` before sending anything.
* Signals go bot -> API; the API refetches from Mongo and pushes to browsers.

Trust model
-----------
* the socket *connection* is authenticated by a shared secret sent in the socket.io
  handshake ``auth`` (``{secret, bot_id, guild_id}``); constant-time-compared API-side;
* every reply frame carries the acting moderator's Discord ``mod_id``; the bot resolves
  the guild member and enforces the ``reply`` ``PermissionLevel``;
* the reply handler's **return value is the ack** (``{ok, error?}``) — no request_id map.

Opt-in: if ``DASHBOARD_WS_SECRET`` or ``DASHBOARD_API_URL`` is unset, the cog loads
but never connects.

Configuration (environment / .env)
----------------------------------
    DASHBOARD_WS_SECRET          shared secret; feature disabled when unset
    DASHBOARD_API_URL            hub URL (dev http://sotfr-api:4000, prod wss://api.sotfr.net)
    DASHBOARD_MAX_ATTACHMENT_MB  per-file size cap for relayed attachments (default 8)
"""

import asyncio
import os
from types import SimpleNamespace

import discord
import socketio
from discord.ext import commands

from core import checks
from core.models import getLogger
from core.utils import match_ticket_id

logger = getLogger(__name__)

MAX_ATTACHMENTS = 10
NAMESPACE = "/modmail"


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
    """Socket.io client dialing the dashboard realtime hub."""

    def __init__(self, bot):
        self.bot = bot
        self.secret = (os.environ.get("DASHBOARD_WS_SECRET") or "").strip()
        self.url = (os.environ.get("DASHBOARD_API_URL") or "").strip()
        try:
            self.max_attachment_bytes = int(
                float(os.environ.get("DASHBOARD_MAX_ATTACHMENT_MB") or 8) * 1024 * 1024
            )
        except ValueError:
            self.max_attachment_bytes = 8 * 1024 * 1024

        # reconnection=True gives us native backoff/resume; no manual reconnect loop.
        self.sio = socketio.AsyncClient(reconnection=True, logger=False, engineio_logger=False)
        self.sio.on("connect", self._on_connect, namespace=NAMESPACE)
        self.sio.on("disconnect", self._on_disconnect, namespace=NAMESPACE)
        self.sio.on("reply", self._on_reply, namespace=NAMESPACE)

        self._connect_task = None

    # ------------------------------------------------------------------ lifecycle
    async def cog_load(self):
        if not self.secret or not self.url:
            logger.info(
                "Dashboard hub client disabled (set DASHBOARD_WS_SECRET and DASHBOARD_API_URL)."
            )
            return
        # Connect in the background: cog_load runs inside setup_hook, before the
        # gateway READY, so we must not await wait_until_ready() here (deadlock).
        self._connect_task = self.bot.loop.create_task(self._run())

    async def cog_unload(self):
        if self._connect_task is not None:
            self._connect_task.cancel()
        try:
            if self.sio.connected:
                await self.sio.disconnect()
        except Exception:
            pass

    async def _run(self):
        await self.bot.wait_until_ready()
        if not self.bot.guild_id:
            logger.warning("Dashboard hub client disabled (no GUILD_ID configured).")
            return
        auth = {
            "secret": self.secret,
            "bot_id": str(self.bot.user.id),
            "guild_id": str(self.bot.guild_id),
        }
        # Retry only the *initial* connect; socket.io handles drops after that.
        delay = 1
        while not self.sio.connected:
            try:
                await self.sio.connect(
                    self.url, auth=auth, namespaces=[NAMESPACE], transports=["websocket"]
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Dashboard hub connect to %s failed; retrying in %ss.",
                    self.url,
                    delay,
                    exc_info=True,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    # -------------------------------------------------------------- hub callbacks
    async def _on_connect(self):
        logger.info("Dashboard hub connected (%s at %s).", NAMESPACE, self.url)
        # Ask the API to refresh any moderator views that went stale while we were away.
        try:
            await self.sio.emit("resync", namespace=NAMESPACE)
        except Exception:
            logger.debug("resync emit failed.", exc_info=True)

    async def _on_disconnect(self):
        logger.warning("Dashboard hub disconnected (%s).", NAMESPACE)

    async def _on_reply(self, data):
        """Handle an API->bot reply; the return value becomes the socket.io ack."""
        await self.bot.wait_until_ready()
        if not isinstance(data, dict):
            return {"ok": False, "error": "bad_ids"}

        content = (data.get("content") or "").strip()
        raw_attachments = data.get("attachments") or []
        if not content and not raw_attachments:
            return {"ok": False, "error": "empty_message"}

        # Resolve the thread from its channel.
        try:
            channel_id = int(data.get("channel_id"))
            mod_id = int(data.get("mod_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_ids"}

        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return {"ok": False, "error": "thread_not_found"}
        thread = await self.bot.threads.find(channel=channel)
        if thread is None:
            return {"ok": False, "error": "thread_not_found"}

        # Resolve the acting moderator and enforce PermissionLevel for `reply`.
        member = self.bot.guild.get_member(mod_id) if self.bot.guild else None
        if member is None:
            return {"ok": False, "error": "mod_not_in_guild"}
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
            return {"ok": False, "error": "forbidden"}

        # Build relayed attachments (fail-soft: a bad file is skipped, text still sends).
        attachments = await self._build_attachments(raw_attachments)
        message = _DashboardMessage(
            author=member, content=content, channel=thread.channel, attachments=attachments
        )
        try:
            await thread.reply(message, content)
        except Exception:
            logger.error("Dashboard reply failed to send.", exc_info=True)
            return {"ok": False, "error": "send_failed"}

        return {"ok": True}

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

    # ---------------------------------------------------------------- signals out
    def _signal(self, thread, **extra) -> dict:
        # `log_key` is only populated on threads created by *this* process; a thread
        # rebuilt from an existing channel (Threads._find_from_channel) has none, so
        # fall back to the key embedded in the channel topic (same trick as
        # Thread.reply's topic rebuild). Without it the API drops the signal.
        channel = thread.channel
        ticket_id = getattr(thread, "log_key", None)
        if not ticket_id and channel is not None:
            ticket_id = match_ticket_id(channel.topic or "")
        base = {
            "channel_id": str(channel.id) if channel else None,
            "ticket_id": ticket_id,
        }
        base.update(extra)
        return base

    async def _emit(self, event: str, payload: dict):
        if not self.sio.connected:
            logger.info("[diag] %s not emitted: socket disconnected.", event)
            return
        try:
            logger.info("[diag] emitting %s %s", event, payload)
            await self.sio.emit(event, payload, namespace=NAMESPACE)
        except Exception:
            logger.debug("Failed to emit %s to dashboard hub.", event, exc_info=True)

    # -------------------------------------------------------------------- events
    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        recipient_id = str(thread.id) if thread.id else None
        await self._emit("thread_create", self._signal(thread, recipient_id=recipient_id))

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod, message, anonymous, plain):
        await self._emit(
            "thread_message",
            self._signal(thread, from_mod=bool(from_mod), ts=str(discord.utils.utcnow())),
        )

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, silent, delete_channel, message, scheduled):
        await self._emit("thread_close", self._signal(thread))


async def setup(bot):
    await bot.add_cog(Dashboard(bot))
