"""
Регистрация чат-бота FL.ru Bridge в Bitrix24.

Использование:
    python setup_bot.py

Читает config.json для получения учётных данных вебхука Bitrix24,
регистрирует скрытого персонального чат-бота и выводит bot_id + bot_token
для добавления в config.json.
"""

import json
import os
import sys
from b24pysdk import BitrixWebhook, Client

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def main():
    if not os.path.exists(CONFIG_PATH):
        print("ОШИБКА: config.json не найден. Скопируйте config.example.json → config.json и заполните учётные данные Bitrix24.")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    b24 = config.get("b24", {})
    domain = b24.get("domain", "")
    user_id = b24.get("webhook_user_id", 1)
    secret = b24.get("webhook_secret", "")

    if not domain or not secret:
        print("ОШИБКА: поля b24.domain и b24.webhook_secret должны быть заполнены в config.json")
        sys.exit(1)

    token = BitrixWebhook(domain=domain, webhook_token=f"{user_id}/{secret}")
    client = Client(token)

    bot_code = "fl_ru_bridge"
    bot_token = "flru_bridge_token_" + os.urandom(4).hex()

    result = client.call("imbot.v2.Bot.register", {
        "fields": {
            "code": bot_code,
            "botToken": bot_token,
            "type": "personal",
            "isHidden": True,
            "properties": {
                "name": "FL.ru Bridge",
                "workPosition": "Пересылка сообщений с fl.ru",
            },
        }
    }).result

    if "error" in result:
        print(f"ОШИБКА: {result.get('error_description', result['error'])}")
        sys.exit(1)

    bot_id = result["result"]["bot"]["id"]
    print("Бот успешно зарегистрирован!")
    print(f"  Bot ID:   {bot_id}")
    print(f"  Код:      {bot_code}")
    print(f"  Токен:    {bot_token}")
    print()
    print("Добавьте это в config.json → раздел b24:")
    print(f'  "bot_id": {bot_id},')
    print(f'  "bot_token": "{bot_token}",')


if __name__ == "__main__":
    main()
