-- Recording stubs of the DriverWorks API used by driver.lua, for tests only.
-- Every call lands in REC[<api>] so the Python tests can assert on it.

REC = setmetatable({}, { __index = function(t, k) local v = {}; rawset(t, k, v); return v end })
PROPS = { ["Debug Mode"] = "On", ["Bridge Port"] = "18765" }
Properties = PROPS
PERSIST = {}
FAKE_NOW = 1000

local real_time = os.time
os.time = function(t) if t then return real_time(t) end return FAKE_NOW end

local function rec(name, entry) table.insert(REC[name], entry) end

-- Timers: collected, fired manually by the tests through FireTimers().
local timers = {}
function FireTimers()
    local snapshot = {}
    for _, t in ipairs(timers) do snapshot[#snapshot + 1] = t end
    for _, t in ipairs(snapshot) do
        if not t.cancelled then
            t.fn(t, 0)
            if not t.rep then t.cancelled = true end
        end
    end
    local alive = {}
    for _, t in ipairs(timers) do if not t.cancelled then alive[#alive + 1] = t end end
    timers = alive
end
function TIMERS_ACTIVE()
    local n = {}
    for _, t in ipairs(timers) do if not t.cancelled then n[#n + 1] = t end end
    return n
end

C4 = {}
function C4:UpdateProperty(name, value) PROPS[name] = value; rec("UpdateProperty", { name = name, value = value }) end
function C4:SendToProxy(binding, cmd, params, calltype)
    rec("SendToProxy", { binding = binding, cmd = cmd, params = params })
end
function C4:SendToDevice(id, cmd, params) rec("SendToDevice", { id = id, cmd = cmd, params = params }) end
-- Like a real controller the server reports ONLINE; SERVER_BIND_FAILS = true
-- simulates a port still held by another instance (no status ever arrives).
SERVER_BIND_FAILS = false
function C4:CreateServer(port, delim)
    rec("CreateServer", { port = port })
    if not SERVER_BIND_FAILS and OnServerStatusChanged then OnServerStatusChanged(port, "ONLINE") end
end
function C4:DestroyServer(port) rec("DestroyServer", { port = port }) end
function C4:ServerSend(h, data) rec("ServerSend", { h = h, data = data }) end
function C4:ServerCloseClient(h) rec("ServerCloseClient", { h = h }) end
function C4:SetTimer(ms, fn, rep)
    local t = { ms = ms, fn = fn, rep = rep, cancelled = false }
    function t:Cancel() self.cancelled = true end
    table.insert(timers, t)
    return t
end
function C4:PersistSetValue(name, value, enc) PERSIST[name] = value end
function C4:PersistGetValue(name, enc) return PERSIST[name] end
function C4:GetProjectItems(...) rec("GetProjectItems", { ... }); return PROJECT_XML end
function C4:RoomGetId() return 77 end
function C4:GetDeviceDisplayName(id) return "Room " .. tostring(id) end
function C4:GetDeviceID() return 555 end
function C4:GetVariable(dev, var) return "" end
function C4:UpdatePropertyList(name, list, default)
    rec("UpdatePropertyList", { name = name, list = list, default = default })
    PROPS[name] = default
end
-- Room variables: every room has a CURRENT_VOLUME of 40 unless a test sets ROOM_VARS[id].
ROOM_VARS = {}
function C4:GetDeviceVariables(id)
    rec("GetDeviceVariables", { id = id })
    return ROOM_VARS[id] or {
        [1000] = { name = "POWER_STATE", value = "1" },
        [1011] = { name = "CURRENT_VOLUME", value = "40" },
        [1012] = { name = "IS_MUTED", value = "0" },
    }
end
function C4:AddEvent(id, name, desc) rec("AddEvent", { id = id, name = name, desc = desc }) end
function C4:FireEventByID(id) rec("FireEventByID", { id = id }) end
function C4:FireEvent(name) rec("FireEvent", { name = name }) end
VARS = {}
function C4:AddVariable(name, value, vtype, ro, hidden)
    rec("AddVariable", { name = name, value = value, vtype = vtype }); VARS[name] = value; return true
end
function C4:SetVariable(name, value) rec("SetVariable", { name = name, value = value }); VARS[name] = value end
function C4:RegisterVariableListener(dev, var) rec("RegisterVariableListener", { dev = dev, var = var }) end
function C4:url()
    local o = {}
    function o:OnDone(fn) self.done = fn; return self end
    function o:Post(url, body, headers)
        rec("urlPost", { url = url, body = body, headers = headers })
        if self.done then self.done(self, { { code = 200, body = "" } }, 0, "") end
    end
    return o
end
