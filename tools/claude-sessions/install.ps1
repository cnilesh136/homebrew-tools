# claude-sessions installer for Windows.
#
#   irm https://raw.githubusercontent.com/cnilesh136/homebrew-tools/main/tools/claude-sessions/install.ps1 | iex
#
# Installs claude_sessions.py plus `cs` and `claude-sessions` launchers into
# ~\bin, and puts that directory on the user PATH if it is not already there.
# Re-running upgrades in place. Uninstall: remove ~\bin\cs.cmd,
# ~\bin\claude-sessions.cmd and ~\bin\claude_sessions.py.

$ErrorActionPreference = 'Stop'

# Overridable so a fork or branch can be installed for testing before merge:
#   $env:CS_REPO='you/homebrew-tools'; $env:CS_REF='my-branch'
$repo    = if ($env:CS_REPO) { $env:CS_REPO } else { 'cnilesh136/homebrew-tools' }
$ref     = if ($env:CS_REF)  { $env:CS_REF }  else { 'main' }
$srcUrl  = "https://raw.githubusercontent.com/$repo/$ref/tools/claude-sessions/claude_sessions.py"
$destDir = Join-Path $env:USERPROFILE 'bin'
$script  = Join-Path $destDir 'claude_sessions.py'

function Fail($msg) {
    Write-Host "  x $msg" -ForegroundColor Red
    exit 1
}

# Run python and return @{ Ok; Output }.
#
# Windows PowerShell 5.1 wraps a native command's stderr lines in ErrorRecords,
# which $ErrorActionPreference='Stop' would escalate into a terminating error
# and dump a raw traceback over our own messages. Relax the preference for the
# call and judge success by exit code only.
function Invoke-Python {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = (& python @args 2>&1 | Out-String)
        return @{ Ok = ($LASTEXITCODE -eq 0); Output = $out.Trim() }
    } catch {
        return @{ Ok = $false; Output = $_.Exception.Message }
    } finally {
        $ErrorActionPreference = $prev
    }
}

Write-Host ''
Write-Host '  claude-sessions' -ForegroundColor Cyan -NoNewline
Write-Host ' - mission control for your Claude Code sessions'
if ($env:CS_REPO -or $env:CS_REF) {
    Write-Host "  source: $repo@$ref" -ForegroundColor Yellow
}
Write-Host ''

# --- Python check ---------------------------------------------------------
# The launchers invoke `python`, so resolve the same way they will.
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Fail @'
Python 3.9+ is required but `python` was not found on PATH.
    Install from https://python.org (tick "Add python.exe to PATH")
    or run:  winget install Python.Python.3.13
'@
}

# Keep this -c payload free of quote characters: PowerShell mangles quotes
# inside a string handed to a native executable.
$ver = Invoke-Python -c "import sys; print(sys.version_info[0], sys.version_info[1])"
if (-not $ver.Ok -or -not $ver.Output) {
    Fail 'Could not run python. Check your Python install.'
}

$major, $minor = $ver.Output.Split(' ')
if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 9)) {
    Fail "Python $major.$minor found, but 3.9 or newer is required."
}
Write-Host "  + Python $major.$minor" -ForegroundColor Green

# --- Download ------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $destDir | Out-Null
try {
    Invoke-WebRequest -UseBasicParsing -Uri $srcUrl -OutFile $script
} catch {
    Fail "Download failed: $($_.Exception.Message)"
}

# Sanity-check what we downloaded before advertising it as installed: a
# truncated download or a captive-portal login page must not pass silently.
if (-not (Invoke-Python -m py_compile $script).Ok) {
    Remove-Item $script -Force -ErrorAction SilentlyContinue
    Fail 'Download is not valid Python (truncated, or a proxy/login page). Aborting.'
}
Write-Host "  + Downloaded to $script" -ForegroundColor Green

# --- Launchers -----------------------------------------------------------
# .cmd shims rather than a PATHEXT/.py association, so `cs` works from
# PowerShell, cmd, and Git Bash without extra setup. %~dp0 keeps the script
# path relative to the shim, so moving ~\bin does not break them.
$shim = @(
    '@echo off'
    'python "%~dp0claude_sessions.py" %*'
)
foreach ($name in 'cs', 'claude-sessions') {
    Set-Content -Path (Join-Path $destDir "$name.cmd") -Value $shim -Encoding ascii
}
Write-Host '  + Installed commands: cs, claude-sessions' -ForegroundColor Green

# --- PATH ----------------------------------------------------------------
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -notlike "*$destDir*") {
    $newPath = if ([string]::IsNullOrWhiteSpace($userPath)) {
        $destDir
    } else {
        $userPath.TrimEnd(';') + ';' + $destDir
    }
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    $pathAdded = $true
}
# Make it usable in this session too, so the smoke test below can run.
if ($env:Path -notlike "*$destDir*") { $env:Path = "$env:Path;$destDir" }

if ($pathAdded) {
    Write-Host "  + Added $destDir to your user PATH" -ForegroundColor Green
} else {
    Write-Host "  + $destDir already on PATH" -ForegroundColor DarkGray
}

# --- Verify --------------------------------------------------------------
# `list` exercises the real path: transcript parsing plus the Windows console
# setup, which is what actually differs on this platform.
$check = Invoke-Python $script list --limit 1
if (-not $check.Ok) { Fail "Installed, but running it failed: $($check.Output)" }

Write-Host ''
Write-Host '  Installed. Commands available:' -ForegroundColor Cyan
Write-Host ''
Write-Host '    cs               ' -ForegroundColor Cyan -NoNewline
Write-Host 'interactive picker: browse, resume, live status'
Write-Host '    cs search "..."  ' -ForegroundColor Cyan -NoNewline
Write-Host 'full-text search every conversation you ever had'
Write-Host '    cs stats         ' -ForegroundColor Cyan -NoNewline
Write-Host 'what your Claude sessions really cost'
Write-Host '    cs help          ' -ForegroundColor Cyan -NoNewline
Write-Host 'the full tour'
Write-Host ''
Write-Host '  Desktop pings when a session finishes or needs you:'
Write-Host '    cs service start' -ForegroundColor Cyan
Write-Host ''
if ($pathAdded) {
    Write-Host '  Open a new terminal first so PATH takes effect.' -ForegroundColor Yellow
    Write-Host ''
}
