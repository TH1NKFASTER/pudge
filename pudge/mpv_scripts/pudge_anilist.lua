local mp = require 'mp'
local utils = require 'mp.utils'

local tracking_file = os.getenv('PUDGE_ANILIST_TRACKING_FILE') or ''
local python = os.getenv('PUDGE_PYTHON') or 'python3'
local config_path = os.getenv('PUDGE_CONFIG') or ''
local threshold = tonumber(os.getenv('PUDGE_ANILIST_THRESHOLD') or '0.8333333333') or 0.8333333333
local max_remaining_minutes = tonumber(os.getenv('PUDGE_ANILIST_MAX_REMAINING_MINUTES') or '10') or 10
max_remaining_minutes = math.max(0, max_remaining_minutes)
local media_id = os.getenv('PUDGE_ANILIST_MEDIA_ID') or ''
local title = os.getenv('PUDGE_ANILIST_TITLE') or 'anime'
local auto_update = (os.getenv('PUDGE_ANILIST_AUTO_UPDATE') or '0') == '1'
local shortcut_mark_watched = os.getenv('PUDGE_SHORTCUT_MARK_WATCHED') or 'Ctrl+a'
local shortcut_open_anilist = os.getenv('PUDGE_SHORTCUT_OPEN_ANILIST') or 'Ctrl+b'
local shortcut_correct_match = os.getenv('PUDGE_SHORTCUT_CORRECT_MATCH') or 'c'
local shortcut_translate_subtitle = os.getenv('PUDGE_SHORTCUT_TRANSLATE_SUBTITLE') or 'Ctrl+t'
local ui_language = (os.getenv('PUDGE_UI_LANGUAGE') or 'en'):lower()
local app_name = os.getenv('PUDGE_APP_NAME') or 'pudge'
local app_cli = os.getenv('PUDGE_APP_CLI') or 'pudge'
local subtitle_path = os.getenv('PUDGE_SUBTITLE_PATH') or ''

local function tr(english, russian)
    if ui_language == 'ru' then return russian end
    return english
end

local playback_enabled = (os.getenv('PUDGE_PLAYBACK_ENABLED') or '0') == '1'
local playback_video = os.getenv('PUDGE_PLAYBACK_VIDEO') or ''
local playback_interval = tonumber(os.getenv('PUDGE_PLAYBACK_INTERVAL') or '30') or 30
playback_interval = math.max(10, playback_interval)

local triggered = false
local paused = false
local timer = nil
local busy = false
local playback_busy = false
local playback_timer = nil
local active_timer = nil
local last_saved_position = -1
local active_since_save = 0.0
local last_active_clock = nil
local deferred_until = 0.0


-- Work around an mpv/libass rendering glitch where the first external text
-- cue can remain cached on the video surface after its end. Reloading at cue
-- start is too early: the stale image is created only when the cue should be
-- removed. Instead remember the first cue's real end time and reload the same
-- external text track once just after that boundary. Never switch to another
-- subtitle track, because it may be an embedded bitmap/PGS stream.
local subtitle_refresh_enabled = (os.getenv('PUDGE_SUBTITLE_REFRESH') or '1') ~= '0'
local subtitle_refresh_armed = false
local subtitle_refresh_done = false
local subtitle_refresh_busy = false
local subtitle_refresh_arm_timer = nil
local subtitle_refresh_end_timer = nil
local first_subtitle_text = nil
local first_subtitle_end = nil
local study_current_subtitle = ''
local study_subtitle_history = {}
local translation_overlay = mp.create_osd_overlay and mp.create_osd_overlay('ass-events') or nil
local translation_timer = nil
local translation_request = 0
local translation_prewarm_timer = nil
local translation_prewarm_started = false

local function translation_context()
    local parts = {}
    if #study_subtitle_history > 0 then
        table.insert(parts, 'Previous Japanese subtitles:')
        table.insert(parts, table.concat(study_subtitle_history, '\n'))
    end
    local english = mp.get_property('secondary-sub-text', '') or ''
    if english:match('%S') then
        table.insert(parts, 'Corresponding English subtitle (supporting context only):')
        table.insert(parts, english)
    end
    return table.concat(parts, '\n')
