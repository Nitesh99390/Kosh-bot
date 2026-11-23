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
    "video_note": "Video Note",
    "document": "Document",
    "photo": "Photo",
    "animation": "GIF",
    "sticker": "Sticker",
}

SORT_LABELS = {
    "new": "Newest",
    "top": "Most Used",
    "az": "A–Z",
    "size": "Largest",
    "old": "Oldest",
}

SORT_ORDER = ["new", "top", "az", "size", "old"]

DIVIDER = "──────────────────────"


def esc(text: Optional[str]) -> str:
    return html.escape(text or "")


def breadcrumb(*parts: str) -> str:
    """Subtle top breadcrumb trail for nested screens (e.g. Vault › My Files)."""
    return f"<i>{' › '.join(parts)}</i>"


def toggle_label(enabled: bool, label: str) -> str:
    """GramIO / Telegram standard toggle state directly inside button text."""
    return f"{'✅' if enabled else '⬜'} {label}"


def copy_btn(text: str, value: str, style: Optional[str] = None) -> InlineKeyboardButton:
    """Native Telegram one-tap clipboard button (CopyTextButton)."""
    return InlineKeyboardButton(
        text=text,
        copy_text=CopyTextButton(text=value),
        style=style,
    )


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
        return "just now"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    if diff < 86400 * 7:
        return f"{diff // 86400}d ago"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y")


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


