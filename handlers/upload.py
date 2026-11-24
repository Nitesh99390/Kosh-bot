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
    BATCH_CHUNK_SIZE,
    EFFECT_PARTY,
    EFFECT_THUMBS_UP,
    MEDIA_ICONS,
    breadcrumb,
    build_file_card_text,
    build_upload_quick_keyboard,
    esc,
    format_size,
    send_with_effect,
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
_MAX_LOCK_POOL = 1024
_USER_DROP_BUFFER: dict[int, list[dict[str, Any]]] = {}
_USER_DROP_TASKS: dict[int, asyncio.Task] = {}
_USER_DROP_LOCKS: dict[int, asyncio.Lock] = {}

# Per-user burst buffers & debounced live status updater for interactive Pack Builder (`/batch`, `➕ New Pack`, `➕ Add Files`)
_PACK_BUILDER_BUFFER: dict[int, list[dict[str, Any]]] = {}
_PACK_BUILDER_TASKS: dict[int, asyncio.Task] = {}
_PACK_BUILDER_LOCKS: dict[int, asyncio.Lock] = {}


def _prune_idle_locks(
    lock_map: dict[int, asyncio.Lock],
    active_tasks: dict[int, asyncio.Task],
    active_buffers: Optional[dict[int, Any]] = None,
) -> None:
    if len(lock_map) <= _MAX_LOCK_POOL:
        return
    idle_uids = [
        uid
        for uid, lk in lock_map.items()
        if not lk.locked()
        and uid not in active_tasks
        and (active_buffers is None or uid not in active_buffers)
    ]
    for uid in idle_uids:
        lock_map.pop(uid, None)


def _get_drop_lock(user_id: int) -> asyncio.Lock:
    lock = _USER_DROP_LOCKS.get(user_id)
    if lock is None:
        _prune_idle_locks(_USER_DROP_LOCKS, _USER_DROP_TASKS, _USER_DROP_BUFFER)
        lock = asyncio.Lock()
        _USER_DROP_LOCKS[user_id] = lock
    return lock


def _get_builder_lock(user_id: int) -> asyncio.Lock:
    lock = _PACK_BUILDER_LOCKS.get(user_id)
    if lock is None:
        _prune_idle_locks(
            _PACK_BUILDER_LOCKS, _PACK_BUILDER_TASKS, _PACK_BUILDER_BUFFER
        )
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
        stripped = re.sub(
            r"[\s\-–_#]*(?:ep|ch|part|pt|vol)?[\s\-_#]*\d+\s*$",
            "",
            base,
            flags=re.IGNORECASE,
        ).strip()
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


def _extract_msg_id(obj: Any) -> Optional[int]:
    mid = getattr(obj, "message_id", None)
    if mid is None and isinstance(obj, dict):
        mid = obj.get("message_id")
    if mid is None and isinstance(obj, int):
        mid = obj
    return int(mid) if mid is not None else None


