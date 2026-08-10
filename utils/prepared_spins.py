import asyncio
import logging
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import discord

from database.database import (
    get_session_wheel_game_ids,
    get_spin_games_by_ids,
    get_wheel_game_ids,
    get_smart_random_game,
    get_smart_random_game_for_ids,
    get_smart_random_singleplayer_game,
)
from settings import (
    OCI_SPIN_BUCKET,
    OCI_SPIN_NAMESPACE,
    OCI_SPIN_REGION,
    SPIN_CACHE_CHANNEL_ID,
)
from ui.animation import render_spin_gif_file
from utils.spin_gif import (
    SPIN_FRAME_DURATIONS_MS,
    SPIN_GIF_TEMP_DIRECTORY,
    SUSPENSE_DURATION_MS,
    WINNER_FLASH_DURATION_MS,
)


LOGGER = logging.getLogger(__name__)

PREPARED_SPIN_POOL_SIZE = 20
SESSION_SPIN_POOL_SIZE = 5
PREPARED_SPIN_MAX_AGE_SECONDS = 24 * 60 * 60
PREPARED_SPIN_DELETE_DELAY_SECONDS = 15 * 60
PREPARED_SPIN_RETRY_SECONDS = 30
PREPARED_SPIN_RECENT_WINNER_SECONDS = 5 * 60
PREPARED_SPIN_LIBRARY_CHECK_SECONDS = 5
PREPARED_WHEEL_TYPES = (
    "multiplayer",
    "singleplayer",
)
PREPARED_SPIN_DURATION_SECONDS = (
    sum(SPIN_FRAME_DURATIONS_MS)
    + SUSPENSE_DURATION_MS
    + WINNER_FLASH_DURATION_MS
) / 1000
CACHE_MESSAGE_PATTERN = re.compile(
    r"^Prepared Game Night spin \| "
    r"(multiplayer|singleplayer) \| winner (\d+)$"
)
CACHE_FILENAME_PATTERN = re.compile(
    r"^game-night-wheel-(multiplayer|singleplayer)-"
    r"([0-9a-f]{20})\.gif$",
    re.IGNORECASE,
)
SESSION_CACHE_FILENAME_PATTERN = re.compile(
    r"^game-night-session-(\d+)-(\d+)-"
    r"([0-9a-f]{20})\.gif$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PreparedSpin:
    winning_game: tuple
    wheel_type: str
    object_name: str
    image_url: str
    duration_seconds: float
    created_at: float
    oracle_image_url: str | None = None
    cache_message_id: int | None = None
    session_id: int | None = None
    generation: int | None = None
    player_count: int | None = None

    @property
    def winner_id(self) -> int:
        return int(
            self.winning_game[0]
        )


@dataclass
class SessionSpinPool:
    session_id: int
    generation: int
    player_count: int
    include_unverified: bool
    use_normal_wheel: bool
    eligible_game_ids: frozenset[int]
    eligibility: dict
    ready: deque
    target_size: int = SESSION_SPIN_POOL_SIZE


def _public_object_url(
    *,
    namespace: str,
    bucket: str,
    region: str,
    object_name: str,
) -> str:
    return (
        f"https://objectstorage.{region}.oraclecloud.com"
        f"/n/{quote(namespace, safe='')}"
        f"/b/{quote(bucket, safe='')}"
        f"/o/{quote(object_name, safe='/')}"
    )


class PreparedSpinManager:
    """Keep buffered spins ready on Oracle and Discord's CDN."""

    def __init__(
        self,
        bot,
    ) -> None:
        self.bot = bot
        self.namespace = OCI_SPIN_NAMESPACE
        self.bucket = OCI_SPIN_BUCKET
        self.region = OCI_SPIN_REGION
        self.cache_channel_id = SPIN_CACHE_CHANNEL_ID
        configured_values = (
            self.namespace,
            self.bucket,
            self.region,
            self.cache_channel_id,
        )
        self.enabled = all(
            configured_values
        )
        self._ready = {
            wheel_type: deque()
            for wheel_type in PREPARED_WHEEL_TYPES
        }
        self._session_pools: dict[int, SessionSpinPool] = {}
        self._ready_lock = asyncio.Lock()
        self._client_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()
        self._worker_task = None
        self._cleanup_tasks = set()
        self._known_object_names = set()
        self._known_cache_message_ids = set()
        self._recent_winner_ids = {}
        self._wheel_game_ids = None
        self._library_generation = 0
        self._next_library_check_at = 0.0
        self._client = None
        self._cache_channel = None
        self._closed = False

        if (
            any(configured_values)
            and not self.enabled
        ):
            LOGGER.warning(
                "Oracle prepared spins are disabled because "
                "OCI_SPIN_NAMESPACE, OCI_SPIN_BUCKET and "
                "OCI_SPIN_REGION plus SPIN_CACHE_CHANNEL_ID "
                "must all be configured."
            )

    def start(self) -> None:
        if not self.enabled:
            LOGGER.info(
                "Oracle prepared spins are disabled; "
                "Discord attachment delivery remains available"
            )
            return

        if self._worker_task is not None:
            return

        self._worker_task = asyncio.create_task(
            self._worker(),
            name="prepared-spin-worker",
        )
        LOGGER.info(
            "Oracle prepared spins enabled: bucket=%s "
            "region=%s cache_channel=%s pool=%s per wheel",
            self.bucket,
            self.region,
            self.cache_channel_id,
            PREPARED_SPIN_POOL_SIZE,
        )

    async def close(self) -> None:
        self._closed = True
        self._wake_event.set()

        if self._worker_task is not None:
            self._worker_task.cancel()
            await asyncio.gather(
                self._worker_task,
                return_exceptions=True,
            )
            self._worker_task = None

        cleanup_tasks = tuple(
            self._cleanup_tasks
        )

        for task in cleanup_tasks:
            task.cancel()

        if cleanup_tasks:
            await asyncio.gather(
                *cleanup_tasks,
                return_exceptions=True,
            )

        # Ready Discord messages and Oracle objects deliberately survive a
        # normal restart. The next process validates and restores them.

    async def acquire(
        self,
        wheel_type: str,
        *,
        exclude_game_id: int | None = None,
    ) -> PreparedSpin | None:
        if (
            not self.enabled
            or self._client is None
        ):
            return None

        expired_items = []

        async with self._ready_lock:
            item, expired_items = self._take_ready_locked(
                wheel_type,
                exclude_game_id=exclude_game_id,
            )

        for expired_item in expired_items:
            self._schedule_delete(
                expired_item.object_name,
                cache_message_id=(
                    expired_item.cache_message_id
                ),
                delay_seconds=0,
            )

        if item is None:
            self._wake_event.set()
            return None

        self._schedule_mark_consumed(item)
        self._schedule_delete(
            item.object_name,
            cache_message_id=item.cache_message_id,
            delay_seconds=(
                PREPARED_SPIN_DELETE_DELAY_SECONDS
            ),
        )
        self._recent_winner_ids[
            item.winner_id
        ] = (
            time.monotonic()
            + PREPARED_SPIN_RECENT_WINNER_SECONDS
        )
        self._wake_event.set()
        LOGGER.info(
            "Prepared spin acquired: wheel=%s winner_id=%s "
            "age=%.1fs url_ready=true",
            wheel_type,
            item.winner_id,
            time.monotonic() - item.created_at,
        )
        return item

    async def configure_session_pool(
        self,
        session_id: int,
        *,
        generation: int,
        player_count: int,
        include_unverified: bool = False,
        use_normal_wheel: bool = False,
    ) -> dict:
        """Create or replace one temporary player-count-aware pool."""

        eligibility = await get_session_wheel_game_ids(
            player_count,
            include_unverified=include_unverified,
            use_normal_wheel=use_normal_wheel,
        )
        removed_items = []
        clean_session_id = int(session_id)
        pool = SessionSpinPool(
            session_id=clean_session_id,
            generation=int(generation),
            player_count=int(eligibility["player_count"]),
            include_unverified=bool(include_unverified),
            use_normal_wheel=bool(use_normal_wheel),
            eligible_game_ids=frozenset(
                eligibility["game_ids"]
            ),
            eligibility=eligibility,
            ready=deque(),
        )

        async with self._ready_lock:
            existing = self._session_pools.get(
                clean_session_id
            )

            if existing is not None:
                removed_items.extend(existing.ready)

            self._session_pools[clean_session_id] = pool

        for item in removed_items:
            self._schedule_delete(
                item.object_name,
                cache_message_id=item.cache_message_id,
                delay_seconds=0,
            )

        self._wake_event.set()
        LOGGER.info(
            "Session spin pool configured: session=%s generation=%s "
            "players=%s eligible=%s unverified=%s normal_wheel=%s",
            clean_session_id,
            pool.generation,
            pool.player_count,
            eligibility["eligible_count"],
            eligibility["unverified_count"],
            pool.use_normal_wheel,
        )
        return await self.get_session_pool_status(
            clean_session_id
        )

    async def get_session_pool_status(
        self,
        session_id: int,
    ) -> dict:
        async with self._ready_lock:
            pool = self._session_pools.get(
                int(session_id)
            )

            if pool is None:
                return {
                    "exists": False,
                    "ready_count": 0,
                    "target_size": SESSION_SPIN_POOL_SIZE,
                }

            return {
                "exists": True,
                "session_id": pool.session_id,
                "generation": pool.generation,
                "player_count": pool.player_count,
                "ready_count": len(pool.ready),
                "target_size": pool.target_size,
                **{
                    key: value
                    for key, value in pool.eligibility.items()
                    if key != "game_ids"
                },
            }

    async def acquire_session(
        self,
        session_id: int,
        *,
        generation: int,
        exclude_game_id: int | None = None,
    ) -> PreparedSpin | None:
        if (
            not self.enabled
            or self._client is None
        ):
            return None

        clean_session_id = int(session_id)
        expired_items = []

        async with self._ready_lock:
            pool = self._session_pools.get(
                clean_session_id
            )

            if (
                pool is None
                or pool.generation != int(generation)
            ):
                return None

            item, expired_items = self._take_from_queue_locked(
                pool.ready,
                exclude_game_id=exclude_game_id,
            )

        for expired_item in expired_items:
            self._schedule_delete(
                expired_item.object_name,
                cache_message_id=expired_item.cache_message_id,
                delay_seconds=0,
            )

        if item is None:
            self._wake_event.set()
            return None

        self._schedule_mark_consumed(item)
        self._schedule_delete(
            item.object_name,
            cache_message_id=item.cache_message_id,
            delay_seconds=PREPARED_SPIN_DELETE_DELAY_SECONDS,
        )
        self._recent_winner_ids[item.winner_id] = (
            time.monotonic()
            + PREPARED_SPIN_RECENT_WINNER_SECONDS
        )
        self._wake_event.set()
        self._schedule_session_card_refresh(
            clean_session_id
        )
        LOGGER.info(
            "Session spin acquired: session=%s generation=%s "
            "players=%s winner_id=%s",
            clean_session_id,
            generation,
            item.player_count,
            item.winner_id,
        )
        return item

    async def remove_session_pool(
        self,
        session_id: int,
    ) -> int:
        """Delete all unused prepared files owned by one session."""

        async with self._ready_lock:
            pool = self._session_pools.pop(
                int(session_id),
                None,
            )
            removed_items = (
                list(pool.ready)
                if pool is not None
                else []
            )

        for item in removed_items:
            self._schedule_delete(
                item.object_name,
                cache_message_id=item.cache_message_id,
                delay_seconds=0,
            )

        self._wake_event.set()
        return len(removed_items)

    async def invalidate_game(
        self,
        game_id: int,
    ) -> int:
        """Discard prepared results whose winner is a specific game."""

        removed_items = []
        clean_game_id = int(game_id)

        async with self._ready_lock:
            for queue in self._ready.values():
                for item in tuple(queue):
                    if item.winner_id == clean_game_id:
                        queue.remove(item)
                        removed_items.append(item)

            for pool in self._session_pools.values():
                for item in tuple(pool.ready):
                    if item.winner_id == clean_game_id:
                        pool.ready.remove(item)
                        removed_items.append(item)

        for item in removed_items:
            self._schedule_delete(
                item.object_name,
                cache_message_id=item.cache_message_id,
                delay_seconds=0,
            )

        if removed_items:
            self._wake_event.set()

        return len(removed_items)

    async def invalidate_library(self) -> int:
        """Discard every prepared result after wheel membership changes."""

        removed_items = []

        async with self._ready_lock:
            self._library_generation += 1
            self._wheel_game_ids = None

            for queue in self._ready.values():
                removed_items.extend(queue)
                queue.clear()

        for item in removed_items:
            self._schedule_delete(
                item.object_name,
                cache_message_id=item.cache_message_id,
                delay_seconds=0,
            )

        self._next_library_check_at = 0.0
        self._wake_event.set()
        LOGGER.info(
            "Prepared-spin library invalidated; rebuilding both "
            "wheel pools without %s cached results",
            len(removed_items),
        )
        return len(removed_items)

    def _take_ready_locked(
        self,
        wheel_type: str,
        *,
        exclude_game_id: int | None,
    ) -> tuple[PreparedSpin | None, list[PreparedSpin]]:
        queue = self._ready.get(
            wheel_type
        )

        if queue is None:
            return None, []

        return self._take_from_queue_locked(
            queue,
            exclude_game_id=exclude_game_id,
        )

    def _take_from_queue_locked(
        self,
        queue: deque,
        *,
        exclude_game_id: int | None,
    ) -> tuple[PreparedSpin | None, list[PreparedSpin]]:
        now = time.monotonic()
        expired_items = []

        for item in tuple(queue):
            if (
                now - item.created_at
                > PREPARED_SPIN_MAX_AGE_SECONDS
            ):
                queue.remove(item)
                expired_items.append(item)

        for item in queue:
            if (
                exclude_game_id is None
                or item.winner_id != int(exclude_game_id)
            ):
                queue.remove(item)
                return item, expired_items

        return None, expired_items

    async def _worker(self) -> None:
        try:
            while not self._closed:
                if self._client is None:
                    try:
                        self._client = await asyncio.to_thread(
                            self._create_client
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOGGER.exception(
                            "Could not authenticate the Oracle "
                            "instance principal; retrying in %ss",
                            PREPARED_SPIN_RETRY_SECONDS,
                        )
                        await self._wait_for_wake(
                            PREPARED_SPIN_RETRY_SECONDS
                        )
                        continue

                    LOGGER.info(
                        "Oracle Object Storage client authenticated "
                        "with the VM instance principal"
                    )

                if self._cache_channel is None:
                    try:
                        await self._prepare_cache_channel()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOGGER.exception(
                            "Could not initialise Discord spin cache "
                            "channel %s; retrying in %ss",
                            self.cache_channel_id,
                            PREPARED_SPIN_RETRY_SECONDS,
                        )
                        await self._wait_for_wake(
                            PREPARED_SPIN_RETRY_SECONDS
                        )
                        continue

                made_progress = False

                try:
                    await self._refresh_pool_state()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception(
                        "Could not check the prepared-spin game "
                        "library; retrying in %ss",
                        PREPARED_SPIN_RETRY_SECONDS,
                    )
                    await self._wait_for_wake(
                        PREPARED_SPIN_RETRY_SECONDS
                    )
                    continue

                session_pool = (
                    await self._next_session_pool_needing_item()
                )

                if session_pool is not None:
                    try:
                        item = await self._prepare_session_item(
                            session_pool
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOGGER.exception(
                            "Could not prepare session spin for "
                            "session %s; retrying in %ss",
                            session_pool.session_id,
                            PREPARED_SPIN_RETRY_SECONDS,
                        )
                        await self._wait_for_wake(
                            PREPARED_SPIN_RETRY_SECONDS
                        )
                        continue

                    if item is not None:
                        stale_item = False

                        async with self._ready_lock:
                            current_pool = self._session_pools.get(
                                session_pool.session_id
                            )
                            stale_item = (
                                current_pool is None
                                or current_pool.generation
                                != session_pool.generation
                            )

                            if not stale_item:
                                current_pool.ready.append(item)

                        if stale_item:
                            self._schedule_delete(
                                item.object_name,
                                cache_message_id=item.cache_message_id,
                                delay_seconds=0,
                            )
                        else:
                            made_progress = True
                            self._schedule_session_card_refresh(
                                session_pool.session_id
                            )

                if made_progress:
                    continue

                for wheel_type in PREPARED_WHEEL_TYPES:
                    if not await self._needs_item(
                        wheel_type
                    ):
                        continue

                    library_generation = (
                        self._library_generation
                    )

                    try:
                        item = await self._prepare_item(
                            wheel_type
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOGGER.exception(
                            "Could not prepare an Oracle/Discord-hosted "
                            "%s wheel; retrying in %ss",
                            wheel_type,
                            PREPARED_SPIN_RETRY_SECONDS,
                        )
                        await self._wait_for_wake(
                            PREPARED_SPIN_RETRY_SECONDS
                        )
                        break

                    if item is None:
                        continue

                    stale_item = False

                    async with self._ready_lock:
                        stale_item = (
                            library_generation
                            != self._library_generation
                        )

                        if not stale_item:
                            self._ready[
                                wheel_type
                            ].append(item)

                    if stale_item:
                        self._schedule_delete(
                            item.object_name,
                            cache_message_id=(
                                item.cache_message_id
                            ),
                            delay_seconds=0,
                        )
                        continue

                    made_progress = True

                if made_progress:
                    continue

                self._wake_event.clear()

                if await self._any_pool_needs_item():
                    await self._wait_for_wake(
                        PREPARED_SPIN_RETRY_SECONDS
                    )
                else:
                    await self._wait_for_wake(
                        PREPARED_SPIN_LIBRARY_CHECK_SECONDS
                    )

        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                "Oracle prepared-spin worker stopped unexpectedly; "
                "normal Discord spin delivery will remain available"
            )

    async def _needs_item(
        self,
        wheel_type: str,
    ) -> bool:
        async with self._ready_lock:
            return (
                len(self._ready[wheel_type])
                < PREPARED_SPIN_POOL_SIZE
            )

    async def _next_session_pool_needing_item(
        self,
    ) -> SessionSpinPool | None:
        async with self._ready_lock:
            for pool in self._session_pools.values():
                if (
                    pool.eligible_game_ids
                    and len(pool.ready) < pool.target_size
                ):
                    return SessionSpinPool(
                        session_id=pool.session_id,
                        generation=pool.generation,
                        player_count=pool.player_count,
                        include_unverified=pool.include_unverified,
                        use_normal_wheel=pool.use_normal_wheel,
                        eligible_game_ids=pool.eligible_game_ids,
                        eligibility=dict(pool.eligibility),
                        ready=deque(pool.ready),
                        target_size=pool.target_size,
                    )

        return None

    async def _any_pool_needs_item(self) -> bool:
        async with self._ready_lock:
            global_pool_needs_item = any(
                len(self._ready[wheel_type])
                < PREPARED_SPIN_POOL_SIZE
                for wheel_type in PREPARED_WHEEL_TYPES
            )
            session_pool_needs_item = any(
                pool.eligible_game_ids
                and len(pool.ready) < pool.target_size
                for pool in self._session_pools.values()
            )
            return (
                global_pool_needs_item
                or session_pool_needs_item
            )

    async def _refresh_pool_state(self) -> bool:
        """Rebuild pools when wheel membership changes or items age out."""

        now = time.monotonic()

        if now < self._next_library_check_at:
            return False

        self._next_library_check_at = (
            now + PREPARED_SPIN_LIBRARY_CHECK_SECONDS
        )
        current_game_ids = await get_wheel_game_ids()
        removed_items = []
        changed_wheels = []

        async with self._ready_lock:
            if self._wheel_game_ids is None:
                self._wheel_game_ids = current_game_ids
            else:
                changed_wheels = [
                    wheel_type
                    for wheel_type in PREPARED_WHEEL_TYPES
                    if current_game_ids[wheel_type]
                    != self._wheel_game_ids[wheel_type]
                ]

                for wheel_type in changed_wheels:
                    removed_items.extend(
                        self._ready[wheel_type]
                    )
                    self._ready[wheel_type].clear()

                self._wheel_game_ids = current_game_ids

            for wheel_type in PREPARED_WHEEL_TYPES:
                for item in tuple(
                    self._ready[wheel_type]
                ):
                    if (
                        now - item.created_at
                        > PREPARED_SPIN_MAX_AGE_SECONDS
                    ):
                        self._ready[wheel_type].remove(item)
                        removed_items.append(item)

        for item in removed_items:
            self._schedule_delete(
                item.object_name,
                cache_message_id=item.cache_message_id,
                delay_seconds=0,
            )

        if changed_wheels:
            LOGGER.info(
                "Prepared-spin library changed; rebuilding "
                "wheel pools: %s",
                ", ".join(changed_wheels),
            )

        if removed_items:
            self._wake_event.set()

        return bool(changed_wheels or removed_items)

    async def _wait_for_wake(
        self,
        timeout_seconds: float,
    ) -> None:
        self._wake_event.clear()

        try:
            await asyncio.wait_for(
                self._wake_event.wait(),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            pass

    async def _prepare_cache_channel(self) -> None:
        channel = self.bot.get_channel(
            self.cache_channel_id
        )

        if channel is None:
            channel = await self.bot.fetch_channel(
                self.cache_channel_id
            )

        if (
            not hasattr(channel, "send")
            or not hasattr(channel, "history")
        ):
            raise RuntimeError(
                "SPIN_CACHE_CHANNEL_ID must identify a text channel."
            )

        self._cache_channel = channel
        restored, stale_deleted = (
            await self._restore_cache_messages()
        )
        LOGGER.info(
            "Discord spin cache channel ready: channel=%s "
            "restored=%s stale_messages_deleted=%s",
            self.cache_channel_id,
            restored,
            stale_deleted,
        )

    async def _restore_cache_messages(
        self,
    ) -> tuple[int, int]:
        current_game_ids = await get_wheel_game_ids()
        candidates = []
        stale_messages = []
        now_utc = discord.utils.utcnow()

        async for message in self._cache_channel.history(
            limit=200,
            oldest_first=False,
        ):
            if (
                message.author != self.bot.user
                or not message.attachments
            ):
                continue

            attachment = message.attachments[0]
            filename_match = CACHE_FILENAME_PATTERN.fullmatch(
                str(attachment.filename or "")
            )
            session_filename_match = (
                SESSION_CACHE_FILENAME_PATTERN.fullmatch(
                    str(attachment.filename or "")
                )
            )
            content_match = CACHE_MESSAGE_PATTERN.fullmatch(
                str(message.content or "")
            )
            object_name = None

            if filename_match:
                filename_wheel, token = filename_match.groups()
                object_name = (
                    f"prepared/{filename_wheel.casefold()}/"
                    f"game-night-wheel-{token.casefold()}.gif"
                )
            elif session_filename_match:
                (
                    session_id,
                    generation,
                    token,
                ) = session_filename_match.groups()
                object_name = (
                    f"prepared/session/{int(session_id)}/"
                    f"generation-{int(generation)}/"
                    f"game-night-wheel-{token.casefold()}.gif"
                )

            age_seconds = max(
                (
                    now_utc
                    - message.created_at
                ).total_seconds(),
                0.0,
            )

            if (
                content_match is None
                or filename_match is None
                or age_seconds
                > PREPARED_SPIN_MAX_AGE_SECONDS
            ):
                stale_messages.append(
                    (message, object_name)
                )
                continue

            wheel_type, winner_id_text = (
                content_match.groups()
            )
            filename_wheel = filename_match.group(1).casefold()
            winner_id = int(winner_id_text)

            if (
                filename_wheel != wheel_type
                or winner_id
                not in current_game_ids[wheel_type]
            ):
                stale_messages.append(
                    (message, object_name)
                )
                continue

            candidates.append(
                {
                    "message": message,
                    "attachment": attachment,
                    "wheel_type": wheel_type,
                    "winner_id": winner_id,
                    "object_name": object_name,
                    "age_seconds": age_seconds,
                }
            )

        games_by_id = await get_spin_games_by_ids(
            candidate["winner_id"]
            for candidate in candidates
        )
        restored = 0

        async with self._ready_lock:
            for candidate in candidates:
                wheel_type = candidate["wheel_type"]
                winning_game = games_by_id.get(
                    candidate["winner_id"]
                )

                if (
                    winning_game is None
                    or len(self._ready[wheel_type])
                    >= PREPARED_SPIN_POOL_SIZE
                ):
                    stale_messages.append(
                        (
                            candidate["message"],
                            candidate["object_name"],
                        )
                    )
                    continue

                object_name = candidate["object_name"]
                cache_message_id = int(
                    candidate["message"].id
                )
                item = PreparedSpin(
                    winning_game=tuple(winning_game),
                    wheel_type=wheel_type,
                    object_name=object_name,
                    image_url=candidate["attachment"].url,
                    duration_seconds=(
                        PREPARED_SPIN_DURATION_SECONDS
                    ),
                    created_at=(
                        time.monotonic()
                        - candidate["age_seconds"]
                    ),
                    oracle_image_url=_public_object_url(
                        namespace=self.namespace,
                        bucket=self.bucket,
                        region=self.region,
                        object_name=object_name,
                    ),
                    cache_message_id=cache_message_id,
                )
                self._ready[wheel_type].append(item)
                self._known_object_names.add(object_name)
                self._known_cache_message_ids.add(
                    cache_message_id
                )
                restored += 1

            self._wheel_game_ids = current_game_ids

        for message, object_name in stale_messages:
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except discord.DiscordException:
                LOGGER.exception(
                    "Could not delete stale prepared-spin "
                    "cache message %s",
                    message.id,
                )

            if object_name:
                await self._delete_object(object_name)

        return restored, len(stale_messages)

    async def _prepare_item(
        self,
        wheel_type: str,
    ) -> PreparedSpin | None:
        winning_game = await self._choose_winner(
            wheel_type
        )

        if winning_game is None:
            return None

        token = secrets.token_hex(10)
        object_name = (
            f"prepared/{wheel_type}/"
            f"game-night-wheel-{token}.gif"
        )
        gif_path = (
            SPIN_GIF_TEMP_DIRECTORY
            / f"prepared-{wheel_type}-{token}.gif"
        )
        preparation_started_at = time.perf_counter()
        object_uploaded = False
        cache_message_id = None

        try:
            rendered = await render_spin_gif_file(
                winning_game=winning_game,
                wheel_type=wheel_type,
                session=self.bot.http_session,
                output_path=gif_path,
            )
            upload_started_at = time.perf_counter()
            await self._upload_file(
                gif_path,
                object_name,
            )
            oracle_upload_seconds = (
                time.perf_counter()
                - upload_started_at
            )
            object_uploaded = True
            self._known_object_names.add(
                object_name
            )

            cache_upload_started_at = time.perf_counter()
            (
                cache_message_id,
                cache_image_url,
            ) = await self._upload_cache_file(
                gif_path,
                wheel_type=wheel_type,
                winner_id=int(winning_game[0]),
                token=token,
            )
            cache_upload_seconds = (
                time.perf_counter()
                - cache_upload_started_at
            )

        except BaseException:
            if cache_message_id is not None:
                await self._delete_cache_message(
                    cache_message_id
                )

            if object_uploaded:
                await self._delete_object(
                    object_name
                )

            raise

        finally:
            await asyncio.to_thread(
                gif_path.unlink,
                missing_ok=True,
            )

        oracle_image_url = _public_object_url(
            namespace=self.namespace,
            bucket=self.bucket,
            region=self.region,
            object_name=object_name,
        )
        item = PreparedSpin(
            winning_game=tuple(winning_game),
            wheel_type=wheel_type,
            object_name=object_name,
            image_url=cache_image_url,
            duration_seconds=rendered.duration_seconds,
            created_at=time.monotonic(),
            oracle_image_url=oracle_image_url,
            cache_message_id=cache_message_id,
        )
        LOGGER.info(
            "Prepared spin ready: wheel=%s winner_id=%s "
            "frames=%s render=%.3fs oracle_upload=%.3fs "
            "discord_cache_upload=%.3fs size=%.2fMiB total=%.3fs",
            wheel_type,
            item.winner_id,
            rendered.frame_count,
            rendered.render_seconds,
            oracle_upload_seconds,
            cache_upload_seconds,
            rendered.gif_size_bytes / (1024 * 1024),
            time.perf_counter() - preparation_started_at,
        )
        return item

    async def _prepare_session_item(
        self,
        pool: SessionSpinPool,
    ) -> PreparedSpin | None:
        winning_game = await self._choose_session_winner(
            pool
        )

        if winning_game is None:
            return None

        token = secrets.token_hex(10)
        object_name = (
            f"prepared/session/{pool.session_id}/"
            f"generation-{pool.generation}/"
            f"game-night-wheel-{token}.gif"
        )
        gif_path = (
            SPIN_GIF_TEMP_DIRECTORY
            / (
                f"prepared-session-{pool.session_id}-"
                f"{pool.generation}-{token}.gif"
            )
        )
        preparation_started_at = time.perf_counter()
        object_uploaded = False
        cache_message_id = None

        try:
            rendered = await render_spin_gif_file(
                winning_game=winning_game,
                wheel_type="multiplayer",
                session=self.bot.http_session,
                output_path=gif_path,
                eligible_game_ids=pool.eligible_game_ids,
            )
            upload_started_at = time.perf_counter()
            await self._upload_file(
                gif_path,
                object_name,
            )
            oracle_upload_seconds = (
                time.perf_counter()
                - upload_started_at
            )
            object_uploaded = True
            self._known_object_names.add(object_name)

            cache_upload_started_at = time.perf_counter()
            (
                cache_message_id,
                cache_image_url,
            ) = await self._upload_cache_file(
                gif_path,
                wheel_type="multiplayer",
                winner_id=int(winning_game[0]),
                token=token,
                session_id=pool.session_id,
                generation=pool.generation,
                player_count=pool.player_count,
            )
            cache_upload_seconds = (
                time.perf_counter()
                - cache_upload_started_at
            )

        except BaseException:
            if cache_message_id is not None:
                await self._delete_cache_message(
                    cache_message_id
                )

            if object_uploaded:
                await self._delete_object(object_name)

            raise

        finally:
            await asyncio.to_thread(
                gif_path.unlink,
                missing_ok=True,
            )

        item = PreparedSpin(
            winning_game=tuple(winning_game),
            wheel_type="multiplayer",
            object_name=object_name,
            image_url=cache_image_url,
            duration_seconds=rendered.duration_seconds,
            created_at=time.monotonic(),
            oracle_image_url=_public_object_url(
                namespace=self.namespace,
                bucket=self.bucket,
                region=self.region,
                object_name=object_name,
            ),
            cache_message_id=cache_message_id,
            session_id=pool.session_id,
            generation=pool.generation,
            player_count=pool.player_count,
        )
        LOGGER.info(
            "Prepared session spin ready: session=%s generation=%s "
            "players=%s winner_id=%s candidates=%s frames=%s "
            "render=%.3fs oracle_upload=%.3fs "
            "discord_cache_upload=%.3fs total=%.3fs",
            pool.session_id,
            pool.generation,
            pool.player_count,
            item.winner_id,
            rendered.candidate_count,
            rendered.frame_count,
            rendered.render_seconds,
            oracle_upload_seconds,
            cache_upload_seconds,
            time.perf_counter() - preparation_started_at,
        )
        return item

    async def _choose_session_winner(
        self,
        pool: SessionSpinPool,
    ):
        async with self._ready_lock:
            now = time.monotonic()
            self._recent_winner_ids = {
                game_id: expires_at
                for game_id, expires_at
                in self._recent_winner_ids.items()
                if expires_at > now
            }
            current_pool = self._session_pools.get(
                pool.session_id
            )
            reserved_ids = set(
                self._recent_winner_ids
            )

            if current_pool is not None:
                reserved_ids.update(
                    item.winner_id
                    for item in current_pool.ready
                )

        winning_game = None

        for attempt in range(8):
            winning_game = await get_smart_random_game_for_ids(
                pool.eligible_game_ids
            )

            if winning_game is None:
                return None

            if (
                int(winning_game[0]) not in reserved_ids
                or attempt == 7
            ):
                return winning_game

        return winning_game

    async def _choose_winner(
        self,
        wheel_type: str,
    ):
        picker = (
            get_smart_random_singleplayer_game
            if wheel_type == "singleplayer"
            else get_smart_random_game
        )

        async with self._ready_lock:
            now = time.monotonic()
            self._recent_winner_ids = {
                game_id: expires_at
                for game_id, expires_at
                in self._recent_winner_ids.items()
                if expires_at > now
            }
            reserved_ids = {
                item.winner_id
                for item in self._ready[wheel_type]
            }
            reserved_ids.update(
                self._recent_winner_ids
            )

        winning_game = None

        for attempt in range(8):
            winning_game = await picker()

            if winning_game is None:
                return None

            if (
                int(winning_game[0]) not in reserved_ids
                or attempt == 7
            ):
                return winning_game

        return winning_game

    def _create_client(self):
        import oci

        signer = (
            oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        )
        return oci.object_storage.ObjectStorageClient(
            config={
                "region": self.region,
            },
            signer=signer,
        )

    async def _upload_cache_file(
        self,
        gif_path: Path,
        *,
        wheel_type: str,
        winner_id: int,
        token: str,
        session_id: int | None = None,
        generation: int | None = None,
        player_count: int | None = None,
    ) -> tuple[int, str]:
        if self._cache_channel is None:
            raise RuntimeError(
                "Discord spin cache channel is not ready."
            )

        if session_id is None:
            cache_filename = (
                f"game-night-wheel-{wheel_type}-{token}.gif"
            )
            cache_content = (
                "Prepared Game Night spin | "
                f"{wheel_type} | winner {winner_id}"
            )
        else:
            cache_filename = (
                f"game-night-session-{int(session_id)}-"
                f"{int(generation)}-{token}.gif"
            )
            cache_content = (
                "Prepared session spin | "
                f"session {int(session_id)} | "
                f"generation {int(generation)} | "
                f"players {int(player_count)} | "
                f"winner {winner_id}"
            )

        try:
            message = await self._cache_channel.send(
                content=cache_content,
                file=discord.File(
                    gif_path,
                    filename=cache_filename,
                ),
            )
        except discord.DiscordException:
            self._cache_channel = None
            raise

        if not message.attachments:
            await message.delete()
            raise RuntimeError(
                "Discord accepted the cache message without "
                "returning its GIF attachment."
            )

        self._known_cache_message_ids.add(
            message.id
        )
        return (
            int(message.id),
            message.attachments[0].url,
        )

    async def _upload_file(
        self,
        gif_path: Path,
        object_name: str,
    ) -> None:
        async with self._client_lock:
            await asyncio.to_thread(
                self._upload_file_sync,
                gif_path,
                object_name,
            )

    def _upload_file_sync(
        self,
        gif_path: Path,
        object_name: str,
    ) -> None:
        with gif_path.open("rb") as gif_file:
            self._client.put_object(
                namespace_name=self.namespace,
                bucket_name=self.bucket,
                object_name=object_name,
                put_object_body=gif_file,
                content_length=gif_path.stat().st_size,
                content_type="image/gif",
                cache_control="public, max-age=3600, immutable",
            )

    def _schedule_mark_consumed(
        self,
        item: PreparedSpin,
    ) -> None:
        if item.cache_message_id is None:
            return

        task = asyncio.create_task(
            self._mark_cache_message_consumed(item),
            name="prepared-spin-mark-consumed",
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(
            self._cleanup_tasks.discard
        )

    def _schedule_session_card_refresh(
        self,
        session_id: int,
    ) -> None:
        sessions_cog = self.bot.get_cog("Sessions")

        if sessions_cog is None:
            return

        task = asyncio.create_task(
            sessions_cog.refresh_session_card(session_id),
            name="gaming-session-card-refresh",
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(
            self._cleanup_tasks.discard
        )

    async def _mark_cache_message_consumed(
        self,
        item: PreparedSpin,
    ) -> None:
        if self._cache_channel is None:
            return

        if item.session_id is None:
            content = (
                "Consumed Game Night spin | "
                f"{item.wheel_type} | winner {item.winner_id}"
            )
        else:
            content = (
                "Consumed session spin | "
                f"session {item.session_id} | "
                f"generation {item.generation} | "
                f"players {item.player_count} | "
                f"winner {item.winner_id}"
            )

        try:
            await self._cache_channel.get_partial_message(
                int(item.cache_message_id)
            ).edit(
                content=content
            )
        except discord.NotFound:
            pass
        except discord.DiscordException:
            LOGGER.exception(
                "Could not mark prepared spin cache message "
                "%s as consumed",
                item.cache_message_id,
            )

    def _schedule_delete(
        self,
        object_name: str,
        *,
        cache_message_id: int | None = None,
        delay_seconds: float,
    ) -> None:
        task = asyncio.create_task(
            self._delete_later(
                object_name,
                cache_message_id=cache_message_id,
                delay_seconds=delay_seconds,
            ),
            name="prepared-spin-cleanup",
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(
            self._cleanup_tasks.discard
        )

    async def _delete_later(
        self,
        object_name: str,
        *,
        cache_message_id: int | None,
        delay_seconds: float,
    ) -> None:
        await asyncio.sleep(
            max(delay_seconds, 0)
        )
        cleanup_operations = [
            self._delete_object(
                object_name
            )
        ]

        if cache_message_id is not None:
            cleanup_operations.append(
                self._delete_cache_message(
                    cache_message_id
                )
            )

        await asyncio.gather(
            *cleanup_operations,
            return_exceptions=True,
        )

    async def _delete_cache_message(
        self,
        message_id: int,
    ) -> None:
        if self._cache_channel is None:
            return

        try:
            await self._cache_channel.get_partial_message(
                int(message_id)
            ).delete()
        except discord.NotFound:
            pass
        except discord.DiscordException:
            LOGGER.exception(
                "Could not delete prepared spin cache message %s",
                message_id,
            )
            return

        self._known_cache_message_ids.discard(
            int(message_id)
        )

    async def _delete_object(
        self,
        object_name: str,
    ) -> None:
        if self._client is None:
            return

        try:
            async with self._client_lock:
                await asyncio.to_thread(
                    self._client.delete_object,
                    namespace_name=self.namespace,
                    bucket_name=self.bucket,
                    object_name=object_name,
                )
        except Exception as error:
            if (
                getattr(error, "status", None) == 404
                and getattr(error, "code", None)
                == "ObjectNotFound"
            ):
                self._known_object_names.discard(
                    object_name
                )
                return

            LOGGER.exception(
                "Could not delete prepared spin object %s",
                object_name,
            )
            return

        self._known_object_names.discard(
            object_name
        )
