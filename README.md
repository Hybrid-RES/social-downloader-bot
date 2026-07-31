# Social Downloader Bot

Приватный Telegram-бот для домашнего сервера: отправляете ссылку с телефона через **Поделиться → Telegram**, а бот самостоятельно скачивает фото, видео, GIF или аудио на сервер и при необходимости возвращает готовые файлы в тот же чат.

В одном контейнере работают:

- `gallery-dl` — изображения, карусели и медиапубликации;
- `yt-dlp` — видео/аудио и резервный загрузчик;
- `ffmpeg` — объединение видео и аудио и постобработка;
- Deno + `yt-dlp-ejs` — современная поддержка JavaScript-проверок YouTube;
- SQLite — постоянная очередь, история, настройки чатов и защита от повторной постановки URL;
- `python-telegram-bot` — Telegram long polling без открытых входящих портов.

## Логика выбора загрузчика

| Платформа | Основная попытка | Резервная попытка |
|---|---|---|
| YouTube / Shorts | `yt-dlp` | — |
| Instagram | `gallery-dl` | `yt-dlp` |
| X / Twitter | `gallery-dl` | `yt-dlp` |
| TikTok | `gallery-dl` | `yt-dlp` |
| Facebook | `gallery-dl` | `yt-dlp` |
| Threads | нативный экстрактор | `yt-dlp`, `gallery-dl` |
| LinkedIn | нативные изображения / плагин `yt-dlp` | `gallery-dl` |
| Pinterest | `gallery-dl` | `yt-dlp` |
| Tumblr | нативный экстрактор | `gallery-dl`, `yt-dlp` |
| Другой сайт | `yt-dlp` | `gallery-dl` |

Для Twitter и Instagram `gallery-dl` может передавать видео внутреннему модулю `yt-dlp`.

## Структура хранения

```text
/media/social-downloads/
├── 2026-07/
│   ├── YouTube/
│   ├── Instagram/
│   ├── Twitter/
│   ├── TikTok/
│   ├── Facebook/
│   ├── Threads/
│   ├── LinkedIn/
│   ├── Pinterest/
│   ├── Tumblr/
│   ├── Other/
│   └── Failed/
├── 2026-08/
│   └── ...
└── .work/
```

Сначала файлы загружаются в `.work/job-ID`. В конечную папку месяца они перемещаются только после успешного завершения. Одинаковые файлы внутри задания удаляются по SHA-256.

## Автоматическая отправка файлов в Telegram

После успешной загрузки бот может отправить файлы обратно в тот же Telegram-чат:

- MP4 отправляется как видео с предпросмотром;
- остальные файлы отправляются как документы без дополнительного сжатия;
- файлы больше настроенного предела остаются на сервере, а бот присылает уведомление;
- ошибка Telegram-доставки не меняет успешный статус скачивания.

Глобальные настройки Compose:

```yaml
TELEGRAM_SEND_FILES: "true"
TELEGRAM_MAX_UPLOAD_MB: "49"
```

`TELEGRAM_SEND_FILES=false` полностью отключает отправку файлов. При включённой глобальной настройке управление для конкретного чата выполняется командами:

```text
/files_on       включить отправку в текущий чат
/files_off      выключить отправку в текущий чат
/files_status   показать глобальное и чат-специфичное состояние
```

Настройка чата хранится в SQLite и сохраняется после обновлений и перезапусков контейнера.

## Нужны ли логины и пароли социальных сетей

**Нет. Никогда не записывайте логины и пароли Instagram, X, Facebook, LinkedIn или YouTube в Compose либо `.env`.**

Сначала бот пытается скачать публичный материал без авторизации. Когда платформа требует вход, используйте экспортированный файл cookies в Netscape/Mozilla-формате:

```text
/datamain/docker/appdata/social-downloader/cookies/
├── instagram.txt
├── twitter.txt       # также принимается x.txt
├── facebook.txt
├── threads.txt       # при отсутствии используется instagram.txt
├── linkedin.txt
├── pinterest.txt
├── tumblr.txt
├── youtube.txt
└── tiktok.txt
```

Бот автоматически передаёт только подходящий платформе файл. Cookies остаются на домашнем сервере, не входят в Docker-образ и исключены из Git.

Рекомендуется отдельный аккаунт для автоматизированного скачивания: сайты могут завершать сессии, требовать CAPTCHA или ограничивать аккаунт.

```bash
chmod 700 /datamain/docker/appdata/social-downloader/cookies
chmod 600 /datamain/docker/appdata/social-downloader/cookies/*.txt
```

