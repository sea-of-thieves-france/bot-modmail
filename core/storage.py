"""
Optional S3-compatible storage client for Garage (or any S3-compatible service).

When configured, the bot uploads Discord attachments (images, videos, files)
and user avatars to the configured bucket and stores permanent S3 URLs in
MongoDB instead of ephemeral Discord CDN URLs.

Configuration via environment variables (.env):
    S3_ENDPOINT   – internal S3 API URL, e.g. "http://garage:9000"
    S3_ACCESS_KEY – access key ID (obtained from ``garage key create``)
    S3_SECRET_KEY – secret access key
    S3_BUCKET     – target bucket name (default: "modmail")
    S3_REGION     – region string (default: "garage")
    S3_PUBLIC_URL – publicly reachable base URL for generated links
                    (defaults to S3_ENDPOINT; override when the bot's
                    internal Docker address differs from what browsers use,
                    e.g. "http://your-server:9100")

The feature is entirely optional.  If any of S3_ENDPOINT / S3_ACCESS_KEY /
S3_SECRET_KEY is missing, ``S3StorageClient.enabled`` is ``False`` and all
upload methods immediately return ``None`` — existing code paths are
unaffected.
"""

import logging
import os
from typing import Optional

import discord

try:
    import aiobotocore.session
    from botocore.exceptions import ClientError as BotocoreClientError

    _HAS_AIOBOTOCORE = True
except ImportError:
    _HAS_AIOBOTOCORE = False

logger = logging.getLogger(__name__)


