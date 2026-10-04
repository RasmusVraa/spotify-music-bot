import asyncio
import logging
import os
import sys
from datetime import datetime
from aiogram import Bot, Dispatcher
from aiogram.types import InputMediaPhoto
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
import json
from pathlib import Path

from spotify_client import SpotifyClient

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
MESSAGE_IDS_FILE = "music_message_ids.json"
UPDATE_INTERVAL_MINUTES = 10

spotify = SpotifyClient()


def save_message_ids(ids):
    try:
        with open(MESSAGE_IDS_FILE, "w", encoding="utf-8") as f:
            json.dump(ids, f)
    except Exception as e:
        logger.warning(f"Не удалось сохранить message_ids: {e}")


def load_message_ids():
    try:
        with open(MESSAGE_IDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def escape_markdown(text: str) -> str:
    import re

    return re.sub(r"([_!*\[\]()~`>#+\-=|{}\.!])", r"\\\1", text)


def format_time(timestamp):
    if timestamp is None:
        return "только что"
    delta = datetime.now() - datetime.fromtimestamp(timestamp)
    mins = delta.total_seconds() / 60
    hours = mins / 60
    days = hours / 24
    if mins < 1:
        return "только что"
    elif mins < 60:
        return f"{int(mins)} мин назад"
    elif hours < 24:
        return f"{int(hours)} ч назад"
    else:
        return f"{int(days)} д назад"


def is_not_modified_error(exc: Exception) -> bool:
    return "message is not modified" in str(exc).lower()


class TrackState:
    def __init__(self):
        self.last_message_text = None
        self.last_update_time = None
        self.last_artist = None
        self.last_title = None
        self.last_now_playing = None
        self.last_cover = None
        self.last_paused = None
        ids = load_message_ids()
        self.music_message_ids = ids if ids else []


track_state = TrackState()


async def update_telegram_message(bot: Bot):
    track = await spotify.get_current_track()
    if not track:
        return
    artist_md = escape_markdown(track["artist"])
    title_md = escape_markdown(track["title"])
    if track["now_playing"]:
        status = "🟢"
    elif track.get("paused"):
        status = "⏸️"
    else:
        status = f"⏸️"
    status_md = escape_markdown(status)
    track_link_md = (
        f"\n\n[👉 Ссылка]({track['url']})" if track.get("url") else ""
    )
    new_text_md = f"{status_md}*{artist_md}* — *{title_md}*{track_link_md}"
    new_text_plain = (
        f"{status}{track['artist']} - {track['title']}"
        f"👉 Ссылка: {track['url']}"
    )

    if (
        track_state.last_artist == track["artist"]
        and track_state.last_title == track["title"]
        and track_state.last_now_playing == track["now_playing"]
        and bool(track_state.last_paused) == bool(track.get("paused"))
    ):
        now = datetime.now()
        last_update = track_state.last_update_time
        if last_update and (now - last_update).total_seconds() < UPDATE_INTERVAL_MINUTES * 60:
            logger.info("Трек не изменился и прошло меньше 10 минут — не обновляем сообщение.")
            return
        logger.info("Трек не изменился, но прошло 10 минут — обновляем сообщение.")

    try:
        used_text = new_text_md
        used_parse_mode = ParseMode.MARKDOWN_V2
        try:
            msg_id = await upsert_track_message(
                bot, track["cover"], used_text, used_parse_mode
            )
        except TelegramBadRequest as e:
            logger.error(f"Ошибка MarkdownV2, пробуем обычный текст: {e}")
            used_text = new_text_plain
            used_parse_mode = None
            msg_id = await upsert_track_message(
                bot, track["cover"], used_text, used_parse_mode
            )

        track_state.music_message_ids = [msg_id]
        track_state.last_message_text = used_text
        track_state.last_update_time = datetime.now()
        track_state.last_artist = track["artist"]
        track_state.last_title = track["title"]
        track_state.last_now_playing = track["now_playing"]
        track_state.last_paused = bool(track.get("paused"))
        track_state.last_cover = track["cover"]
        save_message_ids(track_state.music_message_ids)
        logger.info(f"Обновлено сообщение: {track['artist']} - {track['title']}")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при обновлении сообщения: {e}")


async def upsert_track_message(bot: Bot, cover: str, caption: str, parse_mode):
    media = InputMediaPhoto(media=cover, caption=caption, parse_mode=parse_mode)
    existing_ids = list(track_state.music_message_ids or [])

    if existing_ids:
        msg_id = existing_ids[0]
        try:
            await bot.edit_message_media(
                chat_id=CHAT_ID,
                message_id=msg_id,
                media=media,
            )
            return msg_id
        except TelegramBadRequest as e:
            if is_not_modified_error(e):
                return msg_id
            try:
                await bot.edit_message_caption(
                    chat_id=CHAT_ID,
                    message_id=msg_id,
                    caption=caption,
                    parse_mode=parse_mode,
                )
                return msg_id
            except TelegramBadRequest as e2:
                if is_not_modified_error(e2):
                    return msg_id
                logger.warning(f"Не удалось изменить сообщение {msg_id}: {e2}")
        except Exception as e:
            logger.warning(f"Не удалось изменить сообщение {msg_id}: {e}")

    msg = await bot.send_photo(
        chat_id=CHAT_ID,
        photo=cover,
        caption=caption,
        parse_mode=parse_mode,
        disable_notification=True,
    )
    for old_id in existing_ids:
        if old_id == msg.message_id:
            continue
        try:
            await bot.delete_message(chat_id=CHAT_ID, message_id=old_id)
        except Exception as e:
            logger.warning(f"Не удалось удалить старое сообщение {old_id}: {e}")
    return msg.message_id


async def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher()
    logger.info("aiogram-бот запущен. Ожидание треков...")
    print("aiogram-бот запущен")
    try:
        while True:
            try:
                await update_telegram_message(bot)
            except Exception as e:
                logger.error(f"Ошибка в цикле: {e}")
            await asyncio.sleep(20)
    except asyncio.CancelledError:
        logger.info("Задача отменена")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    if "--auth" in sys.argv:
        code = None
        if "--code" in sys.argv:
            idx = sys.argv.index("--code")
            if idx + 1 >= len(sys.argv):
                print("Укажите код: python main.py --auth --code 'URL_или_code'")
                sys.exit(1)
            code = sys.argv[idx + 1]
        try:
            spotify.authorize(code=code)
        except Exception as e:
            logger.error(f"Ошибка авторизации Spotify: {e}")
            print(f"Ошибка авторизации Spotify: {e}")
            sys.exit(1)
        sys.exit(0)
    if not spotify.has_refresh_token():
        print("Нет Spotify refresh token.")
        print("На этом сервере один раз выполните: python main.py --auth")
        sys.exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную")
        print("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        print("Бот остановлен")
