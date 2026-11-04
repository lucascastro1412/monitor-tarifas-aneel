# Roda o Monitor de Tarifas ANEEL no PC (IP brasileiro) e envia dados e paineis ao GitHub.
# O push na main dispara o workflow, que republica o painel no GitHub Pages e abre uma Issue para
# cada alerta novo em data/alertas/.
#
# Uso (PowerShell, na pasta do repositorio):
#   .\rodar_local.ps1                  # coleta, commit e push
#   .\rodar_local.ps1 -ForcarPainel    # regera Excel e HTML mesmo sem mudancas
#   .\rodar_local.ps1 -SemPush         # coleta e commit, sem push
#
# Requisitos: Python 3.11+ (comando "py" ou "python", ou o caminho absoluto em $pythonFallback)
# com requirements.txt instalado, e git ja
# autenticado no GitHub (o push nao pode pedir senha se rodar pelo Agendador de Tarefas).
# Log de cada execucao: logs\rodar_local_<data>.log (pasta ignorada pelo git).

param(
    [switch]$ForcarPainel,
    [switch]$SemPush
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

$logs = Join-Path $repo "logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$log = Join-Path $logs ("rodar_local_{0:yyyy-MM-dd_HHmm}.log" -f (Get-Date))
Start-Transcript -Path $log -Append | Out-Null

function Invoke-Git {
    & git @args
    if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') falhou (codigo $LASTEXITCODE)" }
}

$codigo = 0
try {
    Write-Host "== Monitor Tarifas ANEEL (local) - $(Get-Date -Format 'dd/MM/yyyy HH:mm') =="

    # Traz commits novos (ex.: execucoes anteriores) antes de gerar os arquivos.
    Invoke-Git pull --rebase --autostash

    # Python: "py" ou "python" do PATH; se nenhum estiver no PATH (ex.: Agendador de Tarefas com
    # ambiente reduzido), usa o interpretador absoluto em que o requirements.txt foi instalado.
    $pythonFallback = "C:\Users\LucasSilvaCastro\AppData\Local\Python\pythoncore-3.14-64\python.exe"
    if (Get-Command py -ErrorAction SilentlyContinue) { $python = "py" }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $python = "python" }
    elseif (Test-Path $pythonFallback) { $python = $pythonFallback }
    else { throw "Python nao encontrado (nem py, nem python no PATH, nem $pythonFallback)" }
    Write-Host "Python: $python"
    $argsPy = @("monitor_tarifas.py")
    if ($ForcarPainel) { $argsPy += "--forcar-painel" }
    & $python @argsPy
    if ($LASTEXITCODE -ne 0) { throw "monitor_tarifas.py terminou com codigo $LASTEXITCODE" }

    Invoke-Git add -A data
    foreach ($f in @("Painel_Tarifas_ANEEL.xlsx", "Painel_Tarifas_ANEEL.html")) {
        if (Test-Path $f) { Invoke-Git add $f }
    }
    & git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Nada novo para salvar."
    } else {
        $agora = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mmZ")
        Invoke-Git commit -m "Monitor tarifas ANEEL (local): atualizacao $agora"
        if ($SemPush) {
            Write-Host "Commit feito; push nao enviado (-SemPush)."
        } else {
            Invoke-Git pull --rebase
            Invoke-Git push
            Write-Host "Push enviado; o GitHub Actions vai republicar o painel no Pages."
        }
    }

    # Alerta de nova homologacao: o monitor grava alerta.md e data/alertas/<AAAA-MM-DD_HHMM>.md; este
    # ultimo vai no commit acima e, no push, o workflow abre a Issue (nao precisa do GitHub CLI no PC).
    if (Test-Path "alerta.md") {
        if ($SemPush) { Write-Host "ALERTA gerado (data/alertas/); a Issue so abre depois do push." }
        else { Write-Host "ALERTA gerado e enviado; o workflow abre a Issue no GitHub." }
    }
} catch {
    Write-Host "ERRO: $_"
    $codigo = 1
} finally {
    Stop-Transcript | Out-Null
}
exit $codigo
