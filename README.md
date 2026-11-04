# Monitor de Tarifas ANEEL (B3, Fio B SCEE e Grupo A)

Agente diário que acompanha as tarifas homologadas pela ANEEL e mantém um painel de comparação.

**Painel online:** https://lucascastro1412.github.io/monitor-tarifas-aneel/ (Excel: https://lucascastro1412.github.io/monitor-tarifas-aneel/Painel_Tarifas_ANEEL.xlsx)

**O que monitora**
- **B3 Convencional, Tarifa de Aplicação:** TUSD, TE e total (R$/MWh), todas as distribuidoras, histórico completo desde 2010.
- **TUSD Fio B, B3 Convencional:** detalhe SCEE (energia compensada, Lei 14.300), só vigências iniciadas a partir de `fiob_ano_inicial` (padrão 01/01/2026).
- **Grupo A (média e alta tensão), Tarifa de Aplicação:** subgrupos de `subgrupos_grupo_a` (padrão A4 e A3), modalidades Verde e Azul, vigências desde `grupo_a_ano_inicial` (padrão 2016): energia (TUSD+TE, R$/MWh) na ponta e fora ponta e demanda (TUSD, R$/kW; Verde: demanda única; Azul: ponta e fora ponta). TUSD Fio B do Grupo A por posto e unidade desde `fiob_ano_inicial`.
- **IPCA mensal** (Banco Central, SGS série 433), para comparar a B3 com a inflação.

**Nomes comerciais (`distribuidoras.csv`)**

A ANEEL ainda usa siglas antigas (ex.: ELETROPAULO para a Enel SP). O arquivo `distribuidoras.csv` (versionado, editável no Excel ou em editor de texto) liga o CNPJ ao nome comercial atual, grupo econômico, UF e região. Colunas: `cnpj, sigla_aneel, nome_comercial, grupo_economico, uf, regiao` (obrigatórias) e `ufs_atendidas, nomes_anteriores, razao_social, fonte_uf` (informativas). O painel mostra `ENEL SP (ELETROPAULO)` quando o nome comercial difere da sigla, e a busca encontra pelo nome, pela sigla atual ou antiga (ex.: CELPE, COELCE, AMPLA), pela UF, pela região e pelo grupo.
- 115 CNPJs (todos os da base B3), 103 ativos. UF de 103 deles vem do INDGER da ANEEL (dataset `indger-indicadores-gerenciais-da-distribuicao`, arquivo "dados comerciais": estado com mais unidades consumidoras ativas, pelo código IBGE do município). As 12 distribuidoras extintas ou incorporadas (CPFL Mococa, EEB, CNEE, CFLO, RGE antiga etc.) foram preenchidas à mão.
- Distribuidora nova sem linha no CSV aparece com o nome da ANEEL e gera aviso no painel; basta acrescentar a linha.
- Aceita CSV separado por vírgula ou ponto e vírgula (Excel pt-BR) e com ou sem BOM.

**Como funciona**
1. A coleta roda no PC, pelo `rodar_local.ps1` (que chama `monitor_tarifas.py`), porque a ANEEL recusa os runners do GitHub Actions. O script faz commit e push de `data/` e dos painéis; o push dispara o workflow `Monitor Tarifas ANEEL`, que publica o painel no GitHub Pages e abre a Issue de cada alerta novo. Para rodar todo dia, veja "Agendar a coleta diária".
2. A fonte são os **arquivos Parquet completos** da ANEEL. Da API CKAN o script usa só o `package_show`, para achar a URL e o `last_modified` de cada arquivo:
   - B3: Parquet do dataset `tarifas-distribuidoras-energia-eletrica` (resource `9af99ef4-...`, ~2,6 MB);
   - Fio B: Parquet anual do dataset `componentes-tarifarias` (~25 a 58 MB cada), dos anos `fiob_ano_inicial - 2` em diante. Anos novos (ex.: 2027) entram sozinhos quando a ANEEL publicar.
   - Grupo A: os mesmos dois Parquets. Cada arquivo é baixado UMA vez por execução e gera os recortes da B3 e do Grupo A.
