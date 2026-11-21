-- Pudge: subtitle line keys for mpv.
--
--   replay line    seek to the start of the line on screen (between lines: the last one)
--   previous/next  mpv's own sub-seek -1 / +1 on the primary subtitle
--   auto-pause     toggle: pause just before each line ends, so it stays on screen
--
-- The keys come from Settings -> Shortcuts (PUDGE_SHORTCUT_*); an empty value
-- leaves a key unbound. Only the primary subtitle is watched: Pudge shows the
-- prepared Japanese subtitle there and keeps the secondary one off.
--
-- Auto-pause is off at every start and never fights the viewer:
--   * a line is paused for once; resuming plays on,
--   * a seek forgets the line (so a replayed line pauses again at its end),
--   * nothing is paused while the viewer has paused or is seeking,
--   * lines that overlap pause once, when the last of them ends,
--   * the same text shown again after a tiny gap is still the same line.

local mp = require 'mp'
local utils = require 'mp.utils'

local PAUSE_LEAD = 0.1      -- pause this long before the line's end (per 1x speed)
local LATE_WINDOW = 0.3     -- a line end noticed later than this is not paused for
local TINY_GAP = 0.15       -- the same text again within this gap is one line
local NEAR_START = 0.25     -- a line first seen this close to its start calibrates the clock

local function shortcut(name, default)
    local value = os.getenv(name)
    if value == nil then value = default end
    return value
end

local key_replay = shortcut('PUDGE_SHORTCUT_REPLAY_LINE', 'Ctrl+r')
local key_previous = shortcut('PUDGE_SHORTCUT_PREVIOUS_LINE', '')
local key_next = shortcut('PUDGE_SHORTCUT_NEXT_LINE', '')
local key_auto_pause = shortcut('PUDGE_SHORTCUT_AUTO_PAUSE', '')

-- Interface texts: the window's catalog (see pudge_skip_segments.lua).
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

local function tr_key(key, fallback)
    local text = locale[key]
    if type(text) ~= 'string' or text == '' then return fallback end
    return text
end

-- Settings records browser key names; mpv has its own for the named keys.
local MPV_KEY_NAMES = {
    arrowleft = 'LEFT', arrowright = 'RIGHT', arrowup = 'UP', arrowdown = 'DOWN',
    space = 'SPACE', esc = 'ESC', escape = 'ESC', enter = 'ENTER', backspace = 'BS',
    tab = 'TAB', delete = 'DEL', home = 'HOME', ['end'] = 'END',
    pageup = 'PGUP', pagedown = 'PGDWN',
}

local function mpv_key(value)
    local text = tostring(value or ''):gsub('^%s+', ''):gsub('%s+$', '')
    if text == '' then return '' end
    local prefix, last = text:match('^(.*%+)([^+]+)$')
    if not prefix then prefix, last = '', text end
    return prefix .. (MPV_KEY_NAMES[last:lower()] or last)
end

-- The line on screen, or the last one shown (kept for replay until a seek):
-- {raw_start, raw_end, start, finish, text, base_text, showing, joined, paused, replay_start}
local line = nil
local auto_pause = false
local auto_paused = false   -- the current pause is ours
local seeking = false
local time_observed = false
-- sub-start/sub-end are subtitle times; whether they already include sub-delay
-- differs between mpv versions, so it is learned from a line seen at its start.
local times_include_delay = false

local function to_playback(raw)
    if raw == nil then return nil end
    if times_include_delay then return raw end
    return raw + (mp.get_property_number('sub-delay', 0) or 0)
end

local function calibrate(raw_start, pos)
    if raw_start == nil or pos == nil then return end
    local delay = mp.get_property_number('sub-delay', 0) or 0
    if math.abs(delay) < 0.05 then return end
    local with_delay = math.abs(raw_start + delay - pos)
    local without = math.abs(raw_start - pos)
    if math.min(with_delay, without) > NEAR_START then return end -- seen mid-line: no evidence
    times_include_delay = without < with_delay
end

local function pause_for(current)
    current.paused = true
    auto_paused = true
    mp.set_property_bool('pause', true)
end

local function may_pause()
    return auto_pause and not seeking and mp.get_property_bool('pause', false) ~= true
end

local function line_hidden(pos)
    if not line or not line.showing then return end
    line.showing = false
    -- The end was missed by the time check (a dropped frame, no sub-end): pause now.
    if may_pause() and not line.paused and pos ~= nil
        and (line.finish == nil or math.abs(pos - line.finish) <= LATE_WINDOW) then
        pause_for(line)
    end
