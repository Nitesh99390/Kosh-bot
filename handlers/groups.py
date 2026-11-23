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
from utils.formatters import DIVIDER, cancel_kb, esc, home_btn, safe_edit_or_send
from utils.states import GroupStates

router = Router(name="groups")


async def render_groups_panel(
    user_id: int, db: Database
) -> tuple[str, InlineKeyboardMarkup]:
    user = await db.get_user(user_id)
    groups = await db.list_vault_groups(user["vault_id"])
    active_gid = user.get("active_group_id")

    personal = [g for g in groups if not g["is_shared"]]
    shared = [g for g in groups if g["is_shared"]]

    lines = [
        "📁 <b>Folders & Shared Groups</b>",
        DIVIDER,
    ]
    if not groups:
        lines.append(
            "<i>No folders or shared groups yet.</i>\n\n"
            "• <b>Personal Folder</b> — Organize private files inside your vault.\n"
            "• <b>Shared Group</b> — Create a collaborative space with an invite link so friends can search & share together."
        )
    else:
        lines.append(
            f"<code>{len(personal)} Personal · {len(shared)} Shared</code>\n"
            "<i>Tap any folder below to browse files, manage invites, or set auto-upload:</i>"
        )

    buttons: list[list[InlineKeyboardButton]] = []
    for g in groups:
        icon = "👥" if g["is_shared"] else "📁"
        active_mark = "  🎯" if g["id"] == active_gid else ""
        members_tag = f" · {g['member_count']}👤" if g["is_shared"] else ""
        label = f"{icon} {g['name']} ({g['file_count']}{members_tag}){active_mark}"
        buttons.append(
            [InlineKeyboardButton(text=label, callback_data=f"grp:open:{g['id']}")]
        )

    buttons.append(
        [
            InlineKeyboardButton(text="➕ New Folder", callback_data="grp:new:personal"),
            InlineKeyboardButton(text="👥 New Shared Group", callback_data="grp:new:shared"),
        ]
    )

    bottom_row: list[InlineKeyboardButton] = [
        InlineKeyboardButton(text="🔑 Join with Code", callback_data="grp:join"),
    ]
    if active_gid:
        bottom_row.append(
            InlineKeyboardButton(text="✕ Clear Target", callback_data="grp:clear_active")
        )
    buttons.append(bottom_row)
    buttons.append([home_btn()])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_single_group(
    user_id: int, gid: int, db: Database, bot: Bot
) -> Optional[tuple[str, InlineKeyboardMarkup]]:
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

    lines = [
        f"{icon} <b>{esc(grp['name'])}</b>",
        f"<code>{kind} · {grp['file_count']} file(s)"
        + (f" · {grp['member_count']} member(s)" if grp["is_shared"] else "")
        + "</code>",
        DIVIDER,
        f"🎯 <b>Auto-Upload Target:</b> {'Active (All new uploads land here)' if is_active else 'Inactive'}",
        f"🔍 <b>Inline Filter:</b> <code>@{esc(me.username)} g:{esc(grp['name'])}</code>",
    ]

    if grp["is_shared"] and grp.get("invite_code"):
        invite_url = f"https://t.me/{me.username}?start=join_{grp['invite_code']}"
        lines.append(
            f"\n🔑 <b>Invite Code:</b> <code>{grp['invite_code']}</code>\n"
            f"🔗 <b>Invite Link:</b>\n<code>{invite_url}</code>"
        )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"📦 Browse Files ({grp['file_count']})",
                callback_data=f"br:all:def:{gid}:0",
            ),
            InlineKeyboardButton(
                text="🔍 Search Inline",
                switch_inline_query_current_chat=f"g:{grp['name']} ",
            ),
        ]
    ]

    active_btn = (
        InlineKeyboardButton(text="✕ Disable Auto-Upload", callback_data=f"grp:unset:{gid}")
        if is_active
        else InlineKeyboardButton(text="🎯 Set as Auto-Upload Target", callback_data=f"grp:set:{gid}")
    )
    rows.append([active_btn])

    if is_owner:
        owner_row = [
            InlineKeyboardButton(text="✏️ Rename", callback_data=f"grp:rename:{gid}")
        ]
        if grp["is_shared"]:
            owner_row.append(
                InlineKeyboardButton(text="🔄 Reset Invite", callback_data=f"grp:regen:{gid}")
            )
        rows.append(owner_row)

    danger_label = "🗑️ Delete Folder" if (is_owner and not grp["is_shared"]) else (
        "🗑️ Delete Group" if is_owner else "🚪 Leave Group"
    )
    rows.append(
        [
            InlineKeyboardButton(text=danger_label, callback_data=f"grp:rm_confirm:{gid}"),
            InlineKeyboardButton(text="‹ All Folders", callback_data="grp:back"),
        ]
    )
    rows.append([home_btn()])

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
        f"📁 <b>Create Personal Folder</b>\n{DIVIDER}\n"
        f"Enter a clean name for your private folder (up to 40 characters):",
        cancel_kb("nav:groups"),
    )
    await callback.answer()


