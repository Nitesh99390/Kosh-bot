import time
from typing import Optional

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
from utils.cache import TTLCache
from utils.formatters import (
    EFFECT_FIRE,
    EFFECT_PARTY,
    MEDIA_ICONS,
    MEDIA_LABELS,
    SORT_LABELS,
    back_btn,
    breadcrumb,
    build_contact_owner_kb,
    build_file_card_text,
    build_file_keyboard,
    build_file_link_keyboard,
    build_file_manage_keyboard,
    build_trial_expiry_notice_text,
    build_trial_locked_card,
    cancel_kb,
    esc,
    format_grant_badge,
    format_size,
    format_ttl_short,
    home_btn,
    next_sort_key,
    progress_bar,
    safe_edit_or_send,
    send_stored_media,
    send_with_effect,
    tg_retry_call,
    toggle_label,
)
from utils.middleware import check_force_sub, invalidate_force_sub_cache, pop_pending_force_sub_start
from utils.states import SettingsStates, ShareUnlockStates

router = Router(name="start_help")

# Bounded anti-brute-force PIN limiter: (user_id, token) -> {"fails": int, "locked_until": float}
PIN_MAX_ATTEMPTS = 5
PIN_LOCKOUT_SECONDS = 300
_PIN_FAIL_TRACKER: TTLCache[tuple[int, str], dict[str, float]] = TTLCache(
    maxsize=4096, ttl=float(PIN_LOCKOUT_SECONDS * 2)
)

# Bounded duplicate Full Access Request cooldown: (user_id, link_type, link_id) -> monotonic timestamp
ACCESS_REQUEST_COOLDOWN_SECONDS = 600.0  # 10 minutes
_ACCESS_REQUEST_COOLDOWN: TTLCache[tuple[int, str, int], float] = TTLCache(
    maxsize=4096, ttl=ACCESS_REQUEST_COOLDOWN_SECONDS
)


def reset_access_request_cooldowns() -> None:
    """Clears in-memory Full Access request rate-limit entries (useful in unit tests)."""
    _ACCESS_REQUEST_COOLDOWN.clear()


def _check_access_request_cooldown(user_id: int, link_type: str, link_id: int) -> int:
    key = (int(user_id), link_type, int(link_id))
    sent_mono = _ACCESS_REQUEST_COOLDOWN.get(key)
    if sent_mono is None:
        return 0
    elapsed = time.monotonic() - sent_mono
    rem = int(ACCESS_REQUEST_COOLDOWN_SECONDS - elapsed)
    return max(1, rem) if elapsed < ACCESS_REQUEST_COOLDOWN_SECONDS else 0


def _mark_access_request_sent(user_id: int, link_type: str, link_id: int) -> None:
    key = (int(user_id), link_type, int(link_id))
    _ACCESS_REQUEST_COOLDOWN.set(
        key, time.monotonic(), ttl=ACCESS_REQUEST_COOLDOWN_SECONDS
    )


def _check_pin_lockout(user_id: int, token: str) -> int:
    key = (user_id, token)
    entry = _PIN_FAIL_TRACKER.get(key)
    if not entry:
        return 0
    rem = int(entry.get("locked_until", 0) - time.time())
    if rem <= 0:
        if entry.get("locked_until", 0) > 0:
            _PIN_FAIL_TRACKER.invalidate(key)
        return 0
    return rem


def _record_pin_failure(user_id: int, token: str) -> tuple[int, int]:
    key = (user_id, token)
    entry = dict(_PIN_FAIL_TRACKER.get(key) or {"fails": 0, "locked_until": 0.0})
    entry["fails"] = int(entry.get("fails", 0)) + 1
    if entry["fails"] >= PIN_MAX_ATTEMPTS:
        entry["locked_until"] = time.time() + PIN_LOCKOUT_SECONDS
        entry["fails"] = 0
        _PIN_FAIL_TRACKER.set(key, entry, ttl=float(PIN_LOCKOUT_SECONDS + 30))
        return 0, PIN_LOCKOUT_SECONDS
    _PIN_FAIL_TRACKER.set(key, entry, ttl=float(PIN_LOCKOUT_SECONDS * 2))
    return PIN_MAX_ATTEMPTS - int(entry["fails"]), 0


def _clear_pin_failures(user_id: int, token: str) -> None:
    _PIN_FAIL_TRACKER.invalidate((user_id, token))


def _diagnose_file_unavailable(raw_file: dict | None) -> tuple[str, str]:
    now = int(time.time())
    if not raw_file:
        return (
            "⚠️ <b>Link Invalid or Disabled</b>",
            "This share link was removed or disabled by the owner.",
        )
    if raw_file.get("share_paused"):
        return (
            "⏸ <b>Link Temporarily Paused</b>",
            "The owner has paused this link right now. Please try again later.",
        )
    exp = raw_file.get("share_expires_at")
    if exp and int(exp) > 0 and int(exp) < now:
        return (
            "⏳ <b>Link Expired</b>",
            "The validity period of this share link has ended.",
        )
    max_v = int(raw_file.get("share_max_views") or 0)
    cur_v = int(raw_file.get("share_views") or 0)
    if max_v > 0 and cur_v >= max_v:
        return (
            "🚫 <b>Access Limit Reached</b>",
            f"This link reached its maximum open limit ({cur_v}/{max_v}).",
        )
    return (
        "⚠️ <b>Link Unavailable</b>",
        "This file is currently unavailable.",
    )


def _diagnose_pack_unavailable(raw_pack: dict | None) -> tuple[str, str]:
    now = int(time.time())
    if not raw_pack or raw_pack.get("item_count", 0) == 0:
        return (
            "⚠️ <b>Pack Unavailable</b>",
            "This pack link was removed, emptied, or disabled.",
        )
    if raw_pack.get("is_paused"):
        return (
            "⏸ <b>Pack Link Paused</b>",
            "The owner has paused this pack link right now.",
        )
    exp = raw_pack.get("expires_at")
    if exp and int(exp) > 0 and int(exp) < now:
        return (
            "⏳ <b>Pack Link Expired</b>",
            "The validity period of this pack link has ended.",
        )
    max_v = int(raw_pack.get("max_views") or 0)
    cur_v = int(raw_pack.get("views") or 0)
    if max_v > 0 and cur_v >= max_v:
        return (
            "🚫 <b>Pack Limit Reached</b>",
            f"This pack reached its maximum open limit ({cur_v}/{max_v}).",
        )
    return (
        "⚠️ <b>Pack Unavailable</b>",
        "This pack link is no longer active.",
    )


