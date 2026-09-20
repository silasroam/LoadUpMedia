# -*- coding: utf-8 -*-
"""
==============================================================================
 Загрузка готовых видео в Cloudflare R2 (S3-совместимое хранилище).
==============================================================================

 ЗАЧЕМ ЭТО НУЖНО:
   Telegram Bot API НЕ даёт ботам отправлять в чат файлы больше 50 МБ.
   Это ограничение живёт на стороне Telegram и не зависит от оперативной
   памяти сервера (512 МБ на Render Free — это про RAM процесса, а не про
   размер файла для Telegram).

   Поэтому для больших видео мы:
     1. скачиваем файл на диск;
     2. заливаем его в R2;
     3. отдаём пользователю ПРЕСАЙН-ССЫЛКУ (временно действующую),
        по которой он смотрит/скачивает видео в браузере.

   Важно: лимит 50 МБ это НЕ обходит — большое видео по-прежнему не
   приедет в чат файлом. Мы лишь даём рабочий способ получить его.

 БЕЗОПАСНОСТЬ:
   🔒 Все ключи читаются ТОЛЬКО из переменных окружения. В код, в git и в
   логи они не попадают. Задаются на Render в Environment, локально — в .env
   (см. .env.example). Если ключ попал в git или в чат — его НУЖНО отозвать
   в Cloudflare Dashboard → R2 → API → Manage API Tokens.

 ТРЕБУЕМЫЕ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ:
   R2_ACCOUNT_ID         — ID аккаунта Cloudflare (R2 → Overview)
   R2_ACCESS_KEY_ID      — Access Key ID токена R2
   R2_SECRET_ACCESS_KEY  — Secret Access Key токена R2
   R2_BUCKET             — имя бакета (например, bot)
   R2_ENDPOINT           — необязательно; если не задан, собирается
                           из R2_ACCOUNT_ID автоматически
==============================================================================
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

import boto3                      # S3-совместимый клиент (работает с R2)
from botocore.config import Config

logger = logging.getLogger("LoadUpMediaBot")

# --- Настройки из окружения (значений по умолчанию с секретами НЕТ) ---
R2_ACCOUNT_ID: str = os.getenv("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID: str = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY: str = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET: str = os.getenv("R2_BUCKET", "").strip()

# Эндпоинт R2 ОБЯЗАТЕЛЬНО содержит Account ID:
#   https://<ACCOUNT_ID>.r2.cloudflarestorage.com
# Короткий вариант https://cloudflarestorage.com не работает — запрос уйдёт
# «в никуда», поэтому собираем адрес сами, если его не задали явно.
R2_ENDPOINT: str = os.getenv("R2_ENDPOINT", "").strip() or (
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""
)

# Сколько живёт пресайн-ссылка (в секундах). По умолчанию 1 час.
R2_LINK_TTL_SECONDS: int = int(os.getenv("R2_LINK_TTL_SECONDS", "3600"))

# Включатель функции: удобно держать False, если R2 пока не настроен —
# тогда бот просто откажет по размеру, как раньше.
R2_ENABLED: bool = bool(
    R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET
)

_client = None


def _get_client():
    """Ленивая инициализация S3-клиента (создаётся один раз за процесс)."""
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",          # R2 требует именно "auto"
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
    return _client


def is_configured() -> bool:
    """Проверяет, заданы ли все нужные переменные окружения."""
    return R2_ENABLED


def describe_config() -> str:
    """Безопасное описание конфигурации БЕЗ секретов (для логов)."""
    if not R2_ENABLED:
        return "R2 не настроен (не заданы переменные окружения)"
    return f"R2 бакет '{R2_BUCKET}', endpoint '{R2_ENDPOINT}'"


def upload_and_get_link(local_file_path: Path) -> Tuple[str, str]:
    """Загружает файл в R2. Возвращает (пресайн-ссылка, ключ объекта).

    Ключ объекта нужен вызывающей стороне, чтобы потом удалить файл из
    облака (у нас для этого есть фоновая задача).

    ВАЖНО: файл с диска здесь НЕ удаляется. Удалением занимается штатный
    `finally` в обработчике (он сносит всю временную папку запроса целиком),
    поэтому повторное ручное удаление только мешало бы чистке.

    :raises RuntimeError: если R2 не настроен, файла нет или загрузка не удалась.
    """
    if not is_configured():
        raise RuntimeError(
            "R2 не настроен: задайте R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY и R2_BUCKET в переменных окружения."
        )

    path = Path(local_file_path)
    if not path.is_file():
        raise RuntimeError(f"Файл для загрузки не найден: {path}")

    # Ключ объекта: префикс по дате, чтобы объекты не сваливались в одну кашу
    # и не перезаписывали друг друга при совпадении имён.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    object_key = f"media/{stamp}/{path.name}"

    size_mb = path.stat().st_size / (1024 * 1024)
    logger.info("Загружаю в R2 %.1f МБ → %s", size_mb, object_key)

    try:
        client = _get_client()
        client.upload_file(
            str(path),
            R2_BUCKET,
            object_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
    except Exception as exc:
        # Ловим ШИРОКО: boto3 на неверном endpoint/креденшелах бросает
        # ValueError, а не только BotoCoreError/ClientError. Голое исключение
        # здесь уронило бы весь обработчик — а нам нужно показать понятный
        # текст и продолжить работу.
        logger.error("Ошибка загрузки в R2: %s: %s", type(exc).__name__, exc)
        raise RuntimeError("Не удалось загрузить видео в облако.") from exc

    logger.info("Загрузка в R2 завершена: %s", object_key)

    try:
        link = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": object_key},
            ExpiresIn=R2_LINK_TTL_SECONDS,
        )
    except Exception as exc:
        logger.error(
            "Не удалось создать пресайн-ссылку: %s: %s", type(exc).__name__, exc
        )
        raise RuntimeError("Не удалось создать ссылку на видео.") from exc

    return link, object_key


def delete_object(object_key: str) -> bool:
    """Удаляет объект из бакета (для ручной чистки / тестов).

    Возвращает True при успехе. Ошибки логируются, но не выбрасываются:
    уборка не должна ломать основной сценарий.
    """
    if not is_configured():
        return False
    try:
        _get_client().delete_object(Bucket=R2_BUCKET, Key=object_key)
        logger.info("Объект удалён из R2: %s", object_key)
        return True
    except Exception as exc:
        logger.warning(
            "Не удалось удалить объект %s из R2: %s: %s",
            object_key, type(exc).__name__, exc,
        )
        return False
