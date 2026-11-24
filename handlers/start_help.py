import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from typing import Optional

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
from utils.middleware import check_force_sub, invalidate_force_sub_cache
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
    """
    Returns remaining cooldown seconds (> 0) if `(user_id, link_type, link_id)` already sent
    a Full Access request within the last 10 minutes; returns 0 if allowed.
    """
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
    """Returns remaining lockout seconds (>0 if locked out, 0 if allowed)."""
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
    """Records a failed PIN attempt; returns (attempts_left, lockout_seconds_if_triggered)."""
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
            "⚠️ <b>Link Invalid or Revoked</b>",
            "This share link was revoked or removed by the owner.",
        )
    if raw_file.get("share_paused"):
        return (
            "⏸ <b>Link Temporarily Paused</b>",
            "The owner has paused access to this link. Try again later.",
        )
    exp = raw_file.get("share_expires_at")
    if exp and int(exp) > 0 and int(exp) < now:
        return (
            "⏳ <b>Link Expired</b>",
            "The validity timer on this share link has ended.",
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
            "This pack link was revoked, emptied, or removed.",
        )
    if raw_pack.get("is_paused"):
        return (
            "⏸ <b>Pack Link Paused</b>",
            "The owner has temporarily paused this pack link.",
        )
    exp = raw_pack.get("expires_at")
    if exp and int(exp) > 0 and int(exp) < now:
        return (
            "⏳ <b>Pack Link Expired</b>",
            "The validity timer on this pack link has ended.",
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
    Ultra-crisp Hero Dashboard (Linear / Raycast / PostBot Pro UX):
    - Short 4-line scannable layout (Zero fluff!)
    - Focused 3-row action grid (6 buttons total)
    """
    user = await db.get_user(user_id)
    stats = await db.get_vault_stats(user["vault_id"])
    uname = esc(bot_username or "KoshBox_Bot")

    active_folder = "Personal"
    if user.get("active_group_id"):
        grp = await db.get_group(user["active_group_id"])
        if grp:
            active_folder = f"{'👥' if grp['is_shared'] else '📁'} {grp['name']}"

    mode_badges: list[str] = []
    if user.get("bulk_mode"):
        mode_badges.append("⚡ Bulk")
    if user.get("default_one_time"):
        mode_badges.append("👤 1×/User")
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
    Compact Pro Settings & Trial Security Hub (Linear / Raycast mobile-first grid):
    - Bulk Mode, Default Sort, Auto-Delete Timer (incl. 2h Trial), Content Protection (No-Fwd)
    - Default 1×/User Trial Mode, Dynamic Per-Owner Support Contact (`@username` / `t.me/...`)
    - Analytics, Trash, Sync Accounts, Guide
    """
    user = await db.get_user(user_id)
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

    active_folder = "Personal"
    if user.get("active_group_id"):
        grp = await db.get_group(user["active_group_id"])
        if grp:
            active_folder = f"{'👥' if grp['is_shared'] else '📁'} {grp['name']}"

    text = (
        f"{breadcrumb('Vault', 'Settings')}\n"
        f"⚙️ <b>Settings &amp; Trial Security</b>\n"
        f"<blockquote>"
        f"👤 1×/User: <b>{'ON' if one_time_on else 'OFF'}</b>  ·  🛡️ No-Fwd: <b>{'ON' if prot_on else 'Off'}</b>\n"
        f"⏱ Auto-Del: <b>{ttl_lbl}</b>  ·  💬 Support: {contact_badge}\n"
        f"⚡ Bulk: <b>{'ON' if bulk_on else 'Off'}</b>  ·  ↕️ Sort: <b>{sort_label}</b>  ·  🎯 <b>{esc(active_folder)}</b>"
        f"</blockquote>"
    )

    contact_btn_lbl = "💬 Support: Set" if saved_contact else "💬 Support Contact"

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"👤 1×/User: {'ON' if one_time_on else 'OFF'}",
                callback_data="set:1x",
                style="success" if one_time_on else None,
            ),
            InlineKeyboardButton(
                text=toggle_label(prot_on, "No-Fwd"),
                callback_data="set:prot",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"⏱ Auto-Del: {ttl_lbl}",
                callback_data="set:ttl",
            ),
            InlineKeyboardButton(
                text=contact_btn_lbl,
                callback_data="set:contact",
                style="primary" if not saved_contact else None,
            ),
        ],
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
    callback: CallbackQuery,
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
            await callback.answer("Still not joined — join first!", show_alert=True)
            return
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
        callback,
        "welcome",
        text,
        kb,
        bot,
        db,
        config,
        answer_text="✓ Verified!",
        also_ack=True,
    )


