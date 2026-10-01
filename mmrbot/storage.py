"""SQLite-хранилище: чаты, игроки, засчитанные ранкед-матчи.

Соединение открывается на каждый вызов (объём операций крошечный, конкуренция низкая),
что снимает проблему «SQLite objects can only be used in the same thread» с aiogram.
Матчи хранятся в сыром виде (player_slot/radiant_win), чтобы stats.aggregate оставался
единственным источником истины для расчёта побед/поражений.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Optional

DEFAULT_DIGEST_HOUR = 10
DEFAULT_MMR_STEP = 25
DEFAULT_TZ = "Europe/Moscow"


@dataclass
class Chat:
    chat_id: int
    digest_hour: int
    mmr_step: int
    tz: str
    last_digest_date: Optional[str] = None  # ISO-дата последнего отправленного дайджеста
    notify_steam: bool = True  # оповещать о смене ника/аватарки Steam
    notify_games: bool = True  # оповещать о новых играх и достижениях
    notify_weekly: bool = True  # недельная сводка
    last_weekly: Optional[str] = None  # ключ ISO-недели последней отправленной сводки


@dataclass
class Player:
    id: int
    chat_id: int
    account_id: int
    display_name: str
    anchor_mmr: Optional[int]
    anchor_ts: int
    created_ts: int
    last_rank_tier: Optional[int]
    last_leaderboard_rank: Optional[int]
    updated_ts: Optional[int]
    last_gpm: Optional[float] = None
    last_xpm: Optional[float] = None
    last_last_hits: Optional[float] = None
    last_lanes: Optional[str] = None
    last_gpm_median: Optional[float] = None
    last_gpm_best: Optional[float] = None
    steam_name: Optional[str] = None  # последний известный ник в Steam (для оповещений о смене)
    steam_avatar: Optional[str] = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id          INTEGER PRIMARY KEY,
    digest_hour      INTEGER NOT NULL DEFAULT 10,
    mmr_step         INTEGER NOT NULL DEFAULT 25,
    tz               TEXT    NOT NULL DEFAULT 'Europe/Moscow',
    last_digest_date TEXT,
    notify_steam     INTEGER NOT NULL DEFAULT 1,
    notify_games     INTEGER NOT NULL DEFAULT 1,
    notify_weekly    INTEGER NOT NULL DEFAULT 1,
    last_weekly      TEXT
);
CREATE TABLE IF NOT EXISTS achievements (
    player_id INTEGER NOT NULL,
    code      TEXT    NOT NULL,
    earned_ts INTEGER NOT NULL,
    detail    TEXT,
    PRIMARY KEY (player_id, code)
);
CREATE TABLE IF NOT EXISTS players (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id               INTEGER NOT NULL,
    account_id            INTEGER NOT NULL,
    display_name          TEXT    NOT NULL,
    anchor_mmr            INTEGER,
    anchor_ts             INTEGER NOT NULL,
    created_ts            INTEGER NOT NULL,
    last_rank_tier        INTEGER,
    last_leaderboard_rank INTEGER,
    updated_ts            INTEGER,
    last_gpm              REAL,
    last_xpm              REAL,
    last_last_hits        REAL,
    last_lanes            TEXT,
    last_gpm_median       REAL,
    last_gpm_best         REAL,
    steam_name            TEXT,
    steam_avatar          TEXT,
    UNIQUE(chat_id, account_id)
);
CREATE TABLE IF NOT EXISTS matches (
    player_id   INTEGER NOT NULL,
    match_id    INTEGER NOT NULL,
    start_time  INTEGER NOT NULL,
    player_slot INTEGER NOT NULL,
    radiant_win INTEGER NOT NULL,
    lobby_type  INTEGER,
    kills       INTEGER NOT NULL DEFAULT 0,
    deaths      INTEGER NOT NULL DEFAULT 0,
    assists     INTEGER NOT NULL DEFAULT 0,
    hero_id      INTEGER,
    duration     INTEGER,
    party_size   INTEGER,
    average_rank INTEGER,
    gpm          REAL,
    xpm          REAL,
    last_hits    INTEGER,
    denies       INTEGER,
    hero_damage  INTEGER,
    tower_damage INTEGER,
    hero_healing INTEGER,
    net_worth    INTEGER,
    level        INTEGER,
    perf_score   REAL,
    bench_json   TEXT,
    enriched     INTEGER NOT NULL DEFAULT 0,
    position     INTEGER,
    role         TEXT,
    lane         TEXT,
    imp          INTEGER,
    stratz_done  INTEGER NOT NULL DEFAULT 0,
    stratz_tries INTEGER NOT NULL DEFAULT 0,
    enrich_tries INTEGER NOT NULL DEFAULT 0,
    notified     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (player_id, match_id)
);
"""


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Лёгкие миграции для БД, созданных предыдущими версиями схемы."""
        def add_missing(table: str, columns: dict[str, str]) -> None:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

        add_missing("chats", {
            "last_digest_date": "TEXT", "notify_steam": "INTEGER NOT NULL DEFAULT 1",
            "notify_games": "INTEGER NOT NULL DEFAULT 1", "notify_weekly": "INTEGER NOT NULL DEFAULT 1",
            "last_weekly": "TEXT",
        })
        # Старая история — уже «оповещённая»: иначе после обновления бот завалил бы чат старыми играми.
        had_notified = "notified" in {r["name"] for r in conn.execute("PRAGMA table_info(matches)").fetchall()}
        if not had_notified:
            conn.execute("ALTER TABLE matches ADD COLUMN notified INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE matches SET notified = 1")
        add_missing("players", {
            "last_gpm": "REAL", "last_xpm": "REAL", "last_last_hits": "REAL",
            "last_lanes": "TEXT", "last_gpm_median": "REAL", "last_gpm_best": "REAL",
            "steam_name": "TEXT", "steam_avatar": "TEXT",
        })
        add_missing("matches", {
            "duration": "INTEGER", "party_size": "INTEGER", "average_rank": "INTEGER",
            "gpm": "REAL", "xpm": "REAL", "last_hits": "INTEGER", "denies": "INTEGER",
            "hero_damage": "INTEGER", "tower_damage": "INTEGER", "hero_healing": "INTEGER",
            "net_worth": "INTEGER", "level": "INTEGER", "perf_score": "REAL",
            "bench_json": "TEXT", "enriched": "INTEGER NOT NULL DEFAULT 0",
            "position": "INTEGER", "role": "TEXT", "lane": "TEXT", "imp": "INTEGER",
            "stratz_done": "INTEGER NOT NULL DEFAULT 0",
            "stratz_tries": "INTEGER NOT NULL DEFAULT 0",
            "enrich_tries": "INTEGER NOT NULL DEFAULT 0",
        })

    def _conn(self) -> sqlite3.Connection:
        # timeout: фоновые джобы и хендлеры пишут из разных потоков — ждём блокировку, а не падаем.
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")  # читатели не блокируют писателя
        return conn

    # --- chats ----------------------------------------------------------

    def get_or_create_chat(self, chat_id: int) -> Chat:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO chats (chat_id, digest_hour, mmr_step, tz) VALUES (?, ?, ?, ?)",
                    (chat_id, DEFAULT_DIGEST_HOUR, DEFAULT_MMR_STEP, DEFAULT_TZ),
                )
                row = conn.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
            return self._chat_from_row(row)

    @staticmethod
    def _chat_from_row(row: sqlite3.Row) -> Chat:
        return Chat(
            chat_id=row["chat_id"],
            digest_hour=row["digest_hour"],
            mmr_step=row["mmr_step"],
            tz=row["tz"],
            last_digest_date=row["last_digest_date"],
            notify_steam=bool(row["notify_steam"]),
            notify_games=bool(row["notify_games"]),
            notify_weekly=bool(row["notify_weekly"]),
            last_weekly=row["last_weekly"],
        )

    def set_chat_notify_games(self, chat_id: int, enabled: bool) -> None:
        self.get_or_create_chat(chat_id)
        with self._conn() as conn:
            conn.execute("UPDATE chats SET notify_games = ? WHERE chat_id = ?", (1 if enabled else 0, chat_id))

    def set_chat_notify_weekly(self, chat_id: int, enabled: bool) -> None:
        self.get_or_create_chat(chat_id)
        with self._conn() as conn:
            conn.execute("UPDATE chats SET notify_weekly = ? WHERE chat_id = ?", (1 if enabled else 0, chat_id))

    def set_last_weekly(self, chat_id: int, key: str) -> None:
        self.get_or_create_chat(chat_id)
        with self._conn() as conn:
            conn.execute("UPDATE chats SET last_weekly = ? WHERE chat_id = ?", (key, chat_id))

    def set_chat_tz(self, chat_id: int, tz: str) -> None:
        self.get_or_create_chat(chat_id)
        with self._conn() as conn:
            conn.execute("UPDATE chats SET tz = ? WHERE chat_id = ?", (tz, chat_id))

    def set_chat_notify_steam(self, chat_id: int, enabled: bool) -> None:
        self.get_or_create_chat(chat_id)
        with self._conn() as conn:
            conn.execute("UPDATE chats SET notify_steam = ? WHERE chat_id = ?", (1 if enabled else 0, chat_id))

    def set_chat_step(self, chat_id: int, step: int) -> None:
        self.get_or_create_chat(chat_id)
        with self._conn() as conn:
            conn.execute("UPDATE chats SET mmr_step = ? WHERE chat_id = ?", (step, chat_id))

    def set_chat_digest_hour(self, chat_id: int, hour: int) -> None:
        self.get_or_create_chat(chat_id)
        with self._conn() as conn:
            conn.execute("UPDATE chats SET digest_hour = ? WHERE chat_id = ?", (hour, chat_id))

    def set_last_digest_date(self, chat_id: int, date_str: str) -> None:
        self.get_or_create_chat(chat_id)
        with self._conn() as conn:
            conn.execute("UPDATE chats SET last_digest_date = ? WHERE chat_id = ?", (date_str, chat_id))

    def list_chats(self) -> list[Chat]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM chats").fetchall()
        return [self._chat_from_row(r) for r in rows]

    # --- players --------------------------------------------------------

    def _player_from_row(self, row: sqlite3.Row) -> Player:
        return Player(
            id=row["id"],
            chat_id=row["chat_id"],
            account_id=row["account_id"],
            display_name=row["display_name"],
            anchor_mmr=row["anchor_mmr"],
            anchor_ts=row["anchor_ts"],
            created_ts=row["created_ts"],
            last_rank_tier=row["last_rank_tier"],
            last_leaderboard_rank=row["last_leaderboard_rank"],
            updated_ts=row["updated_ts"],
            last_gpm=row["last_gpm"],
            last_xpm=row["last_xpm"],
            last_last_hits=row["last_last_hits"],
            last_lanes=row["last_lanes"],
            last_gpm_median=row["last_gpm_median"],
            last_gpm_best=row["last_gpm_best"],
            steam_name=row["steam_name"],
            steam_avatar=row["steam_avatar"],
        )

    def update_player_steam(self, player_id: int, name: Optional[str], avatar: Optional[str]) -> dict:
        """Сохранить ник/аватарку Steam; вернуть изменения относительно прошлых значений.

        Первое заполнение (раньше значения не было) изменением не считается. Пустые значения
        не затирают сохранённые.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT steam_name, steam_avatar FROM players WHERE id = ?", (player_id,)
            ).fetchone()
            if row is None:
                return {}
            old_name, old_avatar = row["steam_name"], row["steam_avatar"]
            changes: dict = {}
            if name and old_name is not None and name != old_name:
                changes["name"] = (old_name, name)
            if avatar and old_avatar is not None and avatar != old_avatar:
                changes["avatar"] = True
            conn.execute(
                "UPDATE players SET steam_name = ?, steam_avatar = ? WHERE id = ?",
                (name or old_name, avatar or old_avatar, player_id),
            )
        return changes

    def add_player(
        self,
        chat_id: int,
        account_id: int,
        display_name: str,
        anchor_mmr: Optional[int],
        anchor_ts: int,
        created_ts: int,
    ) -> Player:
        with self._conn() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO players (chat_id, account_id, display_name, anchor_mmr, anchor_ts, created_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (chat_id, account_id, display_name, anchor_mmr, anchor_ts, created_ts),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Этот аккаунт уже добавлен в этот чат.") from exc
            row = conn.execute("SELECT * FROM players WHERE id = ?", (cur.lastrowid,)).fetchone()
            return self._player_from_row(row)

    def get_player(self, chat_id: int, key: str) -> Optional[Player]:
        # Регистронезависимое сравнение делаем в Python: SQLite lower() не трогает кириллицу.
        key = (key or "").strip()
        key_lower = key.lower()
        players = self.list_players(chat_id)
        for player in players:
            if player.display_name.lower() == key_lower:
                return player
        # str.isdigit() шире int() (unicode-цифры), поэтому int() под защитой.
        try:
            account_id = int(key)
        except ValueError:
            return None
        for player in players:
            if player.account_id == account_id:
                return player
        return None

    def get_player_by_account_id(self, chat_id: int, account_id: int) -> Optional[Player]:
        """Точный поиск по account_id (без неоднозначности имён) — для внутренней перечитки."""
        for player in self.list_players(chat_id):
            if player.account_id == account_id:
                return player
        return None

    def list_players(self, chat_id: int) -> list[Player]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM players WHERE chat_id = ? ORDER BY id", (chat_id,)
            ).fetchall()
        return [self._player_from_row(r) for r in rows]

    def remove_player(self, chat_id: int, key: str) -> bool:
        player = self.get_player(chat_id, key)
        if player is None:
            return False
        with self._conn() as conn:
            conn.execute("DELETE FROM matches WHERE player_id = ?", (player.id,))
            conn.execute("DELETE FROM achievements WHERE player_id = ?", (player.id,))
            conn.execute("DELETE FROM players WHERE id = ?", (player.id,))
        return True

    def set_player_anchor(self, player_id: int, anchor_mmr: int, anchor_ts: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE players SET anchor_mmr = ?, anchor_ts = ? WHERE id = ?",
                (anchor_mmr, anchor_ts, player_id),
            )

    def update_player_rank(
        self, player_id: int, rank_tier: Optional[int], leaderboard_rank: Optional[int], updated_ts: int
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE players SET last_rank_tier = ?, last_leaderboard_rank = ?, updated_ts = ? WHERE id = ?",
                (rank_tier, leaderboard_rank, updated_ts, player_id),
            )

    def update_player_totals(
        self, player_id: int, gpm: Optional[float], xpm: Optional[float], last_hits: Optional[float]
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE players SET last_gpm = ?, last_xpm = ?, last_last_hits = ? WHERE id = ?",
                (gpm, xpm, last_hits, player_id),
            )

    def update_player_insights(
        self, player_id: int, lanes_json: Optional[str], gpm_median: Optional[float], gpm_best: Optional[float]
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE players SET last_lanes = ?, last_gpm_median = ?, last_gpm_best = ? WHERE id = ?",
                (lanes_json, gpm_median, gpm_best, player_id),
            )

    # --- matches --------------------------------------------------------

    def add_matches(self, player_id: int, matches: list[dict]) -> int:
        """Вставить матчи (INSERT OR IGNORE по (player_id, match_id)). Вернуть число новых."""
        inserted = 0
        with self._conn() as conn:
            for m in matches:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO matches "
                    "(player_id, match_id, start_time, player_slot, radiant_win, lobby_type, "
                    " kills, deaths, assists, hero_id, duration, party_size, average_rank) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        player_id,
                        m["match_id"],
                        m["start_time"],
                        m["player_slot"],
                        1 if m["radiant_win"] else 0,
                        m.get("lobby_type"),
                        m.get("kills", 0) or 0,
                        m.get("deaths", 0) or 0,
                        m.get("assists", 0) or 0,
                        m.get("hero_id"),
                        m.get("duration"),
                        m.get("party_size"),
                        m.get("average_rank"),
                    ),
                )
                inserted += cur.rowcount
        return inserted

    _DETAIL_FIELDS = (
        "gpm", "xpm", "last_hits", "denies", "hero_damage",
        "tower_damage", "hero_healing", "net_worth", "level",
    )

    def update_match_details(self, player_id: int, match_id: int, details: dict, perf_score) -> None:
        """Записать обогащённые пер-матч поля + perf_score + benchmarks(JSON), пометить enriched=1."""
        assignments = ", ".join(f"{field} = ?" for field in self._DETAIL_FIELDS)
        bench_json = json.dumps(details.get("benchmarks") or {})
        params = [details.get(field) for field in self._DETAIL_FIELDS]
        params += [perf_score, bench_json, player_id, match_id]
        with self._conn() as conn:
            conn.execute(
                f"UPDATE matches SET {assignments}, perf_score = ?, bench_json = ?, enriched = 1 "
                "WHERE player_id = ? AND match_id = ?",
                params,
            )

    def update_match_stratz(self, player_id: int, match_id: int, info: dict) -> None:
        """Записать данные Stratz: позиция/роль/лейн/IMP + дозаполнить пустые числовые поля."""
        fill = ", ".join(f"{f} = COALESCE({f}, ?)" for f in self._DETAIL_FIELDS + ("party_size",))
        params = [info.get("position"), info.get("role"), info.get("lane"), info.get("imp")]
        params += [info.get(f) for f in self._DETAIL_FIELDS + ("party_size",)]
        params += [player_id, match_id]
        with self._conn() as conn:
            conn.execute(
                f"UPDATE matches SET position = ?, role = ?, lane = ?, imp = ?, stratz_done = 1, {fill} "
                "WHERE player_id = ? AND match_id = ?",
                params,
            )

    def get_match_ids_without_stratz(
        self, player_id: int, since_ts: int, limit: int = 20, max_tries: int = 5
    ) -> list[int]:
        """Матчи, которых Stratz ещё не отдал (свежие первыми); после max_tries промахов — сдаёмся."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT match_id FROM matches WHERE player_id = ? AND (stratz_done = 0 OR party_size IS NULL) "
                "AND stratz_tries < ? AND start_time >= ? ORDER BY start_time DESC LIMIT ?",
                (player_id, max_tries, since_ts, limit),
            ).fetchall()
        return [r["match_id"] for r in rows]

    def mark_stratz_miss(self, player_id: int, match_ids: list[int]) -> None:
        """Stratz не вернул матч — копим попытки, чтобы не спрашивать вечно."""
        with self._conn() as conn:
            conn.executemany(
                "UPDATE matches SET stratz_tries = stratz_tries + 1 WHERE player_id = ? AND match_id = ?",
                [(player_id, mid) for mid in match_ids],
            )

    def get_unenriched_match_ids(
        self, player_id: int, since_ts: int, limit: int, max_tries: int = 3
    ) -> list[int]:
        """match_id матчей без обогащения (свежие первыми); после max_tries пустых ответов — сдаёмся."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT match_id FROM matches WHERE player_id = ? AND enriched = 0 "
                "AND enrich_tries < ? AND start_time >= ? ORDER BY start_time DESC LIMIT ?",
                (player_id, max_tries, since_ts, limit),
            ).fetchall()
        return [r["match_id"] for r in rows]

    def mark_enrich_miss(self, player_id: int, match_id: int) -> None:
        """OpenDota не отдал детали матча — копим попытки, чтобы он не блокировал очередь."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE matches SET enrich_tries = enrich_tries + 1 WHERE player_id = ? AND match_id = ?",
                (player_id, match_id),
            )

    def get_unnotified_matches(self, player_id: int, since_ts: int) -> list[dict]:
        """Матчи, о которых ещё не оповещали, не старше since_ts (свежие — кандидаты на оповещение)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM matches WHERE player_id = ? AND notified = 0 AND start_time >= ? ORDER BY start_time",
                (player_id, since_ts),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_notified(self, player_id: int) -> None:
        """Пометить все текущие матчи игрока оповещёнными (в т.ч. старые — их не объявляем)."""
        with self._conn() as conn:
            conn.execute("UPDATE matches SET notified = 1 WHERE player_id = ? AND notified = 0", (player_id,))

    # --- достижения -------------------------------------------------------

    def get_achievements(self, player_id: int) -> dict[str, tuple[int, Optional[str]]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT code, earned_ts, detail FROM achievements WHERE player_id = ?", (player_id,)
            ).fetchall()
        return {r["code"]: (r["earned_ts"], r["detail"]) for r in rows}

    def add_achievements(self, player_id: int, items: dict, earned_ts: int) -> None:
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO achievements (player_id, code, earned_ts, detail) VALUES (?, ?, ?, ?)",
                [(player_id, code, earned_ts, detail) for code, detail in items.items()],
            )

    def get_matches(self, player_id: int, since_ts: Optional[int] = None) -> list[dict]:
        query = "SELECT * FROM matches WHERE player_id = ?"
        params: list = [player_id]
        if since_ts is not None:
            query += " AND start_time >= ?"
            params.append(since_ts)
        query += " ORDER BY start_time"
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
