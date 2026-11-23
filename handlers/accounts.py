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
    Ultra-crisp Synced Accounts Panel:
    - Minimal header + clean blockquote device list
    """
    user = await db.get_user(user_id)
    linked = await db.get_linked_accounts(user["vault_id"])

    trail = (
        breadcrumb("Vault", "Accounts", "Unlink")
        if manage_unlink
        else breadcrumb("Vault", "Accounts")
    )

    acc_lines: list[str] = []
    for acc in linked:
        uname = f"@{esc(acc['username'])}" if acc["username"] else "no-user"
        you_tag = " · <i>You</i>" if acc["user_id"] == user["user_id"] else ""
        acc_lines.append(
            f"• <b>{esc(acc['full_name'] or 'User')}</b> ({uname}){you_tag}"
        )

    lines = [
        trail,
        f"🔗 <b>Synced Accounts</b> · <i>{len(linked)} linked</i>",
        "<blockquote>" + "\n".join(acc_lines) + "</blockquote>",
    ]

    buttons: list[list[InlineKeyboardButton]] = []
    if manage_unlink and len(linked) > 1:
        lines.append("<i>Select an account to detach:</i>")
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
        buttons.append([back_btn("nav:accounts", "◀ Accounts")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

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
            [InlineKeyboardButton(text="🔓 Unlink Device", callback_data="acc:manage")]
        )
    buttons.append([back_btn("nav:settings", "◀ Settings"), home_btn()])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _build_link_code_panel(
    user_id: int,
    username: str,
    full_name: str,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> tuple[str, InlineKeyboardMarkup]:
    user = await db.ensure_user(user_id, username, full_name)
    req = await db.create_link_request(
        owner_user_id=user["user_id"],
        owner_vault_id=user["vault_id"],
        ttl_seconds=config.link_code_ttl_seconds,
    )
    uname = bot_username or "KoshBox_Bot"
    deep_link = f"https://t.me/{uname}?start=link_{req['code']}"
    mins = max(1, config.link_code_ttl_seconds // 60)

    text = (
        f"{breadcrumb('Vault', 'Accounts', 'Link')}\n"
        f"🔐 <b>Sync Code</b> · <i>{mins}m</i>\n"
        f"<blockquote>Code: <code>{req['code']}</code>  ·  <code>/verify {req['code']}</code></blockquote>\n"
        f"<i>Open on your 2nd account, then approve here.</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                copy_btn("📋 Copy Link", deep_link, style="primary"),
                copy_btn("📋 Copy Code", req["code"]),
            ],
            [
                InlineKeyboardButton(text="🔄 Refresh", callback_data="nav:link"),
                back_btn("nav:accounts", "◀ Accounts"),
            ],
        ]
    )
    return text, kb


@router.message(Command("link"))
async def cmd_link(
    message: Message,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user:
        return
    text, kb = await _build_link_code_panel(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
        db,
        config,
        bot_username,
    )
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data == "nav:link")
async def cb_nav_link(
    callback: CallbackQuery,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    text, kb = await _build_link_code_panel(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
        db,
        config,
        bot_username,
    )
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data == "acc:manage")
async def cb_acc_manage(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    text, kb = await render_accounts_panel(callback.from_user.id, db, manage_unlink=True)
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data == "acc:enter_code")
async def cb_enter_code_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AccountStates.waiting_for_verify_code)
    await safe_edit_or_send(
        callback,
        f"🔑 <b>Enter Sync Code</b>\n<blockquote>Send the 6-character code from your primary account:</blockquote>",
        cancel_kb("nav:accounts"),
        also_ack=True,
    )


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
            "⚠️ <b>Expired or Invalid Code</b>\n<blockquote>Run /link on your primary account.</blockquote>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )
        return

    if req["owner_user_id"] == requester["user_id"]:
        await message.answer(
            "⚠️ <b>Same Account</b>\n<blockquote>Open this code on your 2nd Telegram account.</blockquote>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )
        return

    if req["owner_vault_id"] == requester["vault_id"]:
        await message.answer(
            "✅ <b>Already Synced</b>",
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
            "⚠️ Code unavailable. Run /link again.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )
        return

    uname_display = f"@{esc(requester['username'])}" if requester["username"] else "no-user"
    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✕ Reject",
                    callback_data=f"acc:reject:{req['code']}",
                    style="danger",
                ),
                InlineKeyboardButton(
                    text="✓ Approve",
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
                f"🛡️ <b>Sync Request</b>\n"
                f"<blockquote>👤 <b>{esc(requester['full_name'])}</b> ({uname_display})\n"
                f"🆔 <code>{requester['user_id']}</code></blockquote>"
            ),
            reply_markup=confirm_kb,
        )
    except Exception:
        await message.answer("⚠️ Could not reach primary account.")
        return

    await message.answer(
        "⏳ <b>Approval Sent</b>\n<blockquote>Tap <b>✓ Approve</b> on your primary account.</blockquote>"
    )


@router.message(Command("verify"))
async def cmd_verify(
    message: Message, command: CommandObject, state: FSMContext, db: Database, bot: Bot
) -> None:
    code = (command.args or "").strip()
    if not code:
        await state.set_state(AccountStates.waiting_for_verify_code)
        await message.answer(
            "🔑 <b>Verify Code</b>\n<blockquote>Send the 6-character sync code:</blockquote>",
            reply_markup=cancel_kb("nav:accounts"),
        )
        return
    await handle_verify_code(message, code, db, bot)


@router.callback_query(F.data.startswith("acc:approve:"))
async def cb_approve_link(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user or not callback.message:
        return
    code = callback.data.split(":")[2]
    req = await db.approve_link_request(code, callback.from_user.id)
    if not req:
        await safe_edit_or_send(callback, "⚠️ Expired or already handled.", also_ack=True)
        return

    await safe_edit_or_send(
        callback,
        f"🎉 <b>Synced</b> · <b>{esc(req['requester_name'])}</b>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔗 Accounts", callback_data="nav:accounts"),
                    home_btn(),
                ]
            ]
        ),
        answer_text="Synced!",
        also_ack=True,
    )
    uname = bot_username or "KoshBox_Bot"
    try:
        await bot.send_message(
            chat_id=req["requester_user_id"],
            text=(
                f"🎉 <b>Vault Synced</b>\n"
                f"<blockquote>Inline anywhere: <code>@{esc(uname)} </code></blockquote>"
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔍 Search Inline",
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


@router.callback_query(F.data.startswith("acc:reject:"))
async def cb_reject_link(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user or not callback.message:
        return
    code = callback.data.split(":")[2]
    req = await db.reject_link_request(code, callback.from_user.id)
    if not req:
        await safe_edit_or_send(callback, "⚠️ Already handled.", also_ack=True)
        return

    await safe_edit_or_send(
        callback,
        "✕ <b>Declined</b>",
        InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        answer_text="Declined",
        also_ack=True,
    )
    if req.get("requester_user_id"):
        try:
            await bot.send_message(
                chat_id=req["requester_user_id"],
                text="✕ Sync request declined.",
            )
        except Exception:
            pass


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
    await edit_or_send_banner(callback, "guide", text, kb, bot, db, config, also_ack=True)


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
                back_btn("acc:manage", "◀ Cancel"),
                InlineKeyboardButton(
                    text="🔓 Unlink",
                    callback_data=f"acc:unlink:{target_id}",
                    style="danger",
                ),
            ]
        ]
    )
    await safe_edit_or_send(
        callback,
        f"⚠️ <b>Unlink {t_name}?</b>\n<blockquote>Moves <code>{target_id}</code> to a separate vault.</blockquote>",
        kb,
        also_ack=True,
    )


@router.callback_query(F.data.startswith("acc:unlink:"))
async def cb_unlink(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    target_id = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    ok = await db.unlink_account(target_id, user["vault_id"])
    if not ok:
        await callback.answer("Cannot unlink last account.", show_alert=True)
        return
    text, kb = await render_accounts_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb, answer_text="Unlinked", also_ack=True)
