-- Minimal mpv scripting API mock for Pudge's Lua scripts (Lua 5.1–5.4 / LuaJIT).
-- Usage: lua mpv_mock.lua scenario.lua script1.lua [script2.lua ...]
-- The scenario gets a global `sim` and runs after the scripts are loaded.

local unpack = unpack or table.unpack
local props = {}
local observers = {}
local events = {}
local bindings = {}      -- name -> {key=, fn=, forced=, opts=}
local messages = {}
local timers = {}
local clock = 0.0
local log = {}
local overlays = {}

local function record(kind, value) table.insert(log, {kind = kind, value = value}) end

local function notify(name)
    for _, obs in ipairs(observers) do
        if obs.name == name then obs.fn(name, props[name]) end
    end
end

local function new_timer(seconds, fn, periodic)
    local t = {due = clock + seconds, interval = seconds, fn = fn, periodic = periodic, active = true}
    function t:kill() self.active = false end
    function t:stop() self.active = false end
    function t:resume() if not self.active then self.active = true; self.due = clock + self.interval end end
    function t:is_enabled() return self.active end
    table.insert(timers, t)
    return t
end

local mp = {msg = {}}
for _, level in ipairs({'info', 'warn', 'error', 'debug', 'verbose', 'trace'}) do
    mp.msg[level] = function(...) record('msg.' .. level, table.concat({...}, ' ')) end
end

function mp.get_property(name, default)
    local v = props[name]
    if v == nil then return default end
    if type(v) == 'table' then return default end
    return tostring(v)
end
function mp.get_property_number(name, default)
    local v = props[name]
    if type(v) == 'number' then return v end
    return default
end
function mp.get_property_bool(name, default)
    local v = props[name]
    if type(v) == 'boolean' then return v end
    return default
end
function mp.get_property_native(name, default)
    local v = props[name]
    if v == nil then return default end
    return v
end
function mp.set_property(name, value) props[name] = value; notify(name) end
mp.set_property_bool = mp.set_property
mp.set_property_number = mp.set_property
mp.set_property_native = mp.set_property

function mp.observe_property(name, _kind, fn) table.insert(observers, {name = name, fn = fn}) end
function mp.register_event(name, fn)
    events[name] = events[name] or {}
    table.insert(events[name], fn)
end
function mp.register_script_message(name, fn) messages[name] = fn end
function mp.get_time() return clock end
function mp.add_timeout(seconds, fn) return new_timer(seconds, fn, false) end
function mp.add_periodic_timer(seconds, fn) return new_timer(seconds, fn, true) end
function mp.osd_message(text) record('osd', text) end

function mp.add_forced_key_binding(key, name, fn, opts)
    bindings[name or key] = {key = key, fn = fn, forced = true, opts = opts or {}}
end
function mp.add_key_binding(key, name, fn, opts)
    bindings[name or key] = {key = key, fn = fn, forced = false, opts = opts or {}}
end
function mp.remove_key_binding(name) bindings[name] = nil end

function mp.create_osd_overlay(format)
    local o = {format = format, data = '', res_x = 0, res_y = 0, z = 0}
    function o:update() record('overlay', self.data) end
    function o:remove() self.data = '' end
    table.insert(overlays, o)
    return o
end

function mp.commandv(...)
    local args = {...}
    record('command', table.concat(args, ' '))
    if args[1] == 'seek' then
        local target = tonumber(args[2])
        if args[3] and args[3]:find('absolute') then props['time-pos'] = target
        else props['time-pos'] = (props['time-pos'] or 0) + target end
        if props['duration'] then props['percent-pos'] = props['time-pos'] / props['duration'] * 100 end
        notify('time-pos')
    elseif args[1] == 'script-message' then
        local handler = messages[args[2]]
        if handler then handler(select(3, unpack(args))) end
    end
    return true
end
function mp.command_native(tbl)
    record('subprocess', table.concat(tbl.args or {}, ' '))
    return {status = 0, stdout = '', stderr = ''}
end
function mp.command_native_async(tbl, cb)
    if tbl.name == 'subprocess' then record('subprocess', table.concat(tbl.args or {}, ' '))
    else record('command_native', table.concat(tbl, ' ')) end
    if cb then cb(true, {status = 0, stdout = sim_stdout or '', stderr = ''}) end
end

