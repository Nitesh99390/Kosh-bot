import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Database
from handlers import accounts, admin, file_actions, groups, inline, start_help, upload
from utils.formatters import (
    build_file_card_text,
    build_file_keyboard,
    format_duration,
    format_size,
    next_sort_key,
    progress_bar,
)


async def run_tests() -> None:
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

        # 6. Inline query parser & card rendering
        parsed = inline._parse_inline_query("g:chill type:audio fav top #lofi midnight")
        assert parsed["media_type"] == "audio"
        assert parsed["group_name_filter"] == "chill"
        assert parsed["only_favorites"] is True
        assert parsed["tag_filter"] == "#lofi"

        card_txt = build_file_card_text(await db.get_file(f1["id"]), "KoshBox_Bot", header_badge="✓ <b>Saved to Vault</b>")
        assert "Midnight Rain Remix" in card_txt and "Best late night track" in card_txt
        kb = build_file_keyboard(await db.get_file(f1["id"]), back_cb="br:all:new:0:0")
        assert len(kb.inline_keyboard) == 5

        # 7. Browse & Analytics UI builders
        br_txt, br_kb = await file_actions.render_browse_page(1001, "all", "def", 0, 0, db)
        assert "All Vault Files" in br_txt
        stats_data = await db.get_vault_stats(u1["vault_id"])
        assert stats_data["total_files"] == 2 and stats_data["total_uses"] == 2

        # 8. Account sync & merge
        u3 = await db.ensure_user(1003, "alice_alt", "Alice Alt")
        link_req = await db.create_link_request(1001, u1["vault_id"], 300)
        assert await db.attach_requester_to_link(link_req["code"], 1003, "Alice Alt", "alice_alt")
        approved = await db.approve_link_request(link_req["code"], 1001)
        assert approved is not None
        linked_accs = await db.get_linked_accounts(u1["vault_id"])
        assert len(linked_accs) == 2

        assert format_size(5_242_880) == "5.0 MB"
        assert format_duration(215) == "3:35"
        assert len(progress_bar(0.5, 8)) == 8
        assert next_sort_key("new") == "top"

        await db.close()
        print("ALL TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(run_tests())
