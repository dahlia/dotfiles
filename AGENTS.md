AGENTS.md
=========

This repository is a Dojang-managed source dotfiles repository.
This document defines the working rules for humans and AI agents who edit it.


Core principles
---------------

 -  Files in this repository are **source configs**; files on the local machine are
    **target configs**.
 -  Sync source -> target with `dojang apply`.
 -  Sync target -> source with `dojang reflect`.
 -  Always validate safely with `--dry-run` first (for example,
    `dojang --dry-run apply`).
 -  `dojang.toml` is the single source of truth. Define path and condition
    branching only in the manifest.
 -  `.dojang/` is device-local internal state. Do not edit it manually and do not
    commit it.


Current repository layout (from manifest)
-----------------------------------------

### Directory routes (`[dirs]`)

 -  `HOME`: `$HOME` (POSIX), `$UserProfile` (Windows)
 -  `HOME.posix`: `$HOME` (POSIX only)
 -  `XDG_CONFIG_HOME`: `${XDG_CONFIG_HOME:-$HOME/.config}` (POSIX),
    `$UserProfile/.config` (Windows)
 -  `AppData`: `${XDG_CONFIG_HOME:-$HOME/.config}` (POSIX), `$AppData` (Windows)
 -  `STACK_ROOT`: `${STACK_ROOT:-$HOME/.stack}` (POSIX),
    `${STACK_ROOT:-$LocalAppData/Programs/stack}` (Windows)
 -  `PSProfile`: `${XDG_CONFIG_HOME:-$HOME/.config}/powershell` (POSIX),
    `$UserProfile/Documents/PowerShell` (Windows)

### Per-file routes (`[files]`)

 -  `gitconfig/.gitconfig_common` -> `$HOME/.gitconfig_common` /
    `$UserProfile/.gitconfig_common`
 -  `gitconfig/.gitconfig.linux` -> `$HOME/.gitconfig` on `non-wsl-linux`
 -  `gitconfig/.gitconfig.wsl` -> `$HOME/.gitconfig` on `wsl`
 -  `gitconfig/.gitconfig.macos` -> `$HOME/.gitconfig` on `macos`
 -  `gitconfig/.gitconfig.windows` -> `$UserProfile/.gitconfig` on `windows`
 -  `.psqlrc` -> POSIX/Windows split with `${PSQLRC:-...}` fallback rules
 -  `Microsoft.WindowsTerminal.json` -> Windows Terminal settings path

### Monikers (`[monikers]`)

 -  `posix`, `linux`, `macos`, `windows`
 -  `wsl`: `os = linux && kernel-release $= '-microsoft-standard-WSL2'`
 -  `non-wsl-linux`: Linux environment that is not WSL2

### Ignore policy (`[ignores]`)

 -  Main routes use `"*"`, so the workflow is centered on explicitly registered
    files.
 -  Ignored target files can still be reflected with `dojang reflect -f`.


Workflow
--------

1.  **Check current state**
     -  `dojang status`
2.  **Choose the edit path**
     -  Already managed file: edit the source file directly
     -  Change made in target file: `dojang reflect <target-path>`
     -  Add new file:
         -  create from target path with `dojang edit <target-path>`, or
         -  create source file under a route directory and run `dojang apply`
3.  **Validate**
     -  `dojang --dry-run apply`
4.  **Apply for real (when needed)**
     -  `dojang apply`


Manifest editing guidelines
---------------------------

 -  Use `[dirs]` when only directory paths differ by environment.
 -  Use `[files]` when the filename itself differs by environment.
 -  Create reusable monikers first, then reference them in routes.
 -  If multiple routes match the same file, the more specific condition wins.
 -  When adding overlapping routes, update `ignores` together to reduce duplicate
    scans and noise.
 -  For hooks, use `command`, `args`, `when`, `working-directory`, and
    `ignore-failure` fields.
     -  In `--dry-run`, hooks are not executed; Dojang prints what would run.


Conflict handling
-----------------

 -  If `dojang apply` stops on conflict, identify targets with `dojang status`.
 -  Inspect detailed differences with `dojang diff`.
 -  Resolution strategies:
     -  Prefer source: `dojang apply -f`
     -  Prefer target: `dojang reflect -f`
     -  Merge manually, then run `dojang apply -f`


Environment simulation
----------------------

 -  To verify behavior on another OS/architecture, use an environment file.
 -  Example:
     -  `dojang env -o dojang-env.toml`
     -  `dojang --dry-run --env-file dojang-env.toml apply`
 -  Global options (`--dry-run`, `--env-file`) must come before the subcommand.


References
----------

 -  https://dojang.dev/start/
 -  https://dojang.dev/manifest/
 -  https://dojang.dev/routing/
 -  https://dojang.dev/hooks/
 -  https://dojang.dev/cookbook/conflicts/
 -  https://dojang.dev/cookbook/simulate-other-environment/
