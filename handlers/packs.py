import asyncio
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database import Database
from utils.formatters import (
    MEDIA_ICONS,
    back_btn,
    breadcrumb,
    cancel_kb,
    copy_btn,
    esc,
    format_expiry_badge,
    format_max_views_badge,
    format_size,
    format_ttl_short,
    home_btn,
    safe_edit_or_send,
    schedule_auto_delete,
    send_stored_media,
    share_url_btn,
    tg_retry_call,
    toggle_label,
)
from utils.states import PackStates

router = Router(name="packs")

PACKS_PAGE_SIZE = 5
PACK_DELIVERY_BATCH_SIZE = 20
PACK_ITEMS_EDIT_PAGE_SIZE = 6

# Per-(chat_id, pack_id) lock to prevent duplicate overlapping deliveries on rapid clicks
_ACTIVE_DELIVERIES: set[tuple[int, int]] = set()


async def deliver_pack_to_chat(
    bot: Bot,
    chat_id: int,
    pack: dict[str, Any],
    db: Database,
    *,
    auto_delete_ttl: int = 0,
    force_protect: bool = False,
    start_offset: int = 0,
    count_view: bool = True,
) -> int:
    """
    Flood-safe, chunked delivery of Multi-File Packs (supports 100+ files cleanly!):
    - Prevents overlapping duplicate runs per `(chat_id, pack['id'])`.
    - Delivers up to `PACK_DELIVERY_BATCH_SIZE` (20) files per chunk so Telegram never
      triggers harsh 429 FloodWaits, and offers a 1-tap `📥 Send Next Batch` button!
    - Supports `protect_content` (No-Forward) and `auto_delete_ttl` cleanup.
    """
    delivery_key = (chat_id, int(pack["id"]))
    if delivery_key in _ACTIVE_DELIVERIES:
        return 0

    _ACTIVE_DELIVERIES.add(delivery_key)
    try:
        items = await db.get_pack_files(pack["id"])
        total_items = len(items)
        if total_items == 0:
            await tg_retry_call(bot.send_message, chat_id, "⚠️ <b>Empty Pack</b>")
            return 0

        start_offset = max(0, min(start_offset, total_items))
        slice_items = items[start_offset : start_offset + PACK_DELIVERY_BATCH_SIZE]
        if not slice_items:
            return 0

        if count_view and start_offset == 0:
            await db.increment_pack_views(pack["id"])

        protect = bool(pack.get("protect_content")) or force_protect
        end_idx = start_offset + len(slice_items)
        range_badge = (
            f"{start_offset + 1}–{end_idx} of {total_items}"
            if total_items > PACK_DELIVERY_BATCH_SIZE
            else f"{total_items} files"
        )
        desc_line = (
            f"\n<blockquote>{esc(pack['description'])}</blockquote>"
            if (start_offset == 0 and pack.get("description"))
            else ""
        )
        ttl_line = (
            f"\n<i>⏱ Auto-deletes in {format_ttl_short(auto_delete_ttl)}</i>"
            if auto_delete_ttl > 0
            else ""
        )

        sent_ids: list[int] = []
        header = await tg_retry_call(
            bot.send_message,
            chat_id=chat_id,
            text=(
                f"📦 <b>{esc(pack['title'])}</b> · <i>{range_badge} ({format_size(pack['total_size'])})</i>"
                f"{desc_line}{ttl_line}"
            ),
            protect_content=protect,
        )
        if getattr(header, "message_id", None):
            sent_ids.append(header.message_id)

        delivered = 0
        for idx, f in enumerate(slice_items):
            sent = await send_stored_media(
                bot,
                chat_id,
                f,
                protect_content=protect,
                auto_delete_ttl=0,  # grouped cleanup below
            )
            if sent:
                delivered += 1
                if getattr(sent, "message_id", None):
                    sent_ids.append(sent.message_id)
            # Gentle pacing for large batches to stay well below Telegram 20-msg/sec limits
            await asyncio.sleep(0.08 if (idx + 1) % 10 else 0.35)

        remaining = total_items - end_idx
        if remaining > 0:
            nxt_count = min(PACK_DELIVERY_BATCH_SIZE, remaining)
            cont_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"📥 Send Next {nxt_count} ({end_idx + 1}–{end_idx + nxt_count} of {total_items})",
                            callback_data=f"pk:more:{pack['share_token']}:{end_idx}",
                            style="primary",
                        )
                    ]
                ]
            )
            footer = await tg_retry_call(
                bot.send_message,
                chat_id=chat_id,
                text=(
                    f"✅ <b>Delivered {start_offset + 1}–{end_idx}</b> of <b>{total_items}</b> · "
                    f"<i>{remaining} remaining</i>"
                ),
                reply_markup=cont_kb,
                protect_content=protect,
            )
            if getattr(footer, "message_id", None):
                sent_ids.append(footer.message_id)

        if auto_delete_ttl > 0 and sent_ids:
            schedule_auto_delete(bot, chat_id, sent_ids, auto_delete_ttl)

        return delivered
    finally:
        _ACTIVE_DELIVERIES.discard(delivery_key)


