import asyncio
import re
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Config
from database import Database
from utils.formatters import (
    DIVIDER,
    MEDIA_ICONS,
    build_file_card_text,
    build_file_keyboard,
    esc,
    home_btn,
)

router = Router(name="upload")

HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")

# Debounce buffer for Telegram media albums (media_group_id)
_ALBUM_BUFFER: dict[str, list[dict[str, Any]]] = {}
_ALBUM_TASKS: dict[str, asyncio.Task] = {}


def _extract_media(msg: Message) -> Optional[dict[str, Any]]:
    caption = (msg.caption or "").strip()
    hashtags = " ".join(f"#{m}" for m in HASHTAG_RE.findall(caption))

    if msg.audio:
        a = msg.audio
        title = a.title or a.file_name or "Untitled Audio"
        return {
            "media_type": "audio",
            "file_id": a.file_id,
            "file_unique_id": a.file_unique_id,
            "title": title,
            "performer": a.performer or "",
            "file_name": a.file_name or "",
            "mime_type": a.mime_type or "audio/mpeg",
            "file_size": a.file_size or 0,
            "duration": a.duration or 0,
            "caption": caption,
            "tags": hashtags,
        }

    if msg.document:
        d = msg.document
        title = d.file_name or (caption[:60] if caption else "Untitled Document")
        return {
            "media_type": "document",
            "file_id": d.file_id,
            "file_unique_id": d.file_unique_id,
            "title": title,
            "performer": "",
            "file_name": d.file_name or "",
            "mime_type": d.mime_type or "",
            "file_size": d.file_size or 0,
            "duration": 0,
            "caption": caption,
            "tags": hashtags,
        }

    if msg.video:
        v = msg.video
        title = v.file_name or (caption[:60] if caption else "Untitled Video")
        return {
            "media_type": "video",
            "file_id": v.file_id,
            "file_unique_id": v.file_unique_id,
            "title": title,
            "performer": "",
            "file_name": v.file_name or "",
            "mime_type": v.mime_type or "video/mp4",
            "file_size": v.file_size or 0,
            "duration": v.duration or 0,
            "caption": caption,
            "tags": hashtags,
        }

    if msg.video_note:
        vn = msg.video_note
        return {
            "media_type": "video_note",
            "file_id": vn.file_id,
            "file_unique_id": vn.file_unique_id,
            "title": f"Video Note ({vn.duration}s)",
            "performer": "",
            "file_name": "",
            "mime_type": "video/mp4",
            "file_size": vn.file_size or 0,
            "duration": vn.duration or 0,
            "caption": "",
            "tags": "",
        }

    if msg.voice:
        vc = msg.voice
        title = caption[:60] if caption else f"Voice Note ({vc.duration}s)"
        return {
            "media_type": "voice",
            "file_id": vc.file_id,
            "file_unique_id": vc.file_unique_id,
            "title": title,
            "performer": "",
            "file_name": "",
            "mime_type": vc.mime_type or "audio/ogg",
            "file_size": vc.file_size or 0,
            "duration": vc.duration or 0,
            "caption": caption,
            "tags": hashtags,
        }

    if msg.photo:
        p = msg.photo[-1]
        title = caption[:60] if caption else f"Photo_{p.file_unique_id[:6]}"
        return {
            "media_type": "photo",
            "file_id": p.file_id,
            "file_unique_id": p.file_unique_id,
            "title": title,
            "performer": "",
            "file_name": "",
            "mime_type": "image/jpeg",
            "file_size": p.file_size or 0,
            "duration": 0,
            "caption": caption,
            "tags": hashtags,
        }

    if msg.animation:
        an = msg.animation
        title = an.file_name or (caption[:60] if caption else "GIF Animation")
        return {
            "media_type": "animation",
            "file_id": an.file_id,
            "file_unique_id": an.file_unique_id,
            "title": title,
            "performer": "",
            "file_name": an.file_name or "",
            "mime_type": an.mime_type or "video/mp4",
            "file_size": an.file_size or 0,
            "duration": an.duration or 0,
            "caption": caption,
            "tags": hashtags,
        }

    if msg.sticker:
        st = msg.sticker
        emoji = st.emoji or "🧩"
        set_name = st.set_name or "Sticker"
        return {
            "media_type": "sticker",
            "file_id": st.file_id,
            "file_unique_id": st.file_unique_id,
            "title": f"{emoji} {set_name}",
            "performer": "",
            "file_name": "",
            "mime_type": "",
            "file_size": st.file_size or 0,
            "duration": 0,
            "caption": "",
            "tags": emoji,
        }

    return None


MEDIA_FILTER = (
    F.audio
    | F.document
    | F.video
    | F.video_note
    | F.voice
    | F.photo
    | F.animation
    | F.sticker
)


