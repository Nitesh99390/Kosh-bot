import asyncio
import os
import re
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Config
from database import Database
from handlers.packs import render_pack_detail
from utils.formatters import (
    MEDIA_ICONS,
    breadcrumb,
    build_file_card_text,
    build_upload_quick_keyboard,
    esc,
    format_size,
    tg_retry_call,
)
from utils.states import PackStates

router = Router(name="upload")

HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")

# Debounce windows (in seconds) — tunable via env or overridden in tests for fast assertions
DROP_WINDOW_SECONDS: float = float(os.getenv("MULTI_DROP_WINDOW_SECONDS", "1.4"))
PACK_STATUS_DEBOUNCE_SECONDS: float = float(
    os.getenv("PACK_BUILDER_DEBOUNCE_SECONDS", "0.9")
)

# Per-user burst buffers for automatic Multi-File Pack creation (>1 file dropped together)
_USER_DROP_BUFFER: dict[int, list[dict[str, Any]]] = {}
_USER_DROP_TASKS: dict[int, asyncio.Task] = {}
_USER_DROP_LOCKS: dict[int, asyncio.Lock] = {}

# Per-user debounced live status updater for interactive Pack Builder (`/batch`, `➕ New Pack`, `➕ Add Files`)
_PACK_BUILDER_TASKS: dict[int, asyncio.Task] = {}
_PACK_BUILDER_LOCKS: dict[int, asyncio.Lock] = {}


def _get_drop_lock(user_id: int) -> asyncio.Lock:
    lock = _USER_DROP_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _USER_DROP_LOCKS[user_id] = lock
    return lock


def _get_builder_lock(user_id: int) -> asyncio.Lock:
    lock = _PACK_BUILDER_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _PACK_BUILDER_LOCKS[user_id] = lock
    return lock


def _clean_auto_pack_title(files: list[dict[str, Any]]) -> str:
    """Derives a clean, human-readable Pack title from a batch of uploaded files."""
    if not files:
        return "File Pack"
    first_raw = (files[0].get("title") or "File Pack").strip()
    # Strip common file extensions
    base = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", first_raw)
    base = base.replace("_", " ").strip()
    # Strip trailing episode/chapter/part numbers if multiple files share a series prefix
    if len(files) >= 2:
        stripped = re.sub(r"[\s\-–_#]*(?:ep|ch|part|pt|vol)?[\s\-_#]*\d+\s*$", "", base, flags=re.IGNORECASE).strip()
        if len(stripped) >= 4:
            base = stripped
    base = base[:38].strip() or "File Pack"
    return f"{base} ({len(files)})"[:60]


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


async def _queue_user_drop(
    user_id: int,
    entry: dict[str, Any],
    anchor_message: Message,
    bot: Bot,
    db: Database,
) -> None:
    """
    Sliding-window coalescer for all incoming user drops outside Pack Builder:
    - 1 file dropped alone -> sends standard single-file receipt card (or 1-line Bulk receipt).
    - 2 or more files dropped/forwarded together (even 100+ files!) -> automatically
      saves all of them into ONE Pack and sends ONE clean Pack Share Link card!
    """
    async with _get_drop_lock(user_id):
        _USER_DROP_BUFFER.setdefault(user_id, []).append(entry)
        prev_task = _USER_DROP_TASKS.get(user_id)
        if prev_task and not prev_task.done():
            prev_task.cancel()
        _USER_DROP_TASKS[user_id] = asyncio.create_task(
            _flush_user_drop(user_id, anchor_message, bot, db)
        )