-- tiny JSON decoder (objects, arrays, strings, numbers, true/false/null)
local function decode(str)
    local pos = 1
    local function ws() pos = str:find('[^%s]', pos) or #str + 1 end
    local value
    local function string_()
        local out, i = {}, pos + 1
        while true do
            local c = str:sub(i, i)
            if c == '"' then pos = i + 1; return table.concat(out) end
            if c == '\\' then
                local n = str:sub(i + 1, i + 1)
                if n == 'u' then
                    local code = tonumber(str:sub(i + 2, i + 5), 16)
                    if code < 128 then table.insert(out, string.char(code))
                    elseif code < 2048 then table.insert(out, string.char(192 + math.floor(code / 64), 128 + code % 64))
                    else table.insert(out, string.char(224 + math.floor(code / 4096), 128 + math.floor(code / 64) % 64, 128 + code % 64)) end
                    i = i + 6
                else
                    local map = {n = '\n', t = '\t', r = '\r', b = '\b', f = '\f'}
                    table.insert(out, map[n] or n); i = i + 2
                end
            else table.insert(out, c); i = i + 1 end
        end
    end
    value = function()
        ws()
        local c = str:sub(pos, pos)
        if c == '{' then
            local obj = {}; pos = pos + 1; ws()
            if str:sub(pos, pos) == '}' then pos = pos + 1; return obj end
            while true do
                ws(); local k = string_(); ws(); pos = pos + 1
                obj[k] = value(); ws()
                local d = str:sub(pos, pos); pos = pos + 1
                if d == '}' then return obj end
            end
        elseif c == '[' then
            local arr = {}; pos = pos + 1; ws()
            if str:sub(pos, pos) == ']' then pos = pos + 1; return arr end
            while true do
                table.insert(arr, value()); ws()
                local d = str:sub(pos, pos); pos = pos + 1
                if d == ']' then return arr end
            end
        elseif c == '"' then return string_()
        elseif str:sub(pos, pos + 3) == 'true' then pos = pos + 4; return true
        elseif str:sub(pos, pos + 4) == 'false' then pos = pos + 5; return false
        elseif str:sub(pos, pos + 3) == 'null' then pos = pos + 4; return nil
        else
            local s, e = str:find('^-?[%d%.eE+-]+', pos)
            local n = tonumber(str:sub(s, e)); pos = e + 1; return n
        end
    end
    local ok, result = pcall(value)
    if ok then return result end
    return nil, 'error'
end

local utils = {
    getpid = function() return 4242 end,
    parse_json = decode,
    join_path = function(a, b)
        if b:sub(1, 1) == '/' then return b end
        return (a:gsub('/$', '')) .. '/' .. b
    end,
    format_json = function() return '{}' end,
}

package.preload['mp'] = function() return mp end
package.preload['mp.utils'] = function() return utils end
package.preload['mp.msg'] = function() return mp.msg end

-- ------------------------------------------------------------- sim API ---
sim = {log = log, props = props, bindings = bindings}

function sim.set(name, value) props[name] = value; notify(name) end
function sim.fire(name) for _, fn in ipairs(events[name] or {}) do fn({event = name}) end end
function sim.run_timers()
    for _, t in ipairs(timers) do
        if t.active and t.due <= clock + 1e-9 then
            if t.periodic then t.due = t.due + t.interval else t.active = false end
            t.fn()
        end
    end
end
-- Real playback: wall clock and (unless paused) time-pos advance together.
function sim.play(seconds, step)
    step = step or 0.1
    local n = math.floor(seconds / step + 0.5)
    for _ = 1, n do
        clock = clock + step
        if props['pause'] ~= true then
            props['time-pos'] = (props['time-pos'] or 0) + step
            if props['duration'] then props['percent-pos'] = props['time-pos'] / props['duration'] * 100 end
            notify('time-pos')
        end
        sim.run_timers()
    end
end
function sim.seek(target) mp.commandv('seek', tostring(target), 'absolute+exact') end
function sim.binding_for_key(key)
    for name, b in pairs(bindings) do if b.key == key then return name, b end end
    return nil
end
function sim.press(key)
    local _, b = sim.binding_for_key(key)
    if not b then record('passthrough', key); return false end
    if b.opts and b.opts.complex then b.fn({event = 'down'}); b.fn({event = 'up'}) else b.fn() end
    return true
end
function sim.move(x, y) sim.set('mouse-pos', {x = x, y = y, hover = true}) end
function sim.click(x, y) sim.move(x, y); return sim.press('MBTN_LEFT') end
function sim.overlay()
    local parts = {}
    for _, o in ipairs(overlays) do if o.data ~= '' then table.insert(parts, o.data) end end
    return table.concat(parts, '\n')
end
function sim.commands(pattern)
    local out = {}
    for _, row in ipairs(log) do
        if (row.kind == 'command' or row.kind == 'subprocess') and (not pattern or row.value:find(pattern)) then
            table.insert(out, row.value)
        end
    end
    return out
end
function sim.button()
    -- Button rectangle from the drawn overlay: \pos(x0,y0) + "l w 0 w h".
    local data = sim.overlay()
    local x0, y0 = data:match('\\an7\\pos%((%d+),(%d+)%)')
    local w, h = data:match('m 0 0 l (%d+) 0 %d+ (%d+)')
    if not x0 then return nil end
    return tonumber(x0), tonumber(y0), tonumber(x0) + tonumber(w), tonumber(y0) + tonumber(h)
end
function sim.dump()
    for _, row in ipairs(log) do io.stderr:write(row.kind .. ': ' .. tostring(row.value) .. '\n') end
end

local scenario = arg[1]
for i = 2, #arg do
    local chunk = assert(loadfile(arg[i]))
    chunk()
end
local ok, err = pcall(assert(loadfile(scenario)))
if not ok then
    sim.dump()
    io.stderr:write('SCENARIO_FAILED: ' .. tostring(err) .. '\n')
    os.exit(1)
end
print('SCENARIO_OK')
