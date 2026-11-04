# Fonte de dados ANEEL - Tarifas e Componentes (validado em 03/11/2026; Grupo A em 04/11/2026)

Base por tras do painel "Luz na Tarifa" (https://portalrelatorios.aneel.gov.br/luznatarifa/basestarifas), publicada no portal CKAN de dados abertos da ANEEL, sem autenticacao.

**Como o monitor usa a fonte (desde 03/11/2026):** so os arquivos PARQUET completos de cada dataset. Da API, so `package_show` (`https://dadosabertos.aneel.gov.br/api/3/action/package_show?id=<dataset>`), para achar a `url` e o `last_modified` de cada arquivo. O `datastore_search` deixou de ser usado (ver armadilhas 7 e 8 da secao 4).

## 1. Tarifas homologadas (TUSD + TE finais)

- Dataset: `tarifas-distribuidoras-energia-eletrica` (id `5a583f3e-1646-4f67-bf0f-69db4203e89e`)
- **Resource PARQUET usado pelo monitor:** `9af99ef4-376a-4493-a075-5e48ab4da51b` (`tarifas-homologadas-distribuidoras-energia-eletrica.parquet`, 2,6 MB, 657.709 linhas, 3 row groups; `last_modified` 2026-11-03T11:09:00).
- Outros resources: CSV com datastore `fcf2906c-7c32-4b9b-a637-054e7a5234f4` (179 MB), XML (647 MB), dicionario PDF.
- Cobertura: vigencias de 2010-02-03 ate 2026-10-23 (fim de vigencia ate 2027).
- Atualizacao: o arquivo e recriado inteiro (na pratica, quase todo dia).

### Esquema do Parquet
`DatGeracaoConjuntoDados` date32, `DscREH` string, `SigAgente` string, `NumCNPJDistribuidora` **int64** (completar com `zfill(14)`), `DatInicioVigencia`/`DatFimVigencia` **date32**, `DscBaseTarifaria`, `DscSubGrupo`, `DscModalidadeTarifaria`, `DscClasse`, `DscSubClasse`, `DscDetalhe`, `NomPostoTarifario`, `DscUnidadeTerciaria`, `SigAgenteAcessante` (string), `VlrTUSD`/`VlrTE` **double**. Mesmos nomes do datastore; textos em NFC ("Tarifa de Aplicação" com acentos compostos).

### Recorte B3
`DscSubGrupo = "B3"`, `DscModalidadeTarifaria = "Convencional"`, `DscBaseTarifaria = "Tarifa de Aplicação"` -> 2.750 linhas distintas (o arquivo repete linhas identicas). Conferido em 03/11/2026: as 2.750 linhas tem a mesma chave e os mesmos valores (6 casas) do recorte que vinha do datastore.

Exemplos: CPFL-PIRATINING, Despacho 3.612 de 16/10/2026, vigencia 2026-10-23 a 2027-10-22, TUSD 430,40 + TE 328,42 = 758,82 R$/MWh; LIGHT SESA, REH 3.571, vigencia 2026-03-15, 578,56 + 302,00 = 880,56 R$/MWh.

## 2. Componentes tarifarias (inclui TUSD_FioB)

- Dataset: `componentes-tarifarias` (id `613e6b74-1a4b-4c48-a231-096815e96bd5`)
- PARTICIONADO POR ANO DO PROCESSO TARIFARIO: 1 CSV (datastore) + 1 Parquet por ano, 2012-2026. Novo ano = novo resource, descoberto pelo nome/URL (`componentes-tarifarias-<ano>.parquet`) no `package_show`.
- Parquets lidos pelo monitor (03/11/2026): 2024 `5a29d53f-b7a6-4436-8497-7de00214d8fb` (24,6 MB), 2025 `2a7a5364-8bd7-4464-9c56-587d77329447` (57,9 MB), 2026 `59c32c89-8e86-4122-a7cf-a0d5fa9d0ff5` (24,5 MB).
- Atualizacao: recargas frequentes; cada arquivo tem o proprio `last_modified`.

### Esquema do Parquet (2024 a 2026)
Mesmos nomes do datastore, tipados: `NumCPFCNPJ` **int64** (`zfill(14)`), `DatInicioVigencia`/`DatFimVigencia`/`DatGeracaoConjuntoDados` **date32**, `VlrComponenteTarifario` **double**. Demais campos string: `DscResolucaoHomologatoria`, `SigNomeAgente`, `DscBaseTarifaria`, `DscSubGrupoTarifario`, `DscModalidadeTarifaria`, `DscClasseConsumidor`, `DscSubClasseConsumidor`, `DscDetalheConsumidor`, `DscPostoTarifario`, `DscUnidade`, `SigNomeAgenteAcessante`, `DscComponenteTarifario`.

### Valores de filtro (grafia exata)
- `DscComponenteTarifario`: "TUSD_FioB" (nao existe TUSD_FioA isolado; Fio A diluido em TUSD_RB/TUSD_CCT/TUSDG_*)
- `DscDetalheConsumidor`: "SCEE", "APE", "Não se aplica", "TIPO 01", "TIPO 02"
- `DscBaseTarifaria`: "Tarifa de Aplicação", "Base Econômica", "CVA"

### Recorte Fio B do monitor
`DscComponenteTarifario = "TUSD_FioB"`, `DscSubGrupoTarifario = "B3"`, `DscModalidadeTarifaria = "Convencional"`, `DscBaseTarifaria = "Tarifa de Aplicação"` e `DatInicioVigencia >= <fiob_ano_inicial>-01-01` (config.json, padrao 2026). Em 03/11/2026: 394 linhas distintas com vigencia em 2026 (2 do arquivo de 2024, 210 do de 2025, 182 do de 2026).

**O ano do arquivo nao e o ano da vigencia.** O arquivo de um ano traz vigencias que comecam em 01/01 do ano seguinte (a REH e dividida na virada do escalonamento da Lei 14.300; ex.: LIGHT SESA, REH 3.474, 2026-01-01 a 2026-03-14, no arquivo de 2025) e, em ciclos longos, ate dois anos depois (CEA, REH 3.430 de 10/12/2024, vigencia 2026-01-01 a 2026-04-12, no arquivo de 2024). Por isso o monitor le os arquivos de `fiob_ano_inicial - 2` em diante (`ANOS_ARQUIVO_ANTES = 2`). O de 2023 nao tem vigencia iniciada em 2026.

Exemplos (SCEE): LIGHT SESA, REH 3.571, vigencia 2026-03-15 a 2027-03-14, 225,3456 R$/MWh (anterior 210,3790 em 2026-01-01); ENEL RJ, REH 3.570, 361,0248 R$/MWh, efetivo 2026 (60%) 216,61 (anterior 352,3575 em 2026-01-01).

## 3. Regras de negocio

- Fio B efetivo SCEE (Lei 14.300, GD II/III): valor nominal x escalonamento do ano de inicio da vigencia (2023 15%, 2024 30%, 2025 45%, 2026 60%, 2027 75%, 2028 90%).
- Chave de negocio para dedupe e deteccao de mudanca: CNPJ + REH + vigencia + subgrupo + modalidade + base + classe + subclasse + detalhe + posto + unidade + acessante (o nome da distribuidora fica fora: muda com o tempo, ex.: "AMBAR ENERGIA RR" x "Ambar Roraima").
- Fio B SCEE <= 1 R$/MWh e tratado como suspeito (aviso no painel, fora do ranking e das medianas). Em 03/11/2026: CERGAPA 0,00 (REH 3.768), CERGRAL 0,29 (REH 3.769) e CERSAD 0,00 (REH 3.772), todas de 30/09/2026.

## 4. Armadilhas (criticas)

1. Filtros com acentos: comparar em NFC e NFD. No pyarrow 25 `pc.utf8_normalize(col, "NFC")` devolveu texto decomposto (NFD) e zerou o filtro; o monitor usa `pc.is_in` contra as duas formas.
2. Os Parquets repetem linhas identicas (ex.: 2024 tinha 820 linhas no recorte Fio B B3 para 208 distintas): dedupe obrigatorio.
3. CNPJ chega como inteiro e perde zeros a esquerda: `zfill(14)`.
4. Algumas linhas tem a mesma chave com valores diferentes (ex.: CASTRO-DIS 2022-07-30 no historico antigo do Fio B; nada no recorte de 2026): o monitor ordena e fica com o maior valor, de forma deterministica.
5. `_id` e `DatGeracaoConjuntoDados` mudam a cada recarga: nao usar como chave.
6. A URL de download contem o id do dataset (`/dataset/<id>/resource/<id>/download/...`): pegar sempre a `url` do `package_show`, nao montar na mao.
7. (Historico, API) `datastore_search_sql` desabilitado; `datastore_search` com `filters` case-sensitive, valores em texto com virgula decimal e limite de ~32.000 linhas por pagina; PowerShell/shell corrompem "Tarifa de Aplicação" e a API devolve total=0 com success=true.
8. (Historico, API) Datastore de componentes 2025 INCOMPLETO em 03/11/2026: `a4060165-3a0c-404f-926c-83901088b67c` com 5.560 registros de 1 distribuidora, contra 1,39 mi em 2024. O Parquet do mesmo ano estava completo. Foi o motivo para abandonar o datastore e usar sempre o Parquet.
9. Nao usar o CSV dos componentes (800 MB a 1,5 GB por ano); o Parquet tem o mesmo conteudo em 12 a 58 MB.

## 5. Rotina do monitor (monitor_tarifas.py)

- `package_show` dos dois datasets -> `url`, `last_modified` e `size` de cada Parquet. Se o `package_show` falhar, usa os recortes salvos e avisa.
- Cache: `last_modified` (e o corte de data do Fio B) em `data/estado.json` (`parquet.<nome>`); recorte filtrado em `data/recorte_tarifas_b3.csv` e `data/recorte_fiob_<ano>.csv`. Novo download so quando o `last_modified` muda. O Parquet bruto vai para uma pasta temporaria e e apagado; nunca fica em `data/`.
- Download em streaming, timeout de 90 s por bloco, 3 tentativas, limite total de 15 min por arquivo. Se falhar (ou o recorte novo cair mais de 20% contra o salvo), usa o recorte salvo e registra em `estado.json` (`avisos_fonte`), que aparece nos avisos do painel.
- Tempos em 03/11/2026: primeira execucao 57 s, 109,6 MB baixados; execucao com cache valido 6 s, sem download.
- Tempos em 04/11/2026 (com IPCA e comparacoes): execucao com download so da B3 12 s; com cache valido, sem regerar painel, menos de 10 s.

## 6. Nome comercial, UF e regiao (distribuidoras.csv)

- A base de tarifas usa a sigla do agente na ANEEL, que muitas vezes e o nome antigo (ELETROPAULO = Enel SP, CEB-DIS virou "Neoenergia Brasilia" etc.). A ANEEL reescreve a sigla em todo o historico: a base nao guarda os nomes antigos (so CPFL JAGUARI e RGE SUL aparecem como sigla antiga de um mesmo CNPJ).
- `distribuidoras.csv` liga o CNPJ ao nome comercial, grupo, UF e regiao. Montado em 04/11/2026 para os 115 CNPJs da base B3.
- **Fonte da UF:** dataset `indger-indicadores-gerenciais-da-distribuicao`, arquivo Parquet "dados comerciais" (`92492adf-9944-4702-becf-1340ee68859d`, 14,4 MB; campos `NumCNPJ`, `SigAgente`, `NomAgente`, `DatReferenciaInformada`, `CodMunicipioIBGE`, `QtdUCAtiva`). Para cada CNPJ, na referencia mais recente, a UF e a do municipio (2 primeiros digitos do codigo IBGE) com mais UCs ativas; `ufs_atendidas` lista as demais. Cobriu 103 dos 115 CNPJs; os 12 ausentes sao distribuidoras extintas ou incorporadas (RGE antiga, CERR, EDEVP, EBO, ENF, CPFL Mococa, Sul Paulista, Leste Paulista, Santa Cruz antiga, EEB, CNEE, CFLO), preenchidas a mao.
- O INDGER tambem traz `SigAgente` atualizado (ex.: "Enel SP", "Neoenergia Elektro") e a razao social, usados como referencia para o nome comercial. Excecao: o INDGER ainda chama a Equatorial GO de "Enel GO".
- Outros datasets olhados e descartados para UF: SAMP, IASC, subsidios e INDQUAL (tem CNPJ, mas nao UF); BDGD (por distribuidora, sem CNPJ); relacao de empreendimentos de GD (tem UF, mas o Parquet tem 108 MB).
- A busca do painel tambem usa a coluna `nomes_anteriores` (manual: CELPE, COELCE, AMPLA, CEMAR, CELPA, CEPISA, CEAL, CELG-D, ESCELSA, BANDEIRANTE etc.).

## 7. IPCA (Banco Central)

- API SGS, serie 433 (IPCA, variacao % mensal): `https://api.bcb.gov.br/dados/serie/bcdata.sgs.433/dados?formato=json&dataInicial=01/01/2010`. Sem autenticacao; resposta `[{"data": "01/01/2010", "valor": "0.75"}, ...]`. Em 04/11/2026: 201 meses, ate 09/2026.
- Cache: `data/ipca_bcb.json` (`mensal` {"AAAA-MM": valor}, `baixado_em`, `ultima_tentativa`). Novo download so quando o mes muda; enquanto o IPCA do mes anterior nao sai (IBGE divulga por volta do dia 10), no maximo 1 tentativa por dia. Validacao: comeca em 2010-01, mais de 100 meses, sem mes faltando. Falhou: usa o cache e grava aviso em `estado.json` (`avisos_fonte`), que aparece no painel.
- O workflow ja commita `data/` inteiro (`git add -A data`), entao o cache entra no repositorio sem mudanca no .github.

## 8. Linha B3 com detalhe SCEE na base de tarifas (TE SCEE)

- A base `tarifas-distribuidoras-energia-eletrica` tem, para B3 Convencional, uma linha com `DscDetalhe = "SCEE"` alem da linha normal (627 das 2.750 linhas do recorte). A TUSD e igual a da linha normal; a TE e bem menor. Ex.: ELETROPAULO, REH 3.596, vigencia 04/07/2026: TUSD 472,42 nas duas; TE 316,96 (normal) x 37,36 (SCEE).
- Conferido na base de componentes (Parquet 2026, mesma vigencia): na linha SCEE ficam zeradas as parcelas de custo de energia da TE (TE_ENERGIA 257,18; TE_ANGRA 11,30; TE_TRANSPORTE_ITAIPU 9,85; TE_TUST_ITAIPU 4,10) e o TE_SUBSIDIO muda (-3,34 para -0,51). Sobram TE_ESSERR 31,58 + TE_PeD 3,11 + TE_Per_RB 7,04 + TE_CDE_GD 0,17 + TE_CDE_COVID -3,19 + TE_CDE_ELET -0,84 + TE_SUBSIDIO -0,51 = 37,36. A TUSD SCEE tem todos os componentes (Fio B 212,08 igual ao normal).
- Leitura: TUSD + TE SCEE = componentes tarifarias nao associadas ao custo da energia, a base do faturamento da energia compensada pela regra do art. 17 da Lei 14.300 (apos a transicao). Na transicao (GD II ate 2028) a energia compensada paga so o % do Fio B, entao o monitor nao usa essa linha nos calculos; fica registrada na metodologia do painel.

## 9. Grupo A (A4, A3) na base de tarifas e de componentes (validado em 04/11/2026)

### Tarifas (`tarifas-distribuidoras-energia-eletrica`)
- Modalidades por subgrupo (todas as bases): A4 tem Azul, Verde, Convencional, Distribuicao, Geracao, Energia suprimento, "Nao se aplica" e as variantes ABRACE (Azul/Verde CATIVO e LIVRE, Convencional ABRACE). A3 tem Azul, Convencional (so historico; 1 CNPJ em 2026), Distribuicao, Geracao, Energia suprimento e ABRACE, **sem Verde**. A Verde so existe para A3a, A4 e AS.
- Postos: "Ponta", "Fora ponta", "Nao se aplica" (demanda da Verde) e os sazonais antigos "Ponta seca/umida", "Fora ponta seca/umida" (fora do recorte).
- Unidades (`DscUnidadeTerciaria`): "MWh" (energia) e "kW" (demanda). Linhas de demanda: TUSD em R$/kW e **TE = 0**.
  - Verde: energia FP e P (MWh, TUSD+TE) e demanda unica (posto "Nao se aplica", kW). A TUSD da energia na ponta embute a demanda da ponta (ENEL SP 2026: 922,41 x 150,62 na FP).
  - Azul: energia FP e P (TUSD igual nos dois postos, TE diferente) e demanda P e FP (kW). A demanda FP da Azul e igual a demanda da Verde.
- `DscDetalhe` no A4/A3 (Tarifa de Aplicacao): "Nao se aplica", "APE", "SCEE", "Cooperativas autorizadas", "<500GWh". SCEE so nas linhas de energia (TUSD igual, TE reduzida, como na B3). Subclasse "Cooperativa de eletrificacao rural" e acessantes especificos tambem aparecem.
- Retificacoes: ha vigencias com duas resolucoes no mesmo inicio (ex.: CHESP 22/11/2016, REH 2.173 e "DSP RETIFICACAO"); o monitor fica com a ultima em ordem de texto, como na B3.
- Recorte do monitor: `DscSubGrupo` in `subgrupos_grupo_a`, modalidade in (Verde, Azul), base "Tarifa de Aplicacao", classe/subclasse/acessante "Nao se aplica", detalhe in ("Nao se aplica", "SCEE"), posto in (Ponta, Fora ponta, Nao se aplica), inicio >= `grupo_a_ano_inicial`-01-01 -> 14.649 linhas desde 2016 (A4 Verde 5.355, A4 Azul 6.722, A3 Azul 2.572; 3,3 MB). Desde 2010 seriam 20.339 linhas (4,6 MB).
- CNPJs: A4 Verde 114, A4 Azul 114, A3 Azul 49 (desde 2016); ativos 103, 103 e 45. A3 com vigencia em 2026: 42.
- Conferido contra o Parquet bruto: ENEL SP A4 Verde, REH 3.596, 04/07/2026: energia FP 150,62+301,88 = 452,50; P 922,41+482,42 = 1.404,83; demanda 19,04 R$/kW. LIGHT A4 Verde, REH 3.571, 15/03/2026: 500,38; 1.613,74; 28,05. CEMIG A3 Azul, REH 3.589, 28/05/2026: 426,66; 605,89; demanda P 36,43 e FP 13,65.

### TUSD Fio B (`componentes-tarifarias`)
- Existe para A4 e A3 nas modalidades Verde/Azul, nas bases Tarifa de Aplicacao, Base Economica e CVA. `DscUnidade` "R$/MWh" e "R$/kW" (com o prefixo R$/, diferente da base de tarifas).
- Detalhes: "Nao se aplica", "APE" e "SCEE"; o SCEE so existe para R$/MWh e tem o mesmo valor do normal.
- Valores (2.647 linhas de vigencias desde 01/01/2026, arquivos 2024 a 2026): energia FP = 0 em todas as linhas (Verde e Azul); energia P = 0 na Azul; na Verde a energia P carrega o custo de rede da ponta (mediana 1.175 R$/MWh; ENEL SP 486,45; LIGHT 732,65). Demanda em R$/kW (A4 Verde mediana 16,10; A4 Azul P 46,99 e FP 16,10; A3 Azul P 15,14 e FP 3,33). Ha acessante especifico (INTERCEMENT), fora do recorte.
- CNPJs com Fio B desde 2026: A4 Verde 103, A4 Azul 103, A3 Azul 42. Sem Fio B em 2026: CERTHIL, Energisa AC e Neoenergia Brasilia no A3 Azul (aviso no painel).
- Recortes do monitor: `recorte_fiob_a_<ano>.csv` (17 linhas em 2024, 1.401 em 2025, 1.229 em 2026), lidos do mesmo download do Fio B da B3.

### Regras do monitor para o Grupo A
- Snapshot `data/tarifas_grupo_a.csv` e `data/fiob_grupo_a.csv`, mesma chave de negocio (inclui subgrupo, modalidade, posto e unidade). Primeira execucao: linha de base sem alerta. Escopo (`subgrupos`, `inicio_min`, `fiob_inicio_min`) fica em `estado.json` (`escopo_grupo_a`); linhas que entram por ampliacao do escopo viram linha de base, sem alerta.
- Valor do kWh compensado = energia (TUSD+TE) - Fio B efetivo da energia (SCEE R$/MWh x escalonamento). A demanda nao e compensada.
- Tempos em 04/11/2026: primeira execucao com o Grupo A 89 s e 109,6 MB (tarifas 2,6 + componentes 24,6 + 57,9 + 24,5); com cache valido 14 s, sem download.
