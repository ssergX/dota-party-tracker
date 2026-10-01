"""Точка входа: python -m mmrbot — long-polling + планировщик дайджеста."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import ErrorEvent

from mmrbot.bot import router, set_bot_commands
from mmrbot.charts import warmup
from mmrbot.config import load_config
from mmrbot.opendota import OpenDota
from mmrbot.scheduler import setup_scheduler
from mmrbot.stratz import Stratz
from mmrbot.storage import Storage


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()

    storage = Storage(config.db_path)
    od = OpenDota(api_key=config.opendota_api_key)
    stratz = Stratz(config.stratz_api_key) if config.stratz_api_key else None

    bot = Bot(config.bot_token, default=DefaultBotProperties(link_preview_is_disabled=True))
    dp = Dispatcher()
    dp["storage"] = storage
    dp["od"] = od
    dp["stratz"] = stratz
    dp.include_router(router)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        # Страховочная сеть: любой неотловленный сбой хендлера — в лог, а не в падение.
        logging.getLogger(__name__).exception("Необработанная ошибка хендлера: %s", event.exception)
        return True

    await set_bot_commands(bot)
    asyncio.get_running_loop().run_in_executor(None, warmup)  # прогрев matplotlib: первый график без задержки

    scheduler = setup_scheduler(bot, storage, od, stratz, backup_keep=config.backup_keep)
    scheduler.start()
    logging.getLogger(__name__).info("Бот запущен (long-polling). Ctrl+C для остановки.")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
