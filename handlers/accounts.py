import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Config
from database import Database
from utils.banners import edit_or_send_banner, send_banner_message
from utils.formatters import (
    back_btn,
    breadcrumb,
    cancel_kb,
    copy_btn,
    esc,
    home_btn,
    safe_edit_or_send,
)
from utils.states import AccountStates

router = Router(name="accounts")


async def render_accounts_panel(
    user_id: int, db: Database, manage_unlink: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Clean Synced Accounts View:
    - Primary screen lists connected accounts cleanly inside a blockquote with just 2 rows of buttons.
    - Unlink actions only appear inside '🔓 Manage Linked' sub-view (Progressive Disclosure).
    """
    user = await db.get_user(user_id)
    linked = await db.get_linked_accounts(user["vault_id"])

    trail = (
        breadcrumb("Vault", "Settings", "Accounts", "Unlink")
        if manage_unlink
        else breadcrumb("Vault", "Settings", "Synced Accounts")
    )

    acc_lines: list[str] = []
    for acc in linked:
        uname = f"@{esc(acc['username'])}" if acc["username"] else "No username"
        you_tag = " · <i>This device</i>" if acc["user_id"] == user["user_id"] else ""
        acc_lines.append(
            f"• <b>{esc(acc['full_name'] or 'Telegram User')}</b> ({uname}){you_tag}"
        )

    lines = [
        trail,
        f"🔗 <b>Synced Accounts</b> · <i>{len(linked)} connected</i>\n",
        "<blockquote>" + "\n".join(acc_lines) + "</blockquote>\n",
    ]

    buttons: list[list[InlineKeyboardButton]] = []
    if manage_unlink and len(linked) > 1:
        lines.append("<i>Select an account below to detach it into a separate vault:</i>")
        for acc in linked:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"🔓 Unlink {(acc['full_name'] or str(acc['user_id']))[:22]}",
                        callback_data=f"acc:unlink_ask:{acc['user_id']}",
                        style="danger",
                    )
                ]
            )
        buttons.append([back_btn("nav:accounts", "◀ Back to Accounts")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

    lines.append("<i>Connected accounts share your vault, folders, and inline search in real time.</i>")

    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ Link Account",
                callback_data="nav:link",
                style="primary",
            ),
            InlineKeyboardButton(
                text="🔑 Enter Code",
                callback_data="acc:enter_code",
            ),
        ]
    )
    if len(linked) > 1:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🔓 Manage / Unlink",
                    callback_data="acc:manage",
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


async def _build_link_code_panel(
    user_id: int, username: str, full_name: str, db: Database, bot: Bot, config: Config
) -> tuple[str, InlineKeyboardMarkup]:
    user = await db.ensure_user(user_id, username, full_name)
    req = await db.create_link_request(
        owner_user_id=user["user_id"],
        owner_vault_id=user["vault_id"],
        ttl_seconds=config.link_code_ttl_seconds,
    )
    me = await bot.get_me()
    deep_link = f"https://t.me/{me.username}?start=link_{req['code']}"
    mins = max(1, config.link_code_ttl_seconds // 60)

    text = (
        f"{breadcrumb('Vault', 'Accounts', 'Link New')}\n"
        f"🔐 <b>Link Secondary Account</b>\n\n"
        f"<blockquote>"
        f"<b>Sync Code:</b> <code>{req['code']}</code> <i>(valid {mins}m)</i>\n"
        f"<b>Command:</b> <code>/verify {req['code']}</code>"
        f"</blockquote>\n\n"
        f"<i>Copy the link or code below, open it on your second account, then approve the request here.</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                copy_btn("📋 Copy Sync Link", deep_link, style="primary"),
                copy_btn("📋 Copy Code", req["code"]),
            ],
            [
                InlineKeyboardButton(text="🔄 New Code", callback_data="nav:link"),
                back_btn("nav:accounts", "◀ Accounts"),
            ],
        ]
    )
    return text, kb


@router.message(Command("link"))
async def cmd_link(message: Message, db: Database, bot: Bot, config: Config) -> None:
    if not message.from_user:
        return
    text, kb = await _build_link_code_panel(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
        db,
        bot,
        config,
    )
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data == "nav:link")
async def cb_nav_link(
    callback: CallbackQuery, db: Database, bot: Bot, config: Config
) -> None:
    if not callback.from_user:
        return
    text, kb = await _build_link_code_panel(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
        db,
        bot,
        config,
    )
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "acc:manage")
async def cb_acc_manage(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    text, kb = await render_accounts_panel(callback.from_user.id, db, manage_unlink=True)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "acc:enter_code")
async def cb_enter_code_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AccountStates.waiting_for_verify_code)
    await safe_edit_or_send(
        callback,
        f"{breadcrumb('Vault', 'Accounts', 'Enter Code')}\n"
        f"🔑 <b>Enter Sync Code</b>\n\n"
        f"<blockquote>Paste the 6-character code generated on your primary Telegram account:</blockquote>",
        cancel_kb("nav:accounts"),
    )
    await callback.answer()


@router.message(AccountStates.waiting_for_verify_code)
async def fsm_verify_code(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not message.text:
        return
    raw = message.text.strip()
    if raw.lower().startswith("/verify"):
        parts = raw.split(maxsplit=1)
        raw = parts[1] if len(parts) > 1 else ""
    if "link_" in raw:
        raw = raw.split("link_", 1)[1].split()[0]
    await state.clear()
    await handle_verify_code(message, raw, db, bot)


async def handle_verify_code(
    message: Message, code: str, db: Database, bot: Bot
) -> None:
    if not message.from_user:
        return
    requester = await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    req = await db.get_link_request(code.strip())
    now = int(time.time())

    if not req or req["status"] != "pending" or req["expires_at"] < now:
        await message.answer(
            "⚠️ <b>Invalid or Expired Code</b>\n"
            "<blockquote>Generate a fresh code from your primary account using /link.</blockquote>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )
        return

    if req["owner_user_id"] == requester["user_id"]:
        await message.answer(
            "⚠️ <b>Same Account Detected</b>\n"
            "<blockquote>Open the link or code from your <i>secondary</i> Telegram account.</blockquote>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )
        return

    if req["owner_vault_id"] == requester["vault_id"]:
        await message.answer(
            "✅ <b>Already Synced</b>\n"
            "<blockquote>This account is already connected to that vault.</blockquote>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )
        return

    ok = await db.attach_requester_to_link(
        code=req["code"],
        requester_user_id=requester["user_id"],
        requester_name=requester["full_name"],
        requester_username=requester["username"],
    )
    if not ok:
        await message.answer(
            "⚠️ Could not process verification code. Generate a new one with /link.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )
        return

    uname_display = f"@{esc(requester['username'])}" if requester["username"] else "No username"
    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✕ Reject",
                    callback_data=f"acc:reject:{req['code']}",
                    style="danger",
                ),
                InlineKeyboardButton(
                    text="✓ Approve & Sync",
                    callback_data=f"acc:approve:{req['code']}",
                    style="success",
                ),
            ]
        ]
    )

    try:
        await bot.send_message(
            chat_id=req["owner_user_id"],
            text=(
                f"🛡️ <b>Vault Sync Request</b>\n\n"
                f"<blockquote>"
                f"👤 <b>Account:</b> {esc(requester['full_name'])} ({uname_display})\n"
                f"🆔 <b>User ID:</b> <code>{requester['user_id']}</code>"
                f"</blockquote>\n\n"
                f"<i>Approve to merge and sync your files & folders across both accounts.</i>"
            ),
            reply_markup=confirm_kb,
        )
    except Exception:
        await message.answer("⚠️ Failed to notify the primary account. Please try again.")
        return

    await message.answer(
        "⏳ <b>Approval Requested</b>\n\n"
        "<blockquote>Switch to your primary Telegram account and tap <b>✓ Approve & Sync</b>.</blockquote>"
    )


@router.message(Command("verify"))
async def cmd_verify(
    message: Message, command: CommandObject, state: FSMContext, db: Database, bot: Bot
) -> None:
    code = (command.args or "").strip()
    if not code:
        await state.set_state(AccountStates.waiting_for_verify_code)
        await message.answer(
            f"{breadcrumb('Vault', 'Accounts', 'Verify')}\n"
            f"🔑 <b>Verify Sync Code</b>\n\n"
            f"<blockquote>Send the 6-character code generated on your primary account:</blockquote>",
            reply_markup=cancel_kb("nav:accounts"),
        )
        return
    await handle_verify_code(message, code, db, bot)


@router.callback_query(F.data.startswith("acc:approve:"))
async def cb_approve_link(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user or not callback.message:
        return
    code = callback.data.split(":")[2]
    req = await db.approve_link_request(code, callback.from_user.id)
    if not req:
        await safe_edit_or_send(callback, "⚠️ Request expired or already processed.")
        await callback.answer()
        return

    await safe_edit_or_send(
        callback,
        f"🎉 <b>Accounts Synced</b>\n\n"
        f"<blockquote><b>{esc(req['requester_name'])}</b> (<code>{req['requester_user_id']}</code>) "
        f"is now connected to your vault.</blockquote>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔗 View Accounts", callback_data="nav:accounts"),
                    home_btn(),
                ]
            ]
        ),
    )
    me = await bot.get_me()
    try:
        await bot.send_message(
            chat_id=req["requester_user_id"],
            text=(
                f"🎉 <b>Vault Sync Active</b>\n\n"
                f"<blockquote>All your files and folders are now unified.</blockquote>\n\n"
                f"<i>Inline anywhere:</i> <code>@{esc(me.username)} </code>"
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔍 Open Inline Vault",
                            switch_inline_query_current_chat="",
                            style="primary",
                        ),
                        home_btn(),
                    ]
                ]
            ),
        )
    except Exception:
        pass
    await callback.answer("Accounts synced!")


@router.callback_query(F.data.startswith("acc:reject:"))
async def cb_reject_link(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user or not callback.message:
        return
    code = callback.data.split(":")[2]
    req = await db.reject_link_request(code, callback.from_user.id)
    if not req:
        await safe_edit_or_send(callback, "⚠️ Request already processed.")
        await callback.answer()
        return

    await safe_edit_or_send(
        callback,
        "✕ <b>Sync Request Declined</b>\n<i>No changes were made to your vault.</i>",
        InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )
    if req.get("requester_user_id"):
        try:
            await bot.send_message(
                chat_id=req["requester_user_id"],
                text="✕ Your vault sync request was declined by the primary account.",
            )
        except Exception:
            pass
    await callback.answer("Declined")


@router.message(Command("accounts"))
async def cmd_accounts(
    message: Message, db: Database, bot: Bot, config: Config
) -> None:
    if not message.from_user:
        return
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    text, kb = await render_accounts_panel(message.from_user.id, db)
    await send_banner_message(message, "guide", text, kb, bot, db, config)


@router.callback_query(F.data == "nav:accounts")
async def cb_nav_accounts(
    callback: CallbackQuery, db: Database, bot: Bot, config: Config
) -> None:
    if not callback.from_user:
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await render_accounts_panel(callback.from_user.id, db)
    await edit_or_send_banner(callback, "guide", text, kb, bot, db, config)
    await callback.answer()


@router.callback_query(F.data.startswith("acc:unlink_ask:"))
async def cb_unlink_ask(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    target_id = int(callback.data.split(":")[2])
    target_user = await db.get_user(target_id)
    t_name = esc(target_user["full_name"]) if target_user else str(target_id)
    # Safe action LEFT, Destructive action RIGHT (danger)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                back_btn("acc:manage", "◀ Cancel"),
                InlineKeyboardButton(
                    text="🔓 Yes, Unlink",
                    callback_data=f"acc:unlink:{target_id}",
                    style="danger",
                ),
            ]
        ]
    )
    await safe_edit_or_send(
        callback,
        f"⚠️ <b>Unlink {t_name}?</b>\n\n"
        f"<blockquote>Account <code>{target_id}</code> will be moved into a fresh empty vault. "
        f"Existing files stay safe in this vault.</blockquote>",
        kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("acc:unlink:"))
async def cb_unlink(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    target_id = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    ok = await db.unlink_account(target_id, user["vault_id"])
    if not ok:
        await callback.answer("Cannot unlink the only remaining account.", show_alert=True)
        return
    text, kb = await render_accounts_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer("Account unlinked")
