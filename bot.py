#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 LoadUpMediaBot — Telegram-бот для скачивания медиа
 Поддержка: TikTok, Instagram Reels, YouTube Shorts, Pinterest
 Стек: Python 3.10+, aiogram 3.x, yt-dlp (+ ffmpeg)
 Целевое окружение: Render.com Free (0.1 CPU / 512 МБ RAM)
================================================================================

 ЧТО ДЕЛАЕТ БОТ:
   1. Принимает ссылку на видео и определяет платформу по домену.
   2. СНАЧАЛА запрашивает только МЕТАДАННЫЕ через yt-dlp (без скачивания!)
      и проверяет ожидаемый размер файла. Если он больше лимита Telegram
      (50 МБ) — отклоняет загрузку, не тратя ни трафик, ни RAM сервера.
   3. Скачивает видео через yt-dlp В ОТДЕЛЬНОМ ПОТОКЕ (run_in_executor),
      ПОТОКОВО на диск маленькими буферами — файл НЕ поднимается в RAM.
   4. Отправляет медиа файлом (FSInputFile стримит файл с диска чанками)
      и ГАРАНТИРОВАННО удаляет временную папку с диска в finally.

 ПОЧЕМУ ЭТО РАБОТАЕТ НА 512 МБ RAM:
   - yt-dlp пишет данные сразу на диск (outtmpl -> файл), в памяти живёт
     только небольшой буфер (buffersize ~64 КБ);
   - FSInputFile в aiogram читает файл с диска чанками по 64 КБ
     (aiofiles), поэтому исходящая отправка тоже не съедает RAM;
   - жёсткий лимит max_filesize в yt-dlp обрывает скачивание, если файл
     всё же оказался больше лимита (вторая линия обороны);
   - каждый запрос получает СВОЮ временную папку (tempfile.mkdtemp),
     которая сносится рекурсивно (shutil.rmtree) сразу после отправки.

 КАК ЗАПУСТИТЬ:
   1. Получите токен бота у @BotFather и задайте его ТОЛЬКО как переменную
      окружения BOT_TOKEN — в код токен вписывать нельзя, репозиторий
      публичный:
         export BOT_TOKEN="123456:ABC-ваш-токен"
      На Render: Dashboard → Environment → Add Environment Variable.
      Локально удобно положить токен в файл .env (см. .env.example);
      .env добавлен в .gitignore и в репозиторий не попадёт.
   2. Запуск:  python3 bot.py

 ВАЖНО ДЛЯ RENDER.COM (и любого хостинга):
   - Запускать ровно ОДИН инстанс бота. Если работают два polling-процесса
     с одним токеном, Telegram отдаёт TelegramConflictError
     («terminated by other getUpdates request»). Остановите лишний процесс.
   - В Build Command добавьте установку ffmpeg (см. requirements.txt /
     render.yaml): без него yt-dlp не склеит видео+аудио дорожки.
   - Бесплатный инстанс Render «засыпает» — используйте внешний UptimeRobot
     или переведите сервис на платный тариф для 24/7 работы.

 ПРИМЕЧАНИЯ:
   - Instagram часто требует авторизацию: раскомментируйте
     COOKIES_FROM_BROWSER = "chrome" (или "firefox") в блоке настроек.
   - Официальный Bot API не принимает файлы больше ~50 МБ.
