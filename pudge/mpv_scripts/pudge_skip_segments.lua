-- Pudge: "Skip opening/ending" button for segments detected by Pudge.
--
-- Pudge writes a session JSON (PUDGE_SEGMENTS_FILE) with already detected
-- segments before starting mpv; this script never analyses anything.
-- The button is shown only inside [start, end) and seeks to `end`
-- (absolute+exact), so a scene after the ending credits stays.  Clicks are
-- captured only while the pointer is over the button; the key binding exists
-- only while the button is visible.  Automatic skipping is opt-in and happens
-- once per segment when playback runs into it (never after seeking back).

local mp = require 'mp'
local utils = require 'mp.utils'

local segments_file = os.getenv('PUDGE_SEGMENTS_FILE') or ''
local ui_language = (os.getenv('PUDGE_UI_LANGUAGE') or 'en'):lower()
local skip_key = os.getenv('PUDGE_SHORTCUT_SKIP_SEGMENT') or 'Tab'
local auto_skip = {
    intro = (os.getenv('PUDGE_AUTO_SKIP_INTRO') or '0') == '1',
    outro = (os.getenv('PUDGE_AUTO_SKIP_OUTRO') or '0') == '1',
}

local function tr(english, russian)
    if ui_language == 'ru' then return russian end
    return english
end

local session = nil
local segments = {}
local current = nil
local last_time = nil
local auto_done = {}
local hovered = false
local key_bound = false
local mouse_bound = false
local button = nil -- {x0, y0, x1, y1}
local overlay = mp.create_osd_overlay and mp.create_osd_overlay('ass-events') or nil

local function finite(value)
    return type(value) == 'number' and value == value and value ~= math.huge and value ~= -math.huge
end

local function read_session()
    if segments_file == '' then return nil end
    local handle = io.open(segments_file, 'r')
    if not handle then
        mp.msg.warn('pudge skip: session file is missing')
        return nil
    end
    local content = handle:read('*a')
    handle:close()
    local data = utils.parse_json(content or '')
    if type(data) ~= 'table' or data.version ~= 1 or type(data.segments) ~= 'table' then
        mp.msg.warn('pudge skip: invalid session file')
        return nil
    end
    return data
end

local function same_file(data)
    local path = mp.get_property('path') or ''
    if path == '' then return false end
    if path == data.video or path == data.video_absolute then return true end
    local cwd = mp.get_property('working-directory') or ''
    if cwd ~= '' and utils.join_path(cwd, path) == data.video_absolute then return true end
    return false
end

local function valid_segments(data, duration)
    local result = {}
    for index, row in ipairs(data.segments or {}) do
        local kind, start, finish = row.kind, row.start, row['end']
        local ok = (kind == 'intro' or kind == 'outro')
            and finite(start) and finite(finish)
            and start >= 0 and start < finish
            and (not finite(duration) or duration <= 0 or finish <= duration)
        if ok then
            table.insert(result, {id = index, kind = kind, start = start, finish = finish})
        else
            mp.msg.warn('pudge skip: ignored invalid segment #' .. tostring(index))
        end
    end
    return result
end

local function label(segment)
    if segment.kind == 'intro' then
        return tr('Skip opening', 'Пропустить опенинг')
    end
    return tr('Skip ending', 'Пропустить эндинг')
end

local function ass_escape(value)
    value = tostring(value or '')
    value = value:gsub('\\', '\\h'):gsub('{', '\\{'):gsub('}', '\\}')
    return value
end

local function point_in_button(x, y)
    return button ~= nil and finite(x) and finite(y)
        and x >= button[1] and x <= button[3] and y >= button[2] and y <= button[4]
end

local unbind_mouse, update_hover

local function clear_button()
    button = nil
    if overlay then
        overlay.data = ''
        overlay:update()
    end
    if unbind_mouse then unbind_mouse() end
end

