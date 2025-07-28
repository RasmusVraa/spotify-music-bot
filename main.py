import asyncio
import logging
import os
from datetime import datetime, timedelta
import pylast
from aiogram import Bot, Dispatcher
from aiogram.types import FSInputFile
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
import json

load_dotenv()

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
LASTFM_USERNAME = os.getenv("LASTFM_USERNAME")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
MESSAGE_IDS_FILE = "music_message_ids.json"
UPDATE_INTERVAL_MINUTES = 10

network = pylast.LastFMNetwork(api_key=LASTFM_API_KEY)
user = network.get_user(LASTFM_USERNAME)



# --- Работа с message_id ---
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
    # Экранирует спецсимволы для MarkdownV2
    import re
    return re.sub(r'([_!*\[\]()~`>#+\-=|{}\.!])', r'\\\1', text)

def get_track_url(artist, title):
    try:
        track = network.get_track(artist, title)
        return track.get_url()
    except Exception as e:
        logger.error(f"Ошибка при получении URL трека: {e}")
        return None

def get_cover_image_safe(track):
    try:
        if hasattr(track, 'image') and track.image:
            for size in ['extralarge', 'large', 'medium', 'small']:
                for img in track.image:
                    if img.get('size') == size and img.get('#text', '').startswith('http'):
                        return img['#text']
        cover = track.get_cover_image(size=pylast.SIZE_EXTRA_LARGE)
        if cover and cover.startswith('http'):
            return cover
        artist_cover = track.artist.get_cover_image(size=pylast.SIZE_EXTRA_LARGE)
        if artist_cover and artist_cover.startswith('http'):
            return artist_cover
    except Exception as e:
        logger.debug(f"Не удалось получить обложку: {e}")
    return "https://lastfm.freetls.fastly.net/i/u/300x300/2a96cbd8b46e442fc41c2b86b821562f.png"

def get_current_track():
    try:
        track = user.get_now_playing()
        if track:
            artist = track.artist.name
            title = track.title
            cover = get_cover_image_safe(track)
            url = get_track_url(artist, title)
            return {
                'artist': artist,
                'title': title,
                'cover': cover,
                'url': url,
                'now_playing': True,
                'timestamp': None
            }
        recent_tracks = user.get_recent_tracks(limit=1)
        if not recent_tracks:
            logger.warning("Не получены данные о треках")
            return None
        last_track = recent_tracks[0]
        artist = last_track.track.artist.name
        title = last_track.track.title
        cover = get_cover_image_safe(last_track.track)
        timestamp = int(last_track.timestamp) if hasattr(last_track, 'timestamp') else None
        url = get_track_url(artist, title)
        return {
            'artist': artist,
            'title': title,
            'cover': cover,
            'url': url,
            'now_playing': False,
            'timestamp': timestamp
        }
    except Exception as e:
        logger.error(f"Ошибка при получении трека: {e}", exc_info=True)
        return None

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

class TrackState:
    def __init__(self):
        self.last_message_text = None
        self.last_update_time = None
        self.last_artist = None
        self.last_title = None
        self.last_now_playing = None
        ids = load_message_ids()
        self.music_message_ids = ids if ids else []

track_state = TrackState()

async def update_telegram_message(bot: Bot):
    track = get_current_track()
    if not track:
        return
    # Формируем текст с MarkdownV2
    artist_md = escape_markdown(track['artist'])
    title_md = escape_markdown(track['title'])
    status = "🟢 Сейчас слушает 🟢" if track['now_playing'] else f"⏸️ Трек играл ({format_time(track['timestamp'])})"
    status_md = escape_markdown(status)
    track_link_md = f"\n\n[👉 Ссылка на трек]({track['url']})" if track.get('url') else ""
    new_text_md = f"*{artist_md}* — *{title_md}*\n\n{status_md}{track_link_md}"
    new_text_plain = f"{track['artist']} — {track['title']}\n\n{status}\n\n👉 Ссылка на трек: {track['url']}"

    # Проверяем, изменился ли трек или статус
    if (
        track_state.last_artist == track['artist'] and
        track_state.last_title == track['title'] and
        track_state.last_now_playing == track['now_playing']
    ):
        now = datetime.now()
        last_update = track_state.last_update_time
        if last_update and (now - last_update).total_seconds() < UPDATE_INTERVAL_MINUTES * 60:
            logger.info("Трек не изменился и прошло меньше 10 минут — не обновляем сообщение.")
            return
        logger.info("Трек не изменился, но прошло 10 минут — обновляем сообщение.")

    # Удаляем все старые музыкальные сообщения
    for msg_id in track_state.music_message_ids:
        try:
            await bot.delete_message(chat_id=CHAT_ID, message_id=msg_id)
            logger.info(f"Удалено старое музыкальное сообщение с ID: {msg_id}")
        except Exception as e:
            logger.warning(f"Не удалось удалить сообщение {msg_id}: {e}")
    track_state.music_message_ids = []
    # Пробуем отправить с MarkdownV2
    try:
        msg = await bot.send_photo(
            chat_id=CHAT_ID,
            photo=track['cover'],
            caption=new_text_md,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_notification=True
        )
        track_state.music_message_ids = [msg.message_id]
        track_state.last_message_text = new_text_md
        track_state.last_update_time = datetime.now()
        track_state.last_artist = track['artist']
        track_state.last_title = track['title']
        track_state.last_now_playing = track['now_playing']
        save_message_ids(track_state.music_message_ids)
        logger.info(f"Создано новое сообщение: {track['artist']} - {track['title']}")
    except TelegramBadRequest as e:
        logger.error(f"Ошибка при создании сообщения с MarkdownV2: {e}")
        # Fallback: обычный текст
        try:
            msg = await bot.send_photo(
                chat_id=CHAT_ID,
                photo=track['cover'],
                caption=new_text_plain,
                parse_mode=None,
                disable_notification=True
            )
            track_state.music_message_ids = [msg.message_id]
            track_state.last_message_text = new_text_plain
            track_state.last_update_time = datetime.now()
            track_state.last_artist = track['artist']
            track_state.last_title = track['title']
            track_state.last_now_playing = track['now_playing']
            save_message_ids(track_state.music_message_ids)
            logger.info(f"Создано новое сообщение (fallback): {track['artist']} - {track['title']}")
        except Exception as e2:
            logger.error(f"Ошибка при создании fallback-сообщения: {e2}")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при отправке сообщения: {e}")

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
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную")
        print("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        print("Бот остановлен") 