@router.callback_query(F.data.startswith("pk:more:"))
async def cb_pack_deliver_more(
    callback: CallbackQuery, db: Database, bot: Bot
) -> None:
    """Delivers the next paginated batch of files for a large Pack (e.g. 50–100+ files)."""
    if not callback.from_user or not callback.message:
        return
    parts = callback.data.split(":")
    if len(parts) < 4 or not parts[3].isdigit():
        await callback.answer("Invalid batch cursor.", show_alert=True)
        return
    token = parts[2]
    offset = int(parts[3])

    # Allow continuation even if max_views reached after initial unlock (check raw token, block only if revoked/expired/paused)
    pack = await db.get_pack_by_token(token, include_unavailable=True)
    if not pack or pack["item_count"] == 0:
        await callback.answer("⚠️ Pack no longer available.", show_alert=True)
        return
    if pack.get("is_paused"):
        await callback.answer("⏸ This pack link was paused by the owner.", show_alert=True)
        return
    import time as _time

    if pack.get("expires_at") and int(pack["expires_at"]) < int(_time.time()):
        await callback.answer("⏳ This pack link has expired.", show_alert=True)
        return

    creator = await db.get_user(pack["creator_user_id"])
    visitor = await db.get_user(callback.from_user.id)
    ttl = int(
        (creator or {}).get("auto_delete_ttl")
        or (visitor or {}).get("auto_delete_ttl")
        or 0
    )

    await callback.answer("📥 Sending next batch…")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await deliver_pack_to_chat(
        bot,
        callback.message.chat.id,
        pack,
        db,
        auto_delete_ttl=ttl,
        start_offset=offset,
        count_view=False,
    )


