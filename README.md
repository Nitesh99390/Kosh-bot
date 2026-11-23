# KoshBox — Modern Telegram Cloud Vault & Inline Bot

A fast, lightweight, Oracle Cloud–ready Telegram bot with a **clean, single-panel UI/UX** that archives your media & files into a private Dump Channel and lets you search and send them from **any chat** via Telegram's native **Inline Menu** (`@YourBot query`).

## Highlights & Upgraded UI/UX
- **Single-Panel App Navigation**: Every menu (`/start`, My Files, Folders, Analytics, Settings, Synced Accounts, Guide) transitions smoothly in-place without cluttering your chat history.
- **Universal Media Archiving + Smart Album Batching**:
  - Supports **Audio, Voice, Video, Video Notes, Documents (PDF/APK/ZIP/etc.), Photos, GIFs, and Stickers**.
  - Forwarding a multi-file **Media Album** produces one clean summary card instead of spamming multiple messages.
- **Advanced File Browser (`/files`, `/search`)**:
  - Instant media filter pills: `All`, `★ Fav`, `🎵 Audio`, `🎬 Video`, `📄 Docs`, `🖼️ Photos`.
  - One-tap sorting: **Newest**, **Most Used**, **A–Z**, **Largest**, **Oldest**.
  - Rich file card actions: **📥 Get File**, **📤 Send to Chat**, **✏️ Rename**, **🏷️ Tags**, **💬 Inline Caption**, **📁 Move Folder**, **★ Favorite**, **🔗 Public Share Link**, and **🗑️ Recycle Trash**.
- **Safe Recycle Trash & Instant Undo (`/trash`)**:
  - Deleting a file moves it to **Trash** with a one-tap **↩️ Undo (Restore)** button so you never lose files accidentally.
- **Personal Folders & Collaborative Shared Groups (`/groups`)**:
  - Browse files directly inside any folder, rename folders, set an **🎯 Auto-Upload Target**, or generate/reset **Shared Group Invite Links** (`t.me/YourBot?start=join_XXXX`).
- **Native Inline Search (`@YourBot query`)**:
  - Personal vault isolation (`is_personal=True`) with hybrid **SQLite FTS5** + substring matching and infinite scroll.
  - Inline modifiers: `type:audio`, `type:video`, `type:doc`, `type:photo`, `type:voice`, `type:gif`, `type:sticker`, `g:folder`, `#tag`, `fav`, `top`, `sort:az`, `sort:size`.
- **Multi-Account Real-Time Sync (`/accounts`, `/link`, `/verify`)**:
  - Connect secondary Telegram accounts via one-tap deep-link or 6-character code with **Approve / Reject** owner verification.
- **Visual Analytics (`/stats`) & Interactive Admin Console (`/admin`)**:
  - Progress-bar media breakdown, top sent inline files, 24h active user metrics, one-tap SQLite snapshot backup, and rich broadcast.

---

## Step 1: Telegram & BotFather Setup

1. Open [@BotFather](https://t.me/BotFather) and create a bot with `/newbot` → copy your `BOT_TOKEN`.
2. **Enable Inline Mode** (required for `@BotUsername` search in any chat):
   - Send `/setinline` to `@BotFather` → select your bot → set placeholder: `Search vault (or type:audio, g:folder, fav, top)...`
3. **Recommended**: Send `/setinlinefeedback` → select your bot → choose `100%` so "Most Used" counters update automatically.
4. **Create a Private Dump Channel**:
   - Create a private Telegram Channel and add your bot as **Administrator** with **Post Messages** permission.
   - Copy the channel ID (starts with `-100...`).

---

## Step 2: Deploy on Oracle Cloud (or Any Linux VPS)

1. Configure `.env`:
   ```bash
   cp .env.example .env
   nano .env
   ```
   Set `BOT_TOKEN`, `DUMP_CHANNEL_ID`, and `ADMIN_IDS`.
2. Run the automated installer (creates `.venv` + `systemd` 24/7 service):
   ```bash
   chmod +x deploy_oracle.sh
   ./deploy_oracle.sh
   ```
3. Check live status:
   ```bash
   sudo systemctl status koshbox
   journalctl -u koshbox -f
   ```
