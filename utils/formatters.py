import asyncio
import html
import time
from datetime import datetime, timezone
from typing import Any, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
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
    86400: "24h",
    604800: "7d",
}

DIVIDER = "──────────────────────"

_AUTO_DELETE_TASKS: set[asyncio.Task] = set()


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


def schedule_auto_delete(
    bot: Bot, chat_id: int, message_ids: list[int], ttl_seconds: int
) -> None:
    """Schedules background message cleanup for delivered files/packs when Auto-Delete is enabled."""
    if ttl_seconds <= 0 or not message_ids:
        return

    async def _worker() -> None:
        await asyncio.sleep(ttl_seconds)
        for mid in message_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass

    task = asyncio.create_task(_worker())
    _AUTO_DELETE_TASKS.add(task)
    task.add_done_callback(_AUTO_DELETE_TASKS.discard)


async def safe_edit_or_send(
    callback: CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """
    Smoothly edit the existing panel message in-place:
    - Photo banner card -> edits caption seamlessly if <= 1024 chars
    - Text card -> edits text in-place
    """
    if not callback.message:
        return
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


async def send_stored_media(
    bot: Bot,
    chat_id: int,
    file_row: dict[str, Any],
    custom_caption: Optional[str] = None,
    *,
    protect_content: bool = False,
    auto_delete_ttl: int = 0,
) -> Optional[Message]:
    """
    Delivers any archived vault item into a chat.
    Supports `protect_content` (no forward/save) and optional `auto_delete_ttl` timer!
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

    if mtype == "sticker":
        sent = await bot.send_sticker(
            chat_id=chat_id, sticker=fid, protect_content=protect_content
        )
    elif mtype == "video_note":
        sent = await bot.send_video_note(
            chat_id=chat_id, video_note=fid, protect_content=protect_content
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
        sent = await sender(
            chat_id=chat_id,
            **{kwarg_name: fid},
            caption=caption,
            protect_content=protect_content,
        )

    if sent and auto_delete_ttl > 0:
        msg_id = getattr(sent, "message_id", None)
        if msg_id:
            schedule_auto_delete(bot, chat_id, [msg_id], auto_delete_ttl)
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
        sec_badges: list[str] = []
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
            InlineKeyboardButton(
                text="🔗 Link",
                callback_data=share_cb,
            ),
        ],
        [
            InlineKeyboardButton(
                text=fav_label,
                callback_data=f"ffav:{fid}:{back_cb}" if back_cb else f"f:fav:{fid}",
            ),
            InlineKeyboardButton(
                text="🛠️ Edit",
                callback_data=manage_cb,
            ),
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
    pin_lbl = "🔑 PIN: ON" if file_row.get("share_pin_hash") else "🔑 Set PIN"
    prot_lbl = toggle_label(bool(file_row.get("protect_share")), "No-Fwd")
    exp_lbl = f"⏳ {format_expiry_badge(file_row.get('share_expires_at'))}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                copy_btn("📋 Copy Link", share_url, style="primary"),
                share_url_btn("↗️ Share", share_url, file_row.get("title", "")),
            ],
            [
                InlineKeyboardButton(text=pin_lbl, callback_data=f"f:pin:{fid}"),
                InlineKeyboardButton(text=prot_lbl, callback_data=f"f:prot:{fid}"),
            ],
            [
                InlineKeyboardButton(text=exp_lbl, callback_data=f"f:exp:{fid}"),
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
    Ultra-short 2-row receipt keyboard shown right after uploading a file:
    Row 1: [📤 Send] [🔗 Link] [🛠️ Edit]
    Row 2: [📁 Folder] [📦 Files]
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
                InlineKeyboardButton(
                    text="🛠️ Edit",
                    callback_data=f"f:manage:{fid}",
                ),
            ],
            [
                InlineKeyboardButton(text="📁 Folder", callback_data=f"f:move:{fid}"),
                InlineKeyboardButton(text="📦 Files", callback_data="br:all:def:0:0"),
            ],
        ]
    )
