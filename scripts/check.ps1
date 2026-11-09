param([switch]$Postgres,[switch]$Docker)
$ErrorActionPreference = 'Stop'
$recodeRoot = Split-Path -Parent $PSScriptRoot
$fnmVersions = Join-Path $env:USERPROFILE '.fnm\node-versions'
$nodeCandidates = @(Get-ChildItem -LiteralPath $fnmVersions -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^v(2[2-9]|[3-9]\d)\.' } |
    Sort-Object { [version]$_.Name.TrimStart('v') } -Descending)
if ($nodeCandidates.Count -gt 0) {
    $selectedNode = Join-Path $nodeCandidates[0].FullName 'installation'
    $env:Path = $selectedNode + ';' + $env:Path
}
$dockerBin = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin'
if (Test-Path -LiteralPath $dockerBin) { $env:Path = $dockerBin + ';' + $env:Path }
if ($Postgres) { $env:DEVFLOW_TEST_DATABASE_URL = 'postgresql+psycopg://devflow:devflow_test@127.0.0.1:55439/devflow_test' }
if ($Docker) { $env:DEVFLOW_RUN_DOCKER_TESTS = '1' }
Push-Location $recodeRoot
try {
    & '.\.venv\Scripts\python.exe' -m pytest backend/tests -q
    if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed' }
    Push-Location pi-service
    try {
        & node --experimental-strip-types --test test/protocol.test.ts test/hosted.test.ts
        if ($LASTEXITCODE -ne 0) { throw 'Pi tests failed' }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Pi bridge compilation failed' }
    } finally { Pop-Location }
} finally { Pop-Location }
