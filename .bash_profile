# macOS #######################################################################
if [[ "$(uname)" = "Darwin" ]]; then
  if [[ -f /opt/homebrew/bin/brew ]]; then
    export HOMEBREW_PATH=/opt/homebrew
    PATH="$HOMEBREW_PATH/bin:$PATH"
  else
    export HOMEBREW_PATH=/usr/local
  fi

  if [[ -d "$HOMEBREW_PATH/include" ]]; then
    C_INCLUDE_PATH="$HOMEBREW_PATH/include"
  fi

  if [[ -d "$HOMEBREW_PATH/lib" ]]; then
    LDFLAGS="$LDFLAGS -L$HOMEBREW_PATH/lib"
    DYLD_LIBRARY_PATH="$HOMEBREW_PATH/lib"
  fi

  if [[ -d "$HOMEBREW_PATH/opt/coreutils" ]]; then
    PATH="$HOMEBREW_PATH/opt/coreutils/libexec/gnubin:$PATH"
    MANPATH="$HOMEBREW_PATH/opt/coreutils/libexec/gnuman:$MANPATH"
  else
    alias ls='ls -GF'
    echo "coreutils is not installed" > /dev/stderr
  fi

  if [[ -d "$HOMEBREW_PATH/opt/findutils/libexec" ]]; then
    PATH="$HOMEBREW_PATH/opt/findutils/libexec/gnubin:$PATH"
    MANPATH="$HOMEBREW_PATH/opt/findutils/libexec/gnuman:$MANPATH"
  else
    echo "findutils is not installed" > /dev/stderr
  fi

  if [[ -d "$HOMEBREW_PATH/opt/grep/libexec" ]]; then
    PATH="$HOMEBREW_PATH/opt/grep/libexec/gnubin:$PATH"
    MANPATH="$HOMEBREW_PATH/opt/grep/libexec/gnuman:$MANPATH"
  else
    echo "grep is not installed" > /dev/stderr
  fi

  if [[ -d "$HOMEBREW_PATH/opt/gnu-sed/libexec" ]]; then
    PATH="$HOMEBREW_PATH/opt/gnu-sed/libexec/gnubin:$PATH"
    MANPATH="$HOMEBREW_PATH/opt/gnu-sed/libexec/gnuman:$MANPATH"
  else
    echo "gnu-sed is not installed" > /dev/stderr
  fi

  if [[ -d "$HOMEBREW_PATH/opt/gnu-tar/libexec/gnubin" ]]; then
    PATH="$HOMEBREW_PATH/opt/gnu-tar/libexec/gnubin:$PATH"
    MANPATH="$HOMEBREW_PATH/opt/gnu-tar/libexec/gnuman:$MANPATH"
  else
    echo "gnu-tar is not installed" > /dev/stderr
  fi

  if [[ -d "$HOMEBREW_PATH/opt/openjdk/bin" ]]; then
    PATH="$HOMEBREW_PATH/opt/openjdk/bin:$PATH"
    MANPATH="$HOMEBREW_PATH/opt/openjdk/share/man:$MANPATH"
  fi

  if [[ -r "$HOMEBREW_PATH/etc/profile.d/bash_completion.sh" ]]; then
    export BASH_COMPLETION_COMPAT_DIR="$HOMEBREW_PATH/etc/bash_completion.d"
    . "$HOMEBREW_PATH/etc/profile.d/bash_completion.sh"
  else
    echo "bash-completion2 is not installed" > /dev/stderr
  fi

  export PATH
  export MANPATH
  export C_INCLUDE_PATH
  export LDFLAGS
  export DYLD_LIBRARY_PATH
  export LANG=en_US.UTF-8
  export LC_CTYPE=en_US.UTF-8
fi

# WSL2 ########################################################################
if [[ "$(uname)" = Linux && "$(uname -r)" = *-microsoft-standard-WSL2 ]]; then
  if command -v keychain > /dev/null; then
    eval "$(keychain --eval --agents ssh id_ed25519)"
  else
    echo "keychain is not installed" > /dev/stderr
  fi

  if command -v wslview > /dev/null; then
    export BROWSER="$(command -v wslview)"
  else
    echo "wslu is not installed" >&2
  fi
fi

# User-local directories ######################################################
export PATH="$HOME/.local/bin:$PATH"

# exa (ls alt) ################################################################
if command -v exa > /dev/null; then
  alias ls='exa --classify --group-directories-first'
  alias tree='exa --tree'
else
  if [[ "$(type -t ls)" != "alias" ]]; then
    alias ls='ls --color=auto --indicator-style=slash'
  fi
  echo "exa is not installed" > /dev/stderr
fi

# bat (cat alt) ###############################################################
if command -v bat > /dev/null; then
  alias cat=bat
else
  echo "bat is not installed" > /dev/stderr
fi

# Neovim & vi mode ############################################################
if command -v nvim > /dev/null; then
  export EDITOR="$(command -v nvim)"
  export NVIM_TUI_ENABLE_TRUE_COLOR=1

  alias vi=nvim
  alias nvi=nvim
  alias vim=nvim

  if [[ "$(uname)" = "Darwin" ]] && command -v vimr > /dev/null; then
    alias gvim=vimr
  else
    alias gvim=nvim
  fi

  alias mvim=gvim
else
  echo "nvim (i.e., Neovim) is not installed" > /dev/stderr
fi

set editing-mode vi
set -o vi

# Haskell Stack ###############################################################
if command -v stack > /dev/null; then
  eval "$(stack --bash-completion-script stack)"
else
  echo "stack (i.e., Haskell Stack) is not installed" > /dev/stderr
fi

# Deno ########################################################################