3. Cada Parquet só é baixado de novo quando o `last_modified` muda. O download é em streaming para uma pasta temporária (timeout de 90 s por bloco, limite de 15 min), o arquivo é lido com `pyarrow` (só as colunas e linhas do recorte) e apagado. Fica em `data/` só o recorte filtrado (em ordem fixa, para o git só ver diferença quando o conteúdo muda): `recorte_tarifas_b3.csv`, `recorte_fiob_<ano>.csv`, `recorte_tarifas_grupo_a.csv` e `recorte_fiob_a_<ano>.csv`.
4. O script normaliza, compara com o snapshot (`data/tarifas_b3.csv`, `data/fiob_b3.csv`, `data/tarifas_grupo_a.csv`, `data/fiob_grupo_a.csv`) pela chave de negócio (CNPJ, REH, vigência, subgrupo, modalidade, base, classe, subclasse, detalhe, posto, unidade, acessante) e, se houver nova REH/Despacho ou valor alterado:
   - registra em `data/log_mudancas.csv`;
   - regera `Painel_Tarifas_ANEEL.xlsx` e `Painel_Tarifas_ANEEL.html` (antes `Painel_Tarifas_B3.*`) e salva no repositório;
   - grava o alerta em `alerta.md` (local, fora do git) e em `data/alertas/<AAAA-MM-DD_HHMM>.md` (entra no commit); no push, o workflow abre uma **Issue** no GitHub com a tabela de mudanças (TUSD, TE, total, variação %, Fio B e Fio B efetivo) e a seção "Grupo A": uma linha por distribuidora, subgrupo e modalidade, com energia fora ponta, energia ponta e demanda e a variação de cada uma contra a vigência anterior. O GitHub manda a Issue por e-mail.
   - O log grava a série do Grupo A como `Grupo A TUSD+TE A4 Verde`, `Fio B Grupo A A3 Azul` etc.
5. IPCA: cache em `data/ipca_bcb.json`, baixado de novo só quando o mês muda (enquanto o IPCA do mês anterior não sai, no máximo 1 tentativa por dia). Timeout de 90 s, 3 tentativas. Se a API do Banco Central falhar, usa o cache e avisa no painel; sem cache, as comparações com a inflação ficam vazias e a execução segue.
6. O painel também é regerado quando a vigência atual de alguma distribuidora muda (uma vigência começa ou termina), quando o calendário de reajustes muda ou quando chega IPCA novo, mesmo sem mudança na base.
7. Falhas:
   - download ou leitura de um Parquet falhou (ou recorte novo com queda > 20%): usa o recorte salvo, avisa no log e no painel (`estado.json`, `avisos_fonte`) e segue;
   - sem recorte salvo, recorte B3 vazio ou snapshot com queda > 20%: nada é sobrescrito, a execução falha e o GitHub avisa por e-mail.

**Por que ler o Parquet de anos anteriores no Fio B**

O arquivo anual de componentes é organizado pelo processo tarifário, não pelo início da vigência. O de 2025 traz vigências que começam em 01/01/2026 (a REH é dividida na virada do escalonamento da Lei 14.300) e o de 2024 traz a CEA de 01/01/2026 a 12/04/2026 (REH 3.430, de 12/2024). Por isso o script lê os arquivos de `fiob_ano_inicial - 2` em diante e filtra por `DatInicioVigencia >= <ano>-01-01`.

**Painéis**

