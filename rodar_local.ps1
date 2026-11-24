# Roda o Monitor de Tarifas ANEEL no PC (IP brasileiro) e envia dados e paineis ao GitHub.
# O push na main dispara o workflow, que republica o painel no GitHub Pages e abre uma Issue para
# cada alerta novo em data/alertas/.
#
# Uso (PowerShell, na pasta do repositorio):
#   .\rodar_local.ps1                  # coleta, commit e push
#   .\rodar_local.ps1 -ForcarPainel    # regera Excel e HTML mesmo sem mudancas
#   .\rodar_local.ps1 -SemPush         # coleta e commit, sem push
#   .\rodar_local.ps1 -ForcarImpostos  # roda o script de impostos mesmo se ja rodou hoje
#
# Impostos: os impostos sao atualizados por um script privado da Thopen a partir das faturas; sem ele o painel
# usa o ultimo CSV publicado. O caminho do script fica em config.local.json (nao versionado) ou na variavel
# MONITOR_SCRIPT_IMPOSTOS. Frequencia: no maximo 1 vez por dia (pula se impostos_meta.json for de hoje).
#
# Requisitos: Python 3.11+ (comando "py" ou "python", ou o caminho absoluto em $pythonFallback)
# com requirements.txt instalado, e git ja
# autenticado no GitHub (o push nao pode pedir senha se rodar pelo Agendador de Tarefas).
# Log de cada execucao: logs\rodar_local_<data>.log (pasta ignorada pelo git).

param(
    [switch]$ForcarPainel,
    [switch]$SemPush,
    [switch]$ForcarImpostos
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

    # Impostos (ICMS, PIS e COFINS das faturas): script privado, fora deste repositorio. Caminho em
    # config.local.json ({"script_impostos": "C:\\...\\atualizar_impostos.py"}, nao versionado) ou na variavel
    # MONITOR_SCRIPT_IMPOSTOS. Roda no maximo 1 vez por dia (pula se impostos_meta.json ja foi gerado hoje).
    # Qualquer falha so gera aviso: a coleta da ANEEL segue com o CSV anterior.
    try {
        $scriptImp = $env:MONITOR_SCRIPT_IMPOSTOS
        $cfgLocal = Join-Path $repo "config.local.json"
        if (-not $scriptImp -and (Test-Path $cfgLocal)) {
            $scriptImp = (Get-Content $cfgLocal -Raw -Encoding UTF8 | ConvertFrom-Json).script_impostos
        }
        $metaImp = Join-Path $repo "impostos_meta.json"
        $hojeImp = $false
        if (Test-Path $metaImp) {
            $ger = (Get-Content $metaImp -Raw -Encoding UTF8 | ConvertFrom-Json).gerado_em
            $hojeImp = ([string]$ger).StartsWith((Get-Date -Format "yyyy-MM-dd"))
        }
        if (-not $scriptImp) {
            Write-Host "Impostos: script privado nao configurado; usando o ultimo CSV publicado."
        } elseif (-not (Test-Path $scriptImp)) {
            Write-Host "AVISO impostos: script nao encontrado ($scriptImp); usando o CSV anterior."
        } elseif ($hojeImp -and -not $ForcarImpostos) {
            Write-Host "Impostos: ja atualizados hoje ($ger); pulando."
        } else {
            $env:ACESSO_CH = "1"   # credenciais: so nas variaveis de ambiente do usuario, lidas pelo script
            Write-Host "Impostos: atualizando a partir das faturas..."
            $saidaImp = Join-Path $repo "impostos_distribuidoras.csv"
            $outImp = Join-Path $logs "impostos_stdout.txt"
            $errImp = Join-Path $logs "impostos_stderr.txt"
            $pyExe = (Get-Command $python).Source
            $argImp = @("`"$scriptImp`"", "--saida", "`"$saidaImp`"", "--repo", "`"$repo`"")
            $proc = Start-Process -FilePath $pyExe -ArgumentList $argImp -NoNewWindow -PassThru `
                -RedirectStandardOutput $outImp -RedirectStandardError $errImp
            $null = $proc.Handle  # sem isso o ExitCode pode vir vazio no PowerShell 5.1
            if (-not $proc.WaitForExit(600000)) {
                $proc.Kill()
                Write-Host "AVISO impostos: tempo esgotado (10 min); usando o CSV anterior."
            } else {
                $proc.WaitForExit()
                Get-Content $outImp, $errImp -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" }
                if ($proc.ExitCode -ne 0) {
                    Write-Host "AVISO impostos: script terminou com codigo $($proc.ExitCode); usando o CSV anterior."
                }
            }
        }
    } catch {
        Write-Host "AVISO impostos: $_ ; usando o CSV anterior."
    } finally {
        Remove-Item Env:\ACESSO_CH -ErrorAction SilentlyContinue
    }

    $argsPy = @("monitor_tarifas.py")
    if ($ForcarPainel) { $argsPy += "--forcar-painel" }
    & $python @argsPy
    if ($LASTEXITCODE -ne 0) { throw "monitor_tarifas.py terminou com codigo $LASTEXITCODE" }

    Invoke-Git add -A data
    foreach ($f in @("Painel_Tarifas_ANEEL.xlsx", "Painel_Tarifas_ANEEL.html", "impostos_distribuidoras.csv",
                     "impostos_meta.json")) {
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