if [[ "$DENO_HOME" != "" && -d "$DENO_HOME" ]]; then
  PATH="$DENO_HOME/bin:$PATH"
elif [[ -d "$HOME/.deno" ]]; then
  export DENO_HOME="$HOME/.deno"
  PATH="$DENO_HOME/bin:$PATH"
elif ! command -v deno > /dev/null; then
  echo "deno is not installed" > /dev/stderr
fi

# Python & pyenv ##############################################################
if command -v pyenv > /dev/null; then
  eval "$(pyenv init -)"
  if command -v pyenv-virtualenv-init > /dev/null; then
    eval "$(pyenv virtualenv-init -)";
  else
    echo "pyenv-virtualenv is not installed" > /dev/stderr
  fi
fi

if [[ -d "$HOMEBREW_PATH/opt/python@2.7/bin" ]]; then
  export PATH="/opt/homebrew/opt/python@2.7/bin:$PATH"
  export LDFLAGS="$LDFLAGS -L/opt/homebrew/opt/python@2.7/lib"
fi

if command -v pipx > /dev/null; then
  eval "$(register-python-argcomplete pipx)"
else
  echo "pipx is not installed" > /dev/stderr
fi

# Ruby ########################################################################
for rv in ~/.gem/ruby/*; do
  if [[ -d "$rv/bin" ]]; then
    PATH="$rv/bin:$PATH"
  fi
  export PATH
done

# Rust ########################################################################
if [[ -f "$HOME/.cargo/env" ]]; then
  # shellcheck source=/dev/null
  . "$HOME/.cargo/env"
fi
if command -v rustup > /dev/null; then
  eval "$(rustup completions bash rustup)"
  eval "$(rustup completions bash cargo)"
else
  for _rust_toolchain in "$HOME"/.rustup/toolchains/stable-*; do
    if [[ -f "$_rust_toolchain/etc/bash_completion.d/cargo" ]]; then
      # shellcheck source=/dev/null
      . "$_rust_toolchain/etc/bash_completion.d/cargo"
      break
    fi
  done
fi
unset _rust_toolchain

# .NET Core ###################################################################
if command -v dotnet > /dev/null; then
  # https://docs.microsoft.com/dotnet/core/tools/enable-tab-autocomplete#bash
  _dotnet_bash_complete() {
    local word=${COMP_WORDS[COMP_CWORD]}

    local completions
    completions="$(dotnet complete --position "${COMP_POINT}" "${COMP_LINE}" 2>/dev/null)"
    # shellcheck disable=SC2181
    if [ $? -ne 0 ]; then
      completions=""
    fi

    COMPREPLY=()
    while IFS='' read -r line; do
      COMPREPLY+=("$line")
    done < <(compgen -W "$completions" -- "$word")
  }
  complete -f -F _dotnet_bash_complete dotnet
else
  echo "dotnet-sdk (i.e., .NET Core SDK) is not installed" > /dev/stderr
fi

# Prompt (PS1) ################################################################
if command -v starship > /dev/null; then
  eval "$(starship init bash)"
else
  echo "starship is not installed; fallback to bare PS1/PS2 & vcprompt..."
  PS1='\[\e[0;35m\]\u\[\e[m\]'
  if [[ -n "$SSH_CLIENT" || -n "$SSH_CLIENT" || -n "$SSH_TTY" ]]; then
    PS1="$PS1"'\[\e[2;3m\]@\[\e[2;91m\]\h\[\e[m\]'
  fi
  PS1="$PS1"' \[\e[2;3m\]\w\[\e[m\] '
  if command -v vcprompt > /dev/null; then
    export VCPROMPT_FORMAT='<%b%m%u>'
    PS1="$PS1"'\[\e[0;34m\]$(vcprompt)'
  else
    echo "vcprompt is not installed" > /dev/stderr
  fi
  PS1="$PS1"'\[\e[m\]\[\e[1;32m\]\$\[\e[m\] '
  export PS1
fi

# iTerm #######################################################################
if [[ "$TERM_PROGRAM" = "iTerm.app" ]]; then
  if [[ -e "$HOME/.iterm2_shell_integration.bash" ]]; then
    # shellcheck source=/dev/null
    . "$HOME/.iterm2_shell_integration.bash"
  else
    {
      echo "$HOME/.iterm2_shell_integration.bash file does not exist."
      echo "See also: iTerm2 -> Install Shell Integration"
    } > /dev/stderr
  fi
fi

# fzf #########################################################################
if command -v rg > /dev/null; then
  export FZF_DEFAULT_COMMAND="rg --files"
fi

if [[ -f "$HOME/.fzf.bash" ]]; then
  # shellcheck source=/dev/null
  . "$HOME/.fzf.bash"
fi

# Werkzeug & Flask ############################################################
export WERKZEUG_DEBUG_PIN=off

# Heroku ######################################################################
if command -v heroku > /dev/null; then
  eval "$(heroku autocomplete:script bash)"
fi

# Libplanet CLI Tools #########################################################
if command -v planet > /dev/null; then
  eval "$(planet --completion bash)"
fi

# Git Delta <https://github.com/dandavison/delta> #############################
if ! command -v delta > /dev/null; then
  echo "delta (Git Delta) is not installed" >&2
fi

# User-defined commands #######################################################
if [[ -f "$HOME/.bash_profile_fn" ]]; then
  # shellcheck source=/dev/null
  . "$HOME/.bash_profile_fn"
fi

# Overrides ###################################################################
if [[ -f "$HOME/.bash_profile_extra" ]]; then
  # shellcheck source=/dev/null
  . "$HOME/.bash_profile_extra"
fi

# vim: set ai et ts=2 sw=2 ss=2 :