end

local function ass_escape(value)
    value = tostring(value or '')
    value = value:gsub('\\', '\\h')
    value = value:gsub('{', '\\{'):gsub('}', '\\}')
    value = value:gsub('\r?\n', '\\N')
    return value
end

local function clear_translation(invalidate)
    if invalidate then translation_request = translation_request + 1 end
    if translation_timer then
        translation_timer:kill()
        translation_timer = nil
    end
    if translation_overlay then
        translation_overlay.data = ''
        translation_overlay:update()
    end
end

local function show_translation(value, seconds)
    local text = tostring(value or '')
    if text == '' then return end
    if not translation_overlay then
        mp.osd_message(text, seconds or 12)
        return
    end
    local dimensions = mp.get_property_native('osd-dimensions') or {}
    local width = tonumber(dimensions.w) or 1280
    local height = tonumber(dimensions.h) or 720
    translation_overlay.res_x = width
    translation_overlay.res_y = height
    local font_size = math.max(24, math.min(42, math.floor(height * 0.038)))
    local y = math.floor(height * 0.78)
    translation_overlay.data = string.format(
        '{\\an2\\pos(%d,%d)\\q1\\fs%d\\bord3\\shad1\\c&HFFFFFF&\\3c&H101722&}%s',
        math.floor(width / 2), y, font_size, ass_escape(text)
    )
    translation_overlay:update()
    if translation_timer then translation_timer:kill() end
    translation_timer = mp.add_timeout(seconds or 12, function()
        translation_timer = nil
        if translation_overlay then
            translation_overlay.data = ''
            translation_overlay:update()
        end
    end)
end


local function disable_secondary_subtitles()
    mp.set_property('secondary-sid', 'no')
    mp.set_property_bool('secondary-sub-visibility', false)
end

local function selected_subtitle_track()
    local selected_sid = mp.get_property_number('sid')
    if not selected_sid then return nil, nil end
    local tracks = mp.get_property_native('track-list') or {}
    local selected = nil
    for _, track in ipairs(tracks) do
        if track.type == 'sub' and track.id == selected_sid then
            selected = track
            break
        end
    end
    return selected_sid, selected
end

local function cancel_subtitle_refresh_timers()
    if subtitle_refresh_arm_timer then
        subtitle_refresh_arm_timer:kill()
        subtitle_refresh_arm_timer = nil
    end
    if subtitle_refresh_end_timer then
        subtitle_refresh_end_timer:kill()
        subtitle_refresh_end_timer = nil
    end
end

local function finish_subtitle_refresh(previous_visibility)
    mp.add_timeout(0.10, function()
        mp.set_property_bool('sub-visibility', previous_visibility)
        subtitle_refresh_busy = false
    end)
end

local function refresh_after_first_subtitle(reason)
    if not subtitle_refresh_enabled or subtitle_refresh_done or subtitle_refresh_busy then return end

    disable_secondary_subtitles()
    local selected_sid, selected = selected_subtitle_track()
    if not selected_sid or not selected then return end
    -- Limit the workaround to the prepared external text subtitles used by
    -- pudge. Embedded and bitmap tracks must never participate.
    if selected.external ~= true or selected.image == true then
        subtitle_refresh_done = true
        return
    end

    subtitle_refresh_done = true
    subtitle_refresh_busy = true
    if subtitle_refresh_end_timer then
        subtitle_refresh_end_timer:kill()
        subtitle_refresh_end_timer = nil
    end

    local previous_visibility = mp.get_property_bool('sub-visibility', true)
    mp.set_property_bool('sub-visibility', false)

    mp.command_native_async({'sub-reload', tostring(selected_sid)}, function(success)
        if not success then
            mp.msg.warn(app_name .. ' subtitle cleanup: sub-reload failed')
            finish_subtitle_refresh(previous_visibility)
            return
        end

        -- Re-seek only after the first cue has ended. At this point the rebuilt
        -- subtitle surface should be empty (or contain the next real cue), so a
        -- stale image of the first cue cannot be copied back onto the frame.
        local current_time = mp.get_property_number('time-pos', 0) or 0
        mp.commandv('seek', tostring(math.max(0, current_time)), 'absolute+exact')
        mp.msg.info(string.format(
            app_name .. ' subtitle cleanup: reloaded external sid=%s after first cue (%s)',
            tostring(selected_sid),
            tostring(reason or 'unknown')
        ))
        finish_subtitle_refresh(previous_visibility)
    end)