local function draw_button()
    if not current or not overlay then
        clear_button()
        return
    end
    local dims = mp.get_property_native('osd-dimensions') or {}
    local width = tonumber(dims.w) or 0
    local height = tonumber(dims.h) or 0
    if width <= 0 or height <= 0 then return end
    local font = math.max(18, math.floor(height * 0.03))
    local hint = skip_key ~= '' and ('  ' .. skip_key) or ''
    local text = label(current)
    local text_width = math.floor((#text / (ui_language == 'ru' and 1.8 or 1.0) + #hint * 0.8) * font * 0.55)
    local box_w = text_width + font * 2
    local box_h = math.floor(font * 2.1)
    local x1 = math.floor(width - width * 0.03)
    local y1 = math.floor(height * 0.82)
    local x0, y0 = x1 - box_w, y1 - box_h
    button = {x0, y0, x1, y1}
    local box_color = hovered and '&HFFFFFF&' or '&H000000&'
    local box_alpha = hovered and '&H20&' or '&H60&'
    local text_color = hovered and '&H101010&' or '&HFFFFFF&'
    overlay.res_x = width
    overlay.res_y = height
    overlay.z = 20
    overlay.data = string.format(
        '{\\an7\\pos(%d,%d)\\bord1\\3c&HFFFFFF&\\3a&H80&\\shad0\\1c%s\\1a%s\\p1}m 0 0 l %d 0 %d %d 0 %d{\\p0}\n'
            .. '{\\an5\\pos(%d,%d)\\fs%d\\bord0\\shad0\\1c%s\\b1}%s{\\b0\\fs%d\\1a&H40&}%s',
        x0, y0, box_color, box_alpha, box_w, box_w, box_h, box_h,
        math.floor((x0 + x1) / 2), math.floor((y0 + y1) / 2), font, text_color,
        ass_escape(text), math.floor(font * 0.75), ass_escape(hint)
    )
    overlay:update()
end

local skip_current

local function set_key(active)
    if active and not key_bound and skip_key ~= '' then
        key_bound = true
        mp.add_forced_key_binding(skip_key, 'pudge_skip_segment', function() skip_current('key') end)
    elseif not active and key_bound then
        key_bound = false
        mp.remove_key_binding('pudge_skip_segment')
    end
end

skip_current = function(reason)
    local segment = current
    if not segment then return end
    local from = mp.get_property_number('time-pos') or segment.start
    -- Let the tracker close its active-time interval before the jump: a
    -- skip never adds watched seconds (they are wall-clock, not position).
    mp.commandv('script-message', 'pudge-segment-skip', segment.kind,
        string.format('%.3f', from), string.format('%.3f', segment.finish), reason or 'manual')
    auto_done[segment.id] = true
    current = nil
    set_key(false)
    clear_button()
    mp.commandv('seek', string.format('%.3f', segment.finish), 'absolute+exact')
    mp.msg.info(string.format('pudge skip: %s %.1f -> %.1f (%s)', segment.kind, from, segment.finish,
        reason or 'manual'))
end

local function on_click(event)
    local kind = type(event) == 'table' and event.event or 'press'
    if kind ~= 'up' and kind ~= 'press' then return end
    local pos = mp.get_property_native('mouse-pos') or {}
    if point_in_button(tonumber(pos.x), tonumber(pos.y)) then
        skip_current('click')
    end
end

local function bind_mouse()
    if mouse_bound then return end
    mouse_bound = true
    mp.add_forced_key_binding('MBTN_LEFT', 'pudge_skip_click', on_click, {complex = true})
    -- A double click on the button must not toggle fullscreen.
    mp.add_forced_key_binding('MBTN_LEFT_DBL', 'pudge_skip_dbl', function() end)
end

unbind_mouse = function()
    if not mouse_bound then return end
    mouse_bound = false
    mp.remove_key_binding('pudge_skip_click')
    mp.remove_key_binding('pudge_skip_dbl')
    hovered = false
end

update_hover = function(_, pos)
    if type(pos) ~= 'table' then pos = mp.get_property_native('mouse-pos') or {} end
    local inside = current ~= nil and point_in_button(tonumber(pos.x), tonumber(pos.y))
    if inside then bind_mouse() else unbind_mouse() end
    if inside ~= hovered then
        hovered = inside
        draw_button()
    end
end

local function active_segment(t)
    for _, segment in ipairs(segments) do
        if t >= segment.start and t < segment.finish then return segment end
    end
    return nil
end

local function evaluate(_, value)
    local t = tonumber(value)
    if not finite(t) or #segments == 0 then return end
    local segment = active_segment(t)
    if segment ~= current then
        local previous = last_time
        current = segment
        if segment then
            -- Natural entry: playback ran into the segment (or the file
            -- started at its first second), not a seek back into it.
            local natural = (previous ~= nil and previous < segment.start and t - previous < 1.5)
                or (previous == nil and t - segment.start < 1.0)
            if auto_skip[segment.kind] and natural and not auto_done[segment.id] then
                last_time = t
                mp.osd_message(segment.kind == 'intro'
                    and tr('Opening skipped', 'Опенинг пропущен')
                    or tr('Ending skipped', 'Эндинг пропущен'), 2)
                skip_current('auto')
                return
            end
            set_key(true)
            draw_button()
            update_hover()
        else
            set_key(false)
            clear_button()
        end
    end
    last_time = t
end

local function reset()
    current = nil
    last_time = nil
    auto_done = {}
    set_key(false)
    clear_button()
end

mp.register_event('file-loaded', function()
    reset()
    segments = {}
    session = read_session()
    if not session then return end
    if not same_file(session) then
        mp.msg.info('pudge skip: session belongs to another file; disabled')
        return
    end
    segments = valid_segments(session, mp.get_property_number('duration'))
    mp.msg.info(string.format('pudge skip: %d segment(s) loaded', #segments))
    evaluate(nil, mp.get_property_number('time-pos'))
end)

mp.register_event('end-file', function()
    reset()
    segments = {}
end)

mp.observe_property('duration', 'number', function(_, duration)
    if session and #segments > 0 and finite(duration) then
        segments = valid_segments(session, duration)
    end
end)
mp.observe_property('time-pos', 'number', evaluate)
mp.observe_property('osd-dimensions', 'native', function() if current then draw_button() end end)
mp.observe_property('mouse-pos', 'native', update_hover)

-- Exposed for input.conf users: `KEY script-binding pudge_skip_segments/skip`.
mp.add_key_binding(nil, 'skip', function() skip_current('binding') end)