| Painel / aba | Conteúdo |
|---|---|
| `Painel_Tarifas_ANEEL.html` | Painel interativo (arquivo único, abre no navegador), em abas. Gráficos via Chart.js (CDN, precisa de internet) |
| Visão geral | KPIs (mediana da B3, valor do kWh compensado mediano, maior e menor, B3 acumulada desde 2016 x IPCA, Fio B, reajustes próximos), avisos de qualidade, índice da B3 nacional x IPCA (01/01/2016 = 100), próximos reajustes e os 10 maiores e 10 menores valores do kWh compensado |
| Distribuidora | Uma distribuidora (padrão ENEL SP): ficha (sigla, nomes anteriores, UF, grupo, razão social), tarifa atual, Fio B, valor do kWh compensado, posição no ranking, último e próximo reajuste, posição contra as medianas nacional, da região e da UF, composição TUSD x TE x Fio B, acumulado desde 2016, 2020, 12 meses e último reajuste x IPCA (com aumento real), gráfico da B3 contra a B3 de 2016 corrigida pelo IPCA e vigências |
| Comparador | 2 a 4 distribuidoras (padrão ENEL SP, LIGHT, ENEL RJ e CPFL PIRATININGA): tabela lado a lado (inclui linhas A4 Verde), composição TUSD x TE (com Fio B), valor do kWh compensado, B3 anual e Fio B por vigência |
| Grupo A | Seletores de subgrupo (A4/A3) e modalidade (Verde/Azul), padrão A4 Verde. KPIs (medianas de energia FP, energia P, demanda, variação do último reajuste, valor compensado FP e Fio B), matriz distribuidora x ano com heatmap (energia FP, energia P, demanda), histórico das distribuidoras do comparador, relação ponta / fora ponta, ranking por métrica, simulador Verde x Azul (demanda kW, consumo MWh/mês e % na ponta; custo mensal sem impostos, bandeiras e ultrapassagem, com ponto de equilíbrio), valor do kWh compensado (FP e P) e Fio B por posto e unidade. A ficha da aba Distribuidora ganhou o bloco Grupo A |
| Rankings | Ranking (gráfico e tabela completa) por valor do kWh compensado, % da tarifa, B3 total, Fio B, Fio B / TUSD, último reajuste, acumulado e aumento real; matriz distribuidora x ano com heatmap |
| Regiões | Medianas por região e por UF (B3, valor compensado, Fio B, efetivo, acumulado, aumento real), em barras e mapa de calor |
| Calendário | Vigências que terminam nos próximos 90 dias, com a tarifa atual e a nova homologação, se já publicada |
| Mudanças / Metodologia | Log do agente e regras de cálculo |
| `Painel_Tarifas_ANEEL.xlsx` | Abas abaixo (nome comercial nas matrizes) |
| Leia-me | Metodologia, avisos de qualidade e data da última atualização |
| Comparativo | Uma linha por distribuidora com tudo do comparador (vigência, TUSD, TE, total, Fio B, efetivo, valor compensado, reajustes, acumulados, CAGR, rankings, diferença contra a mediana) |
| Valor kWh Compensado | Ranking das ativas pelo valor do kWh compensado (GD II) |
| Acumulado x IPCA | B3 desde 2016, desde 2020, 12 meses e último reajuste, com CAGR, IPCA e aumento real |
| Regioes UF | Medianas por Brasil, região e UF |
| Calendario | Próximos reajustes (90 dias) |
| Distribuidoras | O mapa `distribuidoras.csv` com o rótulo usado no painel |
| B3 Total Anual / B3 Var % Anual | Distribuidora x ano (desde 2010), TUSD+TE e variação |
| FioB SCEE Anual / Efetivo | Distribuidora x ano, desde 2026: Fio B SCEE e efetivo (x % Lei 14.300) |
| FioB SCEE Ranking | Variação entre a vigência mais recente e a anterior da mesma distribuidora, desde 2026 |
| B3 Vigencias / FioB SCEE Vigencias | Todas as vigências com variação contra a anterior |
| Grupo A Atual | Uma linha por distribuidora, subgrupo e modalidade: vigência atual, TUSD e TE da energia FP e P, demanda, relações P/FP, último reajuste, Fio B por posto e unidade, efetivo e valor do kWh compensado |
| Grupo A Anual | Distribuidora x ano (desde `grupo_a_ano_inicial`), com colunas de subgrupo, modalidade e métrica (energia FP, energia P, demanda, demanda FP) |
| Grupo A Vigencias / Grupo A Fio B | Todas as vigências com variação contra a anterior; Fio B desde `fiob_ano_inicial` |
| Log Mudancas | Tudo o que o agente detectou, mais recente primeiro |

