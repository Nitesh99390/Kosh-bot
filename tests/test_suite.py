import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Database
from handlers import accounts, admin, file_actions, groups, inline, start_help, upload
from utils.formatters import (
    breadcrumb,
    build_file_card_text,
    build_file_keyboard,
    build_file_manage_keyboard,
    build_upload_quick_keyboard,
    copy_btn,
    format_duration,
    format_size,
    next_sort_key,
    progress_bar,
    toggle_label,
)


class _DummyBot:
    async def get_me(self):
        return SimpleNamespace(username="KoshBox_Bot", id=99999, supports_inline_queries=True)


async def run_tests() -> None:
    dummy_bot = _DummyBot()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_koshbox.db")
        db = Database(db_path)
        await db.connect()

        # 1. Users & Vaults
        u1 = await db.ensure_user(1001, "alice", "Alice Verma")
        u2 = await db.ensure_user(1002, "bob", "Bob Sharma")
        assert u1["vault_id"] != u2["vault_id"]

        # 2. Sort preferences & bulk mode
        assert await db.toggle_bulk_mode(1001) is True
        assert await db.toggle_bulk_mode(1001) is False
        assert await db.set_default_sort(1001, "top") == "top"

        # 3. Folders & Shared Groups + Rename + Invite regeneration
        personal_grp = await db.create_group(u1["vault_id"], "Lofi Beats", is_shared=False)
        shared_grp = await db.create_group(u1["vault_id"], "Study Club", is_shared=True)
        assert shared_grp["invite_code"] is not None
        old_code = shared_grp["invite_code"]

        await db.rename_group(personal_grp["id"], "Chill Mixes")
        updated_personal = await db.get_group(personal_grp["id"])
        assert updated_personal["name"] == "Chill Mixes"

        new_code = await db.regenerate_group_invite(shared_grp["id"])
        assert new_code != old_code
        assert await db.join_shared_group(u2["vault_id"], old_code) is None
        joined = await db.join_shared_group(u2["vault_id"], new_code)
        assert joined is not None and joined["id"] == shared_grp["id"]

        # 4. Files, FTS5, Sorting, Type filtering
        f1 = await db.add_file(
            vault_id=u1["vault_id"],
            uploader_user_id=1001,
            group_id=personal_grp["id"],
            media_type="audio",
            file_id="AUD_FILE_1",
            file_unique_id="UNIQ_1",
            dump_message_id=501,
            title="Midnight Rain Remix",
            performer="DJ Nova",
            file_size=5_242_880,
            duration=215,
            tags="#lofi #chill",
        )
        f2 = await db.add_file(
            vault_id=u1["vault_id"],
            uploader_user_id=1001,
            group_id=shared_grp["id"],
            media_type="document",
            file_id="DOC_FILE_2",
            file_unique_id="UNIQ_2",
            dump_message_id=502,
            title="Architecture Notes.pdf",
            file_size=1_048_576,
            tags="#notes",
        )

        # Check FTS5 & shared group visibility for u2
        u2_results = await db.search_files(u2["vault_id"], text_query="Architecture")
        assert len(u2_results) == 1 and u2_results[0]["id"] == f2["id"]
        u2_private_check = await db.search_files(u2["vault_id"], text_query="Midnight")
        assert len(u2_private_check) == 0

        # Check caption update, favorite, use_count, sorting
        await db.update_file_caption(f1["id"], "Best late night track")
        await db.toggle_favorite(f1["id"])
        await db.increment_use_count(f2["id"])
        await db.increment_use_count(f2["id"])

        top_sorted = await db.search_files(u1["vault_id"], sort_by="top")
        assert top_sorted[0]["id"] == f2["id"]

        size_sorted = await db.search_files(u1["vault_id"], sort_by="size")
        assert size_sorted[0]["id"] == f1["id"]

        # 5. Safe Trash, Restore, and Purge
        await db.trash_file(f1["id"])
        assert await db.count_trashed_files(u1["vault_id"]) == 1
        active_after_trash = await db.search_files(u1["vault_id"], text_query="Midnight")
        assert len(active_after_trash) == 0

        await db.restore_file(f1["id"])
        assert await db.count_trashed_files(u1["vault_id"]) == 0
        restored_search = await db.search_files(u1["vault_id"], text_query="Midnight")
        assert len(restored_search) == 1

        # 6. Inline query parser
        parsed = inline._parse_inline_query("g:chill type:audio fav top #lofi midnight")
        assert parsed["media_type"] == "audio"
        assert parsed["group_name_filter"] == "chill"
        assert parsed["only_favorites"] is True
        assert parsed["tag_filter"] == "#lofi"

        # 7. Progressive-Disclosure UI/UX Keyboard Layout Verification
        # 7a. Home Dashboard: strictly 3 rows, 5 buttons total
        home_txt, home_kb = await start_help.render_home_dashboard(
            1001, "Alice", db, dummy_bot  # type: ignore[arg-type]
        )
        assert "<blockquote>" in home_txt
        assert len(home_kb.inline_keyboard) == 3
        total_home_btns = sum(len(r) for r in home_kb.inline_keyboard)
        assert total_home_btns == 5

        # 7b. Primary File Detail Card: 3 rows (Get+Send, Star+Edit&More, Back+Home)
        f1_fresh = await db.get_file(f1["id"])
        card_txt = build_file_card_text(f1_fresh, "KoshBox_Bot")
        assert "Midnight Rain Remix" in card_txt and "Best late night track" in card_txt
        assert "<blockquote>" in card_txt

        kb_primary = build_file_keyboard(
            f1_fresh, back_cb="br:all:new:0:0", bot_username="KoshBox_Bot"
        )
        assert len(kb_primary.inline_keyboard) == 3
        assert kb_primary.inline_keyboard[0][0].style == "primary"

        # 7c. Secondary File Manage Drawer: progressive disclosure + public share CopyTextButton
        await db.toggle_share_token(f1["id"])
        f1_shared = await db.get_file(f1["id"])
        kb_manage = build_file_manage_keyboard(
            f1_shared, back_cb="br:all:new:0:0", bot_username="KoshBox_Bot"
        )
        assert any(
            btn.copy_text is not None
            for row in kb_manage.inline_keyboard
            for btn in row
        )
        # Ensure all callback_data stay strictly <= 64 bytes
        for kb_test in (home_kb, kb_primary, kb_manage):
            for row in kb_test.inline_keyboard:
                for btn in row:
                    if btn.callback_data:
                        assert len(btn.callback_data.encode("utf-8")) <= 64

        # 7d. Upload Quick Receipt Keyboard: compact 2 rows
        up_kb = build_upload_quick_keyboard(f1_shared)
        assert len(up_kb.inline_keyboard) == 2

        # 7e. Browse Page + Dedicated Filter & Sort Drawer
        br_txt, br_kb = await file_actions.render_browse_page(1001, "all", "def", 0, 0, db)
        assert "My Files" in br_txt
        # 2 file items + 1 toolbar row + 1 back row = 4 clean rows!
        assert len(br_kb.inline_keyboard) == 4

        flt_txt, flt_kb = file_actions.render_filter_sort_drawer("audio", "top", 0, 0)
        assert "Filter & Sort Options" in flt_txt
        assert len(flt_kb.inline_keyboard) == 6

        # 7f. Groups 2-column grid + Primary & Manage views
        grp_txt, grp_kb = await groups.render_groups_panel(1001, db)
        assert "Folders & Shared Groups" in grp_txt
        # 2 groups fit on 1 2-column row + 1 footer row = 2 rows total!
        assert len(grp_kb.inline_keyboard) == 2

        sgrp = await groups.render_single_group(
            1001, shared_grp["id"], db, dummy_bot, manage_mode=False  # type: ignore[arg-type]
        )
        assert sgrp is not None
        assert len(sgrp[1].inline_keyboard) == 4  # Open/Inline, AutoUpload/Manage, CopyInvite, Back/Home

        sgrp_m = await groups.render_single_group(
            1001, shared_grp["id"], db, dummy_bot, manage_mode=True  # type: ignore[arg-type]
        )
        assert sgrp_m is not None
        assert any(b.style == "danger" for r in sgrp_m[1].inline_keyboard for b in r)

        # 7g. Settings & Stats & Admin panels
        set_txt, set_kb = await start_help._render_settings_panel(1001, db)
        assert "Settings & Vault Tools" in set_txt
        assert len(set_kb.inline_keyboard) == 4

        st_txt, st_kb = await start_help._render_stats_panel(1001, db)
        assert "Storage & Usage Analytics" in st_txt
        assert len(st_kb.inline_keyboard) == 2

        adm_txt, adm_kb = await admin._render_admin_panel(db)
        assert "Admin Console" in adm_txt
        assert len(adm_kb.inline_keyboard) == 2

        # 8. Account sync & merge + Progressive Unlink view
        u3 = await db.ensure_user(1003, "alice_alt", "Alice Alt")
        link_req = await db.create_link_request(1001, u1["vault_id"], 300)
        assert await db.attach_requester_to_link(link_req["code"], 1003, "Alice Alt", "alice_alt")
        approved = await db.approve_link_request(link_req["code"], 1001)
        assert approved is not None
        linked_accs = await db.get_linked_accounts(u1["vault_id"])
        assert len(linked_accs) == 2

        acc_txt, acc_kb = await accounts.render_accounts_panel(1001, db, manage_unlink=False)
        assert len(acc_kb.inline_keyboard) == 3
        acc_m_txt, acc_m_kb = await accounts.render_accounts_panel(1001, db, manage_unlink=True)
        assert len(acc_m_kb.inline_keyboard) == 3

        # 9. Helpers
        assert format_size(5_242_880) == "5.0 MB"
        assert format_duration(215) == "3:35"
        assert len(progress_bar(0.5, 8)) == 8
        assert next_sort_key("new") == "top"
        assert toggle_label(True, "Bulk") == "✅ Bulk"
        assert toggle_label(False, "Bulk") == "⬜ Bulk"
        assert "Vault › Files" in breadcrumb("Vault", "Files")
        assert copy_btn("Copy", "hello").copy_text.text == "hello"

        await db.close()
        print("ALL UI/UX & DATABASE TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(run_tests())
