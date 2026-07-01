import asyncio
import json
import logging
import logging.handlers
import os
import sys
import traceback

from bridge import Bridge

LOG_PATH = os.path.join(os.path.dirname(__file__), "log.txt")


def setup_logging():
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)


def handle_exception(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logging.getLogger("main").critical(
        "Необработанное исключение:\n%s",
        "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
    )


sys.excepthook = handle_exception

log = logging.getLogger("main")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def check_config(config: dict) -> list[str]:
    issues = []

    b24 = config.get("b24", {})
    if not b24.get("domain"):
        issues.append("b24.domain не указан")
    if not b24.get("webhook_secret"):
        issues.append("b24.webhook_secret не указан")
    if not b24.get("bot_id"):
        issues.append("b24.bot_id не указан")
    if not b24.get("bot_token"):
        issues.append("b24.bot_token не указан")

    flru = config.get("flru", {})
    cookies = flru.get("cookies", {})
    if not cookies.get("PHPSESSID"):
        issues.append("flru.cookies.PHPSESSID не указан (направление fl.ru отключено)")
    if not cookies.get("id"):
        issues.append("flru.cookies.id не указан")
    if not cookies.get("pwd"):
        issues.append("flru.cookies.pwd не указан")
    if not cookies.get("XSRF-TOKEN"):
        issues.append("flru.cookies.XSRF-TOKEN не указан")
    if not cookies.get("name"):
        issues.append("flru.cookies.name не указан")
    if not flru.get("csrf_token"):
        issues.append("flru.csrf_token не указан")

    return issues


async def main():
    setup_logging()

    if not os.path.exists(CONFIG_PATH):
        log.error("config.json не найден. Скопируйте config.example.json → config.json и заполните учётные данные.")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    issues = check_config(config)
    if issues:
        log.warning("Проблемы с конфигурацией:")
        for issue in issues:
            log.warning(f"  - {issue}")
        log.warning("")

    log.info("Запуск моста FL.ru ↔ Bitrix24")
    log.info(f"  Домен B24: {config['b24']['domain']}")
    log.info(f"  ID бота:   {config['b24']['bot_id']}")
    log.info(f"  Интервал опроса fl.ru: {config['flru'].get('poll_interval_seconds', 30)}с")
    log.info(f"  Интервал опроса B24:   5с")
    log.info(f"  Лог-файл: {LOG_PATH}")
    log.info("")

    bridge = Bridge(config)
    try:
        await bridge.start()
    except KeyboardInterrupt:
        log.info("Завершение работы...")
    except Exception:
        log.critical("Критическая ошибка моста:\n%s", traceback.format_exc())
    finally:
        await bridge.stop()


if __name__ == "__main__":
    asyncio.run(main())
