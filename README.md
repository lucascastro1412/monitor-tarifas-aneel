# Monitor de Tarifas ANEEL (B3, Fio B SCEE e Grupo A)

Agente diário que acompanha as tarifas homologadas pela ANEEL e mantém um painel de comparação.

**Painel online:** https://lucascastro1412.github.io/monitor-tarifas-aneel/ (Excel: https://lucascastro1412.github.io/monitor-tarifas-aneel/Painel_Tarifas_ANEEL.xlsx)

**O que monitora**
- **B3 Convencional, Tarifa de Aplicação:** TUSD, TE e total (R$/MWh), todas as distribuidoras, histórico completo desde 2010.
- **TUSD Fio B, B3 Convencional:** detalhe SCEE (energia compensada, Lei 14.300), só vigências iniciadas a partir de `fiob_ano_inicial` (padrão 01/01/2026).
- **Grupo A (média e alta tensão), Tarifa de Aplicação:** subgrupos de `subgrupos_grupo_a` (padrão A4 e A3), modalidades Verde e Azul, vigências desde `grupo_a_ano_inicial` (padrão 2016): energia (TUSD+TE, R$/MWh) na ponta e fora ponta e demanda (TUSD, R$/kW; Verde: demanda única; Azul: ponta e fora ponta). TUSD Fio B do Grupo A por posto e unidade desde `fiob_ano_inicial`.
- **IPCA mensal** (Banco Central, SGS série 433), para comparar a B3 com a inflação.
- **Tributos (ICMS, PIS e COFINS) e calculadora GD:** tarifa com tributos (V) e valor da energia compensada com os tributos evitados (W), pela mesma regra da planilha Tarifas.xlsx do usuário (ver "Impostos e calculadora GD").
- **Arrendamento de usinas:** pagamento ao dono (também em R$/kWh), parte e margem da Thopen, deságio nominal e deságios do dono sobre a tarifa sem impostos, com impostos e sobre o valor compensado, por MWh, por mês e por ano; condições definidas por deságio ou por preço ao parceiro em R$/kWh; quadro com as tarifas da distribuidora (ver "Arrendamento de usinas").
- **Visão de negociação:** cálculo reverso do deságio ou VB a partir do pedido do dono ou da margem mínima da Thopen, faixa de negociação, projeção do contrato com VPL e textos de proposta e de análise interna.

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
   - **Histórico cumulativo.** O snapshot guarda tudo o que já foi coletado. Quando a ANEEL tira linhas da base (em 24/11/2026 ela apagou 220 linhas antigas da B3 e 1.482 do Grupo A), elas **não** saem do snapshot: recebem `removida_fonte_em = AAAA-MM-DD` (coluna vazia nas linhas ativas) e continuam valendo no histórico, nas matrizes, nas linhas do tempo e nos gráficos. Remoções não abrem Issue e não entram na contagem de "linhas que mudaram"; vão para o log com tipo `removida_na_fonte` e o painel mostra um aviso resumido ("A ANEEL removeu da base N vigências antigas em DD/MM/AAAA; mantidas no histórico do painel"). Se a linha voltar à fonte, a marca é limpa sem alerta (log `voltou_na_fonte`); se voltar com outro valor, conta como alterada.
   - **Retificação.** Se a ANEEL troca uma vigência por outra (mesma distribuidora e mesmo início, outra REH, ou a mesma REH com outro fim ou valor), a substituída sai por remoção e a nova entra como nova. Quando há uma linha ativa e uma removida para a mesma distribuidora e o mesmo início (no Grupo A, também subgrupo e modalidade), vale a ativa: na linha do tempo, nas matrizes e na vigência atual. Removidas sem substituta (histórico antigo) continuam valendo. Vale para as 4 séries; no Fio B continua o corte de `fiob_ano_inicial`.
