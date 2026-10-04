import asyncio
import base64
import json
import logging
import os
import secrets
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TOKEN_URL = "https://accounts.spotify.com/api/token"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
CURRENTLY_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"
RECENTLY_PLAYED_URL = "https://api.spotify.com/v1/me/player/recently-played"
SCOPES = "user-read-currently-playing user-read-recently-played"
DEFAULT_COVER = (
    "https://lastfm.freetls.fastly.net/i/u/300x300/"
    "2a96cbd8b46e442fc41c2b86b821562f.png"
)
TOKENS_FILE = BASE_DIR / "spotify_tokens.json"


def _env(name, default=None):
    value = os.getenv(name, default)
    if value is None:
        return default
    value = str(value).strip().strip('"').strip("'")
    return value or default


def _parse_iso_timestamp(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def _largest_image(images):
    if not images:
        return DEFAULT_COVER
    best = max(images, key=lambda img: img.get("height") or 0)
    url = best.get("url")
    return url if url else DEFAULT_COVER


def _track_from_item(item, now_playing, timestamp=None):
    if not item:
        return None
    artists = item.get("artists") or []
    artist = ", ".join(a.get("name") for a in artists if a.get("name")) or "Unknown"
    album = item.get("album") or {}
    if item.get("type") == "episode":
        show = item.get("show") or {}
        artist = show.get("name") or artist
        album = show
    return {
        "artist": artist,
        "title": item.get("name") or "Unknown",
        "cover": _largest_image(album.get("images")),
        "url": (item.get("external_urls") or {}).get("spotify"),
        "now_playing": now_playing,
        "timestamp": timestamp,
    }


class SpotifyClient:
    def __init__(self):
        self.client_id = _env("SPOTIFY_CLIENT_ID")
        self.client_secret = _env("SPOTIFY_CLIENT_SECRET")
        self.redirect_uri = _env(
            "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
        )
        self._refresh_token = _env("SPOTIFY_REFRESH_TOKEN")
        self._access_token = None
        self._access_expires_at = 0
        self._lock = asyncio.Lock()
        self._load_tokens()

    def has_refresh_token(self):
        return bool(self._refresh_token)

    def _load_tokens(self):
        if self._refresh_token:
            return
        try:
            with open(TOKENS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._refresh_token = (data.get("refresh_token") or "").strip() or None
            self._access_token = data.get("access_token")
            self._access_expires_at = data.get("expires_at") or 0
        except Exception:
            return

    def _save_tokens(self):
        try:
            with open(TOKENS_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "refresh_token": self._refresh_token,
                        "access_token": self._access_token,
                        "expires_at": self._access_expires_at,
                    },
                    f,
                )
        except Exception as e:
            logger.warning(f"Не удалось сохранить Spotify-токены: {e}")

    def _basic_auth_header(self):
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}

    async def _refresh_access_token(self, session: aiohttp.ClientSession):
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Задайте SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET")
        if not self._refresh_token:
            raise RuntimeError(
                "Нет Spotify refresh token. На сервере выполните: python main.py --auth"
            )
        async with session.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
            headers={
                **self._basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ) as resp:
            payload = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"Не удалось обновить Spotify token: {payload}")
        self._access_token = payload["access_token"]
        if payload.get("refresh_token"):
            self._refresh_token = payload["refresh_token"]
        self._access_expires_at = time.time() + int(payload.get("expires_in", 3600)) - 60
        self._save_tokens()

    async def _ensure_access_token(self, session: aiohttp.ClientSession):
        async with self._lock:
            now = time.time()
            if not self._access_token or now >= self._access_expires_at:
                await self._refresh_access_token(session)

    async def _get_json(self, session, url, params=None):
        await self._ensure_access_token(session)
        headers = {"Authorization": f"Bearer {self._access_token}"}
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status == 401:
                await self._refresh_access_token(session)
                headers = {"Authorization": f"Bearer {self._access_token}"}
                async with session.get(url, headers=headers, params=params) as retry:
                    if retry.status == 204:
                        return None, 204
                    return await retry.json(content_type=None), retry.status
            if resp.status == 204:
                return None, 204
            return await resp.json(content_type=None), resp.status

    async def get_current_track(self):
        try:
            async with aiohttp.ClientSession() as session:
                data, status = await self._get_json(session, CURRENTLY_PLAYING_URL)
                if status == 200 and data and data.get("item"):
                    is_playing = bool(data.get("is_playing"))
                    track = _track_from_item(
                        data["item"],
                        now_playing=is_playing,
                        timestamp=None,
                    )
                    if track and not is_playing:
                        track["paused"] = True
                    return track

                recent, recent_status = await self._get_json(
                    session, RECENTLY_PLAYED_URL, params={"limit": 1}
                )
                if recent_status != 200 or not recent or not recent.get("items"):
                    logger.warning("Не получены данные о треках из Spotify")
                    return None
                last = recent["items"][0]
                return _track_from_item(
                    last.get("track"),
                    now_playing=False,
                    timestamp=_parse_iso_timestamp(last.get("played_at")),
                )
        except Exception as e:
            logger.error(f"Ошибка при получении трека из Spotify: {e}", exc_info=True)
            return None

    def _parse_auth_input(self, raw, expected_state=None):
        raw = (raw or "").strip().strip('"').strip("'")
        if not raw:
            return None
        if raw.startswith("http"):
            query = parse_qs(urlparse(raw).query)
            error = (query.get("error") or [None])[0]
            if error:
                raise RuntimeError(f"Spotify отклонил авторизацию: {error}")
            returned_state = (query.get("state") or [None])[0]
            if expected_state and returned_state and returned_state != expected_state:
                raise RuntimeError("Неверный state в ответе Spotify")
            return (query.get("code") or [None])[0]
        return raw

    def _exchange_code(self, code):
        body = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        ).encode()
        req = urllib.request.Request(
            TOKEN_URL,
            data=body,
            headers={
                **self._basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode())
        if not payload.get("refresh_token"):
            raise RuntimeError(f"Spotify не вернул refresh_token: {payload}")
        self._access_token = payload["access_token"]
        self._refresh_token = payload["refresh_token"]
        self._access_expires_at = time.time() + int(payload.get("expires_in", 3600)) - 60
        self._save_tokens()

    def _finish_auth(self):
        print(f"Токены сохранены на сервере: {TOKENS_FILE}")
        print("При желании добавьте в .env:")
        print(f"SPOTIFY_REFRESH_TOKEN={self._refresh_token}")
        print("Дальше: python main.py")

    def authorize(self, code=None):
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Задайте SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET в .env")

        if code:
            parsed = self._parse_auth_input(code)
            if not parsed:
                raise RuntimeError("Не найден code")
            self._exchange_code(parsed)
            self._finish_auth()
            return

        state = secrets.token_urlsafe(16)
        auth_url = AUTHORIZE_URL + "?" + urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "scope": SCOPES,
                "state": state,
                "show_dialog": "true",
            }
        )
        print("Бот ставится на сервер. ПК не нужен.")
        print()
        print("1. В Spotify Dashboard Redirect URI должен быть:")
        print(f"   {self.redirect_uri}")
        print()
        print("2. По SSH на сервере вы сейчас в --auth. Откройте эту ссылку")
        print("   в браузере телефона (или любого устройства) и нажмите Agree:")
        print()
        print(auth_url)
        print()
        print("3. Телефон откроет адрес вида")
        print("   http://127.0.0.1:8888/callback?code=...  — страница не загрузится, это ок.")
        print("   Скопируйте адрес целиком и либо вставьте ниже, либо выполните:")
        print("   python main.py --auth --code 'ВСТАВЛЕННЫЙ_АДРЕС'")
        print()

        if not sys.stdin.isatty():
            print("Терминал неинтерактивный. Скопируйте ссылку выше, получите code и запустите:")
            print("python main.py --auth --code 'code_или_url'")
            return

        raw = input("Вставьте URL или code сюда: ").strip()
        parsed = self._parse_auth_input(raw, expected_state=state)
        if not parsed:
            raise RuntimeError("Код авторизации не введён")
        self._exchange_code(parsed)
        self._finish_auth()
