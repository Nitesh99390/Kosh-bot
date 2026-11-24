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
    build_file_link_keyboard,
    build_file_manage_keyboard,
    cancel_kb,
    esc,
    format_expiry_badge,
    format_max_views_badge,
    format_size,
    format_ttl_short,
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
    "round": "video_note",
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
    "round": "⭕ Rounds",
}


async def _ensure_cb_user(callback: CallbackQuery, db: Database) -> dict:
    assert callback.from_user is not None
    return await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )


def _parse_fid_and_ctx(data: str, prefix: str) -> tuple[int, Optional[str]]:
    """
    Parses callbacks like `f:1x:12` or `f:1x:12:all:new:0:0` cleanly into `(fid, back_cb)`.
    """
    rest = data[len(prefix) :]
    parts = rest.split(":")
    fid = int(parts[0])
    if len(parts) >= 5:
        ctx = ":".join(parts[1:5])
        return fid, f"br:{ctx}"
    return fid, None


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
    Clean, Simple & Scannable File Browser:
    - 5 clean file buttons per page
    - Accurate search count & pagination
    - Symmetric 2-column action & navigation footer
    """
    user = await db.get_user(user_id)
    if not user:
        user = await db.ensure_user(user_id)
    if sort_key == "def" or sort_key not in SORT_LABELS:
        sort_key = user.get("default_sort") or "new"

    only_fav = flt == "fav"
    media_type = FILTER_TO_MEDIA.get(flt)
    group_filter = gid if gid > 0 else None

    total_matching = await db.count_files(
        user["vault_id"],
        text_query=text_query,
        media_type=media_type,
        group_id_filter=group_filter,
        only_favorites=only_fav,
    )
    total_pages = max(1, (total_matching + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    offset = page * BROWSE_PAGE_SIZE

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

    if group_filter:
        grp = await db.get_group(group_filter)
        folder_name = grp["name"] if grp else "Folder"
        folder_icon = "👥" if (grp and grp["is_shared"]) else "📁"
        trail = breadcrumb("🏠 Home", "📁 Folders", esc(folder_name))
        heading = f"{folder_icon} <b>{esc(folder_name)}</b>"
    elif only_fav:
        trail = breadcrumb("🏠 Home", "⭐ Starred")
        heading = "⭐ <b>Starred Files</b>"
    elif media_type:
        trail = breadcrumb("🏠 Home", "📦 My Files", MEDIA_LABELS.get(media_type, "Filter"))
        heading = f"{MEDIA_ICONS.get(media_type, '📦')} <b>{MEDIA_LABELS.get(media_type, 'Files')}</b>"
    elif text_query:
        trail = breadcrumb("🏠 Home", "🔎 Search Results")
        heading = f"🔎 <b>Search:</b> <code>{esc(text_query[:26])}</code>"
    else:
        trail = breadcrumb("🏠 Home", "📦 My Files")
        heading = "📦 <b>My Files</b>"

    sort_name = SORT_LABELS.get(sort_key, "Newest")
    flt_badge = FILTER_LABELS.get(flt, "All Files")

    lines = [
        trail,
        f"{heading}",
        f"<i>{total_matching} file(s)  •  {flt_badge}  •  Sort: {sort_name}</i>",
    ]

    buttons: list[list[InlineKeyboardButton]] = []

    if not items:
        if text_query or flt != "all":
            lines.append("<blockquote>No matching files found. Try clearing the filter or search.</blockquote>")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="🔄 Show All Files",
                        callback_data=f"br:all:{sort_key}:{gid}:0",
                        style="primary",
                    )
                ]
            )
        else:
            lines.append(
                "<blockquote>Your vault is empty right now.\nSend or forward any file here to save it!</blockquote>"
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="📤 How to Upload",
                        callback_data="nav:upload",
                        style="primary",
                    )
                ]
            )
    else:
        for f in items:
            icon = MEDIA_ICONS.get(f["media_type"], "📦")
            star = "⭐ " if f.get("is_favorite") else ""
            link_dot = " 🔗" if f.get("share_token") else ""
            sz = f" · {format_size(f['file_size'])}" if f.get("file_size") else ""
            label = f"{star}{icon} {f['title'][:25]}{sz}{link_dot}"
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"fo:{f['id']}:{flt}:{sort_key}:{gid}:{page}",
                    )
                ]
            )

    if (page > 0 or has_next) and not text_query:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    text="⬅️ Prev",
                    callback_data=f"br:{flt}:{sort_key}:{gid}:{page - 1}",
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f"📄 {page + 1}/{total_pages}",
                callback_data="noop",
            )
        )
        if has_next:
            nav_row.append(
                InlineKeyboardButton(
                    text="Next ➡️",
                    callback_data=f"br:{flt}:{sort_key}:{gid}:{page + 1}",
                )
            )
        buttons.append(nav_row)

    active_filter_dot = " ●" if flt != "all" else ""
    buttons.append(
        [
            InlineKeyboardButton(
                text=f"🎛️ Filter & Sort{active_filter_dot}",
                callback_data=f"bf:{flt}:{sort_key}:{gid}:{page}",
            ),
            InlineKeyboardButton(
                text="🔎 Search",
                callback_data=f"f:sp:{flt}:{sort_key}:{gid}:{page}",
            ),
        ]
    )

    bottom_row: list[InlineKeyboardButton] = []
    if group_filter:
        bottom_row.append(back_btn(f"grp:open:{group_filter}", "⬅️ Folder"))
        bottom_row.append(home_btn())
    elif text_query or flt != "all":
        bottom_row.append(back_btn("br:all:def:0:0", "⬅️ All Files"))
        bottom_row.append(home_btn())
    else:
        bottom_row.append(
            InlineKeyboardButton(text="⭐ Starred", callback_data="br:fav:def:0:0")
        )
        bottom_row.append(home_btn())
    buttons.append(bottom_row)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def render_filter_sort_drawer(
    flt: str, sort_key: str, gid: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Complete, symmetric 2-column Filter & Sort Drawer (includes Stickers & Video Rounds!).
    """
    cur_flt_lbl = FILTER_LABELS.get(flt, "All Files")
    cur_sort_lbl = SORT_LABELS.get(sort_key, "Newest")

    text = (
        f"{breadcrumb('🏠 Home', '📦 My Files', '🎛️ Filter & Sort')}\n"
        f"🎛️ <b>Filter &amp; Sort Files</b>\n"
        f"<blockquote>Filter: <b>{cur_flt_lbl}</b>  •  Sort: <b>{cur_sort_lbl}</b></blockquote>\n"
        f"<i>Tap a category to filter immediately, or tap Sort to change order:</i>"
    )

    def _fbtn(key: str, lbl: str) -> InlineKeyboardButton:
        is_sel = flt == key
        prefix = "✅ " if is_sel else ""
        return InlineKeyboardButton(
            text=f"{prefix}{lbl}",
            callback_data=f"bfs:{key}:{sort_key}:{gid}",
            style="success" if is_sel else None,
        )

    next_idx = (
        (SORT_ORDER.index(sort_key) + 1) % len(SORT_ORDER)
        if sort_key in SORT_ORDER
        else 0
    )
    next_sort = SORT_ORDER[next_idx]

    rows = [
        [_fbtn("all", "All Files"), _fbtn("fav", "⭐ Starred")],
        [_fbtn("audio", "🎵 Audio"), _fbtn("video", "🎬 Video")],
        [_fbtn("doc", "📄 Docs"), _fbtn("photo", "🖼️ Photos")],
        [_fbtn("voice", "🎙️ Voice"), _fbtn("gif", "🎞️ GIFs")],
        [_fbtn("sticker", "🧩 Stickers"), _fbtn("round", "⭕ Rounds")],
        [
            InlineKeyboardButton(
                text=f"↕️ Sort: {cur_sort_lbl}",
                callback_data=f"bf:{flt}:{next_sort}:{gid}:0",
            ),
            InlineKeyboardButton(
                text="✓ Done",
                callback_data=f"br:{flt}:{sort_key}:{gid}:0",
                style="primary",
            ),
        ],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _render_file_link_card(
    file_row: dict,
    bot_username: str,
    unique_claims: int = 0,
    owner_global_ttl: int = 0,
) -> str:
    icon = MEDIA_ICONS.get(file_row["media_type"], "📦")
    has_link = bool(file_row.get("share_token"))
    lines = [
        breadcrumb("🏠 Home", "📦 File", "🔗 Share Link"),
        f"🔗 <b>Share Link &amp; Security</b>\n{icon} <b>{esc(file_row['title'][:36])}</b>",
    ]
    if not has_link:
        lines.append(
            "<blockquote>Create a permanent shareable link for this file.\n"
            "You can optionally add <b>1×/User Trial Lock</b>, <b>Auto-Delete</b>, <b>PIN</b>, or <b>No-Forward</b> protection.</blockquote>"
        )
    else:
        url = f"https://t.me/{bot_username}?start=file_{file_row['share_token']}"
        pin_status = "🟢 ON" if file_row.get("share_pin_hash") else "⚪ OFF"
        prot_status = "🟢 ON" if file_row.get("protect_share") else "⚪ OFF"
        one_time_on = bool(file_row.get("share_one_time"))
        one_time_str = f"🟢 ON ({unique_claims} used)" if one_time_on else "⚪ OFF"
        exp_status = format_expiry_badge(file_row.get("share_expires_at"))
        per_link_ttl = int(file_row.get("auto_delete_ttl") or 0)
        if per_link_ttl > 0:
            adel_status = format_ttl_short(per_link_ttl)
        elif owner_global_ttl > 0:
            adel_status = f"Off <i>(Global: {format_ttl_short(owner_global_ttl)})</i>"
        else:
            adel_status = "Off"
        views_badge = format_max_views_badge(
            int(file_row.get("share_views") or 0),
            int(file_row.get("share_max_views") or 0),
        )
        state_badge = (
            "⏸ Paused"
            if file_row.get("share_paused")
            else "🟢 Permanent"
            if not file_row.get("share_expires_at")
            else "🟢 Active"
        )
        lines.append(
            f"<blockquote>"
            f"📡 <b>Status:</b> {state_badge}  •  {views_badge}  •  ⏳ <b>Link:</b> {exp_status}\n"
            f"💣 <b>Auto-Del:</b> {adel_status}  •  👤 <b>1×/User:</b> {one_time_str}\n"
            f"🛡️ <b>No-Fwd:</b> {prot_status}  •  🔑 <b>PIN:</b> {pin_status}"
            f"</blockquote>\n"
            f"🔗 <code>{url}</code>"
        )
    return "\n".join(lines)


async def render_move_file_menu(
    user_id: int,
    fid: int,
    db: Database,
    back_cb: Optional[str] = None,
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    """Renders the Move File to Folder picker while preserving `back_cb` context."""
    user = await db.get_user(user_id)
    if not user:
        user = await db.ensure_user(user_id)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        return None
    groups = await db.list_vault_groups(user["vault_id"])

    ctx = (
        back_cb[3:]
        if (back_cb and back_cb.startswith("br:") and len(back_cb.split(":")) == 5)
        else ""
    )
    ctx_suffix = f":{ctx}" if ctx else ""
    return_to_card = f"fo:{fid}:{ctx}" if ctx else f"f:open:{fid}"

    buttons: list[list[InlineKeyboardButton]] = []
    row_buf: list[InlineKeyboardButton] = []
    for g in groups:
        icon = "👥" if g["is_shared"] else "📁"
        is_cur = f.get("group_id") == g["id"]
        prefix = "✅ " if is_cur else f"{icon} "
        row_buf.append(
            InlineKeyboardButton(
                text=f"{prefix}{g['name'][:16]}",
                callback_data=f"f:setgrp:{fid}:{g['id']}{ctx_suffix}",
                style="success" if is_cur else None,
            )
        )
        if len(row_buf) == 2:
            buttons.append(row_buf)
            row_buf = []
    if row_buf:
        buttons.append(row_buf)

    is_personal = not f.get("group_id")
    unassigned_prefix = "✅ " if is_personal else "📂 "
    buttons.append(
        [
            InlineKeyboardButton(
                text=f"{unassigned_prefix}Personal Vault (Default)",
                callback_data=f"f:setgrp:{fid}:0{ctx_suffix}",
                style="success" if is_personal else None,
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ New Folder",
                callback_data=f"grp:new_for_file:{fid}",
                style="primary",
            ),
            back_btn(return_to_card, "⬅️ Back"),
        ]
    )

    text = (
        f"{breadcrumb('🏠 Home', '📦 File', '📁 Move Folder')}\n"
        f"📁 <b>Choose Folder</b>\n"
        f"<blockquote>📄 <b>{esc(f['title'][:40])}</b>\nSelect where to keep this file:</blockquote>"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_trash_page(
    user_id: int, page: int, db: Database
) -> tuple[str, InlineKeyboardMarkup]:
    user = await db.get_user(user_id)
    if not user:
        user = await db.ensure_user(user_id)
    total = await db.count_trashed_files(user["vault_id"])
    total_pages = max(1, (total + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    offset = page * BROWSE_PAGE_SIZE
    items = await db.list_trashed_files(
        user["vault_id"], limit=BROWSE_PAGE_SIZE + 1, offset=offset
    )
    has_next = len(items) > BROWSE_PAGE_SIZE
    items = items[:BROWSE_PAGE_SIZE]

    lines = [
        breadcrumb("🏠 Home", "⚙️ Settings", "🗑️ Trash"),
        f"🗑️ <b>Recycle Trash</b> · <i>{total} item(s)</i>",
    ]
    buttons: list[list[InlineKeyboardButton]] = []

    if not items:
        lines.append("<blockquote>Trash is clean and empty.</blockquote>")
    else:
        lines.append("<i>Tap any file below to restore or permanently delete it:</i>")
        for f in items:
            icon = MEDIA_ICONS.get(f["media_type"], "📦")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{icon} {f['title'][:30]}",
                        callback_data=f"f:open:{f['id']}",
                    )
                ]
            )

    if page > 0 or has_next:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"trash:{page - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"📄 {page + 1}/{total_pages}", callback_data="noop"))
        if has_next:
            nav_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"trash:{page + 1}"))
        buttons.append(nav_row)

    if total > 0:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="💥 Empty Trash",
                    callback_data="trash:empty_confirm",
                    style="danger",
                )
            ]
        )

    buttons.append([back_btn("nav:settings", "⬅️ Settings"), home_btn()])
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
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data.startswith("bfs:"))
async def cb_select_filter_and_apply(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    _, flt, sort_key, gid_s = callback.data.split(":")
    await _ensure_cb_user(callback, db)
    text, kb = await render_browse_page(
        callback.from_user.id, flt, sort_key, int(gid_s), 0, db
    )
    await safe_edit_or_send(
        callback,
        text,
        kb,
        answer_text=f"Filter: {FILTER_LABELS.get(flt, 'All Files')}",
        also_ack=True,
    )


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
    await state.update_data(search_gid=0)
    await message.answer(
        f"{breadcrumb('🏠 Home', '🔎 Search')}\n"
        f"🔎 <b>Search Your Vault</b>\n"
        f"<blockquote>Type any file name, keyword, artist, or <code>#tag</code>:</blockquote>",
        reply_markup=cancel_kb("br:all:def:0:0"),
    )


@router.callback_query(F.data == "f:search_prompt")
async def cb_search_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchStates.waiting_for_query)
    await state.update_data(search_gid=0)
    await safe_edit_or_send(
        callback,
        f"{breadcrumb('🏠 Home', '🔎 Search')}\n"
        f"🔎 <b>Search Your Vault</b>\n"
        f"<blockquote>Type any file name, keyword, artist, or <code>#tag</code>:</blockquote>",
        cancel_kb("br:all:def:0:0"),
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:sp:"))
async def cb_search_prompt_ctx(callback: CallbackQuery, state: FSMContext) -> None:
    ctx_tail = callback.data.split(":", 2)[2]
    back_target = "br:" + ctx_tail
    ctx_parts = ctx_tail.split(":")
    gid_val = int(ctx_parts[2]) if len(ctx_parts) >= 3 and ctx_parts[2].isdigit() else 0
    await state.set_state(SearchStates.waiting_for_query)
    await state.update_data(search_gid=gid_val)
    await safe_edit_or_send(
        callback,
        f"{breadcrumb('🏠 Home', '🔎 Search')}\n"
        f"🔎 <b>Search Files</b>\n"
        f"<blockquote>Type any file name, keyword, artist, or <code>#tag</code>:</blockquote>",
        cancel_kb(back_target),
        also_ack=True,
    )


@router.message(SearchStates.waiting_for_query)
async def fsm_run_search(
    message: Message, state: FSMContext, db: Database
) -> None:
    if not message.from_user or not message.text:
        return
    q = message.text.strip()
    if q.startswith("/") and q.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    data = await state.get_data()
    gid_val = int(data.get("search_gid") or 0)
    await state.clear()
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    text, kb = await render_browse_page(
        message.from_user.id, "all", "def", gid_val, 0, db, text_query=q
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
    user = await _ensure_cb_user(callback, db)
    cnt = await db.count_trashed_files(user["vault_id"])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                back_btn("trash:0", "⬅️ Cancel"),
                InlineKeyboardButton(
                    text="💥 Delete All",
                    callback_data="trash:empty_do",
                    style="danger",
                ),
            ]
        ]
    )
    await safe_edit_or_send(
        callback,
        f"⚠️ <b>Empty Trash ({cnt} files)?</b>\n<blockquote>This permanently deletes all items in Trash and cannot be undone.</blockquote>",
        kb,
        also_ack=True,
    )