**Regras do Fio B no painel**
- Ranking do Fio B: vigência mais recente contra a anterior da mesma distribuidora, dentro do período desde `fiob_ano_inicial`. Sem vigência anterior no período, a distribuidora fica fora.
- Fio B SCEE <= 1 R$/MWh é tratado como suspeito (ex.: CERGAPA, CERGRAL e CERSAD nas REHs de 30/09/2026): aparece no aviso de qualidade e fica fora do ranking, da mediana e da média.
- Fio B efetivo = Fio B SCEE x escalonamento do ano de início da vigência (2026: 60%).

**Regras do Grupo A**
- Linha usada: classe, subclasse e acessante "Não se aplica"; detalhe normal (o recorte também guarda o SCEE; APE, cooperativas e "<500GWh" ficam fora). Postos sazonais antigos (seca/úmida) ficam fora.
- A Verde só existe para A3a, A4 e AS: o A3 só tem Azul (49 CNPJs desde 2016, 45 ativos, contra 114 e 103 no A4).
- Fio B na base: energia fora ponta = 0 (Verde e Azul), energia ponta = 0 na Azul; na Verde a energia ponta carrega o custo de rede da ponta (ex.: ENEL SP 486,45 R$/MWh). Demanda em R$/kW. O SCEE só existe na energia e é igual ao normal.
- Valor do kWh compensado = energia (TUSD+TE) - Fio B efetivo da energia (Fio B R$/MWh x % da Lei 14.300). No A4 Verde fora ponta é a própria energia (ENEL SP 452,50). A demanda não é compensada.
- Na Azul, 52 das 103 distribuidoras A4 (quase todas cooperativas) têm TE igual na ponta e fora ponta: relação P/FP 1,00x.
- Para incluir A3a ou A2: acrescentar em `subgrupos_grupo_a`. O recorte é baixado de novo e as linhas do subgrupo novo entram como linha de base, sem alerta (o escopo anterior fica em `estado.json`, `escopo_grupo_a`). O mesmo vale para baixar `grupo_a_ano_inicial`.

**Comparações (painel e Excel)**
- Vigência atual: a última com início até a data do painel. Ativa: vigência atual ainda válida ou vencida há no máximo 180 dias (rankings e medianas usam só as ativas).
- Valor do kWh compensado (GD II) = B3 total - Fio B efetivo. Ex.: ENEL SP 2026 = 789,38 - 127,25 (212,08 x 60%) = 662,13 R$/MWh (83,9% da tarifa). Sem tributos, bandeira e custo de disponibilidade. Para GD III o valor é menor (100% do Fio B, 40% do Fio A, TFSEE e P&D/EE), o que não está modelado.
- Último reajuste: tarifa atual contra o patamar anterior (a divisão de 01/01 do Fio B, sem mudança na B3, não conta como reajuste). IPCA do período: do início do patamar anterior até o mês anterior ao reajuste.
- Acumulado e CAGR: B3 atual contra a B3 vigente em 01/01/2016, 01/01/2020 e 12 meses antes; IPCA até o último mês divulgado; aumento real = (1 + B3) / (1 + IPCA) - 1.
- TE com detalhe SCEE: a base de tarifas traz uma linha B3 SCEE com a TUSD igual e a TE bem menor (ENEL SP 2026: 316,96 x 37,36). É a TE sem as parcelas de custo de energia (TE_ENERGIA, Angra, Itaipu), ou seja, TUSD + TE SCEE = componentes não associadas ao custo da energia (Lei 14.300, art. 17). Não entra nos cálculos; ver docs.

