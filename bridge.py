import asyncio
import json
import logging
import os
import sys

from db import (
    init_db,
    get_dialog,
    get_dialog_by_b24_chat_id,
    upsert_dialog,
    update_last_flru_message,
    update_last_b24_message,
)
from b24_client import B24Client
from flru_client import FlRuClient, clean_operator_text, FlRuAuthError, FlRuSendError

log = logging.getLogger("bridge")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

# ── Типы диалогов fl.ru ──
DT_OFFER = "offer"
DT_ORDER = "order"
DT_TSERVICE = "tservice"
DT_SYSTEM = "system"

# ── Префиксы ENTITY_TYPE для Bitrix24 ──
ET_OFFER = "FL_RU_OFFER"
ET_ORDER = "FL_RU_ORDER"
ET_TSERVICE = "FL_RU_TSERVICE"
ET_SYSTEM = "FL_RU_DIALOG"


class Bridge:
    def __init__(self, config: dict):
        self.config = config
        self.b24 = B24Client(
            domain=config["b24"]["domain"],
            webhook_user_id=config["b24"]["webhook_user_id"],
            webhook_secret=config["b24"]["webhook_secret"],
            bot_id=config["b24"]["bot_id"],
            bot_token=config["b24"]["bot_token"],
            your_user_id=config["b24"]["your_user_id"],
        )
        self.flru = FlRuClient(
            cookies=config["flru"]["cookies"],
            csrf_token=config["flru"]["csrf_token"],
            user_agent=config["flru"]["user_agent"],
        )
        self.flru_poll_interval = config["flru"].get("poll_interval_seconds", 30)
        self.b24_poll_interval = 5
        self._running = False

    async def start(self):
        await init_db()
        if self.config["flru"]["cookies"].get("PHPSESSID"):
            try:
                await self.flru.get_unread_counts()
            except FlRuAuthError as e:
                log.critical("Не удалось запустить мост: %s", e)
                return
        self._running = True
        log.info("Мост запущен")
        await asyncio.gather(
            self._poll_flru(),
            self._poll_b24(),
        )

    async def stop(self):
        self._running = False
        await self.b24.close()
        await self.flru.close()
        log.info("Мост остановлен")

    # ═══════════════════════════════════════════════════════
    #  fl.ru → Bitrix24
    # ═══════════════════════════════════════════════════════

    async def _poll_flru(self):
        while self._running:
            try:
                await self._check_flru_messages()
            except FlRuAuthError as e:
                log.critical("Остановка моста: %s", e)
                self._running = False
                return
            except Exception as e:
                log.error(f"Ошибка опроса fl.ru: {e}")
            await asyncio.sleep(self.flru_poll_interval)

    async def _check_flru_messages(self):
        if not self.config["flru"]["cookies"].get("PHPSESSID"):
            log.debug("Куки fl.ru не настроены — пропускаем опрос")
            return

        await asyncio.gather(
            self._sync_offers(),
            self._sync_orders(),
            self._sync_tservices(),
            self._sync_system_dialogs(),
        )

    async def _sync_offers(self):
        offers = await self.flru.list_offers()
        for offer in offers:
            await self._sync_offer_dialog(offer)

    async def _sync_offer_dialog(self, offer: dict):
        offer_id = str(offer["id"])
        project_id = str(offer["project_id"])
        project_title = offer.get("title", f"Проект #{project_id}")
        your_flru_id = self.config["flru"].get("your_user_id")

        dialog = await get_dialog(DT_OFFER, offer_id)

        msgs = await self.flru.get_offer_messages(
            int(project_id), int(offer_id)
        )
        items = msgs.get("items", [])
        if not items:
            return

        if not dialog:
            max_id = max(m.get("id", 0) for m in items)
            await upsert_dialog(DT_OFFER, offer_id)
            await update_last_flru_message(DT_OFFER, offer_id, max_id, 0)
            log.debug(f"База отклика {offer_id}: max_id={max_id}")
            return

        users_by_id = {u["id"]: u for u in msgs.get("users", [])}

        new_messages = []
        last_known_id = dialog["last_flru_message_id"]
        for msg in items:
            if msg["id"] > last_known_id and msg.get("from_id") != your_flru_id:
                new_messages.append(msg)

        if not new_messages:
            return

        other_name = self._resolve_other_name(msgs, your_flru_id)
        if not other_name:
            other_name = f"Заказчик #{project_id}"

        b24_chat_id = await self._ensure_b24_chat(
            flru_type=DT_OFFER,
            flru_dialog_id=offer_id,
            flru_project_id=project_id,
            other_user_name=other_name,
            title=f"FL.ru: {project_title}",
            description=f"Отклик на проекте #{project_id}: {project_title}",
            entity_type=ET_OFFER,
            entity_id=offer_id,
        )
        if not b24_chat_id:
            return

        dialog_id = f"chat{b24_chat_id}"
        for msg in reversed(new_messages):
            await self._forward_flru_message(msg, dialog_id, other_name, users_by_id,
                                              DT_OFFER, offer_id)
            await asyncio.sleep(1)

    async def _forward_flru_message(self, msg: dict, dialog_id: str,
                                      sender_name: str, users_by_id: dict,
                                      flru_type: str, flru_dialog_id: str):
        text = msg.get("text", "")
        files = msg.get("files", [])

        if files:
            for f in files:
                try:
                    file_url = f.get("url", "")
                    file_name = f.get("source_name", f.get("name", "файл"))
                    if not file_url:
                        continue
                    log.info(f"  fl.ru → B24 [{flru_type} {flru_dialog_id}]: скачиваем {file_name}")
                    file_content = await self.flru.download_file(file_url)
                    caption = f"[B]{sender_name}:[/B]"
                    await self.b24.bot_upload_file(dialog_id, file_name, file_content, caption)
                    await asyncio.sleep(1)
                except Exception as e:
                    log.error(f"  Не удалось переслать файл {file_name}: {e}")

        if text and text.strip():
            sender = self._get_sender_name(msg, users_by_id, sender_name)
            log.info(f"  fl.ru → B24 [{flru_type} {flru_dialog_id}]: {sender}: {text[:80]}")
            await self.b24.bot_send_offer_message(dialog_id, sender, text)

        await update_last_flru_message(
            flru_type, flru_dialog_id, msg["id"], msg.get("time", 0)
        )

    def _resolve_other_name(self, msgs: dict, your_flru_id: int) -> str:
        from_id = None
        for msg in msgs.get("items", []):
            fid = msg.get("from_id")
            if fid and fid != your_flru_id:
                from_id = fid
                break
        if not from_id:
            return ""
        for u in msgs.get("users", []):
            if u.get("id") == from_id:
                return u.get("full_name") or u.get("name") or ""
        projects = msgs.get("projects", {})
        if isinstance(projects, dict):
            proj = projects.get(str(from_id), {})
            return proj.get("name", "") if isinstance(proj, dict) else str(proj)
        return ""

    def _get_sender_name(self, msg: dict, users_by_id: dict, fallback: str) -> str:
        from_id = msg.get("from_id")
        if from_id and from_id in users_by_id:
            u = users_by_id[from_id]
            return u.get("full_name") or u.get("name") or fallback
        return fallback

    async def _sync_orders(self):
        orders = await self.flru.list_orders()
        for order in orders:
            await self._sync_order_dialog(order)

    async def _sync_order_dialog(self, order: dict):
        order_id = str(order["id"])
        other_name = "Клиент"
        other_user_id = None
        try:
            employer = order.get("employer", {})
            freelancer = order.get("freelancer", {})
            if employer and freelancer:
                emp_id = employer.get("id") if isinstance(employer, dict) else employer
                if emp_id == self.config["b24"]["your_user_id"]:
                    other = freelancer if isinstance(freelancer, dict) else {"name": str(freelancer)}
                else:
                    other = employer if isinstance(employer, dict) else {"name": str(employer)}
                other_name = other.get("name", "Клиент")
                other_user_id = other.get("id")
        except Exception:
            pass

        dialog = await get_dialog(DT_ORDER, order_id)
        msgs = await self.flru.get_order_messages(int(order_id))
        items = msgs.get("items", [])
        if not items:
            return

        if not dialog:
            max_id = max(m.get("id", 0) for m in items)
            await upsert_dialog(DT_ORDER, order_id)
            await update_last_flru_message(DT_ORDER, order_id, max_id, 0)
            log.debug(f"База заказа {order_id}: max_id={max_id}")
            return

        new_messages = []
        last_known_id = dialog["last_flru_message_id"]
        for msg in items:
            if msg.get("id", 0) > last_known_id:
                new_messages.append(msg)

        if not new_messages:
            return

        b24_chat_id = await self._ensure_b24_chat(
            flru_type=DT_ORDER,
            flru_dialog_id=order_id,
            other_user_id=other_user_id,
            other_user_name=other_name,
            title=f"FL.ru Заказ: {other_name}",
            description=f"Безопасная сделка #{order_id}",
            entity_type=ET_ORDER,
            entity_id=order_id,
        )
        if not b24_chat_id:
            return

        dialog_id = f"chat{b24_chat_id}"
        for msg in reversed(new_messages):
            await self._forward_flru_message(msg, dialog_id, other_name, {},
                                              DT_ORDER, order_id)
            await asyncio.sleep(1)

    async def _sync_tservices(self):
        dialogs = await self.flru.list_tservice_dialogs()
        for d in dialogs:
            await self._sync_tservice_dialog(d)

    async def _sync_tservice_dialog(self, d: dict):
        dialog_id_val = str(d["id"])
        other_user = d.get("user", {})
        other_name = other_user.get("name", f"Пользователь {other_user.get('id', '?')}")

        dialog = await get_dialog(DT_TSERVICE, dialog_id_val)
        msgs = await self.flru.get_tservice_messages(int(dialog_id_val))
        items = msgs.get("items", [])
        if not items:
            return

        if not dialog:
            max_id = max(m.get("id", 0) for m in items)
            await upsert_dialog(DT_TSERVICE, dialog_id_val)
            await update_last_flru_message(DT_TSERVICE, dialog_id_val, max_id, 0)
            log.debug(f"База услуги {dialog_id_val}: max_id={max_id}")
            return

        new_messages = []
        last_known_id = dialog["last_flru_message_id"]
        for msg in items:
            if msg.get("id", 0) > last_known_id:
                new_messages.append(msg)

        if not new_messages:
            return

        b24_chat_id = await self._ensure_b24_chat(
            flru_type=DT_TSERVICE,
            flru_dialog_id=dialog_id_val,
            other_user_name=other_name,
            title=f"FL.ru Услуга: {other_name}",
            description=f"Диалог типовой услуги #{dialog_id_val}",
            entity_type=ET_TSERVICE,
            entity_id=dialog_id_val,
        )
        if not b24_chat_id:
            return

        b24_dialog_id = f"chat{b24_chat_id}"
        for msg in reversed(new_messages):
            await self._forward_flru_message(msg, b24_dialog_id, other_name, {},
                                              DT_TSERVICE, dialog_id_val)
            await asyncio.sleep(1)

    async def _sync_system_dialogs(self):
        dialogues = await self.flru.list_dialogues()
        if not isinstance(dialogues, list):
            return
        for d in dialogues:
            await self._sync_system_dialog(d)

    async def _sync_system_dialog(self, d: dict):
        dialog_id_val = str(d.get("id", d.get("dialog_id", "")))
        if not dialog_id_val:
            return
        other_name = d.get("name", d.get("user_name", "Пользователь"))

        dialog = await get_dialog(DT_SYSTEM, dialog_id_val)
        msgs = await self.flru.get_dialogue_messages(int(dialog_id_val))
        items = msgs.get("items", []) if isinstance(msgs, dict) else (msgs if isinstance(msgs, list) else [])
        if not items:
            return

        if not dialog:
            max_id = max(m.get("id", 0) for m in items)
            await upsert_dialog(DT_SYSTEM, dialog_id_val)
            await update_last_flru_message(DT_SYSTEM, dialog_id_val, max_id, 0)
            log.debug(f"База диалога {dialog_id_val}: max_id={max_id}")
            return

        new_messages = []
        last_known_id = dialog["last_flru_message_id"]
        for msg in items:
            if msg.get("id", 0) > last_known_id:
                new_messages.append(msg)

        if not new_messages:
            return

        other_user_id = d.get("user_id", d.get("id"))
        b24_chat_id = await self._ensure_b24_chat(
            flru_type=DT_SYSTEM,
            flru_dialog_id=dialog_id_val,
            other_user_id=other_user_id,
            other_user_name=other_name,
            title=f"FL.ru: {other_name}",
            description=f"Личное сообщение от {other_name}",
            entity_type=ET_SYSTEM,
            entity_id=dialog_id_val,
        )
        if not b24_chat_id:
            return

        b24_dialog_id = f"chat{b24_chat_id}"
        for msg in reversed(new_messages):
            await self._forward_flru_message(msg, b24_dialog_id, other_name, {},
                                              DT_SYSTEM, dialog_id_val)
            await asyncio.sleep(1)

    async def _ensure_b24_chat(self, flru_type: str, flru_dialog_id: str,
                                flru_project_id: str = None, other_user_id: int = None,
                                other_user_name: str = "Клиент", title: str = "",
                                description: str = "", entity_type: str = "",
                                entity_id: str = "") -> int | None:
        existing = await get_dialog(flru_type, flru_dialog_id)
        if existing and existing.get("b24_chat_id"):
            return existing["b24_chat_id"]

        try:
            b24_chat_id = await self.b24.get_chat_by_entity(entity_type, entity_id)
        except Exception:
            b24_chat_id = None

        if not b24_chat_id:
            log.info(f"Создаём чат Bitrix24: {title}")
            b24_chat_id = await self.b24.create_chat(
                title=title,
                description=description,
                entity_type=entity_type,
                entity_id=entity_id,
            )
            await asyncio.sleep(1)
            try:
                msgs = await self.b24.get_messages(f"chat{b24_chat_id}", limit=1)
                latest = msgs.get("messages", [])
                if latest:
                    await update_last_b24_message(b24_chat_id, latest[0]["id"])
            except Exception:
                pass

        await upsert_dialog(
            flru_type=flru_type,
            flru_dialog_id=flru_dialog_id,
            flru_project_id=flru_project_id,
            b24_chat_id=b24_chat_id,
            flru_other_user_id=other_user_id,
            flru_other_user_name=other_user_name,
        )
        return b24_chat_id

    # ═══════════════════════════════════════════════════════
    #  Bitrix24 → fl.ru
    # ═══════════════════════════════════════════════════════

    async def _poll_b24(self):
        while self._running:
            try:
                await self._check_b24_replies()
            except Exception as e:
                log.error(f"Ошибка опроса B24: {e}")
            await asyncio.sleep(self.b24_poll_interval)

    async def _check_b24_replies(self):
        import aiosqlite
        from db import DB_PATH

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM dialogs WHERE b24_chat_id IS NOT NULL"
            ) as cursor:
                dialogs = [dict(row) for row in await cursor.fetchall()]

        for d in dialogs:
            try:
                await self._sync_b24_reply(d)
            except Exception as e:
                log.error(f"Ошибка синхронизации ответа B24 [чат {d.get('b24_chat_id')}]: {e}")

    async def _sync_b24_reply(self, dialog: dict):
        b24_chat_id = dialog["b24_chat_id"]
        last_known_id = dialog.get("last_b24_message_id", 0)
        dialog_id = f"chat{b24_chat_id}"
        your_id = self.config["b24"]["your_user_id"]

        try:
            msgs = await self.b24.get_messages(dialog_id, limit=20, first_id=last_known_id)
        except Exception as e:
            return

        messages = msgs.get("messages", [])
        if not messages:
            return

        if not last_known_id:
            latest_id = max(m["id"] for m in messages)
            await update_last_b24_message(b24_chat_id, latest_id)
            log.debug(f"База ответов чата {b24_chat_id}: last_msg_id={latest_id}")
            return

        new_from_you = [
            m for m in messages
            if m["id"] > last_known_id and m["author_id"] == your_id
        ]
        if not new_from_you:
            return

        new_from_you.sort(key=lambda m: m["id"])

        top_files = msgs.get("files", []) if isinstance(msgs.get("files"), list) else []

        flru_type = dialog["flru_type"]
        flru_dialog_id = dialog["flru_dialog_id"]

        for msg in new_from_you:
            text = clean_operator_text(msg.get("text", ""))
            b24_files = msg.get("files", []) if isinstance(msg.get("files"), list) else []
            if top_files:
                b24_files = b24_files + top_files
                top_files = []

            log.info(f"  B24 → fl.ru [{flru_type} {flru_dialog_id}]: текст={text[:80]} файлов={len(b24_files)}")
            try:
                await self._send_reply_to_flru(dialog, text, b24_files, msg["id"])
            except FlRuAuthError:
                raise
            except Exception as e:
                log.error(
                    f"  Не удалось доставить сообщение на fl.ru (msg #{msg['id']}): {e}. "
                    f"Повтор при следующем опросе."
                )
                return
            await update_last_b24_message(b24_chat_id, msg["id"])
            await asyncio.sleep(1)

    async def _send_reply_to_flru(self, dialog: dict, text: str, b24_files: list, b24_msg_id: int):
        flru_type = dialog["flru_type"]
        flru_dialog_id = dialog["flru_dialog_id"]
        project_id = dialog.get("flru_project_id")
        other_user_id = dialog.get("flru_other_user_id")

        if not text.strip() and not b24_files:
            return

        file_urls = []
        file_failures = 0
        for f in b24_files:
            file_name = f.get("name", "файл")
            try:
                file_content = await self.b24.download_file_v2(
                    f.get("id", 0), f.get("chatId", 0))
                cdn_url = await self.flru.upload_file_legacy(
                    file_content, file_name,
                    f.get("type", "application/octet-stream"))
                file_urls.append(cdn_url)
                log.info(f"    загружен {file_name} → {cdn_url}")
                await asyncio.sleep(1)
            except Exception as e:
                file_failures += 1
                log.error(f"    не удалось загрузить {file_name} на fl.ru: {e}")

        if file_failures and not file_urls and not text.strip():
            raise FlRuSendError(
                f"все вложения ({file_failures}) не загрузились — сообщение не отправлено"
            )

        if file_urls:
            link_text = "\n".join(file_urls)
            if text.strip():
                text = text.strip() + "\n" + link_text
            else:
                text = link_text

        sent = False
        if text.strip():
            if flru_type == DT_OFFER and project_id and len(text) > 1000:
                log.warning(f"Обрезаем сообщение отклика с {len(text)} до 1000 символов (ограничение fl.ru)")
                text = text[:997] + "..."
            await self._send_text_to_flru(flru_type, flru_dialog_id, project_id,
                                           other_user_id, text)
            sent = True

        if sent:
            b24_chat_id = dialog["b24_chat_id"]
            dialog_id = f"chat{b24_chat_id}"
            await self.b24.mark_message_read(dialog_id, b24_msg_id)
            log.debug(f"    доставлено на fl.ru: {dialog_id} msg #{b24_msg_id}")

    async def _send_text_to_flru(self, flru_type: str, flru_dialog_id: str,
                                   project_id: str, other_user_id: int, text: str):
        if flru_type == DT_OFFER and project_id:
            if len(text) > 1000:
                log.warning(f"Обрезаем сообщение отклика с {len(text)} до 1000 символов (ограничение fl.ru)")
                text = text[:997] + "..."
            await self.flru.send_offer_message(
                int(project_id), int(flru_dialog_id), text
            )
        elif flru_type == DT_ORDER:
            await self.flru.send_order_message(int(flru_dialog_id), text)
        elif flru_type == DT_TSERVICE:
            await self.flru.send_tservice_message(int(flru_dialog_id), text)
        elif flru_type == DT_SYSTEM and other_user_id:
            await self.flru.send_dialogue_message(int(other_user_id), text)


async def main():
    if not os.path.exists(CONFIG_PATH):
        log.error(f"config.json не найден: {CONFIG_PATH}")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    missing_cookies = not config["flru"]["cookies"].get("PHPSESSID")
    if missing_cookies:
        log.warning("Куки fl.ru не настроены — направление fl.ru → B24 отключено")
        log.warning("Заполните flru.cookies в config.json из инструментов разработчика браузера")

    bridge = Bridge(config)
    try:
        await bridge.start()
    except KeyboardInterrupt:
        log.info("Завершение работы...")
    finally:
        await bridge.stop()


if __name__ == "__main__":
    asyncio.run(main())