async def _deliver_shared_file_with_claim_check(
    message: Message,
    visitor: dict,
    file_row: dict,
    db: Database,
    bot: Bot,
) -> None:
    """
    Delivers a shared single file with:
    - Owner self-retrieval bypass: ZERO trial/claim lock, ZERO auto-delete, ZERO `protect_content`, ZERO trial alerts
    - Full Access Grant bypass (`link_access_grants`): skips 1x/User lock & auto-delete timer, unlimited opens while valid
    - Atomic 1-Time Per-User Trial Claim Lock (`INSERT OR IGNORE` BEFORE sending) for regular trial visitors
    - 2-column `🔓 Request Full Access` + `💬 Contact Owner` CTA on Trial Delivery, Repeat-Open & Expiry Notice
    - Restart-safe `scheduled_deletes` timer
    """
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
        await db.increment_file_share_views(file_row["id"])
    else:
        await message.answer("⚠️ Unsupported media format.")


async def _deliver_shared_pack_with_claim_check(
    message: Message,
    visitor: dict,
    pack: dict,
    db: Database,
    bot: Bot,
    config: Optional[Config] = None,
) -> None:
    """
    Delivers a shared multi-file Pack with:
    - Owner self-retrieval bypass: ZERO trial/claim lock, ZERO auto-delete, ZERO `protect_content`, ZERO trial alerts
    - Full Access Grant bypass (`link_access_grants`): skips 1x/User lock & auto-delete timer, unlimited opens while valid
    - Atomic 1-Time Per-User Trial Claim Lock (`INSERT OR IGNORE` BEFORE sending) for regular trial visitors
    - 2-column `🔓 Request Full Access` + `💬 Contact Owner` CTA on Trial Delivery, Repeat-Open & Expiry Notice
    - Native 100-in-1 `copy_messages` batch delivery + restart-safe `scheduled_deletes`
    """
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

    raw_ttl = await db.resolve_pack_auto_delete_ttl(pack)
    ttl = 0 if has_full_access else raw_ttl
    force_protect = bool((creator or {}).get("protect_content"))
    is_trial_or_protected = (not has_full_access) and (
        ttl > 0
        or bool(pack.get("one_time_per_user"))
        or bool(pack.get("protect_content"))
        or force_protect
    )
    await deliver_pack_to_chat(
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
        count_view=True,
        dump_channel_id=config.dump_channel_id if config else None,
        contact_url=contact_url,
        celebrate=True,
    )


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

    # Deep-link: shared single file (Owner Bypass + Full Access Grant + 1x/User Claim Lock + PIN + No-Fwd + Auto-Delete)
    if args.startswith("file_"):
        token = args[5:].strip()
        raw_file = await db.get_file_by_share_token(token, include_unavailable=True)
        if not raw_file:
            hdr, reason = _diagnose_file_unavailable(None)
            await message.answer(f"{hdr}\n<blockquote>{reason}</blockquote>")
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

        # Owner & approved Full Access users can always retrieve their unlocked content;
        # regular visitors must pass paused/expiry/max_views link checks
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

        # Pre-check 1x/User claim BEFORE asking for PIN so repeat trial visitors aren't prompted unnecessarily
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
                f"🔒 <b>PIN Required</b>\n"
                f"<blockquote>Enter PIN to unlock <b>{esc(file_row['title'][:36])}</b>:</blockquote>",
                reply_markup=cancel_kb("nav:home"),
            )
            return

        await _deliver_shared_file_with_claim_check(message, user, file_row, db, bot)
        return

    # Deep-link: shared multi-file Pack (Owner Bypass + Full Access Grant + 1x/User Claim Lock + 100-in-1 Batch Delivery)
    if args.startswith("pack_"):
        token = args[5:].strip()
        raw_pack = await db.get_pack_by_token(token, include_unavailable=True)
        if not raw_pack or raw_pack.get("item_count", 0) == 0:
            hdr, reason = _diagnose_pack_unavailable(raw_pack)
            await message.answer(f"{hdr}\n<blockquote>{reason}</blockquote>")
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
                f"🔒 <b>PIN Required</b>\n"
                f"<blockquote>Enter PIN to unlock <b>{esc(pack['title'])}</b> ({pack['item_count']} files):</blockquote>",
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
                "⚠️ <b>Invalid Invite</b>\n<blockquote>Link expired or reset.</blockquote>"
            )
            return
        uname = bot_username or "KoshBox_Bot"
        await send_with_effect(
            message.answer,
            f"🎉 <b>Joined {esc(group['name'])}</b> · <i>{group.get('file_count', 0)} files</i>\n"
            f"<blockquote>Inline: <code>@{esc(uname)} g:{esc(group['name'])}</code></blockquote>",
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
            message_effect_id=EFFECT_PARTY,
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
                ],
                [home_btn("🏠 Vault")],
            ]
        )
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


