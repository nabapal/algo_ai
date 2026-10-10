param(
    [string]$SshKey = (Join-Path $env:USERPROFILE 'Downloads\ssh-key-2026-10-03.key'),
    [string]$Remote = 'opc@130.210.30.223',
    [string]$RemoteDir = '/home/opc/algo_ai'
)

$ErrorActionPreference = 'Stop'
$LocalRoot = $PSScriptRoot
$RequiredFiles = @(
    'app.py',
    'app_config.py',
    'ai_sentiment.py',
    'engine.py',
    'option_signal.py',
    'decision.py',
    'fyers_client.py',
    'trading_journal.py',
    'requirements.txt',
    'settings.example.json',
    'run.sh'
)
$RequiredDirectories = @('templates', 'static')

foreach ($tool in @('ssh', 'scp')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Required command '$tool' was not found. Install the Windows OpenSSH Client and retry."
    }
}
if (-not (Test-Path -LiteralPath $SshKey -PathType Leaf)) {
    throw "SSH private key not found: $SshKey"
}
foreach ($relativePath in $RequiredFiles + $RequiredDirectories) {
    $sourcePath = Join-Path $LocalRoot $relativePath
    if (-not (Test-Path -LiteralPath $sourcePath)) {
        throw "Required deployment path not found: $sourcePath"
    }
}

$deployId = [guid]::NewGuid().ToString('N')
$remoteStage = "/tmp/algo-ai-deploy-$deployId"
if ($Remote -match "['`r`n]" -or $RemoteDir -match "['`r`n]") {
    throw 'Remote and RemoteDir must not contain single quotes or newlines.'
}
$remoteStageQuoted = $remoteStage
$remoteDirQuoted = $RemoteDir
$remoteScriptPath = Join-Path ([System.IO.Path]::GetTempPath()) "algo-ai-deploy-$deployId.sh"

$remoteScript = @'
#!/usr/bin/env bash
set -euo pipefail
stage="$1"
app_dir="$2"
mkdir -p "$app_dir/logs"
for file in app.py app_config.py ai_sentiment.py engine.py option_signal.py decision.py fyers_client.py trading_journal.py requirements.txt settings.example.json run.sh; do
    test -f "$stage/$file"
done
test -d "$stage/templates"
test -d "$stage/static"

for file in app.py app_config.py ai_sentiment.py engine.py option_signal.py decision.py fyers_client.py trading_journal.py requirements.txt settings.example.json run.sh; do
    cp -f "$stage/$file" "$app_dir/$file"
done
for directory in templates static; do
    mkdir -p "$app_dir/$directory"
    cp -a "$stage/$directory/." "$app_dir/$directory/"
done
chmod +x "$app_dir/run.sh"

# Stop only this app's Python process. This avoids killing the deployment shell.
pids="$(pgrep -f '[p]ython3 app.py' || true)"
if [ -n "$pids" ]; then
    kill $pids || true
    for attempt in {1..10}; do
        still_running="$(pgrep -f '[p]ython3 app.py' || true)"
        [ -z "$still_running" ] && break
        sleep 1
    done
    still_running="$(pgrep -f '[p]ython3 app.py' || true)"
    if [ -n "$still_running" ]; then
        kill -9 $still_running || true
    fi
fi

log_file="$app_dir/logs/app-$(date +%Y%m%d-%H%M%S).log"
nohup bash -c "cd '$app_dir' && ./run.sh" > "$log_file" 2>&1 < /dev/null &
app_pid=$!
rm -rf -- "$stage"
printf 'Deployment uploaded and restart started. PID=%s\nLog=%s\n' "$app_pid" "$log_file"
'@

try {
    Set-Content -LiteralPath $remoteScriptPath -Value $remoteScript -Encoding Ascii
    Write-Host "Creating remote staging directory: $remoteStage"
    & ssh -i $SshKey $Remote "mkdir -p '$remoteStageQuoted'"
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the remote staging directory.' }

    $sources = @($RequiredFiles + $RequiredDirectories | ForEach-Object { Join-Path $LocalRoot $_ })
    $sources += $remoteScriptPath
    Write-Host 'Uploading the selected application files...'
    & scp -i $SshKey -r @sources "${Remote}:$remoteStage/"
    if ($LASTEXITCODE -ne 0) { throw 'File upload failed; the running application was not restarted.' }

    Write-Host 'Updating files and restarting the application...'
    & ssh -i $SshKey $Remote "bash '$remoteStage/algo-ai-deploy-$deployId.sh' '$remoteStageQuoted' '$remoteDirQuoted'"
    if ($LASTEXITCODE -ne 0) { throw 'Remote deployment or restart command failed. Check the VM and its application log.' }
} finally {
    if (Test-Path -LiteralPath $remoteScriptPath) {
        Remove-Item -LiteralPath $remoteScriptPath -Force
    }
}
