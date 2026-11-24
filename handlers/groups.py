from typing import Optional

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
    back_btn,
    breadcrumb,
    build_file_card_text,
    build_file_keyboard,
    cancel_kb,
    copy_btn,
    esc,
    home_btn,
    safe_edit_or_send,
    share_url_btn,
    toggle_label,
)
from utils.states import GroupStates

router = Router(name="groups")

GROUPS_PAGE_SIZE = 6


async def _ensure_cb_user(callback: CallbackQuery, db: Database) -> dict:
    assert callback.from_user is not None
    return await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )


async def render_groups_panel(
    user_id: int, db: Database, page: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Clean, Simple & Balanced Folders & Shared Groups Hub:
    - 2-column folder grid
    - Direct 1-tap New Folder & Shared Group buttons right at the bottom when reasonable
    """
    user = await db.get_user(user_id)
    if not user:
        user = await db.ensure_user(user_id)
    groups = await db.list_vault_groups(user["vault_id"])
    active_gid = user.get("active_group_id")

    personal = [g for g in groups if not g["is_shared"]]
    shared = [g for g in groups if g["is_shared"]]

    total = len(groups)
    total_pages = max(1, (total + GROUPS_PAGE_SIZE - 1) // GROUPS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * GROUPS_PAGE_SIZE
    slice_groups = groups[start : start + GROUPS_PAGE_SIZE]

    lines = [
        breadcrumb("🏠 Home", "📁 Folders & Groups"),
        "📁 <b>Folders &amp; Shared Groups</b>",
    ]
    if not groups:
        lines.append(
            "<blockquote>Organize your files into private <b>Folders</b> or create <b>Shared Groups</b> to collaborate with friends via invite code!</blockquote>"
        )
    else:
        target_line = ""
        if active_gid:
            active_obj = next((x for x in groups if x["id"] == active_gid), None)
            if active_obj:
                target_line = f"\n📥 <b>Upload Target:</b> {esc(active_obj['name'])}"
        lines.append(
            f"<blockquote>📁 <b>{len(personal)}</b> Personal  •  👥 <b>{len(shared)}</b> Shared{target_line}</blockquote>"
        )

    buttons: list[list[InlineKeyboardButton]] = []
    row_buf: list[InlineKeyboardButton] = []
    for g in slice_groups:
        icon = "👥" if g["is_shared"] else "📁"
        target_dot = " 🎯" if g["id"] == active_gid else ""
        label = f"{icon} {g['name'][:14]} ({g['file_count']}){target_dot}"
        row_buf.append(
            InlineKeyboardButton(text=label, callback_data=f"grp:open:{g['id']}")
        )
        if len(row_buf) == 2:
            buttons.append(row_buf)
            row_buf = []
    if row_buf:
        buttons.append(row_buf)

    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(text="⬅️ Prev", callback_data=f"grp:page:{page - 1}")
            )
        nav_row.append(
            InlineKeyboardButton(text=f"📄 {page + 1}/{total_pages}", callback_data="noop")
        )
        if page + 1 < total_pages:
            nav_row.append(
                InlineKeyboardButton(text="Next ➡️", callback_data=f"grp:page:{page + 1}")
            )
        buttons.append(nav_row)

    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ New Folder",
                callback_data="grp:new:personal",
                style="primary",
            ),
            InlineKeyboardButton(
                text="👥 Shared / Join",
                callback_data="grp:create_menu",
            ),
        ]
    )
    if active_gid:
        buttons.append(
            [
                InlineKeyboardButton(text="🎯 Reset Upload Target", callback_data="grp:clear_active"),
                home_btn("🏠 Home"),
            ]
        )
    else:
        buttons.append([home_btn("🏠 Back to Home")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def render_group_create_menu(has_active_target: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"{breadcrumb('🏠 Home', '📁 Folders', '➕ Create or Join')}\n"
        f"➕ <b>Create or Join Space</b>\n"
        f"<blockquote>"
        f"• <b>Private Folder</b> — Organize files inside your personal vault\n"
        f"• <b>Shared Group</b> — Invite others to view &amp; share files together\n"
        f"• <b>Join Code</b> — Enter an 8-letter invite code"
        f"</blockquote>"
    )
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="📁 New Folder",
                callback_data="grp:new:personal",
                style="primary",
            ),
            InlineKeyboardButton(
                text="👥 New Shared Group",
                callback_data="grp:new:shared",
            ),
        ],
        [
            InlineKeyboardButton(text="🔑 Join via Code", callback_data="grp:join"),
        ],
    ]
    if has_active_target:
        rows[1].append(
            InlineKeyboardButton(text="🎯 Reset Target", callback_data="grp:clear_active")
        )
    rows.append([back_btn("nav:groups", "⬅️ Folders"), home_btn()])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def render_single_group(
    user_id: int,
    gid: int,
    db: Database,
    bot: Optional[Bot] = None,
    manage_mode: bool = False,
    bot_username: str = "KoshBox_Bot",
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    """
    Clean Single Folder / Shared Group card:
    Includes 1-tap '🎒 Pack All into Link' and clear Auto-Save Target toggle.
    """
    user = await db.get_user(user_id)
    if not user:
        user = await db.ensure_user(user_id)
    if not await db.is_vault_in_group(user["vault_id"], gid):
        return None
    grp = await db.get_group(gid)
    if not grp:
        return None

    uname = bot_username or "KoshBox_Bot"
    is_active = user.get("active_group_id") == gid
    is_owner = grp["owner_vault_id"] == user["vault_id"]
    icon = "👥" if grp["is_shared"] else "📁"
    kind = "Shared Group" if grp["is_shared"] else "Private Folder"

    trail = (
        breadcrumb("🏠 Home", "📁 Folders", esc(grp["name"]), "⚙️ Manage")
        if manage_mode
        else breadcrumb("🏠 Home", "📁 Folders", esc(grp["name"]))
    )

    meta_line = f"{kind}  •  {grp['file_count']} files"
    if grp["is_shared"]:
        meta_line += f"  •  {grp['member_count']} member(s)"

    auto_save_note = "🟢 ON <i>(new uploads save here)</i>" if is_active else "⚪ OFF"
    info_lines = [
        f"📥 <b>Auto-Save Uploads:</b> {auto_save_note}",
        f"🔍 <b>Inline Filter:</b> <code>@{esc(uname)} g:{esc(grp['name'])}</code>",
    ]
    invite_url = ""
    if grp["is_shared"] and grp.get("invite_code"):
        invite_url = f"https://t.me/{uname}?start=join_{grp['invite_code']}"
        info_lines.append(f"🔑 <b>Invite Code:</b> <code>{grp['invite_code']}</code>")

    lines = [
        trail,
        f"{icon} <b>{esc(grp['name'])}</b>\n<i>{meta_line}</i>",
        "<blockquote>" + "\n".join(info_lines) + "</blockquote>",
    ]
    if invite_url and manage_mode:
        lines.append(f"🔗 <code>{invite_url}</code>")

    if manage_mode:
        m_rows: list[list[InlineKeyboardButton]] = []
        if is_owner:
            owner_row = [
                InlineKeyboardButton(text="✏️ Rename", callback_data=f"grp:rename:{gid}")
            ]
            if grp["is_shared"]:
                owner_row.append(
                    InlineKeyboardButton(text="🔄 Reset Invite", callback_data=f"grp:regen:{gid}")
                )
            m_rows.append(owner_row)

        if grp["file_count"] > 0:
            m_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🎒 Pack All ({grp['file_count']}) into Link",
                        callback_data=f"pk:from_grp:{gid}",
                        style="primary",
                    )
                ]
            )

        if invite_url:
            m_rows.append(
                [
                    copy_btn("📋 Copy Invite", invite_url),
                    share_url_btn("↗️ Share Invite", invite_url, f"Join {grp['name']}"),
                ]
            )

        danger_label = "🗑️ Delete Folder" if (is_owner and not grp["is_shared"]) else ("🗑️ Delete Group" if is_owner else "🚪 Leave Group")
        m_rows.append(
            [
                InlineKeyboardButton(
                    text=danger_label,
                    callback_data=f"grp:rm_confirm:{gid}",
                    style="danger",
                ),
                back_btn(f"grp:open:{gid}", "⬅️ Back"),
            ]
        )
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=m_rows)

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"📦 View Files ({grp['file_count']})",
                callback_data=f"br:all:def:{gid}:0",
                style="primary",
            ),
            InlineKeyboardButton(
                text="🔍 Search Folder",
                switch_inline_query_current_chat=f"g:{grp['name']} ",
            ),
        ],
        [
            InlineKeyboardButton(
                text=toggle_label(is_active, "Auto-Save"),
                callback_data=f"grp:unset:{gid}" if is_active else f"grp:set:{gid}",
                style="success" if is_active else None,
            ),
            InlineKeyboardButton(
                text="⚙️ Manage",
                callback_data=f"grp:manage:{gid}",
            ),
        ],
    ]

    if invite_url:
        rows.append(
            [
                copy_btn("📋 Copy Invite", invite_url),
                share_url_btn("↗️ Share Invite", invite_url, f"Join {grp['name']}"),
            ]
        )

    rows.append([back_btn("grp:back", "⬅️ Folders"), home_btn()])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("groups"))
async def cmd_groups(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    text, kb = await render_groups_panel(message.from_user.id, db)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "nav:groups")
async def cb_nav_groups(
    callback: CallbackQuery,
    db: Database,
    state: Optional[FSMContext] = None,
) -> None:
    if not callback.from_user:
        return
    if state is not None:
        await state.clear()
    await _ensure_cb_user(callback, db)
    text, kb = await render_groups_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data.startswith("grp:page:"))
async def cb_groups_page(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await _ensure_cb_user(callback, db)
    page = int(callback.data.split(":")[2])
    text, kb = await render_groups_panel(callback.from_user.id, db, page=page)
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data == "grp:create_menu")
async def cb_group_create_menu(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    user = await _ensure_cb_user(callback, db)
    text, kb = render_group_create_menu(bool(user and user.get("active_group_id")))
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data == "grp:back")
async def cb_grp_back(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await _ensure_cb_user(callback, db)
    text, kb = await render_groups_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data == "grp:clear_active")
async def cb_clear_active(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await _ensure_cb_user(callback, db)
    await db.set_active_group(callback.from_user.id, None)
    text, kb = await render_groups_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb, answer_text="Upload target reset", also_ack=True)


@router.callback_query(F.data == "grp:new:personal")
async def cb_new_personal(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GroupStates.waiting_for_personal_name)
    await state.update_data(move_file_id_after_create=None)
    await safe_edit_or_send(
        callback,
        "📁 <b>Create Private Folder</b>\n<blockquote>Send a name for your new folder (up to 40 characters):</blockquote>",
        cancel_kb("nav:groups"),
        also_ack=True,
    )


@router.callback_query(F.data.startswith("grp:new_for_file:"))
async def cb_new_personal_for_file(callback: CallbackQuery, state: FSMContext) -> None:
    """Creates a new folder right from 'Move File' and automatically moves the file into it!"""
    fid = int(callback.data.split(":")[2])
    await state.set_state(GroupStates.waiting_for_personal_name)
    await state.update_data(move_file_id_after_create=fid)
    await safe_edit_or_send(
        callback,
        "📁 <b>Create Folder &amp; Move File</b>\n<blockquote>Send a name for the new folder — your file will be moved into it automatically:</blockquote>",
        cancel_kb(f"f:move:{fid}"),
        also_ack=True,
    )


@router.callback_query(F.data == "grp:new:shared")
async def cb_new_shared(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GroupStates.waiting_for_shared_name)
    await safe_edit_or_send(
        callback,
        "👥 <b>Create Shared Group</b>\n<blockquote>Send a group name (an invite link &amp; code will be generated automatically):</blockquote>",
        cancel_kb("nav:groups"),
        also_ack=True,
    )


@router.callback_query(F.data == "grp:join")
async def cb_join_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GroupStates.waiting_for_invite_code)
    await safe_edit_or_send(
        callback,
        "🔑 <b>Join Shared Group</b>\n<blockquote>Send the 8-character invite code or paste the invite link:</blockquote>",
        cancel_kb("nav:groups"),
        also_ack=True,
    )


@router.message(GroupStates.waiting_for_personal_name, F.text, ~F.text.startswith('/'))
async def fsm_create_personal(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    name = raw[:40]
    if not name:
        await message.answer("Please enter a valid folder name.", reply_markup=cancel_kb("nav:groups"))
        return
    data = await state.get_data()
    move_fid: Optional[int] = data.get("move_file_id_after_create")
    user = await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    grp = await db.create_group(user["vault_id"], name, is_shared=False)
    await state.clear()

    if move_fid:
        f = await db.get_file(move_fid)
        if f and f["vault_id"] == user["vault_id"]:
            await db.move_file_to_group(move_fid, grp["id"])
            updated_f = await db.get_file(move_fid)
            uname = bot_username or "KoshBox_Bot"
            await message.answer(
                build_file_card_text(
                    updated_f,
                    uname,
                    header_badge=f"✅ <b>Created '{esc(grp['name'])}' &amp; Moved File</b>",
                ),
                reply_markup=build_file_keyboard(updated_f, bot_username=uname),
                disable_web_page_preview=True,
            )
            return

    rendered = await render_single_group(
        message.from_user.id, grp["id"], db, bot, bot_username=bot_username
    )
    if rendered:
        await message.answer(
            f"✅ <b>Folder Created</b>\n{rendered[0]}",
            reply_markup=rendered[1],
            disable_web_page_preview=True,
        )


@router.message(GroupStates.waiting_for_shared_name, F.text, ~F.text.startswith('/'))
async def fsm_create_shared(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    name = raw[:40]
    if not name:
        await message.answer("Please enter a valid group name.", reply_markup=cancel_kb("nav:groups"))
        return
    user = await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    grp = await db.create_group(user["vault_id"], name, is_shared=True)
    await state.clear()
    rendered = await render_single_group(
        message.from_user.id, grp["id"], db, bot, bot_username=bot_username
    )
    if rendered:
        await message.answer(
            f"🎉 <b>Shared Group Ready</b>\n{rendered[0]}",
            reply_markup=rendered[1],
            disable_web_page_preview=True,
        )


@router.message(GroupStates.waiting_for_invite_code, F.text, ~F.text.startswith('/'))
async def fsm_join_group(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    if "join_" in raw:
        tail_parts = raw.split("join_", 1)[1].split()
        raw = tail_parts[0] if tail_parts else ""
    code = raw.upper()
    user = await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    grp = await db.join_shared_group(user["vault_id"], code)
    await state.clear()
    if not grp:
        await message.answer(
            "⚠️ <b>Invalid Invite Code</b>\n<blockquote>Please check the code or ask the owner for a fresh invite link.</blockquote>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔑 Try Again",
                            callback_data="grp:join",
                            style="primary",
                        ),
                        back_btn("nav:groups", "⬅️ Folders"),
                    ]
                ]
            ),
        )
        return
    rendered = await render_single_group(
        message.from_user.id, grp["id"], db, bot, bot_username=bot_username
    )
    if rendered:
        await message.answer(
            f"🎉 <b>Joined Group</b>\n{rendered[0]}",
            reply_markup=rendered[1],
            disable_web_page_preview=True,
        )


@router.callback_query(F.data.startswith("grp:open:"))
async def cb_open_group(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
    state: Optional[FSMContext] = None,
) -> None:
    if not callback.from_user:
        return
    if state is not None:
        await state.clear()
    await _ensure_cb_user(callback, db)
    gid = int(callback.data.split(":")[2])
    rendered = await render_single_group(
        callback.from_user.id,
        gid,
        db,
        bot,
        manage_mode=False,
        bot_username=bot_username,
    )
    if not rendered:
        await callback.answer("Folder not found.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1], also_ack=True)


@router.callback_query(F.data.startswith("grp:manage:"))
async def cb_manage_group(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
    state: Optional[FSMContext] = None,
) -> None:
    if not callback.from_user:
        return
    if state is not None:
        await state.clear()
    await _ensure_cb_user(callback, db)
    gid = int(callback.data.split(":")[2])
    rendered = await render_single_group(
        callback.from_user.id,
        gid,
        db,
        bot,
        manage_mode=True,
        bot_username=bot_username,
    )
    if not rendered:
        await callback.answer("Folder not found.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1], also_ack=True)


@router.callback_query(F.data.startswith("grp:set:"))
async def cb_set_active_group(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    user = await _ensure_cb_user(callback, db)
    gid = int(callback.data.split(":")[2])
    if not await db.is_vault_in_group(user["vault_id"], gid):
        await callback.answer("Folder not found.", show_alert=True)
        return
    await db.set_active_group(callback.from_user.id, gid)
    rendered = await render_single_group(
        callback.from_user.id, gid, db, bot, bot_username=bot_username
    )
    if rendered:
        await safe_edit_or_send(
            callback,
            rendered[0],
            rendered[1],
            answer_text="🎯 Auto-Save Target ON",
            also_ack=True,
        )
    else:
        await callback.answer("🎯 Auto-Save Target ON")


@router.callback_query(F.data.startswith("grp:unset:"))
async def cb_unset_active_group(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    await _ensure_cb_user(callback, db)
    gid = int(callback.data.split(":")[2])
    await db.set_active_group(callback.from_user.id, None)
    rendered = await render_single_group(
        callback.from_user.id, gid, db, bot, bot_username=bot_username
    )
    if rendered:
        await safe_edit_or_send(
            callback, rendered[0], rendered[1], answer_text="Auto-Save Target OFF", also_ack=True
        )
    else:
        await callback.answer("Auto-Save Target OFF")


@router.callback_query(F.data.startswith("grp:rename:"))
async def cb_rename_group_prompt(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    grp = await db.get_group(gid)
    if not grp or grp["owner_vault_id"] != user["vault_id"]:
        await callback.answer("Owner only.", show_alert=True)
        return
    await state.set_state(GroupStates.waiting_for_rename)
    await state.update_data(rename_group_id=gid)
    await safe_edit_or_send(
        callback,
        f"✏️ <b>Rename Folder</b> · <code>{esc(grp['name'])}</code>\n<blockquote>Send the new name (up to 40 chars):</blockquote>",
        cancel_kb(f"grp:manage:{gid}"),
        also_ack=True,
    )


@router.message(GroupStates.waiting_for_rename, F.text, ~F.text.startswith('/'))
async def fsm_save_group_rename(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user or not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    data = await state.get_data()
    gid = data.get("rename_group_id")
    await state.clear()
    if not gid:
        return
    new_name = raw[:40]
    if not new_name:
        return
    user = await db.get_user(message.from_user.id)
    grp = await db.get_group(gid)
    if not grp or not user or grp["owner_vault_id"] != user["vault_id"]:
        return
    await db.rename_group(gid, new_name)
    rendered = await render_single_group(
        message.from_user.id, gid, db, bot, bot_username=bot_username
    )
    if rendered:
        await message.answer(
            f"✅ <b>Renamed</b>\n{rendered[0]}",
            reply_markup=rendered[1],
            disable_web_page_preview=True,
        )


@router.callback_query(F.data.startswith("grp:regen:"))
async def cb_regen_invite(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    grp = await db.get_group(gid)
    if not grp or grp["owner_vault_id"] != user["vault_id"] or not grp["is_shared"]:
        await callback.answer("Owner only.", show_alert=True)
        return
    await db.regenerate_group_invite(gid)
    rendered = await render_single_group(
        callback.from_user.id,
        gid,
        db,
        bot,
        manage_mode=True,
        bot_username=bot_username,
    )
    if rendered:
        await safe_edit_or_send(
            callback,
            rendered[0],
            rendered[1],
            answer_text="🔄 Invite link regenerated",
            also_ack=True,
        )
    else:
        await callback.answer("🔄 Invite link regenerated")


@router.callback_query(F.data.startswith("grp:rm_confirm:"))
async def cb_rm_group_confirm(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    grp = await db.get_group(gid)
    if not grp or not await db.is_vault_in_group(user["vault_id"], gid):
        await callback.answer("Folder not found.", show_alert=True)
        return
    is_owner = grp["owner_vault_id"] == user["vault_id"]
    if is_owner:
        prompt = (
            f"⚠️ <b>Delete '{esc(grp['name'])}'?</b>\n"
            f"<blockquote>Your files inside this folder will NOT be deleted — they stay safe in your Personal Vault.</blockquote>"
        )
        confirm_lbl = "💥 Delete"
    else:
        prompt = f"🚪 <b>Leave '{esc(grp['name'])}'?</b>"
        confirm_lbl = "🚪 Leave"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                back_btn(f"grp:manage:{gid}", "⬅️ Cancel"),
                InlineKeyboardButton(
                    text=confirm_lbl,
                    callback_data=f"grp:rm:{gid}",
                    style="danger",
                ),
            ]
        ]
    )
    await safe_edit_or_send(callback, prompt, kb, also_ack=True)


@router.callback_query(F.data.startswith("grp:rm:"))
async def cb_rm_group(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    user = await _ensure_cb_user(callback, db)
    res = await db.leave_or_delete_group(user["vault_id"], gid)
    msg = "Folder deleted" if res == "deleted" else "Left group"
    text, kb = await render_groups_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb, answer_text=msg, also_ack=True)
