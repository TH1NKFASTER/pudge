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
local shortcut_translate_subtitle = os.getenv('PUDGE_SHORTCUT_TRANSLATE_SUBTITLE') or 'Ctrl+t'
local app_name = os.getenv('PUDGE_APP_NAME') or 'pudge'
local subtitle_path = os.getenv('PUDGE_SUBTITLE_PATH') or ''
-- Two episodes in one file (audit 17А): episode one counts where episode two
-- starts ("half" until subtitles have found that point).
local part_boundary_setting = os.getenv('PUDGE_ANILIST_PART_BOUNDARY') or ''
local part_one_done = false
local part_last_position = nil

-- Interface texts come from the window's string catalog: cli.py passes the
-- path of pudge/web/locales.<lang>.js in PUDGE_LOCALE_FILE. The file is strict
-- JSON wrapped in one JavaScript line (between the first '= {' and the last
-- '};'). Without it every text falls back to English.
local function load_locale()
    local path = os.getenv('PUDGE_LOCALE_FILE') or ''
    if path == '' then return {} end
    local handle = io.open(path, 'r')
    if not handle then return {} end
    local content = handle:read('*a') or ''
    handle:close()
    local start = content:find('= {', 1, true)
    local finish, position = nil, 1
    while true do
        local found = content:find('};', position, true)
        if not found then break end
        finish, position = found, found + 1
    end
    if not start or not finish or finish <= start then return {} end
    local data = utils.parse_json(content:sub(start + 2, finish))
    if type(data) ~= 'table' then return {} end
    return data
end

local locale = load_locale()

-- tr_key('osd.someKey', 'English fallback', {name = value}) with {name} fields.
local function tr_key(key, fallback, params)
    local text = locale[key]
    if type(text) ~= 'string' or text == '' then text = fallback end
    if params then
        text = (text:gsub('{([%w_]+)}', function(name)
            local value = params[name]
            if value == nil then return nil end
            return tostring(value)
        end))
    end
    return text
end

local playback_enabled = (os.getenv('PUDGE_PLAYBACK_ENABLED') or '0') == '1'
local playback_video = os.getenv('PUDGE_PLAYBACK_VIDEO') or ''
local playback_interval = tonumber(os.getenv('PUDGE_PLAYBACK_INTERVAL') or '30') or 30
playback_interval = math.max(10, playback_interval)

local triggered = false
-- The automatic update reached a definite answer for this file (counted, or
-- not needed). `reached` remembers that the watched point was passed, so an
-- episode still uncounted when the file ends (the update was running, was
-- deferred, or the ending was skipped straight to the end) is counted then.
local counted = false
local reached = false
local paused = false
local timer = nil
local busy = false
local playback_busy = false
local playback_timer = nil
local active_timer = nil
local last_saved_position = -1
local active_since_save = 0.0
local active_total = 0.0
local save_sequence = 0
local save_session = tostring(utils.getpid()) .. ":" .. tostring(mp.get_time())
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
-- On-demand translations of this episode found in the cache / translated now; the
-- prewarm process reads them (user-data) for its one summary log line.
local translation_hits = 0
local translation_misses = 0

local function reset_translation_counts()
    translation_hits = 0
    translation_misses = 0
    mp.set_property_native('user-data/pudge/translation-hits', 0)
    mp.set_property_native('user-data/pudge/translation-misses', 0)
