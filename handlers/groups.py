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
    cancel_kb,
    copy_btn,
    esc,
    home_btn,
    safe_edit_or_send,
    toggle_label,
)
from utils.states import GroupStates

router = Router(name="groups")

GROUPS_PAGE_SIZE = 6


async def render_groups_panel(
    user_id: int, db: Database, page: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Clean Folders & Shared Groups Hub:
    - 2-column folder grid when multiple folders exist (prevents tall vertical scrolling)
    - Paginated above 6 folders
    - Progressive Disclosure: '+ Create / Join' opens a clean action modal instead of 3 extra rows
    """
    user = await db.get_user(user_id)
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
        breadcrumb("Vault", "Folders & Groups"),
        "📁 <b>Folders & Shared Groups</b>\n",
    ]
    if not groups:
        lines.append(
            "<blockquote>"
            "No folders or shared groups yet.\n"
            "• <b>Personal Folder</b> — Private organization\n"
            "• <b>Shared Group</b> — Collaborative invite space"
            "</blockquote>"
        )
    else:
        target_line = ""
        if active_gid:
            active_obj = next((x for x in groups if x["id"] == active_gid), None)
            if active_obj:
                target_line = f"\n🎯 <b>Auto-Upload:</b> {esc(active_obj['name'])}"
        lines.append(
            f"<blockquote>"
            f"📁 <b>{len(personal)}</b> Personal  ·  👥 <b>{len(shared)}</b> Shared"
            f"{target_line}"
            f"</blockquote>\n"
            f"<i>Tap a folder to open files or manage settings:</i>"
        )

    buttons: list[list[InlineKeyboardButton]] = []
    # 2-column grid for folders so even 6 folders take only 3 compact rows!
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
                InlineKeyboardButton(text="◀ Prev", callback_data=f"grp:page:{page - 1}")
            )
        nav_row.append(
            InlineKeyboardButton(text=f"{page + 1} / {total_pages}", callback_data="noop")
        )
        if page + 1 < total_pages:
            nav_row.append(
                InlineKeyboardButton(text="Next ▶", callback_data=f"grp:page:{page + 1}")
            )
        buttons.append(nav_row)

    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ Create or Join",
                callback_data="grp:create_menu",
                style="primary",
            ),
            back_btn("nav:home", "◀ Vault"),
        ]
    )

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def render_group_create_menu(has_active_target: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """Progressive disclosure drawer for creating a folder, shared group, or joining via code."""
    text = (
        f"{breadcrumb('Vault', 'Folders', 'Add')}\n"
        f"➕ <b>Create or Join Space</b>\n\n"
        f"<blockquote>"
        f"• <b>Personal Folder</b> — Organize private files\n"
        f"• <b>Shared Group</b> — Invite friends via link\n"
        f"• <b>Join Code</b> — Enter an 8-character invite code"
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
                text="👥 Shared Group",
                callback_data="grp:new:shared",
            ),
        ],
        [
            InlineKeyboardButton(text="🔑 Join with Code", callback_data="grp:join"),
        ],
    ]
    if has_active_target:
        rows[1].append(
            InlineKeyboardButton(text="🎯 Clear Target", callback_data="grp:clear_active")
        )
    rows.append([back_btn("nav:groups", "◀ Back to Folders")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def render_single_group(
    user_id: int, gid: int, db: Database, bot: Bot, manage_mode: bool = False
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    """
    Single Folder / Shared Group view with Progressive Disclosure:
    - Primary View: Browse Files, Inline Search, Copy Invite Link (if shared), Auto-Upload toggle, Manage
    - Manage Sub-View ('⚙️ Manage'): Rename, Reset Invite Link, Leave / Delete
    """
    user = await db.get_user(user_id)
    if not await db.is_vault_in_group(user["vault_id"], gid):
        return None
    grp = await db.get_group(gid)
    if not grp:
        return None

    me = await bot.get_me()
    is_active = user.get("active_group_id") == gid
    is_owner = grp["owner_vault_id"] == user["vault_id"]
    icon = "👥" if grp["is_shared"] else "📁"
    kind = "Shared Group" if grp["is_shared"] else "Personal Folder"

    trail = (
        breadcrumb("Vault", "Folders", esc(grp["name"]), "Manage")
        if manage_mode
        else breadcrumb("Vault", "Folders", esc(grp["name"]))
    )

    meta_line = f"{kind} · {grp['file_count']} file(s)"
    if grp["is_shared"]:
        meta_line += f" · {grp['member_count']} member(s)"

    info_lines = [
        f"🎯 <b>Auto-Upload:</b> {'Active (New uploads land here)' if is_active else 'Off'}",
        f"🔍 <b>Inline:</b> <code>@{esc(me.username)} g:{esc(grp['name'])}</code>",
    ]
    invite_url = ""
    if grp["is_shared"] and grp.get("invite_code"):
        invite_url = f"https://t.me/{me.username}?start=join_{grp['invite_code']}"
        info_lines.append(f"🔑 <b>Code:</b> <code>{grp['invite_code']}</code>")

    lines = [
        trail,
        f"{icon} <b>{esc(grp['name'])}</b>",
        f"<i>{meta_line}</i>\n",
        "<blockquote>" + "\n".join(info_lines) + "</blockquote>",
    ]
    if invite_url and manage_mode:
        lines.append(f"🔗 <b>Invite Link:</b>\n<code>{invite_url}</code>")

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

        if invite_url:
            m_rows.append([copy_btn("📋 Copy Invite Link", invite_url, style="primary")])

        danger_label = (
            "🗑️ Delete Folder"
            if (is_owner and not grp["is_shared"])
            else ("🗑️ Delete Group" if is_owner else "🚪 Leave Group")
        )
        m_rows.append(
            [
                InlineKeyboardButton(
                    text=danger_label,
                    callback_data=f"grp:rm_confirm:{gid}",
                    style="danger",
                )
            ]
        )
        m_rows.append([back_btn(f"grp:open:{gid}", "◀ Back to Folder")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=m_rows)

    # Clean Primary Folder View (max 3-4 rows!)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"📦 Open Files ({grp['file_count']})",
                callback_data=f"br:all:def:{gid}:0",
                style="primary",
            ),
            InlineKeyboardButton(
                text="🔍 Search Inline",
                switch_inline_query_current_chat=f"g:{grp['name']} ",
            ),
        ],
        [
            InlineKeyboardButton(
                text=toggle_label(is_active, "Auto-Upload"),
                callback_data=f"grp:unset:{gid}" if is_active else f"grp:set:{gid}",
            ),
            InlineKeyboardButton(
                text="⚙️ Manage",
                callback_data=f"grp:manage:{gid}",
            ),
        ],
    ]

    if invite_url:
        rows.append([copy_btn("📋 Copy Group Invite Link", invite_url)])

    rows.append(
        [
            back_btn("grp:back", "◀ Folders"),
            home_btn(),
        ]
    )

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
async def cb_nav_groups(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await render_groups_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("grp:page:"))
async def cb_groups_page(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    page = int(callback.data.split(":")[2])
    text, kb = await render_groups_panel(callback.from_user.id, db, page=page)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "grp:create_menu")
async def cb_group_create_menu(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    user = await db.get_user(callback.from_user.id)
    text, kb = render_group_create_menu(bool(user and user.get("active_group_id")))
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "grp:back")
async def cb_grp_back(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    text, kb = await render_groups_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "grp:clear_active")
async def cb_clear_active(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.set_active_group(callback.from_user.id, None)
    text, kb = await render_groups_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer("Auto-upload target cleared")


@router.callback_query(F.data == "grp:new:personal")
async def cb_new_personal(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GroupStates.waiting_for_personal_name)
    await safe_edit_or_send(
        callback,
        f"{breadcrumb('Vault', 'Folders', 'New Folder')}\n"
        f"📁 <b>Create Personal Folder</b>\n\n"
        f"<blockquote>Enter a clean name for your private folder (up to 40 chars):</blockquote>",
        cancel_kb("nav:groups"),
    )
    await callback.answer()


@router.callback_query(F.data == "grp:new:shared")
async def cb_new_shared(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GroupStates.waiting_for_shared_name)
    await safe_edit_or_send(
        callback,
        f"{breadcrumb('Vault', 'Folders', 'New Shared Group')}\n"
        f"👥 <b>Create Shared Group</b>\n\n"
        f"<blockquote>Enter a name for your collaborative group.\n"
        f"You'll receive a 1-tap invite link right after creation:</blockquote>",
        cancel_kb("nav:groups"),
    )
    await callback.answer()


@router.callback_query(F.data == "grp:join")
async def cb_join_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GroupStates.waiting_for_invite_code)
    await safe_edit_or_send(
        callback,
        f"{breadcrumb('Vault', 'Folders', 'Join Group')}\n"
        f"🔑 <b>Join Shared Group</b>\n\n"
        f"<blockquote>Paste the <b>8-character Invite Code</b> or invite link shared with you:</blockquote>",
        cancel_kb("nav:groups"),
    )
    await callback.answer()


@router.message(GroupStates.waiting_for_personal_name)
async def fsm_create_personal(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not message.from_user or not message.text:
        return
    name = message.text.strip()[:40]
    if not name:
        await message.answer("Please enter a valid folder name.", reply_markup=cancel_kb("nav:groups"))
        return
    user = await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    grp = await db.create_group(user["vault_id"], name, is_shared=False)
    await state.clear()
    rendered = await render_single_group(message.from_user.id, grp["id"], db, bot)
    if rendered:
        await message.answer(
            f"✅ <b>Folder Created</b>\n\n{rendered[0]}",
            reply_markup=rendered[1],
            disable_web_page_preview=True,
        )


@router.message(GroupStates.waiting_for_shared_name)
async def fsm_create_shared(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not message.from_user or not message.text:
        return
    name = message.text.strip()[:40]
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
    rendered = await render_single_group(message.from_user.id, grp["id"], db, bot)
    if rendered:
        await message.answer(
            f"🎉 <b>Shared Group Ready</b>\n\n{rendered[0]}",
            reply_markup=rendered[1],
            disable_web_page_preview=True,
        )


@router.message(GroupStates.waiting_for_invite_code)
async def fsm_join_group(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not message.from_user or not message.text:
        return
    raw = message.text.strip()
    if "join_" in raw:
        raw = raw.split("join_", 1)[1].split()[0]
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
            "⚠️ <b>Invite Not Found</b>\n"
            "<blockquote>Double-check the invite code or ask the group owner for an updated link.</blockquote>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔑 Try Again",
                            callback_data="grp:join",
                            style="primary",
                        ),
                        back_btn("nav:groups", "◀ Folders"),
                    ]
                ]
            ),
        )
        return
    rendered = await render_single_group(message.from_user.id, grp["id"], db, bot)
    if rendered:
        await message.answer(
            f"🎉 <b>Joined Shared Group</b>\n\n{rendered[0]}",
            reply_markup=rendered[1],
            disable_web_page_preview=True,
        )


@router.callback_query(F.data.startswith("grp:open:"))
async def cb_open_group(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    rendered = await render_single_group(callback.from_user.id, gid, db, bot, manage_mode=False)
    if not rendered:
        await callback.answer("Folder not found or access denied.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1])
    await callback.answer()


@router.callback_query(F.data.startswith("grp:manage:"))
async def cb_manage_group(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    rendered = await render_single_group(callback.from_user.id, gid, db, bot, manage_mode=True)
    if not rendered:
        await callback.answer("Folder not found or access denied.", show_alert=True)
        return
    await safe_edit_or_send(callback, rendered[0], rendered[1])
    await callback.answer()


@router.callback_query(F.data.startswith("grp:set:"))
async def cb_set_active_group(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    await db.set_active_group(callback.from_user.id, gid)
    rendered = await render_single_group(callback.from_user.id, gid, db, bot)
    if rendered:
        await safe_edit_or_send(callback, rendered[0], rendered[1])
    await callback.answer("🎯 Auto-Upload Target ON!")


@router.callback_query(F.data.startswith("grp:unset:"))
async def cb_unset_active_group(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    await db.set_active_group(callback.from_user.id, None)
    rendered = await render_single_group(callback.from_user.id, gid, db, bot)
    if rendered:
        await safe_edit_or_send(callback, rendered[0], rendered[1])
    await callback.answer("Auto-upload target OFF")


@router.callback_query(F.data.startswith("grp:rename:"))
async def cb_rename_group_prompt(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    grp = await db.get_group(gid)
    if not grp or grp["owner_vault_id"] != user["vault_id"]:
        await callback.answer("Only the folder owner can rename it.", show_alert=True)
        return
    await state.set_state(GroupStates.waiting_for_rename)
    await state.update_data(rename_group_id=gid)
    await safe_edit_or_send(
        callback,
        f"{breadcrumb('Vault', 'Folders', 'Rename')}\n"
        f"✏️ <b>Rename Folder / Group</b>\n\n"
        f"<blockquote>Current: <b>{esc(grp['name'])}</b></blockquote>\n\n"
        f"<i>Send the new name below:</i>",
        cancel_kb(f"grp:open:{gid}"),
    )
    await callback.answer()


@router.message(GroupStates.waiting_for_rename)
async def fsm_save_group_rename(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not message.from_user or not message.text:
        return
    data = await state.get_data()
    gid = data.get("rename_group_id")
    await state.clear()
    if not gid:
        return
    new_name = message.text.strip()[:40]
    if not new_name:
        return
    user = await db.get_user(message.from_user.id)
    grp = await db.get_group(gid)
    if not grp or grp["owner_vault_id"] != user["vault_id"]:
        return
    await db.rename_group(gid, new_name)
    rendered = await render_single_group(message.from_user.id, gid, db, bot)
    if rendered:
        await message.answer(
            f"✅ <b>Renamed!</b>\n\n{rendered[0]}",
            reply_markup=rendered[1],
            disable_web_page_preview=True,
        )


@router.callback_query(F.data.startswith("grp:regen:"))
async def cb_regen_invite(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    grp = await db.get_group(gid)
    if not grp or grp["owner_vault_id"] != user["vault_id"] or not grp["is_shared"]:
        await callback.answer("Only the group owner can reset the invite link.", show_alert=True)
        return
    await db.regenerate_group_invite(gid)
    rendered = await render_single_group(callback.from_user.id, gid, db, bot, manage_mode=True)
    if rendered:
        await safe_edit_or_send(callback, rendered[0], rendered[1])
    await callback.answer("🔄 Invite link regenerated!")


@router.callback_query(F.data.startswith("grp:rm_confirm:"))
async def cb_rm_group_confirm(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    grp = await db.get_group(gid)
    if not grp:
        await callback.answer("Folder not found.", show_alert=True)
        return
    is_owner = grp["owner_vault_id"] == user["vault_id"]
    if is_owner:
        prompt = (
            f"⚠️ <b>Delete {esc(grp['name'])}?</b>\n\n"
            f"<blockquote>Files inside this folder will <b>not</b> be deleted — they safely return to your Personal Vault.</blockquote>"
        )
        confirm_lbl = "💥 Yes, Delete"
    else:
        prompt = (
            f"🚪 <b>Leave {esc(grp['name'])}?</b>\n\n"
            f"<blockquote>You will lose access to shared files in this group until you rejoin with an invite.</blockquote>"
        )
        confirm_lbl = "🚪 Yes, Leave"

    # Safe action LEFT, Destructive action RIGHT (danger)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                back_btn(f"grp:manage:{gid}", "◀ Cancel"),
                InlineKeyboardButton(
                    text=confirm_lbl,
                    callback_data=f"grp:rm:{gid}",
                    style="danger",
                ),
            ]
        ]
    )
    await safe_edit_or_send(callback, prompt, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("grp:rm:"))
async def cb_rm_group(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    res = await db.leave_or_delete_group(user["vault_id"], gid)
    msg = "Folder deleted" if res == "deleted" else "Left shared group"
    text, kb = await render_groups_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer(msg)