**Configuração (`config.json`)**
- `distribuidoras_portfolio`: lista de nomes (ex.: `["LIGHT", "CEMIG", "ENEL RJ"]`). As distribuidoras da lista aparecem destacadas com ★ no painel e nos alertas. Vazio = nenhum destaque.
- `limiar_alerta_pct`: só abre Issue quando a variação for igual ou maior que esse % (0 = qualquer mudança). Todas as mudanças vão para o log de qualquer forma.
- `fiob_ano_inicial`: primeiro ano de vigência do Fio B, da B3 e do Grupo A (padrão 2026). Mudar o valor força novo download dos recortes do Fio B; vigências anteriores saem do snapshot sem gerar alerta.
- `subgrupos_grupo_a`: subgrupos do Grupo A (padrão `["A4", "A3"]`; aceita A3a, A2, A1, AS). Lista vazia desliga o Grupo A.
- `grupo_a_ano_inicial`: primeiro ano de vigência das tarifas do Grupo A (padrão 2016; o recorte desde 2016 tem ~14.600 linhas e ~3,3 MB).
- `painel_url`: link do painel online citado no rodapé do alerta (padrão `https://lucascastro1412.github.io/monitor-tarifas-aneel/`). A variável de ambiente `PAINEL_URL` tem prioridade.

**Dependências:** `requests`, `pandas`, `openpyxl` e `pyarrow`.

**Rodar localmente**
```
pip install -r requirements.txt
python monitor_tarifas.py
python monitor_tarifas.py --forcar-painel   # regera Excel e HTML mesmo sem mudança
python monitor_tarifas.py --so-painel       # offline: regera Excel e HTML a partir de data/, sem rede e sem alterar snapshot, log ou estado
python monitor_tarifas.py --so-ipca         # só atualiza o cache do IPCA (data/ipca_bcb.json)
python monitor_tarifas.py --dir <pasta>     # usa outra pasta base (data/, config.json, painéis), útil para testes
```

Tempos medidos em 03/11/2026: primeira execução 57 s e 109,6 MB baixados (B3 2,6 MB + Fio B 2024, 2025 e 2026); execução com cache válido 6 s, sem download.
Em 04/11/2026, com o Grupo A: primeira execução (linha de base do Grupo A) 89 s e 109,6 MB; execução com cache válido 14 s, sem download.

**Publicação no GitHub Pages**

Em toda execução do workflow (push de `data/` ou dos painéis, ou Run workflow), ele monta a pasta `site/` com `index.html` (cópia de `Painel_Tarifas_ANEEL.html`) e `Painel_Tarifas_ANEEL.xlsx`, e publica no GitHub Pages num job `deploy` separado. Assim o site sempre mostra o painel atual. O link "Baixar Excel" no topo do painel aponta para o `.xlsx` ao lado (funciona no site e na pasta local). A Issue de alerta traz o link do site (variável `PAINEL_URL` no workflow; na coleta local, `painel_url` do `config.json`).

**Ativação (uma vez)**
1. Settings → Pages → Build and deployment → Source: **GitHub Actions**.
2. No PC, na pasta do repositório: `pip install -r requirements.txt` e confira que `git push` funciona sem pedir senha.
3. Rode `.\rodar_local.ps1` no PowerShell. A primeira execução cria a linha de base em `data/` (sem alerta), gera os painéis, faz commit e push; o push publica o site.
4. Alertas por e-mail: veja "Alertas (Issues e e-mail)" abaixo. Não precisa do GitHub CLI no PC.

**Alertas (Issues e e-mail)**

- Quando há mudança, o `monitor_tarifas.py` grava `alerta.md` (cópia local, ignorada pelo git) e `data/alertas/<AAAA-MM-DD_HHMM>.md` (mesmo conteúdo, entra no commit da coleta).
- No push, o workflow procura arquivos **adicionados** em `data/alertas/` entre o commit anterior (`github.event.before`) e o novo, e abre uma Issue para cada um: título = primeira linha do arquivo, corpo = arquivo + rodapé `Arquivo do alerta: data/alertas/...`, label `tarifa-aneel`. Usa o `gh` do runner com o token do próprio workflow; funciona com a coleta no Actions desligada. Apagar ou editar um arquivo de `data/alertas/` não abre Issue.
- Sem duplicata: antes de abrir, o workflow procura o caminho do arquivo no corpo das Issues da label `tarifa-aneel` (abertas e fechadas). Re-run do mesmo commit não abre outra.
- E-mail: a Issue é aberta pelo `github-actions[bot]`, então o GitHub avisa quem acompanha o repositório. Por padrão, o GitHub faz você acompanhar (**Watch: All Activity**) os repositórios que você cria na sua conta, e as notificações de "Watching" vão por e-mail se o e-mail estiver ligado. Para conferir:
  1. No repositório, botão **Watch**: deve estar em **All Activity** ou em **Custom** com **Issues** marcado. "Participating and @mentions" ou "Ignore" não mandam a Issue.
  2. Em https://github.com/settings/notifications, em **Subscriptions → Watching**, marque **Email**. O e-mail vai para o endereço padrão de notificação (ou o roteamento por organização, se houver).
  3. Opcional: filtrar no cliente de e-mail por `[lucascastro1412/monitor-tarifas-aneel]` no assunto.

