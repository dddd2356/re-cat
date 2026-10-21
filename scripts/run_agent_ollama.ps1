# Re-cat — Windows PowerShell용 로컬 Ollama 실행기.
# macOS/Linux의 run_agent_ollma.sh와 같은 역할이다.
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$python = Join-Path $projectRoot ".venv-win\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw ".venv-win이 없습니다. python -m venv .venv-win 후 의존성을 설치하세요."
}
try {
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:11434/api/tags | Out-Null
} catch {
    throw "Ollama가 실행 중이 아닙니다. Ollama 앱/서비스를 먼저 시작하세요."
}

$fast = if ($env:OLLAMA_FAST_MODEL) { $env:OLLAMA_FAST_MODEL } else { "qwen3:0.6b" }
$quality = if ($env:OLLAMA_QUALITY_MODEL) { $env:OLLAMA_QUALITY_MODEL } else { "qwen3:8b" }
$embed = if ($env:OLLAMA_EMBED_MODEL) { $env:OLLAMA_EMBED_MODEL } else { "bge-m3" }
$installed = (& ollama list | Out-String)
foreach ($model in @($fast, $quality, $embed)) {
    if ($installed -notmatch "(?m)^$([regex]::Escape($model))\s") {
        throw "Ollama 모델 '$model'이 없습니다. ollama pull $model 을 실행하세요."
    }
}

$env:LLM_BACKEND = "ollama"
Write-Host "Re-cat 시작: http://127.0.0.1:8100 (fast=$fast, quality=$quality, embedding=$embed)"
& $python -m backend.app.main
