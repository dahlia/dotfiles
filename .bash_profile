# macOS #######################################################################
if [[ "$(uname)" = "Darwin" ]]; then
  if [[ -d /usr/local/opt/coreutils ]]; then
    PATH="/usr/local/opt/coreutils/libexec/gnubin:$PATH"
    MANPATH="/usr/local/opt/coreutils/libexec/gnuman:$MANPATH"
  else
    alias ls='ls -GF'
    echo -e "coreutils is not installed"
  fi

  if [[ -f /usr/local/share/bash-completion/bash_completion ]]; then
    # shellcheck source=/dev/null
    . /usr/local/share/bash-completion/bash_completion
  elif [[ -f /usr/local/etc/bash_completion ]]; then
    # shellcheck source=/dev/null
    . /usr/local/etc/bash_completion
    echo -e \
      "bash-completion2 is not installed but bash-completeion is installed"
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

# ls ##########################################################################
if [[ "$(type -t ls)" != "alias" ]]; then
  alias ls='ls --color=auto --indicator-style=slash'
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
else
  echo -e "pyenv is not installed"
fi

# Rust ########################################################################
if [[ -f "$HOME/.cargo/env" ]]; then
  # shellcheck source=/dev/null
  . "$HOME/.cargo/env"
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

# Overrides ###################################################################
if [[ -f "$HOME/.bash_profile_extra" ]]; then
  # shellcheck source=/dev/null
  . "$HOME/.bash_profile_extra"
fi

# vim: set ai et ts=2 sw=2 ss=2 :