end

local function schedule_first_subtitle_cleanup()
    if subtitle_refresh_done or subtitle_refresh_busy or subtitle_refresh_end_timer then return end
    local cue_end = mp.get_property_number('sub-end/full') or mp.get_property_number('sub-end')
    local current_time = mp.get_property_number('time-pos', 0) or 0
    if not cue_end or cue_end <= current_time then return end

    first_subtitle_end = cue_end
    -- Wait a little beyond the exact SRT boundary so mpv has already changed
    -- its logical subtitle state before the track is rebuilt.
    local delay = math.max(0.02, cue_end - current_time + 0.08)
    subtitle_refresh_end_timer = mp.add_timeout(delay, function()
        subtitle_refresh_end_timer = nil
        refresh_after_first_subtitle('scheduled-end')
    end)
end

local function on_subtitle_text(_, value)
    if type(value) == 'string' and value:match('%S') and value ~= study_current_subtitle then
        clear_translation(true)
        if study_current_subtitle:match('%S') then
            table.insert(study_subtitle_history, study_current_subtitle)
            while #study_subtitle_history > 16 do table.remove(study_subtitle_history, 1) end
        end
        study_current_subtitle = value
    end
    if not subtitle_refresh_armed or subtitle_refresh_done or subtitle_refresh_busy then return end
    if type(value) ~= 'string' or not value:match('%S') then
        -- Fallback for malformed subtitles with no usable end timestamp.
        if first_subtitle_text and not subtitle_refresh_end_timer then
            refresh_after_first_subtitle('text-cleared')
        end
        return
    end

    if not first_subtitle_text then
        first_subtitle_text = value
        schedule_first_subtitle_cleanup()
        return
    end

    -- If the next cue begins immediately and the first cue had no valid end,
    -- rebuild on the text transition instead of leaving the stale first image.
    if value ~= first_subtitle_text and not subtitle_refresh_end_timer then
        refresh_after_first_subtitle('text-changed')
    end
end


local function translate_visible_subtitle()
    local text = mp.get_property('sub-text', '') or ''
    if not text:match('%S') then
        mp.osd_message(tr(
            'No text subtitle is visible',
            'Сейчас нет текстовых субтитров для перевода'
        ), 3)
        return
    end
    mp.set_property_bool('pause', true)
    translation_request = translation_request + 1
    local request_id = translation_request
    show_translation(tr('Translating…', 'Перевожу…'), 30)
    local args = {
        python, '-m', 'pudge.cli',
        '--subtitle-translate',
        '--subtitle-study-text', text,
        '--subtitle-study-context', translation_context(),
    }
    if media_id ~= '' then
        table.insert(args, '--subtitle-study-media-id')
        table.insert(args, media_id)
    end
    if config_path ~= '' then
        table.insert(args, '--config')
        table.insert(args, config_path)
    end
    mp.command_native_async({
        name = 'subprocess',
        args = args,
        capture_stdout = true,
        capture_stderr = true,
        playback_only = false,
    }, function(success, result)
        if request_id ~= translation_request then return end
        local payload = nil
        if success and result and result.status == 0 and result.stdout then
            for line in result.stdout:gmatch('[^\r\n]+') do
                local encoded = line:match('^PUDGE_TRANSLATION_JSON=(.+)$')
                if encoded then
                    payload = utils.parse_json(encoded)
                    break
                end
            end
        end
        if payload and payload.translation and payload.translation ~= '' then
            show_translation(payload.translation, 18)
        else
            show_translation(tr(
                'Could not translate this subtitle',
                'Не удалось перевести этот субтитр'
            ), 5)
            if result and result.stderr and result.stderr ~= '' then
                mp.msg.error(result.stderr)
            end
        end
    end)
