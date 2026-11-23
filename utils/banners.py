import asyncio
import hashlib
import logging
import os
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from config import Config
from database import Database

logger = logging.getLogger("koshbox.banners")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANNERS_DIR = os.path.join(BASE_DIR, "assets", "banners")

# Registry of custom KoshBox 3D illustration banners (PostBot-style hero cards)
BANNER_FILES: dict[str, str] = {
    "welcome": os.path.join(BANNERS_DIR, "welcome.jpg"),
    "upload": os.path.join(BANNERS_DIR, "upload.jpg"),
    "guide": os.path.join(BANNERS_DIR, "guide.jpg"),
}

BANNER_LABELS: dict[str, str] = {
    "welcome": "👋 KoshBox Welcome & Vault Dashboard Banner",
    "upload": "📤 KoshBox Upload & Archive Banner",
    "guide": "🔍 KoshBox Inline Search & Sync Guide Banner",
}

# Per-key asyncio locks + process-lifetime SHA-256 cache so banners never re-hash disk files per click
_ASSET_LOCKS: dict[str, asyncio.Lock] = {}
_LOCAL_SHA_CACHE: dict[str, tuple[float, str]] = {}


def _get_lock(asset_key: str) -> asyncio.Lock:
    lock = _ASSET_LOCKS.get(asset_key)
    if lock is None:
        lock = asyncio.Lock()
        _ASSET_LOCKS[asset_key] = lock
    return lock


def compute_file_sha256(path: str) -> str:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = -1.0
    cached = _LOCAL_SHA_CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if mtime >= 0:
        _LOCAL_SHA_CACHE[path] = (mtime, digest)
    return digest


async def get_or_upload_banner_file_id(
    asset_key: str,
    bot: Bot,
    db: Database,
    config: Config,
    *,
    force_reupload: bool = False,
) -> Optional[str]:
    """
    Returns the cached Telegram `file_id` for a KoshBox banner.

    CRITICAL ARCHITECTURE:
    - Checks SQLite `ui_assets` first. If cached and SHA-256 matches the local file,
      returns the `file_id` immediately with ZERO disk/network upload from the server.
    - If not cached yet (first run or updated artwork), uploads ONCE from the server
      into `config.dump_channel_id`, stores `file_id` + `dump_message_id` + `sha256`
      in SQLite, and reuses that `file_id` forever.
    """
    path = BANNER_FILES.get(asset_key)
    if not path or not os.path.isfile(path):
        logger.warning("Banner file missing for key=%s at %s", asset_key, path)
        return None

    local_sha = compute_file_sha256(path)

    if not force_reupload:
        cached = await db.get_ui_asset(asset_key)
        if cached and cached.get("file_id") and cached.get("sha256") == local_sha:
            return cached["file_id"]

    async with _get_lock(asset_key):
        if not force_reupload:
            cached = await db.get_ui_asset(asset_key)
            if cached and cached.get("file_id") and cached.get("sha256") == local_sha:
                return cached["file_id"]

        label = BANNER_LABELS.get(asset_key, f"KoshBox UI Asset ({asset_key})")
        try:
            dumped = await bot.send_photo(
                chat_id=config.dump_channel_id,
                photo=FSInputFile(path),
                caption=(
                    f"🖼️ <b>{label}</b>\n"
                    f"<code>asset:{asset_key}</code> · <i>Cached for zero-upload bot UI</i>"
                ),
                disable_notification=True,
            )
        except Exception as exc:
            logger.warning(
                "Could not upload UI banner '%s' to Dump Channel (%s): %s",
                asset_key,
                config.dump_channel_id,
                exc,
            )
            # Fall back to any previously cached file_id if available
            fallback = await db.get_ui_asset(asset_key)
            return fallback["file_id"] if fallback else None

        if not dumped or not getattr(dumped, "photo", None):
            return None

        best_photo = dumped.photo[-1]
        saved = await db.upsert_ui_asset(
            asset_key=asset_key,
            file_id=best_photo.file_id,
            file_unique_id=best_photo.file_unique_id or "",
            dump_message_id=dumped.message_id,
            sha256=local_sha,
        )
        logger.info(
            "Cached UI banner '%s' in Dump Channel (msg_id=%s, file_id=%s...)",
            asset_key,
            dumped.message_id,
            best_photo.file_id[:18],
        )
        return saved["file_id"]


