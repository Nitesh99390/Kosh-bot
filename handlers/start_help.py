from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Config
from database import Database
from handlers.accounts import handle_verify_code, render_accounts_panel
from utils.formatters import (
    DIVIDER,
    MEDIA_ICONS,
    MEDIA_LABELS,
    SORT_LABELS,
    esc,
    format_size,
    home_btn,
    next_sort_key,
    progress_bar,
    safe_edit_or_send,
    send_stored_media,
)

router = Router(name="start_help")


async def render_home_dashboard(
    user_id: int, first_name: str, db: Database, bot: Bot
) -> tuple[str, InlineKeyboardMarkup]:
    user = await db.get_user(user_id)
    stats = await db.get_vault_stats(user["vault_id"])
    me = await bot.get_me()
    uname = esc(me.username or "KoshBox_Bot")

    active_folder = "Personal Vault"
    if user.get("active_group_id"):
        grp = await db.get_group(user["active_group_id"])
        if grp:
            active_folder = f"{'👥' if grp['is_shared'] else '📁'} {grp['name']}"

    bulk_badge = "⚡ ON" if user.get("bulk_mode") else "Off"
    total_files = stats["total_files"]
    total_size = format_size(stats["total_size"])
    groups_cnt = stats["groups_count"]

    text = (
        f"✨ <b>KoshBox Cloud Vault</b>\n"
        f"Welcome back, <b>{esc(first_name or 'there')}</b>.\n"
        f"{DIVIDER}\n"
        f"📦 <b>{total_files}</b> files  ·  💾 <b>{total_size}</b>  ·  📁 <b>{groups_cnt}</b> folders\n"
        f"🎯 Upload Target: <b>{esc(active_folder)}</b>  ·  Bulk: <b>{bulk_badge}</b>\n"
        f"{DIVIDER}\n"
        f"Drop any file, audio, video, photo, voice, GIF, or sticker here to archive it instantly.\n\n"
        f"💡 <i>Inline anywhere:</i> <code>@{uname} </code>"
    )

    bulk_btn_text = "⚡ Bulk Mode: ON" if user.get("bulk_mode") else "⚡ Bulk Mode: OFF"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Search Inline",
                    switch_inline_query_current_chat="",
                ),
                InlineKeyboardButton(
                    text="📤 Share to Chat",
                    switch_inline_query="",
                ),
            ],
            [
                InlineKeyboardButton(text="📦 My Files", callback_data="br:all:def:0:0"),
                InlineKeyboardButton(text="📁 Folders & Groups", callback_data="nav:groups"),
            ],
            [
                InlineKeyboardButton(text="★ Favorites", callback_data="br:fav:def:0:0"),
                InlineKeyboardButton(text="📊 Analytics", callback_data="nav:stats"),
            ],
            [
                InlineKeyboardButton(text=bulk_btn_text, callback_data="home:bulk"),
                InlineKeyboardButton(text="⚙️ Settings", callback_data="nav:settings"),
            ],
            [
                InlineKeyboardButton(text="🔗 Sync Accounts", callback_data="nav:accounts"),
                InlineKeyboardButton(text="📖 Quick Guide", callback_data="nav:help"),
            ],
        ]
    )
    return text, kb