5. IPCA: cache em `data/ipca_bcb.json`, baixado de novo só quando o mês muda (enquanto o IPCA do mês anterior não sai, no máximo 1 tentativa por dia). Timeout de 90 s, 3 tentativas. Se a API do Banco Central falhar, usa o cache e avisa no painel; sem cache, as comparações com a inflação ficam vazias e a execução segue.
6. O painel também é regerado quando a vigência atual de alguma distribuidora muda (uma vigência começa ou termina), quando o calendário de reajustes muda ou quando chega IPCA novo, mesmo sem mudança na base.
7. Falhas:
   - download ou leitura de um Parquet falhou (ou recorte novo com queda > 20%): usa o recorte salvo, avisa no log e no painel (`estado.json`, `avisos_fonte`) e segue;
   - sem recorte salvo ou recorte B3 vazio: nada é sobrescrito e a execução falha;
   - **guarda de queda do snapshot** (por série): compara o recorte novo com as linhas **ativas** do snapshot (as já removidas da fonte não contam). Bloqueia, sem gravar nada, se (a) o recorte novo tiver menos de 80% das linhas ativas ou (b) mais de 20% das linhas ativas sumirem de uma vez, mesmo com linhas novas compensando o tamanho. Remoções com menos de 50 linhas nunca bloqueiam. Assim, uma falha da ANEEL (arquivo pela metade) para a coleta, e uma limpeza moderada da base (como a de 24/11/2026: 8% da B3, 10% do Grupo A) passa e fica no histórico. Se uma remoção grande for legítima, rode uma vez `python monitor_tarifas.py --aceitar-queda` (vale também para a guarda do recorte).

**Impostos e calculadora GD (`impostos_distribuidoras.csv`, `icms_gd_distribuidoras.csv`, `referencia_planilha_tarifas.csv`)**

Três CSVs versionados (ponto e vírgula; o leitor aceita decimal com vírgula ou ponto) e `impostos_meta.json`, lidos ao gerar o painel. Quando mudam, o painel é regerado na próxima coleta.
- `impostos_distribuidoras.csv`: alíquotas agregadas por distribuidora e subgrupo (TOTAL, B1, B3, Grupo A...), médias de faturas de UCs consumidoras da base Thopen (cerca de 164 mil faturas nos 3 últimos meses de referência). Sem dado de cliente. Os impostos são atualizados por um script privado da Thopen a partir das faturas; sem ele o painel usa o último CSV publicado. `impostos_meta.json` guarda a data da atualização e a janela de meses das faturas, mostradas na Calculadora ("impostos atualizados em DD/MM, faturas de MM/AA a MM/AA") e na Metodologia. No `rodar_local.ps1`, o caminho do script vai em `config.local.json` (`{"script_impostos": "..."}`, não versionado) ou na variável `MONITOR_SCRIPT_IMPOSTOS`; ele roda no máximo 1 vez por dia e, se falhar, a coleta segue com o CSV anterior. Colunas da origem, sem `fator_gross_up` (fórmula simples, não a cascata), mais `cnpj` (de-para pelo nome com o `distribuidoras.csv`; a CPFL Jaguari, incorporada, aponta para o CNPJ da CPFL Santa Cruz). O painel usa a média do subgrupo B3 se houver, senão a do TOTAL; sem fatura, ICMS e PIS/COFINS da planilha; sem os dois, ICMS 18% e PIS+COFINS 5%. A fonte de cada alíquota aparece na calculadora e no Excel.
- `icms_gd_distribuidoras.csv`: por CNPJ, se o ICMS da TE e o da TUSD entram no benefício da energia compensada (`flag_icms_te`, `flag_icms_tusd`), o repasse `s` (50%; 70% na Cemig) e as alíquotas da planilha. Para as 48 distribuidoras da planilha, as flags vêm das fórmulas da coluna W, linha a linha (fonte "planilha Tarifas.xlsx do usuário"); as demais usam a regra mais comum da mesma UF na planilha ("inferido pela UF"; empate desempatado pela regra mais comum no país) ou 0/0 ("padrão, revisar"). Incoerências da planilha foram mantidas e anotadas em `observacao` (RGE 0/0 x CEEE 1/1 no RS, EDP ES 0/1, V34 da CEEE sem ICMS, TUSD/TE copiados entre distribuidoras).
- `referencia_planilha_tarifas.csv`: TUSD e TE das 48 linhas da planilha (referência fixa), para a Tabela Thopen mostrar o que estava desatualizado.

