Import-Module "$(Split-Path -Parent $PROFILE)/posh-git/src/posh-git.psd1"

Set-PSReadlineKeyHandler -Key Tab -Function Complete

if (Get-Command -ErrorAction SilentlyContinue /opt/homebrew/bin/brew) {
  $(/opt/homebrew/bin/brew shellenv) | Invoke-Expression
}

if (Get-Command -ErrorAction SilentlyContinue starship) {
 (&starship init powershell) | Invoke-Expression
}

# https://learn.microsoft.com/en-us/windows/terminal/tutorials/new-tab-same-directory#powershell-with-starship
function Invoke-Starship-PreCommand {
  $loc = $executionContext.SessionState.Path.CurrentLocation;
  $prompt = "$([char]27)]9;12$([char]7)"
  if ($loc.Provider.Name -eq "FileSystem")
  {
    $prompt += "$([char]27)]9;9;`"$($loc.ProviderPath)`"$([char]27)\"
  }
  $host.ui.Write($prompt)
}