**Coleta no PC (`rodar_local.ps1`) e papel do GitHub Actions**

O portal `dadosabertos.aneel.gov.br` (API e Parquets, tudo no mesmo domínio) costuma recusar conexões de data centers fora do Brasil, como os runners do GitHub Actions (`SSLEOFError UNEXPECTED_EOF` no `package_show`). A recusa é intermitente (em 04/11/2026 uma execução manual no Actions passou), então não dá para depender dela: a coleta roda num IP brasileiro, no PC.

- `.\rodar_local.ps1` faz `git pull`, roda o monitor, faz commit de `data/` e dos painéis e dá `git push`. Opções: `-ForcarPainel` e `-SemPush`. Log em `logs\`.
- O workflow não tem cron. Ele roda no push na `main` que mude `data/` (inclui `data/alertas/`) ou os painéis (o push do `rodar_local.ps1`) e no Run workflow manual, e nesses casos monta o site com o que está commitado, publica no Pages e abre as Issues dos alertas novos.
- Coleta no Actions fica desligada por padrão. Ela só roda no Run workflow com "Coletar na ANEEL" marcado (padrão desmarcado) ou com a variável do repositório `MODO_COLETA` = `actions` (Settings → Secrets and variables → Actions → Variables). Push nunca coleta. Para voltar a coletar todo dia no Actions (se a ANEEL liberar os runners), defina `MODO_COLETA` = `actions` e descomente o bloco `schedule` no workflow.
- Se a coleta no Actions falhar, o painel já commitado é publicado assim mesmo e o job termina como falho no último step, para o GitHub avisar por e-mail.

**Agendar a coleta diária**

Rodar o `rodar_local.ps1` todo dia às 07:30 pelo Agendador de Tarefas do Windows, como o usuário atual. Cole no PowerShell (não precisa ser administrador):

```powershell
$repo = "C:\Users\LucasSilvaCastro\Projetos\monitor-tarifas-aneel"
$acao = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repo\rodar_local.ps1`"" -WorkingDirectory $repo
$gatilho = New-ScheduledTaskTrigger -Daily -At 07:30
$config = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$usuario = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive
Register-ScheduledTask -TaskName "Monitor Tarifas ANEEL" -Action $acao -Trigger $gatilho -Settings $config -Principal $usuario -Description "Coleta diaria das tarifas ANEEL e push para o GitHub Pages"
```

- `StartWhenAvailable`: se o PC estava desligado às 07:30, roda assim que possível. `RunOnlyIfNetworkAvailable`: só roda com rede. `LogonType Interactive`: roda com o usuário logado, sem guardar senha, usando o login do git já configurado.
- Se `py`/`python` não estiver no PATH da tarefa, o script usa `C:\Users\LucasSilvaCastro\AppData\Local\Python\pythoncore-3.14-64\python.exe` (variável `$pythonFallback`).
- Conferir: `Get-ScheduledTask -TaskName "Monitor Tarifas ANEEL" | Get-ScheduledTaskInfo` (mostra `LastRunTime`, `LastTaskResult` = 0 quando deu certo, e `NextRunTime`). Rodar agora para testar: `Start-ScheduledTask -TaskName "Monitor Tarifas ANEEL"`. Log de cada execução em `logs\`.
- Remover: `Unregister-ScheduledTask -TaskName "Monitor Tarifas ANEEL" -Confirm:$false`.

Detalhes da fonte e armadilhas conhecidas: [docs/aneel-fonte-dados.md](docs/aneel-fonte-dados.md).