Regra (igual à planilha, gross-up em cascata): D = TUSD, E = TE, i = ICMS, pc = PIS + COFINS; k_PC = 1/(1-pc); k_full = 1/((1-i)(1-pc)); U = D + E; V = U·k_full; W = (D - FB_ef)·k_TUSD + E·k_TE (k = k_full se o ICMS da parcela entra no benefício, senão k_PC); Y = W - (U - FB_ef); X = s·Y; Z = (U - FB_ef) + X. Preço de venda P = (1 - d)·U (deságio d sobre a tarifa sem tributos, mesma base do Arrendamento); deságio nominal = 1 - P/U; economia do cliente = W - P (crédito da compensada menos o preço). Pagamento ao dono (opção A, igual ao Arrendamento, Fio B com impostos) = P + s·Y_I - FB_ef·k_full, com Y_I = W_I - U; deságio implícito = 1 - dono/U. GD I: FB_ef = 0 (reproduz a planilha 1:1). GD II/III: FB_ef = Fio B SCEE × % do ano da Lei 14.300 (2026 60%, 2027 75%, 2028 90%). GD III ainda paga 40% do Fio A, TFSEE e P&D/EE, o que não está modelado (aviso na calculadora).

Teste de regressão: `py tests/test_calculadora.py [Tarifas.xlsx]` (casos CPFL Paulista e Cemig; com a planilha, confere as 48 linhas contra o cache dela). Histórico cumulativo: `py tests/test_historico.py` (linha removida que fica, linha que volta, retificação, remoção sem alerta e guarda de queda). Arrendamento: `py tests/test_arrendamento.py` (identidade dono + Thopen + Fio B = W_I, GD I sem Fio B, caso fixo CPFL Paulista, deságio implícito CPFL e Enel SP com s = 50% e 70%, implícito negativo, geração e CSV de produtividade).

**Arrendamento de usinas (aba Arrendamento, `produtividade_uf.csv`)**

