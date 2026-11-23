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
from utils.formatters import DIVIDER, cancel_kb, esc, home_btn, safe_edit_or_send
from utils.states import AccountStates

router = Router(name="accounts")


async def render_accounts_panel(
    user_id: int, db: Database
) -> tuple[str, InlineKeyboardMarkup]:
    user = await db.get_user(user_id)
    linked = await db.get_linked_accounts(user["vault_id"])

    lines = [
        f"🔗 <b>Synced Telegram Accounts</b> ({len(linked)})",
        DIVIDER,
        "<i>All connected accounts share one unified vault, folders, and inline search in real time:</i>\n",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for acc in linked:
        uname = f"@{esc(acc['username'])}" if acc["username"] else "No username"
        you_tag = "  <i>(Current)</i>" if acc["user_id"] == user["user_id"] else ""
        lines.append(
            f"• <b>{esc(acc['full_name'] or 'Telegram User')}</b> ({uname}) — <code>{acc['user_id']}</code>{you_tag}"
        )
        if len(linked) > 1:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"🔓 Unlink {(acc['full_name'] or str(acc['user_id']))[:22]}",
                        callback_data=f"acc:unlink_ask:{acc['user_id']}",
                    )
                ]
            )

    buttons.append(
        [
            InlineKeyboardButton(text="➕ Link Another Account", callback_data="nav:link"),
            InlineKeyboardButton(text="🔑 Enter Code", callback_data="acc:enter_code"),
        ]
    )
    buttons.append([home_btn()])
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
        f"🔐 <b>Sync a Secondary Account</b>\n"
        f"{DIVIDER}\n"
        f"<b>Option 1 — One-Tap Link</b> (expires in {mins} min):\n"
        f"<code>{deep_link}</code>\n\n"
        f"<b>Option 2 — Verification Code:</b>\n"
        f"Open @{esc(me.username)} on your second account and send:\n"
        f"<code>/verify {req['code']}</code>\n\n"
        f"🛡️ <i>You will receive an Approve / Reject prompt here before any vault data is merged.</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Generate New Code", callback_data="nav:link"),
                InlineKeyboardButton(text="‹ Synced Accounts", callback_data="nav:accounts"),
            ],
            [home_btn()],
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


@router.callback_query(F.data == "acc:enter_code")
async def cb_enter_code_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AccountStates.waiting_for_verify_code)
    await safe_edit_or_send(
        callback,
        f"🔑 <b>Enter Sync Code</b>\n{DIVIDER}\n"
        f"Paste the 6-character code generated on your primary Telegram account:",
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
            "⚠️ <b>Invalid or Expired Code</b>\nGenerate a fresh code from your primary account using /link.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )
        return

    if req["owner_user_id"] == requester["user_id"]:
        await message.answer(
            "⚠️ <b>Same Account Detected</b>\nOpen the link or code from your <i>secondary</i> Telegram account.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )
        return

    if req["owner_vault_id"] == requester["vault_id"]:
        await message.answer(
            "✓ <b>Already Synced</b>\nThis account is already connected to that vault!",
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
            "⚠️ Could not process verification code. Please generate a new one with /link.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )
        return

    uname_display = f"@{esc(requester['username'])}" if requester["username"] else "No username"
    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✓ Approve & Sync",
                    callback_data=f"acc:approve:{req['code']}",
                ),
                InlineKeyboardButton(
                    text="✕ Reject",
                    callback_data=f"acc:reject:{req['code']}",
                ),
            ]
        ]
    )

    try:
        await bot.send_message(
            chat_id=req["owner_user_id"],
            text=(
                f"🛡️ <b>Vault Sync Request</b>\n"
                f"{DIVIDER}\n"
                f"👤 <b>Account:</b> {esc(requester['full_name'])} ({uname_display})\n"
                f"🆔 <b>User ID:</b> <code>{requester['user_id']}</code>\n\n"
                f"Approve to merge and sync your files & folders across both accounts."
            ),
            reply_markup=confirm_kb,
        )
    except Exception:
        await message.answer("⚠️ Failed to notify the primary account. Please try again.")
        return

    await message.answer(
        f"⏳ <b>Approval Requested</b>\n{DIVIDER}\n"
        f"Switch to your primary Telegram account and tap <b>✓ Approve & Sync</b> to complete setup."
    )


@router.message(Command("verify"))
async def cmd_verify(
    message: Message, command: CommandObject, state: FSMContext, db: Database, bot: Bot
) -> None:
    code = (command.args or "").strip()
    if not code:
        await state.set_state(AccountStates.waiting_for_verify_code)
        await message.answer(
            f"🔑 <b>Verify Sync Code</b>\n{DIVIDER}\n"
            f"Send the 6-character code generated on your primary account:",
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
        f"🎉 <b>Accounts Synced!</b>\n{DIVIDER}\n"
        f"<b>{esc(req['requester_name'])}</b> (<code>{req['requester_user_id']}</code>) "
        f"is now connected to your vault.",
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
                f"🎉 <b>Vault Sync Active!</b>\n{DIVIDER}\n"
                f"All your files and folders are now unified.\n"
                f"Type <code>@{esc(me.username)} </code> in any chat to access your vault!"
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔍 Open Inline Vault",
                            switch_inline_query_current_chat="",
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
        "✕ <b>Sync Request Declined</b>\nNo changes were made to your vault.",
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
async def cmd_accounts(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    text, kb = await render_accounts_panel(message.from_user.id, db)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "nav:accounts")
async def cb_nav_accounts(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await render_accounts_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("acc:unlink_ask:"))
async def cb_unlink_ask(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    target_id = int(callback.data.split(":")[2])
    target_user = await db.get_user(target_id)
    t_name = esc(target_user["full_name"]) if target_user else str(target_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔓 Yes, Unlink", callback_data=f"acc:unlink:{target_id}"),
                InlineKeyboardButton(text="‹ Cancel", callback_data="nav:accounts"),
            ]
        ]
    )
    await safe_edit_or_send(
        callback,
        f"⚠️ <b>Unlink {t_name}?</b>\n{DIVIDER}\n"
        f"Account <code>{target_id}</code> will be separated into a fresh empty vault. "
        f"Existing files stay safe in this vault.",
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
