from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database import Database
from utils.formatters import (
    DIVIDER,
    MEDIA_ICONS,
    MEDIA_LABELS,
    SORT_LABELS,
    build_file_card_text,
    build_file_keyboard,
    cancel_kb,
    esc,
    format_size,
    home_btn,
    next_sort_key,
    safe_edit_or_send,
    send_stored_media,
)
from utils.states import FileEditStates, SearchStates

router = Router(name="file_actions")

BROWSE_PAGE_SIZE = 7

FILTER_TO_MEDIA = {
    "audio": "audio",
    "video": "video",
    "doc": "document",
    "photo": "photo",
    "voice": "voice",
    "gif": "animation",
    "sticker": "sticker",
}


async def render_browse_page(
    user_id: int,
    flt: str,
    sort_key: str,
    gid: int,
    page: int,
    db: Database,
    text_query: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    user = await db.get_user(user_id)
    if sort_key == "def" or sort_key not in SORT_LABELS:
        sort_key = user.get("default_sort") or "new"

    only_fav = flt == "fav"
    media_type = FILTER_TO_MEDIA.get(flt)
    group_filter = gid if gid > 0 else None

    offset = max(0, page) * BROWSE_PAGE_SIZE
    rows = await db.search_files(
        user["vault_id"],
        text_query=text_query,
        media_type=media_type,
        group_id_filter=group_filter,
        only_favorites=only_fav,
        sort_by=sort_key,
        limit=BROWSE_PAGE_SIZE + 1,
        offset=offset,
    )
    has_next = len(rows) > BROWSE_PAGE_SIZE
    items = rows[:BROWSE_PAGE_SIZE]

    total_matching = (
        await db.count_files(
            user["vault_id"],
            media_type=media_type,
            group_id_filter=group_filter,
            only_favorites=only_fav,
        )
        if not text_query
        else len(items)
    )

    # Header context
    if group_filter:
        grp = await db.get_group(group_filter)
        folder_title = f"{'👥' if (grp and grp['is_shared']) else '📁'} {grp['name']}" if grp else "Folder"
        heading = f"<b>{esc(folder_title)}</b>"
    elif only_fav:
        heading = "★ <b>Favorite Files</b>"
    elif media_type:
        heading = f"{MEDIA_ICONS.get(media_type, '📦')} <b>{MEDIA_LABELS.get(media_type, 'Files')}</b>"
    elif text_query:
        heading = f"🔎 <b>Search:</b> <code>{esc(text_query[:30])}</code>"
    else:
        heading = "📦 <b>All Vault Files</b>"

    sort_name = SORT_LABELS.get(sort_key, "Newest")
    total_pages = max(1, (total_matching + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)

    lines = [
        heading,
        f"<code>{total_matching} item(s) · Sort: {sort_name} · Page {page + 1}/{total_pages}</code>",
        DIVIDER,
    ]

    buttons: list[list[InlineKeyboardButton]] = []

    if not items:
        lines.append(
            "<i>No matching files found here yet.</i>\n"
            "Drop any file into this chat or switch filters below."
        )
    else:
        lines.append("<i>Tap any item to preview, edit, share, or send:</i>")
        back_token = f"br:{flt}:{sort_key}:{gid}:{page}"
        for f in items:
            icon = MEDIA_ICONS.get(f["media_type"], "📦")
            star = "★ " if f.get("is_favorite") else ""
            sz = f" · {format_size(f['file_size'])}" if f.get("file_size") else ""
            label = f"{star}{icon} {f['title'][:32]}{sz}"
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"fo:{f['id']}:{flt}:{sort_key}:{gid}:{page}",
                    )
                ]
            )

    # Filter pill bar (compact, clean)
    if not text_query:
        def _pill(key: str, lbl: str) -> InlineKeyboardButton:
            mark = "● " if flt == key else ""
            return InlineKeyboardButton(
                text=f"{mark}{lbl}",
                callback_data=f"br:{key}:{sort_key}:{gid}:0",
            )

        buttons.append(
            [
                _pill("all", "All"),
                _pill("fav", "★ Fav"),
                _pill("audio", "🎵"),
                _pill("video", "🎬"),
                _pill("doc", "📄"),
                _pill("photo", "🖼️"),
            ]
        )

    # Pagination + Sort + Search row
    if page > 0 or has_next:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    text="‹ Prev",
                    callback_data=f"br:{flt}:{sort_key}:{gid}:{page - 1}",
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f"{page + 1} / {total_pages}",
                callback_data="noop",
            )
        )
        if has_next:
            nav_row.append(
                InlineKeyboardButton(
                    text="Next ›",
                    callback_data=f"br:{flt}:{sort_key}:{gid}:{page + 1}",
                )
            )
        buttons.append(nav_row)

    next_s = next_sort_key(sort_key)
    ctrl_row = [
        InlineKeyboardButton(
            text=f"↕️ Sort: {sort_name}",
            callback_data=f"br:{flt}:{next_s}:{gid}:0",
        ),
        InlineKeyboardButton(text="🔎 Search", callback_data="f:search_prompt"),
    ]
    buttons.append(ctrl_row)

    bottom_row: list[InlineKeyboardButton] = []
    if group_filter:
        bottom_row.append(
            InlineKeyboardButton(text="‹ Back to Folder", callback_data=f"grp:open:{group_filter}")
        )
    else:
        bottom_row.append(
            InlineKeyboardButton(
                text="⚡ Inline Vault",
                switch_inline_query_current_chat="",
            )
        )
    bottom_row.append(home_btn())
    buttons.append(bottom_row)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_trash_page(
    user_id: int, page: int, db: Database
) -> tuple[str, InlineKeyboardMarkup]:
    user = await db.get_user(user_id)
    total = await db.count_trashed_files(user["vault_id"])
    offset = max(0, page) * BROWSE_PAGE_SIZE
    items = await db.list_trashed_files(
        user["vault_id"], limit=BROWSE_PAGE_SIZE + 1, offset=offset
    )
    has_next = len(items) > BROWSE_PAGE_SIZE
    items = items[:BROWSE_PAGE_SIZE]

    lines = [
        f"🗑️ <b>Recycle Trash</b> ({total})",
        DIVIDER,
    ]
    buttons: list[list[InlineKeyboardButton]] = []

    if not items:
        lines.append("<i>Your trash is completely clean.</i>")
    else:
        lines.append("<i>Tap a file to restore it or delete it permanently:</i>")
        for f in items:
            icon = MEDIA_ICONS.get(f["media_type"], "📦")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{icon} {f['title'][:36]}",
                        callback_data=f"f:open:{f['id']}",
                    )
                ]
            )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="‹ Prev", callback_data=f"trash:{page - 1}"))
    if has_next:
        nav_row.append(InlineKeyboardButton(text="Next ›", callback_data=f"trash:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    if total > 0:
        buttons.append(
            [InlineKeyboardButton(text="🔥 Empty Entire Trash", callback_data="trash:empty_confirm")]
        )

    buttons.append(
        [
            InlineKeyboardButton(text="📦 My Files", callback_data="br:all:def:0:0"),
            home_btn(),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(Command("files"))
async def cmd_files(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    text, kb = await render_browse_page(message.from_user.id, "all", "def", 0, 0, db)
    await message.answer(text, reply_markup=kb)


@router.message(Command("search"))
async def cmd_search(
    message: Message, command: CommandObject, state: FSMContext, db: Database
) -> None:
    if not message.from_user:
        return
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    query = (command.args or "").strip()
    if query:
        text, kb = await render_browse_page(
            message.from_user.id, "all", "def", 0, 0, db, text_query=query
        )
        await message.answer(text, reply_markup=kb)
        return

    await state.set_state(SearchStates.waiting_for_query)
    await message.answer(
        f"🔎 <b>Search Your Vault</b>\n{DIVIDER}\n"
        f"Type any keyword, artist, filename, or <code>#tag</code> to find matching files:",
        reply_markup=cancel_kb("br:all:def:0:0"),
    )


@router.callback_query(F.data == "f:search_prompt")
async def cb_search_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchStates.waiting_for_query)
    await safe_edit_or_send(
        callback,
        f"🔎 <b>Search Your Vault</b>\n{DIVIDER}\n"
        f"Type any keyword, artist, filename, or <code>#tag</code> to find matching files:",
        cancel_kb("br:all:def:0:0"),
    )
    await callback.answer()


@router.message(SearchStates.waiting_for_query)
async def fsm_run_search(
    message: Message, state: FSMContext, db: Database
) -> None:
    if not message.from_user or not message.text:
        return
    q = message.text.strip()
    await state.clear()
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    text, kb = await render_browse_page(
        message.from_user.id, "all", "def", 0, 0, db, text_query=q
    )
    await message.answer(text, reply_markup=kb)


@router.message(Command("trash"))
async def cmd_trash(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    text, kb = await render_trash_page(message.from_user.id, 0, db)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "trash:empty_confirm")
async def cb_trash_empty_confirm(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    user = await db.get_user(callback.from_user.id)
    cnt = await db.count_trashed_files(user["vault_id"])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔥 Yes, Delete All Forever", callback_data="trash:empty_do"),
                InlineKeyboardButton(text="‹ Cancel", callback_data="trash:0"),
            ]
        ]
    )
    await safe_edit_or_send(
        callback,
        f"⚠️ <b>Empty Recycle Trash?</b>\n{DIVIDER}\n"
        f"This will permanently remove <b>{cnt}</b> file(s) from your vault index. This cannot be undone.",
        kb,
    )
    await callback.answer()


@router.callback_query(F.data == "trash:empty_do")
async def cb_trash_empty_do(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    user = await db.get_user(callback.from_user.id)
    removed = await db.empty_trash(user["vault_id"])
    text, kb = await render_trash_page(callback.from_user.id, 0, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer(f"Deleted {removed} file(s) forever.")


@router.callback_query(F.data.startswith("trash:"))
async def cb_trash_list(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    part = callback.data.split(":")[1]
    if not part.isdigit():
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await render_trash_page(callback.from_user.id, int(part), db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


# Legacy browse:0 compatibility + new br:flt:sort:gid:page
@router.callback_query(F.data.startswith("browse:"))
async def cb_legacy_browse(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    page = int(callback.data.split(":")[1])
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await render_browse_page(callback.from_user.id, "all", "def", 0, page, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("br:"))
async def cb_browse_modern(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    _, flt, sort_key, gid_s, page_s = callback.data.split(":")
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await render_browse_page(
        callback.from_user.id, flt, sort_key, int(gid_s), int(page_s), db
    )
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("fo:"))
async def cb_open_file_with_context(
    callback: CallbackQuery, db: Database, bot: Bot
) -> None:
    if not callback.from_user:
        return
    _, fid_s, flt, sort_key, gid_s, page_s = callback.data.split(":")
    fid = int(fid_s)
    user = await db.get_user(callback.from_user.id)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    me = await bot.get_me()
    back_cb = f"br:{flt}:{sort_key}:{gid_s}:{page_s}"
    await safe_edit_or_send(
        callback,
        build_file_card_text(f, me.username or ""),
        build_file_keyboard(f, back_cb=back_cb),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("f:open:"))
async def cb_open_file(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    me = await bot.get_me()
    await safe_edit_or_send(
        callback,
        build_file_card_text(f, me.username or ""),
        build_file_keyboard(f),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("f:get:"))
async def cb_get_file_here(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user or not callback.message:
        return
    fid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    ok = await send_stored_media(bot, callback.message.chat.id, f)
    if ok:
        await callback.answer("📥 Sent to chat!")
    else:
        await callback.answer("Unsupported media format.", show_alert=True)


@router.callback_query(F.data.startswith("f:fav:"))
async def cb_toggle_fav(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    is_fav = await db.toggle_favorite(fid)
    updated = await db.get_file(fid)
    me = await bot.get_me()
    await safe_edit_or_send(
        callback,
        build_file_card_text(updated, me.username or ""),
        build_file_keyboard(updated),
    )
    await callback.answer("★ Starred in Favorites" if is_fav else "Removed from Favorites")


@router.callback_query(F.data.startswith("f:share:"))
async def cb_toggle_share(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    token = await db.toggle_share_token(fid)
    updated = await db.get_file(fid)
    me = await bot.get_me()
    await safe_edit_or_send(
        callback,
        build_file_card_text(updated, me.username or ""),
        build_file_keyboard(updated),
    )
    await callback.answer("🔗 Public share link active!" if token else "Share link revoked")


@router.callback_query(F.data.startswith("f:trash:") | F.data.startswith("f:del:"))
async def cb_trash_file(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("Only the vault owner can trash this file.", show_alert=True)
        return
    await db.trash_file(fid)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="↩️ Undo (Restore)", callback_data=f"f:restore:{fid}"),
                InlineKeyboardButton(text="🔥 Delete Forever", callback_data=f"f:purge:{fid}"),
            ],
            [
                InlineKeyboardButton(text="📦 Back to Files", callback_data="br:all:def:0:0"),
                home_btn(),
            ],
        ]
    )
    await safe_edit_or_send(
        callback,
        f"🗑️ <b>Moved to Trash</b>\n{DIVIDER}\n"
        f"<b>{esc(f['title'])}</b> was moved to Recycle Trash.\n"
        f"<i>Tap Undo below to restore it immediately.</i>",
        kb,
    )
    await callback.answer("Moved to Trash")


@router.callback_query(F.data.startswith("f:restore:"))
async def cb_restore_file(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("File not found.", show_alert=True)
        return
    await db.restore_file(fid)
    restored = await db.get_file(fid)
    me = await bot.get_me()
    await safe_edit_or_send(
        callback,
        build_file_card_text(
            restored, me.username or "", header_badge="♻️ <b>Restored to Vault</b>"
        ),
        build_file_keyboard(restored),
    )
    await callback.answer("Restored!")


@router.callback_query(F.data.startswith("f:purge:"))
async def cb_purge_file(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("Only the vault owner can delete this file.", show_alert=True)
        return
    await db.delete_file(fid)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑️ Back to Trash", callback_data="trash:0"),
                home_btn(),
            ]
        ]
    )
    await safe_edit_or_send(
        callback,
        f"🔥 <b>Permanently Deleted</b>\n{DIVIDER}\n<b>{esc(f['title'])}</b> has been removed.",
        kb,
    )
    await callback.answer("Deleted forever")


@router.callback_query(F.data.startswith("f:rename:"))
async def cb_rename_prompt(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    await state.set_state(FileEditStates.waiting_for_title)
    await state.update_data(edit_file_id=fid)
    await safe_edit_or_send(
        callback,
        f"✏️ <b>Rename File</b>\n{DIVIDER}\n"
        f"Current: <code>{esc(f['title'])}</code>\n\n"
        f"Send the new title below:",
        cancel_kb(f"f:open:{fid}"),
    )
    await callback.answer()


@router.message(FileEditStates.waiting_for_title)
async def fsm_save_title(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not message.text:
        return
    data = await state.get_data()
    fid = data.get("edit_file_id")
    await state.clear()
    if not fid:
        return
    new_title = message.text.strip()[:120]
    if not new_title:
        return
    await db.update_file_title(fid, new_title)
    updated = await db.get_file(fid)
    me = await bot.get_me()
    await message.answer(
        build_file_card_text(
            updated, me.username or "", header_badge="✓ <b>Title Updated</b>"
        ),
        reply_markup=build_file_keyboard(updated),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("f:tags:"))
async def cb_tags_prompt(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    await state.set_state(FileEditStates.waiting_for_tags)
    await state.update_data(edit_file_id=fid)
    cur_tags = f"<code>{esc(f['tags'])}</code>" if f.get("tags") else "<i>None</i>"
    await safe_edit_or_send(
        callback,
        f"🏷️ <b>Edit Tags</b>\n{DIVIDER}\n"
        f"File: <b>{esc(f['title'])}</b>\n"
        f"Current tags: {cur_tags}\n\n"
        f"Send space-separated tags (e.g. <code>#lofi #chill #2026</code>), or send <code>-</code> to clear:",
        cancel_kb(f"f:open:{fid}"),
    )
    await callback.answer()


@router.message(FileEditStates.waiting_for_tags)
async def fsm_save_tags(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not message.text:
        return
    data = await state.get_data()
    fid = data.get("edit_file_id")
    await state.clear()
    if not fid:
        return
    raw = message.text.strip()
    if raw == "-":
        tags = ""
    else:
        tags = " ".join(
            t if t.startswith("#") else f"#{t}" for t in raw.split() if t.strip()
        )[:150]
    await db.update_file_tags(fid, tags)
    updated = await db.get_file(fid)
    me = await bot.get_me()
    await message.answer(
        build_file_card_text(
            updated, me.username or "", header_badge="✓ <b>Tags Updated</b>"
        ),
        reply_markup=build_file_keyboard(updated),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("f:cap:"))
async def cb_caption_prompt(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    await state.set_state(FileEditStates.waiting_for_caption)
    await state.update_data(edit_file_id=fid)
    cur_cap = f"<i>{esc(f['caption'][:120])}</i>" if f.get("caption") else "<i>None</i>"
    await safe_edit_or_send(
        callback,
        f"💬 <b>Edit Inline Caption</b>\n{DIVIDER}\n"
        f"File: <b>{esc(f['title'])}</b>\n"
        f"Current caption: {cur_cap}\n\n"
        f"Send the caption to attach when sending this file inline (or send <code>-</code> to clear):",
        cancel_kb(f"f:open:{fid}"),
    )
    await callback.answer()


@router.message(FileEditStates.waiting_for_caption)
async def fsm_save_caption(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not message.text:
        return
    data = await state.get_data()
    fid = data.get("edit_file_id")
    await state.clear()
    if not fid:
        return
    raw = message.text.strip()
    caption = "" if raw == "-" else raw[:900]
    await db.update_file_caption(fid, caption)
    updated = await db.get_file(fid)
    me = await bot.get_me()
    await message.answer(
        build_file_card_text(
            updated, me.username or "", header_badge="✓ <b>Caption Updated</b>"
        ),
        reply_markup=build_file_keyboard(updated),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("f:move:"))
async def cb_move_menu(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    groups = await db.list_vault_groups(user["vault_id"])

    buttons: list[list[InlineKeyboardButton]] = []
    for g in groups:
        icon = "👥" if g["is_shared"] else "📁"
        current_mark = " ✓" if f.get("group_id") == g["id"] else ""
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {g['name']}{current_mark}",
                    callback_data=f"f:setgrp:{fid}:{g['id']}",
                )
            ]
        )
    unassigned_mark = " ✓" if not f.get("group_id") else ""
    buttons.append(
        [
            InlineKeyboardButton(
                text=f"📂 Personal Vault (Unassigned){unassigned_mark}",
                callback_data=f"f:setgrp:{fid}:0",
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(text="➕ New Folder", callback_data="grp:new:personal"),
            InlineKeyboardButton(text="‹ Back", callback_data=f"f:open:{fid}"),
        ]
    )

    await safe_edit_or_send(
        callback,
        f"📁 <b>Move File to Folder</b>\n{DIVIDER}\n"
        f"Select a destination for <b>{esc(f['title'])}</b>:",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("f:setgrp:"))
async def cb_set_file_group(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    _, _, fid_s, gid_s = callback.data.split(":")
    fid = int(fid_s)
    gid: Optional[int] = int(gid_s) or None
    user = await db.get_user(callback.from_user.id)
    if gid is not None and not await db.is_vault_in_group(user["vault_id"], gid):
        await callback.answer("Invalid folder.", show_alert=True)
        return
    await db.move_file_to_group(fid, gid)
    updated = await db.get_file(fid)
    me = await bot.get_me()
    await safe_edit_or_send(
        callback,
        build_file_card_text(updated, me.username or ""),
        build_file_keyboard(updated),
    )
    await callback.answer("📁 Moved to folder!")
