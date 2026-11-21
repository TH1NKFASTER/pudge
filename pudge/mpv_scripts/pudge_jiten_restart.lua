-- Pudge: starts JitenMPV again when its program dies while mpv is open.
--
-- JitenMPV's Lua script runs a separate program and, when that program exits
-- with an error, only logs "JitenMPV plugin exited (exit status N); press F10
-- to restart it". On macOS 27 the program sometimes crashes about a second
-- after mpv opens (a fault inside its .NET runtime; mpv reports exit status -1).
-- This script does what F10 does - the plugin's own start binding - once a
-- second after such a crash, at most twice per mpv run. A plugin that could not
-- be started at all, or that macOS blocked, is left alone: restarting it would
-- fail the same way.

local mp = require 'mp'
local utils = require 'mp.utils'

local MAX_RESTARTS = 2
local RESTART_DELAY_SECONDS = 1.0
local START_BINDING = 'jiten-mpv-toggle'

-- Interface texts come from the window's string catalog: cli.py passes the
-- path of pudge/web/locales.<lang>.js in PUDGE_LOCALE_FILE (see
-- pudge_skip_segments.lua). Without it every text falls back to English.
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

local restarts = 0
local pending = false

local function crashed(text)
    if not text:find('JitenMPV plugin exited', 1, true) then return false end
    if not text:find('exit status', 1, true) then return false end -- a normal exit
    if text:find('could not start', 1, true) or text:find('Developer Tools', 1, true) then
        return false
    end
    return true
end

mp.enable_messages('warn')
mp.register_event('log-message', function(event)
    local script = event.prefix or ''
    if script:sub(1, 6) == 'pudge_' or not script:find('jiten', 1, true) then return end
    if not crashed(event.text or '') then return end
    if pending or restarts >= MAX_RESTARTS then return end
    pending = true
    mp.add_timeout(RESTART_DELAY_SECONDS, function()
        pending = false
        restarts = restarts + 1
        mp.msg.info(string.format('restarting JitenMPV (%d of %d)', restarts, MAX_RESTARTS))
        mp.osd_message(tr_key('osd.jitenRestarting', 'JitenMPV crashed, restarting it…'), 4)
        mp.commandv('script-binding', script .. '/' .. START_BINDING)
    end)
end)
