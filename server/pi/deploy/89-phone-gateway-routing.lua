-- Keep phone HFP streams out of local speakers and monitor capture.
-- The API creates private PCM nodes and links these streams explicitly.
table.insert(bluez_monitor.rules, {
  matches = {
    {
      { "api.bluez5.profile", "equals", "headset-audio-gateway" },
      { "node.name", "matches", "bluez_input.*" },
    },
    {
      { "api.bluez5.profile", "equals", "headset-audio-gateway" },
      { "node.name", "matches", "bluez_output.*" },
    },
  },
  apply_properties = {
    ["node.autoconnect"] = false,
    ["priority.session"] = 0,
  },
})