@router.message(ShareUnlockStates.waiting_for_pack_pin)
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
        answer_text="⚡ Bulk ON" if enabled else "Bulk OFF",
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
    bot_username: str = "KoshBox_Bot",
) -> None:
    if not callback.from_user:
        return
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

        _, flt, sort_key, gid_s, page_s = target.split(":")
        text, kb = await render_browse_page(
            callback.from_user.id, flt, sort_key, int(gid_s), int(page_s), db
        )
        await safe_edit_or_send(
            callback, text, kb, answer_text="Cancelled", also_ack=True
        )
        return
    elif target.startswith("f:link:"):
        fid = int(target.split(":")[2])
        f = await db.get_file(fid)
        if f:
            from handlers.file_actions import _render_file_link_card

            claims = await db.count_link_claims("file", fid)
            owner = await db.get_user(f["uploader_user_id"])
            owner_ttl = int((owner or {}).get("auto_delete_ttl") or 0)
            await safe_edit_or_send(
                callback,
                _render_file_link_card(
                    f, uname, unique_claims=claims, owner_global_ttl=owner_ttl
                ),
                build_file_link_keyboard(f, bot_username=uname),
                answer_text="Cancelled",
                also_ack=True,
            )
            return
    elif target.startswith("f:manage:"):
        fid = int(target.split(":")[2])
        f = await db.get_file(fid)
        if f:
            await safe_edit_or_send(
                callback,
                build_file_card_text(f, uname, edit_mode=True),
                build_file_manage_keyboard(f, bot_username=uname),
                answer_text="Cancelled",
                also_ack=True,
            )
            return
    elif target.startswith("f:open:"):
        fid = int(target.split(":")[2])
        f = await db.get_file(fid)
        if f:
            await safe_edit_or_send(
                callback,
                build_file_card_text(f, uname),
                build_file_keyboard(f, bot_username=uname),
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
    await safe_edit_or_send(callback, text, kb, also_ack=True)


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
    await safe_edit_or_send(callback, text, kb, also_ack=True)


@router.callback_query(F.data == "set:bulk")
async def cb_settings_bulk(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
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
    user = await db.get_user(callback.from_user.id)
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
        f"{breadcrumb('Vault', 'Settings', 'Support')}\n"
        f"💬 <b>Support / Sales Contact</b> · {cur_lbl}\n"
        f"<blockquote>Send your Telegram <code>@username</code> or <code>https://t.me/...</code> link shown on Trial Ended &amp; Auto-Delete notices (or <code>-</code> to reset to your own handle):</blockquote>",
        cancel_kb("nav:settings"),
        also_ack=True,
    )


@router.message(SettingsStates.waiting_for_support_contact)
async def fsm_save_support_contact(
    message: Message, state: FSMContext, db: Database
) -> None:
    if not message.from_user or not message.text:
        return
    raw = message.text.strip()
    try:
        saved_url = await db.set_support_contact(message.from_user.id, raw)
    except ValueError:
        await message.answer(
            "⚠️ <b>Invalid Handle or Link</b>\n"
            "<blockquote>Send a valid <code>@username</code> (e.g. <code>@MySupport</code>), a <code>https://t.me/...</code> URL, or <code>-</code> to clear:</blockquote>",
            reply_markup=cancel_kb("nav:settings"),
        )
        return
    await state.clear()
    text, kb = await _render_settings_panel(message.from_user.id, db)
    badge = f"✅ <b>Support Contact Set:</b> <code>{esc(saved_url)}</code>" if saved_url else "✅ <b>Support Contact Reset</b>"
    await message.answer(
        f"{badge}\n\n{text}",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "set:prot")
async def cb_settings_protect(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
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
    new_ttl = await db.cycle_auto_delete_ttl(callback.from_user.id)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(
        callback,
        text,
        kb,
        answer_text=f"⏱ Auto-Delete: {format_ttl_short(new_ttl)}",
        also_ack=True,
    )


@router.callback_query(F.data == "set:sort")
async def cb_settings_sort(callback: CallbackQuery, db: Database) -> None:
    if not callback.from_user:
        return
    user = await db.get_user(callback.from_user.id)
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
    await db.set_active_group(callback.from_user.id, None)
    text, kb = await _render_settings_panel(callback.from_user.id, db)
    await safe_edit_or_send(
        callback, text, kb, answer_text="Target reset", also_ack=True
    )


# -------------------- FULL ACCESS / PAID UNLOCK REQUEST & OWNER APPROVAL SYSTEM --------------------


def _build_owner_approval_keyboard(
    short_type: str, item_id: int, visitor_uid: int
) -> InlineKeyboardMarkup:
    """
    2-column 1-tap Approval Card keyboard for the owner (`callback_data <= 64` bytes):
    Row 1: [ ✅ 1 Day ] · [ ✅ 7 Days ]
    Row 2: [ ✅ 30 Days ] · [ ♾️ Lifetime ]
    Row 3: [ ❌ Reject ]
    """
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
    """
    Visitor taps `🔓 Request Full Access` (`req:<f|p>:<id>`):
    - Bounded 10m TTLCache rate-limit per `(user_id, link_type, link_id)` to prevent owner spam.
    - Sends a sleek 1-tap Approval Card to all linked accounts of the owner's `vault_id`.
    - Confirms to the visitor with `💬 Contact Owner` CTA.
    """
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

    # Owners never need to request access to their own vault content
    if (
        visitor["user_id"] == preferred_owner_uid
        or visitor["vault_id"] == owner_vault_id
    ):
        await callback.answer(
            "🔓 You own this content — permanent & unrestricted!", show_alert=True
        )
        return

    # If visitor already holds an active Full Access Grant, let them know immediately
    existing_grant = await db.get_active_access_grant(
        link_type, item_id, visitor["user_id"]
    )
    if existing_grant is not None:
        badge = format_grant_badge(existing_grant["expires_at"])
        await callback.answer(f"✅ You already have {badge}!", show_alert=True)
        return

    # 10-minute bounded TTLCache rate-limit per (user_id, link_type, link_id)
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
        f"<i>Tap a duration below to grant full non-trial access in 1 tap:</i>"
    )
    approval_kb = _build_owner_approval_keyboard(
        short_type, item_id, int(visitor["user_id"])
    )

    # Deliver Approval Card to all linked owner accounts in `owner_vault_id` (ensuring preferred uploader/creator is included)
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
        "<blockquote>You can also message them directly to complete your purchase.</blockquote>"
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
    """
    Owner taps an Approval Card action (`gr:<f|p>:<item_id>:<uid>:<days>`):
    - `days` in `{1, 7, 30, 0}` -> grants persistent Full Access in `link_access_grants`
      (`0` = Lifetime), cancels any pending trial auto-delete for that user + item, updates the
      owner's card in-place, and DMs the visitor with an instant `📥 Open Unlocked Content` deep-link button!
    - `days == -1` -> rejects the request, updates the owner's card, and politely notifies the visitor.
    """
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
                f"<i>Tap below anytime to retrieve your unlocked content:</i>"
            ),
            reply_markup=visitor_kb,
            disable_web_page_preview=True,
            message_effect_id=EFFECT_PARTY,
        )
    except Exception:
        pass
