from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

from voicebot.config import load_config
from voicebot.db import Database
from voicebot.handlers import AppContext, router
from voicebot.stt_routerai import RouterAITranscriber


async def main() -> None:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    routerai_key = os.getenv("ROUTERAI_API_KEY")
    config_path = os.getenv("CONFIG_PATH", "config.yaml")
    db_path = os.getenv("DB_PATH", "data/bot.sqlite3")

    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing in .env")
    if not routerai_key:
        raise RuntimeError("ROUTERAI_API_KEY is missing in .env")
    if not Path(config_path).exists():
        raise RuntimeError(f"Config file not found: {config_path}")

    config = load_config(config_path)
    db = Database(db_path)
    await db.open()
    await db.upsert_config_admins(config.admin_user_ids)

    transcriber = RouterAITranscriber(routerai_key, base_url=config.routerai_base_url)
    ctx = AppContext(config=config, db=db, transcriber=transcriber)

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp.include_router(router)

    logging.info("Starting @%s", config.bot_username or "unknown")
    try:
        # Drop old pending updates from experiments so the bot starts cleanly.
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, ctx=ctx)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