async def render_home_dashboard(
    user_id: int,
    first_name: str,
    db: Database,
    bot: Optional[Bot] = None,
    bot_username: str = "KoshBox_Bot",
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Super Clean, Simple & Beginner-Friendly Vault Dashboard:
    - Clear welcome greeting + neat 2-line summary card
    - Balanced 2-column symmetric 6-button grid (max 2 buttons per row, clear action verbs)
    """
    user = await db.ensure_user(user_id, "", first_name) if not await db.get_user(user_id) else await db.get_user(user_id)
    assert user is not None
    stats = await db.get_vault_stats(user["vault_id"])

    active_folder = "Personal Vault"
    if user.get("active_group_id"):
        grp = await db.get_group(user["active_group_id"])
        if grp:
            active_folder = f"{'👥' if grp['is_shared'] else '📁'} {grp['name']}"

    mode_badges: list[str] = []
    if user.get("default_one_time"):
        mode_badges.append("👤 1× Preview")
    if user.get("protect_content"):
        mode_badges.append("🛡️ No-Forward")
    if user.get("auto_delete_ttl"):
        mode_badges.append(f"⏱ Auto-Del {format_ttl_short(user['auto_delete_ttl'])}")
    if user.get("bulk_mode"):
        mode_badges.append("⚡ Quiet Mode")
    mode_str = " · ".join(mode_badges) if mode_badges else "Standard (No extra locks)"

    total_files = stats["total_files"]
    total_size = format_size(stats["total_size"])
    packs_cnt = stats.get("packs_count", 0)
    uname = esc(bot_username or "KoshBox_Bot")

    greeting = f"👋 Welcome, <b>{esc(first_name[:22])}</b>" if first_name else "☁️ <b>KoshBox Vault</b>"

    if total_files == 0 and packs_cnt == 0:
        text = (
            f"✨ <b>KoshBox Cloud Vault</b>\n"
            f"{greeting} — your private cloud library is ready.\n"
            f"<blockquote>"
            f"<b>How to start in 30 seconds:</b>\n"
            f"1️⃣ <b>Save a File</b> — Send or forward any media here.\n"
            f"2️⃣ <b>Auto-Pack</b> — Drop 2+ files together for 1 shareable link.\n"
            f"3️⃣ <b>Share Anywhere</b> — Type <code>@{uname}</code> in any chat."
            f"</blockquote>\n"
            f"💡 <i>Just drop a file right here, or choose an option below:</i>"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📤 Upload First File",
                        callback_data="nav:upload",
                        style="primary",
                    ),
                    InlineKeyboardButton(
                        text="📖 How It Works",
                        callback_data="nav:help",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🎒 Create a Pack",
                        callback_data="pk:new",
                    ),
                    InlineKeyboardButton(
                        text="📁 Folders",
                        callback_data="nav:groups",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="⚙️ Settings &amp; Locks",
                        callback_data="nav:settings",
                    ),
                ],
            ]
        )
        return text, kb

    text = (
        f"✨ <b>KoshBox</b> · {greeting}\n"
        f"<blockquote>"
        f"📦 <b>{total_files}</b> Files <i>({total_size})</i>  •  🎒 <b>{packs_cnt}</b> Packs\n"
        f"📥 <b>Auto-Save:</b> {esc(active_folder)}\n"
        f"🛡️ <b>Defaults:</b> {mode_str}"
        f"</blockquote>\n"
        f"💡 <i>Drop any file to save it, or send 2+ files together to auto-pack:</i>"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📦 My Files ({total_files})",
                    callback_data="br:all:def:0:0",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text=f"🎒 Packs ({packs_cnt})",
                    callback_data="nav:packs",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📁 Folders",
                    callback_data="nav:groups",
                ),
                InlineKeyboardButton(
                    text="🔍 Inline Search",
                    switch_inline_query_current_chat="",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="➕ Upload / Pack",
                    callback_data="nav:upload",
                ),
                InlineKeyboardButton(
                    text="⚙️ Settings",
                    callback_data="nav:settings",
                ),
            ],
        ]
    )
    return text, kb


def render_upload_hub_card() -> tuple[str, InlineKeyboardMarkup]:
    """Crystal-clear, beginner-friendly Upload & Auto-Pack Hub card."""
    caption = (
        f"{breadcrumb('🏠 Home', '📤 Upload & Pack')}\n"
        f"📤 <b>Upload &amp; Bundle Files</b>\n"
        f"<blockquote>"
        f"1️⃣ <b>Single File</b> — Send or forward any media to save privately.\n"
        f"2️⃣ <b>Auto-Pack (2+ Files)</b> — Forward multiple files together to get <b>1 Pack Link</b> in exact 1→N order.\n"
        f"3️⃣ <b>Custom Pack</b> — Tap <b>➕ Build New Pack</b> to collect files across multiple chats.\n"
        f"🏷️ <b>Tip:</b> Add <code>#tags</code> in captions to find files faster."
        f"</blockquote>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Build New Pack",
                    callback_data="pk:new",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📁 Set Upload Folder",
                    callback_data="nav:groups",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📦 My Files",
                    callback_data="br:all:def:0:0",
                ),
                home_btn("🏠 Home"),
            ],
        ]
    )
    return caption, kb


def _render_help_panel(
    bot_username: str, help_tab: str = "inline"
) -> tuple[str, InlineKeyboardMarkup]:
    uname = esc(bot_username or "KoshBox_Bot")

    if help_tab == "commands":
        text = (
            f"{breadcrumb('🏠 Home', '📖 Help', '⌨️ Commands')}\n"
            f"⌨️ <b>Commands &amp; Shortcuts</b>\n"
            f"<blockquote>"
            f"<b>Vault &amp; Files</b>\n"
            f"• /start — Home Dashboard\n"
            f"• /files — Browse, filter &amp; sort files\n"
            f"• /search — Search by title, artist, or #tag\n"
            f"• /trash — Restore or permanently delete\n\n"
            f"<b>Packs &amp; Sharing</b>\n"
            f"• /packs — Manage multi-file Pack Links\n"
            f"• /batch — Collect files into a custom pack\n"
            f"• /groups — Private folders &amp; shared spaces\n"
            f"• /settings — Link security &amp; preferences"
            f"</blockquote>"
        )
    elif help_tab == "security":
        text = (
            f"{breadcrumb('🏠 Home', '📖 Help', '🔒 Link Locks')}\n"
            f"🔒 <b>Link Protection Explained</b>\n"
            f"<blockquote>"
            f"• <b>👤 1× Preview (Trial):</b> Each visitor can open the link once; repeat taps ask for Full Access.\n"
            f"• <b>⏱ Auto-Delete:</b> Clears delivered files from the visitor's chat after your timer ends.\n"
            f"• <b>🛡️ Block Forwarding:</b> Prevents visitors from saving or forwarding files.\n"
            f"• <b>🔑 PIN Lock:</b> Requires a password before opening.\n"
            f"• <b>🔓 Full Access Grant:</b> Approve a visitor for permanent, timer-free access."
            f"</blockquote>"
        )
    else:
        text = (
            f"{breadcrumb('🏠 Home', '📖 Help', '✨ Basics')}\n"
            f"✨ <b>How to Use KoshBox</b>\n"
            f"<blockquote>"
            f"<b>1. Save:</b> Send or forward any media here — it's stored privately.\n"
            f"<b>2. Auto-Pack:</b> Forward 2+ files together to get 1 ordered Pack Link.\n"
            f"<b>3. Share Anywhere:</b> Type <code>@{uname}</code> in any chat:\n"
            f"  • <code>@{uname} song</code> — Search by name\n"
            f"  • <code>@{uname} t:video</code> — Filter (<code>audio</code>, <code>doc</code>, <code>photo</code>, <code>gif</code>)\n"
            f"  • <code>@{uname} fav</code> or <code>packs</code> — Starred / Packs"
            f"</blockquote>"
        )

    tab_basics = "● Basics" if help_tab not in ("commands", "security") else "○ Basics"
    tab_sec = "● Locks" if help_tab == "security" else "○ Locks"
    tab_cmd = "● Commands" if help_tab == "commands" else "○ Commands"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=tab_basics, callback_data="help:inline"),
                InlineKeyboardButton(text=tab_sec, callback_data="help:security"),
                InlineKeyboardButton(text=tab_cmd, callback_data="help:commands"),
            ],
            [
                InlineKeyboardButton(
                    text="🔍 Try Inline Search",
                    switch_inline_query_current_chat="",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📤 Upload Files",
                    callback_data="nav:upload",
                ),
            ],
            [back_btn("nav:settings", "⬅️ Settings"), home_btn()],
        ]
    )
    return text, kb


async def _render_stats_panel(user_id: int, db: Database) -> tuple[str, InlineKeyboardMarkup]:
    user = await db.get_user(user_id)
    assert user is not None
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
        breakdown = "<i>No files uploaded yet.</i>"

    summary_block = (
        f"📦 <b>{total_files}</b> Files  •  💾 <b>{format_size(stats['total_size'])}</b>  •  🎒 <b>{stats.get('packs_count', 0)}</b> Packs\n"
        f"⭐ <b>{stats['fav_count']}</b> Starred  •  📤 <b>{stats['total_uses']}×</b> Sent  •  📁 <b>{stats['groups_count']}</b> Folders"
    )

    lines = [
        breadcrumb("🏠 Home", "⚙️ Settings", "📊 Analytics"),
        "📊 <b>Vault Analytics</b>",
        f"<blockquote>{summary_block}</blockquote>",
        breakdown,
    ]

    if stats["top_files"]:
        top_lines = [
            f"{idx}. {MEDIA_ICONS.get(tf['media_type'], '📦')} <b>{esc(tf['title'][:26])}</b> · <code>{tf['use_count']}×</code>"
            for idx, tf in enumerate(stats["top_files"], start=1)
        ]
        lines.append("<b>🔥 Most Used Files</b>\n" + "\n".join(top_lines))

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 My Files", callback_data="br:all:def:0:0", style="primary"),
                InlineKeyboardButton(text="🔄 Refresh", callback_data="nav:stats"),
            ],
            [back_btn("nav:settings", "⬅️ Settings"), home_btn()],
        ]
    )
    return "\n".join(lines), kb


async def _render_settings_panel(user_id: int, db: Database) -> tuple[str, InlineKeyboardMarkup]:
    """
    Clean, well-grouped Settings & Security Hub:
    Every toggle clearly shows ON / OFF or its current value so there is zero confusion.
    """
    user = await db.get_user(user_id)
    assert user is not None
    trash_cnt = await db.count_trashed_files(user["vault_id"])
    sort_key = user.get("default_sort") or "new"
    sort_label = SORT_LABELS.get(sort_key, "Newest")
    bulk_on = bool(user.get("bulk_mode"))
    prot_on = bool(user.get("protect_content"))
    one_time_on = bool(user.get("default_one_time"))
    ttl_val = int(user.get("auto_delete_ttl") or 0)
    ttl_lbl = format_ttl_short(ttl_val)

    saved_contact = (user.get("support_contact") or "").strip()
    resolved_contact = await db.resolve_owner_contact_url(
        user["vault_id"], preferred_user_id=user_id
    )
    if saved_contact:
        contact_short = saved_contact.replace("https://t.me/", "@")
        contact_badge = f"<code>{esc(contact_short[:26])}</code>"
    elif resolved_contact:
        auto_short = resolved_contact.replace("https://t.me/", "@")
        contact_badge = f"<i>Auto ({esc(auto_short[:22])})</i>"
    else:
        contact_badge = "<i>Not set</i>"

    active_folder = "Personal Vault"
    if user.get("active_group_id"):
        grp = await db.get_group(user["active_group_id"])
        if grp:
            active_folder = f"{'👥' if grp['is_shared'] else '📁'} {grp['name']}"

    text = (
        f"{breadcrumb('🏠 Home', '⚙️ Settings')}\n"
        f"⚙️ <b>Settings &amp; Link Protection</b>\n"
        f"<blockquote>"
        f"<b>🔒 Share Link Defaults</b> <i>(for new links)</i>\n"
        f"• <b>One-Time Preview:</b> {'🟢 ON' if one_time_on else '⚪ OFF'}  •  <b>No-Forward:</b> {'🟢 ON' if prot_on else '⚪ OFF'}\n"
        f"• <b>Auto-Delete Timer:</b> {ttl_lbl}  •  <b>Owner Contact:</b> {contact_badge}\n\n"
        f"<b>🎛️ Vault Preferences</b>\n"
        f"• <b>Quiet Upload:</b> {'🟢 ON' if bulk_on else '⚪ OFF'}  •  <b>Default Sort:</b> {sort_label}\n"
        f"• <b>Upload Folder:</b> {esc(active_folder)}"
        f"</blockquote>\n"
        f"<i>Tap any button below to toggle or configure:</i>"
    )

    contact_btn_lbl = "💬 Owner Contact ✓" if saved_contact else "💬 Set Owner Contact"

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"{'🟢' if one_time_on else '⚪'} 1× Preview: {'ON' if one_time_on else 'OFF'}",
                callback_data="set:1x",
                style="success" if one_time_on else None,
            ),
            InlineKeyboardButton(
                text=f"{'🟢' if prot_on else '⚪'} No-Forward: {'ON' if prot_on else 'OFF'}",
                callback_data="set:prot",
                style="success" if prot_on else None,
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"⏱ Auto-Delete: {ttl_lbl}",
                callback_data="set:ttl",
                style="success" if ttl_val > 0 else None,
            ),
            InlineKeyboardButton(
                text=contact_btn_lbl,
                callback_data="set:contact",
                style="primary" if not saved_contact else None,
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{'🟢' if bulk_on else '⚪'} Quiet Upload: {'ON' if bulk_on else 'OFF'}",
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
            InlineKeyboardButton(text="📖 Help Guide", callback_data="nav:help"),
        ],
    ]
    if user.get("active_group_id"):
        rows.append(
            [
                InlineKeyboardButton(text="🎯 Reset Upload Folder", callback_data="set:clear_grp"),
                home_btn("🏠 Home"),
            ]
        )
    else:
        rows.append([home_btn("🏠 Back to Home")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "fsub:check")
async def cb_force_sub_verify(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    if config.force_sub_channel:
        invalidate_force_sub_cache(config.force_sub_channel, callback.from_user.id)
        ok = await check_force_sub(
            bot, config.force_sub_channel, callback.from_user.id, bypass_cache=True
        )
        if not ok:
            await callback.answer("Still not joined — please join the channel first!", show_alert=True)
            return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    pending_args = pop_pending_force_sub_start(callback.from_user.id)
    if pending_args and isinstance(callback.message, Message):
        try:
            await callback.answer("✅ Membership verified!")
        except Exception:
            pass
        try:
            await callback.message.delete()
        except Exception:
            pass
        fake_msg = callback.message.model_copy(update={"from_user": callback.from_user})
        fake_cmd = CommandObject(prefix="/", command="start", args=pending_args)
        await cmd_start(fake_msg, fake_cmd, state, db, bot, config, bot_username=bot_username)
        return
    text, kb = await render_home_dashboard(
        callback.from_user.id,
        callback.from_user.first_name or "",
        db,
        bot,
        bot_username=bot_username,
    )
    await edit_or_send_banner(
        callback,
        "welcome",
        text,
        kb,
        bot,
        db,
        config,
        answer_text="✅ Membership verified!",
        also_ack=True,
    )


async def _deliver_shared_file_with_claim_check(
    message: Message,
    visitor: dict,
    file_row: dict,
    db: Database,
    bot: Bot,
) -> None:
    owner = await db.get_user(file_row["uploader_user_id"])
    is_owner = (
        visitor["user_id"] == file_row["uploader_user_id"]
        or visitor["vault_id"] == file_row["vault_id"]
    )

    if is_owner:
        sent_owner = await send_stored_media(
            bot,
            message.chat.id,
            file_row,
            protect_content=False,
            auto_delete_ttl=0,
            db=db,
            extra_note="🔓 Owner Access — permanent & unrestricted",
        )
        if sent_owner:
            await db.increment_use_count(file_row["id"])
        else:
            await message.answer("⚠️ Unsupported media format.")
        return

    grant = await db.get_active_access_grant("file", file_row["id"], visitor["user_id"])
    has_full_access = grant is not None
    contact_url = await db.resolve_owner_contact_url(
        file_row["vault_id"], preferred_user_id=file_row["uploader_user_id"]
    )
    req_cb = f"req:f:{file_row['id']}"

    claimed_this_call = False
    if not has_full_access and file_row.get("share_one_time"):
        claimed_now = await db.try_claim_link("file", file_row["id"], visitor["user_id"])
        if not claimed_now:
            locked_txt, locked_kb = build_trial_locked_card(
                contact_url,
                title=file_row.get("title") or "",
                request_access_cb=req_cb,
            )
            await message.answer(
                locked_txt, reply_markup=locked_kb, disable_web_page_preview=True
            )
            return
        claimed_this_call = True

    if not has_full_access and int(file_row.get("share_max_views") or 0) > 0:
        reserved = await db.try_increment_file_share_views(file_row["id"])
        if not reserved:
            if claimed_this_call:
                await db.release_link_claim("file", file_row["id"], visitor["user_id"])
            await message.answer(
                "🔒 <b>View Limit Reached</b>\n<blockquote>This link has reached its maximum view count.</blockquote>"
            )
            return
        view_already_counted = True
    else:
        view_already_counted = False

    raw_ttl = await db.resolve_file_auto_delete_ttl(file_row)
    ttl = 0 if has_full_access else raw_ttl
    protect = bool(file_row.get("protect_share")) or bool((owner or {}).get("protect_content"))
    is_trial_or_protected = (not has_full_access) and (
        ttl > 0 or bool(file_row.get("share_one_time")) or protect
    )
    visitor_cta_kb = (
        build_contact_owner_kb(contact_url, request_access_cb=req_cb)
        if is_trial_or_protected
        else None
    )
    expiry_notice = (
        build_trial_expiry_notice_text(file_row.get("title") or "File", ttl)
        if ttl > 0
        else ""
    )
    grant_note = (
        f"🔓 {format_grant_badge(grant['expires_at'])}" if grant else ""
    )

    sent = await send_stored_media(
        bot,
        message.chat.id,
        file_row,
        protect_content=protect,
        auto_delete_ttl=ttl,
        db=db,
        notify_text=expiry_notice,
        contact_url=contact_url,
        message_effect_id=EFFECT_FIRE,
        reply_markup=visitor_cta_kb,
        extra_note=grant_note,
        link_type="file",
        link_id=int(file_row["id"]),
    )
    if sent:
        if not view_already_counted:
            await db.increment_file_share_views(file_row["id"])
    else:
        if claimed_this_call:
            await db.release_link_claim("file", file_row["id"], visitor["user_id"])
        await message.answer("⚠️ Unsupported media format.")


async def _deliver_shared_pack_with_claim_check(
    message: Message,
    visitor: dict,
    pack: dict,
    db: Database,
    bot: Bot,
    config: Optional[Config] = None,
) -> None:
    is_owner = (
        visitor["user_id"] == pack["creator_user_id"]
        or visitor["vault_id"] == pack["vault_id"]
    )
    if is_owner:
        await deliver_pack_to_chat(
            bot,
            message.chat.id,
            pack,
            db,
            auto_delete_ttl=0,
            force_protect=False,
            is_owner=True,
            count_view=False,
            dump_channel_id=config.dump_channel_id if config else None,
            celebrate=False,
        )
        return

    grant = await db.get_active_access_grant("pack", pack["id"], visitor["user_id"])
    has_full_access = grant is not None
    creator = await db.get_user(pack["creator_user_id"])
    contact_url = await db.resolve_owner_contact_url(
        pack["vault_id"], preferred_user_id=pack["creator_user_id"]
    )
    req_cb = f"req:p:{pack['id']}"

    claimed_this_call = False
    if not has_full_access and pack.get("one_time_per_user"):
        claimed_now = await db.try_claim_link("pack", pack["id"], visitor["user_id"])
        if not claimed_now:
            locked_txt, locked_kb = build_trial_locked_card(
                contact_url,
                title=pack.get("title") or "",
                request_access_cb=req_cb,
            )
            await message.answer(
                locked_txt, reply_markup=locked_kb, disable_web_page_preview=True
            )
            return
        claimed_this_call = True

    count_view_in_deliver = True
    if not has_full_access and int(pack.get("max_views") or 0) > 0:
        reserved = await db.try_increment_pack_views(pack["id"])
        if not reserved:
            if claimed_this_call:
                await db.release_link_claim("pack", pack["id"], visitor["user_id"])
            await message.answer(
                "🔒 <b>View Limit Reached</b>\n<blockquote>This pack link has reached its maximum view count.</blockquote>"
            )
            return
        count_view_in_deliver = False

    raw_ttl = await db.resolve_pack_auto_delete_ttl(pack)
    ttl = 0 if has_full_access else raw_ttl
    force_protect = bool((creator or {}).get("protect_content"))
    is_trial_or_protected = (not has_full_access) and (
        ttl > 0
        or bool(pack.get("one_time_per_user"))
        or bool(pack.get("protect_content"))
        or force_protect
    )
    delivered = await deliver_pack_to_chat(
        bot,
        message.chat.id,
        pack,
        db,
        auto_delete_ttl=ttl,
        force_protect=force_protect,
        is_owner=False,
        has_full_access=has_full_access,
        access_badge=format_grant_badge(grant["expires_at"]) if grant else "",
        show_request_cta=is_trial_or_protected,
        count_view=count_view_in_deliver,
        dump_channel_id=config.dump_channel_id if config else None,
        contact_url=contact_url,
        celebrate=True,
    )
    if delivered == 0 and claimed_this_call:
        await db.release_link_claim("pack", pack["id"], visitor["user_id"])


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    db: Database,
    bot: Bot,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not message.from_user:
        return
    await state.clear()
    pre_existing = await db.get_user(message.from_user.id)
    is_first_welcome = pre_existing is None
    user = await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    args = (command.args or "").strip()

    # Deep-link: shared single file
    if args.startswith("file_"):
        token = args[5:].strip()
        raw_file = await db.get_file_by_share_token(token, include_unavailable=True)
        if not raw_file:
            hdr, reason = _diagnose_file_unavailable(None)
            await message.answer(
                f"{hdr}\n<blockquote>{reason}</blockquote>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
            )
            return

        is_owner = (
            user["user_id"] == raw_file["uploader_user_id"]
            or user["vault_id"] == raw_file["vault_id"]
        )
        has_full_access = (
            False
            if is_owner
            else await db.has_active_access_grant("file", raw_file["id"], user["user_id"])
        )

        file_row = (
            raw_file
            if (is_owner or has_full_access)
            else await db.get_file_by_share_token(token)
        )
        if not file_row:
            hdr, reason = _diagnose_file_unavailable(raw_file)
            contact_url = await db.resolve_owner_contact_url(
                raw_file["vault_id"], preferred_user_id=raw_file["uploader_user_id"]
            )
            unavail_kb = build_contact_owner_kb(
                contact_url, request_access_cb=f"req:f:{raw_file['id']}"
            )
            await message.answer(
                f"{hdr}\n<blockquote>{reason}</blockquote>",
                reply_markup=unavail_kb,
                disable_web_page_preview=True,
            )
            return

        if (
            not is_owner
            and not has_full_access
            and file_row.get("share_one_time")
            and await db.has_claimed_link("file", file_row["id"], user["user_id"])
        ):
            contact_url = await db.resolve_owner_contact_url(
                file_row["vault_id"], preferred_user_id=file_row["uploader_user_id"]
            )
            locked_txt, locked_kb = build_trial_locked_card(
                contact_url,
                title=file_row.get("title") or "",
                request_access_cb=f"req:f:{file_row['id']}",
            )
            await message.answer(
                locked_txt, reply_markup=locked_kb, disable_web_page_preview=True
            )
            return

        if file_row.get("share_pin_hash") and not is_owner and not has_full_access:
            lock_rem = _check_pin_lockout(message.from_user.id, f"f:{token}")
            if lock_rem > 0:
                await message.answer(
                    f"⛔ <b>Too Many Wrong Attempts</b>\n"
                    f"<blockquote>Please wait <b>{format_ttl_short(lock_rem)}</b> before trying again.</blockquote>"
                )
                return
            await state.set_state(ShareUnlockStates.waiting_for_file_pin)
            await state.update_data(unlock_file_token=token)
            await message.answer(
                f"🔒 <b>Password / PIN Required</b>\n"
                f"<blockquote>Send the PIN to unlock <b>{esc(file_row['title'][:36])}</b>:</blockquote>",
                reply_markup=cancel_kb("nav:home"),
            )
            return

        await _deliver_shared_file_with_claim_check(message, user, file_row, db, bot)
        return

    # Deep-link: shared multi-file Pack
    if args.startswith("pack_"):
        token = args[5:].strip()
        raw_pack = await db.get_pack_by_token(token, include_unavailable=True)
        if not raw_pack or raw_pack.get("item_count", 0) == 0:
            hdr, reason = _diagnose_pack_unavailable(raw_pack)
            await message.answer(
                f"{hdr}\n<blockquote>{reason}</blockquote>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
            )
            return

        is_owner = (
            user["user_id"] == raw_pack["creator_user_id"]
            or user["vault_id"] == raw_pack["vault_id"]
        )
        has_full_access = (
            False
            if is_owner
            else await db.has_active_access_grant("pack", raw_pack["id"], user["user_id"])
        )

        pack = (
            raw_pack
            if (is_owner or has_full_access)
            else await db.get_pack_by_token(token)
        )
        if not pack or pack["item_count"] == 0:
            hdr, reason = _diagnose_pack_unavailable(raw_pack)
            contact_url = await db.resolve_owner_contact_url(
                raw_pack["vault_id"], preferred_user_id=raw_pack["creator_user_id"]
            )
            unavail_kb = build_contact_owner_kb(
                contact_url, request_access_cb=f"req:p:{raw_pack['id']}"
            )
            await message.answer(
                f"{hdr}\n<blockquote>{reason}</blockquote>",
                reply_markup=unavail_kb,
                disable_web_page_preview=True,
            )
            return

        if (
            not is_owner
            and not has_full_access
            and pack.get("one_time_per_user")
            and await db.has_claimed_link("pack", pack["id"], user["user_id"])
        ):
            contact_url = await db.resolve_owner_contact_url(
                pack["vault_id"], preferred_user_id=pack["creator_user_id"]
            )
            locked_txt, locked_kb = build_trial_locked_card(
                contact_url,
                title=pack.get("title") or "",
                request_access_cb=f"req:p:{pack['id']}",
            )
            await message.answer(
                locked_txt, reply_markup=locked_kb, disable_web_page_preview=True
            )
            return

        if pack.get("pin_hash") and not is_owner and not has_full_access:
            lock_rem = _check_pin_lockout(message.from_user.id, f"p:{token}")
            if lock_rem > 0:
                await message.answer(
                    f"⛔ <b>Too Many Wrong Attempts</b>\n"
                    f"<blockquote>Please wait <b>{format_ttl_short(lock_rem)}</b> before trying again.</blockquote>"
                )
                return
            await state.set_state(ShareUnlockStates.waiting_for_pack_pin)
            await state.update_data(unlock_pack_token=token)
            await message.answer(
                f"🔒 <b>Password / PIN Required</b>\n"
                f"<blockquote>Send the PIN to unlock <b>{esc(pack['title'])}</b> ({pack['item_count']} files):</blockquote>",
                reply_markup=cancel_kb("nav:home"),
            )
            return

        await _deliver_shared_pack_with_claim_check(message, user, pack, db, bot, config)
        return

    # Deep-link: join shared group
    if args.startswith("join_"):
        invite = args[5:]
        group = await db.join_shared_group(user["vault_id"], invite)
        if not group:
            await message.answer(
                "⚠️ <b>Invalid Invite Link</b>\n<blockquote>This group invite has expired or been reset.</blockquote>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
            )
            return
        uname = bot_username or "KoshBox_Bot"
        await send_with_effect(
            message.answer,
            f"🎉 <b>Joined {esc(group['name'])}!</b> · <i>{group.get('file_count', 0)} files</i>\n"
            f"<blockquote>Search inline anytime: <code>@{esc(uname)} g:{esc(group['name'])}</code></blockquote>",
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
            message_effect_id=EFFECT_PARTY,
        )
        return

    # Deep-link: link secondary account
    if args.startswith("link_"):
        code = args[5:]
        await handle_verify_code(message, code, db, bot)
        return

    if args == "upload":
        upload_caption, upload_kb = render_upload_hub_card()
        await send_banner_message(
            message, "upload", upload_caption, upload_kb, bot, db, config
        )
        return

    text, kb = await render_home_dashboard(
        message.from_user.id,
        message.from_user.first_name or "",
        db,
        bot,
        bot_username=bot_username,
    )
    await send_banner_message(
        message,
        "welcome",
        text,
        kb,
        bot,
        db,
        config,
        message_effect_id=EFFECT_PARTY if is_first_welcome else None,
    )


@router.callback_query(F.data == "nav:upload")
async def cb_nav_upload(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    if not callback.from_user:
        return
    await state.clear()
    caption, kb = render_upload_hub_card()
    await edit_or_send_banner(
        callback, "upload", caption, kb, bot, db, config, also_ack=True
    )


@router.message(Command("upload"))
async def cmd_upload(
    message: Message,
    state: FSMContext,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    if not message.from_user:
        return
    await state.clear()
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    caption, kb = render_upload_hub_card()
    await send_banner_message(message, "upload", caption, kb, bot, db, config)


@router.message(ShareUnlockStates.waiting_for_file_pin, F.text, ~F.text.startswith('/'))
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

    lock_key = f"f:{token}"
    lock_rem = _check_pin_lockout(message.from_user.id, lock_key)
    if lock_rem > 0:
        await state.clear()
        await message.answer(
            f"⛔ <b>Locked Out</b> · Retry in <b>{format_ttl_short(lock_rem)}</b>."
        )
        return

    file_row = await db.get_file_by_share_token(token)
    if not file_row:
        await state.clear()
        raw_file = await db.get_file_by_share_token(token, include_unavailable=True)
        hdr, reason = _diagnose_file_unavailable(raw_file)
        await message.answer(f"{hdr}\n<blockquote>{reason}</blockquote>")
        return

    if not verify_pin(pin_attempt, file_row.get("share_pin_hash") or ""):
        left, locked_for = _record_pin_failure(message.from_user.id, lock_key)
        if locked_for > 0:
            await state.clear()
            await message.answer(
                f"⛔ <b>Too Many Wrong Attempts</b>\n"
                f"<blockquote>Locked for <b>{format_ttl_short(locked_for)}</b> to protect this link.</blockquote>"
            )
            return
        await message.answer(
            f"❌ <b>Wrong PIN</b> · <i>{left} attempt(s) left</i> — try again:",
            reply_markup=cancel_kb("nav:home"),
        )
        return

    _clear_pin_failures(message.from_user.id, lock_key)
    await state.clear()
    visitor = await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    await _deliver_shared_file_with_claim_check(message, visitor, file_row, db, bot)


@router.message(ShareUnlockStates.waiting_for_pack_pin, F.text, ~F.text.startswith('/'))
async def fsm_unlock_shared_pack(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    config: Optional[Config] = None,
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

    lock_key = f"p:{token}"
    lock_rem = _check_pin_lockout(message.from_user.id, lock_key)
    if lock_rem > 0:
        await state.clear()
        await message.answer(
            f"⛔ <b>Locked Out</b> · Retry in <b>{format_ttl_short(lock_rem)}</b>."
        )
        return

    pack = await db.get_pack_by_token(token)
    if not pack or pack.get("item_count", 0) == 0:
        await state.clear()
        raw_pack = await db.get_pack_by_token(token, include_unavailable=True)
        hdr, reason = _diagnose_pack_unavailable(raw_pack)
        await message.answer(f"{hdr}\n<blockquote>{reason}</blockquote>")
        return

    if not verify_pin(pin_attempt, pack.get("pin_hash") or ""):
        left, locked_for = _record_pin_failure(message.from_user.id, lock_key)
        if locked_for > 0:
            await state.clear()
            await message.answer(
                f"⛔ <b>Too Many Wrong Attempts</b>\n"
                f"<blockquote>Locked for <b>{format_ttl_short(locked_for)}</b> to protect this pack.</blockquote>"
            )
            return
        await message.answer(
            f"❌ <b>Wrong PIN</b> · <i>{left} attempt(s) left</i> — try again:",
            reply_markup=cancel_kb("nav:home"),
        )
        return

    _clear_pin_failures(message.from_user.id, lock_key)
    await state.clear()
    visitor = await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    await _deliver_shared_pack_with_claim_check(message, visitor, pack, db, bot, config)


@router.callback_query(F.data == "nav:home")
async def cb_nav_home(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    config: Config,
    bot_username: str = "KoshBox_Bot",
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
        callback.from_user.id,
        callback.from_user.first_name or "",
        db,
        bot,
        bot_username=bot_username,
    )
    await edit_or_send_banner(
        callback, "welcome", text, kb, bot, db, config, also_ack=True
    )


@router.callback_query(F.data == "home:bulk")
async def cb_home_bulk(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    config: Config,
    bot_username: str = "KoshBox_Bot",
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
        callback.from_user.id,
        callback.from_user.first_name or "",
        db,
        bot,
        bot_username=bot_username,
    )
    await edit_or_send_banner(
        callback,
        "welcome",
        text,
        kb,
        bot,
        db,
        config,
        answer_text="⚡ Silent Bulk ON" if enabled else "Silent Bulk OFF",
        also_ack=True,
    )


@router.message(Command("help"))
async def cmd_help(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> None:
    text, kb = _render_help_panel(bot_username, "inline")
    await send_banner_message(message, "guide", text, kb, bot, db, config)


@router.callback_query(F.data == "nav:help")
async def cb_nav_help(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> None:
    text, kb = _render_help_panel(bot_username, "inline")
    await edit_or_send_banner(
        callback, "guide", text, kb, bot, db, config, also_ack=True
    )


@router.callback_query(F.data.startswith("help:"))
async def cb_switch_help_tab(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> None:
    tab = callback.data.split(":", 1)[1]
    text, kb = _render_help_panel(bot_username, tab)
    await edit_or_send_banner(
        callback, "guide", text, kb, bot, db, config, also_ack=True
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if message.from_user:
        from handlers.upload import _PACK_BUILDER_BUFFER, _PACK_BUILDER_TASKS

        _PACK_BUILDER_BUFFER.pop(message.from_user.id, None)
        pend_task = _PACK_BUILDER_TASKS.pop(message.from_user.id, None)
        if pend_task and not pend_task.done():
            pend_task.cancel()
    current = await state.get_state()
    if current is None:
        await message.answer(
            "ℹ️ Nothing active right now.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn("🏠 Home")]]),
        )
        return
    await state.clear()
    await message.answer(
        "✖️ <b>Cancelled</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn("🏠 Home")]]),
    )


@router.callback_query(F.data.startswith("fsm:cancel:"))
async def cb_fsm_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    config: Config,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    from handlers.upload import _PACK_BUILDER_BUFFER, _PACK_BUILDER_TASKS

    _PACK_BUILDER_BUFFER.pop(callback.from_user.id, None)
    pend_task = _PACK_BUILDER_TASKS.pop(callback.from_user.id, None)
    if pend_task and not pend_task.done():
        pend_task.cancel()

    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    await state.clear()
    target = callback.data.split(":", 2)[2]
    uname = bot_username or "KoshBox_Bot"

    if target == "nav:settings":
        text, kb = await _render_settings_panel(callback.from_user.id, db)
        await safe_edit_or_send(
            callback, text, kb, answer_text="Cancelled", also_ack=True
        )
        return
    if target == "nav:groups":
        from handlers.groups import render_groups_panel

        text, kb = await render_groups_panel(callback.from_user.id, db)
        await safe_edit_or_send(
            callback, text, kb, answer_text="Cancelled", also_ack=True
        )
        return
    if target == "nav:accounts":
        text, kb = await render_accounts_panel(callback.from_user.id, db)
        await edit_or_send_banner(
            callback,
            "guide",
            text,
            kb,
            bot,
            db,
            config,
            answer_text="Cancelled",
            also_ack=True,
        )
        return
    if target == "adm:refresh":
        if callback.from_user.id not in config.admin_ids:
            await callback.answer("Admin only.", show_alert=True)
            return
        from handlers.admin import _render_admin_panel

        text, kb = await _render_admin_panel(db)
        await safe_edit_or_send(
            callback, text, kb, answer_text="Cancelled", also_ack=True
        )
        return
    if target.startswith("pk:list:"):
        pg = int(target.split(":")[2])
        text, kb = await render_packs_list(callback.from_user.id, db, pg)
        await safe_edit_or_send(
            callback, text, kb, answer_text="Cancelled", also_ack=True
        )
        return
    if target.startswith("pk:open:"):
        pid = int(target.split(":")[2])
        from handlers.packs import render_pack_detail

        rendered = await render_pack_detail(
            callback.from_user.id, pid, db, bot, bot_username=uname
        )
        if rendered:
            await safe_edit_or_send(
                callback,
                rendered[0],
                rendered[1],
                answer_text="Cancelled",
                also_ack=True,
            )
            return
    elif target.startswith("pk:edit:"):
        pid = int(target.split(":")[2])
        from handlers.packs import render_pack_edit_drawer

        rendered = await render_pack_edit_drawer(callback.from_user.id, pid, db, page=0)
        if rendered:
            await safe_edit_or_send(
                callback,
                rendered[0],
                rendered[1],
                answer_text="Cancelled",
                also_ack=True,
            )
            return
    elif target.startswith("br:"):
        from handlers.file_actions import render_browse_page

        br_parts = target.split(":")
        if len(br_parts) == 5:
            _, flt, sort_key, gid_s, page_s = br_parts
            text, kb = await render_browse_page(
                callback.from_user.id, flt, sort_key, int(gid_s), int(page_s), db
            )
            await safe_edit_or_send(
                callback, text, kb, answer_text="Cancelled", also_ack=True
            )
            return
    elif target.startswith("fsl:") or target.startswith("f:link:"):
        if target.startswith("fsl:"):
            sub_p = target.split(":", 2)
            fid = int(sub_p[1])
            back_ctx = sub_p[2] if len(sub_p) > 2 else None
        else:
            fid = int(target.split(":")[2])
            back_ctx = None
        caller_u = await db.get_user(callback.from_user.id)
        f = await db.get_file(fid)
        if f and caller_u and not f.get("is_trashed") and f["vault_id"] == caller_u["vault_id"]:
            from handlers.file_actions import _render_file_link_card

            claims = await db.count_link_claims("file", fid)
            owner = await db.get_user(f["uploader_user_id"])
            owner_ttl = int((owner or {}).get("auto_delete_ttl") or 0)
            await safe_edit_or_send(
                callback,
                _render_file_link_card(
                    f, uname, unique_claims=claims, owner_global_ttl=owner_ttl
                ),
                build_file_link_keyboard(f, back_cb=back_ctx, bot_username=uname),
                answer_text="Cancelled",
                also_ack=True,
            )
            return
    elif target.startswith("fm:") or target.startswith("f:manage:"):
        if target.startswith("fm:"):
            sub_p = target.split(":", 2)
            fid = int(sub_p[1])
            back_ctx = sub_p[2] if len(sub_p) > 2 else None
        else:
            fid = int(target.split(":")[2])
            back_ctx = None
        caller_u = await db.get_user(callback.from_user.id)
        f = await db.get_file(fid)
        if f and caller_u and not f.get("is_trashed") and await db.can_vault_access_file(caller_u["vault_id"], f):
            await safe_edit_or_send(
                callback,
                build_file_card_text(f, uname, edit_mode=True),
                build_file_manage_keyboard(f, back_cb=back_ctx, bot_username=uname),
                answer_text="Cancelled",
                also_ack=True,
            )
            return
    elif target.startswith("f:move:") or target.startswith("fmv:"):
        if target.startswith("fmv:"):
            sub_p = target.split(":", 2)
            fid = int(sub_p[1])
            back_ctx = f"br:{sub_p[2]}" if len(sub_p) > 2 and sub_p[2] else None
        else:
            fid = int(target.split(":")[2])
            back_ctx = None
        from handlers.file_actions import render_move_file_menu

        rendered_mv = await render_move_file_menu(callback.from_user.id, fid, db, back_cb=back_ctx)
        if rendered_mv:
            await safe_edit_or_send(
                callback,
                rendered_mv[0],
                rendered_mv[1],
                answer_text="Cancelled",
                also_ack=True,
            )
            return
    elif target.startswith("f:open:") or target.startswith("fo:"):
        if target.startswith("fo:"):
            fo_parts = target.split(":")
            fid = int(fo_parts[1])
            back_ctx = f"br:{':'.join(fo_parts[2:6])}" if len(fo_parts) >= 6 else None
        else:
            fid = int(target.split(":")[2])
            back_ctx = None
        caller_u = await db.get_user(callback.from_user.id)
        f = await db.get_file(fid)
        if f and caller_u and not f.get("is_trashed") and await db.can_vault_access_file(caller_u["vault_id"], f):
            await safe_edit_or_send(
                callback,
                build_file_card_text(f, uname),
                build_file_keyboard(f, back_cb=back_ctx, bot_username=uname),
                answer_text="Cancelled",
                also_ack=True,
            )
            return
    elif target.startswith("grp:manage:"):
        gid = int(target.split(":")[2])
        from handlers.groups import render_single_group

        rendered = await render_single_group(
            callback.from_user.id, gid, db, bot, manage_mode=True, bot_username=uname
        )
        if rendered:
            await safe_edit_or_send(
                callback,
                rendered[0],
                rendered[1],
                answer_text="Cancelled",
                also_ack=True,
            )
            return
    elif target.startswith("grp:open:"):
        gid = int(target.split(":")[2])
        from handlers.groups import render_single_group

        rendered = await render_single_group(
            callback.from_user.id, gid, db, bot, bot_username=uname
        )
        if rendered:
            await safe_edit_or_send(
                callback,
                rendered[0],
                rendered[1],
                answer_text="Cancelled",
                also_ack=True,
            )
            return

    text, kb = await render_home_dashboard(
        callback.from_user.id,
        callback.from_user.first_name or "",
        db,
        bot,
        bot_username=uname,
    )
    await edit_or_send_banner(
        callback,
        "welcome",
        text,
        kb,
        bot,
        db,
        config,
        answer_text="Cancelled",
        also_ack=True,
    )


@router.message(Command("bulk"))
async def cmd_bulk(message: Message, db: Database, state: Optional[FSMContext] = None) -> None:
    if not message.from_user:
        return
    if state is not None:
        await state.clear()
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    enabled = await db.toggle_bulk_mode(message.from_user.id)
    if enabled:
        await message.answer(
            "⚡ <b>Silent Bulk Mode: ON</b>\n<blockquote>Drop multiple files for compact 1-line confirmations.</blockquote>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="⚪ Turn Bulk OFF", callback_data="set:bulk"),
                        home_btn(),
                    ]
                ]
            ),
        )
    else:
        await message.answer(
            "✅ <b>Silent Bulk Mode: OFF</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database, state: Optional[FSMContext] = None) -> None:
    if not message.from_user:
        return
    if state is not None:
        await state.clear()
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
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.message(Command("settings"))
async def cmd_settings(message: Message, db: Database, state: Optional[FSMContext] = None) -> None:
    if not message.from_user:
        return
    if state is not None:
        await state.clear()
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    text, kb = await _render_settings_panel(message.from_user.id, db)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "nav:settings")
async def cb_nav_settings(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not callback.from_user:
        return
    await state.clear()
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data == "set:bulk")
async def cb_settings_bulk(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    enabled = await db.toggle_bulk_mode(callback.from_user.id)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(
        callback,
        text,
        kb,
        answer_text="⚡ Bulk ON" if enabled else "Bulk OFF",
        also_ack=True,
    )


@router.callback_query(F.data == "set:1x")
async def cb_settings_one_time(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    enabled = await db.toggle_default_one_time(callback.from_user.id)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(
        callback,
        text,
        kb,
        answer_text=(
            "👤 Default 1×/User Trial Lock ON"
            if enabled
            else "👤 Default 1×/User Trial Lock OFF"
        ),
        also_ack=True,
    )


@router.callback_query(F.data == "set:contact")
async def cb_settings_contact_prompt(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not callback.from_user:
        return
    user = await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    cur = (user or {}).get("support_contact") or ""
    resolved = await db.resolve_owner_contact_url(
        user["vault_id"], preferred_user_id=callback.from_user.id
    )
    cur_lbl = f"<code>{esc(cur)}</code>" if cur else (
        f"<i>Auto ({esc(resolved)})</i>" if resolved else "<i>Not set</i>"
    )
    await state.set_state(SettingsStates.waiting_for_support_contact)
    await safe_edit_or_send(
        callback,
        f"{breadcrumb('🏠 Home', '⚙️ Settings', '💬 Contact')}\n"
        f"💬 <b>Support / Owner Contact</b> · {cur_lbl}\n"
        f"<blockquote>Send your Telegram <code>@username</code> or <code>https://t.me/...</code> link shown on Trial &amp; Auto-Delete cards (or send <code>-</code> to reset):</blockquote>",
        cancel_kb("nav:settings"),
        also_ack=True,
    )


@router.message(SettingsStates.waiting_for_support_contact, F.text, ~F.text.startswith('/'))
async def fsm_save_support_contact(
    message: Message, state: FSMContext, db: Database
) -> None:
    if not message.from_user or not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") and raw.lower().split()[0] in ("/cancel", "/start"):
        await state.clear()
        return
    try:
        saved_url = await db.set_support_contact(message.from_user.id, raw)
    except ValueError:
        await message.answer(
            "⚠️ <b>Invalid Username or Link</b>\n"
            "<blockquote>Send a valid <code>@username</code> (e.g. <code>@MySupport</code>), a <code>https://t.me/...</code> link, or <code>-</code> to clear:</blockquote>",
            reply_markup=cancel_kb("nav:settings"),
        )
        return
    await state.clear()
    text, kb = await _render_settings_panel(message.from_user.id, db)
    badge = f"✅ <b>Support Contact Saved:</b> <code>{esc(saved_url)}</code>" if saved_url else "✅ <b>Support Contact Reset</b>"
    await message.answer(
        f"{badge}\n\n{text}",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "set:prot")
async def cb_settings_protect(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    enabled = await db.toggle_protect_content(callback.from_user.id)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(
        callback,
        text,
        kb,
        answer_text=(
            "🛡️ Content Protection ON (No Forward/Save)"
            if enabled
            else "Content Protection OFF"
        ),
        also_ack=True,
    )


@router.callback_query(F.data == "set:ttl")
async def cb_settings_ttl(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    new_ttl = await db.cycle_auto_delete_ttl(callback.from_user.id)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(
        callback,
        text,
        kb,
        answer_text=f"💣 Auto-Delete: {format_ttl_short(new_ttl)}",
        also_ack=True,
    )


@router.callback_query(F.data == "set:sort")
async def cb_settings_sort(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    user = await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    nxt = next_sort_key(user.get("default_sort") or "new")
    await db.set_default_sort(callback.from_user.id, nxt)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(
        callback,
        text,
        kb,
        answer_text=f"Sort: {SORT_LABELS[nxt]}",
        also_ack=True,
    )


@router.callback_query(F.data == "set:clear_grp")
async def cb_settings_clear_grp(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    await db.set_active_group(callback.from_user.id, None)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(
        callback, text, kb, answer_text="Upload folder reset to Personal Vault", also_ack=True
    )


# -------------------- FULL ACCESS / PAID UNLOCK REQUEST & OWNER APPROVAL SYSTEM --------------------


def _build_owner_approval_keyboard(
    short_type: str, item_id: int, visitor_uid: int
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ 1 Day",
                    callback_data=f"gr:{short_type}:{item_id}:{visitor_uid}:1",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="✅ 7 Days",
                    callback_data=f"gr:{short_type}:{item_id}:{visitor_uid}:7",
                    style="success",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✅ 30 Days",
                    callback_data=f"gr:{short_type}:{item_id}:{visitor_uid}:30",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="♾️ Lifetime",
                    callback_data=f"gr:{short_type}:{item_id}:{visitor_uid}:0",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Reject",
                    callback_data=f"gr:{short_type}:{item_id}:{visitor_uid}:-1",
                    style="danger",
                )
            ],
        ]
    )


def _format_grant_duration_label(days: int) -> str:
    if days == 0:
        return "♾️ Lifetime"
    if days == 1:
        return "1 Day"
    return f"{days} Days"


@router.callback_query(F.data.startswith("req:"))
async def cb_request_full_access(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
) -> None:
    if not callback.from_user:
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in ("f", "p") or not parts[2].isdigit():
        await callback.answer("Invalid request.", show_alert=True)
        return

    short_type = parts[1]
    link_type = "file" if short_type == "f" else "pack"
    item_id = int(parts[2])

    visitor = await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )

    if link_type == "file":
        item = await db.get_file(item_id)
        if not item or item.get("is_trashed") or not item.get("share_token"):
            await callback.answer("⚠️ This file link is no longer active.", show_alert=True)
            return
        owner_vault_id = int(item["vault_id"])
        preferred_owner_uid = int(item["uploader_user_id"])
        item_title = item.get("title") or "Shared File"
        kind_badge = f"{MEDIA_ICONS.get(item['media_type'], '📦')} File"
    else:
        item = await db.get_pack(item_id)
        if not item or item.get("item_count", 0) == 0:
            await callback.answer("⚠️ This pack link is no longer active.", show_alert=True)
            return
        owner_vault_id = int(item["vault_id"])
        preferred_owner_uid = int(item["creator_user_id"])
        item_title = item.get("title") or "Shared Pack"
        kind_badge = f"🎒 Pack ({item['item_count']} files)"

    if (
        visitor["user_id"] == preferred_owner_uid
        or visitor["vault_id"] == owner_vault_id
    ):
        await callback.answer(
            "🔓 You own this content — permanent & unrestricted!", show_alert=True
        )
        return

    existing_grant = await db.get_active_access_grant(
        link_type, item_id, visitor["user_id"]
    )
    if existing_grant is not None:
        badge = format_grant_badge(existing_grant["expires_at"])
        await callback.answer(f"✅ You already have {badge}!", show_alert=True)
        return

    cooldown_rem = _check_access_request_cooldown(
        visitor["user_id"], link_type, item_id
    )
    if cooldown_rem > 0:
        mins = max(1, (cooldown_rem + 59) // 60)
        await callback.answer(
            f"⏳ Request already sent! Please wait ~{mins}m or message the owner directly.",
            show_alert=True,
        )
        return

    _mark_access_request_sent(visitor["user_id"], link_type, item_id)

    contact_url = await db.resolve_owner_contact_url(
        owner_vault_id, preferred_user_id=preferred_owner_uid
    )

    visitor_name = esc(
        (callback.from_user.full_name or visitor.get("full_name") or "Telegram User")[:48]
    )
    raw_uname = (callback.from_user.username or visitor.get("username") or "").strip().lstrip("@")
    uname_display = f"@{esc(raw_uname)}" if raw_uname else "<i>none</i>"

    approval_text = (
        f"🔓 <b>Full Access Request</b>\n"
        f"<blockquote>"
        f"👤 <b>Visitor:</b> {visitor_name} ({uname_display})\n"
        f"🆔 <b>User ID:</b> <code>{visitor['user_id']}</code>\n"
        f"📦 <b>{kind_badge}:</b> <b>{esc(item_title[:48])}</b>"
        f"</blockquote>\n"
        f"<i>Tap a duration below to approve full access in 1 tap:</i>"
    )
    approval_kb = _build_owner_approval_keyboard(
        short_type, item_id, int(visitor["user_id"])
    )

    owner_accounts = await db.get_linked_accounts(owner_vault_id)
    recipient_uids: list[int] = []
    if preferred_owner_uid:
        recipient_uids.append(preferred_owner_uid)
    for acc in owner_accounts:
        uid = int(acc["user_id"])
        if uid not in recipient_uids:
            recipient_uids.append(uid)

    for owner_uid in recipient_uids:
        try:
            await tg_retry_call(
                bot.send_message,
                chat_id=owner_uid,
                text=approval_text,
                reply_markup=approval_kb,
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    confirm_text = (
        "📩 <b>Full access request sent to the owner!</b>\n"
        "<blockquote>You will receive an instant notification here once approved, or message the owner below:</blockquote>"
    )
    confirm_kb = build_contact_owner_kb(contact_url)

    await callback.answer("📩 Full access request sent!")
    if callback.message:
        await callback.message.answer(
            confirm_text,
            reply_markup=confirm_kb,
            disable_web_page_preview=True,
        )


@router.callback_query(F.data.startswith("gr:"))
async def cb_owner_grant_or_reject_access(
    callback: CallbackQuery,
    db: Database,
    bot: Bot,
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 5 or parts[1] not in ("f", "p"):
        await callback.answer("Invalid approval action.", show_alert=True)
        return
    try:
        short_type = parts[1]
        item_id = int(parts[2])
        visitor_uid = int(parts[3])
        days = int(parts[4])
    except ValueError:
        await callback.answer("Invalid approval payload.", show_alert=True)
        return

    if days not in (-1, 0, 1, 7, 30):
        await callback.answer("Unsupported duration.", show_alert=True)
        return

    approver = await db.ensure_user(
        callback.from_user.id,
        callback.from_user.username or "",
        callback.from_user.full_name or "",
    )
    link_type = "file" if short_type == "f" else "pack"
    uname = bot_username or "KoshBox_Bot"

    if link_type == "file":
        item = await db.get_file(item_id)
        if not item or item.get("is_trashed"):
            await callback.answer("File no longer exists.", show_alert=True)
            return
        if approver["vault_id"] != item["vault_id"] and approver["user_id"] != item["uploader_user_id"]:
            await callback.answer("Owner only.", show_alert=True)
            return
        item_title = item.get("title") or "Shared File"
        share_token = item.get("share_token") or ""
        open_url = f"https://t.me/{uname}?start=file_{share_token}" if share_token else ""
        owner_vault_id = int(item["vault_id"])
        preferred_owner_uid = int(item["uploader_user_id"])
    else:
        item = await db.get_pack(item_id)
        if not item:
            await callback.answer("Pack no longer exists.", show_alert=True)
            return
        if approver["vault_id"] != item["vault_id"] and approver["user_id"] != item["creator_user_id"]:
            await callback.answer("Owner only.", show_alert=True)
            return
        item_title = item.get("title") or "Shared Pack"
        share_token = item.get("share_token") or ""
        open_url = f"https://t.me/{uname}?start=pack_{share_token}" if share_token else ""
        owner_vault_id = int(item["vault_id"])
        preferred_owner_uid = int(item["creator_user_id"])

    visitor_row = await db.get_user(visitor_uid)
    visitor_label = esc(
        ((visitor_row or {}).get("full_name") or f"User {visitor_uid}")[:42]
    )
    v_uname = ((visitor_row or {}).get("username") or "").strip().lstrip("@")
    visitor_tag = f"{visitor_label} (@{esc(v_uname)})" if v_uname else f"{visitor_label} (<code>{visitor_uid}</code>)"

    if days == -1:
        await db.revoke_link_access(link_type, item_id, visitor_uid)
        contact_url = await db.resolve_owner_contact_url(
            owner_vault_id, preferred_user_id=preferred_owner_uid
        )
        owner_done_text = (
            f"❌ <b>Full Access Request Declined</b>\n"
            f"<blockquote>"
            f"👤 <b>Visitor:</b> {visitor_tag}\n"
            f"📦 <b>Content:</b> <b>{esc(item_title[:48])}</b>"
            f"</blockquote>"
        )
        await safe_edit_or_send(
            callback,
            owner_done_text,
            None,
            answer_text="❌ Request declined",
            also_ack=True,
        )
        try:
            await tg_retry_call(
                bot.send_message,
                chat_id=visitor_uid,
                text=(
                    f"❌ <b>Full Access Request Declined</b>\n"
                    f"<blockquote>Your request for <b>{esc(item_title[:44])}</b> was not approved. "
                    f"Message the owner below if you need assistance.</blockquote>"
                ),
                reply_markup=build_contact_owner_kb(contact_url),
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        return

    await db.grant_link_access(
        link_type,
        item_id,
        visitor_uid,
        approver["user_id"],
        days=days,
    )
    dur_lbl = _format_grant_duration_label(days)

    owner_approved_text = (
        f"✅ <b>Full Access Granted ({dur_lbl})</b>\n"
        f"<blockquote>"
        f"👤 <b>Visitor:</b> {visitor_tag}\n"
        f"🆔 <b>User ID:</b> <code>{visitor_uid}</code>\n"
        f"📦 <b>Content:</b> <b>{esc(item_title[:48])}</b>\n"
        f"⏳ <b>Access:</b> <b>{dur_lbl}</b> (No Trial / Auto-Delete)"
        f"</blockquote>"
    )
    owner_revoke_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚫 Revoke Access",
                    callback_data=f"gr:{short_type}:{item_id}:{visitor_uid}:-1",
                    style="danger",
                )
            ]
        ]
    )
    await safe_edit_or_send(
        callback,
        owner_approved_text,
        owner_revoke_kb,
        answer_text=f"✅ Granted {dur_lbl}!",
        also_ack=True,
    )

    visitor_kb_rows: list[list[InlineKeyboardButton]] = []
    if open_url:
        visitor_kb_rows.append(
            [
                InlineKeyboardButton(
                    text="📥 Open Unlocked Content",
                    url=open_url,
                    style="primary",
                )
            ]
        )
    visitor_kb = (
        InlineKeyboardMarkup(inline_keyboard=visitor_kb_rows)
        if visitor_kb_rows
        else None
    )
    try:
        await send_with_effect(
            bot.send_message,
            chat_id=visitor_uid,
            text=(
                f"🎉 <b>Full Access Unlocked!</b>\n"
                f"<blockquote>"
                f"📦 <b>{esc(item_title[:48])}</b>\n"
                f"✅ <b>Access:</b> {dur_lbl} · Zero Trial Timer"
                f"</blockquote>\n"
                f"<i>Tap below anytime to open your unlocked content:</i>"
            ),
            reply_markup=visitor_kb,
            disable_web_page_preview=True,
            message_effect_id=EFFECT_PARTY,
        )
    except Exception:
        pass
