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
    format_size,
    format_ttl_short,
    home_btn,
    safe_edit_or_send,
    schedule_auto_delete,
    send_stored_media,
    share_url_btn,
    toggle_label,
)
from utils.states import PackStates

router = Router(name="packs")

PACKS_PAGE_SIZE = 5


async def deliver_pack_to_chat(
    bot: Bot,
    chat_id: int,
    pack: dict[str, Any],
    db: Database,
    *,
    auto_delete_ttl: int = 0,
    force_protect: bool = False,
) -> int:
    """Delivers all files inside a pack cleanly with optional protect_content & auto-delete."""
    items = await db.get_pack_files(pack["id"])
    if not items:
        await bot.send_message(chat_id, "⚠️ <b>Empty Pack</b>")
        return 0

    await db.increment_pack_views(pack["id"])
    protect = bool(pack.get("protect_content")) or force_protect

    sent_ids: list[int] = []
    header = await bot.send_message(
        chat_id=chat_id,
        text=(
            f"📦 <b>{esc(pack['title'])}</b> · <i>{len(items)} files ({format_size(pack['total_size'])})</i>"
            + (f"\n<i>⏱ Auto-deletes in {format_ttl_short(auto_delete_ttl)}</i>" if auto_delete_ttl > 0 else "")
        ),
        protect_content=protect,
    )
    if getattr(header, "message_id", None):
        sent_ids.append(header.message_id)

    delivered = 0
    for f in items:
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
        await asyncio.sleep(0.06)

    if auto_delete_ttl > 0 and sent_ids:
        schedule_auto_delete(bot, chat_id, sent_ids, auto_delete_ttl)

    return delivered


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
            "<blockquote>Bundle multiple files into <b>one shareable link</b> — with optional PIN & expiry.</blockquote>"
        )
    else:
        for p in packs:
            lock = "🔒 " if p.get("pin_hash") else ""
            lbl = f"{lock}🎒 {p['title'][:22]} ({p['item_count']}) · {format_size(p['total_size'])}"
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
    user_id: int, pack_id: int, db: Database, bot: Bot
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    """Single Pack detail card with 1-tap Copy Link, Share, PIN, Protect, Expiry, and Delete."""
    user = await db.get_user(user_id)
    pack = await db.get_pack(pack_id)
    if not pack or pack["vault_id"] != user["vault_id"]:
        return None

    items = await db.get_pack_files(pack_id)
    me = await bot.get_me()
    share_url = f"https://t.me/{me.username}?start=pack_{pack['share_token']}"

    sec_parts = [f"👁 {pack['views']}×"]
    if pack.get("pin_hash"):
        sec_parts.append("🔑 PIN")
    if pack.get("protect_content"):
        sec_parts.append("🛡️ No-Fwd")
    if pack.get("expires_at"):
        sec_parts.append(f"⏳ {format_expiry_badge(pack['expires_at'])}")

    preview_lines: list[str] = []
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

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                copy_btn("📋 Copy Link", share_url, style="primary"),
                share_url_btn("↗️ Share", share_url, pack["title"]),
            ],
            [
                InlineKeyboardButton(text="📥 Get All", callback_data=f"pk:get:{pack_id}"),
                InlineKeyboardButton(text=pin_lbl, callback_data=f"pk:pin:{pack_id}"),
            ],
            [
                InlineKeyboardButton(text=prot_lbl, callback_data=f"pk:prot:{pack_id}"),
                InlineKeyboardButton(text=exp_lbl, callback_data=f"pk:exp:{pack_id}"),
            ],
            [
                InlineKeyboardButton(text="🔄 Reset Link", callback_data=f"pk:regen:{pack_id}"),
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


def _render_batch_collector(count: int) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"{breadcrumb('Vault', 'Packs', 'Builder')}\n"
        f"🎒 <b>Batch Pack Builder</b> · <b>{count}</b> queued\n"
        f"<blockquote>Drop or forward multiple files right now, then tap <b>✓ Finish Pack</b>.</blockquote>"
    )
    rows: list[list[InlineKeyboardButton]] = []
    if count > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✓ Finish Pack ({count})",
                    callback_data="pk:finish",
                    style="success",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="✕ Cancel", callback_data="fsm:cancel:pk:list:0")])
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
    await state.update_data(pack_file_ids=[])
    text, kb = _render_batch_collector(0)
    await message.answer(text, reply_markup=kb)


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
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


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
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "pk:new")
async def cb_pack_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PackStates.collecting_items)
    await state.update_data(pack_file_ids=[])
    text, kb = _render_batch_collector(0)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer("Drop files to bundle!")