A Thopen arrenda a usina, recebe o valor da energia compensada (tarifa mais o benefício fiscal dos tributos evitados) e paga o dono com deságio. Por MWh compensado (função `calc_arrendamento`, replicada no JS):
- U = D + E; W_I = D·k_TUSD + E·k_TE (forma da planilha, GD I, sem Fio B); Y = W_I - U (benefício fiscal); FB_ef = Fio B SCEE × % do ano da Lei 14.300 (0 em GD I).
- Opção A: o deságio d incide só sobre a tarifa e o benefício fiscal é dividido pelo VB s (parte do dono; padrão 50%, Cemig 70%). Pagamento ao dono = (1 - d)·U + s·Y - custo do Fio B; parte da Thopen = d·U + (1 - s)·Y; dono + Thopen + custo do Fio B = W_I. O dono paga o Fio B: padrão com impostos (FB_ef·k_full, como na fatura) ou sem impostos (FB_ef), por seletor; a memória de cálculo mostra os dois.
- Desconto ao cliente final = c·W_I (padrão 0%); margem líquida da Thopen = parte da Thopen - desconto (em vermelho se negativa).
- Deságio nominal = d (parâmetro, só sobre U). Deságio implícito = 1 - dono / U (sobre a tarifa sem tributos, líquido do VB e do Fio B): diminui quando s aumenta, aumenta com o Fio B e fica negativo quando o dono recebe mais que U. Ex.: CPFL Paulista GD I, d = 30%: 22,83% com s = 50% e 19,97% com s = 70%.
- Deságio do dono sobre três bases: sobre a tarifa sem impostos = 1 - dono / U (o deságio implícito); sobre a tarifa com impostos = 1 - dono / V, com V = U·k_full; e, como referência, sobre o valor compensado = 1 - dono / W_I. Ex.: CPFL Paulista GD I, d = 30%, s = 50%: dono 570,60 R$/MWh (0,5706 R$/kWh), U = 739,45, V = 949,23, W_I = 845,42; deságios 22,83%, 39,89% e 32,51%.
- Condições comerciais, "Definir por: Deságio (%) | Preço ao parceiro (R$/kWh)". O parceiro é o dono da usina e o preço P é o pagamento líquido ao dono por kWh compensado, já descontado o custo do Fio B (a mesma grandeza "dono" do modelo). No modo preço, P_MWh = 1.000·P e o deságio nominal equivalente sai da fórmula fechada do cálculo reverso com o VB s fixo: d = 1 - (P_MWh + custo_FioB - s·Y) / U (função `desagio_por_preco`). Todas as saídas (cards, memória, sensibilidade, faixa, projeção e textos de copiar) usam esse d; se ele sair de 0 a 100% o cálculo continua, com destaque em vermelho. No modo deságio o painel mostra o preço resultante em R$/kWh. Alternar os modos preserva o negócio (o campo do outro modo recebe o valor atual, com 8 casas decimais). Ex.: Enel SP GD II 2026, d = 30%, s = 50%: dono 447,57 R$/MWh = 0,44757 R$/kWh; deságio sobre U = 789,38: 43,30%; sobre V = 1.016,69: 55,98%.
- Quadro "Tarifas da distribuidora" (perto das condições comerciais): TUSD, TE, U, V, W_I, Y, Fio B SCEE, Fio B efetivo (× % do ano) e custo do Fio B com impostos em R$/kWh e R$/MWh, preço ao parceiro, ICMS e PIS+COFINS com a fonte, os três deságios e a vigência (início e fim).
- Geração: UFV = kWp × produtividade (kWh/kWp/ano, padrão da UF) / 12 × (1 - perdas); CGH ou outra = kW × fator de capacidade (padrão 50%) × 730 h; ou MWh/mês direto. Energia compensada = geração × % compensada (padrão 100%).
- `produtividade_uf.csv` (`uf;kwh_kwp_ano;fonte`, versionado, editável): produtividade típica de UFV fixa por UF, de cerca de 1.300 no Sul a 1.700 no Nordeste. Referência aproximada, derivada da irradiação média do Atlas Brasileiro de Energia Solar (INPE/LABREN, 2ª ed., 2017) com PR típico, arredondada; para proposta firme, use a simulação do projeto.
- Saídas: cards (dono, preço ao parceiro em R$/kWh, Thopen, margem líquida em R$/mês, deságio nominal ou nominal equivalente, deságio sobre a tarifa sem impostos e com impostos, com o deságio sobre W_I de referência), tabela por MWh, mês e ano (geração, W_I, dono, Thopen, Fio B, desconto, margem), memória de cálculo, sensibilidade deságio 20 a 40% × VB 30 a 70% (dono R$/mês, margem R$/mês, deságio sobre U ou deságio sobre V, heatmap com a célula atual), botão "Copiar resumo" (texto para proposta) e link para a Calculadora com a mesma distribuidora.
- Premissas: estimativa, sem bandeiras, CIP, custo de disponibilidade, inadimplência, O&M e tributos sobre a receita. GD III usa a mesma regra de FB_ef da calculadora (o % é editável); pela Lei 14.300 ela paga 100% do Fio B mais 40% do Fio A, TFSEE e P&D/EE, o que não está modelado.
- Excel, aba "Arrendamento (referência)": todas as distribuidoras para uma usina-padrão de 1 MWh/mês, d = 30% e s = 50%, Fio B com impostos, em GD I, II e III (II e III com o Fio B efetivo da vigência atual), com V tarifa com impostos e, por GD, preço ao dono em R$/kWh, deságio implícito (sobre U) e deságio sobre V.

**Visão de negociação (aba Arrendamento, seção no fim)**

Para negociar a usina com o proprietário, a partir da configuração da aba (funções `resolver_arrendamento` e `projecao_arrendamento`, replicadas no JS). custo_FioB = FB_ef·k_full (ou FB_ef, conforme o seletor).
- Proposta ao dono (cards): recebimento em R$/MWh, R$/mês e R$/ano; preço em R$/kWh; dono / W_I (e deságio sobre W_I) e dono / U; deságios sobre a tarifa sem e com impostos e o nominal; Fio B pago pelo dono em R$/mês.
- Cálculo reverso (linear em d e em s), com seletor "variável a ajustar: deságio ou VB":
  - Pedido do dono (R$/mês, R$/MWh ou R$/kWh; R$/mês ÷ MWh compensado no mês; R$/kWh × 1.000): d = 1 - (dono + custo_FioB - s·Y) / U com o s atual, ou s = (dono + custo_FioB - (1 - d)·U) / Y com o d atual; mostra os deságios resultantes sobre a tarifa sem e com impostos, a margem da Thopen (R$/MWh, R$/mês e % de W_I) e se é viável (margem ≥ margem mínima).
  - Margem mínima da Thopen m (R$/mês, R$/MWh ou % de W_I, padrão 20% de W_I, já líquida do desconto c·W_I): d_min = (m + c·W_I - (1 - s)·Y) / U, ou s_max = 1 - (m + c·W_I - d·U) / Y; pagamento máximo ao dono (teto da negociação) = W_I - custo_FioB - c·W_I - m, em R$/MWh, R$/mês e R$/ano, e o deságio implícito correspondente.
  - Sem solução: valor resolvido fora de 0 a 100%, U = 0 (resolver d) ou Y = 0 (resolver s: sem benefício fiscal o VB não muda nada).