end

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
        mp.osd_message(tr_key('osd.noTextSubtitleVisible', 'No text subtitle is visible'), 3)
        return
    end
    mp.set_property_bool('pause', true)
    translation_request = translation_request + 1
    local request_id = translation_request
    show_translation(tr_key('reading.translating', 'Translating…'), 30)
    local args = {
        python, '-m', 'pudge.cli',
        '--subtitle-translate',
        '--subtitle-study-text', text,
        '--subtitle-study-context', translation_context(),
    }
    if subtitle_path ~= '' then
        -- A line of the prepared subtitle file: Python builds the same text and
        -- context as the prewarm from the file, so the prepared translation is used.
        table.insert(args, '--subtitle-study-file')
        table.insert(args, subtitle_path)
        table.insert(args, '--subtitle-study-time')
        table.insert(args, string.format('%.3f', mp.get_property_number('time-pos', 0) or 0))
    end
    if media_id ~= '' then
        table.insert(args, '--subtitle-study-media-id')
        table.insert(args, media_id)
    end
    if config_path ~= '' then
        table.insert(args, '--config')
        table.insert(args, config_path)
    end
    local english = mp.get_property('secondary-sub-text', '') or ''
    if subtitle_path ~= '' and english:match('%S') then
        -- One argument: an English line may start with "-" (dialogue dash).
        table.insert(args, '--subtitle-study-secondary=' .. english)
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
            if payload.cached then translation_hits = translation_hits + 1 else translation_misses = translation_misses + 1 end
            mp.set_property_native('user-data/pudge/translation-hits', translation_hits)
            mp.set_property_native('user-data/pudge/translation-misses', translation_misses)
            show_translation(payload.translation, 18)
        else
            show_translation(tr_key('osd.couldNotTranslateSubtitle', 'Could not translate this subtitle'), 5)
            if result and result.stderr and result.stderr ~= '' then
                mp.msg.error(result.stderr)
            end
        end
    end)
end

local function explain_current_subtitle()
    if (os.getenv('PUDGE_LLM_CONFIGURED') or '0') ~= '1' then
        mp.osd_message(tr_key('osd.configureLlm', 'Configure the LLM in Settings'), 4)
        return
    end
    local text = study_current_subtitle ~= '' and study_current_subtitle or (mp.get_property('sub-text', '') or '')
    if not text:match('%S') then mp.osd_message(tr_key('osd.noCurrentSubtitleLine', 'No current subtitle line'), 3); return end
    local payload = utils.format_json({text=text, context=translation_context(), video=playback_video,
                                      position=mp.get_property_number('time-pos', 0) or 0})
    mp.set_property_bool('pause', true)
    mp.command_native_async({name='subprocess', args={python, '-m', 'pudge.mpv_assistant_request', payload},
        capture_stdout=true, capture_stderr=true, playback_only=false}, function(success, result)
        if not success or not result or result.status ~= 0 then
            mp.osd_message(tr_key('osd.couldNotOpenAssistant', 'Could not open subtitle assistant'), 4)
        end
    end)
end
mp.add_key_binding(os.getenv('PUDGE_SHORTCUT_EXPLAIN_SUBTITLE') or 'Ctrl+Shift+t', 'pudge-explain-subtitle', explain_current_subtitle)
-- Closing this companion window ends its owned playback session even when
-- input.conf disables or overrides mpv's default close action.
local function close_playback_window()
    mp.set_property_native('user-data/pudge/closing', true)
    mp.commandv('quit')
end
mp.add_forced_key_binding('CLOSE_WIN', 'pudge-close-window', close_playback_window)
mp.add_forced_key_binding('Meta+w', 'pudge-close-window-shortcut', close_playback_window)

local function start_translation_prewarm()
    if translation_prewarm_started or subtitle_path == '' then return end
    translation_prewarm_started = true
    local args = {
        python, '-m', 'pudge.cli',
        '--subtitle-prewarm-file', subtitle_path,
        '--subtitle-prewarm-from', tostring(mp.get_property_number('time-pos', 0) or 0),
        '--subtitle-prewarm-ipc', mp.get_property('options/input-ipc-server', '') or '',
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
        -- Not killed at the end of the file: the process sees another file / a
        -- closed player itself and logs its summary first.
        playback_only = false,
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
        mp.osd_message(tr_key('osd.anilistUpdateRunning', 'AniList: an update is already running'), 3)
        mp.msg.info('AniList manual update ignored: helper is busy')
        return false
    end
    if tracking_file == '' then
        mp.osd_message(tr_key('osd.anilistTrackingUnavailable', 'AniList: tracking is unavailable for this file'), 5)
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

local function export_episode_debug()
    if playback_video == '' then
        mp.osd_message(tr_key('osd.debugExportUnavailable', 'Debug export is unavailable for this file'), 4)
        return
    end
    mp.osd_message(tr_key('osd.exportingEpisodeLogs', 'Exporting episode logs…'), 2)
    local args = {
        python, '-m', 'pudge.cli',
        '--export-episode-debug',
        '--playback-video', playback_video,
    }
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
        local ok = success and result and result.status == 0
        mp.osd_message(ok and tr_key('osd.episodeLogsExported', 'Episode logs exported; Finder opened')
            or tr_key('osd.couldNotExportEpisodeLogs', 'Could not export episode logs'), 5)
        if not ok and result and result.stderr and result.stderr ~= '' then
            mp.msg.error(result.stderr)
        end
    end)
