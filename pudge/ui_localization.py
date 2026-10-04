"""Language selection and known legacy errors at the presentation boundary.

Error templates preserve interpolated paths, titles and provider responses.
The matching JavaScript catalog is checked against this one by tests.
"""

from __future__ import annotations

import re


def ui_text(language: str, english: str, russian: str) -> str:
    return russian if str(language or "en").lower() == "ru" else english


ERROR_TRANSLATIONS = (
    (
        "Нужен AniList ID или ссылка вида https://anilist.co/anime/12345",
        "An AniList ID or a URL such as https://anilist.co/anime/12345 is required",
    ),
    ("AniList ID должен быть положительным", "AniList ID must be positive"),
    ("Файл субтитров не найден: {0}", "Subtitle file not found: {0}"),
    ("Видео не найдено: {0}", "Video not found: {0}"),
    ("Все torrent backend отключены", "All torrent backends are disabled"),
    ("AniList id={0} отсутствует в локальной базе", "AniList id={0} is missing from the local database"),
    ("Поиск релизов отключён", "Release search is disabled"),
    ("Torrent-загрузки отключены в настройках", "Torrent downloads are disabled in Settings"),
    ("AniList id={0} отсутствует в базе", "AniList id={0} is missing from the database"),
    (
        "{0} принял запрос, но торрент не появился в списке загрузок. Повтори попытку; если ошибка сохранится, открой Diagnostics — там будет причина проверки {1}.",
        "{0} accepted the request, but the torrent did not appear in Downloads. Retry; if the error persists, open Diagnostics for the {1} check details.",
    ),
    ("Не удалось прочитать контейнер через ffprobe: {0}", "Could not read the container with ffprobe: {0}"),
    ("Не найден mpv: {0}", "mpv not found: {0}"),
    ("Ошибка AniList: HTTP {0} для {1}", "AniList error: HTTP {0} for {1}"),
    ("Ошибка AniList: некорректный JSON: {0}", "AniList error: invalid JSON: {0}"),
    ("AniList вернул ошибку: {0}", "AniList returned an error: {0}"),
    ("AniList вернул ответ без data", "AniList returned a response without data"),
    ("Ошибка AniList: {0}", "AniList error: {0}"),
    ("Не удалось определить пользователя AniList", "Could not identify the AniList user"),
    ("Аниме AniList id={0} не найдено", "Anime AniList id={0} was not found"),
    ("AniList не вернул обновлённую запись списка", "AniList did not return the updated list entry"),
    ("Недопустимый статус AniList: {0}", "Invalid AniList status: {0}"),
    ("Оценка AniList должна быть от 1 до 10", "AniList score must be between 1 and 10"),
    ("AniList не вернул сохранённую оценку", "AniList did not return the saved score"),
    ("Номер серии для AniList должен быть положительным", "AniList episode number must be positive"),
    (
        "Встроенный torrent backend не найден: aria2c не установлен. Повторно запусти install.sh или выполни: brew install aria2",
        "The built-in torrent backend was not found: aria2c is not installed. Run install.sh again or use: brew install aria2",
    ),
    ("aria2 RPC недоступен: {0}{1}", "aria2 RPC unavailable: {0}{1}"),
    ("aria2 RPC недоступен: {0}", "aria2 RPC unavailable: {0}"),
    (
        "Kill switch включён, но VPN interface не указан. Укажи активный интерфейс VPN (обычно utun…)",
        "Kill switch is enabled, but the VPN interface is not set. Enter the active VPN interface (usually utun…)",
    ),
    (
        "VPN interface {0} сейчас недоступен; torrent traffic заблокирован",
        "VPN interface {0} is unavailable; torrent traffic is blocked",
    ),
    ("Встроенный aria2 backend отключён", "The built-in aria2 backend is disabled"),
    ("Не удалось запустить aria2c: {0}", "Could not start aria2c: {0}"),
    ("aria2c запущен, но RPC не ответил на порту {0}", "aria2c started, but RPC did not respond on port {0}"),
    ("Не удалось остановить старый Pudge aria2 sidecar", "Could not stop the previous Pudge aria2 sidecar"),
    ("aria2 RPC не запущен", "aria2 RPC is not running"),
    (
        "Релиз не содержит ни magnet-ссылки, ни torrent URL",
        "The release contains neither a magnet link nor a torrent URL",
    ),
    ("Не задан JIMAKU_API_KEY", "JIMAKU_API_KEY is not set"),
    ("Ошибка Jimaku API: {0}", "Jimaku API error: {0}"),
    ("Ошибка Jimaku API: неизвестная сетевая ошибка", "Jimaku API error: unknown network error"),
    ("Не удалось скачать {0}: {1}", "Could not download {0}: {1}"),
    ("Nyaa вернул некорректный RSS: {0}", "Nyaa returned invalid RSS: {0}"),
    (
        "Команда перед поиском Nyaa завершилась с ошибкой: {0}",
        "The command before the Nyaa search failed: {0}",
    ),
    (
        "Для SOCKS-прокси не установлена зависимость socksio. Повторно запустите ./install.sh.",
        "The SOCKS proxy dependency socksio is not installed. Run ./install.sh again.",
    ),
    ("Nyaa недоступен{0}: {1}", "Nyaa unavailable{0}: {1}"),
    (
        "Команда VPN перед скачиванием завершилась с ошибкой: {0}",
        "The VPN command before downloading failed: {0}",
    ),
    ("qBittorrent Web API недоступен: {0}", "qBittorrent Web API unavailable: {0}"),
    ("qBittorrent не принял логин: HTTP {0}", "qBittorrent rejected the login: HTTP {0}"),
    ("qBittorrent не принял логин: {0}", "qBittorrent rejected the login: {0}"),
    ("Не удалось получить версию qBittorrent: {0}", "Could not get the qBittorrent version: {0}"),
    (
        "qBittorrent найден, но API key отклонён. Проверьте, что ключ создан в qBittorrent 5.2 → Settings → Web UI → API keys и вставлен полностью.",
        "qBittorrent was found, but the API key was rejected. Check that the key was created in qBittorrent 5.2 → Settings → Web UI → API keys and pasted in full.",
    ),
    ("Не удалось получить версию qBittorrent: HTTP {0}", "Could not get the qBittorrent version: HTTP {0}"),
    ("Не удалось получить категории qBittorrent: {0}", "Could not get qBittorrent categories: {0}"),
    ("Не удалось получить теги qBittorrent: {0}", "Could not get qBittorrent tags: {0}"),
    ("Не удалось снять теги qBittorrent: {0}", "Could not remove qBittorrent tags from the torrent: {0}"),
    ("Не удалось удалить теги qBittorrent: {0}", "Could not delete qBittorrent tags: {0}"),
    (
        "Не удалось создать категорию qBittorrent '{0}': {1}",
        "Could not create qBittorrent category '{0}': {1}",
    ),
    (
        "Торрент добавлен, но не удалось назначить категорию/теги: {0}",
        "The torrent was added, but its category/tags could not be assigned: {0}",
    ),
    (
        "Не удалось обновить путь торрента qBittorrent: {0}",
        "Could not update the qBittorrent torrent path: {0}",
    ),
    ("Не удалось перепроверить торрент qBittorrent: {0}", "Could not recheck the qBittorrent torrent: {0}"),
    ("Не удалось добавить торрент: {0}", "Could not add the torrent: {0}"),
    (
        "qBittorrent не добавил торрент (409 Conflict). Торрент не найден среди существующих; возможны некорректная magnet-ссылка или недоступная папка загрузки: {0}.{1}",
        "qBittorrent did not add the torrent (409 Conflict). It was not found among existing torrents; the magnet link may be invalid or the download folder unavailable: {0}.{1}",
    ),
    (
        "qBittorrent принял запрос, но не смог добавить торрент (failure_count={0})",
        "qBittorrent accepted the request, but could not add the torrent (failure_count={0})",
    ),
    ("qBittorrent вернул неожиданный ответ: {0}", "qBittorrent returned an unexpected response: {0}"),
    (
        "Не удалось получить metadata торрента через qBittorrent: {0}",
        "Could not get torrent metadata with qBittorrent: {0}",
    ),
    ("Не удалось запустить торрент: {0}", "Could not start the torrent: {0}"),
    ("Не удалось остановить торрент: {0}", "Could not stop the torrent: {0}"),
    ("Не удалось получить загрузки qBittorrent: {0}", "Could not get qBittorrent downloads: {0}"),
    ("Не удалось получить файлы торрента: {0}", "Could not get torrent files: {0}"),
    ("Не удалось выбрать файлы торрента: {0}", "Could not select torrent files: {0}"),
    ("Не удалось удалить торрент: {0}", "Could not delete the torrent: {0}"),
    ("Некорректный timestamp субтитров: {0}", "Invalid subtitle timestamp: {0}"),
    (" напрямую", " directly"),
    (" через {0}", " through {0}"),
    (" Ответ qBittorrent: {0}", " qBittorrent response: {0}"),
    (
        "qBittorrent установлен, но Web UI API не отвечает по адресу {0}. Откройте qBittorrent → Settings → Web UI, включите «Web User Interface (Remote control)» и укажите в {1} тот же порт. API key отвечает только за авторизацию и сам Web UI не запускает. Техническая ошибка: {2}",
        "qBittorrent is installed, but its Web UI API does not respond at {0}. Open qBittorrent → Settings → Web UI, enable “Web User Interface (Remote control)” and set the same port in {1}. The API key only handles authorization and does not start the Web UI. Technical error: {2}",
    ),
    ("Не удалось запустить {0}: {1}", "Could not start {0}: {1}"),
    (
        "AniList сохранил изменение, но не отдал обновлённый список; используются локальные данные",
        "AniList saved the change, but did not return the updated list; using local data",
    ),
    (
        "Jimaku: найден архив .7z/.rar, но 7-Zip не установлен",
        "Jimaku: found a .7z/.rar archive, but 7-Zip is not installed",
    ),
    ("Jimaku: не удалось распаковать {0}", "Jimaku: could not extract {0}"),
    (
        "AniList вернул HTTP 500 на расширенный запрос; использован упрощённый запрос, данные графа оставлены из кэша",
        "AniList returned HTTP 500 for the extended query; used a simplified query and kept cached graph data",
    ),
)


