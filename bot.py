import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from config import load_config
from database import Database
from handlers import accounts, admin, file_actions, groups, inline, packs, start_help, upload
from utils.banners import warmup_banners_in_dump_channel
from utils.middleware import GuardAndThrottleMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("koshbox")


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

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Shared dependencies
    dp["db"] = db
    dp["config"] = config

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

    me = await bot.get_me()
    logger.info("Starting KoshBox as @%s (id=%s)", me.username, me.id)
    if not me.supports_inline_queries:
        logger.warning(
            "Inline mode is DISABLED in @BotFather! Run /setinline in @BotFather for @%s.",
            me.username,
        )

    await _set_bot_commands(bot)

    # Pre-warm custom 3D UI banners into Dump Channel ONCE (reuses SQLite file_id on subsequent boots)
    banner_status = await warmup_banners_in_dump_channel(bot, db, config)
    logger.info("UI Banner cache status (Dump Channel): %s", banner_status)

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
