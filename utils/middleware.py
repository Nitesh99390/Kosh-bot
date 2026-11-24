import time
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatMemberStatus
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    Message,
    TelegramObject,
)

from config import Config
from database import Database
from utils.cache import TTLCache

# Bounded FORCE_SUB_CHANNEL membership cache (maxsize=5000, ttl=300s)
_FORCE_SUB_CACHE: TTLCache[tuple[str, int], bool] = TTLCache(maxsize=5000, ttl=300.0)
# Preserves `/start <payload>` across Force-Subscribe verification so deep links resume cleanly
_PENDING_FSUB_START: TTLCache[int, str] = TTLCache(maxsize=2048, ttl=600.0)


def invalidate_force_sub_cache(channel_id_or_username: str, user_id: int) -> None:
    """Immediately invalidates cached force-sub membership when user taps Recheck."""
    key_str = channel_id_or_username.strip()
    _FORCE_SUB_CACHE.invalidate((key_str, user_id))


def set_pending_force_sub_start(user_id: int, payload: str) -> None:
    clean = (payload or "").strip()
    if clean:
        _PENDING_FSUB_START.set(user_id, clean)


def pop_pending_force_sub_start(user_id: int) -> Optional[str]:
    return _PENDING_FSUB_START.pop(user_id, None)


class GuardAndThrottleMiddleware(BaseMiddleware):
    """
    Production outer middleware inspired by top GitHub Aiogram v3 & FileStore bots:
    1. Anti-Flood / Throttling: prevents rapid button-mashing & spam (skips media albums).
    2. User Ban Guard: blocks banned users cleanly across messages, callbacks, and inline queries.
    3. Optional Force-Subscribe Gate: verifies membership in `FORCE_SUB_CHANNEL` with bounded TTLCache.
    """

    def __init__(self, rate_limit: float = 0.35, max_tracked_users: int = 4096) -> None:
        self.rate_limit = rate_limit
        self._hits: TTLCache[int, float] = TTLCache(
            maxsize=max_tracked_users,
            ttl=max(rate_limit * 4.0, 30.0),
        )

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)

        config: Config | None = data.get("config")
        db: Database | None = data.get("db")
        bot: Bot | None = data.get("bot")

        is_admin = bool(config and user.id in config.admin_ids)

        # 1. Lightweight anti-flood (NEVER drop media uploads so 100+ file drops never lose files)
        is_media_upload = isinstance(event, Message) and bool(
            getattr(event, "media_group_id", None)
            or getattr(event, "audio", None)
            or getattr(event, "document", None)
            or getattr(event, "video", None)
            or getattr(event, "video_note", None)
            or getattr(event, "voice", None)
            or getattr(event, "photo", None)
            or getattr(event, "animation", None)
            or getattr(event, "sticker", None)
        )
        if self.rate_limit > 0 and not is_admin and not is_media_upload:
            now = time.monotonic()
            prev = self._hits.get(user.id)
            if prev is not None and (now - prev) < self.rate_limit:
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer("⏳ Slow down a bit…")
                    except Exception:
                        pass
                elif isinstance(event, InlineQuery):
                    try:
                        await event.answer([], cache_time=1, is_personal=True)
                    except Exception:
                        pass
                return None
            self._hits.set(user.id, now)

        # 2. Banned user check (messages, callbacks, AND inline queries)
        if db and not is_admin and await db.is_user_banned(user.id):
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("🚫 Access restricted.", show_alert=True)
                except Exception:
                    pass
            elif isinstance(event, Message) and event.chat.type == "private":
                try:
                    await event.answer("🚫 <b>Access Restricted</b>")
                except Exception:
                    pass
            elif isinstance(event, InlineQuery):
                try:
                    await event.answer([], cache_time=5, is_personal=True)
                except Exception:
                    pass
            return None

        # 3. Optional Force-Subscribe Gate (private messages, private callbacks, and inline queries)
        if (
            config
            and config.force_sub_channel
            and bot
            and not is_admin
            and isinstance(event, (Message, CallbackQuery, InlineQuery))
        ):
            if isinstance(event, Message) and event.chat.type != "private":
                return await handler(event, data)
            if isinstance(event, CallbackQuery):
                cb_msg = event.message
                if not isinstance(cb_msg, Message) or cb_msg.chat.type != "private":
                    return await handler(event, data)
                if event.data == "fsub:check":
                    invalidate_force_sub_cache(config.force_sub_channel, user.id)
                    return await handler(event, data)

            joined = await check_force_sub(bot, config.force_sub_channel, user.id)
            if not joined:
                if isinstance(event, InlineQuery):
                    try:
                        await event.answer([], cache_time=5, is_personal=True)
                    except Exception:
                        pass
                    return None

                # Preserve `/start <payload>` so `fsub:check` can replay the deep link after joining
                if isinstance(event, Message) and event.text:
                    parts = event.text.strip().split(maxsplit=1)
                    if parts and parts[0].split("@", 1)[0].lower() == "/start" and len(parts) > 1:
                        set_pending_force_sub_start(user.id, parts[1])

                join_url = config.force_sub_url or (
                    f"https://t.me/{config.force_sub_channel.lstrip('@')}"
                    if config.force_sub_channel.startswith("@")
                    else "https://t.me/"
                )
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="📢 Join Channel",
                                url=join_url,
                                style="primary",
                            ),
                            InlineKeyboardButton(
                                text="✓ Joined",
                                callback_data="fsub:check",
                                style="success",
                            ),
                        ]
                    ]
                )
                prompt = (
                    "🔒 <b>Channel Membership Required</b>\n"
                    "<blockquote>Join our channel to unlock Vault &amp; Share Links.</blockquote>"
                )
                if isinstance(event, Message):
                    try:
                        await event.answer(prompt, reply_markup=kb)
                    except Exception:
                        pass
                else:
                    try:
                        await event.answer("Join the channel first!", show_alert=True)
                    except Exception:
                        pass
                    if isinstance(event.message, Message):
                        try:
                            await event.message.answer(prompt, reply_markup=kb)
                        except Exception:
                            pass
                return None

        return await handler(event, data)


async def check_force_sub(
    bot: Bot,
    channel_id_or_username: str,
    user_id: int,
    *,
    bypass_cache: bool = False,
) -> bool:
    cache_key = (channel_id_or_username.strip(), user_id)
    if bypass_cache:
        _FORCE_SUB_CACHE.invalidate(cache_key)
    else:
        cached = _FORCE_SUB_CACHE.get(cache_key)
        if cached is not None:
            return cached

    target: int | str = channel_id_or_username.strip()
    if isinstance(target, str) and target.lstrip("-").isdigit():
        target = int(target)
    try:
        member = await bot.get_chat_member(chat_id=target, user_id=user_id)
        if member.status in {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.MEMBER,
        }:
            is_member = True
        elif member.status == ChatMemberStatus.RESTRICTED:
            is_member = bool(getattr(member, "is_member", False))
        else:
            is_member = False
        # Cache positive membership for full TTL (300s), negative briefly (15s) to absorb spam clicks
        _FORCE_SUB_CACHE.set(cache_key, is_member, ttl=300.0 if is_member else 15.0)
        return is_member
    except Exception:
        # Fail-open if misconfigured so the bot never bricks itself
        return True
