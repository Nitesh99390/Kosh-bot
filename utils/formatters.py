import logging
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
    21600: "6h",
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

_MARKUP_FALLBACK_KEYWORDS = (
    "reply_markup",
    "style",
    "copy_text",
    "button",
    "inline_keyboard",
    "can't parse inline keyboard",
    "button_type_invalid",
)


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


def downgrade_reply_markup(
    markup: Optional[InlineKeyboardMarkup],
    *,
    drop_copy_text: bool = False,
) -> Optional[InlineKeyboardMarkup]:
    """
    Produces a universally compatible `InlineKeyboardMarkup` fallback when an older Telegram
    client, custom Bot API server, or restricted chat rejects `style` or `copy_text`:
    - Strips `style` and `icon_custom_emoji_id`
    - Optionally converts `copy_text` buttons cleanly into `url` (if HTTP/t.me) or `callback_data="noop"`
    """
    if markup is None or not getattr(markup, "inline_keyboard", None):
        return markup

    new_rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        new_row: list[InlineKeyboardButton] = []
        for btn in row:
            data = btn.model_dump(exclude_none=True)
            data.pop("style", None)
            data.pop("icon_custom_emoji_id", None)
            if drop_copy_text and "copy_text" in data:
                ct = data.pop("copy_text")
                raw_val = ct.get("text", "") if isinstance(ct, dict) else getattr(ct, "text", "")
                if isinstance(raw_val, str) and raw_val.startswith(("https://", "http://")):
                    data["url"] = raw_val
                else:
                    data["callback_data"] = "noop"
            new_row.append(InlineKeyboardButton(**data))
        if new_row:
            new_rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=new_rows)


def _is_markup_bad_request(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _MARKUP_FALLBACK_KEYWORDS)


def esc(text: Optional[str]) -> str:
    return html.escape(text or "")


def breadcrumb(*parts: str) -> str:
    """Clean, readable breadcrumb trail (e.g. 🏠 Home › 📦 My Files)."""
    clean = [p for p in parts if p]
    return f"<i>{' › '.join(clean)}</i>"


def toggle_label(enabled: bool, label: str) -> str:
    """Simple, crystal-clear ON/OFF toggle indicator that anyone understands immediately."""
    state_txt = "ON" if enabled else "OFF"
    dot = "🟢" if enabled else "⚪"
    return f"{dot} {label}: {state_txt}"


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
        return f"👁 {cur} opens"
    return f"👁 {cur}/{lim}"


async def tg_retry_call(coro_factory, *args, max_retries: int = 4, **kwargs):
    """
    Executes a Telegram Bot API coroutine with:
    1. Automatic FloodWait (`TelegramRetryAfter`) backoff so 100+ file bursts never drop files.
    2. Automatic `reply_markup` style/CopyText downgrade fallback if a Bot API proxy or client
       rejects Bot API 9.4 button styles.
    """
    markup_downgrade_level = 0
    for attempt in range(max_retries + 2):
        try:
            return await coro_factory(*args, **kwargs)
        except TelegramRetryAfter as exc:
            wait_s = min(max(float(exc.retry_after), 0.5), 45.0) + 0.25
            if attempt + 1 >= max_retries + 2:
                raise
            await asyncio.sleep(wait_s)
        except TelegramBadRequest as exc:
            rm = kwargs.get("reply_markup")
            if rm is not None and _is_markup_bad_request(exc) and markup_downgrade_level < 2:
                markup_downgrade_level += 1
                kwargs = {
                    **kwargs,
                    "reply_markup": downgrade_reply_markup(
                        rm, drop_copy_text=(markup_downgrade_level >= 2)
                    ),
                }
                continue
            raise
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
        return "Just now"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    if diff < 86400 * 7:
        return f"{diff // 86400}d ago"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d")


def progress_bar(ratio: float, width: int = 8) -> str:
    filled = max(0, min(width, round(ratio * width)))
    return "▰" * filled + "▱" * (width - filled)


def cancel_kb(back_callback: str = "nav:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Cancel", callback_data=f"fsm:cancel:{back_callback}")]
        ]
    )


def home_btn(label: str = "🏠 Home") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data="nav:home")


def back_btn(callback_data: str, label: str = "⬅️ Back") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=callback_data)


