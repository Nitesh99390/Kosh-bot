import asyncio
import html
import time
from collections.abc import Awaitable
from datetime import datetime, timezone
from typing import Any, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

MEDIA_ICONS = {
    "audio": "🎵",
    "voice": "🎙️",
    "video": "🎬",
    "video_note": "⭕",
    "document": "📄",
    "photo": "🖼️",
    "animation": "🎞️",
    "sticker": "🧩",
}

MEDIA_LABELS = {
    "audio": "Audio",
    "voice": "Voice",
    "video": "Video",
    "video_note": "Round",
    "document": "Doc",
    "photo": "Photo",
    "animation": "GIF",
    "sticker": "Sticker",
}

SORT_LABELS = {
    "new": "Newest",
    "top": "Popular",
    "az": "A–Z",
    "size": "Largest",
    "old": "Oldest",
}

SORT_ORDER = ["new", "top", "az", "size", "old"]

TTL_LABELS = {
    0: "Off",
    300: "5m",
    900: "15m",
    3600: "1h",
    7200: "2h",
    86400: "24h",
    604800: "7d",
}

# Official Telegram Bot API Built-in Free Full-Screen Message Effects (Private Chats)
EFFECT_PARTY = "5046509860389126442"  # 🎉 Confetti / Party
EFFECT_FIRE = "5104841245755180586"   # 🔥 Fire
EFFECT_THUMBS_UP = "5107584321108051014"  # 👍 Thumbs Up
EFFECT_HEART = "5159385139981059251"  # ❤️ Heart

BATCH_CHUNK_SIZE = 100

DIVIDER = "──────────────────────"

_AUTO_DELETE_TASKS: set[asyncio.Task] = set()


async def _await(awaitable: Awaitable[Any]) -> Any:
    """Wraps any awaitable (including unhashable aiogram 3 TelegramMethod objects) into a real coroutine."""
    return await awaitable


async def safe_gather(*tasks: Awaitable[Any]) -> list[Any]:
    """
    Runs awaitables concurrently via `asyncio.gather(..., return_exceptions=True)`,
    wrapping each item in a true coroutine so unhashable aiogram 3 Pydantic
    `TelegramMethod` instances (`AnswerCallbackQuery`, `EditMessageText`, etc.)
    never trigger `TypeError: unhashable type` inside `asyncio.gather()`.
    """
    return await asyncio.gather(*(_await(c) for c in tasks), return_exceptions=True)


def esc(text: Optional[str]) -> str:
    return html.escape(text or "")


def breadcrumb(*parts: str) -> str:
    """Subtle compact breadcrumb trail (e.g. Vault › Files)."""
    return f"<i>{' › '.join(parts)}</i>"


def toggle_label(enabled: bool, label: str) -> str:
    """Native-style crisp toggle indicator."""
    return f"{'✅' if enabled else '⬜'} {label}"


def format_ttl_short(seconds: Optional[int]) -> str:
    if not seconds or seconds <= 0:
        return "Off"
    if seconds in TTL_LABELS:
        return TTL_LABELS[seconds]
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def format_expiry_badge(expires_at: Optional[int]) -> str:
    if not expires_at:
        return "Never"
    rem = max(0, int(expires_at) - int(time.time()))
    if rem == 0:
        return "Expired"
    if rem < 3600:
        return f"{max(1, rem // 60)}m left"
    if rem < 86400:
        return f"{round(rem / 3600)}h left"
    return f"{round(rem / 86400)}d left"


def format_max_views_badge(views: int, max_views: int) -> str:
    cur = max(0, int(views or 0))
    lim = max(0, int(max_views or 0))
    if lim <= 0:
        return f"👁 {cur}×"
    return f"👁 {cur}/{lim}"


async def tg_retry_call(coro_factory, *args, max_retries: int = 4, **kwargs):
    """
    Executes a Telegram Bot API coroutine with automatic FloodWait (`TelegramRetryAfter`)
    backoff so 100+ file bursts or pack deliveries never crash or drop files.
    """
    for attempt in range(max_retries):
        try:
            return await coro_factory(*args, **kwargs)
        except TelegramRetryAfter as exc:
            wait_s = min(max(float(exc.retry_after), 0.5), 45.0) + 0.25
            if attempt + 1 >= max_retries:
                raise
            await asyncio.sleep(wait_s)
    return None


