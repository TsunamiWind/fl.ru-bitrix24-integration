import aiohttp
import json
import re

FLRU_BASE = "https://www.fl.ru"


def clean_operator_text(text: str) -> str:
    text = re.sub(r"\[b\][^\[\]]*:\[/b\]\s*", "", text)
    text = text.replace("[br]", "\n")
    text = re.sub(r"\[/?\w+[^\]]*\]", "", text)
    return text.strip()


class FlRuClient:
    def __init__(self, cookies: dict, csrf_token: str, user_agent: str):
        self.cookies = cookies
        self.csrf_token = csrf_token
        self.user_agent = user_agent
        self._session: aiohttp.ClientSession = None
        self._session_cache: dict = {}

    @property
    def _headers(self):
        return {
            "User-Agent": self.user_agent,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "X-CSRF-TOKEN": self.csrf_token,
        }

    async def _ensure_session(self):
        if self._session is None:
            import aiohttp.resolver
            resolver = aiohttp.resolver.ThreadedResolver()
            connector = aiohttp.TCPConnector(resolver=resolver)
            self._session = aiohttp.ClientSession(cookies=self.cookies, connector=connector)

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def _get(self, path: str):
        await self._ensure_session()
        url = f"{FLRU_BASE}{path}" if path.startswith("/") else path
        async with self._session.get(url, headers=self._headers) as resp:
            if resp.status == 403:
                raise Exception("PHPSESSID истёк — обновите куки в config.json")
            return await resp.json()

    async def _post_form(self, path: str, data: dict):
        await self._ensure_session()
        url = f"{FLRU_BASE}{path}" if path.startswith("/") else path
        headers = {
            **self._headers,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with self._session.post(url, data=data, headers=headers) as resp:
            if resp.status == 403:
                raise Exception("PHPSESSID истёк — обновите куки в config.json")
            return await resp.json()

    async def get_unread_counts(self) -> dict:
        return await self._get("/user/cnt-new/chat/")

    # ── Отклики ──

    async def list_offers(self, limit: int = 50) -> list:
        data = await self._get(f"/projects/offers/?limit={limit}")
        return data.get("items", [])

    async def get_offer_messages(self, project_id: int, offer_id: int, limit: int = 40) -> dict:
        return await self._get(
            f"/projects/{project_id}/offers/{offer_id}/messages/?limit={limit}"
        )

    async def send_offer_message(
        self, project_id: int, offer_id: int, text: str, reply_to: int = 0
    ) -> dict:
        session_id = await self._create_file_session(offer_id=offer_id)
        return await self.send_offer_message_with_session(
            session_id, project_id, offer_id, text, reply_to
        )

    async def send_offer_message_with_session(
        self, session_id: str, project_id: int, offer_id: int,
        text: str, reply_to: int = 0
    ) -> dict:
        data = {
            "text": text,
            "format": "raw",
            "file_session_id": session_id,
        }
        if reply_to:
            data["reply_to"] = str(reply_to)
        return await self._post_form(
            f"/projects/{project_id}/offers/{offer_id}/messages/", data
        )

    async def mark_offer_read(self, project_id: int, offer_id: int):
        return await self._post_form(
            f"/projects/{project_id}/offers/{offer_id}/read/", {}
        )

    # ── Заказы / Безопасные сделки ──

    async def list_orders(self, limit: int = 5) -> list:
        data = await self._get(f"/api/orders/?limit={limit}")
        return data.get("items", [])

    async def get_order_messages(self, order_id: int, limit: int = 40) -> dict:
        return await self._get(f"/api/orders/messages/?order_id={order_id}&limit={limit}")

    async def send_order_message(self, order_id: int, text: str) -> dict:
        session_id = await self._create_file_session()
        return await self.send_order_message_with_session(session_id, order_id, text)

    async def send_order_message_with_session(
        self, session_id: str, order_id: int, text: str
    ) -> dict:
        return await self._post_form(
            "/api/orders/messages/",
            {"text": text, "format": "raw", "order_id": str(order_id), "file_session_id": session_id},
        )

    async def mark_order_read(self, order_id: int):
        return await self._post_form(f"/api/orders/{order_id}/messages/read/", {})

    # ── Типовые услуги ──

    async def list_tservice_dialogs(self, limit: int = 10) -> list:
        data = await self._get(f"/api/uslugi-freelancera/dialogs/?limit={limit}")
        return data.get("items", [])

    async def get_tservice_messages(self, dialog_id: int, limit: int = 40) -> dict:
        return await self._get(
            f"/api/uslugi-freelancera/dialog/{dialog_id}/messages/?dialog_id={dialog_id}&limit={limit}"
        )

    async def send_tservice_message(self, dialog_id: int, text: str) -> dict:
        session_id = await self._create_file_session()
        return await self.send_tservice_message_with_session(session_id, dialog_id, text)

    async def send_tservice_message_with_session(
        self, session_id: str, dialog_id: int, text: str
    ) -> dict:
        return await self._post_form(
            f"/api/uslugi-freelancera/dialog/{dialog_id}/messages/",
            {"text": text, "format": "raw", "file_session_id": session_id},
        )

    async def mark_tservice_read(self, dialog_id: int):
        return await self._post_form(
            f"/api/uslugi-freelancera/dialog/{dialog_id}/messages/read/", {}
        )

    # ── Личные сообщения / FL Team ──

    async def list_dialogues(self) -> list:
        data = await self._get("/dialogues/")
        return data.get("items", []) if isinstance(data, dict) else data

    async def get_dialogue_messages(self, dialog_id: int, limit: int = 40) -> dict:
        return await self._get(f"/dialogues/{dialog_id}/?limit={limit}")

    async def send_dialogue_message(self, to_user_id: int, text: str) -> dict:
        session_id = await self._create_file_session()
        return await self.send_dialogue_message_with_session(session_id, to_user_id, text)

    async def send_dialogue_message_with_session(
        self, session_id: str, to_user_id: int, text: str
    ) -> dict:
        return await self._post_form(
            "/dialogues/",
            {"to_id": str(to_user_id), "msg_text": text, "file_session_id": session_id},
        )

    # ── Загрузка файлов ──

    async def _create_file_session(self, offer_id: int = None) -> str:
        if offer_id is not None:
            cache_key = f"offer_{offer_id}"
        else:
            cache_key = "text_only"
        if hasattr(self, "_session_cache") and self._session_cache.get(cache_key):
            return self._session_cache[cache_key]

        if offer_id is not None:
            resp = await self._post_form(
                "/storage/upload/session/",
                {"type": "offer_message", "options[offer_id]": str(offer_id)},
            )
        else:
            resp = await self._post_form("/storage/upload/session/", {"type": "logo"})

        sid = resp["session_id"]
        if not hasattr(self, "_session_cache"):
            self._session_cache = {}
        self._session_cache[cache_key] = sid
        return sid

    async def download_file(self, url: str) -> bytes:
        await self._ensure_session()
        async with self._session.get(url, allow_redirects=True,
                                      headers=self._headers) as resp:
            if resp.status != 200:
                raise Exception(f"Не удалось скачать файл с fl.ru: HTTP {resp.status}")
            return await resp.read()

    async def upload_file(self, session_id: str, file_data: bytes,
                           file_name: str, mime_type: str = "application/octet-stream") -> dict:
        await self._ensure_session()
        data = aiohttp.FormData()
        data.add_field("files[]", file_data, filename=file_name, content_type=mime_type)
        headers = {
            **self._headers,
            "File-Session": session_id,
        }
        url = f"{FLRU_BASE}/storage/upload/"
        async with self._session.post(url, data=data, headers=headers) as resp:
            return await resp.json()

    async def upload_file_legacy(self, file_data: bytes, file_name: str,
                                  mime_type: str = "application/octet-stream") -> str:
        await self._ensure_session()
        data = aiohttp.FormData()
        data.add_field("attachedfiles_file", file_data,
                       filename=file_name, content_type=mime_type)
        data.add_field("attachedfiles_action", "add")
        data.add_field("attachedfiles_type", "project")
        url = f"{FLRU_BASE}/attachedfiles2.php"
        async with self._session.post(url, data=data) as resp:
            html = await resp.text()
        match = re.search(r"message\.path = '(https://st\.fl\.ru/[^']+)'", html)
        if not match:
            raise Exception(f"Не удалось загрузить файл на fl.ru через legacy endpoint")
        return match.group(1)