@router.callback_query(F.data == "grp:new:shared")
async def cb_new_shared(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GroupStates.waiting_for_shared_name)
    await safe_edit_or_send(
        callback,
        f"👥 <b>Create Shared Group</b>\n{DIVIDER}\n"
        f"Enter a name for your collaborative group.\n"
        f"<i>You'll receive a shareable invite link right after creation:</i>",
        cancel_kb("nav:groups"),
    )
    await callback.answer()


@router.callback_query(F.data == "grp:join")
async def cb_join_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GroupStates.waiting_for_invite_code)
    await safe_edit_or_send(
        callback,
        f"🔑 <b>Join Shared Group</b>\n{DIVIDER}\n"
        f"Paste the <b>8-character Invite Code</b> shared with you:",
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
            f"✓ <b>Personal Folder Created!</b>\n\n{rendered[0]}",
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
            f"🎉 <b>Shared Group Ready!</b>\n\n{rendered[0]}",
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
            "⚠️ <b>Invite Not Found</b>\nDouble-check the invite code or ask the owner for a fresh link.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🔑 Try Again", callback_data="grp:join"),
                        InlineKeyboardButton(text="‹ Folders", callback_data="nav:groups"),
                    ]
                ]
            ),
        )
        return
    rendered = await render_single_group(message.from_user.id, grp["id"], db, bot)
    if rendered:
        await message.answer(
            f"🎉 <b>Joined Shared Group!</b>\n\n{rendered[0]}",
            reply_markup=rendered[1],
            disable_web_page_preview=True,
        )


@router.callback_query(F.data.startswith("grp:open:"))
async def cb_open_group(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    rendered = await render_single_group(callback.from_user.id, gid, db, bot)
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
    await callback.answer("🎯 Set as Auto-Upload Target!")


@router.callback_query(F.data.startswith("grp:unset:"))
async def cb_unset_active_group(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    gid = int(callback.data.split(":")[2])
    await db.set_active_group(callback.from_user.id, None)
    rendered = await render_single_group(callback.from_user.id, gid, db, bot)
    if rendered:
        await safe_edit_or_send(callback, rendered[0], rendered[1])
    await callback.answer("Auto-upload target disabled")


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
        f"✏️ <b>Rename Folder / Group</b>\n{DIVIDER}\n"
        f"Current name: <b>{esc(grp['name'])}</b>\n\n"
        f"Send the new name below:",
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
            f"✓ <b>Folder Renamed!</b>\n\n{rendered[0]}",
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
    rendered = await render_single_group(callback.from_user.id, gid, db, bot)
    if rendered:
        await safe_edit_or_send(callback, rendered[0], rendered[1])
    await callback.answer("🔄 Invite link regenerated! Old link revoked.")


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
            f"⚠️ <b>Delete {esc(grp['name'])}?</b>\n{DIVIDER}\n"
            f"Files inside this folder will <b>not</b> be deleted — they will safely return to your Unassigned Vault."
        )
        confirm_lbl = "🗑️ Yes, Delete"
    else:
        prompt = (
            f"🚪 <b>Leave {esc(grp['name'])}?</b>\n{DIVIDER}\n"
            f"You will lose access to shared files in this group unless you rejoin via invite."
        )
        confirm_lbl = "🚪 Yes, Leave"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=confirm_lbl, callback_data=f"grp:rm:{gid}"),
                InlineKeyboardButton(text="‹ Cancel", callback_data=f"grp:open:{gid}"),
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
