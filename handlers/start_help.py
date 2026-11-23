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
from utils.banners import edit_or_send_banner, send_banner_message
from utils.formatters import (
    DIVIDER,
    MEDIA_ICONS,
    MEDIA_LABELS,
    SORT_LABELS,
    back_btn,
    breadcrumb,
    build_file_card_text,
    build_file_keyboard,
    esc,
    format_size,
    home_btn,
    next_sort_key,
    progress_bar,
    safe_edit_or_send,
    send_stored_media,
    toggle_label,
)

router = Router(name="start_help")


async def render_home_dashboard(
    user_id: int, first_name: str, db: Database, bot: Bot
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Professional Hero Dashboard following GramIO & Telegram Mobile UX guidelines:
    - Clean 3-zone text hierarchy (Title -> Blockquote summary -> Subtle tip)
    - Strict 3-row button grid (5 buttons total, never a wall of 10 buttons!)
    - Secondary utilities live inside '⚙️ Settings & Tools' via progressive disclosure.
    """
    user = await db.get_user(user_id)
    stats = await db.get_vault_stats(user["vault_id"])
    me = await bot.get_me()
    uname = esc(me.username or "KoshBox_Bot")

    active_folder = "Personal Vault"
    if user.get("active_group_id"):
        grp = await db.get_group(user["active_group_id"])
        if grp:
            active_folder = f"{'👥' if grp['is_shared'] else '📁'} {grp['name']}"

    bulk_badge = "⚡ Bulk ON" if user.get("bulk_mode") else "Standard"
    total_files = stats["total_files"]
    total_size = format_size(stats["total_size"])
    groups_cnt = stats["groups_count"]

    greeting = f", <b>{esc(first_name)}</b>" if first_name else ""
    text = (
        f"👋 <b>Welcome to @{uname}</b>{greeting}\n\n"
        f"<i>Your personal Telegram Cloud Vault — archive any file once and send it in one click from any chat.</i>\n\n"
        f"<blockquote>"
        f"📦 <b>{total_files}</b> files  ·  💾 <b>{total_size}</b>  ·  📁 <b>{groups_cnt}</b> folders\n"
        f"🎯 Target: <b>{esc(active_folder)}</b>  ·  {bulk_badge}"
        f"</blockquote>\n\n"
        f"👇 <b>Drop media here or use the buttons below to get started</b>"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Search Vault Inline",
                    switch_inline_query_current_chat="",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(text="📦 My Files", callback_data="br:all:def:0:0"),
                InlineKeyboardButton(text="📁 Folders", callback_data="nav:groups"),
            ],
            [
                InlineKeyboardButton(text="⭐ Favorites", callback_data="br:fav:def:0:0"),
                InlineKeyboardButton(text="⚙️ Settings & Tools", callback_data="nav:settings"),
            ],
        ]
    )
    return text, kb


def _render_help_panel(
    bot_username: str, help_tab: str = "inline"
) -> tuple[str, InlineKeyboardMarkup]:
    uname = esc(bot_username or "KoshBox_Bot")

    if help_tab == "commands":
        text = (
            f"{breadcrumb('Vault', 'Settings', 'Guide')}\n"
            f"📖 <b>Bot Commands</b>\n\n"
            f"<blockquote>"
            f"• /start — Vault Dashboard\n"
            f"• /files — Browse & filter files\n"
            f"• /search — Keyword or tag search\n"
            f"• /groups — Folders & Shared Groups\n"
            f"• /bulk — Toggle Bulk Upload Mode\n"
            f"• /trash — Recycle Trash\n"
            f"• /accounts — Synced accounts\n"
            f"• /stats — Vault analytics\n"
            f"• /settings — Preferences & tools"
            f"</blockquote>"
        )
    else:
        text = (
            f"{breadcrumb('Vault', 'Settings', 'Guide')}\n"
            f"📖 <b>Inline Search Guide</b>\n\n"
            f"<blockquote>"
            f"Type <code>@{uname}</code> in <b>any</b> chat to find & send files:\n\n"
            f"• <code>@{uname} lofi</code> — Title, artist, caption, tags\n"
            f"• <code>@{uname} type:audio</code> — Media filter (<code>audio</code>, <code>video</code>, <code>doc</code>, <code>photo</code>, <code>voice</code>, <code>gif</code>)\n"
            f"• <code>@{uname} g:music</code> — Inside a folder or group\n"
            f"• <code>@{uname} #chill</code> — Hashtag filter\n"
            f"• <code>@{uname} fav</code> · <code>top</code> — Starred or most used"
            f"</blockquote>\n\n"
            f"<i>Combine freely:</i> <code>@{uname} g:music type:audio fav</code>"
        )

    tab_inline_lbl = "● Inline Syntax" if help_tab == "inline" else "○ Inline Syntax"
    tab_cmd_lbl = "● Commands" if help_tab == "commands" else "○ Commands"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=tab_inline_lbl, callback_data="help:inline"),
                InlineKeyboardButton(text=tab_cmd_lbl, callback_data="help:commands"),
            ],
            [
                InlineKeyboardButton(
                    text="🔍 Try Inline Search",
                    switch_inline_query_current_chat="",
                    style="primary",
                )
            ],
            [
                back_btn("nav:settings", "◀ Settings"),
                home_btn(),
            ],
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
        breakdown = "<i>No files archived yet — drop any media in chat to begin.</i>"

    summary_block = (
        f"📦 <b>{total_files}</b> files  ·  💾 <b>{format_size(stats['total_size'])}</b>\n"
        f"⭐ <b>{stats['fav_count']}</b> starred  ·  📈 <b>{stats['total_uses']}×</b> inline sends\n"
        f"📁 <b>{stats['groups_count']}</b> folders  ·  🔗 <b>{stats['linked_accounts']}</b> account(s)"
    )
    if stats["trash_count"] > 0:
        summary_block += f"\n🗑️ <b>{stats['trash_count']}</b> item(s) in Trash"

    lines = [
        breadcrumb("Vault", "Settings", "Analytics"),
        "📊 <b>Storage & Usage Analytics</b>\n",
        f"<blockquote>{summary_block}</blockquote>\n",
        f"<b>Media Breakdown</b>\n{breakdown}",
    ]

    if stats["top_files"]:
        top_lines = []
        for idx, tf in enumerate(stats["top_files"], start=1):
            ic = MEDIA_ICONS.get(tf["media_type"], "📦")
            top_lines.append(
                f"{idx}. {ic} <b>{esc(tf['title'][:32])}</b> — <code>{tf['use_count']}×</code>"
            )
        lines.append("\n<b>Top Sent Inline</b>\n" + "\n".join(top_lines))

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 Browse Files", callback_data="br:all:def:0:0"),
                InlineKeyboardButton(text="🔄 Refresh", callback_data="nav:stats"),
            ],
            [
                back_btn("nav:settings", "◀ Settings"),
                home_btn(),
            ],
        ]
    )
    return "\n".join(lines), kb


async def _render_settings_panel(user_id: int, db: Database) -> tuple[str, InlineKeyboardMarkup]:
    """
    Clean Settings & Tools Hub:
    - Toggles strictly in Row 1 (never mixed with navigation buttons in the same row)
    - Clear 2-column destination grid for Sync, Trash, Analytics, Guide
    - Single Back to Home footer
    """
    user = await db.get_user(user_id)
    trash_cnt = await db.count_trashed_files(user["vault_id"])
    sort_key = user.get("default_sort") or "new"
    sort_label = SORT_LABELS.get(sort_key, "Newest")
    bulk_on = bool(user.get("bulk_mode"))

    active_folder = "Personal Vault (Default)"
    if user.get("active_group_id"):
        grp = await db.get_group(user["active_group_id"])
        if grp:
            active_folder = f"{'👥' if grp['is_shared'] else '📁'} {grp['name']}"

    text = (
        f"{breadcrumb('Vault', 'Settings & Tools')}\n"
        f"⚙️ <b>Settings & Vault Tools</b>\n\n"
        f"<blockquote>"
        f"⚡ <b>Bulk Mode:</b> {'On (Compact replies)' if bulk_on else 'Off (Rich cards)'}\n"
        f"↕️ <b>Default Sort:</b> {sort_label}\n"
        f"🎯 <b>Upload Target:</b> {esc(active_folder)}\n"
        f"🗑️ <b>Recycle Trash:</b> {trash_cnt} item(s)"
        f"</blockquote>\n\n"
        f"<i>Tap a preference to toggle or open a tool below:</i>"
    )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=toggle_label(bulk_on, "Bulk Mode"),
                callback_data="set:bulk",
            ),
            InlineKeyboardButton(
                text=f"↕️ Sort: {sort_label}",
                callback_data="set:sort",
            ),
        ],
        [
            InlineKeyboardButton(text="📊 Analytics", callback_data="nav:stats"),
            InlineKeyboardButton(text=f"🗑️ Trash ({trash_cnt})", callback_data="trash:0"),
        ],
        [
            InlineKeyboardButton(text="🔗 Sync Accounts", callback_data="nav:accounts"),
            InlineKeyboardButton(text="📖 Quick Guide", callback_data="nav:help"),
        ],
    ]
    if user.get("active_group_id"):
        rows.append(
            [
                InlineKeyboardButton(
                    text="🎯 Reset Upload Target",
                    callback_data="set:clear_grp",
                )
            ]
        )
    rows.append([back_btn("nav:home", "◀ Back to Vault")])
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
                "⚠️ <b>Link Unavailable</b>\n"
                "<blockquote>This share link is invalid, revoked, or the file was removed.</blockquote>"
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
            await message.answer(
                "⚠️ <b>Invalid Invite</b>\n"
                "<blockquote>This shared group link has expired or been regenerated.</blockquote>"
            )
            return
        me = await bot.get_me()
        await message.answer(
            f"🎉 <b>Joined Shared Group</b>\n\n"
            f"<blockquote>👥 <b>{esc(group['name'])}</b> · {group.get('file_count', 0)} file(s)</blockquote>\n\n"
            f"<i>Inline filter:</i> <code>@{esc(me.username)} g:{esc(group['name'])}</code>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📂 Open Group",
                            callback_data=f"grp:open:{group['id']}",
                            style="primary",
                        ),
                        InlineKeyboardButton(
                            text="🔍 Search Group",
                            switch_inline_query_current_chat=f"g:{group['name']} ",
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
        upload_caption = (
            f"💡 <b>Ready to Archive into Your Vault</b>\n\n"
            f"<i>Send or forward any <b>audio, video, document, photo, voice note, GIF, or sticker</b> right here — it is saved permanently and ready for instant inline sharing.</i>\n\n"
            f"<blockquote>"
            f"🏷️ <b>Pro Tip:</b> Include hashtags like <code>#music #work</code> in captions to auto-tag your files."
            f"</blockquote>\n\n"
            f"<b>Drop a file anytime when you're ready</b>"
        )
        upload_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📁 Target Folder", callback_data="nav:groups"),
                    home_btn("🏠 Vault"),
                ]
            ]
        )
        await send_banner_message(
            message, "upload", upload_caption, upload_kb, bot, db, config
        )
        return

    text, kb = await render_home_dashboard(
        message.from_user.id, message.from_user.first_name or "", db, bot
    )
    await send_banner_message(message, "welcome", text, kb, bot, db, config)


@router.callback_query(F.data == "nav:home")
async def cb_nav_home(
    callback: CallbackQuery, state: FSMContext, db: Database, bot: Bot, config: Config
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
    await edit_or_send_banner(callback, "welcome", text, kb, bot, db, config)
    await callback.answer()


@router.callback_query(F.data == "home:bulk")
async def cb_home_bulk(
    callback: CallbackQuery, db: Database, bot: Bot, config: Config
) -> None:
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
    await edit_or_send_banner(callback, "welcome", text, kb, bot, db, config)
    await callback.answer(
        "⚡ Bulk Upload Mode ON" if enabled else "Bulk Upload Mode OFF"
    )


@router.message(Command("help"))
async def cmd_help(message: Message, bot: Bot, db: Database, config: Config) -> None:
    me = await bot.get_me()
    text, kb = _render_help_panel(me.username or "", "inline")
    await send_banner_message(message, "guide", text, kb, bot, db, config)


@router.callback_query(F.data == "nav:help")
async def cb_nav_help(
    callback: CallbackQuery, bot: Bot, db: Database, config: Config
) -> None:
    me = await bot.get_me()
    text, kb = _render_help_panel(me.username or "", "inline")
    await edit_or_send_banner(callback, "guide", text, kb, bot, db, config)
    await callback.answer()


@router.callback_query(F.data.startswith("help:"))
async def cb_switch_help_tab(
    callback: CallbackQuery, bot: Bot, db: Database, config: Config
) -> None:
    tab = callback.data.split(":", 1)[1]
    me = await bot.get_me()
    text, kb = _render_help_panel(me.username or "", tab)
    await edit_or_send_banner(callback, "guide", text, kb, bot, db, config)
    await callback.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer(
            "ℹ️ No active prompt right now.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn("🏠 Open Vault")]]),
        )
        return
    await state.clear()
    await message.answer(
        "✕ <b>Action cancelled.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn("🏠 Open Vault")]]),
    )


@router.callback_query(F.data.startswith("fsm:cancel:"))
async def cb_fsm_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    config: Config,
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
        await edit_or_send_banner(callback, "guide", text, kb, bot, db, config)
    elif target == "adm:refresh":
        from handlers.admin import _render_admin_panel

        text, kb = await _render_admin_panel(db)
        await safe_edit_or_send(callback, text, kb)
    elif target.startswith("br:"):
        from handlers.file_actions import render_browse_page

        _, flt, sort_key, gid_s, page_s = target.split(":")
        text, kb = await render_browse_page(
            callback.from_user.id, flt, sort_key, int(gid_s), int(page_s), db
        )
        await safe_edit_or_send(callback, text, kb)
    elif target.startswith("f:manage:"):
        fid = int(target.split(":")[2])
        f = await db.get_file(fid)
        if f:
            me = await bot.get_me()
            from utils.formatters import build_file_manage_keyboard

            await safe_edit_or_send(
                callback,
                build_file_card_text(f, me.username or "", edit_mode=True),
                build_file_manage_keyboard(f, bot_username=me.username or ""),
            )
    elif target.startswith("f:open:"):
        fid = int(target.split(":")[2])
        f = await db.get_file(fid)
        if f:
            me = await bot.get_me()
            await safe_edit_or_send(
                callback,
                build_file_card_text(f, me.username or ""),
                build_file_keyboard(f, bot_username=me.username or ""),
            )
        else:
            text, kb = await render_home_dashboard(
                callback.from_user.id, callback.from_user.first_name or "", db, bot
            )
            await edit_or_send_banner(callback, "welcome", text, kb, bot, db, config)
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
        await edit_or_send_banner(callback, "welcome", text, kb, bot, db, config)
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
            "⚡ <b>Bulk Upload Mode: ON</b>\n\n"
            "<blockquote>Forward or drop multiple files — I will archive them silently with compact one-line confirmations.</blockquote>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="⬜ Turn Off Bulk", callback_data="set:bulk"),
                        home_btn(),
                    ]
                ]
            ),
        )
    else:
        await message.answer(
            "✅ <b>Bulk Upload Mode: OFF</b>\n<i>Interactive upload cards are restored.</i>",
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
    await callback.answer("Upload target reset")
