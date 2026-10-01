"""Ежедневный бэкап SQLite-базы (через sqlite3 backup API — безопасно при работающем боте)."""
from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def backup_db(db_path: str, keep: int, today: Optional[date] = None) -> Optional[Path]:
    """Сделать копию базы в `<папка базы>/backups/<имя>-ГГГГММДД.db` и оставить `keep` последних.

    Возвращает путь новой копии; None — бэкап выключен (keep=0), базы нет или за сегодня копия уже есть.
    """
    source = Path(db_path)
    if keep <= 0 or not source.exists():
        return None
    folder = source.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{source.stem}-{(today or date.today()).strftime('%Y%m%d')}.db"
    if target.exists():
        return None
    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    copies = sorted(folder.glob(f"{source.stem}-*.db"))
    for old in copies[:-keep]:
        try:
            old.unlink()
        except OSError:
            log.warning("Не удалось удалить старый бэкап %s", old, exc_info=True)
    log.info("Бэкап БД: %s", target)
    return target