- Faixa de negociação: Abertura (padrão d atual + 10 p.p.), Alvo (configuração atual) e Limite (d_min ou s_max da margem mínima, arredondado a 0,01 p.p. para o lado seguro), d e s editáveis; para cada coluna preço em R$/kWh, dono R$/MWh e R$/mês, deságios sobre U, V e W_I e margem da Thopen em R$/mês e % de W_I; coluna em vermelho quando a margem fica abaixo da mínima. "Recalcular faixa" volta aos padrões.
- Projeção do contrato: prazo (padrão 10 anos), ano de início (padrão o ano da data de referência), reajuste anual da tarifa (padrão IPCA acumulado dos últimos 12 meses divulgados), degradação anual da geração (padrão 0,5% UFV, 0% CGH), taxa de desconto (padrão 12% a.a.) e Fio B pelo escalonamento da Lei 14.300 (sim/não). No ano t (t = 0 no primeiro): D, E e Fio B SCEE × (1 + reajuste)^t, com tributos, flags, d, s e c fixos; % do Fio B 60% em 2026, 75% em 2027, 90% em 2028 e 100% de 2029 em diante (aproximação: a regra pós-2028 será definida pela ANEEL), ou o % fixo informado com o escalonamento desligado; GD I sem Fio B; GD III com a mesma regra do Fio B e o aviso existente. Energia do ano = MWh/mês × (1 - degradação)^t × 12. Seletor "Manter fixo ao longo do contrato": Deságio (padrão; no modo preço fixa o d equivalente do ano 1, e o preço ao parceiro varia com a tarifa e o Fio B) ou Preço (R$/kWh, reajustado pelo índice): P_t = P·(1 + reajuste)^t, com P = preço do ano 1, e o d de cada ano é recalculado para manter esse preço, d_t = 1 - (1.000·P_t + custo_FioB_t - s·Y_t) / U_t; com o degrau do Fio B da Lei 14.300, d_t cai e a Thopen absorve o aumento do Fio B. Tabela por ano (% Fio B, U, V, Y, Fio B do dono, d, preço R$/kWh, dono R$/MWh, energia, dono, Thopen e margem líquida R$/ano, deságios sobre U, V e W_I), totais, VPL (fluxos no fim de cada ano: Σ fluxo_t / (1 + taxa)^(t+1)) para o dono e para a Thopen, e gráfico dono × Thopen por ano.
- "Copiar proposta": texto para o proprietário (usina, distribuidora, modalidade, deságio ou preço e VB, valor no ano 1 em R$/kWh, R$/MWh e R$/mês, deságios sobre a tarifa sem e com impostos, regra de reajuste, evolução nos anos 1, 3 e 5 e total do contrato, premissas), sem a margem da Thopen. "Copiar análise interna": o mesmo texto mais tarifas em R$/kWh, margem atual, margem mínima, d_min e teto, pedido do dono, faixa (com preço e os três deságios), d, preço e deságios por ano e VPL.
- Limitações: reajuste único para TUSD, TE e Fio B (os reajustes da ANEEL diferem por componente e distribuidora), tributos constantes, sem bandeiras, CIP, custo de disponibilidade, inadimplência, O&M e tributos sobre a receita.
- Excel, aba "Negociação (referência)": projeção de 10 anos das distribuidoras com alíquotas de faturas, usina-padrão de 1 MWh/mês, d = 30%, s = 50%, GD II, Fio B com impostos e escalonamento, reajuste = IPCA 12 meses, sem degradação; uma linha por ano, mais Total e VPL 12% a.a. por distribuidora.
- Testes: `py tests/test_negociacao.py` (ida e volta do pedido e da margem mínima na CPFL Paulista e Enel SP, resolver s, casos sem solução, projeção GD II Enel SP com o degrau do Fio B 2026 a 2027, GD I constante, reajuste e degradação, VPL à mão, identidade anual, preço em R$/kWh e deságios sobre U, V e W_I na CPFL Paulista e Enel SP, ida e volta entre os modos deságio e preço e projeção com preço fixo reajustado).

