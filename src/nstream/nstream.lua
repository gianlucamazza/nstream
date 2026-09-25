-- nstream on-screen overlay (single renderer for both in-player cards).
--
-- Loaded by nstream via `--script` (additive — never touches the user's mpv.conf).
-- It draws two things in the same minimal ASS style (bottom-right):
--   * a brief "resume" toast at file start, when resuming mid-playback;
--   * near the end of an episode, a compact next-episode card with a countdown
--     that lets the user jump to the next episode now (ENTER) or dismiss it (ESC).
-- The advance decision is handed back to nstream through a tiny signal file, since
-- the Lua script and nstream run in separate processes.
--
-- script-opts (read_options prefix "nstream"):
--   nstream-info=<path>    file whose first line is the next-episode label
--   nstream-signal=<path>  file this script writes "next"/"cast" to for nstream
--   nstream-lead=<sec>     how many seconds before the end to show the card
--   nstream-resume=<sec>   resume position; when >1 the resume toast is shown
--   nstream-cast=yes       bind Alt+C to hand playback to the TV (re-cast)

local options = require 'mp.options'

local opts = { info = "", signal = "", outcome = "", lead = 15, resume = 0, cast = "" }
options.read_options(opts, "nstream")

-- Keep final media evidence even when mpv closes IPC before its last event is read.
local loaded = false
local function record_outcome(reason)
    if opts.outcome == "" then return end
    local f = io.open(opts.outcome, "w")
    if not f then return end
    f:write((loaded and "1" or "0") .. "\n" .. reason .. "\n")
    f:close()
end
mp.register_event("file-loaded", function()
    loaded = true
    record_outcome("loaded")
end)

local function read_first_line(path)
    if path == "" then return nil end
    local f = io.open(path, "r")
    if not f then return nil end
    local line = f:read("*l")
    f:close()
    return line
end

local next_label = read_first_line(opts.info) or "Prossimo episodio"

-- Mirror nstream's Python _fmt_time: H:MM:SS, or M:SS under an hour.
local function fmt_time(s)
    s = math.floor(s)
    local h = math.floor(s / 3600)
    local m = math.floor((s % 3600) / 60)
    local sec = s % 60
    if h > 0 then return string.format("%d:%02d:%02d", h, m, sec) end
    return string.format("%d:%02d", m, sec)
end

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

-- Design-system palette, in ASS &HBBGGRR& (note: ASS is BGR, not RGB).
-- Mirrors nstream's ui.py: accent = Netflix red #e50914, bright text, dim grey, amber.
local C_ACCENT = "&H1409E5&" -- #e50914
local C_TEXT = "&HF5F5F5&" -- #f5f5f5
local C_DIM = "&HBFBFBF&"
local C_WARN = "&H0FC4F1&" -- #f1c40f (amber)
local C_BORDER = "&H000000&"

-- Countdown colour shifts as the deadline approaches (white -> amber -> red).
local function count_color(secs)
    if secs <= 2 then return C_ACCENT end
    if secs <= 5 then return C_WARN end
    return C_TEXT
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
    -- Compact two-line card: a bold accent title (▶ + episode label) and a countdown
    -- line whose seconds colour-shift as the deadline nears, with key hints.
    -- \226\150\182 = "▶", \194\183 = "·", \226\143\142 = "⏎" (escaped to keep this ASCII-only).
    overlay.data = string.format(
        "{\\an3\\bord2\\shad1\\3c%s\\1c%s\\b1\\fs26}\226\150\182 %s\\N"
        .. "{\\b0\\fs17\\1c%s}tra {\\1c%s}%d s{\\1c%s} \194\183 \226\143\142 ora \194\183 esc",
        C_BORDER, C_ACCENT, ass_escape(next_label),
        C_DIM, count_color(secs), secs, C_DIM)
    overlay:update()
end

-- Resume toast: shown briefly at file start, in the same style as the card.
-- \226\143\181 = "⏵".
mp.register_event("file-loaded", function()
    if not opts.resume or opts.resume <= 1 then return end
    -- \226\143\181 = "⏵" (accent), label in bright text — same chrome as the card.
    overlay.data = string.format(
        "{\\an3\\bord2\\shad1\\3c%s\\fs24\\1c%s}\226\143\181 {\\1c%s}Ripresa da %s",
        C_BORDER, C_ACCENT, C_TEXT, fmt_time(opts.resume))
    overlay:update()
    mp.add_timeout(4, function()
        if not active then overlay:remove() end
    end)
end)

mp.observe_property("duration", "number", function(_, val)
    duration = val
end)

mp.observe_property("time-pos", "number", function(_, pos)
    if opts.info == "" or triggered or cancelled or not pos or not duration or duration <= 0 then
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
    record_outcome(ev.reason or "unknown")
    if ev.reason == "eof" and active and not cancelled then
        trigger()
    end
end)

-- In-player "send to TV": Alt+C signals nstream to re-cast from the current position
-- and quits mpv. Bound for the whole session, only when nstream enabled it (catt found).
if opts.cast == "yes" then
    mp.add_forced_key_binding("alt+c", "nstream-cast", function()
        if opts.signal ~= "" then
            local f = io.open(opts.signal, "w")
            if f then
                f:write("cast\n")
                f:close()
            end
        end
        mp.osd_message("nstream: invio al TV...", 3)
        mp.commandv("quit")
    end)
end
