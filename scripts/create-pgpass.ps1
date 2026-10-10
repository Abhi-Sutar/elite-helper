# Creates the libpq password file used by elite-helper so no password appears in any URL, task or config.
# Run as YOUR normal user (not elevated) after setting the role passwords in PostgreSQL:
#     .\scripts\create-pgpass.ps1
# You type both passwords into hidden prompts; they are only written to the file below, readable by you alone.
param(
    [string]$Path = (Join-Path $env:LOCALAPPDATA 'elite-helper\pgpass.conf'),
    [string]$HostName = 'localhost',
    [int]$Port = 5432
)
$ErrorActionPreference = 'Stop'

function Read-Plain($prompt) {
    $secure = Read-Host -AsSecureString -Prompt $prompt
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
# pgpass format: host:port:database:user:password, with \ and : escaped inside fields
function Esc($s) { return ($s -replace '\\', '\\' -replace ':', '\:') }

$writerPw = Read-Plain 'Password for role elite_writer'
$readerPw = Read-Plain 'Password for role elite_reader'
if (-not $writerPw -or -not $readerPw) { throw 'Both passwords are required.' }

$lines = @()
foreach ($db in 'elite', 'elite_dev', 'elite_test') {
    $lines += "$HostName`:$Port`:$db`:elite_writer`:$(Esc $writerPw)"
}
foreach ($db in 'elite', 'elite_dev') {
    $lines += "$HostName`:$Port`:$db`:elite_reader`:$(Esc $readerPw)"
}

New-Item -ItemType Directory -Force -Path (Split-Path $Path) | Out-Null
[IO.File]::WriteAllText($Path, ($lines -join "`n") + "`n", (New-Object Text.UTF8Encoding($false)))
# only the current user may read it
icacls $Path /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
Write-Host "Wrote $Path ($($lines.Count) entries, readable only by $env:USERNAME)."
Write-Host "URL form: postgresql+psycopg://elite_writer@localhost:5432/elite?passfile=$($Path -replace '\\','/')"