end

local function accumulate_active_time()
    local now = mp.get_time()
    if last_active_clock == nil then
        last_active_clock = now
        return
    end
    local delta = math.max(0, math.min(5, now - last_active_clock))
    last_active_clock = now
    if not paused then
        active_since_save = active_since_save + delta
        active_total = active_total + delta
    end
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
    save_sequence = save_sequence + 1
    local captured_active = active_since_save
    active_since_save = 0.0
    local args = {
        python, '-m', 'pudge.playback_save',
        '--playback-save',
        '--playback-video', playback_video,
        '--playback-position', string.format('%.3f', position),
        '--playback-duration', string.format('%.3f', duration or 0),
        '--playback-active-seconds', string.format('%.3f', captured_active),
        '--playback-session', save_session,
        '--playback-sequence', tostring(save_sequence),
        '--playback-active-total', string.format('%.3f', active_total),
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
    local fallback_request = request
    local owner_url = os.getenv('PUDGE_ASSISTANT_URL') or ''
    local owner_token = os.getenv('PUDGE_ASSISTANT_TOKEN') or ''
    local use_owner = owner_token ~= '' and owner_url:match('^http://127%.0%.0%.1:%d+/api/mpv/explain$')
    if use_owner then
        request = {
            name='subprocess', capture_stdout=true, capture_stderr=true, playback_only=false,
            args={'/usr/bin/curl','--noproxy','*','--max-time','3','--silent','--show-error','--fail',
                  '--request','POST','--header','Content-Type: application/json','--header','X-Pudge-Token: '..owner_token,
                  '--data-raw',utils.format_json({video=playback_video,position=position,duration=duration or 0,
                     session=save_session,sequence=save_sequence,active_total=active_total}),
                  (owner_url:gsub('/explain$','/playback-save'))}
        }
    end
    local function completed(success,result)
        playback_busy = false
        if not success or not result or result.status ~= 0 then
            active_since_save = active_since_save + captured_active
            mp.msg.warn(app_name .. ' playback position save failed')
            return false
        end
        return true
    end
    if force then
        local result = mp.command_native(request)
        if use_owner and (not result or result.status ~= 0) then result=mp.command_native(fallback_request) end
        return completed(true,result)
    end
    mp.command_native_async(request,function(success,result)
        if use_owner and (not success or not result or result.status ~= 0) then
            -- A lost HTTP reply may follow a committed save. The identical
            -- cumulative marker makes this fallback safe without double time.
            mp.command_native_async(fallback_request,completed)
        else
            completed(success,result)
        end
    end)
end

local function part_boundary()
    if part_boundary_setting == '' then return nil end
    local value = tonumber(part_boundary_setting)
    if value and value > 0 then return value end
    local duration = mp.get_property_number('duration')
    if part_boundary_setting == 'half' and duration and duration > 0 then return duration / 2 end
    return nil
end

local function update_part_one(manual)
    local started = run_helper('update', manual and {'--part', '1', '--manual'} or {'--part', '1'}, function(success, result)
        local stdout = result and result.stdout or ''
        if stdout:find('ANILIST_DEFERRED:1', 1, true) ~= nil then
            -- Not watched enough of episode one: shown once, not retried.
            osd_from_result(result, nil)
            return
        end
        local ok = success and result and result.status == 0
        osd_from_result(result, ok and tr_key('osd.anilistUpdated', 'AniList updated')
            or tr_key('osd.couldNotUpdateAnilist', 'Could not update AniList'))
    end)
    if started then part_one_done = true end
    return started
end

local function check_part_boundary()
    local boundary = part_boundary()
    if not boundary or part_one_done then return false end
    local position = mp.get_property_number('time-pos')
    local previous = part_last_position
    part_last_position = position
    -- Only a crossing during playback counts (also over a skipped ending);
    -- resuming inside episode two does not count episode one.
    if not position or not previous or previous >= boundary or position < boundary then return false end
    if position - boundary > 600 then return false end
    save_playback(true)
    return update_part_one(false)
end

local function update_anilist(manual)
    if manual then
        local boundary = part_boundary()
        local position = mp.get_property_number('time-pos')
        if boundary and position and position < boundary then
            mp.osd_message(tr_key('osd.anilistCountingEpisodeOne', 'AniList: counting episode one…'), 2)
            save_playback(true)
            update_part_one(true)
            return
        end
    end
    if triggered and not manual then return end
    if manual then
        -- Give immediate feedback even before the Python helper starts. This
        -- makes a missing tracker or a subprocess failure visible instead of
        -- making Ctrl+A appear to do nothing.
        mp.osd_message(tr_key('osd.anilistCountingEntry', 'AniList: counting this entry…'), 2)
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
        if ok and not deferred then counted = true end
        osd_from_result(result, ok and tr_key('osd.anilistUpdated', 'AniList updated')
            or tr_key('osd.couldNotUpdateAnilist', 'Could not update AniList'))
        if not ok and result and result.stderr and result.stderr ~= '' then
            mp.msg.error(result.stderr)
        end
    end)
    if not started and not manual then triggered = false end
