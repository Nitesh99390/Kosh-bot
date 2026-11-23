import asyncio
import os
import shutil
import tempfile

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Config
from database import Database
from utils.formatters import DIVIDER, cancel_kb, format_size, home_btn, safe_edit_or_send
from utils.states import AdminStates

router = Router(name="admin")


def _is_admin(user_id: int, config: Config) -> bool:
    return user_id in config.admin_ids


async def _render_admin_panel(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    stats = await db.get_global_stats()
    text = (
        f"🛠️ <b>KoshBox Admin Console</b>\n"
        f"{DIVIDER}\n"
        f"👤 <b>Total Users:</b> {stats['users']}  <i>({stats['active_24h']} active in 24h)</i>\n"
        f"🔐 <b>Unique Vaults:</b> {stats['vaults']}\n"
        f"📦 <b>Active Files:</b> {stats['files']}  <i>({stats['trashed']} in trash)</i>\n"
        f"📁 <b>Folders & Groups:</b> {stats['groups']}\n"
        f"💾 <b>Total Archived:</b> {format_size(stats['total_size'])}\n"
        f"{DIVIDER}\n"
        f"<i>Select an administrative action below:</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💾 Backup Database", callback_data="adm:backup"),
                InlineKeyboardButton(text="📣 Broadcast", callback_data="adm:broadcast"),
            ],
            [
                InlineKeyboardButton(text="🔄 Refresh Metrics", callback_data="adm:refresh"),
                home_btn(),
            ],
        ]
    )
    return text, kb


@router.message(Command("admin"))
async def cmd_admin(message: Message, db: Database, config: Config) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, config):
        return
    text, kb = await _render_admin_panel(db)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "adm:refresh")
async def cb_admin_refresh(
    callback: CallbackQuery, db: Database, config: Config
) -> None:
    if not callback.from_user or not _is_admin(callback.from_user.id, config):
        await callback.answer("Admin access required.", show_alert=True)
        return
    text, kb = await _render_admin_panel(db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer("Metrics updated")


async def _send_db_backup(chat_id: int, bot: Bot, db: Database) -> bool:
    if not os.path.exists(db.db_path):
        return False
    with tempfile.TemporaryDirectory() as tmpdir:
        snap_path = os.path.join(tmpdir, "koshbox_backup.db")
        if db.conn:
            await db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        shutil.copy2(db.db_path, snap_path)
        await bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(snap_path),
            caption="💾 <b>KoshBox SQLite Snapshot</b>",
        )
    return True


@router.message(Command("backup"))
async def cmd_backup(
    message: Message, bot: Bot, db: Database, config: Config
) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, config):
        return
    ok = await _send_db_backup(message.chat.id, bot, db)
    if not ok:
        await message.answer("⚠️ Database file not found.")


@router.callback_query(F.data == "adm:backup")
async def cb_admin_backup(
    callback: CallbackQuery, bot: Bot, db: Database, config: Config
) -> None:
    if (
        not callback.from_user
        or not callback.message
        or not _is_admin(callback.from_user.id, config)
    ):
        await callback.answer("Admin access required.", show_alert=True)
        return
    ok = await _send_db_backup(callback.message.chat.id, bot, db)
    await callback.answer("Backup sent!" if ok else "Database file not found.", show_alert=not ok)


@router.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message, state: FSMContext, config: Config
) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, config):
        return
    await state.set_state(AdminStates.waiting_for_broadcast)
    await message.answer(
        f"📣 <b>New Broadcast</b>\n{DIVIDER}\n"
        f"Send the message (text, photo, video, or document) you want copied to all KoshBox users:",
        reply_markup=cancel_kb("adm:refresh"),
    )


@router.callback_query(F.data == "adm:broadcast")
async def cb_admin_broadcast(
    callback: CallbackQuery, state: FSMContext, config: Config
) -> None:
    if not callback.from_user or not _is_admin(callback.from_user.id, config):
        await callback.answer("Admin access required.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_broadcast)
    await safe_edit_or_send(
        callback,
        f"📣 <b>New Broadcast</b>\n{DIVIDER}\n"
        f"Send the message (text, photo, video, or document) you want copied to all KoshBox users:",
        cancel_kb("adm:refresh"),
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_broadcast)
async def fsm_broadcast(
    message: Message, state: FSMContext, db: Database, bot: Bot, config: Config
) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, config):
        return
    await state.clear()
    user_ids = await db.get_all_user_ids()
    sent, failed = 0, 0
    status_msg = await message.answer(f"📣 Broadcasting to <b>{len(user_ids)}</b> users…")

    for uid in user_ids:
        try:
            await bot.copy_message(
                chat_id=uid,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"✓ <b>Broadcast Complete</b>\n{DIVIDER}\n"
        f"Delivered: <b>{sent}</b>  ·  Skipped/Blocked: <b>{failed}</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="‹ Admin Console", callback_data="adm:refresh"),
                    home_btn(),
                ]
            ]
        ),
    )