def copy_btn(text: str, value: str, style: Optional[str] = None) -> InlineKeyboardButton:
    """Native Telegram one-tap clipboard button (CopyTextButton)."""
    return InlineKeyboardButton(
        text=text,
        copy_text=CopyTextButton(text=value),
        style=style,
    )


def share_url_btn(text: str, link: str, title: str = "") -> InlineKeyboardButton:
    """Native Telegram forward/share sheet button."""
    from urllib.parse import quote

    url = f"https://t.me/share/url?url={quote(link)}"
    if title:
        url += f"&text={quote(title)}"
    return InlineKeyboardButton(text=text, url=url)


def next_sort_key(current: str) -> str:
    if current not in SORT_ORDER:
        return "new"
    idx = (SORT_ORDER.index(current) + 1) % len(SORT_ORDER)
    return SORT_ORDER[idx]


def format_size(num_bytes: int) -> str:
    if not num_bytes or num_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    val = float(num_bytes)
    for unit in units:
        if val < 1024 or unit == units[-1]:
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024
    return f"{num_bytes} B"


def format_duration(seconds: int) -> str:
    if not seconds or seconds <= 0:
        return ""
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def format_date(ts: Optional[int]) -> str:
    if not ts:
        return ""
    diff = max(0, int(time.time()) - int(ts))
    if diff < 60:
        return "now"
    if diff < 3600:
        return f"{diff // 60}m"
    if diff < 86400:
        return f"{diff // 3600}h"
    if diff < 86400 * 7:
        return f"{diff // 86400}d"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d")


def progress_bar(ratio: float, width: int = 8) -> str:
    filled = max(0, min(width, round(ratio * width)))
    return "▰" * filled + "▱" * (width - filled)


def cancel_kb(back_callback: str = "nav:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀ Cancel", callback_data=f"fsm:cancel:{back_callback}")]
        ]
    )


def home_btn(label: str = "🏠 Home") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data="nav:home")


def back_btn(callback_data: str, label: str = "◀ Back") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=callback_data)