@router.callback_query(F.data.startswith("pk:from_grp:"))
async def cb_pack_from_group(
    callback: CallbackQuery, db: Database, bot: Bot
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
        user["vault_id"], group_id_filter=gid, sort_by="new", limit=100, offset=0
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
    rendered = await render_pack_detail(callback.from_user.id, pack["id"], db, bot)
    if rendered:
        await safe_edit_or_send(callback, rendered[0], rendered[1])
    await callback.answer(f"🎒 Packed {len(files)} files!")


@router.callback_query(F.data.startswith("pk:quick_album:"))
async def cb_pack_quick_album(
    callback: CallbackQuery, db: Database, bot: Bot
) -> None:
    """1-Tap Pack creation directly from an uploaded media album receipt."""
    if not callback.from_user:
        return
    raw_ids = callback.data.split(":", 2)[2]
    fids = [int(x) for x in raw_ids.split(",") if x.isdigit()]
    if not fids:
        await callback.answer("No files found.", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    first = await db.get_file(fids[0])
    title = (first["title"][:28] + f" (+{len(fids)-1})") if (first and len(fids) > 1) else "Album Pack"
    pack = await db.create_pack(
        vault_id=user["vault_id"],
        creator_user_id=user["user_id"],
        title=title,
        file_ids=fids,
        protect_content=bool(user.get("protect_content")),
    )
    rendered = await render_pack_detail(callback.from_user.id, pack["id"], db, bot)
    if rendered:
        await safe_edit_or_send(callback, rendered[0], rendered[1])
    await callback.answer("🎒 Album Pack Link Ready!")


@router.message(PackStates.collecting_items, Command("done"))
async def cmd_batch_done(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fids: list[int] = data.get("pack_file_ids") or []
    if not fids:
        await message.answer("⚠️ Drop at least 1 file first, or /cancel.")
        return
    await state.set_state(PackStates.waiting_for_title)
    await message.answer(
        f"✏️ <b>Name this Pack</b> <i>({len(fids)} files)</i>\n"
        f"<blockquote>Send a short title (or <code>-</code> for auto title):</blockquote>",
        reply_markup=cancel_kb("pk:list:0"),
    )


@router.callback_query(F.data == "pk:finish")
async def cb_batch_finish(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    fids: list[int] = data.get("pack_file_ids") or []
    if not fids:
        await callback.answer("Drop at least 1 file first!", show_alert=True)
        return
    await state.set_state(PackStates.waiting_for_title)
    await safe_edit_or_send(
        callback,
        f"✏️ <b>Name this Pack</b> <i>({len(fids)} files)</i>\n"
        f"<blockquote>Send a short title (or <code>-</code> for auto title):</blockquote>",
        cancel_kb("pk:list:0"),
    )
    await callback.answer()


@router.message(PackStates.waiting_for_title)
async def fsm_pack_save_title(
    message: Message, state: FSMContext, db: Database, bot: Bot
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
    rendered = await render_pack_detail(message.from_user.id, pack["id"], db, bot)
    if rendered:
        await message.answer(
            rendered[0], reply_markup=rendered[1], disable_web_page_preview=True
        )


@router.callback_query(F.data.startswith("pk:open:"))
async def cb_pack_open(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    rendered = await render_pack_detail(callback.from_user.id, pid, db, bot)
    if not rendered:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1])
    await callback.answer()


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
    )
    await callback.answer()


@router.message(PackStates.waiting_for_pin)
async def fsm_pack_set_pin(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not message.from_user or not message.text:
        return
    data = await state.get_data()
    pid = data.get("pin_pack_id")
    await state.clear()
    if not pid:
        return
    raw = message.text.strip()
    pin = None if raw == "-" else raw[:24]
    await db.set_pack_pin(pid, pin)
    try:
        await message.delete()
    except Exception:
        pass
    rendered = await render_pack_detail(message.from_user.id, pid, db, bot)
    if rendered:
        await message.answer(
            rendered[0], reply_markup=rendered[1], disable_web_page_preview=True
        )


@router.callback_query(F.data.startswith("pk:prot:"))
async def cb_pack_toggle_prot(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    on = await db.toggle_pack_protect(pid)
    rendered = await render_pack_detail(callback.from_user.id, pid, db, bot)
    if rendered:
        await safe_edit_or_send(callback, rendered[0], rendered[1])
    await callback.answer("🛡️ Content Protection ON" if on else "Content Protection OFF")


@router.callback_query(F.data.startswith("pk:exp:"))
async def cb_pack_cycle_exp(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    new_exp = await db.cycle_pack_expiry(pid)
    rendered = await render_pack_detail(callback.from_user.id, pid, db, bot)
    if rendered:
        await safe_edit_or_send(callback, rendered[0], rendered[1])
    await callback.answer(f"⏳ Expiry: {format_expiry_badge(new_exp)}")


@router.callback_query(F.data.startswith("pk:regen:"))
async def cb_pack_regen(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    pack = await db.get_pack(pid)
    if not pack or pack["vault_id"] != user["vault_id"]:
        await callback.answer("Pack not found.", show_alert=True)
        return
    await db.regenerate_pack_token(pid)
    rendered = await render_pack_detail(callback.from_user.id, pid, db, bot)
    if rendered:
        await safe_edit_or_send(callback, rendered[0], rendered[1])
    await callback.answer("🔄 New pack link generated")


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
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pk:del:"))
async def cb_pack_del(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    pid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    await db.delete_pack(pid, user["vault_id"])
    text, kb = await render_packs_list(callback.from_user.id, db, 0)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer("Pack deleted")