async def warmup_banners_in_dump_channel(
    bot: Bot,
    db: Database,
    config: Config,
    *,
    force_reupload: bool = False,
) -> dict[str, bool]:
    """
    Ensures all UI banner illustrations are uploaded ONCE to the Dump Channel
    at bot startup (or via Admin Console sync), so user interactions are 100% instant
    using cached Telegram `file_id`s.
    """
    status: dict[str, bool] = {}
    for key in BANNER_FILES:
        fid = await get_or_upload_banner_file_id(
            key, bot, db, config, force_reupload=force_reupload
        )
        status[key] = bool(fid)
    return status


async def send_banner_message(
    message: Message,
    asset_key: str,
    caption: str,
    reply_markup: Optional[InlineKeyboardMarkup],
    bot: Bot,
    db: Database,
    config: Config,
) -> Message:
    """
    Sends a PostBot-style visual card (cached photo from Dump Channel + HTML caption + inline keyboard).
    Gracefully falls back to clean HTML text if Dump Channel isn't reachable yet.
    """
    file_id = await get_or_upload_banner_file_id(asset_key, bot, db, config)
    if file_id:
        try:
            return await message.answer_photo(
                photo=file_id,
                caption=caption,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as exc:
            logger.warning(
                "Cached banner file_id invalid for '%s' (%s); refreshing once from Dump Channel.",
                asset_key,
                exc,
            )
            fresh_id = await get_or_upload_banner_file_id(
                asset_key, bot, db, config, force_reupload=True
            )
            if fresh_id:
                try:
                    return await message.answer_photo(
                        photo=fresh_id,
                        caption=caption,
                        reply_markup=reply_markup,
                    )
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("send_banner_message photo fallback (%s): %s", asset_key, exc)

    return await message.answer(
        caption,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


async def edit_or_send_banner(
    callback: CallbackQuery,
    asset_key: str,
    caption: str,
    reply_markup: Optional[InlineKeyboardMarkup],
    bot: Bot,
    db: Database,
    config: Config,
    *,
    answer_text: Optional[str] = None,
    answer_alert: bool = False,
    also_ack: bool = False,
) -> None:
    """
    Smoothly transitions an existing callback panel to a PostBot-style banner card:
    - If the current message already displays the SAME banner image (matched by `file_id` or `file_unique_id`),
      calls `edit_caption` instead of `edit_media` to avoid Telegram server-side media re-processing!
    - When `also_ack=True`, dispatches `callback.answer()` concurrently via `asyncio.gather` to save 1 full RTT.
    """
    if not callback.message:
        if also_ack:
            try:
                await callback.answer(answer_text, show_alert=answer_alert)
            except Exception:
                pass
        return

    file_id = await get_or_upload_banner_file_id(asset_key, bot, db, config)
    if not file_id:
        from utils.formatters import safe_edit_or_send

        await safe_edit_or_send(
            callback,
            caption,
            reply_markup,
            answer_text=answer_text,
            answer_alert=answer_alert,
            also_ack=also_ack,
        )
        return

    async def _do_ui_update() -> None:
        assert callback.message is not None
        photos = getattr(callback.message, "photo", None)
        if photos:
            cached_row = await db.get_ui_asset(asset_key)
            cached_fid = (cached_row or {}).get("file_id") or file_id
            cached_uniq = (cached_row or {}).get("file_unique_id") or ""
            same_photo = any(
                getattr(p, "file_id", None) == cached_fid
                or (cached_uniq and getattr(p, "file_unique_id", None) == cached_uniq)
                for p in photos
            )
            try:
                if same_photo and len(caption) <= 1024:
                    await callback.message.edit_caption(
                        caption=caption,
                        reply_markup=reply_markup,
                    )
                else:
                    await callback.message.edit_media(
                        media=InputMediaPhoto(media=file_id, caption=caption),
                        reply_markup=reply_markup,
                    )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
            except Exception as exc:
                logger.debug("edit_media/edit_caption fallback: %s", exc)

        # Transitioning from a text-only view back to a hero banner card
        sent = await send_banner_message(
            callback.message, asset_key, caption, reply_markup, bot, db, config
        )
        if sent and getattr(callback.message, "text", None):
            try:
                await callback.message.delete()
            except Exception:
                pass

    if also_ack:
        await asyncio.gather(
            callback.answer(answer_text, show_alert=answer_alert),
            _do_ui_update(),
            return_exceptions=True,
        )
    else:
        await _do_ui_update()
