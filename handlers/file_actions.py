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
    MEDIA_ICONS,
    MEDIA_LABELS,
    SORT_LABELS,
    SORT_ORDER,
    back_btn,
    breadcrumb,
    build_file_card_text,
    build_file_keyboard,
    build_file_manage_keyboard,
    cancel_kb,
    esc,
    format_size,
    home_btn,
    safe_edit_or_send,
    send_stored_media,
)
from utils.states import FileEditStates, SearchStates

router = Router(name="file_actions")

BROWSE_PAGE_SIZE = 5

FILTER_TO_MEDIA = {
    "audio": "audio",
    "video": "video",
    "doc": "document",
    "photo": "photo",
    "voice": "voice",
    "gif": "animation",
    "sticker": "sticker",
}

FILTER_LABELS = {
    "all": "All Files",
    "fav": "⭐ Starred",
    "audio": "🎵 Audio",
    "video": "🎬 Video",
    "doc": "📄 Docs",
    "photo": "🖼️ Photos",
    "voice": "🎙️ Voice",
    "gif": "🎞️ GIFs",
    "sticker": "🧩 Stickers",
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
    """
    Clean Mobile-First File Browser:
    - Shows 5 clean items per page (no overwhelming button list)
    - Replaces the cramped 6-pill bar + sort row with a single '🌪️ Filter & Sort' drawer button + '🔎 Search'
    - Clean pagination row only when multiple pages exist
    - Consistent Back / Home footer
    """
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

    # Header & Breadcrumb
    if group_filter:
        grp = await db.get_group(group_filter)
        folder_name = grp["name"] if grp else "Folder"
        folder_icon = "👥" if (grp and grp["is_shared"]) else "📁"
        trail = breadcrumb("Vault", "Folders", esc(folder_name))
        heading = f"{folder_icon} <b>{esc(folder_name)}</b>"
    elif only_fav:
        trail = breadcrumb("Vault", "Favorites")
        heading = "⭐ <b>Starred Files</b>"
    elif media_type:
        trail = breadcrumb("Vault", "My Files", MEDIA_LABELS.get(media_type, "Filtered"))
        heading = f"{MEDIA_ICONS.get(media_type, '📦')} <b>{MEDIA_LABELS.get(media_type, 'Files')}</b>"
    elif text_query:
        trail = breadcrumb("Vault", "Search")
        heading = f"🔎 <b>Results for</b> <code>{esc(text_query[:28])}</code>"
    else:
        trail = breadcrumb("Vault", "My Files")
        heading = "📦 <b>My Files</b>"

    sort_name = SORT_LABELS.get(sort_key, "Newest")
    flt_badge = FILTER_LABELS.get(flt, "All Files")
    total_pages = max(1, (total_matching + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)

    lines = [
        trail,
        heading,
        f"<i>{total_matching} item(s)  ·  {flt_badge}  ·  {sort_name}</i>\n",
    ]

    buttons: list[list[InlineKeyboardButton]] = []

    if not items:
        if text_query or flt != "all":
            lines.append(
                "<blockquote>No files match your current filter or search.</blockquote>"
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="🔄 Reset Filter",
                        callback_data=f"br:all:{sort_key}:{gid}:0",
                        style="primary",
                    )
                ]
            )
        else:
            lines.append(
                "<blockquote>Your vault is empty right now.\n"
                "Drop or forward any file into this chat to archive it!</blockquote>"
            )
    else:
        for f in items:
            icon = MEDIA_ICONS.get(f["media_type"], "📦")
            star = "⭐ " if f.get("is_favorite") else ""
            sz = f" · {format_size(f['file_size'])}" if f.get("file_size") else ""
            label = f"{star}{icon} {f['title'][:28]}{sz}"
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"fo:{f['id']}:{flt}:{sort_key}:{gid}:{page}",
                    )
                ]
            )

    # Clean Pagination Row (only when needed)
    if page > 0 or has_next:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    text="◀ Prev",
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
                    text="Next ▶",
                    callback_data=f"br:{flt}:{sort_key}:{gid}:{page + 1}",
                )
            )
        buttons.append(nav_row)

    # Clean 2-button Toolbar: Filter & Sort Drawer + Search
    active_filter_dot = " ●" if flt != "all" else ""
    buttons.append(
        [
            InlineKeyboardButton(
                text=f"🌪️ Filter & Sort{active_filter_dot}",
                callback_data=f"bf:{flt}:{sort_key}:{gid}:{page}",
            ),
            InlineKeyboardButton(
                text="🔎 Search",
                callback_data=f"f:sp:{flt}:{sort_key}:{gid}:{page}",
            ),
        ]
    )

    # Footer Navigation
    bottom_row: list[InlineKeyboardButton] = []
    if group_filter:
        bottom_row.append(back_btn(f"grp:open:{group_filter}", "◀ Folder"))
        bottom_row.append(home_btn())
    elif text_query:
        bottom_row.append(back_btn("br:all:def:0:0", "◀ All Files"))
        bottom_row.append(home_btn())
    else:
        bottom_row.append(back_btn("nav:home", "◀ Back to Vault"))
    buttons.append(bottom_row)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def render_filter_sort_drawer(
    flt: str, sort_key: str, gid: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Dedicated Filter & Sort Sub-Menu (Progressive Disclosure).
    Keeps the main file list completely clutter-free while offering ALL 8 media filters + 5 sort modes!
    """
    cur_flt_lbl = FILTER_LABELS.get(flt, "All Files")
    cur_sort_lbl = SORT_LABELS.get(sort_key, "Newest")

    text = (
        f"{breadcrumb('Vault', 'My Files', 'Filter & Sort')}\n"
        f"🌪️ <b>Filter & Sort Options</b>\n\n"
        f"<blockquote>"
        f"<b>Active Filter:</b> {cur_flt_lbl}\n"
        f"<b>Sort Order:</b> {cur_sort_lbl}"
        f"</blockquote>\n\n"
        f"<i>Choose a category or cycle the sort order:</i>"
    )

    def _fbtn(key: str, lbl: str) -> InlineKeyboardButton:
        prefix = "✅ " if flt == key else ""
        return InlineKeyboardButton(
            text=f"{prefix}{lbl}",
            callback_data=f"bfs:{key}:{sort_key}:{gid}",
        )

    next_idx = (SORT_ORDER.index(sort_key) + 1) % len(SORT_ORDER) if sort_key in SORT_ORDER else 0
    next_sort = SORT_ORDER[next_idx]

    rows = [
        [_fbtn("all", "All Files"), _fbtn("fav", "⭐ Starred")],
        [_fbtn("audio", "🎵 Audio"), _fbtn("video", "🎬 Video")],
        [_fbtn("doc", "📄 Docs"), _fbtn("photo", "🖼️ Photos")],
        [_fbtn("voice", "🎙️ Voice"), _fbtn("gif", "🎞️ GIFs")],
        [
            InlineKeyboardButton(
                text=f"↕️ Sort: {cur_sort_lbl} (Tap to switch)",
                callback_data=f"bf:{flt}:{next_sort}:{gid}:0",
            )
        ],
        [
            InlineKeyboardButton(
                text="✓ Done — View Files",
                callback_data=f"br:{flt}:{sort_key}:{gid}:0",
                style="primary",
            )
        ],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


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
    total_pages = max(1, (total + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)

    lines = [
        breadcrumb("Vault", "Settings", "Trash"),
        f"🗑️ <b>Recycle Trash</b> · <i>{total} item(s)</i>\n",
    ]
    buttons: list[list[InlineKeyboardButton]] = []

    if not items:
        lines.append("<blockquote>Your recycle trash is clean.</blockquote>")
    else:
        lines.append("<i>Tap an item to restore it or delete permanently:</i>")
        for f in items:
            icon = MEDIA_ICONS.get(f["media_type"], "📦")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{icon} {f['title'][:32]}",
                        callback_data=f"f:open:{f['id']}",
                    )
                ]
            )

    if page > 0 or has_next:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="◀ Prev", callback_data=f"trash:{page - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"{page + 1} / {total_pages}", callback_data="noop"))
        if has_next:
            nav_row.append(InlineKeyboardButton(text="Next ▶", callback_data=f"trash:{page + 1}"))
        buttons.append(nav_row)

    if total > 0:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="💥 Empty Entire Trash",
                    callback_data="trash:empty_confirm",
                    style="danger",
                )
            ]
        )

    buttons.append(
        [
            back_btn("nav:settings", "◀ Settings"),
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


@router.callback_query(F.data.startswith("bf:"))
async def cb_open_filter_drawer(callback: CallbackQuery) -> None:
    _, flt, sort_key, gid_s, page_s = callback.data.split(":")
    text, kb = render_filter_sort_drawer(flt, sort_key, int(gid_s), int(page_s))
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("bfs:"))
async def cb_select_filter_and_apply(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    _, flt, sort_key, gid_s = callback.data.split(":")
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await render_browse_page(
        callback.from_user.id, flt, sort_key, int(gid_s), 0, db
    )
    await safe_edit_or_send(callback, text, kb)
    await callback.answer(f"Filter: {FILTER_LABELS.get(flt, 'All')}")


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
        f"{breadcrumb('Vault', 'Search')}\n"
        f"🔎 <b>Search Your Vault</b>\n\n"
        f"<blockquote>Type any keyword, artist, filename, or <code>#tag</code> below:</blockquote>",
        reply_markup=cancel_kb("br:all:def:0:0"),
    )


@router.callback_query(F.data == "f:search_prompt")
async def cb_search_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchStates.waiting_for_query)
    await safe_edit_or_send(
        callback,
        f"{breadcrumb('Vault', 'Search')}\n"
        f"🔎 <b>Search Your Vault</b>\n\n"
        f"<blockquote>Type any keyword, artist, filename, or <code>#tag</code> below:</blockquote>",
        cancel_kb("br:all:def:0:0"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("f:sp:"))
async def cb_search_prompt_ctx(callback: CallbackQuery, state: FSMContext) -> None:
    back_target = "br:" + callback.data.split(":", 2)[2]
    await state.set_state(SearchStates.waiting_for_query)
    await safe_edit_or_send(
        callback,
        f"{breadcrumb('Vault', 'Search')}\n"
        f"🔎 <b>Search Your Vault</b>\n\n"
        f"<blockquote>Type any keyword, artist, filename, or <code>#tag</code> below:</blockquote>",
        cancel_kb(back_target),
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
    # Safe action LEFT, Destructive action RIGHT with danger style
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                back_btn("trash:0", "◀ Cancel"),
                InlineKeyboardButton(
                    text="💥 Delete All Forever",
                    callback_data="trash:empty_do",
                    style="danger",
                ),
            ]
        ]
    )
    await safe_edit_or_send(
        callback,
        f"⚠️ <b>Empty Recycle Trash?</b>\n\n"
        f"<blockquote>This permanently deletes <b>{cnt}</b> file(s) from your vault. This cannot be undone.</blockquote>",
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
        build_file_keyboard(f, back_cb=back_cb, bot_username=me.username or ""),
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
        build_file_keyboard(f, bot_username=me.username or ""),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("fm:") | F.data.startswith("f:manage:"))
async def cb_open_file_manage(
    callback: CallbackQuery, db: Database, bot: Bot
) -> None:
    """Opens the Progressive Disclosure Level-2 'Edit & More' file drawer."""
    if not callback.from_user:
        return
    if callback.data.startswith("fm:"):
        parts = callback.data.split(":", 2)
        fid = int(parts[1])
        back_cb: Optional[str] = parts[2] if len(parts) > 2 else None
    else:
        fid = int(callback.data.split(":")[2])
        back_cb = None

    user = await db.get_user(callback.from_user.id)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    me = await bot.get_me()
    await safe_edit_or_send(
        callback,
        build_file_card_text(f, me.username or "", edit_mode=True),
        build_file_manage_keyboard(f, back_cb=back_cb, bot_username=me.username or ""),
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


@router.callback_query(F.data.startswith("ffav:") | F.data.startswith("f:fav:"))
async def cb_toggle_fav(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    if callback.data.startswith("ffav:"):
        parts = callback.data.split(":", 2)
        fid = int(parts[1])
        back_cb: Optional[str] = parts[2] if len(parts) > 2 else None
    else:
        fid = int(callback.data.split(":")[2])
        back_cb = None

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
        build_file_keyboard(updated, back_cb=back_cb, bot_username=me.username or ""),
    )
    await callback.answer("⭐ Starred!" if is_fav else "Removed from Starred")


@router.callback_query(F.data.startswith("fsh:") | F.data.startswith("f:share:"))
async def cb_toggle_share(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    if callback.data.startswith("fsh:"):
        parts = callback.data.split(":", 2)
        fid = int(parts[1])
        back_cb: Optional[str] = parts[2] if len(parts) > 2 else None
    else:
        fid = int(callback.data.split(":")[2])
        back_cb = None

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
        build_file_card_text(updated, me.username or "", edit_mode=True),
        build_file_manage_keyboard(updated, back_cb=back_cb, bot_username=me.username or ""),
    )
    await callback.answer("🔗 Public link created!" if token else "Public link revoked")


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
    # Safe Undo action LEFT (success), Permanent Delete RIGHT (danger)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Undo (Restore)",
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
                back_btn("br:all:def:0:0", "◀ My Files"),
                home_btn(),
            ],
        ]
    )
    await safe_edit_or_send(
        callback,
        f"🗑️ <b>Moved to Trash</b>\n\n"
        f"<blockquote><b>{esc(f['title'])}</b> was moved to Recycle Trash.\n"
        f"Tap <b>Undo</b> below to restore it immediately.</blockquote>",
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
        build_file_keyboard(restored, bot_username=me.username or ""),
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
    await safe_edit_or_send(
        callback,
        f"⚠️ <b>Delete Forever?</b>\n\n"
        f"<blockquote>Permanently remove <b>{esc(f['title'])}</b>? This cannot be undone.</blockquote>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    back_btn(f"f:open:{fid}", "◀ Cancel"),
                    InlineKeyboardButton(
                        text="💥 Confirm Delete",
                        callback_data=f"f:purge_do:{fid}",
                        style="danger",
                    ),
                ]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("f:purge_do:"))
async def cb_purge_file_do(callback: CallbackQuery, db: Database) -> None:
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
                back_btn("trash:0", "◀ Trash"),
                home_btn(),
            ]
        ]
    )
    await safe_edit_or_send(
        callback,
        f"💥 <b>Permanently Deleted</b>\n<blockquote><b>{esc(f['title'])}</b> has been removed.</blockquote>",
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
        f"{breadcrumb('Vault', 'Manage File', 'Rename')}\n"
        f"✏️ <b>Rename File</b>\n\n"
        f"<blockquote>Current: <code>{esc(f['title'])}</code></blockquote>\n\n"
        f"<i>Send the new title below:</i>",
        cancel_kb(f"f:manage:{fid}"),
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
            updated, me.username or "", header_badge="✅ <b>Title Updated</b>"
        ),
        reply_markup=build_file_keyboard(updated, bot_username=me.username or ""),
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
        f"{breadcrumb('Vault', 'Manage File', 'Tags')}\n"
        f"🏷️ <b>Edit Tags</b>\n\n"
        f"<blockquote><b>{esc(f['title'])}</b>\nCurrent: {cur_tags}</blockquote>\n\n"
        f"<i>Send hashtags (e.g. <code>#lofi #chill</code>) or <code>-</code> to clear:</i>",
        cancel_kb(f"f:manage:{fid}"),
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
            updated, me.username or "", header_badge="✅ <b>Tags Updated</b>"
        ),
        reply_markup=build_file_keyboard(updated, bot_username=me.username or ""),
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
        f"{breadcrumb('Vault', 'Manage File', 'Caption')}\n"
        f"💬 <b>Inline Caption</b>\n\n"
        f"<blockquote><b>{esc(f['title'])}</b>\nCurrent: {cur_cap}</blockquote>\n\n"
        f"<i>Send caption text for inline shares, or <code>-</code> to clear:</i>",
        cancel_kb(f"f:manage:{fid}"),
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
            updated, me.username or "", header_badge="✅ <b>Caption Updated</b>"
        ),
        reply_markup=build_file_keyboard(updated, bot_username=me.username or ""),
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
    # Arrange folders neatly in up to 2 columns if multiple exist
    row_buf: list[InlineKeyboardButton] = []
    for g in groups:
        icon = "👥" if g["is_shared"] else "📁"
        prefix = "✅ " if f.get("group_id") == g["id"] else f"{icon} "
        row_buf.append(
            InlineKeyboardButton(
                text=f"{prefix}{g['name'][:18]}",
                callback_data=f"f:setgrp:{fid}:{g['id']}",
            )
        )
        if len(row_buf) == 2:
            buttons.append(row_buf)
            row_buf = []
    if row_buf:
        buttons.append(row_buf)

    unassigned_prefix = "✅ " if not f.get("group_id") else "📂 "
    buttons.append(
        [
            InlineKeyboardButton(
                text=f"{unassigned_prefix}Personal Vault (Root)",
                callback_data=f"f:setgrp:{fid}:0",
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(text="➕ New Folder", callback_data="grp:new:personal"),
            back_btn(f"f:manage:{fid}", "◀ Back"),
        ]
    )

    await safe_edit_or_send(
        callback,
        f"{breadcrumb('Vault', 'Manage File', 'Move')}\n"
        f"📁 <b>Move to Folder</b>\n\n"
        f"<blockquote>Select a destination for <b>{esc(f['title'])}</b>:</blockquote>",
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
        build_file_keyboard(updated, bot_username=me.username or ""),
    )
    await callback.answer("📁 Moved to folder!")