async def render_packs_list(
    user_id: int, db: Database, page: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    """Crisp Multi-File Packs Hub."""
    user = await db.get_user(user_id)
    total = await db.count_vault_packs(user["vault_id"])
    offset = max(0, page) * PACKS_PAGE_SIZE
    rows = await db.list_vault_packs(
        user["vault_id"], limit=PACKS_PAGE_SIZE + 1, offset=offset
    )
    has_next = len(rows) > PACKS_PAGE_SIZE
    packs = rows[:PACKS_PAGE_SIZE]
    total_pages = max(1, (total + PACKS_PAGE_SIZE - 1) // PACKS_PAGE_SIZE)

    lines = [
        breadcrumb("Vault", "Packs"),
        f"🎒 <b>File Packs</b> · <i>{total} total</i>",
    ]
    buttons: list[list[InlineKeyboardButton]] = []

    if not packs:
        lines.append(
            "<blockquote>Drop 2+ files together to auto-pack, or tap <b>➕ New Pack</b> to bundle with PIN &amp; limits.</blockquote>"
        )
    else:
        for p in packs:
            badges = ""
            if p.get("is_paused"):
                badges += "⏸ "
            if p.get("pin_hash"):
                badges += "🔒 "
            lbl = f"{badges}🎒 {p['title'][:22]} ({p['item_count']}) · {format_size(p['total_size'])}"
            buttons.append(
                [InlineKeyboardButton(text=lbl, callback_data=f"pk:open:{p['id']}")]
            )

    if page > 0 or has_next:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀", callback_data=f"pk:list:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
        if has_next:
            nav.append(InlineKeyboardButton(text="▶", callback_data=f"pk:list:{page + 1}"))
        buttons.append(nav)

    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ New Pack",
                callback_data="pk:new",
                style="primary",
            ),
            back_btn("nav:home", "◀ Vault"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_pack_detail(
    user_id: int,
    pack_id: int,
    db: Database,
    bot: Optional[Bot] = None,
    bot_username: str = "KoshBox_Bot",
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    """
    Advanced Bug-Proof Pack Detail & Share Link Console:
    - 1-tap Copy Link + Native Share Sheet
    - Get All, Edit Title, Add/Remove Files
    - PIN Lock, No-Forward, Expiry Timer, Max Views Limit, Pause/Resume, Reset Link, Delete
    """
    user = await db.get_user(user_id)
    pack = await db.get_pack(pack_id)
    if not pack or pack["vault_id"] != user["vault_id"]:
        return None

    items = await db.get_pack_files(pack_id)
    uname = bot_username or "KoshBox_Bot"
    share_url = f"https://t.me/{uname}?start=pack_{pack['share_token']}"

    max_v = int(pack.get("max_views") or 0)
    cur_v = int(pack.get("views") or 0)
    sec_parts = [format_max_views_badge(cur_v, max_v)]
    if pack.get("is_paused"):
        sec_parts.append("⏸ Paused")
    if pack.get("pin_hash"):
        sec_parts.append("🔑 PIN")
    if pack.get("protect_content"):
        sec_parts.append("🛡️ No-Fwd")
    if pack.get("expires_at"):
        sec_parts.append(f"⏳ {format_expiry_badge(pack['expires_at'])}")

    preview_lines: list[str] = []
    if pack.get("description"):
        preview_lines.append(f"💬 <i>{esc(pack['description'][:80])}</i>")
    for f in items[:5]:
        ic = MEDIA_ICONS.get(f["media_type"], "📦")
        preview_lines.append(f"• {ic} {esc(f['title'][:32])}")
    if len(items) > 5:
        preview_lines.append(f"<i>…+{len(items) - 5} more</i>")
    if not preview_lines:
        preview_lines.append("<i>No active files</i>")

    text = (
        f"{breadcrumb('Vault', 'Packs', esc(pack['title'][:18]))}\n"
        f"🎒 <b>{esc(pack['title'])}</b>\n"
        f"<i>{pack['item_count']} files · {format_size(pack['total_size'])} · {' · '.join(sec_parts)}</i>\n"
        f"<blockquote>" + "\n".join(preview_lines) + "</blockquote>\n"
        f"🔗 <code>{share_url}</code>"
    )

    pin_lbl = "🔑 PIN: ON" if pack.get("pin_hash") else "🔑 Set PIN"
    prot_lbl = toggle_label(bool(pack.get("protect_content")), "No-Fwd")
    exp_lbl = f"⏳ {format_expiry_badge(pack.get('expires_at'))}"
    lim_lbl = f"👁 Limit: {cur_v}/{max_v}" if max_v > 0 else "👁 Limit: ∞"
    pause_lbl = "▶️ Resume" if pack.get("is_paused") else "⏸ Pause"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                copy_btn("📋 Copy Link", share_url, style="primary"),
                share_url_btn("↗️ Share", share_url, pack["title"]),
            ],
            [
                InlineKeyboardButton(text="📥 Get All", callback_data=f"pk:get:{pack_id}"),
                InlineKeyboardButton(text="🛠️ Edit Pack", callback_data=f"pk:edit:{pack_id}"),
            ],
            [
                InlineKeyboardButton(text=pin_lbl, callback_data=f"pk:pin:{pack_id}"),
                InlineKeyboardButton(text=prot_lbl, callback_data=f"pk:prot:{pack_id}"),
            ],
            [
                InlineKeyboardButton(text=exp_lbl, callback_data=f"pk:exp:{pack_id}"),
                InlineKeyboardButton(text=lim_lbl, callback_data=f"pk:maxv:{pack_id}"),
            ],
            [
                InlineKeyboardButton(text=pause_lbl, callback_data=f"pk:pause:{pack_id}"),
                InlineKeyboardButton(text="🔄 Reset", callback_data=f"pk:regen:{pack_id}"),
                InlineKeyboardButton(
                    text="🗑️ Delete",
                    callback_data=f"pk:del_ask:{pack_id}",
                    style="danger",
                ),
            ],
            [back_btn("pk:list:0", "◀ Packs"), home_btn()],
        ]
    )
    return text, kb


