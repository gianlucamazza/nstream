-- nstream next-episode overlay.
--
-- Loaded by nstream via `--script` (additive — never touches the user's mpv.conf).
-- Near the end of an episode it shows a Netflix-style card with a countdown and
-- lets the user jump to the next episode now (ENTER) or dismiss it (ESC). The
-- decision is handed back to nstream through a tiny signal file, since the Lua
-- script and nstream run in separate processes.
--
-- script-opts (read_options prefix "nstream"):
--   nstream-info=<path>    file whose first line is the next-episode label
--   nstream-signal=<path>  file this script writes "next" to when advancing
--   nstream-lead=<sec>     how many seconds before the end to show the overlay

local options = require 'mp.options'

local opts = { info = "", signal = "", lead = 15 }
options.read_options(opts, "nstream")

local function read_first_line(path)
    if path == "" then return nil end
    local f = io.open(path, "r")
    if not f then return nil end
    local line = f:read("*l")
    f:close()
    return line
end

local next_label = read_first_line(opts.info) or "Prossimo episodio"

-- Escape text for an ASS event, the same way mpv's own bundled scripts do:
-- a trailing zero-width BOM after each backslash stops libass from reading the
-- next characters as an override, and braces are escaped so they render literally.
local function ass_escape(s)
    s = s:gsub("\\", "\\\239\187\191")
    s = s:gsub("{", "\\{")
    s = s:gsub("}", "\\}")
    s = s:gsub("\n", "\\N")
    return s
end

local overlay = mp.create_osd_overlay("ass-events")
local duration = nil
local active = false
local cancelled = false
local triggered = false
local bound = false
local shown_secs = -1

-- Forward declarations so the key-binding callbacks can reference these.
local hide, trigger

-- ENTER/ESC are bound only while the overlay is visible, so we never steal them
-- from the user (or other scripts) during normal playback.
local function bind_keys()
    if bound then return end
    bound = true
    mp.add_forced_key_binding("ENTER", "nstream-next-now", function() trigger() end)
    mp.add_forced_key_binding("ESC", "nstream-next-cancel", function()
        cancelled = true
        hide()
    end)
end

local function unbind_keys()
    if not bound then return end
    bound = false
    mp.remove_key_binding("nstream-next-now")
    mp.remove_key_binding("nstream-next-cancel")
end

hide = function()
    if active then
        active = false
        overlay:remove()
    end
    unbind_keys()
end

trigger = function()
    if triggered then return end
    triggered = true
    unbind_keys()
    if opts.signal ~= "" then
        local f = io.open(opts.signal, "w")
        if f then
            f:write("next\n")
            f:close()
        end
    end
    mp.commandv("quit")
end

local function show(secs)
    active = true
    bind_keys()
    if secs == shown_secs then return end
    shown_secs = secs
    overlay.data = string.format(
        "{\\an3\\bord2\\shad1\\3c&H000000&\\1c&HFFFFFF&\\fs30}\226\150\182 Prossimo episodio\\N"
        .. "{\\fs22\\1c&H9BE6B0&}%s\\N"
        .. "{\\fs20\\1c&HFFFFFF&}tra %d s\\N"
        .. "{\\fs15\\1c&HBFBFBF&}[Enter] ora   [Esc] annulla",
        ass_escape(next_label), secs)
    overlay:update()
end

mp.observe_property("duration", "number", function(_, val)
    duration = val
end)

mp.observe_property("time-pos", "number", function(_, pos)
    if triggered or cancelled or not pos or not duration or duration <= 0 then
        return
    end
    local remaining = duration - pos
    if remaining <= opts.lead then
        show(math.max(0, math.ceil(remaining)))
        if remaining <= 0.5 then
            trigger()
        end
    elseif active and remaining > opts.lead + 5 then
        -- Seeked back out of the window: drop the overlay (and its key bindings).
        shown_secs = -1
        hide()
    end
end)

-- Natural end of file (not a manual quit): advance if the overlay was up.
mp.register_event("end-file", function(ev)
    if ev.reason == "eof" and active and not cancelled then
        trigger()
    end
end)