def build_contact_owner_kb(
    contact_url: str, *, include_home: bool = False
) -> Optional[InlineKeyboardMarkup]:
    """Builds a sleek mobile-first keyboard with the owner's primary `💬 Contact Owner` CTA."""
    rows: list[list[InlineKeyboardButton]] = []
    if contact_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💬 Contact Owner",
                    url=contact_url,
                    style="primary",
                )
            ]
        )
    if include_home:
        rows.append([home_btn("🏠 Vault")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def build_trial_locked_card(
    contact_url: str = "", *, title: str = ""
) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Sleek, respectful Repeat-Open card shown when a visitor re-opens a 1-Time Per-User Trial link
    (sends ZERO files and links directly to the content owner).
    """
    sub = f" · <i>{esc(title[:32])}</i>" if title else ""
    text = (
        f"🔒 <b>Trial Access Ended</b>{sub}\n"
        f"<blockquote>You've already used your one-time trial for this content. "
        f"Reach out to the owner below for full access.</blockquote>"
    )
    return text, build_contact_owner_kb(contact_url)


def build_trial_expiry_notice_text(title: str = "", ttl_seconds: int = 0) -> str:
    """Default sleek expiry notice sent right when a user's auto-delete trial timer ends."""
    dur = f" ({format_ttl_short(ttl_seconds)})" if ttl_seconds > 0 else ""
    label = f" for <b>{esc(title[:36])}</b>" if title else ""
    return (
        f"⏱ <b>Trial Session Ended</b>\n"
        f"<blockquote>Your preview{label}{dur} has been cleared from chat. "
        f"Tap below to contact the owner for full access.</blockquote>"
    )


async def send_with_effect(
    sender_coro,
    *args,
    message_effect_id: Optional[str] = None,
    **kwargs,
):
    """
    Sends a Telegram message/media with an optional Bot API 9.4+ `message_effect_id`.
    Automatically falls back cleanly without `message_effect_id` if the chat type
    (e.g. groups/channels) or custom server rejects the effect ID.
    """
    if message_effect_id:
        try:
            return await tg_retry_call(
                sender_coro, *args, message_effect_id=message_effect_id, **kwargs
            )
        except TypeError:
            # Dummy/test bot or older method signature without message_effect_id
            pass
        except TelegramBadRequest as exc:
            err_l = str(exc).lower()
            if "effect" not in err_l and "message_effect_id" not in err_l:
                raise
    return await tg_retry_call(sender_coro, *args, **kwargs)


async def batch_delete_chat_messages(
    bot: Bot, chat_id: int, message_ids: list[int]
) -> int:
    """
    Deletes up to N messages in chunks of <= 100 using Telegram's native `bot.delete_messages()`
    (1 API call per 100 messages — zero FloodWait!). Falls back cleanly to per-message delete
    if `delete_messages` is unavailable or partially rejected.
    """
    clean_ids = sorted({int(m) for m in message_ids if m and int(m) > 0})
    if not clean_ids:
        return 0

    deleted_total = 0
    batch_fn = getattr(bot, "delete_messages", None)

    for idx in range(0, len(clean_ids), BATCH_CHUNK_SIZE):
        chunk = clean_ids[idx : idx + BATCH_CHUNK_SIZE]
        if batch_fn is not None and len(chunk) > 1:
            try:
                await tg_retry_call(batch_fn, chat_id=chat_id, message_ids=chunk)
                deleted_total += len(chunk)
                continue
            except Exception:
                pass
        # Single message or fallback path
        for mid in chunk:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
                deleted_total += 1
            except Exception:
                pass
    return deleted_total


async def execute_due_scheduled_deletes(bot: Bot, db: Any, limit: int = 50) -> int:
    """
    Processes all due rows in `scheduled_deletes` (`delete_at <= now`):
    1. Batch-deletes delivered messages in chunks of <= 100 via `bot.delete_messages()`.
    2. Sends optional post-expiry notice card with the owner's `💬 Contact Owner` primary button.
    Returns count of processed batch jobs.
    """
    due_rows = await db.pop_due_scheduled_deletes(limit=limit)
    if not due_rows:
        return 0

    for job in due_rows:
        chat_id = int(job["chat_id"])
        msg_ids: list[int] = job.get("message_ids") or []
        if msg_ids:
            await batch_delete_chat_messages(bot, chat_id, msg_ids)

        notify_text = (job.get("notify_text") or "").strip()
        contact_url = (job.get("contact_url") or "").strip()
        if notify_text:
            kb = build_contact_owner_kb(contact_url)
            try:
                await tg_retry_call(
                    bot.send_message,
                    chat_id=chat_id,
                    text=notify_text,
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
    return len(due_rows)


async def run_scheduled_deletes_sweeper(
    bot: Bot, db: Any, interval_seconds: float = 5.0
) -> None:
    """
    Ultra-low-RAM background sweeper coroutine (zero per-delivery sleeping asyncio tasks!).
    Survives systemd/VPS restarts and cleans up expired trials in 100-msg native batches.
    """
    while True:
        try:
            await execute_due_scheduled_deletes(bot, db, limit=50)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(interval_seconds)


async def schedule_persistent_auto_delete(
    bot: Bot,
    db: Optional[Any],
    chat_id: int,
    message_ids: list[int],
    ttl_seconds: int,
    *,
    notify_text: str = "",
    contact_url: str = "",
) -> int:
    """
    Persists an auto-delete timer into SQLite `scheduled_deletes` when `db` is provided
    (zero RAM bloat, 100% restart-safe). Falls back to lightweight in-memory batch delete
    if `db` is None (e.g. isolated unit calls).
    """
    clean_ids = sorted({int(m) for m in message_ids if m and int(m) > 0})
    if ttl_seconds <= 0 or not clean_ids:
        return 0
    if db is not None and hasattr(db, "add_scheduled_delete"):
        delete_at = int(time.time()) + int(ttl_seconds)
        return await db.add_scheduled_delete(
            chat_id=chat_id,
            message_ids=clean_ids,
            delete_at=delete_at,
            notify_text=notify_text,
            contact_url=contact_url,
        )
    schedule_auto_delete(
        bot,
        chat_id,
        clean_ids,
        ttl_seconds,
        notify_text=notify_text,
        contact_url=contact_url,
    )
    return 0


def schedule_auto_delete(
    bot: Bot,
    chat_id: int,
    message_ids: list[int],
    ttl_seconds: int,
    *,
    notify_text: str = "",
    contact_url: str = "",
) -> None:
    """Fallback in-memory batch cleanup using native `batch_delete_chat_messages`."""
    if ttl_seconds <= 0 or not message_ids:
        return

    async def _worker() -> None:
        await asyncio.sleep(ttl_seconds)
        await batch_delete_chat_messages(bot, chat_id, message_ids)
        if notify_text:
            kb = build_contact_owner_kb(contact_url)
            try:
                await tg_retry_call(
                    bot.send_message,
                    chat_id=chat_id,
                    text=notify_text,
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

    task = asyncio.create_task(_worker())
    _AUTO_DELETE_TASKS.add(task)
    task.add_done_callback(_AUTO_DELETE_TASKS.discard)


async def safe_edit_or_send(
    callback: CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    answer_text: Optional[str] = None,
    answer_alert: bool = False,
    also_ack: bool = False,
) -> None:
    """
    Smoothly edit the existing panel message in-place:
    - Photo banner card -> edits caption seamlessly if <= 1024 chars
    - Text card -> edits text in-place
    - When `also_ack=True`, executes `callback.answer(...)` concurrently via `asyncio.gather`
      so button spinner clears immediately and saves 1 full RTT (~145ms).
    """
    if not callback.message:
        if also_ack:
            try:
                await callback.answer(answer_text, show_alert=answer_alert)
            except Exception:
                pass
        return

    async def _do_edit() -> None:
        assert callback.message is not None
        if getattr(callback.message, "photo", None) and len(text) <= 1024:
            try:
                await callback.message.edit_caption(
                    caption=text,
                    reply_markup=reply_markup,
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
        if callback.message.text:
            try:
                await callback.message.edit_text(
                    text,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
        sent = await callback.message.answer(
            text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        if sent and getattr(callback.message, "photo", None):
            try:
                await callback.message.delete()
            except Exception:
                pass

    if also_ack:
        await safe_gather(
            callback.answer(answer_text, show_alert=answer_alert),
            _do_edit(),
        )
    else:
        await _do_edit()


async def send_stored_media(
    bot: Bot,
    chat_id: int,
    file_row: dict[str, Any],
    custom_caption: Optional[str] = None,
    *,
    protect_content: bool = False,
    auto_delete_ttl: int = 0,
    db: Optional[Any] = None,
    notify_text: str = "",
    contact_url: str = "",
    message_effect_id: Optional[str] = None,
) -> Optional[Message]:
    """
    Delivers any archived vault item into a chat.
    Supports `protect_content` (no forward/save), Bot API 9.4 `message_effect_id`,
    and restart-safe `scheduled_deletes` timer + post-expiry Contact Owner notice!
    """
    caption = (
        custom_caption
        if custom_caption is not None
        else (file_row.get("caption") or f"📦 <b>{esc(file_row['title'])}</b>")
    )
    if auto_delete_ttl > 0 and file_row["media_type"] not in ("sticker", "video_note"):
        caption = f"{caption}\n<i>⏱ Auto-deletes in {format_ttl_short(auto_delete_ttl)}</i>"

    mtype = file_row["media_type"]
    fid = file_row["file_id"]
    sent: Optional[Message] = None

    try:
        if mtype == "sticker":
            sent = await send_with_effect(
                bot.send_sticker,
                chat_id=chat_id,
                sticker=fid,
                protect_content=protect_content,
                message_effect_id=message_effect_id,
            )
        elif mtype == "video_note":
            sent = await send_with_effect(
                bot.send_video_note,
                chat_id=chat_id,
                video_note=fid,
                protect_content=protect_content,
                message_effect_id=message_effect_id,
            )
        else:
            method_map = {
                "audio": ("audio", "send_audio"),
                "voice": ("voice", "send_voice"),
                "video": ("video", "send_video"),
                "document": ("document", "send_document"),
                "photo": ("photo", "send_photo"),
                "animation": ("animation", "send_animation"),
            }
            entry = method_map.get(mtype)
            if not entry:
                return None
            kwarg_name, method_name = entry
            sender = getattr(bot, method_name, None)
            if not sender:
                return None
            sent = await send_with_effect(
                sender,
                chat_id=chat_id,
                **{kwarg_name: fid},
                caption=caption,
                protect_content=protect_content,
                message_effect_id=message_effect_id,
            )
    except Exception:
        return None

    if sent and auto_delete_ttl > 0:
        msg_id = getattr(sent, "message_id", None)
        if msg_id:
            await schedule_persistent_auto_delete(
                bot,
                db,
                chat_id,
                [msg_id],
                auto_delete_ttl,
                notify_text=notify_text,
                contact_url=contact_url,
            )
    return sent


def build_file_card_text(
    file_row: dict[str, Any],
    bot_username: str = "",
    header_badge: str = "",
    edit_mode: bool = False,
) -> str:
    """
    Ultra-crisp, compact File Card (Linear / Raycast style):
    Zero fluff, 1-line metadata, compact blockquote summary.
    """
    icon = MEDIA_ICONS.get(file_row["media_type"], "📦")
    fav_badge = " ⭐" if file_row.get("is_favorite") else ""
    lines: list[str] = []

    if header_badge:
        lines.append(header_badge)
    else:
        lines.append(breadcrumb("Vault", "Edit" if edit_mode else "File"))

    lines.append(f"{icon} <b>{esc(file_row['title'])}</b>{fav_badge}")

    meta_parts = [MEDIA_LABELS.get(file_row["media_type"], file_row["media_type"].capitalize())]
    if file_row.get("performer"):
        meta_parts.append(esc(file_row["performer"]))
    if file_row.get("duration"):
        meta_parts.append(format_duration(file_row["duration"]))
    if file_row.get("file_size"):
        meta_parts.append(format_size(file_row["file_size"]))
    if file_row.get("created_at"):
        meta_parts.append(format_date(file_row["created_at"]))
    lines.append(f"<i>{' · '.join(meta_parts)}</i>")

    info_lines: list[str] = []
    folder = file_row.get("group_name") or "Personal"
    uses = file_row.get("use_count") or 0
    row1 = f"📁 {esc(folder)}"
    if uses > 0:
        row1 += f"  ·  📈 {uses}×"
    info_lines.append(row1)

    if file_row.get("tags"):
        info_lines.append(f"🏷️ <code>{esc(file_row['tags'])}</code>")

    if file_row.get("caption"):
        cap = file_row["caption"].strip()
        if len(cap) > 72:
            cap = cap[:69] + "…"
        info_lines.append(f"💬 <i>{esc(cap)}</i>")

    lines.append("<blockquote>" + "\n".join(info_lines) + "</blockquote>")

    if file_row.get("share_token") and bot_username:
        share_url = f"https://t.me/{bot_username}?start=file_{file_row['share_token']}"
        sec_badges: list[str] = [
            format_max_views_badge(
                int(file_row.get("share_views") or 0),
                int(file_row.get("share_max_views") or 0),
            )
        ]
        if file_row.get("share_paused"):
            sec_badges.append("⏸ Paused")
        if file_row.get("share_one_time"):
            sec_badges.append("👤 1×/User")
        if file_row.get("share_pin_hash"):
            sec_badges.append("🔑 PIN")
        if file_row.get("protect_share"):
            sec_badges.append("🛡️ No-Fwd")
        if file_row.get("share_expires_at"):
            sec_badges.append(f"⏳ {format_expiry_badge(file_row['share_expires_at'])}")
        sec_str = f" <i>({' · '.join(sec_badges)})</i>" if sec_badges else ""
        lines.append(f"🔗 <code>{share_url}</code>{sec_str}")

    return "\n".join(lines)


def build_file_keyboard(
    file_row: dict[str, Any],
    back_cb: Optional[str] = None,
    bot_username: str = "",
) -> InlineKeyboardMarkup:
    """
    Crisp 3-row primary File Detail keyboard:
    Row 1: [📥 Get] [📤 Send]
    Row 2: [⭐ Star] [🔗 Share] [🛠️ Edit]
    Row 3: [📋 Copy Link] (if shared) or [◀ Back] [🏠 Home]
    """
    fid = file_row["id"]
    is_trashed = bool(file_row.get("is_trashed"))

    if is_trashed:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="♻️ Restore",
                        callback_data=f"f:restore:{fid}",
                        style="success",
                    ),
                    InlineKeyboardButton(
                        text="💥 Purge",
                        callback_data=f"f:purge:{fid}",
                        style="danger",
                    ),
                ],
                [back_btn("trash:0", "◀ Trash"), home_btn()],
            ]
        )

    fav_label = "⭐ Starred" if file_row.get("is_favorite") else "☆ Star"
    inline_query = (file_row.get("title") or "")[:30]
    manage_cb = f"fm:{fid}:{back_cb}" if back_cb else f"f:manage:{fid}"
    share_cb = f"fsl:{fid}:{back_cb}" if back_cb else f"f:link:{fid}"

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="📥 Get",
                callback_data=f"f:get:{fid}",
                style="primary",
            ),
            InlineKeyboardButton(
                text="📤 Send",
                switch_inline_query=inline_query,
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔗 Link",
                callback_data=share_cb,
            ),
            InlineKeyboardButton(
                text=fav_label,
                callback_data=f"ffav:{fid}:{back_cb}" if back_cb else f"f:fav:{fid}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🛠️ Edit",
                callback_data=manage_cb,
            ),
            InlineKeyboardButton(text="📁 Folder", callback_data=f"f:move:{fid}"),
        ],
    ]

    if file_row.get("share_token") and bot_username:
        share_url = f"https://t.me/{bot_username}?start=file_{file_row['share_token']}"
        rows.append(
            [
                copy_btn("📋 Copy Link", share_url),
                share_url_btn("↗️ Share", share_url, file_row.get("title", "")),
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if back_cb:
        nav_row.append(back_btn(back_cb, "◀ Files"))
        nav_row.append(home_btn())
    else:
        nav_row.append(InlineKeyboardButton(text="📦 Files", callback_data="br:all:def:0:0"))
        nav_row.append(home_btn())
    rows.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_file_manage_keyboard(
    file_row: dict[str, Any],
    back_cb: Optional[str] = None,
    bot_username: str = "",
) -> InlineKeyboardMarkup:
    """
    Crisp Edit & Security Drawer (Progressive Disclosure Level 2).
    """
    fid = file_row["id"]
    return_to_card = (
        f"fo:{fid}:{back_cb[3:]}"
        if (back_cb and back_cb.startswith("br:"))
        else f"f:open:{fid}"
    )
    share_cb = f"fsl:{fid}:{back_cb}" if back_cb else f"f:link:{fid}"

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="✏️ Rename", callback_data=f"f:rename:{fid}"),
            InlineKeyboardButton(text="📁 Folder", callback_data=f"f:move:{fid}"),
        ],
        [
            InlineKeyboardButton(text="🏷️ Tags", callback_data=f"f:tags:{fid}"),
            InlineKeyboardButton(text="💬 Caption", callback_data=f"f:cap:{fid}"),
        ],
        [
            InlineKeyboardButton(text="🔗 Link & Security", callback_data=share_cb),
            InlineKeyboardButton(
                text="🗑️ Trash",
                callback_data=f"f:trash:{fid}",
                style="danger",
            ),
        ],
    ]

    if file_row.get("share_token") and bot_username:
        share_url = f"https://t.me/{bot_username}?start=file_{file_row['share_token']}"
        rows.insert(2, [copy_btn("📋 Copy Share Link", share_url, style="primary")])

    rows.append([back_btn(return_to_card, "◀ File")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_file_link_keyboard(
    file_row: dict[str, Any],
    back_cb: Optional[str] = None,
    bot_username: str = "",
) -> InlineKeyboardMarkup:
    """
    Advanced Share Link Security Panel (GitHub FileStore Pro style):
    - 1-Tap Public Link creation / revocation
    - PIN Lock (Password protected link)
    - Content Protection (Disable Forward / Save)
    - Expiry Timer (Never / 1h / 24h / 7d)
    """
    fid = file_row["id"]
    return_to_card = (
        f"fo:{fid}:{back_cb[3:]}"
        if (back_cb and back_cb.startswith("br:"))
        else f"f:open:{fid}"
    )
    has_link = bool(file_row.get("share_token"))

    if not has_link:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚡ Create Share Link",
                        callback_data=f"fsh:{fid}:{back_cb}" if back_cb else f"f:share:{fid}",
                        style="primary",
                    )
                ],
                [back_btn(return_to_card, "◀ Back")],
            ]
        )

    share_url = f"https://t.me/{bot_username}?start=file_{file_row['share_token']}"
    one_time_on = bool(file_row.get("share_one_time"))
    one_time_lbl = f"👤 1×/User: {'ON' if one_time_on else 'OFF'}"
    pin_lbl = "🔑 PIN: ON" if file_row.get("share_pin_hash") else "🔑 Set PIN"
    prot_lbl = toggle_label(bool(file_row.get("protect_share")), "No-Fwd")
    exp_lbl = f"⏳ {format_expiry_badge(file_row.get('share_expires_at'))}"
    max_v = int(file_row.get("share_max_views") or 0)
    cur_v = int(file_row.get("share_views") or 0)
    limit_lbl = f"👁 Limit: {cur_v}/{max_v}" if max_v > 0 else "👁 Limit: ∞"
    paused = bool(file_row.get("share_paused"))
    pause_lbl = "▶️ Resume" if paused else "⏸ Pause"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                copy_btn("📋 Copy Link", share_url, style="primary"),
                share_url_btn("↗️ Share", share_url, file_row.get("title", "")),
            ],
            [
                InlineKeyboardButton(
                    text=one_time_lbl,
                    callback_data=f"f:1x:{fid}",
                    style="success" if one_time_on else None,
                ),
                InlineKeyboardButton(text=prot_lbl, callback_data=f"f:prot:{fid}"),
            ],
            [
                InlineKeyboardButton(text=pin_lbl, callback_data=f"f:pin:{fid}"),
                InlineKeyboardButton(text=exp_lbl, callback_data=f"f:exp:{fid}"),
            ],
            [
                InlineKeyboardButton(text=limit_lbl, callback_data=f"f:maxv:{fid}"),
                InlineKeyboardButton(text=pause_lbl, callback_data=f"f:pause:{fid}"),
            ],
            [
                InlineKeyboardButton(text="🔄 Reset", callback_data=f"f:regen:{fid}"),
                InlineKeyboardButton(
                    text="🚫 Revoke",
                    callback_data=f"fsh:{fid}:{back_cb}" if back_cb else f"f:share:{fid}",
                    style="danger",
                ),
            ],
            [back_btn(return_to_card, "◀ File")],
        ]
    )


def build_upload_quick_keyboard(file_row: dict[str, Any]) -> InlineKeyboardMarkup:
    """
    Ultra-short 2-row receipt keyboard shown right after uploading a file (max 2 btns/row for 360px):
    Row 1: [📤 Send] [🔗 Link]
    Row 2: [🛠️ Edit] [📁 Folder]
    """
    fid = file_row["id"]
    inline_query = (file_row.get("title") or "")[:30]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 Send",
                    switch_inline_query=inline_query,
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🔗 Link",
                    callback_data=f"f:link:{fid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🛠️ Edit",
                    callback_data=f"f:manage:{fid}",
                ),
                InlineKeyboardButton(text="📁 Folder", callback_data=f"f:move:{fid}"),
            ],
        ]
    )
