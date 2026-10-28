from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

from voicebot.access_ui import router as access_router
from voicebot.config import load_config
from voicebot.db import Database
from voicebot.handlers import AppContext, router
from voicebot.stt_routerai import RouterAITranscriber
from voicebot.runtime import InstanceLock
from voicebot.tariffs import PriceCatalog
from voicebot.formatter import RouterAIFormatter


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
    tariff_path = Path(os.getenv("TARIFF_CACHE_PATH", str(Path(db_path).resolve().parent / "routerai-prices.json"))).resolve()
    if tariff_path.is_relative_to(Path(__file__).resolve().parent):
        raise RuntimeError("TARIFF_CACHE_PATH must be outside application code/releases; use an external runtime directory")
    with InstanceLock(os.getenv('LOCK_PATH', db_path + '.lock')):
        db = Database(db_path)
        await db.open()
        await db.recover_paid_calls()
        await db.upsert_config_admins(config.admin_user_ids)

        transcriber = RouterAITranscriber(routerai_key, base_url=config.routerai_base_url)
        formatter = RouterAIFormatter(routerai_key,config.formatter,config.routerai_base_url) if config.formatter.enabled else None
        prices = PriceCatalog(tariff_path, config.tariffs)
        prices.load()
        ctx = AppContext(config=config, db=db, transcriber=transcriber, formatter=formatter, prices=prices)

        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
        dp = Dispatcher()
        dp.include_router(access_router)
        dp.include_router(router)

        logging.info("Starting @%s", config.bot_username or "unknown")
        prices_task = asyncio.create_task(prices.run(), name="published-tariffs-refresh")
        try:
            # Сохранить сообщения, накопленные во время обновления.
            await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot, ctx=ctx)
        finally:
            prices_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await prices_task
            await db.close()
            await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