async def render_pack_edit_drawer(
    user_id: int, pack_id: int, db: Database, page: int = 0
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    """Manage Pack Title, Note/Description, Add More Files, or Remove Individual Files."""
    user = await db.get_user(user_id)
    pack = await db.get_pack(pack_id)
    if not pack or pack["vault_id"] != user["vault_id"]:
        return None

    items = await db.get_pack_files(pack_id)
    total = len(items)
    total_pages = max(1, (total + PACK_ITEMS_EDIT_PAGE_SIZE - 1) // PACK_ITEMS_EDIT_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PACK_ITEMS_EDIT_PAGE_SIZE
    slice_items = items[start : start + PACK_ITEMS_EDIT_PAGE_SIZE]

    desc_txt = esc(pack["description"][:70]) if pack.get("description") else "None"
    text = (
        f"{breadcrumb('Vault', 'Packs', esc(pack['title'][:16]), 'Edit')}\n"
        f"🛠️ <b>Edit Pack: {esc(pack['title'])}</b>\n"
        f"<blockquote>📦 <b>{total}</b> files · 💬 Note: <i>{desc_txt}</i>\n"
        f"Tap ✖️ on any file below to remove it from this pack.</blockquote>"
    )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="➕ Add Files",
                callback_data=f"pk:add:{pack_id}",
                style="primary",
            ),
            InlineKeyboardButton(text="✏️ Rename", callback_data=f"pk:ren:{pack_id}"),
            InlineKeyboardButton(text="💬 Note", callback_data=f"pk:desc:{pack_id}"),
        ]
    ]

    for f in slice_items:
        ic = MEDIA_ICONS.get(f["media_type"], "📦")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✖️ {ic} {f['title'][:28]}",
                    callback_data=f"pk:rmf:{pack_id}:{f['id']}:{page}",
                )
            ]
        )

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    text="◀", callback_data=f"pk:editp:{pack_id}:{page - 1}"
                )
            )
        nav.append(
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page + 1 < total_pages:
            nav.append(
                InlineKeyboardButton(
                    text="▶", callback_data=f"pk:editp:{pack_id}:{page + 1}"
                )
            )
        rows.append(nav)

    rows.append([back_btn(f"pk:open:{pack_id}", "◀ Back to Pack")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _render_batch_collector(
    count: int, append_pack_id: Optional[int] = None
) -> tuple[str, InlineKeyboardMarkup]:
    mode_lbl = "Add to Pack" if append_pack_id else "Batch Pack Builder"
    cancel_target = f"pk:open:{append_pack_id}" if append_pack_id else "pk:list:0"
    text = (
        f"{breadcrumb('Vault', 'Packs', 'Builder')}\n"
        f"🎒 <b>{mode_lbl}</b> · <b>{count}</b> queued\n"
        f"<blockquote>Drop or forward 1 to 100+ files right now — this card updates live without spamming your chat!</blockquote>"
    )
    rows: list[list[InlineKeyboardButton]] = []
    if count > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✓ Finish ({count})",
                    callback_data="pk:finish",
                    style="success",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="✕ Cancel", callback_data=f"fsm:cancel:{cancel_target}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("packs"))
async def cmd_packs(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    text, kb = await render_packs_list(message.from_user.id, db, 0)
    await message.answer(text, reply_markup=kb)


@router.message(Command("batch"))
async def cmd_batch(message: Message, state: FSMContext, db: Database) -> None:
    if not message.from_user:
        return
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    await state.set_state(PackStates.collecting_items)
    text, kb = _render_batch_collector(0)
    sent = await message.answer(text, reply_markup=kb)
    await state.update_data(
        pack_file_ids=[],
        pack_total_size=0,
        pack_recent_titles=[],
        pack_status_msg_id=getattr(sent, "message_id", None),
        append_pack_id=None,
    )


@router.callback_query(F.data.startswith("pk:list:"))
async def cb_packs_list(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    page = int(callback.data.split(":")[2])
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await render_packs_list(callback.from_user.id, db, page)
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data == "nav:packs")
async def cb_nav_packs(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await render_packs_list(callback.from_user.id, db, 0)
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data == "pk:new")
async def cb_pack_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PackStates.collecting_items)
    text, kb = _render_batch_collector(0)
    msg_id = (
        callback.message.message_id
        if (callback.message and not getattr(callback.message, "photo", None))
        else None
    )
    await state.update_data(
        pack_file_ids=[],
        pack_total_size=0,
        pack_recent_titles=[],
        pack_status_msg_id=msg_id,
        append_pack_id=None,
    )
    await safe_edit_or_send(
        callback, text, kb, answer_text="Drop files to bundle!", also_ack=True
    )


@router.callback_query(F.data.startswith("pk:add:"))
async def cb_pack_add_more_files(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    """Allows appending more files into an existing Pack using the live zero-spam collector."""
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return

    await state.set_state(PackStates.collecting_items)
    text, kb = _render_batch_collector(0, append_pack_id=pid)
    msg_id = (
        callback.message.message_id
        if (callback.message and not getattr(callback.message, "photo", None))
        else None
    )
    await state.update_data(
        pack_file_ids=[],
        pack_total_size=0,
        pack_recent_titles=[],
        pack_status_msg_id=msg_id,
        append_pack_id=pid,
    )
    await safe_edit_or_send(
        callback,
        text,
        kb,
        answer_text="Drop files to add to this pack!",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("pk:from_grp:"))
async def cb_pack_from_group(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    """1-Tap: Bundle an entire Folder/Group into a shareable Pack link!"""
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    grp = await db.get_group(gid)
    if not grp or not await db.is_vault_in_group(user["vault_id"], gid):
        await callback.answer("Folder not found.", show_alert=True)
        return
    files = await db.search_files(
        user["vault_id"], group_id_filter=gid, sort_by="new", limit=500, offset=0
    )
    if not files:
        await callback.answer("Folder has no files to pack.", show_alert=True)
        return
    pack = await db.create_pack(
        vault_id=user["vault_id"],
        creator_user_id=user["user_id"],
        title=grp["name"],
        file_ids=[f["id"] for f in files],
        protect_content=bool(user.get("protect_content")),
    )
    rendered = await render_pack_detail(
        callback.from_user.id, pack["id"], db, bot, bot_username=bot_username
    )
    if rendered:
        await safe_edit_or_send(
            callback,
            rendered[0],
            rendered[1],
            answer_text=f"🎒 Packed {len(files)} files!",
            also_ack=True,
        )
    else:
        await callback.answer(f"🎒 Packed {len(files)} files!")


@router.callback_query(F.data.startswith("pk:quick_album:"))
async def cb_pack_quick_album(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    """Legacy 1-Tap Pack creation callback handler kept for backwards compatibility."""
    if not callback.from_user:
        return
    raw_ids = callback.data.split(":", 2)[2]
    fids = [int(x) for x in raw_ids.split(",") if x.isdigit()]
    if not fids:
        await callback.answer("No files found.", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    first = await db.get_file(fids[0])
    title = (
        (first["title"][:28] + f" (+{len(fids)-1})")
        if (first and len(fids) > 1)
        else "Album Pack"
    )
    pack = await db.create_pack(
        vault_id=user["vault_id"],
        creator_user_id=user["user_id"],
        title=title,
        file_ids=fids,
        protect_content=bool(user.get("protect_content")),
    )
    rendered = await render_pack_detail(
        callback.from_user.id, pack["id"], db, bot, bot_username=bot_username
    )
    if rendered:
        await safe_edit_or_send(
            callback,
            rendered[0],
            rendered[1],
            answer_text="🎒 Pack Link Ready!",
            also_ack=True,
        )
    else:
        await callback.answer("🎒 Pack Link Ready!")


@router.message(PackStates.collecting_items, Command("done"))
async def cmd_batch_done(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user:
        return
    data = await state.get_data()
    fids: list[int] = data.get("pack_file_ids") or []
    append_pid: Optional[int] = data.get("append_pack_id")
    if not fids:
        await message.answer("⚠️ Drop at least 1 file first, or /cancel.")
        return

    if append_pid:
        await state.clear()
        await db.append_files_to_pack(append_pid, fids)
        rendered = await render_pack_detail(
            message.from_user.id, append_pid, db, bot, bot_username=bot_username
        )
        if rendered:
            await message.answer(
                f"✅ <b>Added {len(fids)} file(s)</b>\n{rendered[0]}",
                reply_markup=rendered[1],
                disable_web_page_preview=True,
            )
        return

    await state.set_state(PackStates.waiting_for_title)
    await message.answer(
        f"✏️ <b>Name this Pack</b> <i>({len(fids)} files)</i>\n"
        f"<blockquote>Send a short title, or tap <b>⚡ Auto Title</b>:</blockquote>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚡ Auto Title",
                        callback_data="pk:autotitle",
                        style="primary",
                    )
                ],
                [InlineKeyboardButton(text="◀ Cancel", callback_data="fsm:cancel:pk:list:0")],
            ]
        ),
    )


@router.callback_query(F.data == "pk:finish")
async def cb_batch_finish(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    data = await state.get_data()
    fids: list[int] = data.get("pack_file_ids") or []
    append_pid: Optional[int] = data.get("append_pack_id")
    if not fids:
        await callback.answer("Drop at least 1 file first!", show_alert=True)
        return

    if append_pid:
        await state.clear()
        await db.append_files_to_pack(append_pid, fids)
        rendered = await render_pack_detail(
            callback.from_user.id, append_pid, db, bot, bot_username=bot_username
        )
        if rendered:
            await safe_edit_or_send(
                callback,
                rendered[0],
                rendered[1],
                answer_text=f"✓ Added {len(fids)} file(s)!",
                also_ack=True,
            )
        else:
            await callback.answer(f"✓ Added {len(fids)} file(s)!")
        return

    await state.set_state(PackStates.waiting_for_title)
    await safe_edit_or_send(
        callback,
        f"✏️ <b>Name this Pack</b> <i>({len(fids)} files)</i>\n"
        f"<blockquote>Send a short title, or tap <b>⚡ Auto Title</b>:</blockquote>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚡ Auto Title",
                        callback_data="pk:autotitle",
                        style="primary",
                    )
                ],
                [InlineKeyboardButton(text="◀ Cancel", callback_data="fsm:cancel:pk:list:0")],
            ]
        ),
        also_ack=True,
    )


@router.callback_query(F.data == "pk:autotitle")
async def cb_pack_auto_title(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    data = await state.get_data()
    fids: list[int] = data.get("pack_file_ids") or []
    await state.clear()
    if not fids:
        await callback.answer("No files queued.", show_alert=True)
        return
    user = await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    first = await db.get_file(fids[0])
    title = (
        f"{first['title'][:32]} ({len(fids)})"
        if first
        else f"Pack ({len(fids)} files)"
    )
    pack = await db.create_pack(
        vault_id=user["vault_id"],
        creator_user_id=user["user_id"],
        title=title,
        file_ids=fids,
        protect_content=bool(user.get("protect_content")),
    )
    rendered = await render_pack_detail(
        callback.from_user.id, pack["id"], db, bot, bot_username=bot_username
    )
    if rendered:
        await safe_edit_or_send(
            callback,
            rendered[0],
            rendered[1],
            answer_text="🎒 Pack Ready!",
            also_ack=True,
        )
    else:
        await callback.answer("🎒 Pack Ready!")


@router.message(PackStates.waiting_for_title)
async def fsm_pack_save_title(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
        return
    data = await state.get_data()
    fids: list[int] = data.get("pack_file_ids") or []
    await state.clear()
    if not fids:
        return
    raw = message.text.strip()
    title = f"Pack ({len(fids)} files)" if raw == "-" else raw[:60]
    user = await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    pack = await db.create_pack(
        vault_id=user["vault_id"],
        creator_user_id=user["user_id"],
        title=title,
        file_ids=fids,
        protect_content=bool(user.get("protect_content")),
    )
    rendered = await render_pack_detail(
        message.from_user.id, pack["id"], db, bot, bot_username=bot_username
    )
    if rendered:
        await message.answer(
            rendered[0], reply_markup=rendered[1], disable_web_page_preview=True
        )


@router.callback_query(F.data.startswith("pk:open:"))
async def cb_pack_open(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
    )
    if not rendered:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1], also_ack=True)


@router.callback_query(F.data.startswith("pk:edit:"))
async def cb_pack_edit_drawer(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    rendered = await render_pack_edit_drawer(callback.from_user.id, pid, db, page=0)
    if not rendered:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1], also_ack=True)