end

local function start_translation_prewarm()
    if translation_prewarm_started or subtitle_path == '' then return end
    translation_prewarm_started = true
    local args = {
        python, '-m', 'pudge.cli',
        '--subtitle-prewarm-file', subtitle_path,
        '--subtitle-prewarm-from', tostring(mp.get_property_number('time-pos', 0) or 0),
    }
    if media_id ~= '' then
        table.insert(args, '--subtitle-study-media-id')
        table.insert(args, media_id)
    end
    if config_path ~= '' then
        table.insert(args, '--config')
        table.insert(args, config_path)
    end
    mp.command_native_async({
        name = 'subprocess',
        args = args,
        capture_stdout = true,
        capture_stderr = true,
        playback_only = true,
    }, function(success, result)
        if success and result and result.status == 0 then
            mp.msg.info(app_name .. ' subtitle translation cache prewarm finished')
        elseif result and result.stderr and result.stderr ~= '' then
            mp.msg.warn(app_name .. ' subtitle translation cache prewarm: ' .. result.stderr)
        end
    end)
end

local function osd_from_result(result, fallback)
    local messages = {}
    if result and result.stdout then
        for line in result.stdout:gmatch('[^\r\n]+') do
            local text = line:match('^OSD:%s*(.-)%s*$')
            if text then table.insert(messages, text) end
        end
    end
    if #messages == 0 and fallback then table.insert(messages, fallback) end
    if #messages > 0 then mp.osd_message(table.concat(messages, '\n'), 5) end
end

local function helper_args(action)
    local args = {
        python, '-m', 'pudge.cli',
        '--anilist-action', action,
        '--tracking-file', tracking_file,
    }
    if config_path ~= '' then
        table.insert(args, '--config')
        table.insert(args, config_path)
    end
    return args
end

local function run_helper(action, extra, callback)
    if busy then
        mp.osd_message(tr(
            'AniList: an update is already running',
            'AniList: обновление уже выполняется'
        ), 3)
        mp.msg.info('AniList manual update ignored: helper is busy')
        return false
    end
    if tracking_file == '' then
        mp.osd_message(tr(
            'AniList: tracking is unavailable for this file',
            'AniList: трекер недоступен для этого файла'
        ), 5)
        mp.msg.warn('AniList manual update unavailable: tracking file is empty')
        return false
    end
    busy = true
    local args = helper_args(action)
    if extra then
        for _, value in ipairs(extra) do table.insert(args, value) end
    end
    mp.command_native_async({
        name = 'subprocess',
        args = args,
        capture_stdout = true,
        capture_stderr = true,
        playback_only = false,
    }, function(success, result)
        busy = false
        if callback then callback(success, result) end
    end)
    return true
end

local function accumulate_active_time()
    local now = mp.get_time()
    if last_active_clock == nil then
        last_active_clock = now
        return
    end
    local delta = math.max(0, math.min(5, now - last_active_clock))
    last_active_clock = now
    if not paused then active_since_save = active_since_save + delta end
end

local function save_playback(force)
    if not playback_enabled or playback_video == '' then return end
    if playback_busy and not force then return end
    accumulate_active_time()
    local position = mp.get_property_number('time-pos')
    local duration = mp.get_property_number('duration')
    if not position or position < 0 then return end
    if not force and last_saved_position >= 0 and math.abs(position - last_saved_position) < 5 then return end

    playback_busy = true
    last_saved_position = position
    local captured_active = active_since_save
    active_since_save = 0.0
    local args = {
        python, '-m', 'pudge.cli',
        '--playback-save',
        '--playback-video', playback_video,
        '--playback-position', string.format('%.3f', position),
        '--playback-duration', string.format('%.3f', duration or 0),
        '--playback-active-seconds', string.format('%.3f', captured_active),
    }
    if config_path ~= '' then
        table.insert(args, '--config')
        table.insert(args, config_path)
    end
    local request = {
        name = 'subprocess',
        args = args,
        capture_stdout = true,
        capture_stderr = true,
        playback_only = false,
    }
    if force then
        local result = mp.command_native(request)
        playback_busy = false
        if not result or result.status ~= 0 then
            active_since_save = active_since_save + captured_active
            mp.msg.warn(app_name .. ' playback position save failed')
            return false
        end
        return true
    end
    mp.command_native_async(request, function(success, result)
        playback_busy = false
        if not success or not result or result.status ~= 0 then
            active_since_save = active_since_save + captured_active
            mp.msg.warn(app_name .. ' playback position save failed')
        end
    end)