def _render_help_panel(bot_username: str, help_tab: str = "inline") -> tuple[str, InlineKeyboardMarkup]:
    uname = esc(bot_username or "KoshBox_Bot")

    if help_tab == "commands":
        text = (
            f"📖 <b>KoshBox Guide · Commands</b>\n"
            f"{DIVIDER}\n"
            f"• /start — Open your Vault Dashboard\n"
            f"• /files — Browse, filter & sort your files\n"
            f"• /search — Search files inside this chat\n"
            f"• /groups — Manage Personal Folders & Shared Groups\n"
            f"• /bulk — Toggle silent Bulk Upload Mode\n"
            f"• /trash — View or restore trashed files\n"
            f"• /accounts — Manage synced Telegram accounts\n"
            f"• /link — Generate a multi-account sync code\n"
            f"• /verify <code>CODE</code> — Link a secondary account\n"
            f"• /stats — View storage & usage analytics\n"
            f"• /settings — Configure vault preferences\n"
            f"• /cancel — Cancel any active prompt"
        )
    else:
        text = (
            f"📖 <b>KoshBox Guide · Inline Search</b>\n"
            f"{DIVIDER}\n"
            f"Type <code>@{uname}</code> in <b>any</b> Telegram chat to search and send files instantly:\n\n"
            f"• <code>@{uname} </code> — Recent & favorite files\n"
            f"• <code>@{uname} lofi</code> — Search title, artist, filename, caption, or tags\n"
            f"• <code>@{uname} type:audio</code> — Filter by media (<code>audio</code>, <code>video</code>, <code>doc</code>, <code>photo</code>, <code>voice</code>, <code>gif</code>, <code>sticker</code>)\n"
            f"• <code>@{uname} g:music</code> — Filter by folder or shared group\n"
            f"• <code>@{uname} #chill</code> — Filter by hashtag\n"
            f"• <code>@{uname} fav</code> — Only starred favorites\n"
            f"• <code>@{uname} top</code> — Sort by most frequently sent\n\n"
            f"💡 <b>Pro Tip:</b> Combine filters freely:\n"
            f"<code>@{uname} g:music type:audio fav</code>"
        )

    tab_inline_lbl = "● Inline Filters" if help_tab == "inline" else "○ Inline Filters"
    tab_cmd_lbl = "● Bot Commands" if help_tab == "commands" else "○ Bot Commands"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=tab_inline_lbl, callback_data="help:inline"),
                InlineKeyboardButton(text=tab_cmd_lbl, callback_data="help:commands"),
            ],
            [
                InlineKeyboardButton(
                    text="🔍 Try Inline Search Now",
                    switch_inline_query_current_chat="",
                )
            ],
            [home_btn()],
        ]
    )
    return text, kb


async def _render_stats_panel(user_id: int, db: Database) -> tuple[str, InlineKeyboardMarkup]:
    user = await db.get_user(user_id)
    stats = await db.get_vault_stats(user["vault_id"])
    total_files = stats["total_files"]
    by_type = stats["by_type"]

    if by_type and total_files > 0:
        b_lines = []
        for mtype, info in sorted(by_type.items(), key=lambda x: -x[1]["count"]):
            cnt = info["count"]
            sz = format_size(info["size"])
            ratio = cnt / max(1, total_files)
            bar = progress_bar(ratio, width=7)
            icon = MEDIA_ICONS.get(mtype, "📦")
            label = MEDIA_LABELS.get(mtype, mtype.capitalize())
            b_lines.append(f"{icon} <code>{bar}</code> <b>{cnt}</b> {label} <i>({sz})</i>")
        breakdown = "\n".join(b_lines)
    else:
        breakdown = "<i>No files archived yet — send any media to begin.</i>"

    lines = [
        "📊 <b>Vault Analytics & Storage</b>",
        DIVIDER,
        f"📦 <b>Active Files:</b> {total_files}   ·   💾 <b>Size:</b> {format_size(stats['total_size'])}",
        f"★ <b>Favorites:</b> {stats['fav_count']}   ·   📈 <b>Inline Sends:</b> {stats['total_uses']}×",
        f"📁 <b>Folders/Groups:</b> {stats['groups_count']}   ·   🔗 <b>Synced Accounts:</b> {stats['linked_accounts']}",
    ]
    if stats["trash_count"] > 0:
        lines.append(f"🗑️ <b>In Trash:</b> {stats['trash_count']} file(s)")

    lines.append(f"{DIVIDER}\n<b>Media Distribution</b>\n{breakdown}")

    if stats["top_files"]:
        top_lines = []
        for idx, tf in enumerate(stats["top_files"], start=1):
            ic = MEDIA_ICONS.get(tf["media_type"], "📦")
            top_lines.append(
                f"{idx}. {ic} <b>{esc(tf['title'][:32])}</b> — <code>{tf['use_count']}×</code>"
            )
        lines.append(f"\n<b>Most Used Inline</b>\n" + "\n".join(top_lines))

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 Browse Files", callback_data="br:all:def:0:0"),
                InlineKeyboardButton(text="🔄 Refresh", callback_data="nav:stats"),
            ],
            [
                InlineKeyboardButton(
                    text=f"🗑️ Trash ({stats['trash_count']})",
                    callback_data="trash:0",
                ),
                home_btn(),
            ],
        ]
    )
    return "\n".join(lines), kb


