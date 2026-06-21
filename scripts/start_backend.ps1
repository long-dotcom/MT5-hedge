param(
    [int]$BackendPort = 8000
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$BackendDir = Join-Path $Root "backend"
$RunDir = Join-Path $Root ".run"
$BackendPidFile = Join-Path $RunDir "backend.pid"

function Assert-File($Path, $Message) {
    if (-not (Test-Path $Path)) {
        throw $Message
    }
}

function Test-PortInUse($Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

Assert-File $VenvPython "Virtual environment not found. Run install_packages.cmd first."
Assert-File (Join-Path $Root ".env") ".env not found. Run create_env.cmd first."

New-Item -ItemType Directory -Force $RunDir | Out-Null

if (Test-PortInUse $BackendPort) {
    throw "Backend port $BackendPort is already in use. Run stop_project.cmd or close the process manually."
}

$BackendArgs = @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command",
    "Set-Location '$BackendDir'; & '$VenvPython' -m uvicorn app.main:app --host 127.0.0.1 --port $BackendPort"
)
$BackendProcess = Start-Process powershell.exe -ArgumentList $BackendArgs -PassThru -WindowStyle Normal
$BackendProcess.Id | Set-Content $BackendPidFile

Write-Host "Backend started: http://127.0.0.1:$BackendPort"
Write-Host "Frontend should be served by Nginx from frontend\dist."
