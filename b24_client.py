import asyncio
import base64
import aiohttp

from b24pysdk import BitrixWebhook, Client


class B24Client:
    def __init__(self, domain: str, webhook_user_id: int, webhook_secret: str,
                 bot_id: int, bot_token: str, your_user_id: int):
        token = BitrixWebhook(
            domain=domain,
            webhook_token=f"{webhook_user_id}/{webhook_secret}",
        )
        self._client = Client(token)
        self.bot_id = bot_id
        self.bot_token = bot_token
        self.your_user_id = your_user_id
        self._session: aiohttp.ClientSession = None

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    def _call(self, method: str, params: dict) -> dict:
        req = self._client.call(method, params)
        result = req.result
        if isinstance(result, dict) and "error" in result:
            raise Exception(
                f"Ошибка API Bitrix24 [{method}]: "
                f"{result.get('error_description', result['error'])}"
            )
        return result

    async def _call_async(self, method: str, params: dict) -> dict:
        return await asyncio.to_thread(self._call, method, params)

    # ── Чаты ──

    async def create_chat(self, title: str, entity_type: str, entity_id: str,
                          description: str = "", message: str = None) -> int:
        body = {
            "USERS": [self.your_user_id, self.bot_id],
            "TYPE": "CHAT",
            "TITLE": title,
            "ENTITY_TYPE": entity_type,
            "ENTITY_ID": entity_id,
        }
        if description:
            body["DESCRIPTION"] = description
        if message:
            body["MESSAGE"] = message
        return await self._call_async("im.chat.add", body)

    async def get_chat_by_entity(self, entity_type: str, entity_id: str) -> int | None:
        result = await self._call_async("im.chat.get", {
            "ENTITY_TYPE": entity_type,
            "ENTITY_ID": entity_id,
        })
        return result if isinstance(result, int) else result[0] if result else None

    async def get_dialog(self, dialog_id: str) -> dict:
        return await self._call_async("im.dialog.get", {"DIALOG_ID": dialog_id})

    async def get_messages(self, dialog_id: str, limit: int = 20,
                           first_id: int = None, last_id: int = None) -> dict:
        body = {"DIALOG_ID": dialog_id, "LIMIT": limit}
        if first_id is not None:
            body["FIRST_ID"] = first_id
        if last_id is not None:
            body["LAST_ID"] = last_id
        return await self._call_async("im.dialog.messages.get", body)

    async def mark_dialog_read(self, dialog_id: str, message_id: int = None) -> dict:
        body = {"DIALOG_ID": dialog_id}
        if message_id is not None:
            body["MESSAGE_ID"] = message_id
        return await self._call_async("im.dialog.read", body)

    async def mark_message_read(self, dialog_id: str, message_id: int) -> dict:
        return await self._call_async("im.message.like", {
            "DIALOG_ID": dialog_id,
            "MESSAGE_ID": message_id,
            "ACTION": "plus",
        })

    # ── Сообщения бота ──

    async def bot_send_message(self, dialog_id: str, text: str,
                               reply_id: int = None) -> int:
        body = {
            "botId": self.bot_id,
            "botToken": self.bot_token,
            "dialogId": dialog_id,
            "fields": {"message": text},
        }
        if reply_id:
            body["fields"]["replyId"] = reply_id
        result = await self._call_async("imbot.v2.Chat.Message.send", body)
        return result["id"] if isinstance(result, dict) else result

    async def bot_send_offer_message(self, dialog_id: str, author_name: str,
                                      text: str) -> int:
        formatted = f"[B]{author_name}:[/B][BR]{text}"
        return await self.bot_send_message(dialog_id, formatted)

    # ── Файлы ──

    async def bot_upload_file(self, dialog_id: str, file_name: str,
                               file_content: bytes, message: str = None) -> dict:
        content_b64 = base64.b64encode(file_content).decode("ascii")
        body = {
            "botId": self.bot_id,
            "botToken": self.bot_token,
            "dialogId": dialog_id,
            "fields": {
                "name": file_name,
                "content": content_b64,
            },
        }
        if message:
            body["fields"]["message"] = message
        return await self._call_async("imbot.v2.File.upload", body)

    async def download_file_v2(self, file_id: int, chat_id: int) -> bytes:
        result = await self._call_async("im.v2.File.download", {
            "fileId": file_id,
            "chatId": chat_id,
        })
        url = result.get("downloadUrl", "") or result.get("url", "")
        if not url:
            raise Exception(f"Нет ссылки для скачивания файла {file_id}")
        return await self._download_url(url)

    async def _ensure_session(self):
        if self._session is None:
            import aiohttp.resolver
            resolver = aiohttp.resolver.ThreadedResolver()
            connector = aiohttp.TCPConnector(resolver=resolver)
            self._session = aiohttp.ClientSession(connector=connector)

    async def _download_url(self, url: str) -> bytes:
        await self._ensure_session()
        async with self._session.get(url, allow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }) as resp:
            if resp.status != 200:
                raise Exception(f"Download failed: HTTP {resp.status}")
            return await resp.read()

    # ── Управление ботом ──

    async def update_bot(self, fields: dict) -> dict:
        return await self._call_async("imbot.v2.Bot.update", {
            "botId": self.bot_id,
            "botToken": self.bot_token,
            "fields": fields,
        })
