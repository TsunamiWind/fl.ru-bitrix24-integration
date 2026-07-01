import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "bridge.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dialogs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flru_type TEXT NOT NULL,
                flru_dialog_id TEXT NOT NULL,
                flru_project_id TEXT,
                b24_chat_id INTEGER,
                flru_other_user_id INTEGER,
                flru_other_user_name TEXT,
                last_flru_message_id INTEGER DEFAULT 0,
                last_flru_message_time REAL DEFAULT 0,
                last_b24_message_id INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(flru_type, flru_dialog_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS b24_event_offset (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                offset INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            INSERT OR IGNORE INTO b24_event_offset (id, offset) VALUES (1, 0)
        """)
        await db.commit()


async def get_dialog(flru_type: str, flru_dialog_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM dialogs WHERE flru_type = ? AND flru_dialog_id = ?",
            (flru_type, flru_dialog_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_dialog_by_b24_chat_id(b24_chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM dialogs WHERE b24_chat_id = ?", (b24_chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def upsert_dialog(
    flru_type: str,
    flru_dialog_id: str,
    flru_project_id: str = None,
    b24_chat_id: int = None,
    flru_other_user_id: int = None,
    flru_other_user_name: str = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO dialogs (flru_type, flru_dialog_id, flru_project_id, b24_chat_id,
                                 flru_other_user_id, flru_other_user_name)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(flru_type, flru_dialog_id) DO UPDATE SET
                flru_project_id = COALESCE(excluded.flru_project_id, dialogs.flru_project_id),
                b24_chat_id = COALESCE(excluded.b24_chat_id, dialogs.b24_chat_id),
                flru_other_user_id = COALESCE(excluded.flru_other_user_id, dialogs.flru_other_user_id),
                flru_other_user_name = COALESCE(excluded.flru_other_user_name, dialogs.flru_other_user_name)
            """,
            (flru_type, flru_dialog_id, flru_project_id, b24_chat_id,
             flru_other_user_id, flru_other_user_name),
        )
        await db.commit()


async def update_last_flru_message(
    flru_type: str, flru_dialog_id: str, message_id: int, message_time: float
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE dialogs SET last_flru_message_id = MAX(last_flru_message_id, ?),
                               last_flru_message_time = MAX(last_flru_message_time, ?)
            WHERE flru_type = ? AND flru_dialog_id = ?
            """,
            (message_id, message_time, flru_type, flru_dialog_id),
        )
        await db.commit()


async def update_last_b24_message(b24_chat_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE dialogs SET last_b24_message_id = MAX(last_b24_message_id, ?) WHERE b24_chat_id = ?",
            (message_id, b24_chat_id),
        )
        await db.commit()


async def get_event_offset():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT offset FROM b24_event_offset WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def set_event_offset(offset: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE b24_event_offset SET offset = ? WHERE id = 1", (offset,))
        await db.commit()
