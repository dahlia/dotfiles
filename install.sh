#!/bin/bash
if ! command -v realpath; then
  realpath() {
    python -c 'import os.path, sys; print(os.path.realpath(sys.argv[1]))' "$1"
  }
fi

link-files() {
  source="$(realpath "$1")"; shift
  target="$(realpath "$1")"; shift
  ignores=( "$@" )

  for src in "$source/"* "$source"/.*; do
    rel="${src:$((${#source} + 1))}"
    if [[ "$rel" = "." || "$rel" = ".." ]]; then
      continue
    fi
    for i in "${ignores[@]}"; do
      if [[ "$i" = "$rel" ]]; then
        continue 2
      fi
    done
    dst="$target/$rel"
    {
      shorten-path "$src"
      echo -n " -> "
      shorten-path "$dst"
      echo
    } > /dev/stderr
    if [[ -f "$dst" ]]; then
      if [[ -L "$dst" || ! -d "$dst" ]]; then
        rm "$dst"
      else
        rm -rf "$dst"
      fi
    fi
    ln -sf "$src" "$dst"
  done
}

shorten-path() {
  cwd="$(pwd)"
  if [[ "$1" = "$cwd"/* ]]; then
    echo -n "${1:$((${#cwd} + 1))}"
  elif [[ "$1" = "$HOME"/* ]]; then
    # shellcheck disable=SC2088
    echo -n "~/${1:$((${#HOME} + 1))}"
  else
    echo -n "$1"
  fi
}

link-files "$(dirname "$0")" "$HOME" .config .hg .hgignore README.md install.sh
xdg_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
if [[ -d "$xdg_config_dir" ]]; then
  mkdir -p "$xdg_config_dir"
fi
link-files "$(dirname "$0")/.config" "$xdg_config_dir"
