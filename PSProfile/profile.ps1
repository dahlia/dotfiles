Import-Module "$(Split-Path -Parent $PROFILE)/posh-git/src/posh-git.psd1"

if (Get-Command -ErrorAction SilentlyContinue /opt/homebrew/bin/brew) {
  $(/opt/homebrew/bin/brew shellenv) | Invoke-Expression
}

if (Get-Command -ErrorAction SilentlyContinue starship) {
 (&starship init powershell) | Invoke-Expression
}