def build_contact_owner_kb(
    contact_url: str = "",
    *,
    include_home: bool = False,
    request_access_cb: Optional[str] = None,
) -> Optional[InlineKeyboardMarkup]:
    """
    Builds a sleek mobile-first CTA keyboard:
    - When `request_access_cb` is provided -> 2-column row `[🔓 Request Full Access] [💬 Contact Owner]`
      (or 1-button `[🔓 Request Full Access]` if owner has no contact URL).
    - Otherwise -> single `[💬 Contact Owner]` primary button if `contact_url` exists.
    """
    rows: list[list[InlineKeyboardButton]] = []
    cta_row: list[InlineKeyboardButton] = []
    if request_access_cb:
        cta_row.append(
            InlineKeyboardButton(
                text="🔓 Request Full Access",
                callback_data=request_access_cb,
                style="primary",
            )
        )
    if contact_url:
        cta_row.append(
            InlineKeyboardButton(
                text="💬 Contact Owner",
                url=contact_url,
                style=None if request_access_cb else "primary",
            )
        )
    if cta_row:
        rows.append(cta_row)
    if include_home:
        rows.append([home_btn("🏠 Home")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def build_trial_locked_card(
    contact_url: str = "",
    *,
    title: str = "",
    request_access_cb: Optional[str] = None,
) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Sleek, easy-to-understand Repeat-Open card shown when a visitor re-opens a 1-Time Per-User Trial link.
    """
    sub = f"\n📄 <b>{esc(title[:40])}</b>" if title else ""
    text = (
        f"🔒 <b>Free Preview Already Used</b>{sub}\n"
        f"<blockquote>This link allows <b>1 preview per person</b>.\n"
        f"Tap <b>🔓 Request Full Access</b> below for permanent, unrestricted access.</blockquote>"
    )
    return text, build_contact_owner_kb(
        contact_url, request_access_cb=request_access_cb
    )


def build_trial_expiry_notice_text(title: str = "", ttl_seconds: int = 0) -> str:
    """Default sleek expiry notice sent right when a user's auto-delete trial timer ends."""
    dur = f" ({format_ttl_short(ttl_seconds)})" if ttl_seconds > 0 else ""
    label = f" for <b>{esc(title[:36])}</b>" if title else ""
    return (
        f"⏱ <b>Preview Timer Ended</b>\n"
        f"<blockquote>Your temporary preview{label}{dur} was automatically cleared.\n"
        f"Want permanent access? Tap below to unlock or message the owner.</blockquote>"
    )


def format_grant_badge(expires_at: Optional[int]) -> str:
    """Formats a Full Access Grant duration badge (`Lifetime` vs remaining days/hours)."""
    if expires_at is None:
        return ""
    exp = int(expires_at)
    if exp == 0:
        return "♾️ Lifetime Full Access"
    rem = max(0, exp - int(time.time()))
    if rem <= 0:
        return "Expired"
    if rem < 3600:
        return f"Full Access · {max(1, rem // 60)}m left"
    if rem < 86400:
        return f"Full Access · {round(rem / 3600)}h left"
    return f"Full Access · {max(1, round(rem / 86400))}d left"


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
        for mid in chunk:
            try:
                await tg_retry_call(bot.delete_message, chat_id=chat_id, message_id=mid)
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
    use_deferred_delete = hasattr(db, "get_due_scheduled_deletes") and hasattr(
        db, "delete_scheduled_delete"
    )
    if use_deferred_delete:
        due_rows = await db.get_due_scheduled_deletes(limit=limit)
    else:
        due_rows = await db.pop_due_scheduled_deletes(limit=limit)
    if not due_rows:
        return 0

    for job in due_rows:
        job_id = job.get("id")
        chat_id = int(job["chat_id"])
        msg_ids: list[int] = job.get("message_ids") or []
        notify_text = (job.get("notify_text") or "").strip()
        contact_url = (job.get("contact_url") or "").strip()
        ltype = (job.get("link_type") or "").strip()
        lid = int(job.get("link_id") or 0)

        skip_delivery = False
        if ltype in ("file", "pack") and lid > 0 and hasattr(db, "has_active_access_grant"):
            try:
                if await db.has_active_access_grant(ltype, lid, chat_id):
                    skip_delivery = True
            except Exception:
                pass

        if not skip_delivery:
            if msg_ids:
                await batch_delete_chat_messages(bot, chat_id, msg_ids)

            if notify_text:
                req_cb: Optional[str] = None
                if ltype == "file" and lid > 0:
                    req_cb = f"req:f:{lid}"
                elif ltype == "pack" and lid > 0:
                    req_cb = f"req:p:{lid}"
                kb = build_contact_owner_kb(contact_url, request_access_cb=req_cb)
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

        if use_deferred_delete and job_id is not None:
            try:
                await db.delete_scheduled_delete(int(job_id))
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
    link_type: str = "",
    link_id: int = 0,
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
            link_type=link_type,
            link_id=link_id,
        )
    req_cb: Optional[str] = None
    if link_type == "file" and link_id > 0:
        req_cb = f"req:f:{link_id}"
    elif link_type == "pack" and link_id > 0:
        req_cb = f"req:p:{link_id}"
    schedule_auto_delete(
        bot,
        chat_id,
        clean_ids,
        ttl_seconds,
        notify_text=notify_text,
        contact_url=contact_url,
        request_access_cb=req_cb,
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
    request_access_cb: Optional[str] = None,
) -> None:
    """Fallback in-memory batch cleanup using native `batch_delete_chat_messages`."""
    if ttl_seconds <= 0 or not message_ids:
        return

    async def _worker() -> None:
        await asyncio.sleep(ttl_seconds)
        await batch_delete_chat_messages(bot, chat_id, message_ids)
        if notify_text:
            kb = build_contact_owner_kb(
                contact_url, request_access_cb=request_access_cb
            )
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


async def _call_with_markup_fallback(method, *args, reply_markup: Optional[InlineKeyboardMarkup] = None, **kwargs):
    """Calls a message edit/send helper with automatic button-style downgrade if rejected."""
    try:
        return await method(*args, reply_markup=reply_markup, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return None
        if reply_markup is not None and _is_markup_bad_request(exc):
            plain_rm = downgrade_reply_markup(reply_markup, drop_copy_text=False)
            try:
                return await method(*args, reply_markup=plain_rm, **kwargs)
            except TelegramBadRequest as exc2:
                if "message is not modified" in str(exc2).lower():
                    return None
                safe_rm = downgrade_reply_markup(reply_markup, drop_copy_text=True)
                return await method(*args, reply_markup=safe_rm, **kwargs)
        raise


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
    Smoothly edits the existing panel message in-place without flickers or crashes:
    - Text card -> edits text in-place
    - Photo banner card -> cleanly transitions to text card when opening sub-menus
      (or edits caption if specifically staying on a photo card)
    - Automatically downgrades `style` / `copy_text` if rejected by client/server.
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
                await _call_with_markup_fallback(
                    callback.message.edit_caption,
                    caption=text,
                    reply_markup=reply_markup,
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
            except Exception:
                pass

        if getattr(callback.message, "text", None):
            try:
                await _call_with_markup_fallback(
                    callback.message.edit_text,
                    text,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
            except Exception:
                pass

        sent = await _call_with_markup_fallback(
            callback.message.answer,
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


_fmt_logger = logging.getLogger("koshbox.formatters")


def esc_truncate(raw: str, max_escaped_len: int) -> str:
    """
    Escapes raw text for Telegram HTML and truncates safely BEFORE escaping
    so HTML entities (`&amp;`, `&lt;`) and surrounding tags are NEVER bisected.
    """
    text = str(raw or "")
    if max_escaped_len <= 0:
        return ""
    escaped = esc(text)
    if len(escaped) <= max_escaped_len:
        return escaped
    if max_escaped_len <= 1:
        return "…"
    budget = max_escaped_len - 1
    lo, hi = 0, min(len(text), budget)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(esc(text[:mid])) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return esc(text[:lo]) + "…"


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
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    extra_note: str = "",
    link_type: str = "",
    link_id: int = 0,
) -> Optional[Message]:
    """
    Delivers any archived vault item into a chat.
    Safely escapes raw user captions so `<` or `&` in filenames/captions NEVER break HTML parse_mode!
    """
    suffix_parts: list[str] = []
    if extra_note and file_row["media_type"] not in ("sticker", "video_note"):
        suffix_parts.append(f"\n<i>{esc(extra_note)}</i>")
    if auto_delete_ttl > 0 and file_row["media_type"] not in ("sticker", "video_note"):
        suffix_parts.append(f"\n<i>⏱ Auto-deletes in {format_ttl_short(auto_delete_ttl)}</i>")
    suffix = "".join(suffix_parts)

    # Guard against Telegram's 1024-character media caption limit WITHOUT splitting HTML tags/entities
    max_body_len = max(64, 1020 - len(suffix))
    if custom_caption is not None:
        if len(custom_caption) <= max_body_len:
            body = custom_caption
        else:
            # Fallback to plain-escaped truncation if a custom HTML caption exceeds budget
            body = esc_truncate(custom_caption, max_body_len)
    elif file_row.get("caption"):
        body = esc_truncate(str(file_row["caption"]), max_body_len)
    else:
        title_budget = max(16, max_body_len - len("📦 <b></b>"))
        body = f"📦 <b>{esc_truncate(str(file_row.get('title') or 'Untitled'), title_budget)}</b>"

    caption = f"{body}{suffix}"
    if len(caption) > 1024:
        caption = body[:1020]

    mtype = file_row["media_type"]
    fid = file_row["file_id"]
    sent: Optional[Message] = None
    rm_kwargs: dict[str, Any] = {"reply_markup": reply_markup} if reply_markup else {}

    try:
        if mtype == "sticker":
            sent = await send_with_effect(
                bot.send_sticker,
                chat_id=chat_id,
                sticker=fid,
                protect_content=protect_content,
                message_effect_id=message_effect_id,
                **rm_kwargs,
            )
        elif mtype == "video_note":
            sent = await send_with_effect(
                bot.send_video_note,
                chat_id=chat_id,
                video_note=fid,
                protect_content=protect_content,
                message_effect_id=message_effect_id,
                **rm_kwargs,
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
                **rm_kwargs,
            )
    except Exception as exc:
        _fmt_logger.warning("send_stored_media failed (file_id=%s, type=%s): %s", file_row.get("id"), mtype, exc)
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
                link_type=link_type,
                link_id=link_id,
            )
    return sent


def build_file_card_text(
    file_row: dict[str, Any],
    bot_username: str = "",
    header_badge: str = "",
    edit_mode: bool = False,
) -> str:
    """
    Simple, scannable File Card:
    Clear title, clean metadata line, organized folder/tag box, and clear link status if shared.
    """
    icon = MEDIA_ICONS.get(file_row["media_type"], "📦")
    fav_badge = " ⭐" if file_row.get("is_favorite") else ""
    lines: list[str] = []

    if header_badge:
        lines.append(header_badge)
    else:
        lines.append(breadcrumb("🏠 Home", "📦 Files", "✏️ Edit" if edit_mode else "Details"))

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
    lines.append(f"<i>{'  •  '.join(meta_parts)}</i>")

    info_lines: list[str] = []
    folder = file_row.get("group_name") or "Personal Vault"
    uses = file_row.get("use_count") or 0
    row1 = f"📁 <b>Folder:</b> {esc(folder)}"
    if uses > 0:
        row1 += f"  •  📤 {uses}× sent"
    info_lines.append(row1)

    if file_row.get("tags"):
        info_lines.append(f"🏷️ <b>Tags:</b> <code>{esc(file_row['tags'])}</code>")

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
        if int(file_row.get("auto_delete_ttl") or 0) > 0:
            sec_badges.append(f"💣 {format_ttl_short(int(file_row['auto_delete_ttl']))}")
        if file_row.get("share_expires_at"):
            sec_badges.append(f"⏳ {format_expiry_badge(file_row['share_expires_at'])}")
        lines.append(f"🔗 <b>Share Link</b> <i>({' · '.join(sec_badges)})</i>\n<code>{share_url}</code>")

    return "\n".join(lines)


def _encode_ctx(back_cb: Optional[str]) -> str:
    """Extracts compact `<flt>:<sort>:<gid>:<page>` context if `back_cb` is a `br:...` callback."""
    if back_cb and back_cb.startswith("br:") and len(back_cb.split(":")) == 5:
        return back_cb[3:]
    return ""


def build_file_keyboard(
    file_row: dict[str, Any],
    back_cb: Optional[str] = None,
    bot_username: str = "",
) -> InlineKeyboardMarkup:
    """
    Clean, balanced, beginner-friendly File Detail keyboard (strict 2-button symmetric grid):
    - Row 1: [📥 Get Here] [📤 Send to Chat]
    - Row 2: [🔗 Share Link] [⭐ Star / Unstar]
    - Row 3 (if link active): [📋 Copy Link] [↗️ Forward Link]
    - Row 4: [✏️ Edit Details] [📁 Move Folder]
    - Row 5: [⬅️ Back] [🏠 Home]
    """
    fid = file_row["id"]
    is_trashed = bool(file_row.get("is_trashed"))

    if is_trashed:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="♻️ Restore File",
                        callback_data=f"f:restore:{fid}",
                        style="success",
                    ),
                    InlineKeyboardButton(
                        text="🗑️ Delete Forever",
                        callback_data=f"f:purge:{fid}",
                        style="danger",
                    ),
                ],
                [back_btn("trash:0", "⬅️ Trash"), home_btn()],
            ]
        )

    fav_label = "⭐ Starred" if file_row.get("is_favorite") else "☆ Star File"
    inline_query = (file_row.get("title") or "")[:30]
    ctx = _encode_ctx(back_cb)
    manage_cb = f"fm:{fid}:br:{ctx}" if ctx else f"f:manage:{fid}"
    share_cb = f"fsl:{fid}:br:{ctx}" if ctx else f"f:link:{fid}"
    move_cb = f"fmv:{fid}:{ctx}" if ctx else f"f:move:{fid}"
    fav_cb = f"ffav:{fid}:br:{ctx}" if ctx else f"f:fav:{fid}"
    link_btn_lbl = "🔗 Link Settings" if file_row.get("share_token") else "🔗 Create Link"

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="📥 Get File",
                callback_data=f"f:get:{fid}",
                style="primary",
            ),
            InlineKeyboardButton(
                text="📤 Send in Chat",
                switch_inline_query=inline_query,
            ),
        ],
    ]

    if file_row.get("share_token") and bot_username:
        share_url = f"https://t.me/{bot_username}?start=file_{file_row['share_token']}"
        rows.append(
            [
                copy_btn("📋 Copy Link", share_url, style="success"),
                share_url_btn("↗️ Share Link", share_url, file_row.get("title", "")),
            ]
        )

    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=link_btn_lbl,
                    callback_data=share_cb,
                ),
                InlineKeyboardButton(
                    text=fav_label,
                    callback_data=fav_cb,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Edit Info",
                    callback_data=manage_cb,
                ),
                InlineKeyboardButton(
                    text="📁 Move Folder",
                    callback_data=move_cb,
                ),
            ],
        ]
    )

    if back_cb:
        rows.append([back_btn(back_cb, "⬅️ Back"), home_btn()])
    else:
        rows.append(
            [
                InlineKeyboardButton(text="📦 My Files", callback_data="br:all:def:0:0"),
                home_btn(),
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_file_manage_keyboard(
    file_row: dict[str, Any],
    back_cb: Optional[str] = None,
    bot_username: str = "",
) -> InlineKeyboardMarkup:
    """
    Clean File Edit Drawer — keeps `back_cb` context intact across Rename, Tags, Caption, Move & Trash!
    """
    fid = file_row["id"]
    ctx = _encode_ctx(back_cb)
    return_to_card = f"fo:{fid}:{ctx}" if ctx else f"f:open:{fid}"
    share_cb = f"fsl:{fid}:br:{ctx}" if ctx else f"f:link:{fid}"
    move_cb = f"fmv:{fid}:{ctx}" if ctx else f"f:move:{fid}"

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="✏️ Rename", callback_data=f"f:rename:{fid}"),
            InlineKeyboardButton(text="📁 Move Folder", callback_data=move_cb),
        ],
        [
            InlineKeyboardButton(text="🏷️ Edit Tags", callback_data=f"f:tags:{fid}"),
            InlineKeyboardButton(text="💬 Edit Caption", callback_data=f"f:cap:{fid}"),
        ],
        [
            InlineKeyboardButton(text="🔗 Share & Locks", callback_data=share_cb),
            InlineKeyboardButton(
                text="🗑️ Move to Trash",
                callback_data=f"f:trash:{fid}",
                style="danger",
            ),
        ],
        [back_btn(return_to_card, "⬅️ Back to File"), home_btn()],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_file_link_keyboard(
    file_row: dict[str, Any],
    back_cb: Optional[str] = None,
    bot_username: str = "",
) -> InlineKeyboardMarkup:
    """
    Clean, easy-to-understand File Share Link & Security Panel:
    Preserves `back_cb` context across all toggles so the Back button never forgets the user's folder/page!
    """
    fid = file_row["id"]
    ctx = _encode_ctx(back_cb)
    ctx_suffix = f":{ctx}" if ctx else ""
    return_to_card = f"fo:{fid}:{ctx}" if ctx else f"f:open:{fid}"
    has_link = bool(file_row.get("share_token"))

    if not has_link:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔗 Create Share Link",
                        callback_data=f"fsh:{fid}:br:{ctx}" if ctx else f"f:share:{fid}",
                        style="primary",
                    )
                ],
                [back_btn(return_to_card, "⬅️ Back to File"), home_btn()],
            ]
        )

    share_url = f"https://t.me/{bot_username}?start=file_{file_row['share_token']}"
    one_time_on = bool(file_row.get("share_one_time"))
    one_time_lbl = f"👤 1×/User: {'ON' if one_time_on else 'OFF'}"
    pin_lbl = "🔑 PIN: ON" if file_row.get("share_pin_hash") else "🔑 PIN: OFF"
    prot_lbl = toggle_label(bool(file_row.get("protect_share")), "No-Fwd")
    adel_val = int(file_row.get("auto_delete_ttl") or 0)
    adel_lbl = f"💣 Auto-Del: {format_ttl_short(adel_val)}"
    exp_lbl = f"⏳ Link: {format_expiry_badge(file_row.get('share_expires_at'))}"
    max_v = int(file_row.get("share_max_views") or 0)
    cur_v = int(file_row.get("share_views") or 0)
    limit_lbl = f"👁 Limit: {cur_v}/{max_v}" if max_v > 0 else "👁 Limit: ∞"
    paused = bool(file_row.get("share_paused"))
    pause_lbl = "▶️ Resume Link" if paused else "⏸ Pause Link"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                copy_btn("📋 Copy Link", share_url, style="primary"),
                share_url_btn("↗️ Share", share_url, file_row.get("title", "")),
            ],
            [
                InlineKeyboardButton(
                    text=one_time_lbl,
                    callback_data=f"f:1x:{fid}{ctx_suffix}",
                    style="success" if one_time_on else None,
                ),
                InlineKeyboardButton(
                    text=prot_lbl,
                    callback_data=f"f:prot:{fid}{ctx_suffix}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=adel_lbl,
                    callback_data=f"f:adel:{fid}{ctx_suffix}",
                    style="success" if adel_val > 0 else None,
                ),
                InlineKeyboardButton(
                    text=exp_lbl,
                    callback_data=f"f:exp:{fid}{ctx_suffix}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=pin_lbl,
                    callback_data=f"f:pin:{fid}{ctx_suffix}",
                ),
                InlineKeyboardButton(
                    text=limit_lbl,
                    callback_data=f"f:maxv:{fid}{ctx_suffix}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=pause_lbl,
                    callback_data=f"f:pause:{fid}{ctx_suffix}",
                ),
                InlineKeyboardButton(
                    text="🔄 New Link",
                    callback_data=f"f:regen:{fid}{ctx_suffix}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🚫 Disable Link",
                    callback_data=f"fsh:{fid}:br:{ctx}" if ctx else f"f:share:{fid}",
                    style="danger",
                ),
                back_btn(return_to_card, "⬅️ File"),
            ],
        ]
    )


def build_upload_quick_keyboard(file_row: dict[str, Any]) -> InlineKeyboardMarkup:
    """
    Clean 2-row receipt keyboard shown right after uploading a single file:
    Row 1: [🔗 Create Share Link] [📤 Send in Chat]
    Row 2: [✏️ Edit Info] [📁 Move Folder]
    """
    fid = file_row["id"]
    inline_query = (file_row.get("title") or "")[:30]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔗 Share Link",
                    callback_data=f"f:link:{fid}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📤 Send in Chat",
                    switch_inline_query=inline_query,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Edit Info",
                    callback_data=f"f:manage:{fid}",
                ),
                InlineKeyboardButton(
                    text="📁 Move Folder",
                    callback_data=f"f:move:{fid}",
                ),
            ],
        ]
    )