**Por que ler o Parquet de anos anteriores no Fio B**

O arquivo anual de componentes é organizado pelo processo tarifário, não pelo início da vigência. O de 2025 traz vigências que começam em 01/01/2026 (a REH é dividida na virada do escalonamento da Lei 14.300) e o de 2024 traz a CEA de 01/01/2026 a 12/04/2026 (REH 3.430, de 12/2024). Por isso o script lê os arquivos de `fiob_ano_inicial - 2` em diante e filtra por `DatInicioVigencia >= <ano>-01-01`.

**Painéis**

| Painel / aba | Conteúdo |
|---|---|
| `Painel_Tarifas_ANEEL.html` | Painel interativo (arquivo único, abre no navegador), em abas. Gráficos via Chart.js (CDN, precisa de internet) |
| Visão geral | KPIs (mediana da B3, valor do kWh compensado mediano, maior e menor, B3 acumulada desde 2016 x IPCA, Fio B, reajustes próximos), avisos de qualidade, índice da B3 nacional x IPCA (01/01/2016 = 100), próximos reajustes e os 10 maiores e 10 menores valores do kWh compensado |
| Distribuidora | Uma distribuidora (padrão ENEL SP): ficha (sigla, nomes anteriores, UF, grupo, razão social), tarifa atual, Fio B, valor do kWh compensado, tarifa com tributos (V) e valor compensado com tributos (W), posição no ranking, último e próximo reajuste, posição contra as medianas nacional, da região e da UF, composição TUSD x TE x Fio B, acumulado desde 2016, 2020, 12 meses e último reajuste x IPCA (com aumento real), gráfico da B3 contra a B3 de 2016 corrigida pelo IPCA e vigências |
| Comparador | 2 a 4 distribuidoras (padrão ENEL SP, LIGHT, ENEL RJ e CPFL PIRATININGA): tabela lado a lado (inclui ICMS, PIS+COFINS, tarifa com tributos V, valor compensado com tributos W em GD II e GD I e linhas A4 Verde), composição TUSD x TE (com Fio B), valor do kWh compensado, B3 anual e Fio B por vigência |
| Calculadora | Distribuidora (busca por nome comercial ou sigla) preenche UF, vigência atual, TUSD, TE e Fio B SCEE; data de referência (define o % da Lei 14.300), enquadramento GD I/II/III (padrão GD II), ICMS, PIS e COFINS com a fonte, flags de ICMS TE/TUSD, repasse s e preço de venda ou deságio, todos editáveis, com "Restaurar padrões". Saídas em cards e memória de cálculo: U, V, FB_ef, W, Y, X, Z, deságio nominal (sobre U), economia do cliente por MWh (W - P), pagamento ao dono e deságio implícito (opção A, igual ao Arrendamento) e Fio B residual com tributos. Bloco "Simulação de fatura (estimativa)" com consumo, energia compensada, tipo de ligação, bandeira e CIP |
| Arrendamento | Calculadora de arrendamento de usinas: distribuidora (preenche UF, TUSD, TE, Fio B, impostos, flags, VB e produtividade), data, GD I/II/III, deságio, VB, desconto ao cliente, tipo de usina (UFV, CGH ou MWh/mês) e Fio B com ou sem impostos; cards, resultados por MWh, mês e ano, memória de cálculo, sensibilidade deságio × VB, "Copiar resumo" e "Restaurar padrões"; Visão de negociação: proposta ao dono, cálculo reverso (pedido do dono e margem mínima, resolvendo deságio ou VB), faixa Abertura/Alvo/Limite, projeção do contrato ano a ano com VPL e gráfico, "Copiar proposta" e "Copiar análise interna" |
| Tabela Thopen | Réplica da planilha Tarifas.xlsx com a tarifa ANEEL vigente: D, E, ICMS, PIS+COFINS, flags, s, U, V, W, Y, X e Z (GD I como na planilha, ou GD II), e para as 48 da planilha a TUSD+TE dela e a diferença. Clique no nome abre a Calculadora |
| Grupo A | Seletores de subgrupo (A4/A3) e modalidade (Verde/Azul), padrão A4 Verde. KPIs (medianas de energia FP, energia P, demanda, variação do último reajuste, valor compensado FP e Fio B), matriz distribuidora x ano com heatmap (energia FP, energia P, demanda), histórico das distribuidoras do comparador, relação ponta / fora ponta, ranking por métrica, simulador Verde x Azul (demanda kW, consumo MWh/mês e % na ponta; custo mensal sem impostos, bandeiras e ultrapassagem, com ponto de equilíbrio), valor do kWh compensado (FP e P) e Fio B por posto e unidade. A ficha da aba Distribuidora ganhou o bloco Grupo A |
| Rankings | Ranking (gráfico e tabela completa) por valor do kWh compensado, % da tarifa, B3 total, tarifa B3 com impostos (V), valor compensado com tributos (W, GD II), Fio B, Fio B / TUSD, último reajuste, acumulado e aumento real; matriz distribuidora x ano com heatmap |
| Regiões | Medianas por região e por UF (B3, valor compensado, Fio B, efetivo, acumulado, aumento real), em barras e mapa de calor |
| Calendário | Vigências que terminam nos próximos 90 dias, com a tarifa atual e a nova homologação, se já publicada |
| Mudanças / Metodologia | Log do agente (as 50 últimas novas e alteradas; as remoções da fonte ficam no aviso de qualidade e no Excel) e regras de cálculo, incluindo o histórico cumulativo |
| `Painel_Tarifas_ANEEL.xlsx` | Abas abaixo (nome comercial nas matrizes) |
| Leia-me | Metodologia, avisos de qualidade e data da última atualização |
| Comparativo | Uma linha por distribuidora com tudo do comparador (vigência, TUSD, TE, total, Fio B, efetivo, valor compensado, reajustes, acumulados, CAGR, rankings, diferença contra a mediana) |
| Valor kWh Compensado | Ranking das ativas pelo valor do kWh compensado (GD II) |
| Acumulado x IPCA | B3 desde 2016, desde 2020, 12 meses e último reajuste, com CAGR, IPCA e aumento real |
| Regioes UF | Medianas por Brasil, região e UF |
| Calendario | Próximos reajustes (90 dias) |
| Distribuidoras | O mapa `distribuidoras.csv` com o rótulo usado no painel |
| Calculadora (tabela) | Tabela Thopen: alíquotas, flags, U, V, W/Y/X/Z em GD I e GD II, Fio B residual com tributos, TUSD+TE da planilha e diferença, fontes e observações |
| Impostos / Impostos faturas | Alíquotas e flags usadas por distribuidora, com a fonte; agregados das faturas por distribuidora e subgrupo |
| Arrendamento (referência) | Usina-padrão de 1 MWh/mês, d = 30%, s = 50%: U, V tarifa com impostos, W GD I, Y, Fio B efetivo e, em GD I, II e III, custo do Fio B, dono, preço ao dono em R$/kWh, Thopen, deságio implícito (1 - dono / U) e deságio sobre V (1 - dono / V), mais a produtividade UFV da UF |
| Negociação (referência) | Projeção de 10 anos, GD II, 1 MWh/mês, d = 30%, s = 50%, reajuste IPCA 12 meses, para as distribuidoras com alíquotas de faturas: % do Fio B, U, Y, custo do Fio B, dono R$/MWh e R$/ano, Thopen, margem líquida e deságio implícito por ano, mais Total e VPL 12% a.a. |
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
python monitor_tarifas.py --aceitar-queda   # grava mesmo se a guarda de queda bloquear (remoção grande e legítima)
python monitor_tarifas.py --migrar-historico 7fe4a35^ --data-remocao 2026-11-24
                                            # migração única (já aplicada): reinsere no snapshot as linhas daquela versão
                                            # do git que sumiram, marcadas como removidas, e corrige o tipo no log
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