@router.callback_query(F.data == "trash:empty_do")
async def cb_trash_empty_do(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    user = await _ensure_cb_user(callback, db)
    removed = await db.empty_trash(user["vault_id"])
    text, kb = await render_trash_page(callback.from_user.id, 0, db)
    await safe_edit_or_send(
        callback,
        text,
        kb,
        answer_text=f"Deleted {removed} file(s).",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("trash:"))
async def cb_trash_list(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    part = callback.data.split(":")[1]
    if not part.isdigit():
        return
    await _ensure_cb_user(callback, db)
    text, kb = await render_trash_page(callback.from_user.id, int(part), db)
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data.startswith("browse:"))
async def cb_legacy_browse(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    page = int(callback.data.split(":")[1])
    await _ensure_cb_user(callback, db)
    text, kb = await render_browse_page(callback.from_user.id, "all", "def", 0, page, db)
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data.startswith("br:"))
async def cb_browse_modern(
    callback: CallbackQuery,
    db: Database,
    state: Optional[FSMContext] = None,
) -> None:
    if not callback.from_user:
        return
    if state is not None:
        await state.clear()
    _, flt, sort_key, gid_s, page_s = callback.data.split(":")
    await _ensure_cb_user(callback, db)
    text, kb = await render_browse_page(
        callback.from_user.id, flt, sort_key, int(gid_s), int(page_s), db
    )
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data.startswith("fo:"))
async def cb_open_file_with_context(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
    state: Optional[FSMContext] = None,
) -> None:
    if not callback.from_user:
        return
    if state is not None:
        await state.clear()
    _, fid_s, flt, sort_key, gid_s, page_s = callback.data.split(":")
    fid = int(fid_s)
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    uname = bot_username or "KoshBox_Bot"
    back_cb = f"br:{flt}:{sort_key}:{gid_s}:{page_s}"
    await safe_edit_or_send(
        callback,
        build_file_card_text(f, uname),
        build_file_keyboard(f, back_cb=back_cb, bot_username=uname),
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:open:"))
async def cb_open_file(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
    state: Optional[FSMContext] = None,
) -> None:
    if not callback.from_user:
        return
    if state is not None:
        await state.clear()
    fid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    uname = bot_username or "KoshBox_Bot"
    await safe_edit_or_send(
        callback,
        build_file_card_text(f, uname),
        build_file_keyboard(f, bot_username=uname),
        also_ack=True,
    )


@router.callback_query(F.data.startswith("fm:") | F.data.startswith("f:manage:"))
async def cb_open_file_manage(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
    state: Optional[FSMContext] = None,
) -> None:
    if not callback.from_user:
        return
    if state is not None:
        await state.clear()
    if callback.data.startswith("fm:"):
        parts = callback.data.split(":", 2)
        fid = int(parts[1])
        back_cb: Optional[str] = parts[2] if len(parts) > 2 else None
    else:
        fid = int(callback.data.split(":")[2])
        back_cb = None

    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    uname = bot_username or "KoshBox_Bot"
    await safe_edit_or_send(
        callback,
        build_file_card_text(f, uname, edit_mode=True),
        build_file_manage_keyboard(f, back_cb=back_cb, bot_username=uname),
        also_ack=True,
    )


@router.callback_query(F.data.startswith("fsl:") | F.data.startswith("f:link:"))
async def cb_open_file_link_panel(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
    state: Optional[FSMContext] = None,
) -> None:
    """Opens the dedicated Share Link & Security panel while preserving `back_cb`."""
    if not callback.from_user:
        return
    if state is not None:
        await state.clear()
    if callback.data.startswith("fsl:"):
        parts = callback.data.split(":", 2)
        fid = int(parts[1])
        back_cb: Optional[str] = parts[2] if len(parts) > 2 else None
    else:
        fid = int(callback.data.split(":")[2])
        back_cb = None

    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    uname = bot_username or "KoshBox_Bot"
    claims = await db.count_link_claims("file", fid)
    owner = await db.get_user(f["uploader_user_id"])
    owner_ttl = int((owner or {}).get("auto_delete_ttl") or 0)
    await safe_edit_or_send(
        callback,
        _render_file_link_card(f, uname, unique_claims=claims, owner_global_ttl=owner_ttl),
        build_file_link_keyboard(f, back_cb=back_cb, bot_username=uname),
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:get:"))
async def cb_get_file_here(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user or not callback.message:
        return
    fid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or not user or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    is_owner = (
        user["user_id"] == f["uploader_user_id"]
        or user["vault_id"] == f["vault_id"]
    )
    sent = await send_stored_media(
        bot,
        callback.message.chat.id,
        f,
        protect_content=False if is_owner else bool(user.get("protect_content")),
        auto_delete_ttl=0 if is_owner else int(user.get("auto_delete_ttl") or 0),
        db=db,
        extra_note="🔓 Owner Access — permanent & unrestricted" if is_owner else "",
    )
    if sent:
        await db.increment_use_count(fid)
        await callback.answer(
            "🔓 Owner Access — permanent & unrestricted" if is_owner else "📥 Sent!"
        )
    else:
        await callback.answer("Unsupported format.", show_alert=True)


@router.callback_query(F.data.startswith("ffav:") | F.data.startswith("f:fav:"))
async def cb_toggle_fav(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    if callback.data.startswith("ffav:"):
        parts = callback.data.split(":", 2)
        fid = int(parts[1])
        back_cb: Optional[str] = parts[2] if len(parts) > 2 else None
    else:
        fid = int(callback.data.split(":")[2])
        back_cb = None

    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    is_fav = await db.toggle_favorite(fid)
    updated = await db.get_file(fid)
    uname = bot_username or "KoshBox_Bot"
    await safe_edit_or_send(
        callback,
        build_file_card_text(updated, uname),
        build_file_keyboard(updated, back_cb=back_cb, bot_username=uname),
        answer_text="⭐ Added to Starred" if is_fav else "Removed from Starred",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("fsh:") | F.data.startswith("f:share:"))
async def cb_toggle_share(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    if callback.data.startswith("fsh:"):
        parts = callback.data.split(":", 2)
        fid = int(parts[1])
        back_cb: Optional[str] = parts[2] if len(parts) > 2 else None
    else:
        fid = int(callback.data.split(":")[2])
        back_cb = None

    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    token = await db.toggle_share_token(fid)
    updated = await db.get_file(fid)
    claims = await db.count_link_claims("file", fid)
    owner = await db.get_user(updated["uploader_user_id"])
    owner_ttl = int((owner or {}).get("auto_delete_ttl") or 0)
    uname = bot_username or "KoshBox_Bot"
    await safe_edit_or_send(
        callback,
        _render_file_link_card(updated, uname, unique_claims=claims, owner_global_ttl=owner_ttl),
        build_file_link_keyboard(updated, back_cb=back_cb, bot_username=uname),
        answer_text="🔗 Share Link created!" if token else "🚫 Share Link disabled",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:1x:"))
async def cb_file_toggle_one_time(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    fid, back_cb = _parse_fid_and_ctx(callback.data, "f:1x:")
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("Owner only.", show_alert=True)
        return
    on = await db.toggle_file_one_time(fid)
    updated = await db.get_file(fid)
    claims = await db.count_link_claims("file", fid)
    owner_ttl = int((user or {}).get("auto_delete_ttl") or 0)
    uname = bot_username or "KoshBox_Bot"
    await safe_edit_or_send(
        callback,
        _render_file_link_card(updated, uname, unique_claims=claims, owner_global_ttl=owner_ttl),
        build_file_link_keyboard(updated, back_cb=back_cb, bot_username=uname),
        answer_text="👤 1×/User Trial Lock ON" if on else "👤 1×/User Trial Lock OFF",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:adel:"))
async def cb_file_cycle_auto_delete(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    fid, back_cb = _parse_fid_and_ctx(callback.data, "f:adel:")
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("Owner only.", show_alert=True)
        return
    new_ttl = await db.cycle_file_auto_delete_ttl(fid)
    updated = await db.get_file(fid)
    claims = await db.count_link_claims("file", fid)
    owner_ttl = int((user or {}).get("auto_delete_ttl") or 0)
    uname = bot_username or "KoshBox_Bot"
    if new_ttl > 0:
        ans = f"💣 Auto-Delete: {format_ttl_short(new_ttl)} (overrides global)"
    elif owner_ttl > 0:
        ans = f"💣 Per-link Off (uses global {format_ttl_short(owner_ttl)})"
    else:
        ans = "💣 Auto-Delete: Off"
    await safe_edit_or_send(
        callback,
        _render_file_link_card(updated, uname, unique_claims=claims, owner_global_ttl=owner_ttl),
        build_file_link_keyboard(updated, back_cb=back_cb, bot_username=uname),
        answer_text=ans,
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:prot:"))
async def cb_file_toggle_protect(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    fid, back_cb = _parse_fid_and_ctx(callback.data, "f:prot:")
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("Owner only.", show_alert=True)
        return
    on = await db.toggle_file_protect_share(fid)
    updated = await db.get_file(fid)
    claims = await db.count_link_claims("file", fid)
    owner_ttl = int((user or {}).get("auto_delete_ttl") or 0)
    uname = bot_username or "KoshBox_Bot"
    await safe_edit_or_send(
        callback,
        _render_file_link_card(updated, uname, unique_claims=claims, owner_global_ttl=owner_ttl),
        build_file_link_keyboard(updated, back_cb=back_cb, bot_username=uname),
        answer_text="🛡️ No-Forward ON" if on else "No-Forward OFF",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:exp:"))
async def cb_file_cycle_expiry(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    fid, back_cb = _parse_fid_and_ctx(callback.data, "f:exp:")
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("Owner only.", show_alert=True)
        return
    new_exp = await db.cycle_file_share_expiry(fid)
    updated = await db.get_file(fid)
    claims = await db.count_link_claims("file", fid)
    owner_ttl = int((user or {}).get("auto_delete_ttl") or 0)
    uname = bot_username or "KoshBox_Bot"
    await safe_edit_or_send(
        callback,
        _render_file_link_card(updated, uname, unique_claims=claims, owner_global_ttl=owner_ttl),
        build_file_link_keyboard(updated, back_cb=back_cb, bot_username=uname),
        answer_text=f"⏳ Link Expiry: {format_expiry_badge(new_exp)}",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:maxv:"))
async def cb_file_cycle_max_views(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    fid, back_cb = _parse_fid_and_ctx(callback.data, "f:maxv:")
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("Owner only.", show_alert=True)
        return
    lim = await db.cycle_file_share_max_views(fid)
    updated = await db.get_file(fid)
    claims = await db.count_link_claims("file", fid)
    owner_ttl = int((user or {}).get("auto_delete_ttl") or 0)
    uname = bot_username or "KoshBox_Bot"
    lbl = f"{lim} opens max" if lim > 0 else "Unlimited opens"
    await safe_edit_or_send(
        callback,
        _render_file_link_card(updated, uname, unique_claims=claims, owner_global_ttl=owner_ttl),
        build_file_link_keyboard(updated, back_cb=back_cb, bot_username=uname),
        answer_text=f"👁 Access Limit: {lbl}",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:pause:"))
async def cb_file_toggle_pause(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    fid, back_cb = _parse_fid_and_ctx(callback.data, "f:pause:")
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("Owner only.", show_alert=True)
        return
    paused = await db.toggle_file_share_paused(fid)
    updated = await db.get_file(fid)
    claims = await db.count_link_claims("file", fid)
    owner_ttl = int((user or {}).get("auto_delete_ttl") or 0)
    uname = bot_username or "KoshBox_Bot"
    await safe_edit_or_send(
        callback,
        _render_file_link_card(updated, uname, unique_claims=claims, owner_global_ttl=owner_ttl),
        build_file_link_keyboard(updated, back_cb=back_cb, bot_username=uname),
        answer_text="⏸ Link Paused" if paused else "▶️ Link Active",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:regen:"))
async def cb_file_regen_token(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    fid, back_cb = _parse_fid_and_ctx(callback.data, "f:regen:")
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("Owner only.", show_alert=True)
        return
    await db.regenerate_file_share_token(fid)
    updated = await db.get_file(fid)
    owner_ttl = int((user or {}).get("auto_delete_ttl") or 0)
    uname = bot_username or "KoshBox_Bot"
    await safe_edit_or_send(
        callback,
        _render_file_link_card(updated, uname, unique_claims=0, owner_global_ttl=owner_ttl),
        build_file_link_keyboard(updated, back_cb=back_cb, bot_username=uname),
        answer_text="🔄 New link generated (views & claims reset)",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:pin:"))
async def cb_file_pin_prompt(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not callback.from_user:
        return
    fid, back_cb = _parse_fid_and_ctx(callback.data, "f:pin:")
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("Owner only.", show_alert=True)
        return
    await state.set_state(FileEditStates.waiting_for_share_pin)
    await state.update_data(edit_file_id=fid, edit_back_cb=back_cb)
    status = "🟢 ON" if f.get("share_pin_hash") else "⚪ OFF"
    cancel_target = f"fsl:{fid}:{back_cb}" if back_cb else f"f:link:{fid}"
    await safe_edit_or_send(
        callback,
        f"🔑 <b>Link PIN / Password</b> · <i>{status}</i>\n"
        f"<blockquote>Send a PIN/password (up to 24 chars), or send <code>-</code> to remove PIN:</blockquote>",
        cancel_kb(cancel_target),
        also_ack=True,
    )


@router.message(FileEditStates.waiting_for_share_pin)
async def fsm_save_file_pin(
    message: Message,
    state: FSMContext,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    data = await state.get_data()
    fid = data.get("edit_file_id")
    back_cb = data.get("edit_back_cb")
    await state.clear()
    if not fid:
        return
    pin = None if raw == "-" else raw[:24]
    await db.set_file_share_pin(fid, pin)
    try:
        await message.delete()
    except Exception:
        pass
    updated = await db.get_file(fid)
    if not updated:
        return
    claims = await db.count_link_claims("file", fid)
    owner = await db.get_user(updated["uploader_user_id"])
    owner_ttl = int((owner or {}).get("auto_delete_ttl") or 0)
    uname = bot_username or "KoshBox_Bot"
    await message.answer(
        _render_file_link_card(updated, uname, unique_claims=claims, owner_global_ttl=owner_ttl),
        reply_markup=build_file_link_keyboard(updated, back_cb=back_cb, bot_username=uname),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("f:trash:") | F.data.startswith("f:del:"))
async def cb_trash_file(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("Owner only.", show_alert=True)
        return
    await db.trash_file(fid)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Undo (Restore)",
                    callback_data=f"f:restore:{fid}",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="🗑️ Delete Forever",
                    callback_data=f"f:purge:{fid}",
                    style="danger",
                ),
            ],
            [back_btn("br:all:def:0:0", "⬅️ My Files"), home_btn()],
        ]
    )
    await safe_edit_or_send(
        callback,
        f"🗑️ <b>Moved to Trash</b>\n<blockquote><b>{esc(f['title'])}</b> — tap Undo to restore anytime.</blockquote>",
        kb,
        answer_text="Moved to Trash",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:restore:"))
async def cb_restore_file(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("File not found.", show_alert=True)
        return
    await db.restore_file(fid)
    restored = await db.get_file(fid)
    uname = bot_username or "KoshBox_Bot"
    await safe_edit_or_send(
        callback,
        build_file_card_text(
            restored, uname, header_badge="♻️ <b>File Restored</b>"
        ),
        build_file_keyboard(restored, bot_username=uname),
        answer_text="Restored!",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:purge:"))
async def cb_purge_file(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("Owner only.", show_alert=True)
        return
    await safe_edit_or_send(
        callback,
        f"⚠️ <b>Delete Forever?</b>\n<blockquote>Permanently remove <b>{esc(f['title'])}</b> from your vault?</blockquote>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    back_btn(f"f:open:{fid}", "⬅️ Cancel"),
                    InlineKeyboardButton(
                        text="💥 Delete Forever",
                        callback_data=f"f:purge_do:{fid}",
                        style="danger",
                    ),
                ]
            ]
        ),
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:purge_do:"))
async def cb_purge_file_do(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or f["vault_id"] != user["vault_id"]:
        await callback.answer("Owner only.", show_alert=True)
        return
    await db.delete_file(fid)
    await safe_edit_or_send(
        callback,
        f"💥 <b>Permanently Deleted</b> · <i>{esc(f['title'])}</i>",
        InlineKeyboardMarkup(inline_keyboard=[[back_btn("trash:0", "⬅️ Trash"), home_btn()]]),
        answer_text="Deleted",
        also_ack=True,
    )


@router.callback_query(F.data.startswith("f:rename:"))
async def cb_rename_prompt(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    await state.set_state(FileEditStates.waiting_for_title)
    await state.update_data(edit_file_id=fid)
    await safe_edit_or_send(
        callback,
        f"✏️ <b>Rename File</b>\n<blockquote>Current: <code>{esc(f['title'])}</code></blockquote>\n<i>Send the new file name:</i>",
        cancel_kb(f"f:manage:{fid}"),
        also_ack=True,
    )


@router.message(FileEditStates.waiting_for_title)
async def fsm_save_title(
    message: Message,
    state: FSMContext,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    data = await state.get_data()
    fid = data.get("edit_file_id")
    await state.clear()
    if not fid:
        return
    new_title = raw[:120]
    if not new_title:
        return
    await db.update_file_title(fid, new_title)
    updated = await db.get_file(fid)
    if not updated:
        return
    uname = bot_username or "KoshBox_Bot"
    await message.answer(
        build_file_card_text(
            updated, uname, header_badge="✅ <b>File Renamed</b>"
        ),
        reply_markup=build_file_keyboard(updated, bot_username=uname),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("f:tags:"))
async def cb_tags_prompt(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    await state.set_state(FileEditStates.waiting_for_tags)
    await state.update_data(edit_file_id=fid)
    cur_tags = f"<code>{esc(f['tags'])}</code>" if f.get("tags") else "<i>None</i>"
    await safe_edit_or_send(
        callback,
        f"🏷️ <b>Edit Tags</b> · {cur_tags}\n<blockquote>Send hashtags (e.g. <code>#music #notes</code>) or <code>-</code> to clear:</blockquote>",
        cancel_kb(f"f:manage:{fid}"),
        also_ack=True,
    )


@router.message(FileEditStates.waiting_for_tags)
async def fsm_save_tags(
    message: Message,
    state: FSMContext,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    data = await state.get_data()
    fid = data.get("edit_file_id")
    await state.clear()
    if not fid:
        return
    if raw == "-":
        tags = ""
    else:
        tags = " ".join(
            t if t.startswith("#") else f"#{t}" for t in raw.split() if t.strip()
        )[:150]
    await db.update_file_tags(fid, tags)
    updated = await db.get_file(fid)
    if not updated:
        return
    uname = bot_username or "KoshBox_Bot"
    await message.answer(
        build_file_card_text(
            updated, uname, header_badge="✅ <b>Tags Saved</b>"
        ),
        reply_markup=build_file_keyboard(updated, bot_username=uname),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("f:cap:"))
async def cb_caption_prompt(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.from_user:
        return
    fid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    await state.set_state(FileEditStates.waiting_for_caption)
    await state.update_data(edit_file_id=fid)
    cur_cap = f"<i>{esc(f['caption'][:90])}</i>" if f.get("caption") else "<i>None</i>"
    await safe_edit_or_send(
        callback,
        f"💬 <b>Edit Caption</b> · {cur_cap}\n<blockquote>Send a new caption text or <code>-</code> to clear:</blockquote>",
        cancel_kb(f"f:manage:{fid}"),
        also_ack=True,
    )


@router.message(FileEditStates.waiting_for_caption)
async def fsm_save_caption(
    message: Message,
    state: FSMContext,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    data = await state.get_data()
    fid = data.get("edit_file_id")
    await state.clear()
    if not fid:
        return
    caption = "" if raw == "-" else raw[:900]
    await db.update_file_caption(fid, caption)
    updated = await db.get_file(fid)
    if not updated:
        return
    uname = bot_username or "KoshBox_Bot"
    await message.answer(
        build_file_card_text(
            updated, uname, header_badge="✅ <b>Caption Saved</b>"
        ),
        reply_markup=build_file_keyboard(updated, bot_username=uname),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("fmv:") | F.data.startswith("f:move:"))
async def cb_move_menu(
    callback: CallbackQuery,
    db: Database,
    state: Optional[FSMContext] = None,
) -> None:
    if not callback.from_user:
        return
    if state is not None:
        await state.clear()
    await _ensure_cb_user(callback, db)
    if callback.data.startswith("fmv:"):
        parts = callback.data.split(":", 2)
        fid = int(parts[1])
        back_cb = f"br:{parts[2]}" if len(parts) > 2 and parts[2] else None
    else:
        fid = int(callback.data.split(":")[2])
        back_cb = None

    rendered = await render_move_file_menu(callback.from_user.id, fid, db, back_cb=back_cb)
    if not rendered:
        await callback.answer("File not found.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1], also_ack=True)


@router.callback_query(F.data.startswith("f:setgrp:"))
async def cb_set_file_group(
    callback: CallbackQuery,
    db: Database,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    parts = callback.data.split(":")
    fid = int(parts[2])
    gid: Optional[int] = int(parts[3]) or None
    back_cb = f"br:{':'.join(parts[4:8])}" if len(parts) >= 8 else None

    user = await _ensure_cb_user(callback, db)
    f = await db.get_file(fid)
    if not f or not await db.can_vault_access_file(user["vault_id"], f):
        await callback.answer("File not found.", show_alert=True)
        return
    if gid is not None and not await db.is_vault_in_group(user["vault_id"], gid):
        await callback.answer("Invalid folder.", show_alert=True)
        return
    await db.move_file_to_group(fid, gid)
    updated = await db.get_file(fid)
    uname = bot_username or "KoshBox_Bot"
    await safe_edit_or_send(
        callback,
        build_file_card_text(updated, uname, header_badge="📁 <b>Moved to Folder</b>"),
        build_file_keyboard(updated, back_cb=back_cb, bot_username=uname),
        answer_text="📁 Moved!",
        also_ack=True,
    )
