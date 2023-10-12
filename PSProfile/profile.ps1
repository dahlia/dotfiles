if (Get-Command -ErrorAction SilentlyContinue /opt/homebrew/bin/brew) {
  $(/opt/homebrew/bin/brew shellenv) | Invoke-Expression
}

if (Get-Command -ErrorAction SilentlyContinue starship) {
 (&starship init powershell) | Invoke-Expression
}
