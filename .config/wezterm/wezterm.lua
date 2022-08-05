local wezterm = require "wezterm"
local act = wezterm.action

return {
  font = wezterm.font { family = "Fira Code", weight = "Light" },
  font_size = 16,

  color_scheme = "OneDark (base16)",
  window_frame = { font_size = 14 },

  keys = {
    {
      key = 'k',
      mods = 'CMD',
      action = act.ClearScrollback "ScrollbackAndViewport",
    },
  },
}

-- vim: set et sw=2 ts=2 sts=2 ft=lua:
