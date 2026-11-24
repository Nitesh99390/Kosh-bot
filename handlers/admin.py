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
from utils.banners import BANNER_FILES, warmup_banners_in_dump_channel
from utils.formatters import (
    back_btn,
    breadcrumb,
    cancel_kb,
    format_size,
    home_btn,
    safe_edit_or_send,
)
from utils.states import AdminStates

router = Router(name="admin")


def _is_admin(user_id: int, config: Config) -> bool:
    return user_id in config.admin_ids


async def _render_admin_panel(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    stats = await db.get_global_stats()
    cached_banners = await db.list_ui_assets()
    ban_badge = f" · 🚫 {stats['banned']} banned" if stats.get("banned") else ""
    text = (
        f"{breadcrumb('⚙️ System', '🛠️ Admin Console')}\n"
        f"🛠️ <b>Admin Console</b>\n"
        f"<blockquote>"
        f"👤 <b>{stats['users']}</b> users <i>({stats['active_24h']} active 24h{ban_badge})</i>  •  🔐 <b>{stats['vaults']}</b> vaults\n"
        f"📦 <b>{stats['files']}</b> files  •  🎒 <b>{stats.get('packs', 0)}</b> packs  •  📁 <b>{stats['groups']}</b> folders\n"
        f"💾 <b>{format_size(stats['total_size'])}</b>  •  🖼️ Banners: <b>{len(cached_banners)}/{len(BANNER_FILES)}</b>"
        f"</blockquote>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💾 Backup DB",
                    callback_data="adm:backup",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📣 Broadcast",
                    callback_data="adm:broadcast",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🚫 Ban / Unban",
                    callback_data="adm:ban_prompt",
                ),
                InlineKeyboardButton(
                    text="🖼️ Sync Banners",
                    callback_data="adm:sync_banners",
                ),
            ],
            [
                InlineKeyboardButton(text="🔄 Refresh", callback_data="adm:refresh"),
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
        await callback.answer("Admin only.", show_alert=True)
        return
    text, kb = await _render_admin_panel(db)
    await safe_edit_or_send(callback, text, kb, answer_text="Updated", also_ack=True)


@router.callback_query(F.data == "adm:ban_prompt")
async def cb_admin_ban_prompt(
    callback: CallbackQuery, state: FSMContext, config: Config
) -> None:
    if not callback.from_user or not _is_admin(callback.from_user.id, config):
        await callback.answer("Admin only.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_ban_uid)
    await safe_edit_or_send(
        callback,
        "🚫 <b>Ban / Unban User</b>\n<blockquote>Send numeric <code>User ID</code> to toggle ban status:</blockquote>",
        cancel_kb("adm:refresh"),
        also_ack=True,
    )


@router.message(AdminStates.waiting_for_ban_uid)
async def fsm_admin_toggle_ban(
    message: Message, state: FSMContext, db: Database, config: Config
) -> None:
    if (
        not message.from_user
        or not message.text
        or not _is_admin(message.from_user.id, config)
    ):
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    if not raw.isdigit():
        await message.answer("⚠️ Send a numeric User ID.", reply_markup=cancel_kb("adm:refresh"))
        return
    target_uid = int(raw)
    if target_uid in config.admin_ids:
        await message.answer("⚠️ Cannot ban an admin.", reply_markup=cancel_kb("adm:refresh"))
        return
    await state.clear()
    target = await db.get_user(target_uid)
    if not target:
        await message.answer("⚠️ User ID not found in DB.", reply_markup=cancel_kb("adm:refresh"))
        return
    new_ban = not bool(target.get("is_banned"))
    await db.set_user_banned(target_uid, new_ban)
    text, kb = await _render_admin_panel(db)
    status_lbl = "🚫 Banned" if new_ban else "✅ Unbanned"
    await message.answer(
        f"{status_lbl} <code>{target_uid}</code>\n\n{text}", reply_markup=kb
    )


@router.callback_query(F.data == "adm:sync_banners")
async def cb_admin_sync_banners(
    callback: CallbackQuery, bot: Bot, db: Database, config: Config
) -> None:
    if not callback.from_user or not _is_admin(callback.from_user.id, config):
        await callback.answer("Admin only.", show_alert=True)
        return
    status = await warmup_banners_in_dump_channel(bot, db, config, force_reupload=False)
    ok_cnt = sum(1 for v in status.values() if v)
    text, kb = await _render_admin_panel(db)
    await safe_edit_or_send(
        callback,
        text,
        kb,
        answer_text=f"🖼️ {ok_cnt}/{len(status)} banners cached!",
        also_ack=True,
    )


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
        await message.answer("⚠️ DB file not found.")


@router.callback_query(F.data == "adm:backup")
async def cb_admin_backup(
    callback: CallbackQuery, bot: Bot, db: Database, config: Config
) -> None:
    if (
        not callback.from_user
        or not callback.message
        or not _is_admin(callback.from_user.id, config)
    ):
        await callback.answer("Admin only.", show_alert=True)
        return
    ok = await _send_db_backup(callback.message.chat.id, bot, db)
    await callback.answer("Backup sent!" if ok else "DB not found.", show_alert=not ok)


@router.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message, state: FSMContext, config: Config
) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, config):
        return
    await state.set_state(AdminStates.waiting_for_broadcast)
    await message.answer(
        "📣 <b>Broadcast Message</b>\n<blockquote>Send text or media to copy to all users:</blockquote>",
        reply_markup=cancel_kb("adm:refresh"),
    )


@router.callback_query(F.data == "adm:broadcast")
async def cb_admin_broadcast(
    callback: CallbackQuery, state: FSMContext, config: Config
) -> None:
    if not callback.from_user or not _is_admin(callback.from_user.id, config):
        await callback.answer("Admin only.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_broadcast)
    await safe_edit_or_send(
        callback,
        "📣 <b>Broadcast Message</b>\n<blockquote>Send text or media to copy to all users:</blockquote>",
        cancel_kb("adm:refresh"),
        also_ack=True,
    )


@router.message(AdminStates.waiting_for_broadcast)
async def fsm_broadcast(
    message: Message, state: FSMContext, db: Database, bot: Bot, config: Config
) -> None:
    if not message.from_user or not _is_admin(message.from_user.id, config):
        return
    if message.text and message.text.strip().lower() in ("/cancel", "/start"):
        await state.clear()
        return
    await state.clear()
    user_ids = await db.get_all_user_ids()
    sent, failed = 0, 0
    status_msg = await message.answer(f"⏳ Sending to <b>{len(user_ids)}</b> users…")

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
        f"✅ <b>Broadcast Complete</b> · Sent: <b>{sent}</b> · Skipped: <b>{failed}</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[back_btn("adm:refresh", "⬅️ Admin"), home_btn()]]
        ),
    )
