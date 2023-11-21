local wezterm = require "wezterm"
local act = wezterm.action

local is_windows = wezterm.target_triple == "x86_64-pc-windows-msvc"
local is_macos = wezterm.target_triple == "x86_64-apple-darwin" or
                 wezterm.target_triple == "aarch64-apple-darwin"

local default_prog = nil

if is_windows then
  default_prog = {
    "pwsh.exe",
    "-NoLogo",
  }
end

local launch_menu = {}

if is_windows then
  launch_menu = {
    {
      label = "PowerShell",
      args = {"pwsh.exe", "-NoLogo"},
    },
    {
      label = "Command Prompt",
      args = {"cmd.exe"},
    },
    {
      label = "WSL",
      args = {"wsl.exe"},
    },
    {
      label = "Git Bash",
      args = {
        wezterm.home_dir.."\\scoop\\apps\\git\\current\\bin\\bash.exe",
        "--login",
      },
    }
  }
end

local keys = {
  {
    key = "k",
    mods = "CTRL",
    action = act.ClearScrollback "ScrollbackAndViewport",
  },
  {
    key = "n",
    mods = "CTRL",
    action = act.SpawnWindow,
  },
  {
    key = "t",
    mods = "CTRL",
    action = act.SpawnTab "CurrentPaneDomain",
  },
  {
    key = "{",
    mods = "CTRL|SHIFT",
    action = wezterm.action.ActivateTabRelative(-1),
  },
  {
    key = "}",
    mods = "CTRL|SHIFT",
    action = wezterm.action.ActivateTabRelative(1),
  },
  {
    key = "1",
    mods = "CTRL",
    action = wezterm.action.ActivateTab(0),
  },
  {
    key = "2",
    mods = "CTRL",
    action = wezterm.action.ActivateTab(1),
  },
  {
    key = "3",
    mods = "CTRL",
    action = wezterm.action.ActivateTab(2),
  },
  {
    key = "4",
    mods = "CTRL",
    action = wezterm.action.ActivateTab(3),
  },
  {
    key = "5",
    mods = "CTRL",
    action = wezterm.action.ActivateTab(4),
  },
  {
    key = "0",
    mods = "CTRL",
    action = wezterm.action.ActivateTab(-1),
  },
  {
    key = "c",
    mods = "CTRL",
    action = wezterm.action.CopyTo "Clipboard",
  },
  {
    key = "v",
    mods = "CTRL",
    action = wezterm.action.PasteFrom "Clipboard",
  },
}

if is_windows then
  table.insert(keys, {
    key = "t",
    mods = "CTRL|SHIFT",
    action = act.SpawnCommandInNewTab {
      domain = "CurrentPaneDomain",
      args = {"wsl.exe"},
    },
  })
end

if is_macos then
  keys = {
    {
      key = "k",
      mods = "CMD",
      action = act.ClearScrollback "ScrollbackAndViewport",
    },
    { -- CMD + T in Hangul mode
      key = "ㅅ",
      mods = "CMD",
      action = act.SpawnTab "CurrentPaneDomain",
    },
  }
end

return {
  font = wezterm.font_with_fallback {
    "Sarasa Term K",
    "Iosevka Term",
    { family = "Fira Code", weight = "Light" },
  },
  font_size = is_macos and 20 or 18,

  color_scheme = "OneDark (base16)",
  window_frame = { font_size = 14 },

  default_prog = default_prog,
  launch_menu = launch_menu,
  keys = keys,
}

-- vim: set et sw=2 ts=2 sts=2 ft=lua:
