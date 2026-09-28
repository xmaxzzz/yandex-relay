-- ============================================================
-- Yandex Relay  Control4 Driver  v0.2.0
--
-- Plays the music a Yandex Station has started into the Control4 room the
-- station is bound to. The station stays the queue master (via AlexxIT
-- YandexStation in Home Assistant); this driver only plays the URL it is
-- given, through the controller's own digital audio (SELECT_INTERNET_RADIO),
-- and reports panel transport presses back to Home Assistant.
-- Design, protocol and stages: docs/DESIGN.md in the yandex-relay repo.
--
-- Changelog:
--   v0.2.0 - Volume. HA reports the station volume (/station_volume) and
--            Alice's listening state (/duck); the driver maps them onto the
--            room: one Alice step moves the room by a per-room step from its
--            current level (read from the room's volume variable), bigger
--            changes map onto min..max, first value only remembered; ducking
--            lowers (or mutes) the room while Alice talks and restores it.
--            Per-room calibration in Composer: "Volume: Room" selector plus
--            Step/Max/Min/Duck/Duck Level, "Volume: Current", actions
--            "Set Max/Min from Current". /volume for direct level.
--   v0.1.7 - Pause/stop (from HA and from the panel) switch the room off:
--            digital audio's PAUSE on an internet radio queue left the room
--            sounding (site test with the station, 2026-09-28). The OFF that
--            follows our own ROOM_OFF is not echoed to HA.
--   v0.1.6 - driver.xml: Digital Audio / Digital Audio Client classes autobind
--            to Digital Media on add (TuneIn pattern), so no manual
--            connections; unused Audio End-Point (AUDIO_SELECTION) removed.
--   v0.1.5 - Play after Pause resumes in place through the room while digital
--            audio holds the queue in PAUSE (site: Pause via the room gives a
--            real STATE=PAUSE); restarts the track only when the queue is gone.
--            /resume and the play webhook report resume=in_place|restart|none.
--            QUEUE_INFO_CHANGED handled as a no-op.
--   v0.1.4 - Pause/Stop dashboard buttons are ROOM commands (TuneIn pattern):
--            as PROTOCOL they reached only the driver and the stream kept
--            playing with either return value (site test, 2026-09-28). The
--            room's notification (ROOM_ID) is answered handled=false.
--   v0.1.3 - Panel Pause/Stop return nothing instead of handled=false: with
--            handled=false the stream kept playing (site test, 2026-09-28).
--   v0.1.2 - Bridge server is checked after every start and retried every 30 s
--            until it reports ONLINE (site: a re-added instance stayed
--            "starting" because the removed one still held the port).
--   v0.1.1 - Panel Play restarts the room's last track locally instead of only
--            notifying HA (first site test: with no HA answering, Play did
--            nothing). SELECT_SOURCE handled as a no-op.
--   v0.1.0 - Initial stage-1 driver. HTTP bridge (C4:CreateServer) with
--            /info /pair /rooms /play /pause /resume /stop /state, pairing
--            code auth, rooms discovered from C4:GetProjectItems, per-room
--            state keyed by C4 queue id (TuneIn pattern), queue->rooms map
--            from Digital Audio variable 100002/1009, direct URL with
--            fallback URL on early stream failure, panel transport and
--            selection events pushed to the Home Assistant webhook.
-- ============================================================

local DRIVER_VERSION  = "0.2.0"
local PROXY           = 5001
local DIGITAL_AUDIO   = 100002   -- Digital Audio device (same id TuneIn watches)
local DA_ROOM_MAP_VAR = 1009     -- its room<->queue map variable (XML)
local DEFAULT_PORT    = 18765
local LOCATION_ROOM_TYPE = "8"   -- <type> of a room item in GetProjectItems XML
local FALLBACK_WINDOW = 8        -- s: STOP/END this soon after start = stream failed
local META_TICKS      = 8        -- s: re-assert metadata after start (ICY overwrite)
local MAX_REQUEST     = 65536    -- bytes: max HTTP request we accept
local CLOSE_DELAY_MS  = 1500     -- give the client time to read before we close
local SERVER_CHECK_MS = 10000    -- bridge must report ONLINE this soon after start
local SERVER_RETRY_MS = 30000    -- then retry this often until it does
local DUCK_MAX_MS     = 30000    -- a duck HA never releases is lifted after this
-- Per-room volume defaults (Composer "Volume:" properties override them per room).
local VOL_DEFAULTS    = { step = 5, max = 70, min = 5, duck = "Lower", duck_level = 30 }
local VOL_NO_ROOM     = "-"

-- ============================================================
-- BUNDLED JSON LIBRARY (Jeffrey Friedl, public domain)
-- Assigned to global JSON so usage is JSON:decode / JSON:encode
-- ============================================================
do
local COPYRIGHT = '2010-2011 Jeffrey Friedl'
local VERSION = 20111207.5
local OBJDEF = {VERSION = VERSION}

local isArray  = {__tostring = function() return 'JSON array'  end}; isArray.__index  = isArray
local isObject = {__tostring = function() return 'JSON object' end}; isObject.__index = isObject

function OBJDEF:newArray(tbl)  return setmetatable(tbl or {}, isArray)  end
function OBJDEF:newObject(tbl) return setmetatable(tbl or {}, isObject) end

local function unicode_codepoint_as_utf8(codepoint)
    if codepoint <= 127 then return string.char(codepoint)
    elseif codepoint <= 2047 then
        local high = math.floor(codepoint/0x40)
        return string.char(0xC0+high, 0x80+(codepoint-0x40*high))
    elseif codepoint <= 65535 then
        local high = math.floor(codepoint/0x1000)
        local rem  = codepoint - 0x1000*high
        local mid  = math.floor(rem/0x40)
        local low  = rem - 0x40*mid
        high=0xE0+high; mid=0x80+mid; low=0x80+low
        if (high==0xE0 and mid<0xA0) or (high==0xED and mid>0x9F) then return '?' end
        return string.char(high,mid,low)
    else
        local high = math.floor(codepoint/0x40000)
        local rem  = codepoint - 0x40000*high
        local midA = math.floor(rem/0x1000); rem=rem-0x1000*midA
        local midB = math.floor(rem/0x40);   local low=rem-0x40*midB
        return string.char(0xF0+high, 0x80+midA, 0x80+midB, 0x80+low)
    end
end

function OBJDEF:onDecodeError(msg, text, loc, etc)
    if text then msg = (loc and string.format('%s at char %d of: %s',msg,loc,text) or msg..': '..text) end
    print('JSON decode error: '..msg)
end
OBJDEF.onDecodeOfNilError  = OBJDEF.onDecodeError
OBJDEF.onDecodeOfHTMLError = OBJDEF.onDecodeError
function OBJDEF:onEncodeError(msg) print('JSON encode error: '..msg) end

local function grok_number(self,text,start,etc)
    local int = text:match('^-?[1-9]%d*',start) or text:match('^-?0',start)
    if not int then self:onDecodeError('expected number',text,start,etc) end
    local i = start+#int
    local dec = text:match('^%.%d+',i) or ''
    i=i+#dec
    local exp = text:match('^[eE][-+]?%d+',i) or ''
    i=i+#exp
    local n = tonumber(int..dec..exp)
    if not n then self:onDecodeError('bad number',text,start,etc) end
    return n, i
end

local function grok_string(self,text,start,etc)
    if text:sub(start,start)~='"' then self:onDecodeError("expected opening quote",text,start,etc) end
    local i=start+1; local len=#text; local val=''
    while i<=len do
        local c=text:sub(i,i)
        if c=='"' then return val,i+1 end
        if c~='\\' then val=val..c; i=i+1
        elseif text:match('^\\b',i) then val=val..'\b'; i=i+2
        elseif text:match('^\\f',i) then val=val..'\f'; i=i+2
        elseif text:match('^\\n',i) then val=val..'\n'; i=i+2
        elseif text:match('^\\r',i) then val=val..'\r'; i=i+2
        elseif text:match('^\\t',i) then val=val..'\t'; i=i+2
        else
            local hex=text:match('^\\u([0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F])',i)
            if hex then
                i=i+6; local cp=tonumber(hex,16)
                if cp>=0xD800 and cp<=0xDBFF then
                    local lo=text:match('^\\u([dD][cCdDeEfF][0-9a-fA-F][0-9a-fA-F])',i)
                    if lo then i=i+6; cp=0x2400+(cp-0xD800)*0x400+tonumber(lo,16) end
                end
                val=val..unicode_codepoint_as_utf8(cp)
            else val=val..text:match('^\\(.)',i); i=i+2 end
        end
    end
    self:onDecodeError('unclosed string',text,start,etc)
end

local function skip_ws(text,start)
    local _,e=text:find('^[ \n\r\t]+',start); return e and e+1 or start
end

local grok_one
local function grok_object(self,text,start,etc)
    local i=skip_ws(text,start+1); local val=self.strictTypes and self:newObject{} or {}
    if text:sub(i,i)=='}' then return val,i+1 end
    local len=#text
    while i<=len do
        local key,ni=grok_string(self,text,i,etc); i=skip_ws(text,ni)
        if text:sub(i,i)~=':' then self:onDecodeError('expected colon',text,i,etc) end
        i=skip_ws(text,i+1)
        local v,ni2=grok_one(self,text,i); val[key]=v; i=skip_ws(text,ni2)
        local c=text:sub(i,i)
        if c=='}' then return val,i+1 end
        if c~=',' then self:onDecodeError("expected comma or '}'",text,i,etc) end
        i=skip_ws(text,i+1)
    end
    self:onDecodeError("unclosed '{'",text,start,etc)
end

local function grok_array(self,text,start,etc)
    local i=skip_ws(text,start+1); local val=self.strictTypes and self:newArray{} or {}
    if text:sub(i,i)==']' then return val,i+1 end
    local len=#text
    while i<=len do
        local v,ni=grok_one(self,text,i); table.insert(val,v); i=skip_ws(text,ni)
        local c=text:sub(i,i)
        if c==']' then return val,i+1 end
        if c~=',' then self:onDecodeError("expected comma or ']'",text,i,etc) end
        i=skip_ws(text,i+1)
    end
    self:onDecodeError("unclosed '['",text,start,etc)
end

grok_one = function(self,text,start,etc)
    start=skip_ws(text,start)
    if start>#text then self:onDecodeError('unexpected end',text,nil,etc) end
    local c=text:sub(start,start)
    if c=='"' then return grok_string(self,text,start,etc)
    elseif c=='{' then return grok_object(self,text,start,etc)
    elseif c=='[' then return grok_array(self,text,start,etc)
    elseif text:find('^true',start)  then return true, start+4
    elseif text:find('^false',start) then return false,start+5
    elseif text:find('^null',start)  then return nil,  start+4
    elseif text:find('^[-0-9]',start) then return grok_number(self,text,start,etc)
    else self:onDecodeError("can't parse JSON",text,start,etc) end
end

function OBJDEF:decode(text,etc)
    if type(self)~='table' then OBJDEF:onDecodeError('must call as method') end
    if text==nil then self:onDecodeOfNilError('nil passed to decode',nil,nil,etc); return nil end
    if type(text)~='string' then self:onDecodeError('expected string, got '..type(text),nil,nil,etc); return nil end
    if text:match('^%s*$') then return nil end
    if text:match('^%s*<') then self:onDecodeOfHTMLError('html passed to decode',text,nil,etc); return nil end
    local ok,val=pcall(grok_one,self,text,1,etc)
    if ok then return val end
    print('JSON decode panic: '..tostring(val)); return nil
end

local esc_chars='["%\\%z\001-\031]'
local function esc(c)
    local m={['\n']='\\n',['\r']='\\r',['\t']='\\t',['\b']='\\b',['\f']='\\f',['"']='\\"',['\\']='\\\\'}
    return m[c] or string.format('\\u%04x',c:byte())
end
local function json_str(v) return '"'..v:gsub(esc_chars,esc)..'"' end

local function obj_or_arr(self,T,etc)
    local skeys={}; local hasNum=false; local maxN
    for k in pairs(T) do
        if type(k)=='number' then hasNum=true; if not maxN or maxN<k then maxN=k end
        elseif type(k)=='string' then skeys[#skeys+1]=k
        else self:onEncodeError("non-string/number key") end
    end
    if hasNum and #skeys>0 then self:onEncodeError('mixed keys') end
    if #skeys==0 then return nil,maxN else table.sort(skeys); return skeys end
end

local enc_val
enc_val=function(self,v,par,etc)
    if v==nil then return 'null'
    elseif type(v)=='string' then return json_str(v)
    elseif type(v)=='number' then
        if v~=v then return 'null' elseif v>=math.huge then return '1e+9999'
        elseif v<=-math.huge then return '-1e+9999' else return tostring(v):gsub(',','.')  end
    elseif type(v)=='boolean' then return tostring(v)
    elseif type(v)~='table' then self:onEncodeError("can't encode "..type(v)); return 'null'
    else
        if par[v] then self:onEncodeError('circular ref'); return 'null' end
        par[v]=true
        local keys,maxN=obj_or_arr(self,v,etc)
        local r
        if maxN then
            local items={}
            for i=1,maxN do items[#items+1]=enc_val(self,v[i],par,etc) end
            r='['..table.concat(items,',')..']'
        elseif keys then
            local parts={}
            for _,k in ipairs(keys) do
                parts[#parts+1]=json_str(k)..':'..enc_val(self,v[k],par,etc)
            end
            r='{'..table.concat(parts,',')..'}'
        else r='{}' end
        par[v]=false; return r
    end
end

function OBJDEF:encode(v,etc)
    if type(self)~='table' then OBJDEF:onEncodeError('must call as method') end
    return enc_val(self,v,{},etc)
end

function OBJDEF.__tostring() return 'JSON encode/decode package' end
OBJDEF.__index=OBJDEF
function OBJDEF:new(a)
    local n={}; if a then for k,v in pairs(a) do n[k]=v end end
    return setmetatable(n,OBJDEF)
end

-- Assign to global so rest of driver uses JSON:decode / JSON:encode
JSON = OBJDEF:new()
end  -- end JSON do-block

-- ============================================================
-- LOGGING
-- ============================================================
local function Log(msg)
    if Properties["Debug Mode"] == "On" then print("[YandexRelay] " .. tostring(msg)) end
end
local function LogI(msg) print("[YandexRelay] " .. tostring(msg)) end
local function LogE(msg) print("[YandexRelay][ERROR] " .. tostring(msg)) end

-- ============================================================
-- UTILITIES
-- ============================================================
local function XMLEncode(s)
    if s == nil then return "" end
    return (tostring(s):gsub("&", "&amp;"):gsub('"', "&quot;"):gsub("<", "&lt;")
        :gsub(">", "&gt;"):gsub("'", "&apos;"))
end

local function XMLDecode(s)
    if s == nil then return "" end
    return (tostring(s):gsub("&lt;", "<"):gsub("&gt;", ">"):gsub("&quot;", '"')
        :gsub("&apos;", "'"):gsub("&amp;", "&"))
end

local function UrlDecode(s)
    s = tostring(s or ""):gsub("%+", " ")
    return (s:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end))
end

local function ParseQuery(qs)
    local t = {}
    for pair in tostring(qs or ""):gmatch("[^&]+") do
        local k, v = pair:match("^([^=]*)=?(.*)$")
        if k and k ~= "" then t[UrlDecode(k)] = UrlDecode(v) end
    end
    return t
end

local function HostOf(url)
    return tostring(url or ""):match("^%a+://([^/]+)") or ""
end

local CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  -- no 0/O/1/I
local function RandomCode(n)
    local out = {}
    for i = 1, n do
        local k = math.random(1, #CODE_ALPHABET)
        out[i] = CODE_ALPHABET:sub(k, k)
    end
    return table.concat(out)
end

-- Flat <k>v</k> XML used for navigator event args (TuneIn BuildSimpleXml).
local function SimpleXml(t)
    local r = {}
    for k, v in pairs(t or {}) do
        r[#r + 1] = "<" .. k .. ">" .. XMLEncode(tostring(v)) .. "</" .. k .. ">"
    end
    return table.concat(r)
end

local function Contains(list, value)
    for _, v in ipairs(list or {}) do if tonumber(v) == tonumber(value) then return true end end
    return false
end

-- ============================================================
-- STATE
-- ============================================================
-- gRooms[roomId] = {
--   id, state = "idle"|"starting"|"playing"|"paused"|"stopped"|"ended",
--   queueId, track = {url, fallback_url, title, artist, album, image, duration_ms, key},
--   playing_url, fallback_used, started_at, meta_ticks,
--   intent   = nil|"pause"|"stop"   -- a stop we expect (don't treat as failure)
--   suppress = nil|"PAUSE"|"STOP"   -- room command we sent ourselves (don't echo to HA)
-- }
local gRooms       = {}
local gQueueRoom   = {}   -- [queueId] = roomId that started the queue (session source)
local gRoomMap     = {}   -- [queueId] = {roomId, ...} from Digital Audio 1009
local gProjectRooms = {}  -- { {id=, name=}, ... }
local gPairing     = { code = nil, webhook = nil }
local gClients     = {}   -- [handle] = { buf = "", ip = "" }
local gServerPort  = nil
local gMetaTimer   = nil

local function GetRoom(id)
    id = tonumber(id)
    if not id then return nil end
    local r = gRooms[id]
    if not r then
        r = { id = id, state = "idle" }
        gRooms[id] = r
    end
    return r
end

local function RoomsOfQueue(queueId)
    queueId = tonumber(queueId)
    if not queueId then return nil end
    local rooms = gRoomMap[queueId]
    if rooms and #rooms > 0 then return rooms end
    local src = gQueueRoom[queueId]
    if src then return { src } end
    return nil
end

local function CountActiveRooms()
    local n = 0
    for _, r in pairs(gRooms) do
        if r.state == "playing" or r.state == "starting" then n = n + 1 end
    end
    return n
end

-- ============================================================
-- HOME ASSISTANT WEBHOOK (driver -> HA)
-- ============================================================
local function Webhook(evt)
    if not gPairing.webhook or gPairing.webhook == "" then
        Log("webhook skipped (not paired): " .. tostring(evt.event))
        return
    end
    evt.driver_version = DRIVER_VERSION
    local payload = JSON:encode(evt)
    Log("webhook -> " .. payload)
    local t = C4:url()
    t:OnDone(function(transfer, responses, errCode, errMsg)
        if errCode ~= 0 then
            LogE("webhook " .. tostring(evt.event) .. " failed: " .. tostring(errCode) .. " " .. tostring(errMsg))
            return
        end
        local resp = responses and responses[#responses]
        local code = resp and resp.code or 0
        if code < 200 or code >= 300 then
            LogE("webhook " .. tostring(evt.event) .. " HTTP " .. tostring(code))
        end
    end)
    t:Post(gPairing.webhook, payload, { ["Content-Type"] = "application/json" })
end

local function WebhookState(r)
    Webhook({ event = "state", room_id = r.id, state = r.state })
end

-- ============================================================
-- NAVIGATOR / NOW PLAYING (per room, per queue)
-- ============================================================
local function SendEvent(navId, rooms, name, args)
    local p = { NAME = name, EVTARGS = type(args) == "string" and args or SimpleXml(args) }
    if navId then
        p.NAVID = navId
    elseif rooms and #rooms > 0 then
        local ids = {}
        for i, v in ipairs(rooms) do ids[i] = tostring(v) end
        p.ROOMS = table.concat(ids, ",")
    end
    C4:SendToProxy(PROXY, "SEND_EVENT", p, "COMMAND")
end

local function DataReceived(navId, seq, data)
    C4:SendToProxy(PROXY, "DATA_RECEIVED", { NAVID = navId, SEQ = seq, DATA = data or "" })
end

local function DashboardItems(r)
    if not r or not r.track then return "" end
    if r.state == "playing" or r.state == "starting" then return "SkipRev Pause Stop SkipFwd" end
    return "SkipRev Play SkipFwd"
end

-- navId set: answer that navigator only; otherwise every room on the queue.
local function UpdateDashboard(r, navId)
    if not r then return end
    local args = { Items = DashboardItems(r) }
    if r.queueId then args.QueueId = r.queueId end
    SendEvent(navId, RoomsOfQueue(r.queueId) or { r.id }, "DashboardChanged", args)
end

-- One-item "queue" so the Now Playing screen shows title, artist and cover
-- (same XML shape the Yandex Music driver uses).
local function QueueXML(r)
    local t = r.track or {}
    local img = (t.image and t.image ~= "")
        and ('<image_list width="400" height="400">' .. XMLEncode(t.image) .. '</image_list>')
        or '<image_list width="400" height="400"></image_list>'
    local dur = math.floor((tonumber(t.duration_ms) or 0) / 1000)
    local item = "<item><id>0</id><key>0</key>"
        .. "<title>" .. XMLEncode(t.title or "") .. "</title>"
        .. "<subtitle>" .. XMLEncode(t.artist or "") .. "</subtitle>"
        .. img .. "<duration>" .. tostring(dur) .. "</duration>"
        .. "<isHeader>false</isHeader></item>"
    local np = "<can_shuffle>false</can_shuffle><can_repeat>false</can_repeat>"
        .. "<can_repeat_one>false</can_repeat_one>"
        .. "<title>" .. XMLEncode(t.title or "") .. "</title>"
        .. "<subtitle>" .. XMLEncode(t.artist or "") .. "</subtitle>"
    if t.image and t.image ~= "" then
        np = np .. '<image_list width="400" height="400">' .. XMLEncode(t.image) .. '</image_list>'
    end
    return "<List>" .. (r.track and item or "") .. "</List>"
        .. "<NowPlayingIndex>0</NowPlayingIndex><NowPlaying>" .. np .. "</NowPlaying>"
end

local function UpdateQueue(r, navId)
    if not r then return end
    SendEvent(navId, RoomsOfQueue(r.queueId) or { r.id }, "QueueChanged", QueueXML(r))
end

local function UpdateMediaInfo(r)
    if not r or not r.track then return end
    local t = r.track
    C4:SendToProxy(PROXY, "UPDATE_MEDIA_INFO", {
        ROOMID = tostring(r.id), FORCE = "1",
        LINE1 = t.title or "", LINE2 = t.artist or "", LINE3 = t.album or "",
        IMAGEURL = t.image or "",
    }, "COMMAND", true)
    if r.queueId then
        C4:SendToProxy(PROXY, "UPDATE_MEDIA_INFO", {
            QUEUEID = tostring(r.queueId), MERGE = "True", IMAGEURL = t.image or "",
        }, "COMMAND", true)
    end
    -- The Yandex Music driver needed this unbound update for the Now Playing
    -- main screen. It is not room-scoped, so only send it while a single room
    -- is active; otherwise it would overwrite the other rooms' screens.
    if CountActiveRooms() <= 1 then
        C4:SendToProxy(PROXY, "UPDATE_MEDIA_INFO", {
            TITLE = t.title or "", ARTIST = t.artist or "", ALBUM = t.album or "",
            GENRE = "", IMAGEURL = t.image or "",
        }, "COMMAND", true)
    end
end

local function RefreshUI(r)
    UpdateMediaInfo(r)
    UpdateQueue(r)
    UpdateDashboard(r)
end

-- C4 can overwrite our metadata with the stream's ICY data right after start;
-- re-assert it once a second for META_TICKS seconds (Yandex Music driver v1.0.77).
local function StartMetaTimer()
    if gMetaTimer then return end
    gMetaTimer = C4:SetTimer(1000, function(timer)
        local any = false
        for _, r in pairs(gRooms) do
            if (r.meta_ticks or 0) > 0 then
                r.meta_ticks = r.meta_ticks - 1
                UpdateMediaInfo(r)
                any = true
            end
        end
        if not any then timer:Cancel(); gMetaTimer = nil end
    end, true)
end

-- ============================================================
-- PLAYBACK
-- ============================================================
local function SelectStream(r, url)
    r.playing_url = url
    r.started_at = os.time()
    local t = r.track or {}
    local name = (t.title or "")
    if t.artist and t.artist ~= "" then name = name .. " — " .. t.artist end
    LogI("play room=" .. r.id .. " " .. (r.fallback_used and "fallback" or "direct") .. " " .. url:sub(1, 80))
    C4:SendToProxy(PROXY, "SELECT_INTERNET_RADIO", {
        ROOM_ID      = tostring(r.id),
        STATION_URL  = url,
        QUEUE_INFO   = tostring(t.key or ""),
        STATION_NAME = name,
        FLAGS        = "driver=yandex-relay",
    }, "COMMAND")
end

local function PlayInRoom(r, b)
    local url = (b.url and b.url ~= "") and b.url or nil
    local fb  = (b.fallback_url and b.fallback_url ~= "") and b.fallback_url or nil
    r.track = {
        url = url, fallback_url = fb,
        title = b.title or "", artist = b.artist or "", album = b.album or "",
        image = b.image or "", duration_ms = tonumber(b.duration_ms) or 0,
        key = b.key or tostring(os.time()),
    }
    r.fallback_used = (url == nil)
    -- Replacing a live stream: its STOP/END is expected, not a failure.
    r.intent = (r.state == "playing" or r.state == "starting") and "switch" or nil
    r.state = "starting"
    r.meta_ticks = 0
    SelectStream(r, url or fb)
end

-- Ask the room itself to pause/stop/play, so digital audio handles the stream
-- the same way a panel press would. The command comes back to us through the
-- proxy; the suppress flag stops us from echoing it to HA as a user action.
local function RoomCommand(r, cmd, intent)
    r.suppress = cmd
    r.intent = intent
    C4:SendToDevice(r.id, cmd, {})
    C4:SetTimer(3000, function() if r.suppress == cmd then r.suppress = nil end end)
end

-- Pause and stop switch the room off. Digital audio's PAUSE on an internet
-- radio queue only changes the reported state, the room keeps sounding (site
-- 2026-09-28: STATE=PAUSE while the music went on); ROOM_OFF silences it. The
-- queue is gone afterwards, so a later resume restarts the track.
local function RoomOff(r, intent)
    RoomCommand(r, "ROOM_OFF", intent)
end

-- STOP/END right after a start means the stream did not open: retry once with
-- the fallback URL (AlexxIT proxy) before giving up.
local function TryFallback(r)
    if r.fallback_used or not r.track or not r.track.fallback_url then return false end
    if os.time() - (r.started_at or 0) > FALLBACK_WINDOW then return false end
    LogI("room " .. r.id .. ": direct stream failed, switching to fallback URL")
    r.fallback_used = true
    r.state = "starting"
    r.intent = "switch"
    SelectStream(r, r.track.fallback_url)
    return true
end

-- Continue the room's track. A queue digital audio holds in PAUSE resumes in
-- place through the room (site 2026-09-28: Pause via the room gives a real
-- STATE=PAUSE). Once the queue is gone (after Stop) the track restarts.
-- Returns "in_place", "restart" or "none" so HA knows whether to seek the
-- station back to 0.
local function Resume(r)
    if not r.track then return "none" end
    if r.state == "playing" or r.state == "starting" then return "none" end
    if r.state == "paused" and r.queueId then
        RoomCommand(r, "PLAY", nil)
        return "in_place"
    end
    r.state = "starting"
    r.intent = nil
    SelectStream(r, r.playing_url or r.track.url or r.track.fallback_url)
    return "restart"
end

-- ============================================================
-- ROOMS DISCOVERY
-- ============================================================
local UpdateVolumeRoomList   -- volume section, below

local function DiscoverRooms()
    local rooms, types = {}, {}
    local ok, xml = pcall(function()
        return C4:GetProjectItems("LOCATIONS", "LIMIT_DEVICE_DATA", "NO_ROOT_TAGS")
    end)
    if ok and type(xml) == "string" then
        for id, name, typ in xml:gmatch("<item>%s*<id>(%d+)</id>%s*<name>(.-)</name>%s*<type>(%d+)</type>") do
            types[typ] = (types[typ] or 0) + 1
            if typ == LOCATION_ROOM_TYPE then
                rooms[#rooms + 1] = { id = tonumber(id), name = XMLDecode(name) }
            end
        end
    else
        LogE("GetProjectItems failed: " .. tostring(xml))
    end
    local hist = {}
    for k, v in pairs(types) do hist[#hist + 1] = k .. ":" .. v end
    Log("project location types " .. table.concat(hist, " "))
    if #rooms == 0 then
        -- Fallback: at least the room the driver sits in.
        local rid = C4:RoomGetId()
        if rid then
            local okName, nm = pcall(function() return C4:GetDeviceDisplayName(rid) end)
            rooms[1] = { id = tonumber(rid), name = (okName and nm) or ("Room " .. tostring(rid)) }
            LogE("no rooms parsed from project, using driver room " .. tostring(rid))
        end
    end
    gProjectRooms = rooms
    local names = {}
    for i, r in ipairs(rooms) do names[i] = r.name end
    C4:UpdateProperty("Rooms Found", (#rooms .. ": " .. table.concat(names, ", ")):sub(1, 250))
    if UpdateVolumeRoomList then UpdateVolumeRoomList(rooms) end
    return rooms
end

-- ============================================================
-- DIGITAL AUDIO ROOM MAP (queue -> rooms)
-- ============================================================
local function ParseRoomMap(xml)
    local map = {}
    for q in tostring(xml or ""):gmatch("<queue>(.-)</queue>") do
        local head = q:match("^(.-)<rooms>") or q
        local qid = tonumber(head:match("<id>(%d+)</id>"))
        if qid then
            local rooms = {}
            for rid in (q:match("<rooms>(.-)</rooms>") or ""):gmatch("<id>(%d+)</id>") do
                rooms[#rooms + 1] = tonumber(rid)
            end
            map[qid] = rooms
        end
    end
    return map
end

local function OnRoomMap(xml)
    Log("room map " .. tostring(xml):sub(1, 400))
    gRoomMap = ParseRoomMap(xml)
    -- A playing room that left its queue switched to another source.
    for _, r in pairs(gRooms) do
        if r.state == "playing" and r.queueId and gRoomMap[r.queueId]
            and not Contains(gRoomMap[r.queueId], r.id) then
            LogI("room " .. r.id .. " left queue " .. r.queueId)
            r.state = "stopped"
            Webhook({ event = "deselected", room_id = r.id })
        end
    end
end

local OnRoomVolumeVariableRef   -- volume section, below

function OnWatchedVariableChanged(idDevice, idVariable, strValue)
    idDevice, idVariable = tonumber(idDevice), tonumber(idVariable)
    if idDevice == DIGITAL_AUDIO and idVariable == DA_ROOM_MAP_VAR then
        OnRoomMap(strValue)
    elseif OnRoomVolumeVariableRef then
        OnRoomVolumeVariableRef(idDevice, idVariable, strValue)
    end
end


-- ============================================================
-- VOLUME (per room: station volume -> C4 room volume, ducking)
-- ============================================================
-- HA reports the station volume (0..1) and whether Alice is listening; the
-- driver owns the per-room calibration and the room's real volume:
--   * one Alice step (+-0.1) moves the room by "step" from its CURRENT level,
--     so a level set on a keypad is kept ("louder" adds to it);
--   * a bigger change ("volume 5") maps absolutely onto min..max;
--   * the first value after a (re)start is only remembered (AlexxIT sends the
--     station's level as soon as streaming starts: no jump in the room);
--   * while Alice listens/answers the room is lowered to duck_level % of its
--     level (or muted) and restored exactly afterwards; volume changes made by
--     voice during that time land in the level that is restored.
-- The room's level comes from its CURRENT_VOLUME-like variable (listener).
local gVolCfg   = {}   -- ["<roomId>"] = {step,max,min,duck,duck_level}  (persisted)
local gVol      = {}   -- [roomId] = {cur, var, prev_station, ducked, saved, mode, timer}
local gVolRoomByName = {}
local gVolSelected   = nil   -- room id shown in the "Volume:" properties
local gLoadingProps  = false

local function Clamp(x, lo, hi) if x < lo then return lo elseif x > hi then return hi end return x end
local function Round(x) return math.floor(x + 0.5) end

local function VolCfg(roomId)
    local c = gVolCfg[tostring(roomId)] or {}
    return {
        step = tonumber(c.step) or VOL_DEFAULTS.step,
        max = tonumber(c.max) or VOL_DEFAULTS.max,
        min = tonumber(c.min) or VOL_DEFAULTS.min,
        duck = c.duck or VOL_DEFAULTS.duck,
        duck_level = tonumber(c.duck_level) or VOL_DEFAULTS.duck_level,
    }
end

local function SaveVolCfg(roomId, cfg)
    gVolCfg[tostring(roomId)] = cfg
    C4:PersistSetValue("volume_cfg", gVolCfg)
end

local function VolState(roomId)
    roomId = tonumber(roomId)
    local v = gVol[roomId]
    if not v then v = {}; gVol[roomId] = v end
    return v
end

-- Find the room's current-volume variable once and listen to it.
local function WatchRoomVolume(roomId)
    local v = VolState(roomId)
    if v.var ~= nil then return v end
    v.var = false
    local ok, vars = pcall(function() return C4:GetDeviceVariables(roomId) end)
    if not ok or type(vars) ~= "table" then
        LogE("room " .. roomId .. ": no variables (" .. tostring(vars) .. ")")
        return v
    end
    local names, pick, fallback = {}, nil, nil
    for id, var in pairs(vars) do
        local name = type(var) == "table" and tostring(var.name or var.NAME or "") or ""
        names[#names + 1] = tostring(id) .. "=" .. name
        if name == "CURRENT_VOLUME" then pick = { id = id, var = var }
        elseif not fallback and name:find("VOLUME") and not name:find("MUTE") then
            fallback = { id = id, var = var }
        end
    end
    table.sort(names)
    Log("room " .. roomId .. " variables: " .. table.concat(names, ", "):sub(1, 600))
    pick = pick or fallback
    if not pick then
        LogE("room " .. roomId .. ": no volume variable, relative steps fall back to absolute")
        return v
    end
    v.var = tonumber(pick.id)
    v.cur = tonumber(type(pick.var) == "table" and (pick.var.value or pick.var.VALUE) or nil)
    C4:RegisterVariableListener(roomId, v.var)
    Log("room " .. roomId .. ": volume variable " .. v.var .. " = " .. tostring(v.cur))
    return v
end

local function ShowCurrentVolume(roomId)
    if gVolSelected == roomId then
        local cur = VolState(roomId).cur
        C4:UpdateProperty("Volume: Current", cur and (tostring(cur) .. " %") or "unknown")
    end
end

local function SetRoomVolume(roomId, level)
    level = Clamp(Round(level), 0, 100)
    Log("room " .. roomId .. " volume -> " .. level)
    C4:SendToDevice(roomId, "SET_VOLUME_LEVEL", { LEVEL = tostring(level) })
    VolState(roomId).cur = level
    ShowCurrentVolume(roomId)
    return level
end

local function OnRoomVolumeVariable(roomId, value)
    local v = VolState(roomId)
    v.cur = tonumber(value) or v.cur
    ShowCurrentVolume(roomId)
    Webhook({ event = "volume", room_id = roomId, level = v.cur, ducked = v.ducked or false })
end

OnRoomVolumeVariableRef = function(idDevice, idVariable, value)
    local v = gVol[idDevice]
    if v and v.var and v.var == idVariable then OnRoomVolumeVariable(idDevice, value) end
end

local function StationVolume(roomId, level, initial)
    local v, cfg = WatchRoomVolume(roomId), VolCfg(roomId)
    level = Clamp(tonumber(level) or 0, 0, 1)
    local prev = v.prev_station
    v.prev_station = level
    if initial or prev == nil then return "recorded", nil end
    local d = level - prev
    if math.abs(d) < 0.005 then return "unchanged", nil end
    local base = v.ducked and v.saved or v.cur
    local target, how
    if math.abs(d) <= 0.15 and base then
        target, how = base + (d > 0 and cfg.step or -cfg.step), "step"
    else
        target, how = cfg.min + level * (cfg.max - cfg.min), "absolute"
    end
    target = Clamp(Round(target), cfg.min, cfg.max)
    if v.ducked then
        v.saved = target        -- applied when the duck is lifted
    else
        SetRoomVolume(roomId, target)
    end
    return how, target
end

local function Unduck(roomId)
    local v = VolState(roomId)
    if not v.ducked then return false end
    v.ducked = false
    if v.timer then v.timer:Cancel(); v.timer = nil end
    if v.mode == "Mute" then
        C4:SendToDevice(roomId, "MUTE_OFF", {})
    elseif v.saved then
        SetRoomVolume(roomId, v.saved)
    end
    return true
end

local function Duck(roomId, active)
    local v, cfg = WatchRoomVolume(roomId), VolCfg(roomId)
    if not active then return Unduck(roomId) and "restored" or "not ducked" end
    if cfg.duck == "Off" then return "off" end
    if v.ducked then return "already" end
    local r = gRooms[tonumber(roomId)]
    if not r or r.state ~= "playing" then return "not playing" end
    if cfg.duck == "Mute" then
        C4:SendToDevice(roomId, "MUTE_ON", {})
    else
        if not v.cur then return "volume unknown" end
        v.saved = v.cur
        SetRoomVolume(roomId, v.cur * cfg.duck_level / 100)
    end
    v.ducked, v.mode = true, cfg.duck
    v.timer = C4:SetTimer(DUCK_MAX_MS, function()
        v.timer = nil
        if v.ducked then LogI("room " .. roomId .. ": duck not released, restoring"); Unduck(roomId) end
    end)
    return "ducked"
end

-- Composer: "Volume: Room" picks the room, the fields below edit its settings.
local function LoadVolumeProps(roomId)
    local cfg = VolCfg(roomId)
    gLoadingProps = true
    C4:UpdateProperty("Volume: Step %", tostring(cfg.step))
    C4:UpdateProperty("Volume: Max %", tostring(cfg.max))
    C4:UpdateProperty("Volume: Min %", tostring(cfg.min))
    C4:UpdateProperty("Volume: Duck", cfg.duck)
    C4:UpdateProperty("Volume: Duck Level %", tostring(cfg.duck_level))
    gLoadingProps = false
    WatchRoomVolume(roomId)
    ShowCurrentVolume(roomId)
end

UpdateVolumeRoomList = function(rooms)
    gVolRoomByName = {}
    local names, selectedName = {}, nil
    for _, r in ipairs(rooms) do
        local name = tostring(r.name):gsub(",", " ")
        if gVolRoomByName[name] then name = name .. " (" .. r.id .. ")" end
        gVolRoomByName[name] = r.id
        names[#names + 1] = name
        if r.id == gVolSelected then selectedName = name end
    end
    if not selectedName then gVolSelected = nil end
    C4:UpdatePropertyList("Volume: Room",
        VOL_NO_ROOM .. (#names > 0 and ("," .. table.concat(names, ",")) or ""),
        selectedName or VOL_NO_ROOM)
end

local VOL_FIELDS = {
    ["Volume: Step %"] = "step", ["Volume: Max %"] = "max", ["Volume: Min %"] = "min",
    ["Volume: Duck"] = "duck", ["Volume: Duck Level %"] = "duck_level",
}

-- Returns true when the property was a volume one.
local function OnVolumeProperty(name)
    if name == "Volume: Room" then
        gVolSelected = gVolRoomByName[Properties["Volume: Room"]]
        if gVolSelected then LoadVolumeProps(gVolSelected)
        else C4:UpdateProperty("Volume: Current", "") end
        return true
    end
    local key = VOL_FIELDS[name]
    if not key then return false end
    if gLoadingProps then return true end
    if not gVolSelected then
        LogE("pick a room in \"Volume: Room\" first; " .. name .. " not saved")
        return true
    end
    local cfg = VolCfg(gVolSelected)
    cfg[key] = (key == "duck") and Properties[name] or tonumber(Properties[name])
    if cfg.min >= cfg.max then
        cfg.min = math.max(0, cfg.max - 1)
        gLoadingProps = true
        C4:UpdateProperty("Volume: Min %", tostring(cfg.min))
        gLoadingProps = false
    end
    SaveVolCfg(gVolSelected, cfg)
    Log("room " .. gVolSelected .. " volume settings " .. JSON:encode(cfg))
    return true
end

-- Calibration: set the level you like on a keypad, then take it as max/min.
local function TakeCurrentAs(key)
    if not gVolSelected then LogE("pick a room in \"Volume: Room\" first"); return end
    local cur = WatchRoomVolume(gVolSelected).cur
    if not cur then LogE("room " .. gVolSelected .. ": current volume unknown"); return end
    local cfg = VolCfg(gVolSelected)
    cfg[key] = cur
    if cfg.min >= cfg.max then
        if key == "max" then cfg.min = math.max(0, cur - 1) else cfg.max = math.min(100, cur + 1) end
    end
    SaveVolCfg(gVolSelected, cfg)
    LoadVolumeProps(gVolSelected)
    LogI("room " .. gVolSelected .. ": " .. key .. " = " .. cur .. " %")
end

-- ============================================================
-- HTTP API (HA -> driver)
-- ============================================================
local REASONS = { [200] = "OK", [400] = "Bad Request", [401] = "Unauthorized",
    [404] = "Not Found", [409] = "Conflict", [411] = "Length Required",
    [413] = "Payload Too Large", [500] = "Internal Server Error" }

local function RoomState(r)
    local t = r.track or {}
    local v = gVol[r.id] or {}
    return {
        room_id = r.id, state = r.state, queue_id = r.queueId,
        title = t.title, artist = t.artist,
        source = r.track and (r.fallback_used and "fallback" or "direct") or nil,
        volume = v.cur, ducked = v.ducked or false,
    }
end

local API = {}

API["GET /info"] = function(req)
    return 200, { driver = "yandex-relay", version = DRIVER_VERSION,
        paired = gPairing.webhook ~= nil, rooms = #gProjectRooms }
end

API["POST /pair"] = function(req, b)
    local wh = b.webhook_url
    if type(wh) ~= "string" or not wh:match("^https?://") then
        return 400, { error = "webhook_url must be http(s)" }
    end
    gPairing.webhook = wh
    C4:PersistSetValue("webhook", wh, true)
    C4:UpdateProperty("Paired With", HostOf(wh))
    LogI("paired with " .. HostOf(wh))
    Webhook({ event = "hello" })
    return 200, { ok = true, version = DRIVER_VERSION }
end

API["GET /rooms"] = function(req)
    return 200, { rooms = DiscoverRooms() }
end

API["POST /play"] = function(req, b)
    local r = GetRoom(b.room_id)
    if not r then return 400, { error = "room_id required" } end
    if (not b.url or b.url == "") and (not b.fallback_url or b.fallback_url == "") then
        return 400, { error = "url or fallback_url required" }
    end
    PlayInRoom(r, b)
    return 200, RoomState(r)
end

API["POST /pause"] = function(req, b)
    local r = GetRoom(b.room_id)
    if not r then return 400, { error = "room_id required" } end
    if r.state == "playing" or r.state == "starting" then RoomOff(r, "pause") end
    return 200, RoomState(r)
end

API["POST /stop"] = function(req, b)
    local r = GetRoom(b.room_id)
    if not r then return 400, { error = "room_id required" } end
    if r.state == "playing" or r.state == "starting" then RoomOff(r, "stop") end
    return 200, RoomState(r)
end

-- Resumes in place while digital audio still holds the paused queue, otherwise
-- restarts the track; "resume" in the answer tells c4_relay which one, so it
-- seeks the station back to 0 only after a restart.
API["POST /resume"] = function(req, b)
    local r = GetRoom(b.room_id)
    if not r then return 400, { error = "room_id required" } end
    if not r.track then return 409, { error = "nothing to resume" } end
    local st = RoomState(r)
    st.resume = Resume(r)
    st.state = r.state
    return 200, st
end

API["POST /station_volume"] = function(req, b)
    local r = GetRoom(b.room_id)
    if not r or b.level == nil then return 400, { error = "room_id and level required" } end
    local how, target = StationVolume(r.id, b.level, b.initial == true)
    return 200, { room_id = r.id, result = how, volume = target or VolState(r.id).cur }
end

API["POST /duck"] = function(req, b)
    local r = GetRoom(b.room_id)
    if not r then return 400, { error = "room_id required" } end
    local result = Duck(r.id, b.active == true)
    return 200, { room_id = r.id, result = result, ducked = VolState(r.id).ducked or false }
end

-- Direct room level (tests, relayctl); still capped by the room's max.
API["POST /volume"] = function(req, b)
    local r = GetRoom(b.room_id)
    if not r or b.level == nil then return 400, { error = "room_id and level required" } end
    WatchRoomVolume(r.id)
    local cfg = VolCfg(r.id)
    return 200, { room_id = r.id, volume = SetRoomVolume(r.id, Clamp(tonumber(b.level) or 0, 0, cfg.max)) }
end

API["GET /state"] = function(req)
    local rid = tonumber(req.query.room_id)
    if rid then return 200, RoomState(GetRoom(rid)) end
    local all = {}
    for _, r in pairs(gRooms) do all[#all + 1] = RoomState(r) end
    return 200, { rooms = all }
end

local function ParseRequest(buf)
    local hend = buf:find("\r\n\r\n", 1, true)
    if not hend then return nil end
    local head = buf:sub(1, hend - 1)
    local method, target = head:match("^(%u+)%s+(%S+)%s+HTTP/%d%.%d")
    if not method then return false, 400 end
    local headers = {}
    for line in head:gmatch("\r\n([^\r\n]+)") do
        local k, v = line:match("^([^:]+):%s*(.-)%s*$")
        if k then headers[k:lower()] = v end
    end
    if headers["transfer-encoding"] then return false, 411 end
    local len = tonumber(headers["content-length"] or "0") or 0
    local body = buf:sub(hend + 4)
    if #body < len then return nil end
    local path, qs = target:match("^([^?]*)%??(.*)$")
    return { method = method, path = path, query = ParseQuery(qs), headers = headers,
        body = body:sub(1, len) }
end

local function SendResponse(h, code, tbl)
    local body = JSON:encode(tbl or {})
    C4:ServerSend(h, "HTTP/1.1 " .. code .. " " .. (REASONS[code] or "") .. "\r\n"
        .. "Content-Type: application/json; charset=utf-8\r\n"
        .. "Content-Length: " .. #body .. "\r\n"
        .. "Connection: close\r\n\r\n" .. body)
    local c = gClients[h]
    if c then c.done = true end
    C4:SetTimer(CLOSE_DELAY_MS, function()
        if gClients[h] then pcall(function() C4:ServerCloseClient(h) end); gClients[h] = nil end
    end)
end

local function HandleRequest(h, req)
    if req.headers["x-relay-key"] ~= gPairing.code then
        LogE("rejected " .. req.method .. " " .. req.path .. ": bad pairing code")
        return SendResponse(h, 401, { error = "bad pairing code" })
    end
    local fn = API[req.method .. " " .. req.path]
    if not fn then return SendResponse(h, 404, { error = "no such endpoint" }) end
    local b = {}
    if req.body ~= "" then
        b = JSON:decode(req.body)
        if type(b) ~= "table" then return SendResponse(h, 400, { error = "body must be JSON object" }) end
    end
    Log(req.method .. " " .. req.path .. " " .. req.body:sub(1, 300))
    local ok, code, resp = pcall(fn, req, b)
    if not ok then
        LogE("handler " .. req.path .. ": " .. tostring(code))
        return SendResponse(h, 500, { error = "internal error" })
    end
    SendResponse(h, code, resp)
end

-- CreateServer gives no error when the port is still held (seen on site when a
-- removed instance had not released it yet): the server just never reports
-- ONLINE. So every start is checked, and retried until it comes up.
local gServerOnline  = false
local gServerAttempt = 0

local StartServer
local function CheckServerLater(port)
    local delay = gServerAttempt == 0 and SERVER_CHECK_MS or SERVER_RETRY_MS
    C4:SetTimer(delay, function()
        if gServerPort ~= port or gServerOnline then return end
        gServerAttempt = gServerAttempt + 1
        LogE("bridge port " .. port .. " not online, retry " .. gServerAttempt)
        StartServer()
    end)
end

StartServer = function()
    if gServerPort then pcall(function() C4:DestroyServer(gServerPort) end) end
    local port = tonumber(Properties["Bridge Port"]) or DEFAULT_PORT
    if port ~= gServerPort then gServerAttempt = 0 end
    gServerPort = port
    gServerOnline = false
    C4:UpdateProperty("Bridge Status", (gServerAttempt > 0 and ("retry " .. gServerAttempt) or "starting")
        .. " :" .. port)
    C4:CreateServer(port)
    CheckServerLater(port)
end

function OnServerStatusChanged(nPort, strStatus)
    LogI("bridge port " .. tostring(nPort) .. " " .. tostring(strStatus))
    if tonumber(nPort) ~= gServerPort then return end   -- a port we already left
    gServerOnline = (strStatus == "ONLINE")
    C4:UpdateProperty("Bridge Status", tostring(strStatus) .. " :" .. tostring(nPort))
    if gServerOnline then
        gServerAttempt = 0
    else
        CheckServerLater(gServerPort)
    end
end

function OnServerConnectionStatusChanged(nHandle, nPort, strStatus, strIP)
    if strStatus == "ONLINE" then
        gClients[nHandle] = { buf = "", ip = strIP or "" }
    else
        gClients[nHandle] = nil
    end
end

function OnServerDataIn(nHandle, strData, strIP, strPort)
    local c = gClients[nHandle]
    if not c then c = { buf = "", ip = strIP or "" }; gClients[nHandle] = c end
    if c.done then return end
    c.buf = c.buf .. (strData or "")
    if #c.buf > MAX_REQUEST then return SendResponse(nHandle, 413, { error = "too large" }) end
    local req, err = ParseRequest(c.buf)
    if req == nil then return end            -- incomplete, wait for more data
    if req == false then return SendResponse(nHandle, err, { error = REASONS[err] }) end
    HandleRequest(nHandle, req)
end

-- ============================================================
-- PROXY COMMANDS (C4 -> driver)
-- ============================================================
local RFP = {}

local function Handled(yes)
    return yes and "<ret><handled>true</handled></ret>" or "<ret><handled>false</handled></ret>"
end

local function RoomFromParams(tP)
    return GetRoom(tP.ROOM_ID or tP.ROOMID or tP.idRoom)
end

-- Transport presses. Pause and Stop are ROOM buttons in driver.xml (as in
-- TuneIn): the room stops the stream through digital audio and we only get a
-- notification carrying ROOM_ID, answered handled=false so digital audio goes
-- on. PROTOCOL Pause/Stop reached only this driver and never stopped the
-- stream, whatever we returned (site 2026-09-28). Play and Skip stay ours.
local function LetDigitalAudioHandle(tP)
    if tP.ROOM_ID then return Handled(false) end
    return nil
end

local function Transport(cmd, tP)
    local r = RoomFromParams(tP)
    -- Our own room command (or the PAUSE/STOP a ROOM_OFF may cause) coming back.
    if r and (r.suppress == cmd or (r.suppress == "ROOM_OFF" and (cmd == "PAUSE" or cmd == "STOP"))) then
        if r.suppress == cmd then r.suppress = nil end
        Log("room " .. r.id .. ": own " .. cmd .. " came back, not forwarded")
        return LetDigitalAudioHandle(tP)
    end
    local rid = r and r.id or nil
    if cmd == "SKIP_FWD" then
        Webhook({ event = "transport", room_id = rid, action = "next" }); return Handled(true)
    elseif cmd == "SKIP_REV" then
        Webhook({ event = "transport", room_id = rid, action = "prev" }); return Handled(true)
    elseif cmd == "PLAY" then
        if tP.ROOM_ID then
            -- The room's own PLAY: digital audio resumes the queue itself.
            Webhook({ event = "transport", room_id = rid, action = "play", resume = "in_place" })
            return Handled(false)
        end
        -- Panel Play: act right away so the button works even when HA does not
        -- answer; HA resumes the station, and its /resume is then a no-op
        -- because the room is already playing.
        local how = r and Resume(r) or "none"
        Webhook({ event = "transport", room_id = rid, action = "play", resume = how })
        return Handled(true)
    elseif cmd == "PAUSE" or cmd == "STOP" then
        Webhook({ event = "transport", room_id = rid, action = cmd:lower() })
        if r and (r.state == "playing" or r.state == "starting") then
            RoomOff(r, (cmd == "PAUSE") and "pause" or "stop")
        end
        return LetDigitalAudioHandle(tP)
    end
end

function RFP.PLAY(tP)     return Transport("PLAY", tP) end
function RFP.PAUSE(tP)    return Transport("PAUSE", tP) end
function RFP.STOP(tP)     return Transport("STOP", tP) end
function RFP.SKIP_FWD(tP) return Transport("SKIP_FWD", tP) end
function RFP.SKIP_REV(tP) return Transport("SKIP_REV", tP) end

-- OFF arrives without a room id. After our own ROOM_OFF it is swallowed; a
-- room switched off by the user shows up as its queue stopping or leaving,
-- which already pauses the station.
function RFP.OFF(tP)
    local r = RoomFromParams(tP)
    if not r then
        for _, x in pairs(gRooms) do
            if x.suppress == "ROOM_OFF" then r = x; break end
        end
    end
    if r and r.suppress == "ROOM_OFF" then
        r.suppress = nil
        Log("room " .. r.id .. ": own ROOM_OFF came back, not forwarded")
        return
    end
    if r then r.intent = "stop" end
    Webhook({ event = "transport", room_id = r and r.id or nil, action = "off" })
end

function RFP.DEVICE_SELECTED(tP)
    local r = RoomFromParams(tP)
    if not r then return end
    Log("DEVICE_SELECTED room=" .. r.id)
    if r.track then RefreshUI(r) end
    Webhook({ event = "selected", room_id = r.id })
end

-- Sent when a room picks this source; DEVICE_SELECTED follows and does the work.
function RFP.SELECT_SOURCE(tP) end
-- Queue bookkeeping from digital audio; QUEUE_STATE_CHANGED carries what we need.
function RFP.QUEUE_INFO_CHANGED(tP) end

function RFP.DEVICE_DESELECTED(tP)
    Log("DEVICE_DESELECTED room=" .. tostring(tP.ROOM_ID or tP.idRoom))
end

-- QUEUE_INFO carries the track key we passed in SELECT_INTERNET_RADIO.
-- An event whose key differs from the room's current track is about the
-- previous track (a new /play replaced it) and must not change state.
local function IsStale(r, tP)
    local qi = tP.QUEUE_INFO
    if qi == nil or qi == "" or not r.track then return false end
    return tostring(qi) ~= tostring(r.track.key)
end

function RFP.INTERNET_RADIO_SELECTED(tP)
    local qid = tonumber(tP.QUEUE_ID or tP.QUEUEID)
    local url = tP.STATION_URL or ""
    local r = tP.ROOM_ID and GetRoom(tP.ROOM_ID) or nil
    if not r then
        for _, x in pairs(gRooms) do
            if x.state == "starting" and (x.playing_url == url
                or (x.track and tostring(x.track.key) == tostring(tP.QUEUE_INFO))) then
                r = x; break
            end
        end
    end
    if r and IsStale(r, tP) then Log("INTERNET_RADIO_SELECTED for previous track, ignored"); return end
    if not r then Log("INTERNET_RADIO_SELECTED for unknown room, queue " .. tostring(qid)); return end
    if qid then r.queueId = qid; gQueueRoom[qid] = r.id end
    r.state = "playing"
    r.intent = nil
    r.meta_ticks = META_TICKS
    RefreshUI(r)
    StartMetaTimer()
    WebhookState(r)
end

function RFP.QUEUE_STATE_CHANGED(tP)
    local qid = tonumber(tP.QUEUE_ID or tP.QUEUEID)
    local rid = qid and gQueueRoom[qid]
    local r = rid and gRooms[rid]
    if not r then return end
    local st = tP.STATE or ""
    Log("queue " .. tostring(qid) .. " room " .. r.id .. " " .. st .. " intent=" .. tostring(r.intent))
    if IsStale(r, tP) then Log("state for previous track, ignored"); return end
    if st == "PLAY" then
        r.state = "playing"
        r.intent = nil
    elseif r.intent == "switch" and os.time() - (r.started_at or 0) <= FALLBACK_WINDOW then
        Log("state " .. st .. " of the replaced stream, ignored")
        return
    elseif st == "PAUSE" or st == "STOP" or st == "END" then
        if r.intent == "switch" then r.intent = nil end   -- switch window expired
        if r.intent == nil and TryFallback(r) then return end
        if r.intent == "pause" or st == "PAUSE" then r.state = "paused"
        elseif r.intent == "stop" or st == "STOP" then r.state = "stopped"
        else r.state = "ended" end    -- natural end: the station pushes the next track
        r.intent = nil
    end
    UpdateDashboard(r)
    WebhookState(r)
end

function RFP.QUEUE_DELETED(tP)
    local qid = tonumber(tP.QUEUE_ID or tP.QUEUEID)
    local rid = qid and gQueueRoom[qid]
    if qid then gQueueRoom[qid] = nil; gRoomMap[qid] = nil end
    local r = rid and gRooms[rid]
    if not r or r.queueId ~= qid then return end
    r.queueId = nil
    if r.state == "playing" and r.intent == nil then
        LogI("room " .. r.id .. ": queue deleted while playing")
        r.state = "stopped"
        Webhook({ event = "deselected", room_id = r.id })
    end
end

-- Progress comes from digital audio as "k=v,k=v" (TuneIn forwards it as-is).
function RFP.QUEUE_STREAM_STATUS_CHANGED(tP)
    local rooms = RoomsOfQueue(tP.QUEUE_ID or tP.QUEUEID)
    if not rooms then return end
    local values = {}
    for kv in tostring(tP.STATUS or ""):gmatch("[^,]+") do
        local k, v = kv:match("^([^=]+)=(.*)$")
        if k then values[k] = v end
    end
    SendEvent(nil, rooms, "ProgressChanged", values)
end

-- Never re-assert metadata here: our UPDATE_MEDIA_INFO triggers this event
-- again and the loop took a controller down (Yandex Music driver v1.0.77).
function RFP.QUEUE_MEDIA_INFO_UPDATED(tP) end

function RFP.GetQueue(tP)
    local r = RoomFromParams(tP)
    if r and r.track then UpdateQueue(r, tP.NAVID) end
    if r then UpdateDashboard(r, tP.NAVID) end
end

function RFP.GetDashboard(tP)
    local r = RoomFromParams(tP)
    if r then UpdateDashboard(r, tP.NAVID) end
end

-- Nothing to browse: music is started by voice. Answer so the navigator does
-- not spin, with one hint item.
local function BrowseHint(tP)
    DataReceived(tP.NAVID, tP.SEQ, '<items total="1"><item><id>hint</id>'
        .. '<name>' .. XMLEncode("Скажите станции: «Алиса, включи музыку»") .. '</name>'
        .. '<type>TEXT</type><image></image></item></items>')
end
RFP.GetBrowseMenu  = BrowseHint
RFP.GetBrowseItems = BrowseHint
RFP.Search         = BrowseHint
RFP.SearchItems    = BrowseHint

-- Fired many times a second while playing; kept out of the debug dump.
local NOISY = { QUEUE_STREAM_STATUS_CHANGED = true, QUEUE_MEDIA_INFO_UPDATED = true }

function ReceivedFromProxy(idBinding, strCommand, tParams)
    strCommand = strCommand or ""
    tParams = tParams or {}
    if Properties["Debug Mode"] == "On" and not NOISY[strCommand] then
        local kv = {}
        for k, v in pairs(tParams) do kv[#kv + 1] = tostring(k) .. "=" .. tostring(v):sub(1, 120) end
        table.sort(kv)
        Log("proxy " .. strCommand .. " {" .. table.concat(kv, ", ") .. "}")
    end
    local fn = RFP[strCommand]
    if not fn then Log("unhandled proxy cmd " .. strCommand); return end
    local ok, ret = pcall(fn, tParams)
    if not ok then LogE("proxy cmd " .. strCommand .. ": " .. tostring(ret)); return end
    return ret
end

-- ============================================================
-- PAIRING, ACTIONS, PROPERTIES, LIFECYCLE
-- ============================================================
local function SetPairingCode(code)
    gPairing.code = code
    C4:PersistSetValue("pairing_code", code, true)
    C4:UpdateProperty("Pairing Code", code)
end

local function Unpair()
    gPairing.webhook = nil
    C4:PersistSetValue("webhook", "", true)
    C4:UpdateProperty("Paired With", "")
end

function ExecuteCommand(sCommand, tParams)
    if sCommand ~= "LUA_ACTION" then return end
    local action = tParams and tParams.ACTION
    if action == "RegeneratePairingCode" then
        -- A new code invalidates the HA side anyway, so drop the pairing too.
        SetPairingCode(RandomCode(8)); Unpair()
        LogI("pairing code regenerated, HA must be re-paired")
    elseif action == "Unpair" then
        Unpair(); LogI("unpaired")
    elseif action == "RefreshRooms" then
        DiscoverRooms()
    elseif action == "VolMaxFromCurrent" then
        TakeCurrentAs("max")
    elseif action == "VolMinFromCurrent" then
        TakeCurrentAs("min")
    end
end

function OnPropertyChanged(name)
    if name == "Bridge Port" then StartServer(); return end
    OnVolumeProperty(name)
end

function OnDriverInit()
    C4:UpdateProperty("Driver Version", DRIVER_VERSION)
end

function OnDriverLateInit()
    math.randomseed(os.time() + (tonumber(C4:GetDeviceID()) or 0))
    math.random(); math.random(); math.random()

    local code = C4:PersistGetValue("pairing_code", true)
    if type(code) ~= "string" or code == "" then code = RandomCode(8) end
    SetPairingCode(code)
    local wh = C4:PersistGetValue("webhook", true)
    gPairing.webhook = (type(wh) == "string" and wh ~= "") and wh or nil
    C4:UpdateProperty("Paired With", HostOf(gPairing.webhook))

    local vc = C4:PersistGetValue("volume_cfg")
    gVolCfg = type(vc) == "table" and vc or {}

    C4:SendToProxy(PROXY, "ENABLE_DRIVER", {}, "COMMAND")
    OnRoomMap(C4:GetVariable(DIGITAL_AUDIO, DA_ROOM_MAP_VAR))
    C4:RegisterVariableListener(DIGITAL_AUDIO, DA_ROOM_MAP_VAR)
    DiscoverRooms()
    StartServer()
    LogI("v" .. DRIVER_VERSION .. " ready, " .. #gProjectRooms .. " rooms, paired="
        .. tostring(gPairing.webhook ~= nil))
end

function OnDriverDestroyed()
    pcall(function() C4:DestroyServer() end)
end

-- Test hook: exposes internals to the local test harness only.
if YANDEX_RELAY_TEST then
    YANDEX_RELAY_TEST.rooms = gRooms
    YANDEX_RELAY_TEST.pairing = gPairing
    YANDEX_RELAY_TEST.parse_room_map = ParseRoomMap
    YANDEX_RELAY_TEST.parse_request = ParseRequest
    YANDEX_RELAY_TEST.project_rooms = function() return gProjectRooms end
    YANDEX_RELAY_TEST.vol = gVol
    YANDEX_RELAY_TEST.vol_cfg = function(id) return VolCfg(id) end
end