async def _archive_sorted_entries(
    sorted_entries: list[dict[str, Any]],
    user: dict[str, Any],
    bot: Bot,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> list[dict[str, Any]]:
    """
    Archives a `message.message_id`-ascending sorted list of incoming media messages into
    `DUMP_CHANNEL_ID` and SQLite in STRICT ascending `message_id` order (1 -> N):
    - Dedupes/restores existing vault files first without redundant dump forwards.
    - Archives new unique files in exact `message.message_id` ascending order using
      `bot.forward_messages(..., message_ids=sorted_msg_ids_chunk, disable_notification=True)`
      in chunks of <= 100 (with sequential per-message `forward_message` fallback — NEVER
      concurrent `asyncio.gather` forwarding that races order!).
    - Saves rows into `files` sequentially in that exact order so both `dump_message_id`
      and `pack_items.position` (`1, 2, ..., N`) are strictly sequential!
    """
    if not sorted_entries:
        return []

    uname = bot_username or "KoshBox_Bot"
    active_group_id = user.get("active_group_id")
    if active_group_id and not await db.is_vault_in_group(
        user["vault_id"], active_group_id
    ):
        active_group_id = None
        await db.set_active_group(user["user_id"], None)

    # Pass 1: Check duplicates in vault & deduplicate within the same burst
    resolved_by_idx: dict[int, dict[str, Any]] = {}
    seen_new_uniques: dict[str, int] = {}  # file_unique_id -> first_idx
    dup_of_first_idx: dict[int, int] = {}  # idx -> first_idx
    to_forward_indices: list[int] = []

    for idx, item in enumerate(sorted_entries):
        meta = item["meta"]
        f_uniq = meta["file_unique_id"]
        existing = await db.find_duplicate(user["vault_id"], f_uniq)
        if existing:
            was_trashed = bool(existing.get("is_trashed"))
            if was_trashed:
                await db.restore_file(existing["id"])
            target_file = await db.get_file(existing["id"])
            if target_file is not None:
                resolved_by_idx[idx] = {
                    "status": "restored" if was_trashed else "dup",
                    "file": target_file,
                }
            continue

        if f_uniq in seen_new_uniques:
            dup_of_first_idx[idx] = seen_new_uniques[f_uniq]
            continue

        seen_new_uniques[f_uniq] = idx
        to_forward_indices.append(idx)

    # Pass 2: Archive new files to DUMP_CHANNEL_ID in exact ascending `message_id` order
    forward_batch_fn = getattr(bot, "forward_messages", None)
    dumped_info_by_idx: dict[int, tuple[int, str]] = {}  # idx -> (dump_message_id, stored_file_id)
    error_reported = False

    # Group contiguous entries sharing the same `from_chat_id` (always 1 chat in private DM)
    c_pos = 0
    while c_pos < len(to_forward_indices):
        first_msg: Message = sorted_entries[to_forward_indices[c_pos]]["message"]
        from_chat_id = first_msg.chat.id
        chunk_indices: list[int] = []
        prev_mid = -1
        while (
            c_pos < len(to_forward_indices)
            and len(chunk_indices) < BATCH_CHUNK_SIZE
        ):
            cand_idx = to_forward_indices[c_pos]
            cand_msg: Message = sorted_entries[cand_idx]["message"]
            if cand_msg.chat.id != from_chat_id or cand_msg.message_id <= prev_mid:
                break
            chunk_indices.append(cand_idx)
            prev_mid = cand_msg.message_id
            c_pos += 1

        if not chunk_indices:
            # Safety guard if duplicate message_id ever appeared in mock/synthetic tests
            chunk_indices = [to_forward_indices[c_pos]]
            c_pos += 1

        chunk_succeeded = False
        if forward_batch_fn is not None and len(chunk_indices) > 1:
            sorted_msg_ids_chunk = [
                int(sorted_entries[i]["message"].message_id) for i in chunk_indices
            ]
            try:
                fwd_res = await tg_retry_call(
                    forward_batch_fn,
                    chat_id=config.dump_channel_id,
                    from_chat_id=from_chat_id,
                    message_ids=sorted_msg_ids_chunk,
                    disable_notification=True,
                )
                if fwd_res and len(fwd_res) == len(chunk_indices):
                    parsed_pairs: list[tuple[int, int, str]] = []
                    valid_all = True
                    for sub_i, res_obj in enumerate(fwd_res):
                        d_mid = _extract_msg_id(res_obj)
                        if not d_mid:
                            valid_all = False
                            break
                        orig_meta = sorted_entries[chunk_indices[sub_i]]["meta"]
                        d_meta = (
                            _extract_media(res_obj)
                            if hasattr(res_obj, "audio")
                            else None
                        )
                        s_fid = d_meta["file_id"] if d_meta else orig_meta["file_id"]
                        parsed_pairs.append((chunk_indices[sub_i], d_mid, s_fid))
                    if valid_all:
                        for orig_i, d_mid, s_fid in parsed_pairs:
                            dumped_info_by_idx[orig_i] = (d_mid, s_fid)
                        chunk_succeeded = True
            except Exception:
                chunk_succeeded = False

        if not chunk_succeeded:
            # Sequential per-message fallback — strictly in ascending `message_id` order!
            for orig_i in chunk_indices:
                msg_obj: Message = sorted_entries[orig_i]["message"]
                orig_meta = sorted_entries[orig_i]["meta"]
                try:
                    dumped = await tg_retry_call(
                        bot.forward_message,
                        chat_id=config.dump_channel_id,
                        from_chat_id=msg_obj.chat.id,
                        message_id=msg_obj.message_id,
                    )
                except Exception as exc:
                    if not error_reported:
                        error_reported = True
                        await tg_retry_call(
                            msg_obj.reply,
                            f"⚠️ <b>Dump Channel Error</b>\n"
                            f"<blockquote>Make @{esc(uname)} Admin with Post Messages permission.</blockquote>\n"
                            f"<code>{esc(str(exc))}</code>",
                        )
                    continue
                if dumped is None:
                    continue
                d_mid = _extract_msg_id(dumped)
                if not d_mid:
                    continue
                dumped_meta = (
                    _extract_media(dumped) if hasattr(dumped, "audio") else None
                )
                stored_file_id = (
                    dumped_meta["file_id"] if dumped_meta else orig_meta["file_id"]
                )
                dumped_info_by_idx[orig_i] = (d_mid, stored_file_id)

    # Pass 3: Persist into `files` in exact ascending `message.message_id` order
    for orig_i in to_forward_indices:
        if orig_i not in dumped_info_by_idx:
            continue
        d_mid, stored_file_id = dumped_info_by_idx[orig_i]
        meta = sorted_entries[orig_i]["meta"]
        saved = await db.add_file(
            vault_id=user["vault_id"],
            uploader_user_id=user["user_id"],
            group_id=active_group_id,
            media_type=meta["media_type"],
            file_id=stored_file_id,
            file_unique_id=meta["file_unique_id"],
            dump_message_id=d_mid,
            title=meta["title"],
            performer=meta["performer"],
            file_name=meta["file_name"],
            mime_type=meta["mime_type"],
            file_size=meta["file_size"],
            duration=meta["duration"],
            caption=meta["caption"],
            tags=meta["tags"],
        )
        if saved is not None:
            resolved_by_idx[orig_i] = {"status": "saved", "file": saved}

    for dup_i, first_i in dup_of_first_idx.items():
        first_res = resolved_by_idx.get(first_i)
        if first_res is not None:
            resolved_by_idx[dup_i] = {"status": "dup", "file": first_res["file"]}

    results: list[dict[str, Any]] = []
    for idx in range(len(sorted_entries)):
        if idx in resolved_by_idx:
            results.append(resolved_by_idx[idx])
    return results


async def _queue_user_drop(
    user_id: int,
    raw_entry: dict[str, Any],
    bot: Bot,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> None:
    """
    Sliding-window coalescer for all incoming user drops outside Pack Builder:
    - Buffers incoming media messages BEFORE forwarding them to `DUMP_CHANNEL_ID` so
      concurrent handler invocations can never race `forward_message` out of order!
    - On flush, sorts buffered messages strictly by `message.message_id` ascending (1 -> N),
      archives them sequentially/batch via `forward_messages`, and creates the Pack in exact order.
    """
    async with _get_drop_lock(user_id):
        buf = _USER_DROP_BUFFER.setdefault(user_id, [])
        if len(buf) < 500:
            buf.append(raw_entry)
        prev_task = _USER_DROP_TASKS.get(user_id)
        if prev_task and not prev_task.done():
            prev_task.cancel()
        _USER_DROP_TASKS[user_id] = asyncio.create_task(
            _flush_user_drop(user_id, bot, db, config, bot_username)
        )


async def _flush_user_drop(
    user_id: int,
    bot: Bot,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> None:
    try:
        await asyncio.sleep(DROP_WINDOW_SECONDS)
    except asyncio.CancelledError:
        return

    async with _get_drop_lock(user_id):
        raw_items = _USER_DROP_BUFFER.pop(user_id, [])
        _USER_DROP_TASKS.pop(user_id, None)
    if not _USER_DROP_BUFFER.get(user_id) and user_id not in _USER_DROP_TASKS:
        _USER_DROP_LOCKS.pop(user_id, None)

    if not raw_items:
        return

    # Sort buffered messages strictly by `message.message_id` ascending (exact forward order!)
    sorted_raw = sorted(raw_items, key=lambda e: int(e["message"].message_id))
    anchor_message: Message = sorted_raw[-1]["message"]

    user = await db.get_user(user_id)
    if not user:
        return
    uname = bot_username or "KoshBox_Bot"

    items = await _archive_sorted_entries(
        sorted_raw, user, bot, db, config, bot_username=uname
    )
    if not items:
        return

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
            eff = EFFECT_THUMBS_UP
        elif status == "dup":
            badge = "♻️ <b>Already Saved</b>"
            eff = None
        else:
            badge = "✅ <b>Saved</b>"
            eff = EFFECT_THUMBS_UP

        await send_with_effect(
            anchor_message.reply,
            build_file_card_text(f, uname, header_badge=badge),
            reply_markup=build_upload_quick_keyboard(f),
            disable_web_page_preview=True,
            message_effect_id=eff,
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
                f, uname, header_badge="♻️ <b>Already Saved</b>"
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

    rendered = await render_pack_detail(
        user_id, pack["id"], db, bot, bot_username=uname
    )
    if rendered:
        stat_note = f"✅ <b>Auto-Packed {len(unique_files)} files</b>"
        if dup_cnt:
            stat_note += f" <i>({new_cnt} new · {dup_cnt} in vault)</i>"
        await send_with_effect(
            anchor_message.reply,
            f"{stat_note}\n{rendered[0]}",
            reply_markup=rendered[1],
            disable_web_page_preview=True,
            message_effect_id=EFFECT_PARTY,
        )


@router.message(F.chat.type == "private", MEDIA_FILTER)
async def handle_media_upload(
    message: Message,
    state: FSMContext,
    bot: Bot,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
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
    uname = bot_username or "KoshBox_Bot"

    raw_entry = {"message": message, "meta": meta}

    if in_pack_builder:
        await _queue_pack_builder_item(
            user["user_id"],
            raw_entry,
            state,
            bot,
            db,
            config,
            bot_username=uname,
        )
        return

    await _queue_user_drop(
        user["user_id"],
        raw_entry,
        bot,
        db,
        config,
        uname,
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
        recent_lines.insert(
            0, f"<i>…+{fids_count - len(recent_lines)} earlier file(s)</i>"
        )
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


async def _drain_pack_builder_buffer_locked(
    user_id: int,
    state: FSMContext,
    bot: Bot,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> None:
    """
    Flushes any buffered incoming messages for `user_id` inside `_get_builder_lock(user_id)`:
    sorts strictly by `message.message_id` ascending, archives to `DUMP_CHANNEL_ID` in exact
    order, and appends the resulting `file_id`s into FSM `pack_file_ids`.
    """
    raw_items = _PACK_BUILDER_BUFFER.pop(user_id, [])
    if not raw_items:
        return

    user = await db.get_user(user_id)
    if not user:
        return

    sorted_raw = sorted(raw_items, key=lambda e: int(e["message"].message_id))
    archived = await _archive_sorted_entries(
        sorted_raw, user, bot, db, config, bot_username=bot_username
    )
    if not archived:
        return

    data = await state.get_data()
    fids: list[int] = list(data.get("pack_file_ids") or [])
    total_sz = int(data.get("pack_total_size") or 0)
    recent: list[str] = list(data.get("pack_recent_titles") or [])

    changed = False
    for entry in archived:
        file_row = entry["file"]
        if file_row["id"] not in fids:
            fids.append(file_row["id"])
            total_sz += int(file_row.get("file_size") or 0)
            icon = MEDIA_ICONS.get(file_row["media_type"], "📦")
            recent.append(f"{icon} {file_row['title']}")
            changed = True

    if changed:
        recent = recent[-5:]
        await state.update_data(
            pack_file_ids=fids,
            pack_total_size=total_sz,
            pack_recent_titles=recent,
        )


async def flush_pending_pack_builder_uploads(
    user_id: int,
    state: FSMContext,
    bot: Bot,
    db: Database,
    config: Optional[Config] = None,
    bot_username: str = "KoshBox_Bot",
) -> None:
    """
    Ensures any still-buffered Pack Builder drops are archived & ordered BEFORE `/done`
    or `✓ Finish Pack` reads `pack_file_ids`, even if the user taps Finish immediately!
    """
    if config is None:
        return
    async with _get_builder_lock(user_id):
        prev_task = _PACK_BUILDER_TASKS.pop(user_id, None)
        if prev_task and not prev_task.done():
            prev_task.cancel()
        await _drain_pack_builder_buffer_locked(
            user_id, state, bot, db, config, bot_username=bot_username
        )


async def _queue_pack_builder_item(
    user_id: int,
    raw_entry: dict[str, Any],
    state: FSMContext,
    bot: Bot,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> None:
    """
    Buffers incoming Pack Builder media messages BEFORE forwarding to `DUMP_CHANNEL_ID`,
    sorts them by `message.message_id` ascending on debounce flush, and edits a SINGLE
    live status card in-place (zero spam, 100% deterministic order).
    """
    chat_id = raw_entry["message"].chat.id
    async with _get_builder_lock(user_id):
        buf = _PACK_BUILDER_BUFFER.setdefault(user_id, [])
        if len(buf) < 500:
            buf.append(raw_entry)
        prev_task = _PACK_BUILDER_TASKS.get(user_id)
        if prev_task and not prev_task.done():
            prev_task.cancel()
        _PACK_BUILDER_TASKS[user_id] = asyncio.create_task(
            _flush_pack_builder_status(
                user_id, chat_id, state, bot, db, config, bot_username
            )
        )


async def _flush_pack_builder_status(
    user_id: int,
    chat_id: int,
    state: FSMContext,
    bot: Bot,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> None:
    try:
        await asyncio.sleep(PACK_STATUS_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return

    try:
        async with _get_builder_lock(user_id):
            _PACK_BUILDER_TASKS.pop(user_id, None)
            cur_state = await state.get_state()
            if cur_state != PackStates.collecting_items.state:
                _PACK_BUILDER_BUFFER.pop(user_id, None)
                return

            await _drain_pack_builder_buffer_locked(
                user_id, state, bot, db, config, bot_username=bot_username
            )

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
    finally:
        if (
            user_id not in _PACK_BUILDER_TASKS
            and not _PACK_BUILDER_BUFFER.get(user_id)
        ):
            _PACK_BUILDER_LOCKS.pop(user_id, None)
