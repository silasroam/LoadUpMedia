# LoadUpMediaBot

Telegram-бот для скачивания медиа: TikTok, Instagram Reels, YouTube Shorts, Pinterest.
Стек: Python 3.10+, aiogram 3.x, yt-dlp (+ deno/node как JS-рантайм).

## 🔒 Настройка токена (обязательно)

Токен бота **не хранится в репозитории**. Он читается только из переменной
окружения `BOT_TOKEN` (`bot.py`, блок «НАСТРОЙКИ»):

```python
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
```

Если переменная не задана, бот останавливается сразу с подсказкой.

### На Render

Dashboard → ваш сервис → **Environment** → **Add Environment Variable**:

| Key | Value |
|---|---|
| `BOT_TOKEN` | ваш токен от @BotFather |
| `MAX_CONCURRENT_DOWNLOADS` | `1` (для тарифа Free) |

`render.yaml` уже содержит эти ключи; `BOT_TOKEN` помечен `sync: false`,
поэтому его значение вводится только в UI и в git не попадает.

### Локально

```bash
cp .env.example .env
# впишите свой токен в .env
export BOT_TOKEN="123456:ABC-ваш-токен"   # либо source .env
python3 bot.py
```

`.env` добавлен в `.gitignore` и в репозиторий не попадёт.
В репозитории лежит только `.env.example` с плейсхолдерами.

## ☁️ Облако Cloudflare R2 (для видео больше 50 МБ)

Telegram Bot API не даёт ботам отправлять в чат файлы больше **50 МБ**.
Это ограничение Telegram, а не Render: 512 МБ на Free — это оперативная
память сервера, а не лимит на размер файла.

Поэтому большие видео бот отдаёт **ссылкой**: скачивает файл, заливает в
R2 и присылает пользователю пресайн-ссылку (по умолчанию живёт 1 час).

### Настройка на Render

Dashboard → **Environment** → добавьте (значения — из Cloudflare):

| Key | Где взять |
|---|---|
| `R2_ACCOUNT_ID` | Cloudflare → R2 → Overview (в правой части страницы) |
| `R2_ACCESS_KEY_ID` | R2 → API → Manage API Tokens |
| `R2_SECRET_ACCESS_KEY` | там же (показывается один раз!) |
| `R2_BUCKET` | имя бакета, обычно `bot` |

🔒 **Права токена выдавайте минимальные:**
- Permissions: **Object Read & Write**
- Bucket: **только ваш бакет**, не «All buckets»

Эндпоинт собирается автоматически как
`https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`.
Короткая форма `https://cloudflarestorage.com` **не работает** — без
Account ID запросы уходят «в никуда».

### Необязательные переменные

| Key | По умолчанию | Смысл |
|---|---|---|
| `CLOUD_MAX_DOWNLOAD_GB` | `2` | Максимальный размер видео через облако |
| `CLOUD_UPLOAD_TIMEOUT_SECONDS` | `300` | Таймаут залива в R2 |
| `CLOUD_FILE_TTL_SECONDS` | `600` | Через сколько удалять файл из R2 |
| `R2_LINK_TTL_SECONDS` | `3600` | Сколько живёт пресайн-ссылка |

### 🧹 Очистка мусора в R2 — сделайте обязательно

Объекты в облаке занимают место и стоят денег, поэтому работают
**две независимые линии очистки**:

1. **Код бота** (мгновенно): после отправки ссылки запускается фоновая
   задача — через 10 минут файл удаляется из R2. Настраивается через
   `CLOUD_FILE_TTL_SECONDS`.

2. **Lifecycle Rule в Cloudflare** (страховка) — на случай, если бот
   перезапустился и не успел удалить файл:

   > **Cloudflare Dashboard → R2 → выберите бакет → Settings →
   > Lifecycle Rules → Add rule**
   >
   > - Rule name: `auto-delete-old-media`
   > - Prefix: `media/` (бот складывает файлы в этот префикс)
   > - Action: **Delete objects**
   > - Age: **1 day** (удалять файлы старше 1 дня)

   Это гарантирует, что даже «осиротевшие» файлы не накопятся.
   Настроить правило нужно **один раз** — дальше Cloudflare чистит сам.

### Если R2-ключ утёк

Немедленно: **Cloudflare → R2 → API → Manage API Tokens → удалить токен**
и создать новый. Затем обновить `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`
в переменных окружения. Вычистка ключа из git-истории сама по себе
**не спасает** — ключ нужно именно отозвать.

## Запуск

```bash
pip install -r requirements.txt
BOT_TOKEN="..." python3 bot.py
```

## Если токен всё-таки попал в git

Попадание секрета в историю git нельзя «отменить» обычным коммитом:
старые коммиты остаются доступны по прямой ссылке. Порядок действий:

1. **Отозвать токен**: @BotFather → `/revoke` → выбрать бота → получить новый.
   Старый токен после этого мёртв, даже если утёк.
2. Записать новый токен **только** в переменные окружения (Render / `.env`).
3. Вычистить секрет из истории репозитория (см. ниже).

### Очистка истории

`git-filter-repo` (рекомендуется):

```bash
pip install git-filter-repo
git filter-repo --replace-text <(echo 'СТАРЫЙ_ТОКЕН==>REDACTED')
git push --force origin main
```

Либо через `git filter-branch` (если filter-repo недоступен):

```bash
git filter-branch --tree-filter \
  "grep -rl 'СТАРЫЙ_ТОКЕН' . 2>/dev/null | xargs -r sed -i 's|СТАРЫЙ_ТОКЕН|REDACTED|g'" \
  -- --all
git push --force origin main
```

После force-push секрета в истории не останется, но если репозиторий
публичный и токен пролежал там какое-то время — **отзыв токена
обязателен**, вычистка истории его не спасает.

## Структура

| Файл | Назначение |
|---|---|
| `bot.py` | Логика бота, точка входа, health-check сервер |
| `storage.py` | Загрузка больших видео в Cloudflare R2 |
| `texts.py` | Тексты сообщений (единая точка правки копирайта) |
| `render.yaml` | Конфигурация сервиса Render (type: web, plan: free) |
| `requirements.txt` | Python-зависимости |
| `.env.example` | Шаблон секретов (безопасно коммитить) |