async def safe_edit_or_send(
    callback: CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """
    Smoothly edit the existing panel message in-place:
    - If the panel is a PostBot-style photo banner card, edits its caption seamlessly!
    - If the panel is a text message, edits its text in-place.
    - Falls back cleanly to sending a new message if needed.
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
) -> bool:
    """Send any archived vault item directly into a chat."""
    caption = (
        custom_caption
        if custom_caption is not None
        else (file_row.get("caption") or f"📦 <b>{esc(file_row['title'])}</b>")
    )
    mtype = file_row["media_type"]
    fid = file_row["file_id"]

    if mtype == "sticker":
        await bot.send_sticker(chat_id=chat_id, sticker=fid)
        return True
    if mtype == "video_note":
        await bot.send_video_note(chat_id=chat_id, video_note=fid)
        return True

    send_map = {
        "audio": ("audio", bot.send_audio),
        "voice": ("voice", bot.send_voice),
        "video": ("video", bot.send_video),
        "document": ("document", bot.send_document),
        "photo": ("photo", bot.send_photo),
        "animation": ("animation", bot.send_animation),
    }
    entry = send_map.get(mtype)
    if not entry:
        return False
    kwarg_name, sender = entry
    await sender(chat_id=chat_id, **{kwarg_name: fid}, caption=caption)
    return True


def build_file_card_text(
    file_row: dict[str, Any],
    bot_username: str = "",
    header_badge: str = "",
    edit_mode: bool = False,
) -> str:
    icon = MEDIA_ICONS.get(file_row["media_type"], "📦")
    fav_badge = " ⭐" if file_row.get("is_favorite") else ""
    lines: list[str] = []

    if header_badge:
        lines.append(header_badge)
    else:
        trail = (
            breadcrumb("Vault", "My Files", "Manage File")
            if edit_mode
            else breadcrumb("Vault", "My Files", "Details")
        )
        lines.append(trail)

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
    group_name = file_row.get("group_name") or "Personal Vault"
    info_lines.append(f"📁 <b>Folder:</b> {esc(group_name)}")

    if file_row.get("tags"):
        info_lines.append(f"🏷️ <b>Tags:</b> <code>{esc(file_row['tags'])}</code>")

    if file_row.get("caption"):
        cap_preview = file_row["caption"].strip()
        if len(cap_preview) > 90:
            cap_preview = cap_preview[:87] + "..."
        info_lines.append(f"💬 <b>Caption:</b> <i>{esc(cap_preview)}</i>")

    uses = file_row.get("use_count") or 0
    if uses > 0:
        info_lines.append(f"📈 <b>Sent inline:</b> {uses}×")

    lines.append("<blockquote>" + "\n".join(info_lines) + "</blockquote>")

    if file_row.get("share_token") and bot_username:
        share_url = f"https://t.me/{bot_username}?start=file_{file_row['share_token']}"
        lines.append(f"🔗 <b>Public Link:</b> <code>{share_url}</code>")

    if edit_mode:
        lines.append("<i>Select a property below to edit or organize this file:</i>")

    return "\n".join(lines)


def build_file_keyboard(
    file_row: dict[str, Any],
    back_cb: Optional[str] = None,
    bot_username: str = "",
) -> InlineKeyboardMarkup:
    """
    Clean primary File Detail keyboard using Progressive Disclosure:
    Only primary actions (Get, Send, Favorite, Manage) are shown upfront (3 rows max).
    Editing, moving, link sharing, and trashing live one tap inside '🛠️ Edit & More'.
    """
    fid = file_row["id"]
    is_trashed = bool(file_row.get("is_trashed"))

    if is_trashed:
        rows = [
            [
                InlineKeyboardButton(
                    text="♻️ Restore File",
                    callback_data=f"f:restore:{fid}",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="💥 Delete Forever",
                    callback_data=f"f:purge:{fid}",
                    style="danger",
                ),
            ],
            [
                back_btn("trash:0", "◀ Trash"),
                home_btn(),
            ],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    fav_label = "⭐ Starred" if file_row.get("is_favorite") else "☆ Star"
    inline_query = (file_row.get("title") or "")[:30]
    manage_cb = f"fm:{fid}:{back_cb}" if back_cb else f"f:manage:{fid}"

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="📥 Get Here",
                callback_data=f"f:get:{fid}",
                style="primary",
            ),
            InlineKeyboardButton(
                text="📤 Send to Chat",
                switch_inline_query=inline_query,
            ),
        ],
        [
            InlineKeyboardButton(
                text=fav_label,
                callback_data=f"ffav:{fid}:{back_cb}" if back_cb else f"f:fav:{fid}",
            ),
            InlineKeyboardButton(
                text="🛠️ Edit & More",
                callback_data=manage_cb,
            ),
        ],
    ]

    if file_row.get("share_token") and bot_username:
        share_url = f"https://t.me/{bot_username}?start=file_{file_row['share_token']}"
        rows.append([copy_btn("📋 Copy Public Link", share_url)])

    nav_row: list[InlineKeyboardButton] = []
    if back_cb:
        nav_row.append(back_btn(back_cb, "◀ Files"))
        nav_row.append(home_btn())
    else:
        nav_row.append(InlineKeyboardButton(text="📦 All Files", callback_data="br:all:def:0:0"))
        nav_row.append(home_btn())
    rows.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_file_manage_keyboard(
    file_row: dict[str, Any],
    back_cb: Optional[str] = None,
    bot_username: str = "",
) -> InlineKeyboardMarkup:
    """
    Secondary Management Drawer for a file (Progressive Disclosure level 2).
    Clean 2-column grid for metadata editing, folder organization, public link, and trash.
    """
    fid = file_row["id"]
    has_link = bool(file_row.get("share_token"))
    share_label = "🔗 Revoke Link" if has_link else "🔗 Public Link"
    return_to_card = f"fo:{fid}:{back_cb[3:]}" if (back_cb and back_cb.startswith("br:")) else f"f:open:{fid}"

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="✏️ Rename", callback_data=f"f:rename:{fid}"),
            InlineKeyboardButton(text="📁 Move Folder", callback_data=f"f:move:{fid}"),
        ],
        [
            InlineKeyboardButton(text="🏷️ Edit Tags", callback_data=f"f:tags:{fid}"),
            InlineKeyboardButton(text="💬 Caption", callback_data=f"f:cap:{fid}"),
        ],
        [
            InlineKeyboardButton(
                text=share_label,
                callback_data=f"fsh:{fid}:{back_cb}" if back_cb else f"f:share:{fid}",
            ),
            InlineKeyboardButton(
                text="🗑️ Trash",
                callback_data=f"f:trash:{fid}",
                style="danger",
            ),
        ],
    ]

    if has_link and bot_username:
        share_url = f"https://t.me/{bot_username}?start=file_{file_row['share_token']}"
        rows.insert(2, [copy_btn("📋 Copy Public Link", share_url, style="primary")])

    rows.append([back_btn(return_to_card, "◀ Back to File")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_upload_quick_keyboard(file_row: dict[str, Any]) -> InlineKeyboardMarkup:
    """
    Ultra-clean 2-row receipt keyboard shown right after uploading a file,
    avoiding button walls in the chat history while keeping 1-tap Manage & Send.
    """
    fid = file_row["id"]
    inline_query = (file_row.get("title") or "")[:30]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 Send to Chat",
                    switch_inline_query=inline_query,
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🛠️ Edit & Organize",
                    callback_data=f"f:manage:{fid}",
                ),
            ],
            [
                InlineKeyboardButton(text="📁 Move Folder", callback_data=f"f:move:{fid}"),
                InlineKeyboardButton(text="📦 My Files", callback_data="br:all:def:0:0"),
            ],
        ]
    )
