# KoshBox — Modern Telegram Cloud Vault & Inline Bot

A fast, lightweight, Oracle Cloud–ready Telegram bot with a **clean, Progressive-Disclosure UI/UX** that archives your media & files into a private Dump Channel and lets you search and send them from **any chat** via Telegram's native **Inline Menu** (`@YourBot query`).

## Highlights & Professional UI/UX Architecture
- **PostBot-Style 3D Visual Banner Cards (One-Time Dump Channel Cache)**:
  - Custom 3D KoshBox mascot banners (`assets/banners/welcome.jpg`, `upload.jpg`, `guide.jpg`) give `/start`, Inline Upload (`/start upload`), `/help`, and `/accounts` a polished, premium visual header card.
  - **Zero Repeated Server Uploads**: On first startup, the bot uploads each banner **once** from the server to your private `DUMP_CHANNEL_ID` and saves its Telegram `file_id` + `SHA-256` hash in SQLite (`ui_assets`). Every subsequent card render or navigation transition reuses Telegram's cached `file_id` with zero disk/network upload overhead.
- **Progressive Disclosure & Zero Button Clutter**:
  - **Hero Dashboard (`/start`)**: Focused 3-row layout (`🔍 Search Vault Inline`, `📦 My Files`, `📁 Folders`, `⭐ Favorites`, `⚙️ Settings & Tools`) with native HTML `<blockquote>` cards and breadcrumb navigation.
  - **Clean Upload Receipts**: Uploading a file returns a minimal 2-row receipt (`📤 Send to Chat` + `🛠️ Edit & Organize`) instead of spamming 11 buttons under every message.
  - **2-Tier File Detail & Management**: Primary File Card shows only essential actions (`📥 Get Here`, `📤 Send to Chat`, `⭐ Star`, `🛠️ Edit & More`), while Rename, Move Folder, Tags, Caption, Public Link, and Trash live neatly inside the `🛠️ Edit & More` drawer.
  - **Native Telegram Bot API 9.4 Styling & One-Tap Copy**: Uses `style="primary"`, `style="success"`, `style="danger"` button accents plus native `CopyTextButton` (`📋 Copy Public Link`, `📋 Copy Group Invite Link`, `📋 Copy Sync Code`) and safe-left / destructive-right confirmations.
- **Mobile-First File Browser (`/files`, `/search`)**:
  - Clean 5-item pages with a dedicated `🌪️ Filter & Sort` drawer supporting **All, ⭐ Starred, 🎵 Audio, 🎬 Video, 📄 Docs, 🖼️ Photos, 🎙️ Voice, 🎞️ GIFs** and 5 sort modes (**Newest, Most Used, A–Z, Largest, Oldest**).
- **2-Column Folders & Collaborative Shared Groups (`/groups`)**:
  - Compact 2-column folder grid, `+ Create or Join` action menu, one-tap invite link copying, and `⚙️ Manage` sub-drawer for Rename, Reset Invite, and Leave/Delete.
- **Safe Recycle Trash & Instant Undo (`/trash`)**:
  - Deleting a file moves it to **Trash** with a green **↩️ Undo (Restore)** button and a 2-step confirmation before permanent deletion.
- **Native Inline Search (`@YourBot query`)**:
  - Personal vault isolation (`is_personal=True`) with hybrid **SQLite FTS5** + substring matching and infinite scroll.
  - Inline modifiers: `type:audio`, `type:video`, `type:doc`, `type:photo`, `type:voice`, `type:gif`, `type:sticker`, `g:folder`, `#tag`, `fav`, `top`, `sort:az`, `sort:size`.
- **Multi-Account Real-Time Sync (`/accounts`, `/link`, `/verify`)**:
  - Connect secondary Telegram accounts via one-tap copyable link or 6-character code with **Approve / Reject** owner verification.
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