@router.callback_query(F.data.startswith("pk:editp:"))
async def cb_pack_edit_page(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    _, _, pid_s, page_s = callback.data.split(":")
    rendered = await render_pack_edit_drawer(
        callback.from_user.id, int(pid_s), db, page=int(page_s)
    )
    if not rendered:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1], also_ack=True)


@router.callback_query(F.data.startswith("pk:rmf:"))
async def cb_pack_remove_file(
    callback: CallbackQuery, db: Database, bot: Bot
) -> None:
    if not callback.from_user:
        return
    _, _, pid_s, fid_s, page_s = callback.data.split(":")
    pid, fid, page = int(pid_s), int(fid_s), int(page_s)
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return

    items = await db.get_pack_files(pid)
    if len(items) <= 1:
        await callback.answer(
            "Pack must keep at least 1 file (or delete the pack).", show_alert=True
        )
        return

    await db.remove_file_from_pack(pid, fid)
    rendered = await render_pack_edit_drawer(callback.from_user.id, pid, db, page=page)
    if rendered:
        await safe_edit_or_send(
            callback,
            rendered[0],
            rendered[1],
            answer_text="Removed from pack",
            also_ack=True,
        )
    else:
        await callback.answer("Removed from pack")


@router.callback_query(F.data.startswith("pk:ren:"))
async def cb_pack_rename_prompt(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await state.set_state(PackStates.waiting_for_rename)
    await state.update_data(rename_pack_id=pid)
    await safe_edit_or_send(
        callback,
        f"✏️ <b>Rename Pack</b> · <code>{esc(pack['title'])}</code>\n"
        f"<blockquote>Send new pack title (max 60 chars):</blockquote>",
        cancel_kb(f"pk:edit:{pid}"),
        also_ack=True,
    )


@router.message(PackStates.waiting_for_rename)
async def fsm_pack_rename_save(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
        return
    data = await state.get_data()
    pid = data.get("rename_pack_id")
    await state.clear()
    if not pid:
        return
    user = await db.get_user(message.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or not user or pack["vault_id"] != user["vault_id"]:
        return
    await db.rename_pack(pid, message.text.strip()[:60])
    rendered = await render_pack_detail(
        message.from_user.id, pid, db, bot, bot_username=bot_username
    )
    if rendered:
        await message.answer(
            rendered[0], reply_markup=rendered[1], disable_web_page_preview=True
        )


@router.callback_query(F.data.startswith("pk:desc:"))
async def cb_pack_desc_prompt(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await state.set_state(PackStates.waiting_for_description)
    await state.update_data(desc_pack_id=pid)
    cur_note = esc(pack["description"]) if pack.get("description") else "None"
    await safe_edit_or_send(
        callback,
        f"💬 <b>Pack Welcome Note</b> · <i>{cur_note}</i>\n"
        f"<blockquote>Send a note shown when someone opens your link (or <code>-</code> to clear):</blockquote>",
        cancel_kb(f"pk:edit:{pid}"),
        also_ack=True,
    )


@router.message(PackStates.waiting_for_description)
async def fsm_pack_desc_save(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
        return
    data = await state.get_data()
    pid = data.get("desc_pack_id")
    await state.clear()
    if not pid:
        return
    user = await db.get_user(message.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or not user or pack["vault_id"] != user["vault_id"]:
        return
    raw = message.text.strip()
    note = "" if raw == "-" else raw[:300]
    await db.set_pack_description(pid, note)
    rendered = await render_pack_detail(
        message.from_user.id, pid, db, bot, bot_username=bot_username
    )
    if rendered:
        await message.answer(
            rendered[0], reply_markup=rendered[1], disable_web_page_preview=True
        )


@router.callback_query(F.data.startswith("pk:get:"))
async def cb_pack_get_all(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user or not callback.message:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await callback.answer("📥 Sending pack…")
    await deliver_pack_to_chat(
        bot,
        callback.message.chat.id,
        pack,
        db,
        auto_delete_ttl=int(user.get("auto_delete_ttl") or 0),
        count_view=False,  # Owner testing their own pack doesn't burn max_views quota!
    )


@router.callback_query(F.data.startswith("pk:pin:"))
async def cb_pack_pin_prompt(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await state.set_state(PackStates.waiting_for_pin)
    await state.update_data(pin_pack_id=pid)
    status = "Active" if pack.get("pin_hash") else "Off"
    await safe_edit_or_send(
        callback,
        f"🔑 <b>Pack PIN Lock</b> · <i>{status}</i>\n"
        f"<blockquote>Send a PIN/password (up to 24 chars), or <code>-</code> to remove PIN:</blockquote>",
        cancel_kb(f"pk:open:{pid}"),
        also_ack=True,
    )


@router.message(PackStates.waiting_for_pin)
async def fsm_pack_set_pin(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
        return
    data = await state.get_data()
    pid = data.get("pin_pack_id")
    await state.clear()
    if not pid:
        return
    user = await db.get_user(message.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or not user or pack["vault_id"] != user["vault_id"]:
        return
    raw = message.text.strip()
    pin = None if raw == "-" else raw[:24]
    await db.set_pack_pin(pid, pin)
    try:
        await message.delete()
    except Exception:
        pass
    rendered = await render_pack_detail(
        message.from_user.id, pid, db, bot, bot_username=bot_username
    )
    if rendered:
        await message.answer(
            rendered[0], reply_markup=rendered[1], disable_web_page_preview=True
        )


@router.callback_query(F.data.startswith("pk:prot:"))
async def cb_pack_toggle_prot(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    on = await db.toggle_pack_protect(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
    )
    ans = "🛡️ Content Protection ON" if on else "Content Protection OFF"
    if rendered:
        await safe_edit_or_send(
            callback, rendered[0], rendered[1], answer_text=ans, also_ack=True
        )
    else:
        await callback.answer(ans)


@router.callback_query(F.data.startswith("pk:exp:"))
async def cb_pack_cycle_exp(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    new_exp = await db.cycle_pack_expiry(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
    )
    ans = f"⏳ Expiry: {format_expiry_badge(new_exp)}"
    if rendered:
        await safe_edit_or_send(
            callback, rendered[0], rendered[1], answer_text=ans, also_ack=True
        )
    else:
        await callback.answer(ans)


@router.callback_query(F.data.startswith("pk:maxv:"))
async def cb_pack_cycle_max_views(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    lim = await db.cycle_pack_max_views(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
    )
    lbl = f"{lim} opens max" if lim > 0 else "Unlimited opens"
    ans = f"👁 Access Limit: {lbl}"
    if rendered:
        await safe_edit_or_send(
            callback, rendered[0], rendered[1], answer_text=ans, also_ack=True
        )
    else:
        await callback.answer(ans)


@router.callback_query(F.data.startswith("pk:pause:"))
async def cb_pack_toggle_pause(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    paused = await db.toggle_pack_paused(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
    )
    ans = "⏸ Link Paused" if paused else "▶️ Link Active"
    if rendered:
        await safe_edit_or_send(
            callback, rendered[0], rendered[1], answer_text=ans, also_ack=True
        )
    else:
        await callback.answer(ans)


@router.callback_query(F.data.startswith("pk:regen:"))
async def cb_pack_regen(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await db.regenerate_pack_token(pid)
    rendered = await render_pack_detail(
        callback.from_user.id, pid, db, bot, bot_username=bot_username
    )
    ans = "🔄 New pack link generated (views reset)"
    if rendered:
        await safe_edit_or_send(
            callback, rendered[0], rendered[1], answer_text=ans, also_ack=True
        )
    else:
        await callback.answer(ans)


@router.callback_query(F.data.startswith("pk:del_ask:"))
async def cb_pack_del_ask(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                back_btn(f"pk:open:{pid}", "◀ Cancel"),
                InlineKeyboardButton(
                    text="💥 Delete Pack",
                    callback_data=f"pk:del:{pid}",
                    style="danger",
                ),
            ]
        ]
    )
    await safe_edit_or_send(
        callback,
        f"⚠️ <b>Delete Pack '{esc(pack['title'])}'?</b>\n"
        f"<blockquote>The share link stops working. Your underlying vault files stay untouched.</blockquote>",
        kb,
        also_ack=True,
    )


@router.callback_query(F.data.startswith("pk:del:"))
async def cb_pack_del(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    await db.delete_pack(pid, user["vault_id"])
    text, kb = await render_packs_list(callback.from_user.id, db, 0)
    await safe_edit_or_send(
        callback, text, kb, answer_text="Pack deleted", also_ack=True
    )
