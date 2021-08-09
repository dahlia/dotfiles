# macOS #######################################################################
if [[ "$(uname)" = "Darwin" ]]; then
  if [[ -d /usr/local/opt/coreutils ]]; then
    PATH="/usr/local/opt/coreutils/libexec/gnubin:$PATH"
    MANPATH="/usr/local/opt/coreutils/libexec/gnuman:$MANPATH"
  else
    alias ls='ls -GF'
    echo -e "coreutils is not installed"
  fi

  if [[ -d /usr/local/opt/findutils/libexec ]]; then
    PATH="/usr/local/opt/findutils/libexec/gnubin:$PATH"
    MANPATH="/usr/local/opt/findutils/libexec/gnuman:$MANPATH"
  else
    echo -e "findutils is not installed"
  fi

  if [[ -d /usr/local/opt/grep/libexec ]]; then
    PATH="/usr/local/opt/grep/libexec/gnubin:$PATH"
    MANPATH="/usr/local/opt/grep/libexec/gnuman:$MANPATH"
  else
    echo -e "grep is not installed"
  fi

  if [[ -d /usr/local/opt/gnu-sed/libexec ]]; then
    PATH="/usr/local/opt/gnu-sed/libexec/gnubin:$PATH"
    MANPATH="/usr/local/opt/gnu-sed/libexec/gnuman:$MANPATH"
  else
    echo -e "gnu-sed is not installed"
  fi

  if [[ -d /usr/local/opt/gnu-tar/libexec/gnubin ]]; then
    PATH="/usr/local/opt/gnu-tar/libexec/gnubin:$PATH"
    MANPATH="/usr/local/opt/gnu-tar/libexec/gnuman:$MANPATH"
  else
    echo -e "gnu-tar is not installed"
  fi

  if [[ -r /usr/local/etc/profile.d/bash_completion.sh ]]; then
    export BASH_COMPLETION_COMPAT_DIR=/usr/local/etc/bash_completion.d
    . /usr/local/etc/profile.d/bash_completion.sh
  else
    echo -e "bash-completion2 is not installed"
  fi

  export PATH
  export MANPATH
  export LANG=en_US.UTF-8
  export LC_CTYPE=en_US.UTF-8
fi

# User-local directories ######################################################
export PATH="$HOME/.local/bin:$PATH"

# exa (ls alt) #################################################################
if command -v exa; then
  alias ls='exa --classify --group-directories-first'
else
  if [[ "$(type -t ls)" != "alias" ]]; then
    alias ls='ls --color=auto --indicator-style=slash'
  fi
  echo "exa is not installed"
fi

# bat (cat alt) ###############################################################
if command -v bat; then
  alias cat=bat
else
  echo "bat is not installed"
fi

# Neovim & vi mode ############################################################
if command -v nvim > /dev/null; then
  export EDITOR=nvim
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
  echo -e "nvim (i.e., Neovim) is not installed"
fi

set editing-mode vi
set -o vi

# Haskell Stack ###############################################################
if command -v stack > /dev/null; then
  eval "$(stack --bash-completion-script stack)"
else
  echo -e "stack (i.e., Haskell Stack) is not installed"
fi

# Python & pyenv ##############################################################
if command -v pyenv > /dev/null; then
  eval "$(pyenv init -)"
  if command -v pyenv-virtualenv-init > /dev/null; then
    eval "$(pyenv virtualenv-init -)";
  else
    echo -e "pyenv-virtualenv is not installed"
  fi
fi

if command -v pipx > /dev/null; then
  eval "$(register-python-argcomplete pipx)"
else
  echo -e "pipx is not installed"
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
  echo -e "dotnet-sdk (i.e., .NET Core SDK) is not installed"
fi

# Prompt (PS1) ################################################################
PS1='\[\e[0;35m\]\u\[\e[m\]'
if [[ -n "$SSH_CLIENT" || -n "$SSH_CLIENT" || -n "$SSH_TTY" ]]; then
  PS1="$PS1"'\[\e[2;3m\]@\[\e[2;91m\]\h\[\e[m\]'
fi
PS1="$PS1"' \[\e[2;3m\]\w\[\e[m\] '
if command -v vcprompt > /dev/null; then
  export VCPROMPT_FORMAT='<%b%m%u>'
  PS1="$PS1"'\[\e[0;34m\]$(vcprompt)'
else
  echo -e "vcprompt is not installed"
fi
PS1="$PS1"'\[\e[m\]\[\e[1;32m\]\$\[\e[m\] '
export PS1

# iTerm #######################################################################
if [[ "$TERM_PROGRAM" = "iTerm.app" ]]; then
  if [[ -e "$HOME/.iterm2_shell_integration.bash" ]]; then
    # shellcheck source=/dev/null
    . "$HOME/.iterm2_shell_integration.bash"
  else
    echo -e "$HOME/.iterm2_shell_integration.bash file does not exist."
    echo -e "See also: iTerm2 -> Install Shell Integration"
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