end

-- The file ends (end of file, quit or next file) after the watched point but
-- the episode is not counted yet: count it in a detached helper, which mpv
-- does not kill when it exits. The helper still requires real watch time.
local function count_on_close(at_eof)
    if not auto_update or tracking_file == '' or counted then return end
    if not reached and not at_eof then return end
    counted = true
    local args = helper_args('update')
    mp.command_native_async({
        name = 'subprocess',
        args = args,
        detach = true,
        playback_only = false,
    }, function() end)
    mp.msg.info('AniList: counting the episode on close')
end

local function check_progress()
    accumulate_active_time()
    if not auto_update or triggered then return end
    if tracking_file == '' or paused or busy then return end
    if check_part_boundary() then return end
    local percent = mp.get_property_number('percent-pos')
    local position = mp.get_property_number('time-pos')
    local duration = mp.get_property_number('duration')
    if not percent or percent < threshold * 100 then return end
    if not position or not duration or duration <= 0 then return end
    local remaining_seconds = math.max(0, duration - position)
    if remaining_seconds <= max_remaining_minutes * 60 then
        reached = true
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
    counted = false
    reached = false
    part_one_done = false
    part_last_position = nil
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
    reset_translation_counts()
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
        -- Low-priority Python work starts after mpv has settled. It keeps the next
        -- few cues after the playback position translated (only while playing),
        -- writes only the normal translation cache and stops with the episode.
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
            mp.osd_message(tr_key('osd.anilistManualMode', 'AniList: manual mode — Ctrl+A to count'), 3)
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

mp.register_event('end-file', function(event)
    clear_translation(true)
    if translation_prewarm_timer then
        translation_prewarm_timer:kill()
        translation_prewarm_timer = nil
    end
    cancel_subtitle_refresh_timers()
    save_playback(true)
    count_on_close(type(event) == 'table' and event.reason == 'eof')
    if active_timer then active_timer:stop() end
    if playback_timer then playback_timer:stop() end
end)
mp.register_event('shutdown', function()
    save_playback(true)
    if active_timer then active_timer:stop() end
end)
-- OP/ED skip (pudge_skip_segments.lua): close the active-time interval at
-- the jump.  Active seconds are wall-clock play time, so the skipped part is
-- never counted as watched; percent/remaining checks stay as they are and the
-- helper still requires real active time before an automatic AniList update.
mp.register_script_message('pudge-segment-skip', function(kind, from, to, reason)
    accumulate_active_time()
    mp.msg.info(string.format('pudge segment skip: %s %s -> %s (%s)',
        tostring(kind), tostring(from), tostring(to), tostring(reason)))
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

if shortcut_translate_subtitle ~= '' then
    add_reliable_binding(shortcut_translate_subtitle, 'pudge_subtitle_translate', translate_visible_subtitle)
end

add_reliable_binding('Meta+Shift+l', 'pudge_episode_debug_export', export_episode_debug)

-- Capture the actual decoder choice once the video is configured.
mp.register_event('video-reconfig', function()
    mp.msg.info('EVENT mpv.decoder hwdec=' .. tostring(mp.get_property('hwdec-current', 'unknown'))
        .. ' codec=' .. tostring(mp.get_property('video-codec', 'unknown')))
end)