async def _flush_user_drop(
    user_id: int,
    anchor_message: Message,
    bot: Bot,
    db: Database,
) -> None:
    try:
        await asyncio.sleep(DROP_WINDOW_SECONDS)
    except asyncio.CancelledError:
        return

    async with _get_drop_lock(user_id):
        items = _USER_DROP_BUFFER.pop(user_id, [])
        _USER_DROP_TASKS.pop(user_id, None)

    if not items:
        return

    user = await db.get_user(user_id)
    if not user:
        return
    me = await bot.get_me()

    # Case 1: Exactly ONE file sent -> keep crisp single-file behavior
    if len(items) == 1:
        item = items[0]
        f = item["file"]
        status = item["status"]

        if user.get("bulk_mode"):
            if status in ("dup", "restored"):
                await tg_retry_call(
                    anchor_message.reply,
                    f"♻️ <i>In Vault:</i> <b>{esc(f['title'])}</b>",
                )
            else:
                grp = f" · 📁 {esc(f['group_name'])}" if f.get("group_name") else ""
                await tg_retry_call(
                    anchor_message.reply,
                    f"✅ <b>{esc(f['title'])}</b>{grp}",
                )
            return

        if status == "restored":
            badge = "♻️ <b>Restored</b>"
        elif status == "dup":
            badge = "♻️ <b>Already Saved</b>"
        else:
            badge = "✅ <b>Saved</b>"

        await tg_retry_call(
            anchor_message.reply,
            build_file_card_text(f, me.username or "", header_badge=badge),
            reply_markup=build_upload_quick_keyboard(f),
            disable_web_page_preview=True,
        )
        return

    # Case 2: 2 or MORE files sent together -> Auto-Bundle into a Pack with 1 clean card!
    unique_files: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    new_cnt = 0
    dup_cnt = 0
    for entry in items:
        f = entry["file"]
        if entry["status"] == "saved":
            new_cnt += 1
        else:
            dup_cnt += 1
        if f["id"] not in seen_ids:
            seen_ids.add(f["id"])
            unique_files.append(f)

    # Edge case: user sent the exact same duplicate file twice -> treat as single duplicate
    if len(unique_files) == 1:
        f = unique_files[0]
        await tg_retry_call(
            anchor_message.reply,
            build_file_card_text(
                f, me.username or "", header_badge="♻️ <b>Already Saved</b>"
            ),
            reply_markup=build_upload_quick_keyboard(f),
            disable_web_page_preview=True,
        )
        return

    pack_title = _clean_auto_pack_title(unique_files)
    pack = await db.create_pack(
        vault_id=user["vault_id"],
        creator_user_id=user["user_id"],
        title=pack_title,
        file_ids=[f["id"] for f in unique_files],
        protect_content=bool(user.get("protect_content")),
    )

    rendered = await render_pack_detail(user_id, pack["id"], db, bot)
    if rendered:
        stat_note = f"✅ <b>Auto-Packed {len(unique_files)} files</b>"
        if dup_cnt:
            stat_note += f" <i>({new_cnt} new · {dup_cnt} in vault)</i>"
        await tg_retry_call(
            anchor_message.reply,
            f"{stat_note}\n{rendered[0]}",
            reply_markup=rendered[1],
            disable_web_page_preview=True,
        )


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

    if existing:
        was_trashed = bool(existing.get("is_trashed"))
        if was_trashed:
            await db.restore_file(existing["id"])
        target_file = await db.get_file(existing["id"])
        assert target_file is not None

        if in_pack_builder:
            await _append_to_pack_builder(message, state, bot, target_file)
            return

        await _queue_user_drop(
            user["user_id"],
            {"status": "restored" if was_trashed else "dup", "file": target_file},
            message,
            bot,
            db,
        )
        return

    # Forward to dump channel for permanent Telegram cloud storage (FloodWait-safe)
    try:
        dumped = await tg_retry_call(
            bot.forward_message,
            chat_id=config.dump_channel_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception as exc:
        me = await bot.get_me()
        await tg_retry_call(
            message.reply,
            f"⚠️ <b>Dump Channel Error</b>\n"
            f"<blockquote>Make @{esc(me.username)} Admin with Post Messages permission.</blockquote>\n"
            f"<code>{esc(str(exc))}</code>",
        )
        return

    if dumped is None:
        return

    dumped_meta = _extract_media(dumped)
    stored_file_id = dumped_meta["file_id"] if dumped_meta else meta["file_id"]

    active_group_id = user.get("active_group_id")
    if active_group_id and not await db.is_vault_in_group(
        user["vault_id"], active_group_id
    ):
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
        await _append_to_pack_builder(message, state, bot, saved)
        return

    await _queue_user_drop(
        user["user_id"],
        {"status": "saved", "file": saved},
        message,
        bot,
        db,
    )


def _build_live_pack_status_card(
    fids_count: int,
    total_size: int,
    recent_titles: list[str],
    append_pack_id: Optional[int] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Builds the single consolidated live Pack Builder status message (zero spam!)."""
    mode_lbl = "Add to Pack" if append_pack_id else "Pack Builder"
    cancel_target = f"pk:open:{append_pack_id}" if append_pack_id else "pk:list:0"
    finish_lbl = (
        f"✓ Save {fids_count} to Pack"
        if append_pack_id
        else f"✓ Finish Pack ({fids_count})"
    )

    recent_lines = [f"• {esc(t[:36])}" for t in recent_titles[-4:]]
    if fids_count > len(recent_lines):
        recent_lines.insert(0, f"<i>…+{fids_count - len(recent_lines)} earlier file(s)</i>")
    preview = "\n".join(recent_lines) if recent_lines else "<i>Waiting for files…</i>"

    text = (
        f"{breadcrumb('Vault', 'Packs', mode_lbl)}\n"
        f"🎒 <b>{mode_lbl}</b> · <b>{fids_count}</b> queued <i>({format_size(total_size)})</i>\n"
        f"<blockquote>{preview}</blockquote>\n"
        f"<i>Send more files or tap <b>{esc(finish_lbl)}</b> below.</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=finish_lbl,
                    callback_data="pk:finish",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="✕ Cancel",
                    callback_data=f"fsm:cancel:{cancel_target}",
                ),
            ]
        ]
    )
    return text, kb


async def _append_to_pack_builder(
    message: Message,
    state: FSMContext,
    bot: Bot,
    file_row: dict[str, Any],
) -> None:
    """
    Appends a file to the active Pack Builder session WITHOUT sending 100 separate messages!
    Debounces updates and edits a SINGLE live status card in-place.
    """
    if not message.from_user:
        return
    uid = message.from_user.id

    async with _get_builder_lock(uid):
        data = await state.get_data()
        fids: list[int] = list(data.get("pack_file_ids") or [])
        total_sz = int(data.get("pack_total_size") or 0)
        recent: list[str] = list(data.get("pack_recent_titles") or [])

        if file_row["id"] not in fids:
            fids.append(file_row["id"])
            total_sz += int(file_row.get("file_size") or 0)
            icon = MEDIA_ICONS.get(file_row["media_type"], "📦")
            recent.append(f"{icon} {file_row['title']}")
            recent = recent[-5:]
            await state.update_data(
                pack_file_ids=fids,
                pack_total_size=total_sz,
                pack_recent_titles=recent,
            )

        prev_task = _PACK_BUILDER_TASKS.get(uid)
        if prev_task and not prev_task.done():
            prev_task.cancel()
        _PACK_BUILDER_TASKS[uid] = asyncio.create_task(
            _flush_pack_builder_status(uid, message.chat.id, state, bot)
        )


async def _flush_pack_builder_status(
    user_id: int,
    chat_id: int,
    state: FSMContext,
    bot: Bot,
) -> None:
    try:
        await asyncio.sleep(PACK_STATUS_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return

    async with _get_builder_lock(user_id):
        _PACK_BUILDER_TASKS.pop(user_id, None)
        cur_state = await state.get_state()
        if cur_state != PackStates.collecting_items.state:
            return

        data = await state.get_data()
        fids: list[int] = list(data.get("pack_file_ids") or [])
        if not fids:
            return
        total_sz = int(data.get("pack_total_size") or 0)
        recent: list[str] = list(data.get("pack_recent_titles") or [])
        status_msg_id: Optional[int] = data.get("pack_status_msg_id")
        append_pack_id: Optional[int] = data.get("append_pack_id")

        text, kb = _build_live_pack_status_card(
            len(fids), total_sz, recent, append_pack_id=append_pack_id
        )

        # Edit existing collector/status card in-place so chat stays 100% clean
        if status_msg_id:
            try:
                await tg_retry_call(
                    bot.edit_message_text,
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=text,
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
            except Exception:
                pass

        sent = await tg_retry_call(
            bot.send_message,
            chat_id=chat_id,
            text=text,
            reply_markup=kb,
        )
        if sent and getattr(sent, "message_id", None):
            await state.update_data(pack_status_msg_id=sent.message_id)
