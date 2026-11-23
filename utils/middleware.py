import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatMemberStatus
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from config import Config
from database import Database
from utils.cache import TTLCache

# Bounded FORCE_SUB_CHANNEL membership cache (maxsize=5000, ttl=300s)
_FORCE_SUB_CACHE: TTLCache[tuple[str, int], bool] = TTLCache(maxsize=5000, ttl=300.0)


def invalidate_force_sub_cache(channel_id_or_username: str, user_id: int) -> None:
    """Immediately invalidates cached force-sub membership when user taps Recheck."""
    key_str = channel_id_or_username.strip()
    _FORCE_SUB_CACHE.invalidate((key_str, user_id))


class GuardAndThrottleMiddleware(BaseMiddleware):
    """
    Production outer middleware inspired by top GitHub Aiogram v3 & FileStore bots:
    1. Anti-Flood / Throttling: prevents rapid button-mashing & spam (skips media albums).
    2. User Ban Guard: blocks banned users cleanly (uses cached user row).
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
        is_media_upload = not isinstance(event, CallbackQuery) and bool(
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
                return None
            self._hits.set(user.id, now)

        # 2. Banned user check
        if db and not is_admin and await db.is_user_banned(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Access restricted.", show_alert=True)
            elif isinstance(event, Message) and event.chat.type == "private":
                await event.answer("🚫 <b>Access Restricted</b>")
            return None

        # 3. Optional Force-Subscribe Gate (only for private messages & callbacks)
        if (
            config
            and config.force_sub_channel
            and bot
            and not is_admin
            and isinstance(event, (Message, CallbackQuery))
        ):
            if isinstance(event, CallbackQuery) and event.data == "fsub:check":
                invalidate_force_sub_cache(config.force_sub_channel, user.id)
                return await handler(event, data)

            joined = await check_force_sub(bot, config.force_sub_channel, user.id)
            if not joined:
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
                    "<blockquote>Join our channel to unlock Vault & Share Links.</blockquote>"
                )
                if isinstance(event, Message):
                    await event.answer(prompt, reply_markup=kb)
                else:
                    await event.answer("Join the channel first!", show_alert=True)
                    if event.message:
                        await event.message.answer(prompt, reply_markup=kb)
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
        is_member = member.status in {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.RESTRICTED,
        }
        # Cache positive membership for full TTL (300s), negative briefly (15s) to absorb spam clicks
        _FORCE_SUB_CACHE.set(cache_key, is_member, ttl=300.0 if is_member else 15.0)
        return is_member
    except Exception:
        # Fail-open if misconfigured so the bot never bricks itself
        return True