end

local function update_anilist(manual)
    if triggered and not manual then return end
    if manual then
        -- Give immediate feedback even before the Python helper starts. This
        -- makes a missing tracker or a subprocess failure visible instead of
        -- making Ctrl+A appear to do nothing.
        mp.osd_message(tr(
            'AniList: counting this entry…',
            'AniList: засчитываю серию…'
        ), 2)
        save_playback(true)
    else
        if mp.get_time() < deferred_until then return end
        save_playback(true)
        triggered = true
    end
    local started = run_helper('update', manual and {'--manual'} or nil, function(success, result)
        local stdout = result and result.stdout or ''
        local deferred = stdout:find('ANILIST_DEFERRED:1', 1, true) ~= nil
        if deferred and not manual then
            triggered = false
            deferred_until = mp.get_time() + 30
            osd_from_result(result, nil)
            return
        end
        local ok = success and result and result.status == 0
        osd_from_result(result, ok and tr(
            'AniList updated',
            'AniList обновлён'
        ) or tr(
            'Could not update AniList',
            'Не удалось обновить AniList'
        ))
        if not ok and result and result.stderr and result.stderr ~= '' then
            mp.msg.error(result.stderr)
        end
    end)
    if not started and not manual then triggered = false end
end

local function open_anilist()
    if media_id == '' then return end
    mp.command_native_async({
        name = 'subprocess',
        args = {'open', 'https://anilist.co/anime/' .. media_id},
        playback_only = false,
    }, function() end)
end

local function correct_anilist()
    if tracking_file == '' then return end
    local ok, input = pcall(require, 'mp.input')
    if not ok or not input or not input.get then
        mp.osd_message(tr(
            'Correction: ' .. app_cli .. ' --anilist-correct ID <video>',
            'Исправление: ' .. app_cli .. ' --anilist-correct ID <video>'
        ), 6)
        return
    end
    input.get({
        prompt = tr('AniList ID or URL: ', 'AniList ID или URL: '),
        submit = function(value)
            if not value or value == '' then return end
            run_helper('correct', {'--anilist-id', value}, function(success, result)
                local accepted = success and result and result.status == 0
                osd_from_result(result, accepted and tr(
                    'AniList match corrected',
                    'Сопоставление AniList исправлено'
                ) or tr(
                    'Correction failed',
                    'Ошибка исправления'
                ))
                if accepted and result and result.stdout then
                    local new_id = result.stdout:match('ANILIST_ID:(%d+)')
                    if new_id then media_id = new_id end
                end
            end)
        end,
    })
end

local function check_progress()
    accumulate_active_time()
    if not auto_update or triggered then return end
    if tracking_file == '' or paused or busy then return end
    local percent = mp.get_property_number('percent-pos')
    local position = mp.get_property_number('time-pos')
    local duration = mp.get_property_number('duration')
    if not percent or percent < threshold * 100 then return end
    if not position or not duration or duration <= 0 then return end
    local remaining_seconds = math.max(0, duration - position)
    if remaining_seconds <= max_remaining_minutes * 60 then
        update_anilist(false)
    end
end

local function on_pause(_, value)
    accumulate_active_time()
    paused = value == true
    if timer then
        if paused then timer:stop() else timer:resume() end
    end
end

