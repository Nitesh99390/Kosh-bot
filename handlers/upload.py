import asyncio
import re
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Config
from database import Database
from utils.formatters import (
    MEDIA_ICONS,
    build_file_card_text,
    build_upload_quick_keyboard,
    esc,
    home_btn,
)
from utils.states import PackStates

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
            "title": f"Round ({vn.duration}s)",
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
        title = caption[:60] if caption else f"Voice ({vc.duration}s)"
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
    await asyncio.sleep(1.1)
    items = _ALBUM_BUFFER.pop(album_key, [])
    _ALBUM_TASKS.pop(album_key, None)
    if not items:
        return

    saved_items = [x for x in items if x["status"] == "saved"]
    dup_items = [x for x in items if x["status"] == "dup"]

    title_line = (
        f"✅ <b>Album Saved</b> · <i>{len(saved_items)} new"
        + (f", {len(dup_items)} dup" if dup_items else "")
        + "</i>"
    )
    list_lines: list[str] = []
    for entry in items[:6]:
        f = entry["file"]
        ic = MEDIA_ICONS.get(f["media_type"], "📦")
        badge = "♻️ " if entry["status"] == "dup" else ""
        list_lines.append(f"• {badge}{ic} {esc(f['title'][:30])}")
    if len(items) > 6:
        list_lines.append(f"<i>…+{len(items) - 6} more</i>")

    body = title_line + "\n<blockquote>" + "\n".join(list_lines) + "</blockquote>"

    # Build 1-tap Pack callback if encoded IDs fit in 64 bytes
    all_ids = [str(entry["file"]["id"]) for entry in items]
    pack_cb = f"pk:quick_album:{','.join(all_ids)}"
    action_row = [
        InlineKeyboardButton(
            text="📦 Files",
            callback_data="br:all:def:0:0",
        ),
        InlineKeyboardButton(
            text="🔍 Inline",
            switch_inline_query_current_chat="",
        ),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    if len(pack_cb.encode("utf-8")) <= 64 and len(all_ids) >= 2:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🎒 Create Pack Link",
                    callback_data=pack_cb,
                    style="primary",
                )
            ]
        )
    rows.append(action_row)
    await anchor_message.reply(body, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(F.chat.type == "private", MEDIA_FILTER)
async def handle_media_upload(
    message: Message, state: FSMContext, bot: Bot, db: Database, config: Config
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

    cur_state = await state.get_state()
    in_pack_builder = cur_state == PackStates.collecting_items.state

    existing = await db.find_duplicate(user["vault_id"], meta["file_unique_id"])
    me = await bot.get_me()

    if existing:
        if existing.get("is_trashed"):
            await db.restore_file(existing["id"])
        target_file = await db.get_file(existing["id"])
        assert target_file is not None

        if in_pack_builder:
            await _append_to_pack_builder(message, state, target_file)
            return

        if message.media_group_id:
            album_key = f"{user['user_id']}:{message.media_group_id}"
            _ALBUM_BUFFER.setdefault(album_key, []).append(
                {"status": "dup", "file": target_file}
            )
            if album_key not in _ALBUM_TASKS:
                _ALBUM_TASKS[album_key] = asyncio.create_task(
                    _flush_album_summary(album_key, message)
                )
            return

        if user["bulk_mode"]:
            await message.reply(f"♻️ <i>In Vault:</i> <b>{esc(target_file['title'])}</b>")
        else:
            badge = (
                "♻️ <b>Restored</b>"
                if existing.get("is_trashed")
                else "♻️ <b>Already Saved</b>"
            )
            await message.reply(
                build_file_card_text(target_file, me.username or "", header_badge=badge),
                reply_markup=build_upload_quick_keyboard(target_file),
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
            f"⚠️ <b>Dump Channel Error</b>\n"
            f"<blockquote>Make @{esc(me.username)} Admin with Post Messages permission.</blockquote>\n"
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

    if in_pack_builder:
        await _append_to_pack_builder(message, state, saved)
        return

    # Batch media albums cleanly into a single summary card with 1-tap Pack Link
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
        await message.reply(f"✅ <b>{esc(saved['title'])}</b>{grp}")
        return

    await message.reply(
        build_file_card_text(
            saved,
            me.username or "",
            header_badge="✅ <b>Saved</b>",
        ),
        reply_markup=build_upload_quick_keyboard(saved),
        disable_web_page_preview=True,
    )


async def _append_to_pack_builder(
    message: Message, state: FSMContext, file_row: dict[str, Any]
) -> None:
    data = await state.get_data()
    fids: list[int] = list(data.get("pack_file_ids") or [])
    if file_row["id"] not in fids:
        fids.append(file_row["id"])
        await state.update_data(pack_file_ids=fids)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✓ Finish Pack ({len(fids)})",
                    callback_data="pk:finish",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="✕ Cancel",
                    callback_data="fsm:cancel:pk:list:0",
                ),
            ]
        ]
    )
    await message.reply(
        f"🎒 <b>Added #{len(fids)}:</b> {esc(file_row['title'][:36])}",
        reply_markup=kb,
    )
