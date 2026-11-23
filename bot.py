import asyncio
import logging
import socket

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from config import load_config
from database import Database
from handlers import accounts, admin, file_actions, groups, inline, packs, start_help, upload
from utils.banners import warmup_banners_in_dump_channel
from utils.formatters import run_scheduled_deletes_sweeper
from utils.middleware import GuardAndThrottleMiddleware

try:
    import uvloop  # type: ignore[import-not-found]

    uvloop.install()
    _USING_UVLOOP = True
except Exception:
    _USING_UVLOOP = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("koshbox")


class IPv4AiohttpSession(AiohttpSession):
    """
    Persistent IPv4-only HTTP session for Oracle Cloud / Linux VPS environments
    where IPv6 is blackholed and IPv4 RTT to api.telegram.org is ~145ms:
    - Forces `AF_INET` so aiohttp never stalls on dead AAAA records
    - Keeps warm TCP/TLS connections (`keepalive_timeout=60`, `ttl_dns_cache=300`, `limit=64`)
    """

    def __init__(self, limit: int = 64, **kwargs) -> None:
        super().__init__(limit=limit, **kwargs)
        self._connector_type = aiohttp.TCPConnector
        self._connector_init["family"] = socket.AF_INET
        self._connector_init["ttl_dns_cache"] = 300
        self._connector_init["keepalive_timeout"] = 60.0
        self._connector_init["limit"] = limit


async def _set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Vault Dashboard"),
            BotCommand(command="files", description="Browse & filter files"),
            BotCommand(command="packs", description="Multi-file Pack Links"),
            BotCommand(command="batch", description="Bundle files into 1 link"),
            BotCommand(command="groups", description="Folders & Shared Spaces"),
            BotCommand(command="settings", description="Security, Auto-Delete & Sync"),
        ]
    )


async def main() -> None:
    config = load_config()
    db = Database(config.db_path)
    await db.connect()

    session = IPv4AiohttpSession(limit=64)
    bot = Bot(
        token=config.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Fetch bot identity ONCE at startup so handlers never pay 145ms per-click RTT
    me = await bot.get_me()
    bot_username = me.username or "KoshBox_Bot"
    logger.info(
        "Starting KoshBox as @%s (id=%s, uvloop=%s)",
        bot_username,
        me.id,
        _USING_UVLOOP,
    )
    if not me.supports_inline_queries:
        logger.warning(
            "Inline mode is DISABLED in @BotFather! Run /setinline in @BotFather for @%s.",
            bot_username,
        )

    # Shared workflow dependencies injected into handlers
    dp["db"] = db
    dp["config"] = config
    dp["bot_username"] = bot_username

    # Outer Guard & Anti-Flood Middleware (Ban check, Force-Sub gate, Rate-Limiting)
    guard = GuardAndThrottleMiddleware(rate_limit=config.rate_limit_interval)
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)

    # Register routers (FSM / command routers before catch-all media upload)
    dp.include_router(admin.router)
    dp.include_router(start_help.router)
    dp.include_router(accounts.router)
    dp.include_router(packs.router)
    dp.include_router(groups.router)
    dp.include_router(file_actions.router)
    dp.include_router(inline.router)
    dp.include_router(upload.router)

    await _set_bot_commands(bot)

    # Pre-warm custom 3D UI banners into Dump Channel ONCE (reuses SQLite file_id on subsequent boots)
    banner_status = await warmup_banners_in_dump_channel(bot, db, config)
    logger.info("UI Banner cache status (Dump Channel): %s", banner_status)

    # Start restart-safe, zero-RAM-bloat SQLite batch auto-delete sweeper (`bot.delete_messages()`)
    sweeper_task = asyncio.create_task(
        run_scheduled_deletes_sweeper(bot, db, interval_seconds=5.0)
    )

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        sweeper_task.cancel()
        try:
            await sweeper_task
        except asyncio.CancelledError:
            pass
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