end

local function refresh()
    local text = mp.get_property('sub-text', '') or ''
    local raw_start = mp.get_property_number('sub-start')
    local raw_end = mp.get_property_number('sub-end')
    local pos = mp.get_property_number('time-pos')
    if text == '' or raw_start == nil then
        line_hidden(pos)
        return
    end
    if line and line.raw_start == raw_start and line.raw_end == raw_end then
        -- The same event: another line joining it (or leaving again) changes only the text.
        line.showing = true
        line.text = text
        line.joined = text ~= line.base_text
        return
    end
    local previous = line
    calibrate(raw_start, pos)
    local start, finish = to_playback(raw_start), to_playback(raw_end)
    local current = {
        raw_start = raw_start, raw_end = raw_end, start = start, finish = finish,
        text = text, base_text = text, showing = true, joined = false, paused = false,
        replay_start = start,
    }
    if previous and previous.finish ~= nil and start ~= nil then
        if previous.showing and start < previous.finish - 0.001 then
            -- Overlapping lines are one group: replay from its first line, pause at its end.
            current.replay_start = math.min(previous.replay_start or start, start)
        elseif not previous.showing and text == previous.text
            and start - previous.finish <= TINY_GAP and start >= previous.finish - 0.001 then
            current.replay_start = previous.replay_start or start
            current.paused = previous.paused
        end
    end
    line = current
end

local function on_time(_, pos)
    if pos == nil or not line or not line.showing or line.paused or line.joined then return end
    if line.finish == nil or not may_pause() then return end
    local lead = PAUSE_LEAD * math.max(1, mp.get_property_number('speed', 1) or 1)
    if pos >= line.finish - lead - 0.001 and pos <= line.finish + LATE_WINDOW then
        pause_for(line)
    end
end

local function watch_time(enabled)
    if enabled and not time_observed then
        mp.observe_property('time-pos', 'number', on_time)
        time_observed = true
    elseif not enabled and time_observed and mp.unobserve_property then
        mp.unobserve_property(on_time)
        time_observed = false
    end
end

local function resume_own_pause()
    if auto_paused then mp.set_property_bool('pause', false) end
end

local function replay_line()
    refresh()
    local target = line and line.replay_start
    if target ~= nil then
        mp.commandv('seek', string.format('%.3f', math.max(0, target)), 'absolute+exact')
    else
        -- No line seen since the last seek: mpv's own "previous subtitle".
        mp.commandv('sub-seek', '-1')
    end
    mp.set_property_bool('pause', false)
end

local function step_line(direction)
    mp.commandv('sub-seek', tostring(direction))
    resume_own_pause()
end

local function toggle_auto_pause()
    auto_pause = not auto_pause
    watch_time(auto_pause)
    if auto_pause then
        -- The line on screen was not chosen for a pause yet: it gets one at its end.
        refresh()
        mp.osd_message(tr_key('osd.autoPauseOn', 'Auto-pause at line end: on'), 2)
    else
        resume_own_pause()
        mp.osd_message(tr_key('osd.autoPauseOff', 'Auto-pause at line end: off'), 2)
    end
end

mp.observe_property('sub-text', 'string', refresh)
mp.observe_property('sub-start', 'number', refresh)
mp.observe_property('sub-end', 'number', refresh)
mp.observe_property('pause', 'bool', function(_, value)
    if value ~= true then auto_paused = false end
end)
-- A seek (the viewer's or a line key's) starts over: the line found after it
-- may be paused for again, and the old line is no longer the one to replay.
mp.register_event('seek', function()
    seeking = true
    line = nil
end)
mp.register_event('playback-restart', function()
    seeking = false
    refresh()
end)
mp.register_event('file-loaded', function()
    seeking = false
    line = nil
    auto_paused = false
end)

local function bind(key, name, callback, repeatable)
    key = mpv_key(key)
    if key == '' then
        -- Still reachable from input.conf: "KEY script-binding pudge_line_keys/<name>".
        mp.add_key_binding(nil, name, callback)
        return
    end
    local add = mp.add_forced_key_binding or mp.add_key_binding
    add(key, name, callback, {repeatable = repeatable == true})
end

bind(key_replay, 'pudge-replay-line', replay_line, false)
bind(key_previous, 'pudge-previous-line', function() step_line(-1) end, true)
bind(key_next, 'pudge-next-line', function() step_line(1) end, true)
bind(key_auto_pause, 'pudge-auto-pause', toggle_auto_pause, false)