def _compile_template(template: str) -> tuple[re.Pattern[str], list[str]]:
    fields = []
    parts = []
    for part in re.split(r"(\{\d+\})", template):
        if re.fullmatch(r"\{\d+\}", part):
            fields.append(part[1:-1])
            parts.append("(.*?)")
        else:
            parts.append(re.escape(part))
    return re.compile("^" + "".join(parts) + "$", re.DOTALL), fields


_COMPILED = [
    (*_compile_template(ru), en)
    for ru, en in sorted(
        ERROR_TRANSLATIONS, key=lambda pair: len(re.sub(r"\{\d+\}", "", pair[0])), reverse=True
    )
]


def localize_ui_message(message: object, language: str, *, _depth: int = 0) -> str:
    text = str(message or "")
    if str(language or "en").lower() == "ru" or _depth >= 4:
        return text
    prefix = "Error: " if text.startswith("Error: ") else ""
    body = text[len(prefix) :]
    for pattern, fields, english in _COMPILED:
        match = pattern.fullmatch(body)
        if match:
            values = {
                field: localize_ui_message(value, language, _depth=_depth + 1)
                for field, value in zip(fields, match.groups())
            }
            return prefix + re.sub(r"\{(\d+)\}", lambda part, values=values: values[part[1]], english)
    return text