mp.register_event('file-loaded', function()
    clear_translation(true)
    triggered = false
    paused = mp.get_property_bool('pause', false)
    active_since_save = 0.0
    last_active_clock = mp.get_time()
    deferred_until = 0.0
    subtitle_refresh_armed = false
    subtitle_refresh_done = false
    subtitle_refresh_busy = false
    first_subtitle_text = nil
    first_subtitle_end = nil
    study_current_subtitle = ''
    study_subtitle_history = {}
    translation_prewarm_started = false
    if translation_prewarm_timer then
        translation_prewarm_timer:kill()
        translation_prewarm_timer = nil
    end
    disable_secondary_subtitles()
    cancel_subtitle_refresh_timers()
    if subtitle_refresh_enabled then
        subtitle_refresh_arm_timer = mp.add_timeout(0.05, function()
            subtitle_refresh_arm_timer = nil
            subtitle_refresh_armed = true
            -- The first cue may already be active by the time file-loaded is
            -- delivered, so inspect the current text once after arming.
            on_subtitle_text(nil, mp.get_property('sub-text', ''))
        end)
    end
    if subtitle_path ~= '' then
        -- Low-priority Python work starts after mpv has settled. It walks from
        -- the resume position, writes only the normal translation cache and is
        -- killed automatically with playback.
        translation_prewarm_timer = mp.add_timeout(2.0, function()
            translation_prewarm_timer = nil
            start_translation_prewarm()
        end)
    end

    if tracking_file ~= '' then
        if auto_update then
            if not timer then timer = mp.add_periodic_timer(1.0, check_progress) end
            if paused then timer:stop() else timer:resume() end
        else
            if timer then timer:stop() end
            mp.osd_message(tr(
                'AniList: manual mode — Ctrl+A to count',
                'AniList: ручной режим — Ctrl+A засчитать'
            ), 3)
        end
        mp.msg.info(string.format(
            'AniList tracker loaded: media_id=%s auto=%s threshold=%.1f%% max_remaining=%.1fmin',
            media_id,
            tostring(auto_update),
            threshold * 100,
            max_remaining_minutes
        ))
    end

    if playback_enabled and playback_video ~= '' then
        -- Active watch time must be sampled much more often than the database
        -- save interval. Previously this ran only every 30 seconds while each
        -- sample was capped at 5 seconds, undercounting real viewing by about
        -- six times and preventing automatic completion of short movies.
        if not active_timer then
            active_timer = mp.add_periodic_timer(1.0, accumulate_active_time)
        end
        active_timer:resume()
        if not playback_timer then
            playback_timer = mp.add_periodic_timer(playback_interval, function() save_playback(false) end)
        end
        playback_timer:resume()
    end
end)

mp.register_event('end-file', function()
    clear_translation(true)
    if translation_prewarm_timer then
        translation_prewarm_timer:kill()
        translation_prewarm_timer = nil
    end
    cancel_subtitle_refresh_timers()
    save_playback(true)
    if active_timer then active_timer:stop() end
    if playback_timer then playback_timer:stop() end
end)
mp.register_event('shutdown', function()
    save_playback(true)
    if active_timer then active_timer:stop() end
end)
mp.observe_property('pause', 'bool', on_pause)
mp.observe_property('sub-text', 'string', on_subtitle_text)
local function add_reliable_binding(key, name, callback)
    -- A normal script binding can be shadowed by input.conf or another mpv
    -- script. Manual AniList actions are explicit user commands, so keep them
    -- available with a forced binding. Fall back for older mpv builds.
    if mp.add_forced_key_binding then
        mp.add_forced_key_binding(key, name, callback, {repeatable = false})
    else
        mp.add_key_binding(key, name, callback)
    end
end

if shortcut_mark_watched ~= '' then
    add_reliable_binding(shortcut_mark_watched, 'pudge_anilist_update', function() update_anilist(true) end)
end
if shortcut_open_anilist ~= '' then
    add_reliable_binding(shortcut_open_anilist, 'pudge_anilist_open', open_anilist)
end
if shortcut_correct_match ~= '' then
    mp.add_key_binding(shortcut_correct_match, 'pudge_anilist_correct', correct_anilist)
end

if shortcut_translate_subtitle ~= '' then
    add_reliable_binding(shortcut_translate_subtitle, 'pudge_subtitle_translate', translate_visible_subtitle)
end
