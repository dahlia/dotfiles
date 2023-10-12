local wezterm = require "wezterm"
local act = wezterm.action

return {
  font = wezterm.font_with_fallback {
    "Sarasa Term K",
    "Iosevka Term",
    { family = "Fira Code", weight = "Light" },
  },
  font_size = 20,

  color_scheme = "OneDark (base16)",
  window_frame = { font_size = 14 },

  keys = {
    {
      key = 'k',
      mods = 'CMD',
      action = act.ClearScrollback "ScrollbackAndViewport",
    },
    { -- CMD + T in Hangul mode
      key = "ㅅ",
      mods = "CMD",
      action = act.SpawnTab "CurrentPaneDomain",
    },
  },
}

-- vim: set et sw=2 ts=2 sts=2 ft=lua:
