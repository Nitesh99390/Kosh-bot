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
from database import Database, verify_pin
from handlers.accounts import handle_verify_code, render_accounts_panel
from handlers.packs import deliver_pack_to_chat, render_packs_list
from utils.banners import edit_or_send_banner, send_banner_message
from utils.formatters import (
    MEDIA_ICONS,
    MEDIA_LABELS,
    SORT_LABELS,
    back_btn,
    breadcrumb,
    build_file_card_text,
    build_file_keyboard,
    build_file_link_keyboard,
    build_file_manage_keyboard,
    cancel_kb,
    esc,
    format_size,
    format_ttl_short,
    home_btn,
    next_sort_key,
    progress_bar,
    safe_edit_or_send,
    send_stored_media,
    toggle_label,
)
from utils.middleware import check_force_sub
from utils.states import ShareUnlockStates

router = Router(name="start_help")


async def render_home_dashboard(
    user_id: int, first_name: str, db: Database, bot: Bot
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Ultra-crisp Hero Dashboard (Linear / Raycast / PostBot Pro UX):
    - Short 4-line scannable layout (Zero fluff!)
    - Focused 3-row action grid (6 buttons total)
    """
    user = await db.get_user(user_id)
    stats = await db.get_vault_stats(user["vault_id"])
    me = await bot.get_me()
    uname = esc(me.username or "KoshBox_Bot")

    active_folder = "Personal"
    if user.get("active_group_id"):
        grp = await db.get_group(user["active_group_id"])
        if grp:
            active_folder = f"{'👥' if grp['is_shared'] else '📁'} {grp['name']}"

    mode_badges: list[str] = []
    if user.get("bulk_mode"):
        mode_badges.append("⚡ Bulk")
    if user.get("protect_content"):
        mode_badges.append("🛡️ No-Fwd")
    if user.get("auto_delete_ttl"):
        mode_badges.append(f"⏱ {format_ttl_short(user['auto_delete_ttl'])}")
    mode_str = " · ".join(mode_badges) if mode_badges else "Standard"

    total_files = stats["total_files"]
    total_size = format_size(stats["total_size"])
    packs_cnt = stats.get("packs_count", 0)

    greeting = f" · <b>{esc(first_name)}</b>" if first_name else ""
    text = (
        f"⚡ <b>@{uname}</b>{greeting}\n"
        f"<blockquote>"
        f"📦 <b>{total_files}</b> files  ·  💾 <b>{total_size}</b>  ·  🎒 <b>{packs_cnt}</b> packs\n"
        f"🎯 <b>{esc(active_folder)}</b>  ·  {mode_str}"
        f"</blockquote>\n"
        f"<i>Drop any media to archive · search inline anywhere</i>"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Search Inline",
                    switch_inline_query_current_chat="",
                    style="primary",
                ),
                InlineKeyboardButton(text="📦 Files", callback_data="br:all:def:0:0"),
            ],
            [
                InlineKeyboardButton(text="📁 Folders", callback_data="nav:groups"),
                InlineKeyboardButton(text="🎒 Packs", callback_data="nav:packs"),
            ],
            [
                InlineKeyboardButton(text="⭐ Starred", callback_data="br:fav:def:0:0"),
                InlineKeyboardButton(text="⚙️ Settings", callback_data="nav:settings"),
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
            f"{breadcrumb('Vault', 'Guide')}\n"
            f"📖 <b>Commands</b>\n"
            f"<blockquote>"
            f"/start — Vault Dashboard\n"
            f"/files · /search — Browse & find\n"
            f"/packs · /batch — Multi-file pack links\n"
            f"/groups — Folders & shared spaces\n"
            f"/bulk — Fast silent uploads\n"
            f"/accounts — Multi-account sync\n"
            f"/settings · /stats · /trash"
            f"</blockquote>"
        )
    else:
        text = (
            f"{breadcrumb('Vault', 'Guide')}\n"
            f"📖 <b>Inline Search</b>\n"
            f"<blockquote>"
            f"Type <code>@{uname}</code> in any chat:\n"
            f"• <code>@{uname} lofi</code> — Keyword / tag\n"
            f"• <code>@{uname} t:audio</code> — <code>audio</code> <code>video</code> <code>doc</code> <code>photo</code> <code>gif</code>\n"
            f"• <code>@{uname} g:music</code> — Folder filter\n"
            f"• <code>@{uname} fav</code> · <code>top</code> · <code>packs</code>"
            f"</blockquote>"
        )

    tab_inline_lbl = "● Inline" if help_tab == "inline" else "○ Inline"
    tab_cmd_lbl = "● Commands" if help_tab == "commands" else "○ Commands"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=tab_inline_lbl, callback_data="help:inline"),
                InlineKeyboardButton(text=tab_cmd_lbl, callback_data="help:commands"),
            ],
            [
                InlineKeyboardButton(
                    text="🔍 Try Inline",
                    switch_inline_query_current_chat="",
                    style="primary",
                )
            ],
            [back_btn("nav:settings", "◀ Settings"), home_btn()],
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
            bar = progress_bar(cnt / max(1, total_files), width=6)
            icon = MEDIA_ICONS.get(mtype, "📦")
            label = MEDIA_LABELS.get(mtype, mtype.capitalize())
            b_lines.append(f"{icon} <code>{bar}</code> <b>{cnt}</b> {label} <i>({sz})</i>")
        breakdown = "\n".join(b_lines)
    else:
        breakdown = "<i>No files yet.</i>"

    summary_block = (
        f"📦 <b>{total_files}</b> files · 💾 <b>{format_size(stats['total_size'])}</b> · 🎒 <b>{stats.get('packs_count', 0)}</b> packs\n"
        f"⭐ <b>{stats['fav_count']}</b> starred · 📈 <b>{stats['total_uses']}×</b> inline · 📁 <b>{stats['groups_count']}</b> folders"
    )

    lines = [
        breadcrumb("Vault", "Analytics"),
        "📊 <b>Vault Analytics</b>",
        f"<blockquote>{summary_block}</blockquote>",
        breakdown,
    ]

    if stats["top_files"]:
        top_lines = [
            f"{idx}. {MEDIA_ICONS.get(tf['media_type'], '📦')} <b>{esc(tf['title'][:26])}</b> · <code>{tf['use_count']}×</code>"
            for idx, tf in enumerate(stats["top_files"], start=1)
        ]
        lines.append("<b>Top Inline</b>\n" + "\n".join(top_lines))

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 Files", callback_data="br:all:def:0:0"),
                InlineKeyboardButton(text="🔄 Refresh", callback_data="nav:stats"),
            ],
            [back_btn("nav:settings", "◀ Settings"), home_btn()],
        ]
    )
    return "\n".join(lines), kb


async def _render_settings_panel(user_id: int, db: Database) -> tuple[str, InlineKeyboardMarkup]:
    """
    Compact Pro Settings & Security Hub:
    - Bulk Mode, Default Sort, Auto-Delete Timer, Content Protection (No-Fwd)
    - Analytics, Trash, Sync Accounts, Guide
    """
    user = await db.get_user(user_id)
    trash_cnt = await db.count_trashed_files(user["vault_id"])
    sort_key = user.get("default_sort") or "new"
    sort_label = SORT_LABELS.get(sort_key, "Newest")
    bulk_on = bool(user.get("bulk_mode"))
    prot_on = bool(user.get("protect_content"))
    ttl_val = int(user.get("auto_delete_ttl") or 0)
    ttl_lbl = format_ttl_short(ttl_val)

    active_folder = "Personal"
    if user.get("active_group_id"):
        grp = await db.get_group(user["active_group_id"])
        if grp:
            active_folder = f"{'👥' if grp['is_shared'] else '📁'} {grp['name']}"

    text = (
        f"{breadcrumb('Vault', 'Settings')}\n"
        f"⚙️ <b>Settings & Security</b>\n"
        f"<blockquote>"
        f"⚡ Bulk: <b>{'ON' if bulk_on else 'Off'}</b>  ·  ↕️ Sort: <b>{sort_label}</b>\n"
        f"🛡️ No-Fwd: <b>{'ON' if prot_on else 'Off'}</b>  ·  ⏱ Auto-Del: <b>{ttl_lbl}</b>\n"
        f"🎯 Target: <b>{esc(active_folder)}</b>"
        f"</blockquote>"
    )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=toggle_label(bulk_on, "Bulk"),
                callback_data="set:bulk",
            ),
            InlineKeyboardButton(
                text=f"↕️ {sort_label}",
                callback_data="set:sort",
            ),
        ],
        [
            InlineKeyboardButton(
                text=toggle_label(prot_on, "No-Fwd"),
                callback_data="set:prot",
            ),
            InlineKeyboardButton(
                text=f"⏱ Auto-Del: {ttl_lbl}",
                callback_data="set:ttl",
            ),
        ],
        [
            InlineKeyboardButton(text="📊 Stats", callback_data="nav:stats"),
            InlineKeyboardButton(text=f"🗑️ Trash ({trash_cnt})", callback_data="trash:0"),
        ],
        [
            InlineKeyboardButton(text="🔗 Sync", callback_data="nav:accounts"),
            InlineKeyboardButton(text="📖 Guide", callback_data="nav:help"),
        ],
    ]
    if user.get("active_group_id"):
        rows.append(
            [InlineKeyboardButton(text="🎯 Reset Target", callback_data="set:clear_grp")]
        )
    rows.append([back_btn("nav:home", "◀ Vault")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "fsub:check")
async def cb_force_sub_verify(
    callback: CallbackQuery, db: Database, bot: Bot, config: Config
) -> None:
    if not callback.from_user:
        return
    if config.force_sub_channel:
        ok = await check_force_sub(bot, config.force_sub_channel, callback.from_user.id)
        if not ok:
            await callback.answer("Still not joined — join first!", show_alert=True)
            return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await render_home_dashboard(
        callback.from_user.id, callback.from_user.first_name or "", db, bot
    )
    await edit_or_send_banner(callback, "welcome", text, kb, bot, db, config)
    await callback.answer("✓ Verified!")


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

    # Deep-link: shared single file (supports PIN, Expiry, Protect Content, Auto-Delete)
    if args.startswith("file_"):
        token = args[5:]
        file_row = await db.get_file_by_share_token(token)
        if not file_row:
            await message.answer(
                "⚠️ <b>Link Expired or Invalid</b>\n"
                "<blockquote>This link was revoked, expired, or removed.</blockquote>"
            )
            return

        if file_row.get("share_pin_hash"):
            await state.set_state(ShareUnlockStates.waiting_for_file_pin)
            await state.update_data(unlock_file_token=token)
            await message.answer(
                f"🔒 <b>PIN Required</b>\n"
                f"<blockquote>Enter PIN to unlock <b>{esc(file_row['title'][:36])}</b>:</blockquote>",
                reply_markup=cancel_kb("nav:home"),
            )
            return

        owner = await db.get_user(file_row["uploader_user_id"])
        ttl = int((owner or {}).get("auto_delete_ttl") or user.get("auto_delete_ttl") or 0)
        protect = bool(file_row.get("protect_share")) or bool((owner or {}).get("protect_content"))
        sent = await send_stored_media(
            bot,
            message.chat.id,
            file_row,
            protect_content=protect,
            auto_delete_ttl=ttl,
        )
        if sent:
            await db.increment_use_count(file_row["id"])
        else:
            await message.answer("⚠️ Unsupported media format.")
        return

    # Deep-link: shared multi-file Pack (supports PIN, Expiry, Protect Content, Auto-Delete)
    if args.startswith("pack_"):
        token = args[5:]
        pack = await db.get_pack_by_token(token)
        if not pack or pack["item_count"] == 0:
            await message.answer(
                "⚠️ <b>Pack Unavailable</b>\n"
                "<blockquote>This pack link has expired or been removed.</blockquote>"
            )
            return

        if pack.get("pin_hash"):
            await state.set_state(ShareUnlockStates.waiting_for_pack_pin)
            await state.update_data(unlock_pack_token=token)
            await message.answer(
                f"🔒 <b>PIN Required</b>\n"
                f"<blockquote>Enter PIN to unlock <b>{esc(pack['title'])}</b> ({pack['item_count']} files):</blockquote>",
                reply_markup=cancel_kb("nav:home"),
            )
            return

        creator = await db.get_user(pack["creator_user_id"])
        ttl = int((creator or {}).get("auto_delete_ttl") or user.get("auto_delete_ttl") or 0)
        await deliver_pack_to_chat(bot, message.chat.id, pack, db, auto_delete_ttl=ttl)
        return

    # Deep-link: join shared group
    if args.startswith("join_"):
        invite = args[5:]
        group = await db.join_shared_group(user["vault_id"], invite)
        if not group:
            await message.answer(
                "⚠️ <b>Invalid Invite</b>\n<blockquote>Link expired or reset.</blockquote>"
            )
            return
        me = await bot.get_me()
        await message.answer(
            f"🎉 <b>Joined {esc(group['name'])}</b> · <i>{group.get('file_count', 0)} files</i>\n"
            f"<blockquote>Inline: <code>@{esc(me.username)} g:{esc(group['name'])}</code></blockquote>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📂 Open",
                            callback_data=f"grp:open:{group['id']}",
                            style="primary",
                        ),
                        InlineKeyboardButton(
                            text="🔍 Inline",
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
            f"📤 <b>Archive Media</b>\n"
            f"<blockquote>Drop or forward any file, photo, audio, video, or album here.\n"
            f"Add <code>#tags</code> in caption to auto-organize.</blockquote>"
        )
        upload_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎒 New Pack",
                        callback_data="pk:new",
                        style="primary",
                    ),
                    InlineKeyboardButton(text="📁 Folder", callback_data="nav:groups"),
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


@router.message(ShareUnlockStates.waiting_for_file_pin)
async def fsm_unlock_shared_file(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not message.from_user or not message.text:
        return
    data = await state.get_data()
    token = data.get("unlock_file_token", "")
    pin_attempt = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    file_row = await db.get_file_by_share_token(token)
    if not file_row:
        await state.clear()
        await message.answer("⚠️ Link expired or revoked.")
        return

    if not verify_pin(pin_attempt, file_row.get("share_pin_hash") or ""):
        await message.answer(
            "❌ <b>Wrong PIN</b> — try again:",
            reply_markup=cancel_kb("nav:home"),
        )
        return

    await state.clear()
    owner = await db.get_user(file_row["uploader_user_id"])
    visitor = await db.get_user(message.from_user.id)
    ttl = int((owner or {}).get("auto_delete_ttl") or (visitor or {}).get("auto_delete_ttl") or 0)
    protect = bool(file_row.get("protect_share")) or bool((owner or {}).get("protect_content"))
    sent = await send_stored_media(
        bot, message.chat.id, file_row, protect_content=protect, auto_delete_ttl=ttl
    )
    if sent:
        await db.increment_use_count(file_row["id"])


@router.message(ShareUnlockStates.waiting_for_pack_pin)
async def fsm_unlock_shared_pack(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not message.from_user or not message.text:
        return
    data = await state.get_data()
    token = data.get("unlock_pack_token", "")
    pin_attempt = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    pack = await db.get_pack_by_token(token)
    if not pack:
        await state.clear()
        await message.answer("⚠️ Pack expired or removed.")
        return

    if not verify_pin(pin_attempt, pack.get("pin_hash") or ""):
        await message.answer(
            "❌ <b>Wrong PIN</b> — try again:",
            reply_markup=cancel_kb("nav:home"),
        )
        return

    await state.clear()
    creator = await db.get_user(pack["creator_user_id"])
    visitor = await db.get_user(message.from_user.id)
    ttl = int((creator or {}).get("auto_delete_ttl") or (visitor or {}).get("auto_delete_ttl") or 0)
    await deliver_pack_to_chat(bot, message.chat.id, pack, db, auto_delete_ttl=ttl)


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
    await callback.answer("⚡ Bulk ON" if enabled else "Bulk OFF")


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
            "ℹ️ Nothing active.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn("🏠 Vault")]]),
        )
        return
    await state.clear()
    await message.answer(
        "✕ <b>Cancelled</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn("🏠 Vault")]]),
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
    elif target.startswith("pk:list:"):
        pg = int(target.split(":")[2])
        text, kb = await render_packs_list(callback.from_user.id, db, pg)
        await safe_edit_or_send(callback, text, kb)
    elif target.startswith("pk:open:"):
        pid = int(target.split(":")[2])
        from handlers.packs import render_pack_detail

        rendered = await render_pack_detail(callback.from_user.id, pid, db, bot)
        if rendered:
            await safe_edit_or_send(callback, rendered[0], rendered[1])
    elif target.startswith("br:"):
        from handlers.file_actions import render_browse_page

        _, flt, sort_key, gid_s, page_s = target.split(":")
        text, kb = await render_browse_page(
            callback.from_user.id, flt, sort_key, int(gid_s), int(page_s), db
        )
        await safe_edit_or_send(callback, text, kb)
    elif target.startswith("f:link:"):
        fid = int(target.split(":")[2])
        f = await db.get_file(fid)
        if f:
            me = await bot.get_me()
            from handlers.file_actions import _render_file_link_card

            await safe_edit_or_send(
                callback,
                _render_file_link_card(f, me.username or ""),
                build_file_link_keyboard(f, bot_username=me.username or ""),
            )
    elif target.startswith("f:manage:"):
        fid = int(target.split(":")[2])
        f = await db.get_file(fid)
        if f:
            me = await bot.get_me()
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
            "⚡ <b>Bulk Mode: ON</b>\n<blockquote>Drop multiple files for compact 1-line saves.</blockquote>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="⬜ Bulk Off", callback_data="set:bulk"),
                        home_btn(),
                    ]
                ]
            ),
        )
    else:
        await message.answer(
            "✅ <b>Bulk Mode: OFF</b>",
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
    await callback.answer("⚡ Bulk ON" if enabled else "Bulk OFF")


@router.callback_query(F.data == "set:prot")
async def cb_settings_protect(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    enabled = await db.toggle_protect_content(callback.from_user.id)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer(
        "🛡️ Content Protection ON (No Forward/Save)"
        if enabled
        else "Content Protection OFF"
    )


@router.callback_query(F.data == "set:ttl")
async def cb_settings_ttl(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    new_ttl = await db.cycle_auto_delete_ttl(callback.from_user.id)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer(f"⏱ Auto-Delete: {format_ttl_short(new_ttl)}")


@router.callback_query(F.data == "set:sort")
async def cb_settings_sort(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    user = await db.get_user(callback.from_user.id)
    nxt = next_sort_key(user.get("default_sort") or "new")
    await db.set_default_sort(callback.from_user.id, nxt)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer(f"Sort: {SORT_LABELS[nxt]}")


@router.callback_query(F.data == "set:clear_grp")
async def cb_settings_clear_grp(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.set_active_group(callback.from_user.id, None)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb)
    await callback.answer("Target reset")