class S3StorageClient:
    """S3-compatible storage client. Gracefully disabled when unconfigured."""

    def __init__(self, bot):
        self.bot = bot

        self.endpoint: Optional[str] = os.environ.get("S3_ENDPOINT", "").strip() or None
        self.access_key: Optional[str] = os.environ.get("S3_ACCESS_KEY", "").strip() or None
        self.secret_key: Optional[str] = os.environ.get("S3_SECRET_KEY", "").strip() or None
        self.bucket: str = os.environ.get("S3_BUCKET", "modmail").strip()
        self.region: str = os.environ.get("S3_REGION", "garage").strip()

        # Public-facing base URL written into MongoDB for every attachment/avatar.
        # Garage uses virtual-host routing on its web endpoint, so the bucket name
        # must be the subdomain — the path only contains the object key.
        #
        # Format:  http://<bucket>.<root_domain>:<web_port>
        # Example: http://modmail.localhost:9200
        #          → Garage serves bucket "modmail", root_domain ".localhost"
        #
        # This must point to the Garage *web* endpoint (port 3902 / host 9200),
        # NOT the S3 API endpoint (port 9000 / host 9100) — the S3 API always
        # requires authentication.
        raw_public = os.environ.get("S3_PUBLIC_URL", "").strip()
        self.public_url: str = (raw_public or self.endpoint or "").rstrip("/")
        self._public_url_explicitly_set: bool = bool(raw_public)

        self.enabled: bool = bool(
            _HAS_AIOBOTOCORE and self.endpoint and self.access_key and self.secret_key
        )

        self._session = None
        self._cm = None       # context manager returned by create_client()
        self._client = None   # the actual low-level botocore client

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def setup(self) -> None:
        """Create the persistent aiobotocore S3 client and log the outcome."""
        if not self.enabled:
            if not _HAS_AIOBOTOCORE:
                logger.warning(
                    "aiobotocore is not installed; S3 attachment/avatar storage is disabled."
                )
            else:
                logger.info("S3 storage disabled (S3_ENDPOINT / S3_ACCESS_KEY / S3_SECRET_KEY not set).")
            return

        self._session = aiobotocore.session.get_session()
        self._cm = self._session.create_client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
        )
        self._client = await self._cm.__aenter__()

        if not self._public_url_explicitly_set:
            logger.warning(
                "S3_PUBLIC_URL is not set.  Stored URLs will use the S3 endpoint "
                "(%s) which requires authentication and is NOT publicly accessible.  "
                "Set S3_PUBLIC_URL to the Garage web endpoint address, "
                "e.g.  S3_PUBLIC_URL=http://<your-server-ip>:9200",
                self.endpoint,
            )

        logger.info(
            "S3 storage ready — upload endpoint: %s | public URL prefix: %s/",
            self.endpoint,
            self.public_url,
        )

    async def close(self) -> None:
        """Release the aiobotocore client cleanly."""
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception as exc:
                logger.debug("Error closing S3 client: %s", exc)
            finally:
                self._client = None
                self._cm = None

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _url_for(self, key: str) -> str:
        """Build the public URL for an object key.

        Garage's web endpoint uses virtual-host routing: the bucket is the
        subdomain of ``root_domain``, so ``S3_PUBLIC_URL`` already contains
        the bucket (e.g. ``http://modmail.localhost:9200``).
        The object key is simply appended as the path.

        ``{S3_PUBLIC_URL}/{key}``
        e.g.  http://modmail.localhost:9200/avatars/123/abc.webp
        """
        return f"{self.public_url}/{key}"

    async def _object_exists(self, key: str) -> bool:
        """Return True if the object already exists in the bucket (HEAD request)."""
        try:
            await self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except BotocoreClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("404", "NoSuchKey"):
                return False
            logger.warning("S3 head_object error for key %r: %s", key, exc)
            return False
        except Exception as exc:
            logger.warning("Unexpected S3 head_object error for key %r: %s", key, exc)
            return False

    async def _put(self, key: str, data: bytes, content_type: str) -> str:
        """
        PUT an object with public-read ACL.

        Raises on failure — callers are expected to catch and return None.
        Returns the public URL on success.
        """
        await self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ACL="public-read",
        )
        return self._url_for(key)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def upload_attachment(self, attachment: discord.Attachment) -> Optional[str]:
        """
        Download a Discord attachment and store it in S3.

        Object key: ``attachments/{attachment_id}/{filename}``

        - Idempotent: if the key already exists, returns the existing URL.
        - Fail-silent: returns ``None`` on any download or upload error.

        Returns the public S3 URL or ``None``.
        """
        if not self.enabled or self._client is None:
            return None

        key = f"attachments/{attachment.id}/{attachment.filename}"

        if await self._object_exists(key):
            return self._url_for(key)

        # Download from Discord CDN
        try:
            async with self.bot.session.get(attachment.url) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Cannot download attachment %s (HTTP %d); S3 upload skipped.",
                        attachment.url,
                        resp.status,
                    )
                    return None
                data = await resp.read()
                content_type = resp.content_type or "application/octet-stream"
        except Exception as exc:
            logger.warning("Error downloading attachment %s: %s", attachment.url, exc)
            return None

        # Upload to S3
        try:
            url = await self._put(key, data, content_type)
            logger.debug("Uploaded attachment → %s", url)
            return url
        except Exception as exc:
            logger.warning("Error uploading attachment to S3 (key=%r): %s", key, exc)
            return None

    async def upload_avatar(self, user_id: int, avatar: discord.Asset) -> Optional[str]:
        """
        Upload a Discord user avatar to S3, skipping if the hash is already stored.

        Object key: ``avatars/{user_id}/{hash}.{ext}``
        Animated avatars (hash starts with ``a_``) → GIF; others → WebP.

        - Idempotent: returns existing URL if the avatar hash was already uploaded.
        - Fail-silent: returns ``None`` on any error.

        Returns the public S3 URL or ``None``.
        """
        if not self.enabled or self._client is None:
            return None

        hash_ = avatar.key            # e.g. "a_abc123" or "abc123def456"
        ext = "gif" if hash_.startswith("a_") else "webp"
        key = f"avatars/{user_id}/{hash_}.{ext}"

        if await self._object_exists(key):
            return self._url_for(key)

        # Resolve the download URL (force a consistent format + size)
        try:
            if ext == "gif":
                avatar_url = str(avatar.with_format("gif").with_size(256))
            else:
                avatar_url = str(avatar.with_format("webp").with_size(256))
        except Exception:
            avatar_url = str(avatar)

        # Download from Discord CDN
        try:
            async with self.bot.session.get(avatar_url) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Cannot download avatar %s (HTTP %d); S3 upload skipped.",
                        avatar_url,
                        resp.status,
                    )
                    return None
                data = await resp.read()
        except Exception as exc:
            logger.warning("Error downloading avatar %s: %s", avatar_url, exc)
            return None

        content_type = "image/gif" if ext == "gif" else "image/webp"

        # Upload to S3
        try:
            url = await self._put(key, data, content_type)
            logger.debug("Uploaded avatar → %s", url)
            return url
        except Exception as exc:
            logger.warning("Error uploading avatar to S3 (key=%r): %s", key, exc)
            return None

    async def upload_bytes(
        self, key: str, data: bytes, content_type: str
    ) -> Optional[str]:
        """
        Upload raw bytes to S3 under an explicit key.

        Intended for data already in memory (e.g. lottie stickers converted to PNG).
        Idempotent: if the key already exists the existing URL is returned.

        Returns the public S3 URL or ``None`` on failure.
        """
        if not self.enabled or self._client is None:
            return None

        if await self._object_exists(key):
            return self._url_for(key)

        try:
            url = await self._put(key, data, content_type)
            logger.debug("Uploaded bytes → %s", url)
            return url
        except Exception as exc:
            logger.warning("Error uploading bytes to S3 (key=%r): %s", key, exc)
            return None
