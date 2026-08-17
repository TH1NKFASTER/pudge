from __future__ import annotations

import shlex
import threading
import urllib.parse
import webbrowser
from pathlib import Path

from . import __version__
from .branding import APP_NAME
from .config import load_config, write_config
from .jimaku_trial import apply_jimaku_trial
from .llm import list_models
from .providers.anilist import AniListClient, AniListError
from .tk_utils import SmoothScrollController, enable_edit_shortcuts


NO_MODEL = "Без модели"


def launch_settings(config_path: Path) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:
        raise RuntimeError(
            "Для окна настроек нужен Tkinter. Установите: brew install python-tk@3.12"
        ) from exc

    config = load_config(config_path)
    root = tk.Tk()
    root.title(f"{APP_NAME} — Расширенные настройки")
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    window_width = min(840, max(720, screen_width - 120))
    window_height = min(800, max(620, screen_height - 100))
    window_x = max(20, (screen_width - window_width) // 2)
    # Keep the window near the top of the screen; macOS otherwise may place
    # a tall Tk window too low and hide the action buttons behind the Dock.
    root.geometry(f"{window_width}x{window_height}+{window_x}+20")
    root.minsize(700, 620)
    scroll_controller = SmoothScrollController(root, tk, ttk)

    style = ttk.Style(root)
    try:
        style.theme_use("aqua")
    except tk.TclError:
        pass

    container = ttk.Frame(root, padding=14)
    container.pack(fill="both", expand=True)
    ttk.Label(container, text=APP_NAME, font=("Helvetica Neue", 20, "bold")).pack(anchor="w")
    ttk.Label(container, text=f"Версия: {__version__}").pack(anchor="w")
    ttk.Label(container, text=f"Конфиг: {config_path.expanduser()}").pack(anchor="w", pady=(0, 10))

    notebook = ttk.Notebook(container)
    notebook.pack(fill="both", expand=True)

    tab_canvases: dict[str, object] = {}

    def scrollable_tab(title: str):
        outer = ttk.Frame(notebook)
        notebook.add(outer, text=title)
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        setattr(canvas, "_pudge_page_canvas", True)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, padding=14)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def update_scrollregion(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def fit_width(event) -> None:
            canvas.itemconfigure(window, width=event.width)

        inner.bind("<Configure>", update_scrollregion)
        canvas.bind("<Configure>", fit_width)
        tab_canvases[str(outer)] = canvas
        outer.bind("<Enter>", lambda _e, c=canvas: scroll_controller.set_active_canvas(c), add="+")
        return inner

    llm_tab = scrollable_tab("Модель")
    jimaku_tab = scrollable_tab("Jimaku / AniList")
    search_tab = scrollable_tab("Поиск / тайминг")
    advanced_tab = scrollable_tab("Прочее")

    def activate_selected_tab(_event=None) -> None:
        selected = notebook.select()
        canvas = tab_canvases.get(selected)
        if canvas is not None:
            scroll_controller.set_active_canvas(canvas)

    notebook.bind("<<NotebookTabChanged>>", activate_selected_tab, add="+")
    root.after(0, activate_selected_tab)

    def entry_row(parent, row: int, label: str, variable, *, show: str | None = None, width: int = 48):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        widget = ttk.Entry(parent, textvariable=variable, width=width, show=show)
        widget.grid(row=row, column=1, sticky="ew", pady=5)
        parent.columnconfigure(1, weight=1)
        enable_edit_shortcuts(root, widget, tk)
        return widget

    model_value = config.llm.model if config.llm.enabled and config.llm.model else NO_MODEL
    model_var = tk.StringVar(value=model_value)
    base_url_var = tk.StringVar(value=config.llm.base_url)
    llm_key_var = tk.StringVar(value=config.llm.api_key)
    ambiguity_var = tk.StringVar(value=str(config.llm.ambiguity_margin))
    think_var = tk.BooleanVar(value=config.llm.think)
    keep_alive_var = tk.StringVar(value=config.llm.keep_alive)
    temperature_var = tk.StringVar(value=str(config.llm.temperature))
    num_ctx_var = tk.StringVar(value=str(config.llm.num_ctx))
    timeout_var = tk.StringVar(value=str(config.llm.timeout_seconds))
    validate_reference_var = tk.BooleanVar(value=config.llm.validate_embedded_reference)
    reference_samples_var = tk.StringVar(value=str(config.llm.embedded_reference_sample_count))
    reference_phrases_var = tk.StringVar(value=str(config.llm.embedded_reference_phrases_per_sample))
    reference_similarity_var = tk.StringVar(value=str(config.llm.embedded_reference_min_similarity))

    ttk.Label(llm_tab, text="Модель").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=5)
    model_combo = ttk.Combobox(llm_tab, textvariable=model_var, state="normal", width=46)
    initial_models = [NO_MODEL]
    if config.llm.model:
        initial_models.append(config.llm.model)
    model_combo["values"] = tuple(dict.fromkeys(initial_models))
    model_combo.grid(row=0, column=1, sticky="ew", pady=5)
    enable_edit_shortcuts(root, model_combo, tk)
    llm_tab.columnconfigure(1, weight=1)

    model_status = tk.StringVar(value="Выберите «Без модели», чтобы полностью отключить LLM")

    def refresh_models() -> None:
        model_status.set("Получаю список моделей…")

        def worker() -> None:
            try:
                names = list_models(base_url_var.get().strip(), llm_key_var.get().strip())
                root.after(0, lambda: apply_models(names, None))
            except Exception as exc:  # UI must show connection failures rather than crash
                root.after(0, lambda error=str(exc): apply_models([], error))

        threading.Thread(target=worker, daemon=True).start()

    def apply_models(names: list[str], error: str | None) -> None:
        current = model_var.get().strip()
        values = [NO_MODEL, *names]
        if current and current not in values:
            values.append(current)
        model_combo["values"] = tuple(dict.fromkeys(values))
        if error:
            model_status.set(f"API недоступен: {error}")
        else:
            model_status.set(f"Найдено моделей: {len(names)}")

    ttk.Button(llm_tab, text="Обновить список", command=refresh_models).grid(row=0, column=2, padx=(8, 0))
    entry_row(llm_tab, 1, "URL Ollama API", base_url_var)
    entry_row(llm_tab, 2, "API key модели", llm_key_var, show="•")
    ttk.Label(llm_tab, textvariable=model_status, foreground="#666666").grid(
        row=3, column=0, columnspan=3, sticky="w", pady=(0, 8)
    )
    entry_row(llm_tab, 4, "Порог неоднозначности", ambiguity_var)
    entry_row(llm_tab, 5, "Keep alive", keep_alive_var)
    entry_row(llm_tab, 6, "Контекст (num_ctx)", num_ctx_var)
    entry_row(llm_tab, 7, "Temperature", temperature_var)
    entry_row(llm_tab, 8, "Timeout, секунд", timeout_var)
    ttk.Checkbutton(llm_tab, text="Включить reasoning / think", variable=think_var).grid(
        row=9, column=0, columnspan=2, sticky="w", pady=7
    )
    ttk.Label(
        llm_tab,
        text="Для разбора имён reasoning обычно не нужен. JSON всегда запрашивается через format=json.",
        wraplength=620,
        foreground="#666666",
    ).grid(row=10, column=0, columnspan=3, sticky="w", pady=(4, 8))
    ttk.Separator(llm_tab).grid(row=11, column=0, columnspan=3, sticky="ew", pady=8)
    ttk.Checkbutton(
        llm_tab,
        text="Проверять встроенный английский эталон через LLM",
        variable=validate_reference_var,
    ).grid(row=12, column=0, columnspan=3, sticky="w", pady=5)
    entry_row(llm_tab, 13, "Количество проверяемых участков", reference_samples_var)
    entry_row(llm_tab, 14, "Фраз в каждом участке", reference_phrases_var)
    entry_row(llm_tab, 15, "Минимальное смысловое сходство (0–1)", reference_similarity_var)
    ttk.Label(
        llm_tab,
        text=(
            "Проверка берёт несколько участков по всей серии и сравнивает по несколько "
            "японских и английских реплик. Шумы, звуки, песни и небольшие пропуски допустимы; "
            "при сомнении английская дорожка не используется для ретайминга."
        ),
        wraplength=660,
        foreground="#666666",
    ).grid(row=16, column=0, columnspan=3, sticky="w", pady=(4, 0))

    jimaku_key_var = tk.StringVar(value=config.jimaku.personal_api_key)
    jimaku_url_var = tk.StringVar(value=config.jimaku.base_url)
    anilist_enabled_var = tk.BooleanVar(value=config.anilist.enabled)
    anilist_url_var = tk.StringVar(value=config.anilist.endpoint)
    anilist_client_id_var = tk.StringVar(value=config.anilist.client_id)
    anilist_token_var = tk.StringVar(value=config.anilist.access_token)
    anilist_auto_progress_var = tk.BooleanVar(value=config.anilist.auto_update_progress)
    anilist_threshold_var = tk.StringVar(value=f"{config.anilist.watched_threshold * 100:.1f}")
    anilist_max_remaining_var = tk.StringVar(value=f"{config.anilist.watched_max_remaining_minutes:g}")
    anilist_add_missing_var = tk.BooleanVar(value=config.anilist.add_if_missing)
    anilist_rewatch_var = tk.BooleanVar(value=config.anilist.update_when_rewatching)
    anilist_completed_rewatch_var = tk.BooleanVar(
        value=config.anilist.completed_to_rewatching_on_episode_one
    )
    anilist_complete_final_var = tk.BooleanVar(value=config.anilist.complete_current_final)
    anilist_complete_rewatch_final_var = tk.BooleanVar(
        value=config.anilist.complete_rewatching_final
    )
    anilist_cache_hours_var = tk.StringVar(value=str(config.anilist.mapping_cache_hours))
    anilist_status_var = tk.StringVar(value="")

    entry_row(jimaku_tab, 0, "Jimaku API key", jimaku_key_var, show="•")
    entry_row(jimaku_tab, 1, "Jimaku URL", jimaku_url_var)
    ttk.Checkbutton(jimaku_tab, text="Использовать AniList", variable=anilist_enabled_var).grid(
        row=2, column=0, columnspan=2, sticky="w", pady=7
    )
    entry_row(jimaku_tab, 3, "AniList GraphQL URL", anilist_url_var)
    ttk.Separator(jimaku_tab).grid(row=4, column=0, columnspan=3, sticky="ew", pady=10)
    entry_row(jimaku_tab, 5, "AniList client ID", anilist_client_id_var)
    ttk.Button(
        jimaku_tab,
        text="Создать Client ID",
        command=lambda: webbrowser.open("https://anilist.co/settings/developer"),
    ).grid(row=5, column=2, padx=(8, 0))
    entry_row(jimaku_tab, 6, "AniList access token", anilist_token_var, show="•")

    def open_anilist_auth() -> None:
        client_id = anilist_client_id_var.get().strip()
        if not client_id.isdigit():
            messagebox.showerror("AniList", "Сначала укажите числовой Client ID приложения AniList")
            return
        query = urllib.parse.urlencode({"client_id": client_id, "response_type": "token"})
        webbrowser.open(f"https://anilist.co/api/v2/oauth/authorize?{query}")
        anilist_status_var.set(
            "После подтверждения скопируйте access token со страницы AniList в поле выше"
        )

    def test_anilist_token() -> None:
        token = anilist_token_var.get().strip()
        if not token:
            messagebox.showerror("AniList", "Access token не задан")
            return
        anilist_status_var.set("Проверяю токен…")

        def worker() -> None:
            client = AniListClient(anilist_url_var.get().strip(), access_token=token)
            try:
                viewer = client.viewer()
                text = f"Подключено: {viewer.get('name', viewer.get('id', '-'))}"
                root.after(0, lambda: anilist_status_var.set(text))
            except AniListError as exc:
                root.after(0, lambda error=str(exc): anilist_status_var.set(f"Ошибка: {error}"))
            finally:
                client.close()

        threading.Thread(target=worker, daemon=True).start()

    auth_buttons = ttk.Frame(jimaku_tab)
    auth_buttons.grid(row=7, column=0, columnspan=3, sticky="w", pady=(3, 6))
    ttk.Button(auth_buttons, text="Открыть авторизацию AniList", command=open_anilist_auth).pack(
        side="left"
    )
    ttk.Button(auth_buttons, text="Проверить токен", command=test_anilist_token).pack(
        side="left", padx=(8, 0)
    )
    ttk.Label(jimaku_tab, textvariable=anilist_status_var, foreground="#666666", wraplength=650).grid(
        row=8, column=0, columnspan=3, sticky="w", pady=(0, 8)
    )

    def update_anilist_token_step(*_args) -> None:
        ready = anilist_client_id_var.get().strip().isdigit()
        for widget in jimaku_tab.grid_slaves(row=6):
            (widget.grid if ready else widget.grid_remove)()
        (auth_buttons.grid if ready else auth_buttons.grid_remove)()

    anilist_client_id_var.trace_add("write", update_anilist_token_step)
    update_anilist_token_step()
    ttk.Checkbutton(
        jimaku_tab,
        text="Автоматически засчитывать просмотренную серию в AniList",
        variable=anilist_auto_progress_var,
    ).grid(row=9, column=0, columnspan=3, sticky="w", pady=5)
    entry_row(jimaku_tab, 10, "Засчитывать после просмотра, %", anilist_threshold_var)
    entry_row(jimaku_tab, 11, "Макс. минут до конца", anilist_max_remaining_var)
    ttk.Checkbutton(
        jimaku_tab,
        text="Добавлять аниме в список, если его там нет",
        variable=anilist_add_missing_var,
    ).grid(row=12, column=0, columnspan=3, sticky="w", pady=4)
    ttk.Checkbutton(
        jimaku_tab,
        text="Обновлять прогресс при повторном просмотре",
        variable=anilist_rewatch_var,
    ).grid(row=13, column=0, columnspan=3, sticky="w", pady=4)
    ttk.Checkbutton(
        jimaku_tab,
        text="Серия 1 завершённого аниме начинает повторный просмотр",
        variable=anilist_completed_rewatch_var,
    ).grid(row=14, column=0, columnspan=3, sticky="w", pady=4)
    ttk.Checkbutton(
        jimaku_tab,
        text="Последнюю серию переводить в COMPLETED",
        variable=anilist_complete_final_var,
    ).grid(row=15, column=0, columnspan=3, sticky="w", pady=4)
    ttk.Checkbutton(
        jimaku_tab,
        text="Завершать повторный просмотр на последней серии",
        variable=anilist_complete_rewatch_final_var,
    ).grid(row=16, column=0, columnspan=3, sticky="w", pady=4)
    entry_row(jimaku_tab, 17, "Кэш сопоставления AniList, часов", anilist_cache_hours_var)
    ttk.Label(
        jimaku_tab,
        text=(
            "Прогресс отслеживает Lua-скрипт внутри mpv. Горячие клавиши: "
            "Ctrl+A — засчитать серию, Ctrl+B — открыть AniList, C — исправить AniList ID. "
            "Автодобавление выключено по умолчанию, чтобы не записать ошибочно найденное аниме."
        ),
        foreground="#666666",
        wraplength=650,
    ).grid(row=18, column=0, columnspan=3, sticky="w", pady=(6, 0))

    downloads_var = tk.StringVar(value="; ".join(str(path) for path in config.paths.subtitle_dirs))
    cache_var = tk.StringVar(value=str(config.paths.cache_dir))
    max_files_var = tk.StringVar(value=str(config.paths.max_scanned_files))
    local_score_var = tk.StringVar(value=str(config.matching.local_min_score))
    jimaku_score_var = tk.StringVar(value=str(config.matching.jimaku_min_score))
    prefer_srt_var = tk.BooleanVar(value=config.matching.prefer_srt)
    convert_ass_var = tk.BooleanVar(value=config.matching.convert_ass_to_srt)
    srt_tolerance_ratio_var = tk.StringVar(value=str(config.matching.srt_alignment_tolerance_ratio))
    srt_tolerance_absolute_var = tk.StringVar(value=str(config.matching.srt_alignment_tolerance_absolute))
    evaluate_all_var = tk.BooleanVar(value=config.matching.evaluate_all_jimaku)
    max_candidates_var = tk.StringVar(value=str(config.matching.max_jimaku_candidates))
    ocr_image_subtitles_var = tk.BooleanVar(value=config.matching.ocr_image_subtitles)
    sync_enabled_var = tk.BooleanVar(value=config.sync.enabled)
    sync_engine_var = tk.StringVar(value=config.sync.engine)
    compare_engines_var = tk.BooleanVar(value=config.sync.compare_engines)
    max_offset_var = tk.StringVar(value=str(config.sync.max_offset_seconds))
    quality_offset_var = tk.StringVar(value=str(config.sync.quality_max_offset_seconds))
    skip_low_quality_var = tk.BooleanVar(value=config.sync.skip_on_low_quality)
    vad_var = tk.StringVar(value=config.sync.vad)
    fix_framerate_var = tk.BooleanVar(value=config.sync.fix_framerate)
    gss_var = tk.BooleanVar(value=config.sync.gss)
    alass_split_penalty_var = tk.StringVar(value=str(config.sync.alass_split_penalty))
    alass_timeout_var = tk.StringVar(value=str(config.sync.alass_timeout_seconds))
    segment_validation_var = tk.BooleanVar(value=config.sync.segment_validation)
    segment_count_var = tk.StringVar(value=str(config.sync.segment_count))
    segment_window_var = tk.StringVar(value=str(config.sync.segment_window_seconds))
    segment_max_offset_var = tk.StringVar(value=str(config.sync.segment_max_offset_seconds))
    piecewise_repair_var = tk.BooleanVar(value=config.sync.piecewise_repair)
    piecewise_min_offset_var = tk.StringVar(value=str(config.sync.piecewise_min_offset_seconds))
    piecewise_jump_var = tk.StringVar(value=str(config.sync.piecewise_jump_threshold_seconds))
    piecewise_max_correction_var = tk.StringVar(value=str(config.sync.piecewise_max_correction_seconds))
    pgs_onset_alignment_var = tk.BooleanVar(value=config.sync.pgs_onset_alignment)
    pgs_onset_pulse_var = tk.StringVar(value=str(config.sync.pgs_onset_pulse_seconds))
    pgs_onset_tolerance_var = tk.StringVar(value=str(config.sync.pgs_onset_tolerance_seconds))
    pgs_onset_improvement_var = tk.StringVar(value=str(config.sync.pgs_onset_min_improvement))

    entry_row(search_tab, 0, "Папки внешних субтитров (;, можно пусто)", downloads_var)

    def choose_download_dir() -> None:
        directory = filedialog.askdirectory(initialdir=str(config.paths.subtitle_dirs[0] if config.paths.subtitle_dirs else Path.home() / "Downloads"))
        if directory:
            current = [x.strip() for x in downloads_var.get().split(";") if x.strip()]
            if directory not in current:
                current.append(directory)
            downloads_var.set("; ".join(current))

    ttk.Button(search_tab, text="Добавить папку", command=choose_download_dir).grid(row=0, column=2, padx=(8, 0))
    entry_row(search_tab, 1, "Папка кэша", cache_var)
    entry_row(search_tab, 2, "Максимум файлов", max_files_var)
    entry_row(search_tab, 3, "Минимальный local score", local_score_var)
    entry_row(search_tab, 4, "Минимальный Jimaku score", jimaku_score_var)
    ttk.Checkbutton(search_tab, text="Предпочитать SRT при равном качестве", variable=prefer_srt_var).grid(
        row=5, column=0, columnspan=2, sticky="w", pady=5
    )
    ttk.Checkbutton(search_tab, text="Сравнивать тайминг всех доступных вариантов", variable=evaluate_all_var).grid(
        row=6, column=0, columnspan=2, sticky="w", pady=5
    )
    entry_row(search_tab, 7, "Максимум вариантов Jimaku (0 = все)", max_candidates_var)
    ttk.Checkbutton(
        search_tab,
        text="Использовать OCR для субтитров-картинок",
        variable=ocr_image_subtitles_var,
    ).grid(row=8, column=0, columnspan=2, sticky="w", pady=5)
    ttk.Separator(search_tab).grid(row=9, column=0, columnspan=3, sticky="ew", pady=8)
    ttk.Checkbutton(search_tab, text="Автоматически исправлять тайминг", variable=sync_enabled_var).grid(
        row=10, column=0, columnspan=2, sticky="w", pady=5
    )
    ttk.Label(search_tab, text="Движок тайминга").grid(row=11, column=0, sticky="w", padx=(0, 12), pady=5)
    engine_combo = ttk.Combobox(
        search_tab, textvariable=sync_engine_var, state="readonly",
        values=("auto", "ffsubsync", "alass"), width=46
    )
    engine_combo.grid(row=11, column=1, sticky="ew", pady=5)
    ttk.Checkbutton(search_tab, text="В auto сравнивать ffsubsync и ALASS", variable=compare_engines_var).grid(
        row=12, column=0, columnspan=2, sticky="w", pady=5
    )
    entry_row(search_tab, 13, "Максимальный offset", max_offset_var)
    entry_row(search_tab, 14, "Quality max offset", quality_offset_var)
    ttk.Checkbutton(search_tab, text="Не применять сомнительную синхронизацию", variable=skip_low_quality_var).grid(
        row=15, column=0, columnspan=2, sticky="w", pady=5
    )
    ttk.Label(search_tab, text="VAD").grid(row=16, column=0, sticky="w", padx=(0, 12), pady=5)
    vad_combo = ttk.Combobox(
        search_tab, textvariable=vad_var, state="readonly",
        values=("subs_then_webrtc", "webrtc", "subs_then_auditok", "auditok"), width=46
    )
    vad_combo.grid(row=16, column=1, sticky="ew", pady=5)
    ttk.Checkbutton(search_tab, text="Исправлять несовпадение framerate", variable=fix_framerate_var).grid(
        row=17, column=0, columnspan=2, sticky="w", pady=5
    )
    ttk.Checkbutton(search_tab, text="Точный поиск framerate (GSS, медленнее)", variable=gss_var).grid(
        row=18, column=0, columnspan=2, sticky="w", pady=5
    )
    entry_row(search_tab, 19, "ALASS split penalty (обычно 5–20)", alass_split_penalty_var)
    entry_row(search_tab, 20, "ALASS timeout, секунд", alass_timeout_var)

    mpv_var = tk.StringVar(value=config.tools.mpv)
    ffmpeg_var = tk.StringVar(value=config.tools.ffmpeg)
    ffprobe_var = tk.StringVar(value=config.tools.ffprobe)
    alass_var = tk.StringVar(value=config.tools.alass)
    extra_args_var = tk.StringVar(value=" ".join(shlex.quote(x) for x in config.tools.mpv_extra_args))
    entry_row(advanced_tab, 0, "mpv", mpv_var)
    entry_row(advanced_tab, 1, "ffmpeg", ffmpeg_var)
    entry_row(advanced_tab, 2, "ffprobe", ffprobe_var)
    entry_row(advanced_tab, 3, "alass / alass-cli", alass_var)
    entry_row(advanced_tab, 4, "Доп. аргументы mpv", extra_args_var)
    ttk.Label(
        advanced_tab,
        text="Пример: --profile=anime --fs. Пути с пробелами можно заключать в кавычки.",
        wraplength=620,
        foreground="#666666",
    ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
    ttk.Separator(advanced_tab).grid(row=6, column=0, columnspan=2, sticky="ew", pady=8)
    ttk.Checkbutton(
        advanced_tab, text="Всегда преобразовывать итоговый ASS/SSA в обычный SRT", variable=convert_ass_var
    ).grid(row=7, column=0, columnspan=2, sticky="w", pady=5)
    entry_row(advanced_tab, 8, "Допуск SRT к лучшему score (доля)", srt_tolerance_ratio_var)
    entry_row(advanced_tab, 9, "Минимальный абсолютный допуск SRT", srt_tolerance_absolute_var)
    ttk.Checkbutton(
        advanced_tab, text="Проверять остаточный offset в разных частях серии", variable=segment_validation_var
    ).grid(row=10, column=0, columnspan=2, sticky="w", pady=5)
    entry_row(advanced_tab, 11, "Количество проверочных сегментов", segment_count_var)
    entry_row(advanced_tab, 12, "Длина сегмента, секунд", segment_window_var)
    entry_row(advanced_tab, 13, "Максимальный локальный offset", segment_max_offset_var)
    ttk.Checkbutton(
        advanced_tab, text="Исправлять плавающий offset по сегментам", variable=piecewise_repair_var
    ).grid(row=14, column=0, columnspan=2, sticky="w", pady=5)
    entry_row(advanced_tab, 15, "Минимальный offset для piecewise", piecewise_min_offset_var)
    entry_row(advanced_tab, 16, "Порог резкого скачка offset", piecewise_jump_var)
    entry_row(advanced_tab, 17, "Максимальная локальная поправка", piecewise_max_correction_var)
    ttk.Separator(advanced_tab).grid(row=18, column=0, columnspan=2, sticky="ew", pady=8)
    ttk.Checkbutton(
        advanced_tab,
        text="Ретаймить SUP по миганию картинок и встроенным английским субтитрам",
        variable=pgs_onset_alignment_var,
    ).grid(row=19, column=0, columnspan=2, sticky="w", pady=5)
    entry_row(advanced_tab, 20, "Длина onset-импульса SUP, секунд", pgs_onset_pulse_var)
    entry_row(advanced_tab, 21, "Допуск совпадения onset, секунд", pgs_onset_tolerance_var)
    entry_row(advanced_tab, 22, "Минимальное улучшение onset-score", pgs_onset_improvement_var)

    def parse_float(value: str, label: str) -> float:
        try:
            return float(value.strip())
        except ValueError as exc:
            raise ValueError(f"{label}: нужно число") from exc

    def parse_int(value: str, label: str) -> int:
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{label}: нужно целое число") from exc

    def save(close: bool = False) -> None:
        try:
            model = model_var.get().strip()
            config.llm.enabled = bool(model and model != NO_MODEL)
            if config.llm.enabled:
                config.llm.model = model
            config.llm.base_url = base_url_var.get().strip().rstrip("/")
            config.llm.api_key = llm_key_var.get().strip()
            config.llm.ambiguity_margin = parse_float(ambiguity_var.get(), "Порог неоднозначности")
            config.llm.think = bool(think_var.get())
            config.llm.keep_alive = keep_alive_var.get().strip() or "10m"
            config.llm.temperature = parse_float(temperature_var.get(), "Temperature")
            config.llm.num_ctx = parse_int(num_ctx_var.get(), "num_ctx")
            config.llm.timeout_seconds = parse_float(timeout_var.get(), "Timeout")
            config.llm.validate_embedded_reference = bool(validate_reference_var.get())
            config.llm.embedded_reference_sample_count = parse_int(
                reference_samples_var.get(), "Количество проверяемых участков"
            )
            config.llm.embedded_reference_phrases_per_sample = parse_int(
                reference_phrases_var.get(), "Фраз в каждом участке"
            )
            config.llm.embedded_reference_min_similarity = parse_float(
                reference_similarity_var.get(), "Минимальное смысловое сходство"
            )
            if not 2 <= config.llm.embedded_reference_sample_count <= 20:
                raise ValueError("Количество проверяемых участков должно быть от 2 до 20")
            if not 1 <= config.llm.embedded_reference_phrases_per_sample <= 8:
                raise ValueError("Фраз в каждом участке должно быть от 1 до 8")
            if not 0 <= config.llm.embedded_reference_min_similarity <= 1:
                raise ValueError("Минимальное смысловое сходство должно быть от 0 до 1")

            config.jimaku.personal_api_key = jimaku_key_var.get().strip()
            config.jimaku.api_key = config.jimaku.personal_api_key
            config.jimaku.base_url = jimaku_url_var.get().strip().rstrip("/")
            apply_jimaku_trial(config)
            config.anilist.enabled = bool(anilist_enabled_var.get())
            config.anilist.endpoint = anilist_url_var.get().strip()
            config.anilist.client_id = anilist_client_id_var.get().strip()
            config.anilist.access_token = anilist_token_var.get().strip()
            config.anilist.auto_update_progress = bool(anilist_auto_progress_var.get())
            threshold_percent = parse_float(
                anilist_threshold_var.get(), "Порог просмотра AniList"
            )
            if not 10 <= threshold_percent <= 99:
                raise ValueError("Порог просмотра AniList должен быть от 10 до 99%")
            config.anilist.watched_threshold = threshold_percent / 100
            max_remaining_minutes = parse_float(
                anilist_max_remaining_var.get(), "Макс. минут до конца"
            )
            if not 0 <= max_remaining_minutes <= 180:
                raise ValueError("Макс. минут до конца должно быть от 0 до 180")
            config.anilist.watched_max_remaining_minutes = max_remaining_minutes
            config.anilist.add_if_missing = bool(anilist_add_missing_var.get())
            config.anilist.update_when_rewatching = bool(anilist_rewatch_var.get())
            config.anilist.completed_to_rewatching_on_episode_one = bool(
                anilist_completed_rewatch_var.get()
            )
            config.anilist.complete_current_final = bool(anilist_complete_final_var.get())
            config.anilist.complete_rewatching_final = bool(
                anilist_complete_rewatch_final_var.get()
            )
            config.anilist.mapping_cache_hours = parse_float(
                anilist_cache_hours_var.get(), "Кэш сопоставления AniList"
            )
            if not 1 <= config.anilist.mapping_cache_hours <= 8760:
                raise ValueError("Кэш сопоставления AniList должен быть от 1 до 8760 часов")

            dirs = [Path(x.strip()).expanduser() for x in downloads_var.get().split(";") if x.strip()]
            config.paths.subtitle_dirs = dirs
            config.paths.cache_dir = Path(cache_var.get().strip()).expanduser()
            config.paths.max_scanned_files = parse_int(max_files_var.get(), "Максимум файлов")
            config.matching.local_min_score = parse_float(local_score_var.get(), "Local score")
            config.matching.jimaku_min_score = parse_float(jimaku_score_var.get(), "Jimaku score")
            config.matching.prefer_srt = bool(prefer_srt_var.get())
            config.matching.convert_ass_to_srt = bool(convert_ass_var.get())
            config.matching.srt_alignment_tolerance_ratio = parse_float(
                srt_tolerance_ratio_var.get(), "Допуск SRT"
            )
            config.matching.srt_alignment_tolerance_absolute = parse_float(
                srt_tolerance_absolute_var.get(), "Абсолютный допуск SRT"
            )
            config.matching.evaluate_all_jimaku = bool(evaluate_all_var.get())
            config.matching.max_jimaku_candidates = parse_int(max_candidates_var.get(), "Максимум вариантов Jimaku")
            config.matching.ocr_image_subtitles = bool(ocr_image_subtitles_var.get())

            config.sync.enabled = bool(sync_enabled_var.get())
            config.sync.engine = sync_engine_var.get().strip() or "auto"
            config.sync.compare_engines = bool(compare_engines_var.get())
            config.sync.max_offset_seconds = parse_float(max_offset_var.get(), "Максимальный offset")
            config.sync.quality_max_offset_seconds = parse_float(quality_offset_var.get(), "Quality offset")
            config.sync.skip_on_low_quality = bool(skip_low_quality_var.get())
            config.sync.vad = vad_var.get().strip() or "subs_then_webrtc"
            config.sync.fix_framerate = bool(fix_framerate_var.get())
            config.sync.gss = bool(gss_var.get())
            config.sync.alass_split_penalty = parse_float(alass_split_penalty_var.get(), "ALASS split penalty")
            config.sync.alass_timeout_seconds = parse_float(alass_timeout_var.get(), "ALASS timeout")
            config.sync.segment_validation = bool(segment_validation_var.get())
            config.sync.segment_count = parse_int(segment_count_var.get(), "Количество сегментов")
            config.sync.segment_window_seconds = parse_float(segment_window_var.get(), "Длина сегмента")
            config.sync.segment_max_offset_seconds = parse_float(
                segment_max_offset_var.get(), "Локальный offset"
            )
            config.sync.piecewise_repair = bool(piecewise_repair_var.get())
            config.sync.piecewise_min_offset_seconds = parse_float(
                piecewise_min_offset_var.get(), "Минимальный piecewise offset"
            )
            config.sync.piecewise_jump_threshold_seconds = parse_float(
                piecewise_jump_var.get(), "Порог скачка offset"
            )
            config.sync.piecewise_max_correction_seconds = parse_float(
                piecewise_max_correction_var.get(), "Максимальная локальная поправка"
            )
            config.sync.pgs_onset_alignment = bool(pgs_onset_alignment_var.get())
            config.sync.pgs_onset_pulse_seconds = parse_float(
                pgs_onset_pulse_var.get(), "Длина onset-импульса SUP"
            )
            config.sync.pgs_onset_tolerance_seconds = parse_float(
                pgs_onset_tolerance_var.get(), "Допуск onset SUP"
            )
            config.sync.pgs_onset_min_improvement = parse_float(
                pgs_onset_improvement_var.get(), "Улучшение onset-score SUP"
            )
            if not 0.08 <= config.sync.pgs_onset_pulse_seconds <= 2.0:
                raise ValueError("Длина onset-импульса SUP должна быть от 0.08 до 2 секунд")
            if not 0.1 <= config.sync.pgs_onset_tolerance_seconds <= 3.0:
                raise ValueError("Допуск onset SUP должен быть от 0.1 до 3 секунд")
            if not 0 <= config.sync.pgs_onset_min_improvement <= 1:
                raise ValueError("Улучшение onset-score SUP должно быть от 0 до 1")

            config.tools.mpv = mpv_var.get().strip() or "mpv"
            config.tools.ffmpeg = ffmpeg_var.get().strip() or "ffmpeg"
            config.tools.ffprobe = ffprobe_var.get().strip() or "ffprobe"
            config.tools.alass = alass_var.get().strip() or "alass"
            config.tools.mpv_extra_args = shlex.split(extra_args_var.get())
            write_config(config, config_path)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Не удалось сохранить", str(exc), parent=root)
            return
        if close:
            root.destroy()
        else:
            messagebox.showinfo("Сохранено", f"Настройки записаны в\n{config_path.expanduser()}", parent=root)

    buttons = ttk.Frame(container)
    buttons.pack(fill="x", pady=(12, 0))
    ttk.Button(buttons, text="Закрыть", command=root.destroy).pack(side="right")
    ttk.Button(buttons, text="Сохранить и закрыть", command=lambda: save(True)).pack(side="right", padx=8)
    ttk.Button(buttons, text="Сохранить", command=save).pack(side="right")

    root.update_idletasks()
    root.lift()
    root.after(250, root.focus_force)
    root.mainloop()
    return 0