async def _flush_album_summary(
    album_key: str, anchor_message: Message
) -> None:
    await asyncio.sleep(1.2)
    items = _ALBUM_BUFFER.pop(album_key, [])
    _ALBUM_TASKS.pop(album_key, None)
    if not items:
        return

    saved_items = [x for x in items if x["status"] == "saved"]
    dup_items = [x for x in items if x["status"] == "dup"]

    lines = [
        f"✓ <b>Album Archived</b> ({len(saved_items)} saved"
        + (f", {len(dup_items)} duplicate" if dup_items else "")
        + ")",
        DIVIDER,
    ]
    for entry in items[:8]:
        f = entry["file"]
        ic = MEDIA_ICONS.get(f["media_type"], "📦")
        badge = "♻️ " if entry["status"] == "dup" else ""
        lines.append(f"• {badge}{ic} <b>{esc(f['title'][:36])}</b>")
    if len(items) > 8:
        lines.append(f"<i>…and {len(items) - 8} more item(s)</i>")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 Browse Files", callback_data="br:all:def:0:0"),
                InlineKeyboardButton(
                    text="🔍 Open Inline",
                    switch_inline_query_current_chat="",
                ),
            ],
            [home_btn()],
        ]
    )
    await anchor_message.reply("\n".join(lines), reply_markup=kb)


@router.message(F.chat.type == "private", MEDIA_FILTER)
async def handle_media_upload(
    message: Message, bot: Bot, db: Database, config: Config
) -> None:
    if not message.from_user:
        return

    user = await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    meta = _extract_media(message)
    if not meta:
        return

    existing = await db.find_duplicate(user["vault_id"], meta["file_unique_id"])
    me = await bot.get_me()

    # Auto-restore if the duplicate was sitting in Recycle Trash
    if existing and existing.get("is_trashed"):
        await db.restore_file(existing["id"])
        restored = await db.get_file(existing["id"])
        await message.reply(
            build_file_card_text(
                restored,
                me.username or "",
                header_badge="♻️ <b>Restored from Trash</b>",
            ),
            reply_markup=build_file_keyboard(restored),
            disable_web_page_preview=True,
        )
        return

    if existing:
        existing_full = await db.get_file(existing["id"])
        if message.media_group_id:
            album_key = f"{user['user_id']}:{message.media_group_id}"
            _ALBUM_BUFFER.setdefault(album_key, []).append(
                {"status": "dup", "file": existing_full}
            )
            if album_key not in _ALBUM_TASKS:
                _ALBUM_TASKS[album_key] = asyncio.create_task(
                    _flush_album_summary(album_key, message)
                )
            return

        if user["bulk_mode"]:
            await message.reply(f"♻️ <i>Already archived:</i> <b>{esc(existing_full['title'])}</b>")
        else:
            await message.reply(
                build_file_card_text(
                    existing_full,
                    me.username or "",
                    header_badge="♻️ <b>Already in Your Vault</b>",
                ),
                reply_markup=build_file_keyboard(existing_full),
                disable_web_page_preview=True,
            )
        return

    # Forward to dump channel for permanent Telegram cloud storage
    try:
        dumped = await bot.forward_message(
            chat_id=config.dump_channel_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception as exc:
        await message.reply(
            f"⚠️ <b>Archive Channel Error</b>\n{DIVIDER}\n"
            f"Ensure @{esc(me.username)} is an <b>Administrator</b> in your Dump Channel with Post Messages permission.\n\n"
            f"<code>{esc(str(exc))}</code>"
        )
        return

    dumped_meta = _extract_media(dumped)
    stored_file_id = dumped_meta["file_id"] if dumped_meta else meta["file_id"]

    active_group_id = user.get("active_group_id")
    if active_group_id and not await db.is_vault_in_group(user["vault_id"], active_group_id):
        active_group_id = None
        await db.set_active_group(user["user_id"], None)

    saved = await db.add_file(
        vault_id=user["vault_id"],
        uploader_user_id=user["user_id"],
        group_id=active_group_id,
        media_type=meta["media_type"],
        file_id=stored_file_id,
        file_unique_id=meta["file_unique_id"],
        dump_message_id=dumped.message_id,
        title=meta["title"],
        performer=meta["performer"],
        file_name=meta["file_name"],
        mime_type=meta["mime_type"],
        file_size=meta["file_size"],
        duration=meta["duration"],
        caption=meta["caption"],
        tags=meta["tags"],
    )

    # Batch media albums cleanly into a single summary card
    if message.media_group_id and not user["bulk_mode"]:
        album_key = f"{user['user_id']}:{message.media_group_id}"
        _ALBUM_BUFFER.setdefault(album_key, []).append(
            {"status": "saved", "file": saved}
        )
        if album_key not in _ALBUM_TASKS:
            _ALBUM_TASKS[album_key] = asyncio.create_task(
                _flush_album_summary(album_key, message)
            )
        return

    if user["bulk_mode"]:
        grp = f" · 📁 {esc(saved['group_name'])}" if saved.get("group_name") else ""
        await message.reply(f"✓ <b>Archived:</b> {esc(saved['title'])}{grp}")
        return

    await message.reply(
        build_file_card_text(
            saved,
            me.username or "",
            header_badge="✓ <b>Saved to Vault</b>",
        ),
        reply_markup=build_file_keyboard(saved),
        disable_web_page_preview=True,
    )