## Создание Telegram-бота

1. Откройте `@BotFather`.
2. Выполните `/newbot` и сохраните токен.
3. Узнайте свой числовой Telegram ID, например через `@userinfobot`.
4. Создайте локальный `.env` рядом с Compose:

```env
TELEGRAM_BOT_TOKEN=1234567890:replace_me
ALLOWED_USER_IDS=123456789
```

Можно разрешить несколько пользователей через запятую:

```env
ALLOWED_USER_IDS=123456789,987654321
```

Все остальные получают `Доступ запрещён`.

## Установка Docker Compose

Скопируйте `docker-compose.example.yml` как `docker-compose.yml` и измените два пути хоста:

```yaml
volumes:
  - /datamain/docker/appdata/social-downloader:/config
  - /media/social-downloads:/downloads
```

Подготовьте каталоги. Контейнер по умолчанию работает как UID:GID `1000:1000`:

```bash
sudo mkdir -p /datamain/docker/appdata/social-downloader/cookies
sudo mkdir -p /media/social-downloads
sudo chown -R 1000:1000 /datamain/docker/appdata/social-downloader /media/social-downloads
sudo chmod 700 /datamain/docker/appdata/social-downloader/cookies
```

Запуск:

```bash
docker compose pull
docker compose up -d
docker compose logs -f --tail=100 social-downloader-bot
```

`ports:` не требуется: бот использует исходящее соединение Telegram long polling.

## Обновление

GitHub Actions пересобирает образ:

- при каждом push в `main`;
- вручную через `workflow_dispatch`;
- каждое воскресенье для получения свежих `yt-dlp` и `gallery-dl`;
- после Dependabot PR с обновлением зависимостей.

Обновление сервера:

```bash
docker compose pull social-downloader-bot
docker compose up -d social-downloader-bot
docker image prune -f
```

Проверка:

```bash
docker inspect --format '{{.State.Health.Status}}' social-downloader-bot
docker compose logs --tail=150 social-downloader-bot
```

## Команды Telegram

```text
/status        статистика очереди, отправка файлов и свободное место
/queue         ожидающие и выполняемые задания
/history       последние успешные загрузки
/failed        последние ошибки
/retry 12      повторить ошибочное/отменённое задание №12
/cancel 12     отменить ожидающее или остановить выполняемое задание №12
/files_on      включить отправку файлов в этот чат
/files_off     выключить отправку файлов в этот чат
/files_status  состояние и максимальный размер отправки
/version       версии приложения, yt-dlp, gallery-dl, ffmpeg и Deno
/help          краткая инструкция
```

Обычная работа не требует команд: отправьте одну или несколько ссылок одним сообщением.

## Постоянные данные

В `/config` создаются:

```text
social-downloader.sqlite3
social-downloader.sqlite3-wal
social-downloader.sqlite3-shm
cookies/
```

SQLite хранит URL, статус, попытки, путь, размер, ошибку и настройку отправки файлов для каждого Telegram-чата. После перезапуска задания в состояниях `downloading` и `postprocessing` возвращаются в очередь.

Повторно отправленный URL:

- не создаёт второй job, если первый в очереди или выполняется;
- сообщает путь, если уже успешно скачан;
- может быть повторён командой `/retry ID`, если завершился ошибкой или был отменён.

## Ошибки

Когда ни один загрузчик не создал медиафайл, диагностический отчёт сохраняется как:

```text
YYYY-MM/Failed/job-ID.txt
```

В отчёт попадают URL, коды выхода и ограниченный хвост вывода программ. Токен Telegram не передаётся дочерним процессам командной строкой.

## Безопасность контейнера

Compose включает:

- отсутствие входящих портов;
- allow-list по Telegram user ID;
- непривилегированного пользователя `1000:1000`;
- read-only корневую файловую систему;
- `no-new-privileges`;
- удаление всех Linux capabilities;
- отсутствие `/var/run/docker.sock`;
- отдельные writable mounts только для `/config` и `/downloads`;
- временный `/tmp` в `tmpfs`.

Загружайте только контент, к которому у вас есть законный доступ и право хранения.

## Разработка и тесты

```bash
python -m pip install -r requirements-dev.txt
pytest -q
python -m compileall -q app
```

Локальная сборка:

```bash
docker build -t social-downloader-bot:test .
```

## Используемые проекты

- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [gallery-dl](https://github.com/mikf/gallery-dl)
- [FFmpeg](https://ffmpeg.org/)
- [python-telegram-bot](https://docs.python-telegram-bot.org/)
- [Deno](https://deno.com/)