================================================================================
"""

# ============================================================
#  АВТО-УСТАНОВКА NODE.JS ВНУТРЬ ОКРУЖЕНИЯ PYTHON
# ============================================================
# ВАЖНО: блок стоит ДО импорта yt_dlp/aiogram, чтобы node успел
# появиться в PATH ещё до того, как yt-dlp начнёт искать JS-рантайм.
#
# Зачем: с 2025 года YouTube требует исполнить JavaScript, чтобы получить
# «player response». Без JS-рантайма yt-dlp падает с ошибками
# «Failed to extract any player response» / «nsig extraction failed».
# nodeenv ставит Node.js прямо в окружение Python (sys.prefix), поэтому
# бинарник лежит рядом с интерпретатором и не зависит от системных прав.
#
# На Render этот шаг подстраховывает deno из render.yaml: если deno по
# какой-то причине не встал, node докачается автоматически при старте.
#
# ВАЖНО про Render: pip-слой кэшируется между деплоями. Если после добавления
# nodeenv в requirements.txt в логе сборки его всё ещё нет, значит Render взял
# закэшированный слой — нажмите Settings → Clear build cache & deploy.
import os
import shutil
import subprocess
import sys

# Куда ставим deno, если nodeenv недоступен (только как резервный путь).
_DENO_INSTALL_DIR = os.path.join(
    os.environ.get("DENO_INSTALL", os.path.expanduser("~/.deno"))
)

# Принудительная установка Node.js внутрь виртуального окружения при старте
try:
    node_path = os.path.join(sys.prefix, "bin", "node")
    if not os.path.exists(node_path):
        print("Installing Node.js via nodeenv...", flush=True)
        subprocess.run(
            [sys.executable, "-m", "nodeenv", "-p"],
            check=True,
            capture_output=True,   # не засоряем лог шумом nodeenv
            timeout=300,           # Free-тариф медленный, но не бесконечный
        )
        print("Node.js successfully installed!", flush=True)
    # Кладём папку с node в начало PATH, чтобы yt-dlp его гарантированно нашёл.
    node_bin_dir = os.path.join(sys.prefix, "bin")
    if os.path.isdir(node_bin_dir):
        path_parts = os.environ.get("PATH", "").split(os.pathsep)
        if node_bin_dir not in path_parts:
            os.environ["PATH"] = node_bin_dir + os.pathsep + os.environ.get("PATH", "")
except Exception as e:
    # Причина видна в логе: чаще всего "No module named nodeenv" — это значит,
    # что pip не поставил пакет (обычно из-за кэша сборки Render) и нужен
    # Clear build cache & deploy. Пробуем запасной путь — deno.
    print(f"Failed to auto-install Node.js: {e}", flush=True)

# --- Резервный путь: deno, если node так и не появился ---
# deno самодостаточен (один бинарник) и yt-dlp умеет с ним работать.
# Ставим только тогда, когда node нет — чтобы не тратить время на старте.
deno_bin = os.path.join(_DENO_INSTALL_DIR, "bin", "deno")
if not shutil.which("node") and not shutil.which("deno") and not os.path.exists(deno_bin):
    try:
        print("Installing Deno as JS runtime fallback...", flush=True)
        subprocess.run(
            [
                "sh",
                "-c",
                f'export DENO_INSTALL="{_DENO_INSTALL_DIR}" && '
                "curl -fsSL https://deno.land/install.sh | sh -s -- -y",
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
        print("Deno successfully installed!", flush=True)
    except Exception as e:
        print(f"Failed to auto-install Deno: {e}", flush=True)

# Добавляем папку deno в PATH, если бинарник появился.
_deno_bin_dir = os.path.join(_DENO_INSTALL_DIR, "bin")
if os.path.isdir(_deno_bin_dir):
    _path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if _deno_bin_dir not in _path_parts:
        os.environ["PATH"] = _deno_bin_dir + os.pathsep + os.environ.get("PATH", "")


# ============================================================
#  СТАНДАРТНАЯ БИБЛИОТЕКА PYTHON
# ============================================================
import asyncio            # асинхронность: run_in_executor, таймауты, семафоры
import functools          # functools.partial — передача аргументов в executor
import html               # экранирование пользовательских строк для HTML
import logging            # логирование работы бота
import os                 # переменные окружения, os.path
import re                 # регулярные выражения: валидация ссылок
import shutil             # рекурсивное удаление временной папки
import tempfile           # временные папки под каждое скачивание
from pathlib import Path  # удобная работа с путями
from typing import Any, Dict, Optional  # аннотации типов

# ============================================================
#  СТОРОННИЕ БИБЛИОТЕКИ
# ============================================================
import yt_dlp             # скачивание видео с TikTok/Instagram/YouTube/Pinterest

# Минимальный HTTP-сервер для Render Web Service (health-check по порту).
# На бесплатном тарифе Render сервис типа «web» обязан слушать порт,
# который Render выдаёт в переменной окружения PORT, иначе деплой
# считается «не поднявшимся» (no open ports detected).
from aiohttp import web

# Тексты сообщений бота — единая точка правки копирайта
from texts import PROGRESS_FRAMES, msg

# Загрузка больших видео в Cloudflare R2 (S3-совместимое хранилище).
# Нужно потому, что Telegram Bot API не принимает от ботов файлы > 50 МБ:
# такие видео мы отдаём пресайн-ссылкой. Ключи читаются только из окружения.
import storage

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,        # «message not modified» и прочие ошибки запроса
    TelegramEntityTooLarge,    # файл больше лимита Telegram (413)
    TelegramRetryAfter,        # флуд-контроль: нужно подождать N секунд
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    FSInputFile,               # отправка файла с локального диска (стриминг!)
    Message,
)

# ============================================================
#  НАСТРОЙКИ  ✏️✏️✏️  ВСТАВЬТЕ СВОИ ДАННЫЕ СЮДА  ✏️✏️✏️
# ============================================================

# 1) ТОКЕН БОТА — получите у @BotFather в Telegram.
#    🔒 ТОКЕН ЧИТАЕТСЯ ТОЛЬКО ИЗ ПЕРЕМЕННОЙ ОКРУЖЕНИЯ BOT_TOKEN.
#    Никогда не вписывайте токен сюда: файл лежит в публичном репозитории,
#    и любой, кто его увидит, получит полный доступ к боту.
#
#    Как задать токен:
#      • Render:  Dashboard → ваш сервис → Environment → Add Environment
#                 Variable → key = BOT_TOKEN, value = ваш токен → Save.
#      • Локально:  export BOT_TOKEN="123456:ABC..."  (или файл .env,
#                   см. .env.example — .env в .gitignore и не попадёт в git)
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# Что делать, если токен не задан: падаем сразу и с понятным текстом,
# а не в середине работы. Проверка ниже — единственное место, где это видно.
BOT_TOKEN_HINT = (
    "✋ Токен бота не задан!\n"
    "Задайте переменную окружения BOT_TOKEN — например:\n"
    '    export BOT_TOKEN="123456:ABC-ваш-токен"\n'
    "На Render: Dashboard → Environment → Add Environment Variable.\n"
    "Токен берётся у @BotFather. В КОД его вписывать нельзя — "
    "репозиторий публичный!"
)

# 2) Папка для временных скачанных файлов: подпапка системного tmp.
#    ВАЖНО: на Render дисковое пространство ограничено, поэтому всё
#    скачанное мы удаляем сразу после отправки (см. finally в хэндлере).
TEMP_ROOT_DIR = Path(tempfile.gettempdir()) / "loadup_media_bot"

# Таймаут на СКАЧИВАНИЕ одного видео, в секундах.
DOWNLOAD_TIMEOUT_SECONDS = 600

# Таймаут на получение МЕТАДАННЫХ (проверка размера), в секундах.
METADATA_TIMEOUT_SECONDS = 90

# Таймаут на ОТПРАВКУ файла в Telegram, в секундах.
UPLOAD_TIMEOUT_SECONDS = 600

# Лимит Telegram на загрузку файла ботом (~50 МБ на официальном Bot API).
TELEGRAM_UPLOAD_LIMIT_MB = 50

# Сколько видео бот обрабатывает ОДНОВРЕМЕННО.
# На тарифе Render Free (0.1 CPU / 512 МБ) безопасное значение — 1:
# второй пользователь получит вежливое «сервер занят», зато сервер
# не упадёт по памяти и не будет выброшен по OOM.
# Увеличьте до 2–3 только если переедете на более мощный тариф.
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1")))

# Буфер чтения с сети / записи на диск в байтах.
# Небольшой буфер = мало RAM. 64 КБ — разумный компромисс.
DOWNLOAD_BUFFER_SIZE = 64 * 1024

# Размер чанка при HTTP-скачивании (yt-dlp http_chunk_size).
# Небольшие чанки позволяют обрывать загрузку раньше и держат RAM низко.
HTTP_CHUNK_SIZE = 5 * 1024 * 1024

# Instagram почти всегда требует cookies браузера.
# Укажите "chrome", "firefox", "brave" и т.п. — либо оставьте None
# (для TikTok / YouTube Shorts / Pinterest обычно не требуется).
COOKIES_FROM_BROWSER: Optional[str] = None

# --- JavaScript-рантайм для yt-dlp (YouTube) ---
# С 2025 года YouTube требует исполнить JS, чтобы получить «player response».
# Без JS-рантайма yt-dlp падает с ошибкой
#   «Failed to extract any player response» / «nsig extraction failed».
# yt-dlp по умолчанию ищет рантайм (deno/node/bun/quickjs) в PATH.
# На Render deno ставится в <project>/.deno/bin (см. render.yaml), поэтому
# на всякий случай сами добавляем этот путь в PATH при старте — так бот
# работает, даже если переменная PATH в окружении настроена неверно.
def _register_js_runtime_paths() -> Optional[str]:
    """Ищет deno/node рядом с проектом и добавляет папку в PATH.

    Возвращает путь к найденному рантайму (или None, если ничего не нашли).
    Это «пояс и подтяжки» поверх PATH из render.yaml.
    """
    # 1) Сначала спрашиваем систему: вдруг deno/node уже есть в PATH
    #    (например, /usr/bin/node на локальной машине). Это дешевле всего
    #    и покрывает случаи, когда PATH настроен правильно.
    for exe in ("deno", "node"):
        found = shutil.which(exe)
        if found:
            return found

    project_dir = Path(__file__).resolve().parent
    # 2) Затем — известные каталоги установки (Render, nodeenv, официальный
    #    установщик deno). Кандидаты перечислены от самых вероятных.
    candidates = [
        # Куда ставит deno наш buildCommand: <project>/.deno/bin
        project_dir / ".deno" / "bin",
        # Куда ставит deno официальный установщик по умолчанию.
        Path.home() / ".deno" / "bin",
        # Куда ставит nodeenv: в окружение Python рядом с интерпретатором.
        Path(sys.prefix) / "bin",
        # Постоянный диск Render (на случай, если .deno выживает между
        # деплоями: DENO_INSTALL=/opt/render/.deno).
        Path("/opt/render/.deno/bin"),
        Path("/opt/render/project/.deno/bin"),
        # Отдельно — путь, который Render иногда прописывает в env.
        Path(os.environ.get("DENO_INSTALL", "/nonexistent"), "bin"),
    ]
    for directory in candidates:
        for exe in ("deno", "node"):
            binary = directory / exe
            if binary.is_file() and os.access(binary, os.X_OK):
                path_parts = os.environ.get("PATH", "").split(os.pathsep)
                if str(directory) not in path_parts:
                    os.environ["PATH"] = (
                        str(directory) + os.pathsep + os.environ.get("PATH", "")
                    )
                return str(binary)
    return None


JS_RUNTIME_PATH = _register_js_runtime_paths()

# Расширения, которые отправляем как фото (карточки Pinterest)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# Подпись, добавляемая к каждому отправленному медиафайлу
CAPTION_TEXT = msg("SUCCESS_CAPTION")

# ============================================================
#  ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("LoadUpMediaBot")
logging.getLogger("aiogram.event").setLevel(logging.WARNING)  # меньше шума
# Сам yt-dlp при quiet=True молчит; на всякий случай приглушим его логгер.
logging.getLogger("yt_dlp").setLevel(logging.WARNING)

# ============================================================
#  ОПРЕДЕЛЕНИЕ ПЛАТФОРМЫ ПО ССЫЛКЕ
# ============================================================
PLATFORM_NAMES: Dict[str, str] = {
    "tiktok":     "TikTok",
    "instagram":  "Instagram",
    "youtube":    "YouTube Shorts",
    "youtu.be":   "YouTube Shorts",
    "pinterest":  "Pinterest",
    "pin.it":     "Pinterest",
}


def detect_platform(url: str) -> Optional[str]:
    """Определяет платформу по домену в ссылке.

    Возвращает человекочитаемое название платформы или None,
    если ссылка не относится к поддерживаемым сайтам.
    """
    lowered = url.lower()
    for key, name in PLATFORM_NAMES.items():
        if key in lowered:
            return name
    return None


def extract_url(text: str) -> Optional[str]:
    """Достаёт первый http(s)-адрес из текста сообщения."""
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else None

# ============================================================
#  НАСТРОЙКИ И СКАЧИВАНИЕ ЧЕРЕЗ yt-dlp
# ============================================================

# Единая строка выбора формата: стараемся получить готовый mp4,
# чтобы ffmpeg не перекодировал видео (перекодирование — это CPU и время).
VIDEO_FORMAT_SELECTOR = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

# Лимит в байтах — используется и в предпроверке, и как страховка в yt-dlp.
TELEGRAM_UPLOAD_LIMIT_BYTES = TELEGRAM_UPLOAD_LIMIT_MB * 1024 * 1024


def build_ydl_opts(
    output_path: Path,
    max_filesize: int = TELEGRAM_UPLOAD_LIMIT_BYTES,
) -> Dict[str, Any]:
    """Собирает настройки yt-dlp для СКАЧИВАНИЯ одного видео.

    Ключевая идея — «streaming to disk»: yt-dlp пишет данные прямо в файл
    небольшими блоками, поэтому в оперативной памяти живёт лишь маленький
    буфер (а не весь видеофайл). Это критично на Render с 512 МБ RAM.

    Аргумент output_path нужен, чтобы после скачивания точно знать,
    где лежит готовый файл (yt-dlp сам подставляет своё расширение,
    если формат видео не совпал с нашим шаблоном).
    """
    opts: Dict[str, Any] = {
        # Формат: лучшее видео mp4 + лучший звук m4a; иначе цельный mp4; иначе лучшее.
        "format": VIDEO_FORMAT_SELECTOR,
        # Шаблон имени файла: <tempdir>/%(id)s.%(ext)s — файл сразу на диске.
        "outtmpl": str(output_path / "%(id)s.%(ext)s"),

        # --- МИНИМУМ RAM: потоковая запись на диск ---
        # Размер внутреннего буфера чтения/записи (по умолчанию ~128 КБ).
        "buffersize": DOWNLOAD_BUFFER_SIZE,
        # Качать HLS/DASH-фрагменты строго по одному — меньше пиковая память.
        "concurrent_fragment_downloads": 1,
        # http_chunk_size: качаем чанками, чтобы можно было оборвать по размеру.
        "http_chunk_size": HTTP_CHUNK_SIZE,
        # Не создавать побочных файлов-кэшей yt-dlp на диске.
        "cachedir": False,
        # Не оставлять «.part»-файлы и не докачивать — при обрыве всё удаляем.
        "nopart": True,
        "continuedl": False,
        "overwrites": True,

        # --- ЗАЩИТА ОТ БОЛЬШИХ ФАЙЛОВ (вторая линия обороны) ---
        # Если по какой-то причине метаданные соврали и файл превысит лимит,
        # yt-dlp сам прервёт скачивание, не забивая диск.
        "max_filesize": max_filesize,

        # --- СЕТЕВЫЕ ТАЙМАУТЫ И ПОВТОРЫ ---
        "socket_timeout": 20,      # не зависать на «мёртвом» соединении
        "retries": 3,              # повторы при сетевых сбоях
        "fragment_retries": 3,
        "file_access_retries": 2,

        # --- JavaScript-РАНТАЙМ (обход защиты YouTube) ---
        # Явно разрешаем deno/node: без JS yt-dlp не получит player response
        # и упадёт с «Failed to extract any player response».
        "js_runtimes": {"deno": {}, "node": {}},

        # --- ТИХИЙ РЕЖИМ И ПРОЧЕЕ ---
        "quiet": True,             # не выводить служебные сообщения в консоль
        "noprogress": True,        # и без прогресс-бара (чистая консоль)
        "no_warnings": True,       # без предупреждений yt-dlp в логах
        "noplaylist": True,        # скачиваем только одно видео, не весь плейлист
        "ignore_errors": False,    # ошибку должен увидеть наш обработчик
        "trim_file_name": 120,     # защита от слишком длинных имён файлов
    }

    # Авторизация для Instagram (если требуется): взять cookies из браузера.
    # COOKIES_FROM_BROWSER задаётся в блоке настроек выше ("chrome"/"firefox"/…).
    if COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER,)

    return opts


def build_probe_opts() -> Dict[str, Any]:
    """Настройки yt-dlp для БЫСТРОГО получения метаданных (без скачивания).

    Мы только «спрашиваем» у сайта информацию о видео: формат, размер,
    длительность. Файл при этом не качается, поэтому трафик и дисковое
    место не расходуются.
    """
    opts: Dict[str, Any] = {
        # Тот же селектор форматов, что и при скачивании, — иначе размер
        # реально скачанного файла может отличаться от оценённого.
        "format": VIDEO_FORMAT_SELECTOR,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,        # метаданные только по одному видео
        "skip_download": True,     # ФАЙЛ НЕ СКАЧИВАЕМ — только метаданные
        "socket_timeout": 20,
        "retries": 2,
        "cachedir": False,
        # JS-рантайм нужен и на этапе метаданных: иначе YouTube-видео
        # не «раскроется» и предпроверка размера не сработает.
        "js_runtimes": {"deno": {}, "node": {}},
    }
    if COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER,)
    return opts


# Семафор ограничивает число одновременных тяжёлых операций.
# Так 0.1 CPU и 512 МБ RAM на Render Free не будут «задушены» толпой
# запросов: лишние пользователи получат вежливое «сервер занят».
_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


class FileTooLargeError(RuntimeError):
    """Видео больше лимита Telegram — скачивать его бессмысленно."""

    def __init__(self, size_bytes: Optional[int]) -> None:
        super().__init__(
            "Файл больше допустимого лимита Telegram"
            + (f" ({size_bytes} байт)" if size_bytes else "")
        )
        # Размер в байтах (None, если точный размер определить не удалось).
        self.size_bytes = size_bytes

    @property
    def size_mb(self) -> Optional[float]:
        """Размер файла в мегабайтах (или None, если он неизвестен)."""
        return None if self.size_bytes is None else self.size_bytes / (1024 * 1024)


def _estimate_known_size(info: Dict[str, Any]) -> Tuple[Optional[int], bool]:
    """Оценивает размер БУДУЩЕГО файла по метаданным yt-dlp.

    Возвращает кортеж (размер_в_байтах, точно_ли_известен):
      * суммируем размеры отдельных дорожек из requested_formats
        (это видео + аудио, которые ffmpeg потом склеит в один файл);
      * если дорожек нет — берём filesize/filesize_approx выбранного формата.

    Размер может быть неизвестен (None): некоторые сайты его не отдают.
    В этом случае бот продолжит работу и подстрахуется на этапе скачивания
    (max_filesize + проверка фактического размера файла перед отправкой).
    """
    streams = info.get("requested_formats") or []
    if streams:
        total = 0
        known = False
        for stream in streams:
            size = stream.get("filesize") or stream.get("filesize_approx")
            if size:
                total += int(size)
                known = True
        if known:
            # Небольшой запас на склейку контейнера mp4.
            return int(total * 1.02), False

    size = info.get("filesize") or info.get("filesize_approx")
    if size:
        # filesize — точный размер; filesize_approx — прикидка, даём +5% запаса.
        exact = bool(info.get("filesize"))
        return int(size * (1.0 if exact else 1.05)), exact

    return None, False


async def probe_video_size(url: str) -> Tuple[Optional[int], Dict[str, Any]]:
    """Получает метаданные видео и предварительный размер (без скачивания).

    Тяжёлый синхронный вызов yt-dlp выполняется в отдельном потоке, поэтому
    event loop aiogram НЕ блокируется: бот продолжает отвечать другим людям.

    Возвращает (размер_в_байтах_или_None, info_dict).
    Бросает RuntimeError, если метаданные получить не удалось.
    """
    opts = build_probe_opts()

    def _blocking_probe() -> Dict[str, Any]:
        """Чисто синхронная функция — исполняется в отдельном потоке."""
        with yt_dlp.YoutubeDL(opts) as ydl:
            # download=False => yt-dlp только «спрашивает» сайт о видео.
            return ydl.extract_info(url, download=False)

    loop = asyncio.get_running_loop()
    coro = loop.run_in_executor(None, functools.partial(_blocking_probe))
    try:
        info: Dict[str, Any] = await asyncio.wait_for(
            coro, timeout=METADATA_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"Получение метаданных превысило {METADATA_TIMEOUT_SECONDS} сек."
        )

    # yt-dlp может вернуть playlist-обёртку — берём первое видео.
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next((e for e in info["entries"] if e), info)

    size_bytes, _exact = _estimate_known_size(info)
    return size_bytes, info


async def download_with_ytdlp(url: str, output_path: Path) -> Path:
    """Асинхронная обёртка над yt-dlp: ПОТОКОВО скачивает видео на диск.

    Блокирующий вызов yt_dlp.YoutubeDL(...).download() выполняется через
    loop.run_in_executor в ОТДЕЛЬНОМ ПОТОКЕ — благодаря этому бот продолжает
    отвечать другим пользователям, пока качается видео.

    Возвращает путь к готовому файлу. Бросает RuntimeError при ошибке.
    """
    # Защита от больших файлов и на этом уровне тоже (max_filesize).
    ydl_opts = build_ydl_opts(output_path)

    def _blocking_download() -> None:
        """Чисто синхронная функция — исполняется в отдельном потоке."""
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    coro = asyncio.get_running_loop().run_in_executor(
        None,                       # дефолтный ThreadPoolExecutor
        functools.partial(_blocking_download),
    )
    try:
        await asyncio.wait_for(coro, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"Скачивание превысило {DOWNLOAD_TIMEOUT_SECONDS} сек и было прервано."
        )

    # yt-dlp мог выбрать формат с другим расширением (webm/mkv и т.п.)
    # или с «грязным» id — ищем самый большой готовый файл в НАШЕЙ
    # персональной временной папке (output_path уникален для этого запроса).
    candidates = [
        p for p in output_path.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        # Отбрасываем служебные файлы yt-dlp (они не являются медиа).
        and p.suffix.lower() not in {".part", ".ytdl", ".json", ".description", ".temp"}
    ]
    if not candidates:
        raise RuntimeError("yt-dlp не создал файл — возможно, видео недоступно.")

    # Если скачалось несколько частей (видео + аудио по отдельности),
    # ffmpeg уже склеил их — берём самый большой (это и есть готовое видео).
    result = max(candidates, key=lambda p: p.stat().st_size)

    # Третья линия обороны: даже если метаданные и max_filesize не сработали,
    # не отдаём в Telegram файл, который всё равно не пролезет по лимиту.
    actual_size = result.stat().st_size
    if actual_size > TELEGRAM_UPLOAD_LIMIT_BYTES:
        raise FileTooLargeError(actual_size)

    return result

# ============================================================
#  ОТПРАВКА ГОТОВОГО ФАЙЛА ПОЛЬЗОВАТЕЛЮ
# ============================================================
async def send_media_file(bot: Bot, chat_id: int, file_path: Path) -> None:
    """Отправляет готовый файл в чат: видео — как Video, картинку — как Photo.

    FSInputFile НЕ загружает файл в оперативную память: aiogram читает его
    с диска чанками по 64 КБ (aiofiles) и сразу отправляет в Telegram.
    Это ключевой момент для работы на 512 МБ RAM.

    Отправка обёрнута в wait_for, чтобы «залипший» аплоад не держал
    временный файл на диске бесконечно.
    """
    ext = file_path.suffix.lower()

    async def _do_send() -> None:
        if ext in IMAGE_EXTS:
            # Карточки Pinterest часто являются обычными картинками.
            await bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(file_path),
                caption=CAPTION_TEXT,
            )
            return

        # Всё остальное отправляем как видео (FSInputFile стримит с диска).
        await bot.send_video(
            chat_id=chat_id,
            video=FSInputFile(file_path),
            caption=CAPTION_TEXT,
            supports_streaming=True,
        )

    try:
        await asyncio.wait_for(_do_send(), timeout=UPLOAD_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"Отправка файла превысила {UPLOAD_TIMEOUT_SECONDS} сек и была прервана."
        )
    except TelegramRetryAfter as exc:
        # Telegram просит подождать (флуд-контроль) — ждём и пробуем ещё раз.
        logger.warning("Флуд-контроль Telegram: ждём %s сек.", exc.retry_after)
        await asyncio.sleep(exc.retry_after)
        await asyncio.wait_for(_do_send(), timeout=UPLOAD_TIMEOUT_SECONDS)


def friendly_error_text(exc: Exception, url: str) -> str:
    """Превращает исключение в понятный пользователю текст ошибки.

    Отдельно (и первым делом) обрабатывает FileTooLargeError — это самая
    частая «ожидаемая» ошибка, и текст для неё живёт в texts.py.

    Аргумент url оставлен в сигнатуре, чтобы при желании показать
    его пользователю в сообщении об ошибке.
    """
    # Файл больше лимита Telegram — показываем отдельный понятный текст.
    if isinstance(exc, FileTooLargeError):
        if exc.size_mb is not None:
            return msg(
                "ERROR_TOO_LARGE",
                size_mb=f"{exc.size_mb:.1f}",
                limit_mb=TELEGRAM_UPLOAD_LIMIT_MB,
            )
        return msg("ERROR_TOO_LARGE_UNKNOWN", limit_mb=TELEGRAM_UPLOAD_LIMIT_MB)

    # Наш собственный таймаут скачивания/отправки.
    if "превысило" in str(exc):
        return msg("ERROR_TIMEOUT")

    # yt-dlp не создал файл.
    if "не создал файл" in str(exc):
        return msg("ERROR_NO_FILE")

    raw = str(exc).lower()

    if "unsupported url" in raw:
        return (
            "❌ Сайт не поддерживается или ссылка изменилась.\n"
            "Попробуйте открыть пост в браузере и скопировать адрес из строки поиска."
        )
    if "private" in raw or "login required" in raw:
        return (
            "❌ Это приватный пост (например, закрытый профиль Instagram).\n"
            "Нужна авторизация: администратор должен настроить cookies браузера."
        )
    if "age" in raw and "restricted" in raw:
        return "❌ Видео с возрастным ограничением — доступ запрещён."
    if "removed" in raw or "deleted" in raw or "unavailable" in raw or "not available" in raw:
        return "❌ Видео удалено или временно недоступно."
    if "geo" in raw or "country" in raw:
        return "❌ Видео недоступно в вашем регионе (гео-ограничение)."
    if "429" in raw or "rate limit" in raw:
        return "⏳ Слишком много запросов к сайту. Попробуйте через минуту."

    # Общий запасной вариант — первая строка сообщения об ошибке
    first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else "неизвестная ошибка"
    return (
        "⚠️ Не удалось скачать медиа. Возможные причины:\n"
        "• ссылка битая или пост удалён;\n"
        "• платформа временно ограничила доступ;\n"
        "• Instagram требует авторизацию (нужны cookies браузера).\n"
        f"Детали: <code>{html.escape(first_line[:300])}</code>"
    )

# ============================================================
#  РОУТЕР И ХЭНДЛЕРЫ КОМАНД
# ============================================================
router = Router(name="media_downloader")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """/start — приветствие с информацией о проекте и поддерживаемых платформах."""
    user = message.from_user
    name = html.escape(user.first_name) if user else "друг"
    await message.answer(msg("START", name=name))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """/help — инструкция по скачиванию и список команд."""
    await message.answer(msg("HELP"))

# ============================================================
#  ГЛАВНЫЙ ОБРАБОТЧИК ССЫЛОК
# ============================================================
@router.message(F.text)
async def handle_link(message: Message) -> None:
    """Принимает ссылку, ПРОВЕРЯЕТ размер, скачивает и отправляет файл.

    Полный конвейер:
      1) валидация ссылки и определение платформы;
      2) предпроверка размера через метаданные yt-dlp (БЕЗ скачивания);
      3) потоковое скачивание во временную папку ЭТОГО запроса;
      4) отправка файла из диска чанками (FSInputFile);
      5) гарантированное удаление временной папки в finally.
    """
    # В сообщениях от имени канала from_user отсутствует — игнорируем их
    if not message.from_user:
        return
    user = message.from_user
    url = extract_url(message.text or "")

    # 1) Это вообще ссылка? Если нет — подсказываем формат.
    if not url:
        await message.answer(msg("ERROR_INVALID_LINK"))
        return

    # 2) Ссылка с поддерживаемого сайта?
    platform = detect_platform(url)
    if not platform:
        await message.answer(msg("ERROR_UNSUPPORTED_SITE"))
        return

    # Не берём в работу больше MAX_CONCURRENT_DOWNLOADS задач одновременно:
    # на Render Free (0.1 CPU / 512 МБ) это защищает сервер от OOM. Если
    # семафор занят — честно просим пользователя повторить попытку позже.
    if _DOWNLOAD_SEMAPHORE.locked():
        logger.info("Пользователь %s: сервер занят, запрос отклонён.", user.id)
        await message.answer(msg("ERROR_BUSY"))
        return

    logger.info("Пользователь %s запросил %s: %s", user.id, platform, url)

    # 3) Сообщение-«анимация ожидания» (позже редактируем его в готовое).
    status = await message.answer(
        msg("STATUS_CHECKING", frame=PROGRESS_FRAMES[0])
    )

    work_dir: Optional[Path] = None
    try:
        async with _DOWNLOAD_SEMAPHORE:
            # --- ШАГ 1: ПРЕДПРОВЕРКА РАЗМЕРА (трафик НЕ тратится) ---
            logger.info("Получаю метаданные для %s", url)
            size_bytes, _info = await probe_video_size(url)
            if size_bytes is not None and size_bytes > TELEGRAM_UPLOAD_LIMIT_BYTES:
                size_mb = size_bytes / (1024 * 1024)
                # Файл больше лимита Telegram. Если облако настроено — качаем
                # и отдаём ссылкой (файлом всё равно нельзя). Если нет —
                # честно отказываем, НЕ скачивая (экономим трафик и диск).
                if not storage.is_configured():
                    logger.info(
                        "Отклонено: видео %.1f МБ > лимита %s МБ (%s)",
                        size_mb, TELEGRAM_UPLOAD_LIMIT_MB, url,
                    )
                    await status.edit_text(
                        msg(
                            "ERROR_TOO_LARGE",
                            size_mb=f"{size_mb:.1f}",
                            limit_mb=TELEGRAM_UPLOAD_LIMIT_MB,
                        )
                    )
                    return  # ничего не скачивали — чистить нечего
                logger.info(
                    "Видео %.1f МБ > лимита %s МБ — пойдёт ссылкой в облако.",
                    size_mb, TELEGRAM_UPLOAD_LIMIT_MB,
                )

            if size_bytes is None:
                logger.info("Размер заранее неизвестен — проверю после скачивания.")

            # --- ШАГ 2: ПОТОКОВОЕ СКАЧИВАНИЕ В УНИКАЛЬНУЮ ВРЕМЕННУЮ ПАПКУ ---
            # Отдельная папка на каждый запрос: чужие файлы не пересекаются,
            # а очистка сводится к одному rmtree именно этой папки.
            TEMP_ROOT_DIR.mkdir(parents=True, exist_ok=True)
            work_dir = Path(tempfile.mkdtemp(prefix="job_", dir=TEMP_ROOT_DIR))

            await status.edit_text(
                msg("STATUS_DOWNLOADING", platform=platform, frame=PROGRESS_FRAMES[0])
            )
            logger.info("Начинаю скачивание в %s", work_dir)
            downloaded = await download_with_ytdlp(url, work_dir)
            logger.info(
                "Скачано: %s (%.2f МБ)",
                downloaded.name, downloaded.stat().st_size / (1024 * 1024),
            )

            # --- ШАГ 3: ФАКТИЧЕСКАЯ ПРОВЕРКА РАЗМЕРА ПЕРЕД ОТПРАВКОЙ ---
            actual_mb = downloaded.stat().st_size / (1024 * 1024)
            if actual_mb > TELEGRAM_UPLOAD_LIMIT_MB:
                # Метаданные могли соврать — файл оказался больше лимита.
                # Если облако доступно, отдаём ссылкой; иначе отказываем.
                if not storage.is_configured():
                    logger.info("Отклонено после скачивания: %.1f МБ", actual_mb)
                    await status.edit_text(
                        msg(
                            "ERROR_TOO_LARGE",
                            size_mb=f"{actual_mb:.1f}",
                            limit_mb=TELEGRAM_UPLOAD_LIMIT_MB,
                        )
                    )
                    return  # work_dir удалится в finally

                logger.info(
                    "После скачивания %.1f МБ > лимита — загружаю в облако.",
                    actual_mb,
                )
                await status.edit_text(
                    msg("STATUS_UPLOADING_CLOUD", frame=PROGRESS_FRAMES[0])
                )
                try:
                    # upload_and_get_link синхронный (boto3) — уводим в поток,
                    # чтобы не блокировать event loop бота на время загрузки.
                    link = await asyncio.to_thread(
                        storage.upload_and_get_link, downloaded
                    )
                except RuntimeError as exc:
                    logger.error("Загрузка в облако не удалась: %s", exc)
                    await status.edit_text(
                        msg(
                            "ERROR_CLOUD_UPLOAD",
                            size_mb=f"{actual_mb:.1f}",
                            limit_mb=TELEGRAM_UPLOAD_LIMIT_MB,
                        )
                    )
                    return  # work_dir удалится в finally

                await status.edit_text(
                    msg(
                        "SUCCESS_CLOUD_LINK",
                        size_mb=f"{actual_mb:.1f}",
                        limit_mb=TELEGRAM_UPLOAD_LIMIT_MB,
                        link=html.escape(link, quote=True),
                        ttl_min=storage.R2_LINK_TTL_SECONDS // 60,
                    ),
                    disable_web_page_preview=True,
                )
                logger.info("Ссылка на облако отправлена пользователю %s", user.id)
                return  # work_dir удалится в finally

            # --- ШАГ 4: ОТПРАВКА (файл стримится с диска, не из RAM) ---
            await status.edit_text(msg("STATUS_SENDING"))
            logger.info("Отправляю файл пользователю %s", user.id)
            await send_media_file(message.bot, message.chat.id, downloaded)
            await status.delete()
            logger.info("Файл успешно отправлен пользователю %s", user.id)

    except TelegramEntityTooLarge:
        logger.warning("Telegram отклонил файл как слишком большой (%s)", url)
        await status.edit_text(
            msg(
                "ERROR_TOO_LARGE_UNKNOWN",
                limit_mb=TELEGRAM_UPLOAD_LIMIT_MB,
            )
        )
    except Exception as exc:
        # Любая ошибка -> понятное сообщение пользователю + лог для админа.
        logger.exception("Ошибка при обработке %s", url)
        try:
            await status.edit_text(friendly_error_text(exc, url))
        except TelegramBadRequest:
            pass
    finally:
        # --- ШАГ 5: ГАРАНТИРОВАННАЯ ОЧИСТКА ДИСКА ---
        # Удаляем всю временную папку этого запроса целиком: и при успехе,
        # и при ошибке, и при таймауте. Файловая система Render не забивается.
        if work_dir is not None and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.info("Временная папка удалена: %s", work_dir.name)


# ============================================================
#  ТОЧКА ВХОДА
# ============================================================

# Порт, который Render отдаёт сервису через переменную окружения PORT.
# Локально (без Render) используем 10000 — тот же дефолт, что и у Render.
HEALTHCHECK_PORT = int(os.getenv("PORT", "10000"))


async def handle_healthcheck(request: "web.Request") -> "web.Response":
    """Простой health-check: любой GET отвечает 200 OK.

    Нужен, чтобы Render видел открытый порт и не считал деплой упавшим
    («no open ports detected»). Логику бота не трогает.
    """
    return web.Response(text="OK")


async def start_healthcheck_server() -> "web.AppRunner":
    """Поднимает минимальный HTTP-сервер на порту PORT.

    Возвращает AppRunner — вызывающая сторона может позже корректно
    закрыть сервер (runner.cleanup()).
    """
    app = web.Application()
    app.router.add_get("/", handle_healthcheck)
    app.router.add_get("/healthz", handle_healthcheck)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", HEALTHCHECK_PORT)
    await site.start()

    logger.info("Health-check сервер слушает 0.0.0.0:%d", HEALTHCHECK_PORT)
    return runner


async def main() -> None:
    """Инициализация бота и запуск polling."""
    # Токен обязан прийти из переменной окружения BOT_TOKEN (см. блок
    # НАСТРОЙКИ выше). Если его нет — останавливаемся сразу с подсказкой,
    # чтобы не лезть в Telegram API с пустой строкой.
    if not BOT_TOKEN or "PASTE_YOUR_BOT_TOKEN_HERE" in BOT_TOKEN:
        raise SystemExit(BOT_TOKEN_HINT)

    # Готовим корневую временную папку. На старте вычищаем «хвосты»
    # прошлых запусков (если бот был убит и не успел убрать за собой) —
    # так диск Render не забивается мусором.
    TEMP_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    removed = 0
    for stale in TEMP_ROOT_DIR.iterdir():
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("Очищено временных папок прошлых запусков: %d", removed)

    logger.info("Временная папка: %s", TEMP_ROOT_DIR)
    logger.info(
        "Лимит Telegram: %s МБ | параллельных загрузок: %d",
        TELEGRAM_UPLOAD_LIMIT_MB, MAX_CONCURRENT_DOWNLOADS,
    )
    # Сообщаем, какой JS-рантайм найден: без него YouTube не скачивается.
    if JS_RUNTIME_PATH:
        logger.info("JS-рантайм для yt-dlp: %s", JS_RUNTIME_PATH)
    else:
        logger.warning(
            "JS-рантайм (deno/node) не найден! YouTube-видео, скорее всего, "
            "не скачаются: установите deno (см. render.yaml) или node."
        )

    # Что делать с видео больше лимита Telegram: ссылкой в облако или отказ.
    if storage.is_configured():
        logger.info("Облако R2: %s (ссылки живут %d мин.)",
                    storage.describe_config(), storage.R2_LINK_TTL_SECONDS // 60)
    else:
        logger.warning(
            "Облако R2 не настроено: видео больше %s МБ будут отклоняться. "
            "Задайте R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, "
            "R2_BUCKET (см. .env.example) — тогда они будут отдаваться ссылкой.",
            TELEGRAM_UPLOAD_LIMIT_MB,
        )

    # Экземпляр бота (aiogram 3): ParseMode.HTML по умолчанию
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()          # диспетчер маршрутизации апдейтов
    dp.include_router(router)  # подключаем наши хэндлеры

    # Регистрируем команды для меню Telegram (список слева от поля ввода)
    await bot.set_my_commands([
        BotCommand(command="start", description="🚀 Запустить бота"),
        BotCommand(command="help", description="ℹ️ Справка"),
    ])

    # Сбрасываем «хвост» непрочитанных апдейтов и запускаем polling.
    # ВАЖНО: с одним токеном должен работать РОВНО ОДИН процесс, иначе
    # Telegram отдаёт TelegramConflictError (см. примечание в шапке файла).
    await bot.delete_webhook(drop_pending_updates=True)

    # Поднимаем минимальный HTTP-сервер ДО polling: на бесплатном тарифе
    # Render сервис типа «web» обязан слушать порт, иначе деплой падает
    # с «no open ports detected». Если порт занят (например, запущен
    # второй инстанс), бот продолжит работать — это не критично.
    try:
        healthcheck_runner = await start_healthcheck_server()
    except OSError as exc:
        healthcheck_runner = None
        logger.warning("Не удалось поднять health-check сервер: %s", exc)

    logger.info("Бот запущен. Ожидание сообщений…")
    try:
        await dp.start_polling(bot)
    finally:
        # Аккуратно гасим HTTP-сервер (если он был поднят).
        if healthcheck_runner is not None:
            await healthcheck_runner.cleanup()
            logger.info("Health-check сервер остановлен.")
        # При остановке подчищаем временную папку целиком.
        shutil.rmtree(TEMP_ROOT_DIR, ignore_errors=True)
        logger.info("Бот остановлен, временные файлы очищены.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as e:
        # SystemExit несёт наше сообщение-подсказку — печатаем его
        if isinstance(e, SystemExit) and e.code:
            print(e.code)
        print("\n👋 Бот остановлен.")