async def _render_settings_panel(user_id: int, db: Database) -> tuple[str, InlineKeyboardMarkup]:
    user = await db.get_user(user_id)
    trash_cnt = await db.count_trashed_files(user["vault_id"])
    sort_key = user.get("default_sort") or "new"
    sort_label = SORT_LABELS.get(sort_key, "Newest")
    bulk_on = bool(user.get("bulk_mode"))

    active_folder = "None (Personal Vault Root)"
    if user.get("active_group_id"):
        grp = await db.get_group(user["active_group_id"])
        if grp:
            active_folder = f"{'👥' if grp['is_shared'] else '📁'} {grp['name']}"

    text = (
        f"⚙️ <b>Vault Preferences</b>\n"
        f"{DIVIDER}\n"
        f"⚡ <b>Bulk Upload Mode:</b> {'Enabled (Compact replies)' if bulk_on else 'Disabled (Rich file cards)'}\n"
        f"↕️ <b>Default File Sort:</b> {sort_label}\n"
        f"🎯 <b>Auto-Upload Folder:</b> {esc(active_folder)}\n"
        f"🗑️ <b>Recycle Trash:</b> {trash_cnt} item(s)\n"
        f"{DIVIDER}\n"
        f"<i>Tap any option below to customize your workflow:</i>"
    )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"⚡ Bulk Mode: {'ON' if bulk_on else 'OFF'}",
                callback_data="set:bulk",
            ),
            InlineKeyboardButton(
                text=f"↕️ Sort: {sort_label}",
                callback_data="set:sort",
            ),
        ],
        [
            InlineKeyboardButton(text="📁 Change Upload Folder", callback_data="nav:groups"),
            InlineKeyboardButton(text=f"🗑️ Open Trash ({trash_cnt})", callback_data="trash:0"),
        ],
    ]
    if user.get("active_group_id"):
        rows.append(
            [
                InlineKeyboardButton(
                    text="✕ Reset Auto-Upload Folder",
                    callback_data="set:clear_grp",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="🔗 Synced Accounts", callback_data="nav:accounts"),
            home_btn(),
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    db: Database,
    bot: Bot,
    config: Config,
) -> None:
    if not message.from_user:
        return
    await state.clear()
    user = await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    args = (command.args or "").strip()

    # Deep-link: shared file
    if args.startswith("file_"):
        token = args[5:]
        file_row = await db.get_file_by_share_token(token)
        if not file_row:
            await message.answer(
                "⚠️ <b>Link Unavailable</b>\nThis share link is invalid, revoked, or the file was removed."
            )
            return
        ok = await send_stored_media(bot, message.chat.id, file_row)
        if not ok:
            await message.answer("⚠️ Unsupported media format.")
        return

    # Deep-link: join shared group
    if args.startswith("join_"):
        invite = args[5:]
        group = await db.join_shared_group(user["vault_id"], invite)
        if not group:
            await message.answer("⚠️ <b>Invalid Invite</b>\nThis shared group link has expired or been regenerated.")
            return
        me = await bot.get_me()
        await message.answer(
            f"🎉 <b>Joined Shared Group!</b>\n"
            f"{DIVIDER}\n"
            f"👥 <b>{esc(group['name'])}</b> ({group.get('file_count', 0)} files)\n\n"
            f"All files in this group are now live in your inline menu:\n"
            f"<code>@{esc(me.username)} g:{esc(group['name'])}</code>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔍 Search Group Inline",
                            switch_inline_query_current_chat=f"g:{group['name']} ",
                        ),
                        InlineKeyboardButton(
                            text="📂 Open Group",
                            callback_data=f"grp:open:{group['id']}",
                        ),
                    ],
                    [home_btn()],
                ]
            ),
        )
        return

    # Deep-link: link secondary account
    if args.startswith("link_"):
        code = args[5:]
        await handle_verify_code(message, code, db, bot)
        return

    if args == "upload":
        await message.answer(
            f"📤 <b>Ready to Archive</b>\n"
            f"{DIVIDER}\n"
            f"Send or forward any <b>audio, video, document, photo, voice note, GIF, or sticker</b> right here.\n\n"
            f"<i>Tip: Include hashtags like <code>#music #work</code> in captions to auto-tag!</i>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="📁 Choose Target Folder", callback_data="nav:groups"),
                        home_btn("🏠 Dashboard"),
                    ]
                ]
            ),
        )
        return

    text, kb = await render_home_dashboard(
        message.from_user.id, message.from_user.first_name or "", db, bot
    )
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data == "nav:home")
async def cb_nav_home(
    callback: CallbackQuery, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not callback.from_user:
        return
    await state.clear()
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await render_home_dashboard(
        callback.from_user.id, callback.from_user.first_name or "", db, bot
    )
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "home:bulk")
async def cb_home_bulk(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not callback.from_user:
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    enabled = await db.toggle_bulk_mode(callback.from_user.id)
    text, kb = await render_home_dashboard(
        callback.from_user.id, callback.from_user.first_name or "", db, bot
    )
    await safe_edit_or_send(callback, text, kb)
    await callback.answer(
        "⚡ Bulk Upload Mode ON — forward files freely!" if enabled else "Bulk Upload Mode OFF"
    )


@router.message(Command("help"))
async def cmd_help(message: Message, bot: Bot) -> None:
    me = await bot.get_me()
    text, kb = _render_help_panel(me.username or "", "inline")
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "nav:help")
async def cb_nav_help(callback: CallbackQuery, bot: Bot) -> None:
    me = await bot.get_me()
    text, kb = _render_help_panel(me.username or "", "inline")
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("help:"))
async def cb_switch_help_tab(callback: CallbackQuery, bot: Bot) -> None:
    tab = callback.data.split(":", 1)[1]
    me = await bot.get_me()
    text, kb = _render_help_panel(me.username or "", tab)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer(
            "ℹ️ No active prompt right now.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn("🏠 Open Dashboard")]]),
        )
        return
    await state.clear()
    await message.answer(
        "✕ <b>Action cancelled.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn("🏠 Open Dashboard")]]),
    )


@router.callback_query(F.data.startswith("fsm:cancel:"))
async def cb_fsm_cancel(
    callback: CallbackQuery, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not callback.from_user:
        return
    await state.clear()
    target = callback.data.split(":", 2)[2]

    if target == "nav:groups":
        from handlers.groups import render_groups_panel

        text, kb = await render_groups_panel(callback.from_user.id, db)
        await safe_edit_or_send(callback, text, kb)
    elif target == "nav:accounts":
        text, kb = await render_accounts_panel(callback.from_user.id, db)
        await safe_edit_or_send(callback, text, kb)
    elif target.startswith("f:open:"):
        fid = int(target.split(":")[2])
        f = await db.get_file(fid)
        if f:
            me = await bot.get_me()
            from utils.formatters import build_file_card_text, build_file_keyboard

            await safe_edit_or_send(
                callback,
                build_file_card_text(f, me.username or ""),
                build_file_keyboard(f),
            )
        else:
            text, kb = await render_home_dashboard(
                callback.from_user.id, callback.from_user.first_name or "", db, bot
            )
            await safe_edit_or_send(callback, text, kb)
    elif target.startswith("grp:open:"):
        gid = int(target.split(":")[2])
        from handlers.groups import render_single_group

        rendered = await render_single_group(callback.from_user.id, gid, db, bot)
        if rendered:
            await safe_edit_or_send(callback, rendered[0], rendered[1])
    else:
        text, kb = await render_home_dashboard(
            callback.from_user.id, callback.from_user.first_name or "", db, bot
        )
        await safe_edit_or_send(callback, text, kb)
    await callback.answer("Cancelled")


@router.message(Command("bulk"))
async def cmd_bulk(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    enabled = await db.toggle_bulk_mode(message.from_user.id)
    if enabled:
        await message.answer(
            f"⚡ <b>Bulk Upload Mode: ON</b>\n"
            f"{DIVIDER}\n"
            f"Forward or drop multiple files — I will archive them cleanly with compact confirmations.\n"
            f"Tap below or send /bulk when finished.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✓ Turn Off Bulk Mode", callback_data="set:bulk"),
                        home_btn(),
                    ]
                ]
            ),
        )
    else:
        await message.answer(
            "✓ <b>Bulk Upload Mode: OFF</b>\nDetailed interactive file cards are restored.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    text, kb = await _render_stats_panel(message.from_user.id, db)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "nav:stats")
async def cb_nav_stats(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await _render_stats_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.message(Command("settings"))
async def cmd_settings(message: Message, db: Database) -> None:
    if not message.from_user:
        return
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    text, kb = await _render_settings_panel(message.from_user.id, db)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "nav:settings")
async def cb_nav_settings(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "set:bulk")
async def cb_settings_bulk(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    enabled = await db.toggle_bulk_mode(callback.from_user.id)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer("Bulk Mode ON" if enabled else "Bulk Mode OFF")


@router.callback_query(F.data == "set:sort")
async def cb_settings_sort(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    user = await db.get_user(callback.from_user.id)
    nxt = next_sort_key(user.get("default_sort") or "new")
    await db.set_default_sort(callback.from_user.id, nxt)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer(f"Default sort: {SORT_LABELS[nxt]}")


@router.callback_query(F.data == "set:clear_grp")
async def cb_settings_clear_grp(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.set_active_group(callback.from_user.id, None)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer("Auto-upload folder reset")
