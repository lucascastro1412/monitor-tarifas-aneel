#!/usr/bin/env python3
"""
Monitor de Tarifas ANEEL
  - B3 Convencional, Tarifa de Aplicacao (TUSD + TE), historico completo desde 2010
  - TUSD Fio B, B3 Convencional, Tarifa de Aplicacao (detalhe SCEE e demais), vigencias
    iniciadas a partir de config.json "fiob_ano_inicial" (padrao 2026)
  - Grupo A (config.json "subgrupos_grupo_a", padrao A4 e A3), modalidades Verde e Azul, Tarifa de
    Aplicacao: energia (TUSD+TE, R$/MWh) na ponta e fora ponta e demanda (TUSD, R$/kW), vigencias desde
    "grupo_a_ano_inicial" (padrao 2016), e o TUSD Fio B por posto e unidade desde "fiob_ano_inicial"

Fonte: arquivos PARQUET completos dos datasets de dados abertos da ANEEL
(ver docs/aneel-fonte-dados.md):
  - tarifas-distribuidoras-energia-eletrica (resource 9af99ef4-..., arquivo unico);
  - componentes-tarifarias (1 Parquet por ano do processo tarifario; le os anos
    >= fiob_ano_inicial - 2, porque o arquivo de um ano traz vigencias que comecam em
    1o de janeiro do ano seguinte e, em ciclos longos, ate dois anos depois).
Da API CKAN so se usa package_show, para achar as URLs e o last_modified de cada arquivo.
O Parquet so e baixado de novo quando o last_modified muda; o recorte filtrado (nunca o
arquivo bruto) fica em data/recorte_*.csv. Se o download falhar e houver recorte salvo,
usa o recorte e avisa no log.

Complementos:
  - distribuidoras.csv (versionado, editavel): CNPJ -> nome comercial atual, grupo, UF e regiao.
    O painel mostra "ENEL SP (ELETROPAULO)" quando o nome comercial difere da sigla da ANEEL.
  - IPCA mensal do Banco Central (SGS serie 433), com cache em data/ipca_bcb.json; so baixa de novo
    quando o mes muda (ou, se o IPCA do mes anterior ainda nao saiu, no maximo 1 tentativa por dia).
    Se a API falhar, usa o cache e avisa.

Cada execucao:
  1. atualiza os recortes (download so do que mudou) e o IPCA;
  2. normaliza (datas, valores, duplicatas);
  3. compara com o snapshot salvo em data/ pela chave de negocio. O snapshot e CUMULATIVO: linha que some da
     fonte fica, com removida_fonte_em = data em que sumiu, e continua no historico do painel;
  4. registra mudancas em data/log_mudancas.csv (remocoes como removida_na_fonte, sem alerta);
  5. se houver mudanca, regera os paineis Excel e HTML e escreve alerta.md.

Roda 1 vez por dia no PC (rodar_local.ps1, agendado as 07:30), que faz commit e push de data/ e dos
paineis; o GitHub Actions so publica o painel no Pages. Na mao:  python monitor_tarifas.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

API = "https://dadosabertos.aneel.gov.br/api/3/action/"
PKG_TARIFAS = "tarifas-distribuidoras-energia-eletrica"
RES_TARIFAS_PARQUET = "9af99ef4-376a-4493-a075-5e48ab4da51b"
PKG_COMPONENTES = "componentes-tarifarias"
FUSO_BR = timezone(timedelta(hours=-3))
NA = "Não se aplica"
# Arquivos anuais de componentes lidos: >= fiob_ano_inicial - ANOS_ARQUIVO_ANTES. Com 1 ano so, faltaria
# a CEA de 01/01/2026 a 12/04/2026 (REH 3.430 de 12/2024, que esta no arquivo de 2024; ver docs).
ANOS_ARQUIVO_ANTES = 2
TEMPO_MAX_DOWNLOAD = 15 * 60  # s, limite total por arquivo; cada leitura de bloco tem timeout de 90 s
# Fio B SCEE ate este valor (R$/MWh) e tratado como suspeito: aviso de qualidade, fora de ranking e medianas.
FIOB_SUSPEITO_MAX = 1.0

# Lei 14.300: parcela do Fio B paga sobre a energia compensada (GD II / GD III).
# A partir de 2029 a regra passa a ser definida pela ANEEL (nao modelado).
ESCALONAMENTO_FIOB = {2023: 0.15, 2024: 0.30, 2025: 0.45, 2026: 0.60, 2027: 0.75, 2028: 0.90}

# IPCA mensal (% a.m.) do Banco Central, SGS serie 433
IPCA_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.433/dados"
IPCA_INICIO = "01/01/2010"
ARQ_IPCA = "ipca_bcb.json"
# Mapa CNPJ -> nome comercial, grupo, UF e regiao (versionado, editavel pelo usuario)
ARQ_DISTRIBUIDORAS = "distribuidoras.csv"
COLS_DISTRIBUIDORAS = ["cnpj", "sigla_aneel", "nome_comercial", "grupo_economico", "uf", "regiao"]
REGIAO_UF = {**{u: "Norte" for u in "AC AM AP PA RO RR TO".split()},
             **{u: "Nordeste" for u in "AL BA CE MA PB PE PI RN SE".split()},
             **{u: "Centro-Oeste" for u in "DF GO MS MT".split()},
             **{u: "Sudeste" for u in "ES MG RJ SP".split()},
             **{u: "Sul" for u in "PR RS SC".split()}}
ORDEM_REGIOES = ["Norte", "Nordeste", "Centro-Oeste", "Sudeste", "Sul"]
# Anos-base do acumulado da B3 (valor vigente em 01/01 do ano contra o valor atual)
ANOS_ACUMULADO = (2016, 2020)
DIAS_CALENDARIO = 90   # calendario de reajustes: vigencias que terminam nos proximos N dias
DIAS_ATIVA = 180       # distribuidora "ativa": vigencia atual terminou ha no maximo N dias (ou ainda vale)

FILTRO_TARIFAS = {
    "DscSubGrupo": "B3",
    "DscModalidadeTarifaria": "Convencional",
    "DscBaseTarifaria": "Tarifa de Aplicação",
}
FILTRO_FIOB = {
    "DscComponenteTarifario": "TUSD_FioB",
    "DscSubGrupoTarifario": "B3",
    "DscModalidadeTarifaria": "Convencional",
    "DscBaseTarifaria": "Tarifa de Aplicação",
}

MAPA_TARIFAS = {
    "NumCNPJDistribuidora": "cnpj",
    "SigAgente": "distribuidora",
    "DscREH": "reh",
    "DatInicioVigencia": "inicio",
    "DatFimVigencia": "fim",
    "DscBaseTarifaria": "base",
    "DscSubGrupo": "subgrupo",
    "DscModalidadeTarifaria": "modalidade",
    "DscClasse": "classe",
    "DscSubClasse": "subclasse",
    "DscDetalhe": "detalhe",
    "NomPostoTarifario": "posto",
    "DscUnidadeTerciaria": "unidade",
    "SigAgenteAcessante": "acessante",
    "VlrTUSD": "tusd",
    "VlrTE": "te",
}
MAPA_FIOB = {
    "NumCPFCNPJ": "cnpj",
    "SigNomeAgente": "distribuidora",
    "DscResolucaoHomologatoria": "reh",
    "DatInicioVigencia": "inicio",
    "DatFimVigencia": "fim",
    "DscBaseTarifaria": "base",
    "DscSubGrupoTarifario": "subgrupo",
    "DscModalidadeTarifaria": "modalidade",
    "DscClasseConsumidor": "classe",
    "DscSubClasseConsumidor": "subclasse",
    "DscDetalheConsumidor": "detalhe",
    "DscPostoTarifario": "posto",
    "DscUnidade": "unidade",
    "SigNomeAgenteAcessante": "acessante",
    "VlrComponenteTarifario": "fiob",
}

DIMS = ["cnpj", "distribuidora", "reh", "inicio", "fim", "base", "subgrupo", "modalidade",
        "classe", "subclasse", "detalhe", "posto", "unidade", "acessante"]
# Nome da distribuidora fica fora da chave: muda ao longo do tempo (ex.: AMPLA -> ENEL RJ).
DIMS_CHAVE = [d for d in DIMS if d != "distribuidora"]

SERIES = {
    "tarifa": {"arquivo": "tarifas_b3.csv", "valores": ["tusd", "te"], "rotulo": "B3 TUSD+TE"},
    "fiob": {"arquivo": "fiob_b3.csv", "valores": ["fiob"], "rotulo": "Fio B B3"},
    # Grupo A: o log grava "<rotulo> <subgrupo> <modalidade>" (ex.: "Grupo A TUSD+TE A4 Verde")
    "tarifa_a": {"arquivo": "tarifas_grupo_a.csv", "valores": ["tusd", "te"], "rotulo": "Grupo A TUSD+TE",
                 "grupo_a": True},
    "fiob_a": {"arquivo": "fiob_grupo_a.csv", "valores": ["fiob"], "rotulo": "Fio B Grupo A", "grupo_a": True},
}
# Snapshot cumulativo: a linha que some da fonte continua no snapshot com a data em que sumiu (vazio = ativa).
COL_REMOVIDA = "removida_fonte_em"
# Guarda contra falha da fonte (ver queda_suspeita)
QUEDA_MAX = 0.20
REMOCAO_MIN_BLOQUEIO = 50
DIAS_AVISO_REMOCAO = 90  # o painel avisa as remocoes da fonte dos ultimos N dias
ACEITAR_QUEDA = False    # --aceitar-queda: desliga as guardas de queda (recorte e snapshot) nesta execucao

# ---- Grupo A (media e alta tensao)
# Nome base dos paineis gerados (Excel e HTML)
PAINEL = "Painel_Tarifas_ANEEL"
SUBGRUPOS_A_PADRAO = ["A4", "A3"]
# Verde so existe para A3a, A4 e AS (A3, A2 e A1 so tem Azul); combinacoes sem dado na base ficam de fora.
MODALIDADES_A = ["Verde", "Azul"]
POSTOS_A = ["Ponta", "Fora ponta", NA]       # sem os postos sazonais antigos (seca/umida)
DETALHES_A = [NA, "SCEE"]                   # APE, "<500GWh" e cooperativas ficam fora do recorte
# (posto, unidade) de cada componente da vigencia: energia efp/ep (TUSD+TE, R$/MWh); demanda d
# (Verde: demanda unica; Azul: demanda na ponta) e dfp (Azul: demanda fora ponta), TUSD em R$/kW.
COMP_A = {"Verde": {("Fora ponta", "MWh"): "efp", ("Ponta", "MWh"): "ep", (NA, "kW"): "d"},
          "Azul": {("Fora ponta", "MWh"): "efp", ("Ponta", "MWh"): "ep", ("Ponta", "kW"): "d",
                   ("Fora ponta", "kW"): "dfp"}}
METRICAS_A = {"efp": ("Energia FP", "R$/MWh"), "ep": ("Energia P", "R$/MWh"),
              "d": ("Demanda", "R$/kW"), "dfp": ("Demanda FP", "R$/kW")}

# Leitura de cada Parquet: colunas usadas, filtro, coluna do CNPJ (int64 no arquivo) e de valores.
FONTES = {
    "tarifa": {"mapa": MAPA_TARIFAS, "filtro": FILTRO_TARIFAS, "cnpj": "NumCNPJDistribuidora",
               "valores": {"VlrTUSD", "VlrTE"}},
    "fiob": {"mapa": MAPA_FIOB, "filtro": FILTRO_FIOB, "cnpj": "NumCPFCNPJ",
             "valores": {"VlrComponenteTarifario"}},
}


def fontes_grupo_a(subgrupos: list[str]) -> tuple[dict, dict]:
    """Leitura do Grupo A nos dois Parquets (filtro com lista = qualquer um dos valores).

    So a linha canonica de classe, subclasse e acessante ("Nao se aplica"), detalhe normal ou SCEE e
    postos Ponta, Fora ponta e Nao se aplica: o recorte fica pequeno sem perder o que o painel usa.
    """
    comum = {"DscModalidadeTarifaria": MODALIDADES_A, "DscBaseTarifaria": "Tarifa de Aplicação"}
    ft = {"DscSubGrupo": list(subgrupos), **comum, "DscClasse": NA, "DscSubClasse": NA,
          "SigAgenteAcessante": NA, "DscDetalhe": DETALHES_A, "NomPostoTarifario": POSTOS_A}
    ff = {"DscComponenteTarifario": "TUSD_FioB", "DscSubGrupoTarifario": list(subgrupos), **comum,
          "DscClasseConsumidor": NA, "DscSubClasseConsumidor": NA, "SigNomeAgenteAcessante": NA,
          "DscDetalheConsumidor": DETALHES_A, "DscPostoTarifario": POSTOS_A}
    return ({"mapa": MAPA_TARIFAS, "filtro": ft, "cnpj": "NumCNPJDistribuidora", "valores": {"VlrTUSD", "VlrTE"}},
            {"mapa": MAPA_FIOB, "filtro": ff, "cnpj": "NumCPFCNPJ", "valores": {"VlrComponenteTarifario"}})

SESSAO = requests.Session()
SESSAO.headers["User-Agent"] = "monitor-tarifas-aneel/1.0"


# --------------------------------------------------------------------------- API

# Erros de rede/recusa vistos nesta execucao (para a mensagem final explicar o bloqueio, sem stack trace).
FALHAS_CONEXAO: list[str] = []
MSG_BLOQUEIO = "ANEEL recusou a conexao a partir do runner"


def _eh_recusa(e: Exception) -> bool:
    """Conexao recusada/resetada, TLS interrompido (SSL EOF), timeout de conexao ou HTTP 401/403/451."""
    if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.SSLError,
                      requests.exceptions.ConnectTimeout)):
        return True
    resp = getattr(e, "response", None)
    return resp is not None and getattr(resp, "status_code", None) in (401, 403, 451)


def _descrever(e: Exception) -> str:
    """Resumo de uma linha do erro; recusas de rede ganham o prefixo MSG_BLOQUEIO."""
    txt = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
    if _eh_recusa(e):
        FALHAS_CONEXAO.append(type(e).__name__)
        return f"{MSG_BLOQUEIO} ({type(e).__name__}: {txt})"
    return f"{type(e).__name__}: {txt}"


def _espera(i: int) -> int:
    return 10 * 2 ** i  # backoff: 10 s, 20 s, 40 s ...


def chamar_api(acao: str, params: dict, tentativas: int = 4) -> dict:
    ultimo_erro = None
    for i in range(tentativas):
        try:
            # (conexao, leitura): nenhuma chamada fica pendurada mais de 90 s
            r = SESSAO.get(API + acao, params=params, timeout=(20, 90))
            r.raise_for_status()
            j = r.json()
            if not j.get("success"):
                raise RuntimeError(f"API ANEEL retornou erro: {j.get('error')}")
            return j["result"]
        except Exception as e:  # noqa: BLE001
            ultimo_erro = _descrever(e)
            if i < tentativas - 1:
                print(f"  [aviso] {acao} tentativa {i + 1}/{tentativas} falhou: {ultimo_erro}; "
                      f"nova tentativa em {_espera(i)}s", flush=True)
                time.sleep(_espera(i))
    raise RuntimeError(f"Falha definitiva em {acao} apos {tentativas} tentativas: {ultimo_erro}")


def _info_parquet(r: dict) -> dict:
    return {"resource_id": r["id"], "url": r["url"],
            "last_modified": r.get("last_modified") or r.get("metadata_modified") or "",
            "size": r.get("size")}


def _eh_parquet(r: dict) -> bool:
    return (str(r.get("format") or "").upper() == "PARQUET"
            or str(r.get("url") or "").lower().endswith(".parquet"))


def parquet_tarifas() -> dict:
    """Arquivo Parquet do dataset de tarifas homologadas (resource fixo; se sumir, o unico PARQUET)."""
    pkg = chamar_api("package_show", {"id": PKG_TARIFAS})
    pqs = [r for r in pkg.get("resources", []) if _eh_parquet(r) and r.get("url")]
    escolhido = next((r for r in pqs if r["id"] == RES_TARIFAS_PARQUET), None)
    if escolhido is None and len(pqs) == 1:
        escolhido = pqs[0]
        print(f"  [aviso] resource {RES_TARIFAS_PARQUET} nao encontrado; usado o Parquet {escolhido['id']}")
    if escolhido is None:
        raise RuntimeError(f"Parquet de tarifas nao encontrado em {PKG_TARIFAS} (layout mudou?)")
    return _info_parquet(escolhido)


def parquets_componentes(ano_min: int) -> dict[int, dict]:
    """Parquet anual de componentes-tarifarias, para cada ano >= ano_min (anos novos entram sozinhos)."""
    pkg = chamar_api("package_show", {"id": PKG_COMPONENTES})
    anos: dict[int, dict] = {}
    for r in pkg.get("resources", []):
        if not (_eh_parquet(r) and r.get("url")):
            continue
        achou = re.findall(r"(20\d{2})", r.get("name") or "") or re.findall(r"(20\d{2})", r["url"])
        if achou and int(achou[-1]) >= ano_min:
            anos[int(achou[-1])] = _info_parquet(r)
    if not anos:
        raise RuntimeError(f"Nenhum Parquet anual >= {ano_min} em {PKG_COMPONENTES} (layout mudou?)")
    return dict(sorted(anos.items()))


# ------------------------------------------------------------------- Parquet

def _simplificar(nome: str) -> str:
    s = unicodedata.normalize("NFKD", str(nome)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def baixar_arquivo(url: str, destino: Path, tentativas: int = 3) -> int:
    """Download em streaming; timeout de 90 s por leitura de bloco e limite total."""
    ultimo_erro = None
    for i in range(tentativas):
        try:
            inicio, n = time.monotonic(), 0
            with SESSAO.get(url, stream=True, timeout=(20, 90)) as r:
                r.raise_for_status()
                with open(destino, "wb") as fh:
                    for bloco in r.iter_content(1 << 20):
                        fh.write(bloco)
                        n += len(bloco)
                        if time.monotonic() - inicio > TEMPO_MAX_DOWNLOAD:
                            raise TimeoutError(f"download passou de {TEMPO_MAX_DOWNLOAD} s")
            return n
        except Exception as e:  # noqa: BLE001
            ultimo_erro = _descrever(e)
            if i < tentativas - 1:
                print(f"  [aviso] download tentativa {i + 1}/{tentativas} falhou: {ultimo_erro}; "
                      f"nova tentativa em {_espera(i)}s", flush=True)
                time.sleep(_espera(i))
    raise RuntimeError(f"Falha no download de {url} apos {tentativas} tentativas: {ultimo_erro}")


def ler_parquet(caminho: Path, fonte: dict, inicio_min: str | None = None) -> pd.DataFrame:
    """Le do Parquet so as colunas usadas e aplica o filtro, bloco a bloco (pouca memoria).

    Devolve as colunas com os nomes originais e valores como texto, prontos para normalizar():
    datas date32/timestamp viram 'AAAA-MM-DD', CNPJ inteiro vira texto com 14 digitos e os valores
    ficam numericos (ou texto com virgula, se o arquivo trouxer texto; para_numero trata os dois).
    inicio_min ('AAAA-MM-DD'): mantem so vigencias com DatInicioVigencia >= inicio_min.
    Linhas repetidas (o Parquet tem muitas) saem aqui.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    mapa, filtro, col_cnpj, cols_valor = fonte["mapa"], fonte["filtro"], fonte["cnpj"], fonte["valores"]
    colunas = list(dict.fromkeys(list(mapa) + list(filtro)))
    pf = pq.ParquetFile(caminho)
    nomes = pf.schema_arrow.names
    por_simples = {_simplificar(n): n for n in nomes}
    reais, faltando = {}, []
    for c in colunas:
        real = c if c in nomes else por_simples.get(_simplificar(c))
        if real is None:
            faltando.append(c)
        reais[c] = real
    if faltando:
        raise RuntimeError(f"Parquet sem as colunas {faltando} (layout mudou?). Colunas: {nomes}")

    def texto(arr):
        if pa.types.is_dictionary(arr.type):
            arr = arr.cast(arr.type.value_type)
        if pa.types.is_timestamp(arr.type):
            arr = arr.cast(pa.date32())
        if not (pa.types.is_string(arr.type) or pa.types.is_large_string(arr.type)):
            arr = arr.cast(pa.string())
        return arr

    # Aceita o valor do filtro em NFC ou NFD (acentos compostos ou decompostos). pc.utf8_normalize
    # nao foi usado: no pyarrow 25 a chamada com "NFC" devolveu texto decomposto e zerou o filtro.
    # Valor do filtro: texto ou lista de textos (qualquer um deles).
    alvos = {c: pa.array(sorted({unicodedata.normalize(f, x) for f in ("NFC", "NFD")
                                 for x in ([v] if isinstance(v, str) else v)}))
             for c, v in filtro.items()}
    partes = []
    for i in range(pf.metadata.num_row_groups):
        t = pf.read_row_group(i, columns=list(dict.fromkeys(reais.values())))
        mascara = None
        for c in filtro:
            col = pc.utf8_trim_whitespace(texto(t[reais[c]]))
            m = pc.is_in(col, value_set=alvos[c])
            mascara = m if mascara is None else pc.and_kleene(mascara, m)
        t = t.filter(pc.fill_null(mascara, False))
        if t.num_rows:
            partes.append(t)
    dados = {}
    for c in colunas:
        arrs = [p[reais[c]] for p in partes]
        if c in cols_valor:
            serie = pd.Series([v for a in arrs for v in a.to_pylist()], dtype=object)
        else:
            tipo_int = bool(arrs) and pa.types.is_integer(arrs[0].type)
            serie = pd.Series([v for a in arrs for v in texto(pa.chunked_array(a.chunks)).to_pylist()],
                              dtype=object)
            if tipo_int and c == col_cnpj:
                serie = serie.map(lambda v: v if v is None else str(v).zfill(14))
            else:
                serie = serie.map(lambda v: v if v is None else unicodedata.normalize("NFC", v))
        dados[c] = serie
    df = pd.DataFrame(dados, columns=colunas)
    datas = df["DatInicioVigencia"].dropna()
    if len(datas) and not datas.str.match(r"^\d{4}-\d{2}-\d{2}$").all():
        raise RuntimeError(f"DatInicioVigencia fora do formato AAAA-MM-DD (ex.: {datas.iloc[0]!r}); layout mudou?")
    if inicio_min:
        df = df[df["DatInicioVigencia"].fillna("") >= inicio_min]
    # Ordem fixa: a ANEEL recria o arquivo em outra ordem a cada carga; assim o recorte salvo so muda
    # quando o conteudo muda.
    dims = [c for c in colunas if c not in cols_valor]
    return df.drop_duplicates().sort_values(dims, na_position="first", kind="mergesort").reset_index(drop=True)


def ler_recorte(caminho: Path) -> pd.DataFrame:
    return pd.read_csv(caminho, dtype=str, keep_default_na=False, encoding="utf-8")


def atualizar_recortes(info: dict | None, pedidos: dict, dados: Path, meta_ant: dict,
                       avisos: list[str]) -> dict[str, tuple[pd.DataFrame, dict]]:
    """Recortes filtrados de UM Parquet, cada um com cache em data/recorte_<nome>.csv.

    pedidos: {nome: (fonte, inicio_min ou None, metadados extras ou None)}. Os extras (ex.: lista de
    subgrupos do Grupo A) entram na comparacao do cache: mudar o config.json forca nova leitura.
    - last_modified igual ao do cache (e mesmos inicio_min e extras): le o cache, sem download;
    - algum recorte desatualizado ou ausente: baixa o Parquet UMA vez (streaming, pasta temporaria),
      filtra cada recorte pendente e apaga o arquivo bruto;
    - download ou leitura falhou (ou info=None, package_show indisponivel): usa o cache, se houver,
      e registra o aviso; sem cache, propaga o erro.
    Retorna {nome: (recorte com colunas originais, metadados para estado.json)}.
    """
    res, metas, pendentes = {}, {}, []
    for nome, (_, inicio_min, extra) in pedidos.items():
        cache, ant = dados / f"recorte_{nome}.csv", meta_ant.get(nome, {})
        if info is not None:
            meta = {k: info.get(k) for k in ("resource_id", "last_modified", "size")}
            if inicio_min:
                meta["inicio_min"] = inicio_min
            meta.update(extra or {})
            metas[nome] = meta
            if cache.exists() and all(ant.get(k) == meta[k] for k in meta):
                df = ler_recorte(cache)
                print(f"  {nome}: sem mudanca (last_modified {meta['last_modified']}); recorte {cache.name}, "
                      f"{len(df)} linhas", flush=True)
                res[nome] = (df, dict(meta, linhas=len(df), baixado_em=ant.get("baixado_em", ""), mb=0.0))
                continue
        pendentes.append(nome)
    if not pendentes:
        return res
    falhas: dict[str, Exception] = {}
    erro: Exception | None = None
    if info is None:
        erro = RuntimeError("package_show indisponivel")
    else:
        tmp = Path(tempfile.mkdtemp(prefix="aneel_parquet_"))
        try:
            arq = tmp / "arquivo.parquet"
            t0 = time.monotonic()
            print(f"  {', '.join(pendentes)}: baixando Parquet {info['resource_id']} "
                  f"({(info.get('size') or 0) / 1e6:.1f} MB, last_modified {info['last_modified']}) ...", flush=True)
            n = baixar_arquivo(info["url"], arq)
            mb = round(n / 1e6, 1)
            print(f"  {n / 1e6:.1f} MB em {time.monotonic() - t0:.0f}s", flush=True)
            for nome in pendentes:
                fonte, inicio_min, _ = pedidos[nome]
                cache, n_ant = dados / f"recorte_{nome}.csv", meta_ant.get(nome, {}).get("linhas") or 0
                try:
                    df = ler_parquet(arq, fonte, inicio_min)
                    print(f"  {nome}: {len(df)} linhas no recorte", flush=True)
                    if cache.exists() and n_ant and len(df) < (1 - QUEDA_MAX) * n_ant and not ACEITAR_QUEDA:
                        raise RuntimeError(f"recorte novo com {len(df)} linhas contra {n_ant} no cache "
                                           "(queda > 20%; arquivo incompleto?)")
                    df.to_csv(cache, index=False, encoding="utf-8")
                    res[nome] = (ler_recorte(cache), dict(metas[nome], linhas=len(df), mb=mb,
                                                          baixado_em=datetime.now(FUSO_BR).strftime("%Y-%m-%d")))
                    mb = 0.0  # o download conta uma vez so
                except Exception as e:  # noqa: BLE001
                    falhas[nome] = e
        except Exception as e:  # noqa: BLE001
            erro = e
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    for nome in pendentes:
        if nome in res:
            continue
        e = falhas.get(nome, erro)
        cache, ant = dados / f"recorte_{nome}.csv", meta_ant.get(nome, {})
        if not cache.exists():
            raise RuntimeError(f"{nome}: {e} e nao ha recorte salvo em {cache}") from e
        df = ler_recorte(cache)
        msg = (f"{nome}: falha ao atualizar ({e}); usado o recorte salvo {cache.name} "
               f"(last_modified {ant.get('last_modified') or '?'}, baixado em {ant.get('baixado_em') or '?'})")
        print(f"  [aviso] {msg}", flush=True)
        avisos.append(msg)
        res[nome] = (df, dict(ant, linhas=len(df), mb=0.0))
    return res


# ------------------------------------------------------------ IPCA (Banco Central)

def mes_mais(mes: str, k: int) -> str:
    """'AAAA-MM' + k meses."""
    a, m = int(mes[:4]), int(mes[5:7]) - 1 + k
    return f"{a + m // 12:04d}-{m % 12 + 1:02d}"


def baixar_ipca(tentativas: int = 3) -> dict[str, float]:
    """Serie mensal do IPCA (% a.m.) desde 2010, {'AAAA-MM': valor}. Timeout de 90 s por chamada."""
    ultimo_erro = None
    for i in range(tentativas):
        try:
            r = SESSAO.get(IPCA_URL, params={"formato": "json", "dataInicial": IPCA_INICIO}, timeout=(20, 90))
            r.raise_for_status()
            mensal = {}
            for x in r.json():
                d = str(x["data"])
                if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", d):
                    raise RuntimeError(f"data fora do formato dd/mm/aaaa: {d!r}")
                mensal[f"{d[6:10]}-{d[3:5]}"] = round(float(str(x["valor"]).replace(",", ".")), 4)
            meses = sorted(mensal)
            if len(meses) < 100 or meses[0] != "2010-01":
                raise RuntimeError(f"serie incompleta ({len(meses)} meses, inicio {meses[:1]})")
            if any(mes_mais(a, 1) != b for a, b in zip(meses, meses[1:])):
                raise RuntimeError("serie com meses faltando")
            return {m: mensal[m] for m in meses}
        except Exception as e:  # noqa: BLE001
            ultimo_erro = e
            if i < tentativas - 1:
                print(f"  [aviso] IPCA (BCB) falhou ({e}); nova tentativa em {10 * (i + 1)}s", flush=True)
                time.sleep(10 * (i + 1))
    raise RuntimeError(f"Falha ao baixar o IPCA do BCB: {ultimo_erro}")


def ler_ipca(dados: Path) -> dict | None:
    arq = dados / ARQ_IPCA
    if not arq.exists():
        return None
    try:
        c = json.loads(arq.read_text(encoding="utf-8"))
        return c if c.get("mensal") else None
    except Exception:  # noqa: BLE001
        return None


def atualizar_ipca(dados: Path, hoje: date, avisos: list[str]) -> tuple[dict | None, bool]:
    """Cache do IPCA em data/ipca_bcb.json. Retorna (cache, houve dado novo).

    Baixa de novo so quando o mes muda desde o ultimo download. Enquanto o IPCA do mes anterior nao
    sair (o IBGE divulga por volta do dia 10), tenta no maximo 1 vez por dia. Falhou: usa o cache e avisa.
    """
    cache = ler_ipca(dados)
    hoje_iso, mes = hoje.isoformat(), hoje.strftime("%Y-%m")
    if cache:
        ultimo = max(cache["mensal"])
        if (str(cache.get("baixado_em", ""))[:7] == mes
                and (ultimo >= mes_mais(mes, -1) or cache.get("ultima_tentativa") == hoje_iso)):
            print(f"  IPCA: cache valido (ate {ultimo}, baixado em {cache.get('baixado_em')})", flush=True)
            return cache, False
    try:
        mensal = baixar_ipca()
    except Exception as e:  # noqa: BLE001
        if cache:
            msg = (f"IPCA: falha na API do Banco Central ({e}); usado o cache {ARQ_IPCA} "
                   f"(ate {max(cache['mensal'])}, baixado em {cache.get('baixado_em') or '?'})")
        else:
            msg = f"IPCA: falha na API do Banco Central ({e}) e sem cache; comparacoes com a inflacao indisponiveis"
        print(f"  [aviso] {msg}", flush=True)
        avisos.append(msg)
        return cache, False
    mudou = cache is None or cache.get("mensal") != mensal
    novo = {"fonte": "Banco Central do Brasil, SGS serie 433 (IPCA, variacao % mensal)",
            "url": IPCA_URL, "baixado_em": hoje_iso, "ultima_tentativa": hoje_iso, "mensal": mensal}
    dados.mkdir(parents=True, exist_ok=True)
    (dados / ARQ_IPCA).write_text(json.dumps(novo, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                  encoding="utf-8")
    print(f"  IPCA: {len(mensal)} meses, ate {max(mensal)}" + (" (dado novo)" if mudou else " (sem mudanca)"),
          flush=True)
    return novo, mudou


def ipca_acumulado(mensal: dict | None, m0: str, m1: str) -> tuple[float | None, str | None, str | None]:
    """IPCA acumulado de m0 a m1 (inclusive, 'AAAA-MM'), cortado no ultimo mes disponivel.

    Retorna (variacao, primeiro mes, ultimo mes usado); None se faltar mes no intervalo.
    """
    if not mensal or m0 > m1:
        return None, None, None
    m1 = min(m1, max(mensal))
    if m0 > m1 or m0 < min(mensal):
        return None, None, None
    f, m = 1.0, m0
    while m <= m1:
        if m not in mensal:
            return None, None, None
        f *= 1 + mensal[m] / 100
        m = mes_mais(m, 1)
    return f - 1, m0, m1


# -------------------------------------------------- distribuidoras (nome comercial)

def ler_distribuidoras(base: Path) -> pd.DataFrame:
    """distribuidoras.csv da pasta base ou, se nao houver, da pasta do script. Indice: CNPJ (14 digitos)."""
    for arq in (base / ARQ_DISTRIBUIDORAS, Path(__file__).resolve().parent / ARQ_DISTRIBUIDORAS):
        if not arq.exists():
            continue
        # sep=None: aceita ',' ou ';' (CSV salvo pelo Excel em pt-BR); utf-8-sig: aceita BOM
        df = pd.read_csv(arq, dtype=str, keep_default_na=False, encoding="utf-8-sig", sep=None, engine="python")
        df.columns = [c.strip().lower() for c in df.columns]
        faltando = [c for c in COLS_DISTRIBUIDORAS if c not in df.columns]
        if faltando:
            raise RuntimeError(f"{arq.name} sem as colunas {faltando}")
        for c in df.columns:
            df[c] = df[c].astype(str).str.strip()
        df["cnpj"] = df["cnpj"].str.replace(r"\D", "", regex=True).str.zfill(14)
        df["uf"] = df["uf"].str.upper()
        df["regiao"] = df["regiao"].where(df["regiao"] != "", df["uf"].map(REGIAO_UF).fillna(""))
        return df.drop_duplicates("cnpj", keep="last").set_index("cnpj")
    return pd.DataFrame(columns=COLS_DISTRIBUIDORAS[1:], index=pd.Index([], name="cnpj"))


def rotulo_dist(nome: str, sigla: str) -> str:
    """'ENEL SP (ELETROPAULO)'; so o nome quando a sigla e igual ou prefixo (ex.: CEMIG x CEMIG-D)."""
    nome, sigla = (nome or "").strip(), (sigla or "").strip()
    if not nome:
        return sigla
    s1, s2 = _simplificar(nome), _simplificar(sigla)
    if not s2 or s2 == "naoinformado" or s1.startswith(s2) or s2.startswith(s1):
        return nome
    return f"{nome} ({sigla})"


# --------------------------------------------------------------------- tratamento

def para_numero(v) -> float:
    if v is None:
        return float("nan")
    s = str(v).strip()
    if not s:
        return float("nan")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def adicionar_chave(df: pd.DataFrame) -> pd.DataFrame:
    df["chave"] = df[DIMS_CHAVE].agg("|".join, axis=1)
    return df


def normalizar(bruto: pd.DataFrame, mapa: dict, valores: list[str]) -> pd.DataFrame:
    faltando = [c for c in mapa if c not in bruto.columns]
    if faltando:
        raise RuntimeError(f"Campos ausentes no recorte (layout mudou?): {faltando}")
    df = bruto[list(mapa)].rename(columns=mapa)
    for c in DIMS:
        df[c] = df[c].fillna("").astype(str).str.strip()
    for c in valores:
        df[c] = df[c].map(para_numero).round(6)
    df = df.drop_duplicates()
    df = adicionar_chave(df)
    dup = df["chave"].duplicated(keep=False)
    if dup.any():
        exemplos = sorted({f"{d} {i}" for d, i in df.loc[dup, ["distribuidora", "inicio"]].values})
        print(f"  [aviso] {int(dup.sum())} linhas com mesma chave e valores diferentes "
              f"({'; '.join(exemplos[:5])}); mantido o maior valor")
    # A ordem das linhas muda a cada recarga do arquivo: ordenar antes de descartar torna a
    # escolha deterministica e evita "alteracoes" falsas entre execucoes.
    df = df.sort_values(["chave"] + valores, na_position="first").drop_duplicates("chave", keep="last")
    return df.sort_values(["distribuidora", "inicio", "chave"]).reset_index(drop=True)


def com_marca(df: pd.DataFrame) -> pd.DataFrame:
    """Garante a coluna removida_fonte_em (vazia = linha ativa na fonte), sem NaN."""
    df = df.copy()
    if COL_REMOVIDA not in df.columns:
        df[COL_REMOVIDA] = ""
    df[COL_REMOVIDA] = df[COL_REMOVIDA].fillna("").astype(str).str.strip()
    return df


def ativas(df: pd.DataFrame) -> pd.DataFrame:
    """Linhas do snapshot que ainda estao na fonte."""
    return df if COL_REMOVIDA not in df.columns else df[df[COL_REMOVIDA].fillna("") == ""]


def carregar(caminho: Path, valores: list[str]) -> pd.DataFrame | None:
    if not caminho.exists():
        return None
    df = pd.read_csv(caminho, dtype=str, keep_default_na=False, encoding="utf-8")
    for c in valores:
        df[c] = pd.to_numeric(df[c], errors="coerce").round(6)
    return adicionar_chave(com_marca(df))


def salvar(df: pd.DataFrame, caminho: Path, valores: list[str]) -> None:
    com_marca(df)[DIMS + valores + [COL_REMOVIDA]].to_csv(caminho, index=False, encoding="utf-8",
                                                           float_format="%.6f")


def ordenar_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values(["distribuidora", "inicio", "chave"]).reset_index(drop=True)


def comparar(antigo: pd.DataFrame, novo: pd.DataFrame, valores: list[str], data_remocao: str = ""):
    """Compara o recorte novo da fonte com o snapshot CUMULATIVO, pela chave de negocio.

    Retorna (novos, alterados, removidos, voltaram, snapshot):
    - novos: chaves que nunca estiveram no snapshot;
    - alterados: chaves do recorte ja presentes no snapshot (ativas ou removidas) com valor diferente;
    - removidos: chaves ATIVAS no snapshot que sumiram do recorte; ficam no snapshot com
      removida_fonte_em = data_remocao (as ja marcadas mantem a data original);
    - voltaram: chaves marcadas como removidas que reapareceram com o mesmo valor (a marca e limpa, sem alerta);
    - snapshot: recorte novo (ativo) + linhas que nao estao mais na fonte, com a marca.
    """
    a = com_marca(antigo).set_index("chave")
    n = com_marca(novo).set_index("chave")
    n[COL_REMOVIDA] = ""
    rem_ant = a[COL_REMOVIDA] != ""
    novos = n.loc[n.index.difference(a.index)].reset_index()
    fora = a.index.difference(n.index)
    resto = a.loc[fora].copy()
    sumiu = ~rem_ant.loc[fora].to_numpy()
    removidos = resto[sumiu].reset_index()
    resto.loc[sumiu, COL_REMOVIDA] = data_remocao
    comuns = n.index.intersection(a.index)
    va, vn = a.loc[comuns, valores], n.loc[comuns, valores]
    mudou = (((va - vn).abs() > 1e-6) | (va.isna() != vn.isna())).any(axis=1)
    idx = mudou[mudou].index
    alterados = n.loc[idx].copy()
    for c in valores:
        alterados[c + "_anterior"] = va.loc[idx, c]
    voltaram = n.loc[comuns[rem_ant.loc[comuns].to_numpy() & ~mudou.to_numpy()]].reset_index()
    snap = ordenar_snapshot(pd.concat([n, resto]).rename_axis("chave").reset_index())
    return novos, alterados.reset_index(), removidos, voltaram, snap


def queda_suspeita(n_ativas: int, n_novo: int, n_removidas: int) -> str | None:
    """Guarda contra falha da fonte (ex.: a ANEEL publica um arquivo pela metade).

    Com o snapshot cumulativo, a base de comparacao sao as linhas ATIVAS do snapshot (as ja removidas da fonte
    nao contam). Bloqueia a gravacao quando, numa mesma coleta:
      (a) o recorte novo tem menos de 80% das linhas ativas (queda de tamanho > QUEDA_MAX), ou
      (b) somem da fonte mais de 20% das linhas ativas, mesmo que linhas novas compensem o tamanho.
    Remocoes com menos de REMOCAO_MIN_BLOQUEIO linhas nunca bloqueiam (series pequenas, retificacoes pontuais).
    Remocoes moderadas (ex.: 24/11/2026, a ANEEL tirou 8% da B3 e 10% do Grupo A) passam: as linhas ficam no
    snapshot marcadas e vao para o log como removida_na_fonte. Retorna o motivo do bloqueio ou None.
    """
    if n_ativas <= 0 or n_removidas < REMOCAO_MIN_BLOQUEIO:
        return None
    if n_novo < (1 - QUEDA_MAX) * n_ativas:
        return (f"recorte com {n_novo} linhas contra {n_ativas} linhas ativas no snapshot "
                f"(queda > {QUEDA_MAX:.0%})")
    if n_removidas > QUEDA_MAX * n_ativas:
        return f"{n_removidas} das {n_ativas} linhas ativas do snapshot sumiram da fonte (> {QUEDA_MAX:.0%})"
    return None


def sem_substituidas(df: pd.DataFrame, grupo: list[str]) -> pd.DataFrame:
    """Retificacao: tira as linhas removidas da fonte quando ha linha ATIVA no mesmo grupo (mesma distribuidora e
    mesmo inicio de vigencia; no Grupo A, tambem subgrupo e modalidade). Removidas sem substituta ficam."""
    if COL_REMOVIDA not in df.columns or df.empty:
        return df
    rem = (df[COL_REMOVIDA].fillna("") != "").to_numpy()
    if not rem.any():
        return df
    idx = pd.MultiIndex.from_frame(df[grupo])
    return df[~(rem & idx.isin(idx[~rem]))]


def resumo_removidas(frames: list[pd.DataFrame | None]) -> list[dict]:
    """Por data de remocao: vigencias distintas (CNPJ, resolucao, inicio), linhas e anos de inicio."""
    partes = [f[f[COL_REMOVIDA].fillna("") != ""][["cnpj", "reh", "inicio", COL_REMOVIDA]]
              for f in frames if f is not None and len(f) and COL_REMOVIDA in f.columns]
    if not partes:
        return []
    r = pd.concat(partes, ignore_index=True)
    out = []
    for d, g in r.groupby(COL_REMOVIDA):
        out.append({"data": d, "vigencias": int(len(g.drop_duplicates(["cnpj", "reh", "inicio"]))),
                    "linhas": int(len(g)), "ano_min": g["inicio"].min()[:4], "ano_max": g["inicio"].max()[:4]})
    return sorted(out, key=lambda x: x["data"])


# ------------------------------------------------------------------- visao negocio

def canonico(df: pd.DataFrame, detalhe: str = NA) -> pd.DataFrame:
    """Linha 'cheia' de cada vigencia: sem classe/subclasse/posto/acessante especificos."""
    m = ((df["classe"] == NA) & (df["subclasse"] == NA) & (df["posto"] == NA)
         & (df["acessante"] == NA) & (df["detalhe"] == detalhe)
         & df["unidade"].str.contains("MWh", na=False))
    return df[m].copy()


def linha_do_tempo(df: pd.DataFrame, col: str, cat: pd.DataFrame | None = None) -> pd.DataFrame:
    """Uma linha por distribuidora (CNPJ) e inicio de vigencia, com a variacao contra a vigencia anterior.

    'distribuidora' passa a ser o rotulo de exibicao (nome comercial de distribuidoras.csv, com a sigla
    da ANEEL entre parenteses quando difere); 'sigla' guarda o nome mais recente usado pela ANEEL.
    """
    d = df.copy()
    d["id"] = d["cnpj"].where(d["cnpj"] != "", d["distribuidora"])
    # snapshot cumulativo: linha ativa prevalece sobre a removida da fonte com o mesmo inicio (retificacao)
    d = sem_substituidas(d, ["id", "inicio"])
    d = d.sort_values(["id", "inicio", "reh"]).drop_duplicates(["id", "inicio"], keep="last")
    siglas = d.groupby("id")["distribuidora"].last()  # nome mais recente na ANEEL
    nomes = siglas.copy()
    if cat is not None and len(cat):
        for i, s in siglas.items():
            if i in cat.index and cat.at[i, "nome_comercial"]:
                nomes[i] = rotulo_dist(cat.at[i, "nome_comercial"], s)
    d["sigla"] = d["id"].map(siglas)
    # todas as siglas ja usadas pela ANEEL para o CNPJ (ex.: CELPE e Neoenergia PE), para a busca do painel
    hist = {i: "; ".join(sorted(set(s) - {siglas[i]})) for i, s in d.groupby("id")["distribuidora"]}
    d["siglas_antigas"] = d["id"].map(hist)
    # CNPJs diferentes com o mesmo nome (ex.: RGE antiga x RGE SUL renomeada RGE) seriam
    # fundidos nas matrizes por nome: o CNPJ menos recente recebe sufixo.
    ultimo_inicio = d.groupby("id")["inicio"].max()
    for nome, ids in nomes.groupby(nomes).groups.items():
        if len(ids) > 1:
            mais_recente = ultimo_inicio.loc[list(ids)].idxmax()
            for i in ids:
                if i != mais_recente:
                    nomes.loc[i] = f"{nome} (CNPJ {i}, ate {ultimo_inicio[i][:4]})"
    d["distribuidora"] = d["id"].map(nomes)
    d["anterior"] = d.groupby("id")[col].shift()
    d["inicio_anterior"] = d.groupby("id")["inicio"].shift()
    d["var_pct"] = d[col] / d["anterior"] - 1
    d["ano"] = d["inicio"].str[:4].astype(int)
    return d.reset_index(drop=True)


def matriz_anual(tl: pd.DataFrame, col: str) -> pd.DataFrame:
    """Distribuidora x ano: valor da ultima vigencia iniciada no ano."""
    t = tl.sort_values("inicio").drop_duplicates(["id", "ano"], keep="last")
    p = t.pivot_table(index="distribuidora", columns="ano", values=col, aggfunc="last")
    p.columns = [int(c) for c in p.columns]
    return p.sort_index()


def variacao(p: pd.DataFrame) -> pd.DataFrame:
    return p.div(p.shift(axis=1)) - 1


def suspeito(v) -> pd.Series | bool:
    """Fio B SCEE <= FIOB_SUSPEITO_MAX R$/MWh: valor implausivel (ex.: 0,00 ou centavos)."""
    return v <= FIOB_SUSPEITO_MAX


def ranking_fiob(tl: pd.DataFrame) -> pd.DataFrame:
    """Variacao do Fio B SCEE entre a vigencia mais recente e a anterior da mesma distribuidora.

    tl ja vem so com vigencias a partir de fiob_ano_inicial. Fica fora quem nao tem vigencia
    anterior no periodo ou tem valor suspeito (<= FIOB_SUSPEITO_MAX) em uma das duas.
    """
    ult = tl.sort_values("inicio").drop_duplicates("id", keep="last")
    ult = ult[ult["anterior"].notna() & ~suspeito(ult["fiob"]) & ~suspeito(ult["anterior"])]
    return ult.sort_values(["var_pct", "distribuidora"], ascending=[False, True]).reset_index(drop=True)


def _patamar(tot: list, k: int) -> int:
    """Indice da primeira vigencia do patamar que termina em k (vigencias seguidas com o mesmo total;
    a ANEEL divide a vigencia em 01/01 por causa do escalonamento do Fio B, sem mudar a B3)."""
    while k > 0 and pd.notna(tot[k]) and pd.notna(tot[k - 1]) and abs(tot[k] - tot[k - 1]) < 1e-6:
        k -= 1
    return k


def _valor_em(ini: list[str], fim: list[str], tot: list, data: str):
    """Valor da vigencia em vigor na data (inicio <= data <= fim); None se nao havia vigencia."""
    k = bisect_right(ini, data) - 1
    if k < 0 or (fim[k] and fim[k] < data):
        return None
    return tot[k]


def _real(nominal, inflacao):
    if nominal is None or inflacao is None or pd.isna(nominal) or pd.isna(inflacao):
        return None
    return (1 + nominal) / (1 + inflacao) - 1


def metricas(tl_t: pd.DataFrame, tl_f: pd.DataFrame, cat: pd.DataFrame, ipca: dict | None,
             hoje: date) -> tuple[pd.DataFrame, dict]:
    """Situacao atual de cada distribuidora (uma linha por CNPJ, indice = rotulo de exibicao).

    Vigencia atual = ultima com inicio <= hoje. Inclui valor do kWh compensado (GD II), acumulados
    contra o IPCA, proximo reajuste e posicao nos rankings (so distribuidoras ativas).
    Retorna (tabela, contexto com os periodos de IPCA usados).
    """
    mensal = (ipca or {}).get("mensal") or {}
    hoje_iso = hoje.isoformat()
    lim_ativa = (hoje - timedelta(days=DIAS_ATIVA)).isoformat()
    try:
        d12 = hoje.replace(year=hoje.year - 1)
    except ValueError:  # 29/02
        d12 = hoje.replace(year=hoje.year - 1, day=28)
    mes_fim = mes_mais(hoje.strftime("%Y-%m"), -1)  # ultimo mes completo
    ult_ipca = min(mes_fim, max(mensal)) if mensal else None
    ctx = {"ipca_ultimo_mes": ult_ipca, "periodos": {}}
    ipca_ano = {}
    for ano in ANOS_ACUMULADO:
        ipca_ano[ano] = ipca_acumulado(mensal, f"{ano}-01", mes_fim)
        ctx["periodos"][str(ano)] = ipca_ano[ano]
    ipca_12 = ipca_acumulado(mensal, mes_mais(ult_ipca, -11), ult_ipca) if ult_ipca else (None, None, None)
    ctx["periodos"]["12m"] = ipca_12

    fiob_id = {i: g.sort_values("inicio").reset_index(drop=True) for i, g in tl_f.groupby("id")}
    linhas = []
    for i, g in tl_t.groupby("id"):
        g = g.sort_values("inicio").reset_index(drop=True)
        ini, fim, tot = g["inicio"].tolist(), g["fim"].tolist(), g["total"].tolist()
        k = max(bisect_right(ini, hoje_iso) - 1, 0)
        a = g.iloc[k]
        info = cat.loc[i] if i in cat.index else None
        pega = lambda c: (str(info[c]) if info is not None and c in info.index else "")  # noqa: E731
        antigas = [x.strip() for x in (a["siglas_antigas"] + ";" + pega("nomes_anteriores")).split(";")]
        antigas = [x for x in dict.fromkeys(antigas) if x and _simplificar(x) != _simplificar(a["sigla"])]
        r = {"cnpj": a["cnpj"], "distribuidora": a["distribuidora"], "sigla": a["sigla"],
             "siglas_antigas": "; ".join(antigas),
             "nome": pega("nome_comercial") or a["sigla"], "uf": pega("uf"), "regiao": pega("regiao"),
             "grupo": pega("grupo_economico"), "razao_social": pega("razao_social"), "ufs": pega("ufs_atendidas"),
             "no_catalogo": info is not None,
             "ativa": bool((a["fim"] or "9999") >= lim_ativa or k + 1 < len(g)),
             "inicio": a["inicio"], "fim": a["fim"], "reh": a["reh"],
             "tusd": a["tusd"], "te": a["te"], "total": a["total"]}
        # ultimo reajuste: patamar atual contra o patamar anterior
        p = _patamar(tot, k)
        r["ultimo_reajuste"] = ini[p]
        r["anterior"] = r["var_ult"] = r["ipca_ult"] = r["real_ult"] = None
        r["ipca_ult_de"] = r["ipca_ult_ate"] = None
        if p > 0 and pd.notna(tot[p - 1]) and tot[p - 1]:
            pa = _patamar(tot, p - 1)
            r["anterior"], r["inicio_anterior"] = tot[p - 1], ini[pa]
            r["var_ult"] = a["total"] / tot[p - 1] - 1
            v, de, ate = ipca_acumulado(mensal, ini[pa][:7], mes_mais(ini[p][:7], -1))
            r["ipca_ult"], r["ipca_ult_de"], r["ipca_ult_ate"] = v, de, ate
            r["real_ult"] = _real(r["var_ult"], v)
        # acumulados desde 01/01 dos anos-base e em 12 meses
        for ano in ANOS_ACUMULADO:
            v0 = _valor_em(ini, fim, tot, f"{ano}-01-01")
            ac = a["total"] / v0 - 1 if v0 else None
            anos = (hoje - date(ano, 1, 1)).days / 365.25
            r[f"b3_{ano}"], r[f"acum_{ano}"] = v0, ac
            r[f"cagr_{ano}"] = (1 + ac) ** (1 / anos) - 1 if ac is not None and anos > 0 else None
            r[f"ipca_{ano}"] = ipca_ano[ano][0]
            r[f"real_{ano}"] = _real(ac, ipca_ano[ano][0])
        v12 = _valor_em(ini, fim, tot, d12.isoformat())
        r["b3_12m"] = v12
        r["var_12m"] = a["total"] / v12 - 1 if v12 else None
        r["ipca_12m"] = ipca_12[0]
        r["real_12m"] = _real(r["var_12m"], ipca_12[0])
        # Fio B SCEE vigente
        r.update({"fiob_inicio": None, "fiob_reh": None, "fiob": None, "escalonamento": None,
                  "fiob_efetivo": None, "fiob_suspeito": False})
        gf = fiob_id.get(i)
        if gf is not None:
            kf = bisect_right(gf["inicio"].tolist(), hoje_iso) - 1
            if kf >= 0:
                f = gf.iloc[kf]
                r.update({"fiob_inicio": f["inicio"], "fiob_reh": f["reh"], "fiob": f["fiob"],
                          "escalonamento": f["escalonamento"], "fiob_efetivo": f["fiob_efetivo"],
                          "fiob_suspeito": bool(suspeito(f["fiob"]))})
        ok_fiob = r["fiob"] is not None and pd.notna(r["fiob"]) and not r["fiob_suspeito"]
        r["fiob_tusd"] = r["fiob"] / a["tusd"] if ok_fiob and a["tusd"] else None
        tem_efet = ok_fiob and pd.notna(r["fiob_efetivo"])
        r["valor_comp"] = a["total"] - r["fiob_efetivo"] if tem_efet else None
        r["pct_comp"] = r["valor_comp"] / a["total"] if r["valor_comp"] is not None and a["total"] else None
        # proximo reajuste
        if k + 1 < len(g):
            n = g.iloc[k + 1]
            r.update({"prox_publicada": True, "prox_inicio": n["inicio"], "prox_reh": n["reh"],
                      "prox_total": n["total"], "prox_var": n["total"] / a["total"] - 1 if a["total"] else None})
        else:
            prev = (date.fromisoformat(a["fim"]) + timedelta(days=1)).isoformat() if a["fim"] else None
            r.update({"prox_publicada": False, "prox_inicio": prev, "prox_reh": None, "prox_total": None,
                      "prox_var": None})
        linhas.append(r)
    met = pd.DataFrame(linhas).set_index("distribuidora").sort_index()
    for c in met.columns:
        if c not in ("no_catalogo", "ativa", "fiob_suspeito", "prox_publicada") and met[c].dtype == object:
            met[c] = met[c].where(met[c].notna(), None)
    num = ["tusd", "te", "total", "anterior", "var_ult", "ipca_ult", "real_ult", "b3_12m", "var_12m", "ipca_12m",
           "real_12m", "fiob", "escalonamento", "fiob_efetivo", "fiob_tusd", "valor_comp", "pct_comp", "prox_total",
           "prox_var"] + [f"{p}_{a}" for a in ANOS_ACUMULADO for p in ("b3", "acum", "cagr", "ipca", "real")]
    for c in num:
        met[c] = pd.to_numeric(met[c], errors="coerce")
    at = met["ativa"]
    ok_f = at & met["fiob"].notna() & ~met["fiob_suspeito"]
    for col, mask, nome in [("total", at & met["total"].notna(), "rank_b3"), ("fiob", ok_f, "rank_fiob"),
                            ("valor_comp", at & met["valor_comp"].notna(), "rank_comp")]:
        met[nome] = met.loc[mask, col].rank(ascending=False, method="min")
        met["n_" + nome] = int(mask.sum())
    return met, ctx


def medianas(met: pd.DataFrame) -> dict:
    """Medianas das distribuidoras ativas: nacional, por regiao e por UF."""
    a = met[met["ativa"]]

    def resumo(g: pd.DataFrame) -> dict:
        f = g[g["fiob"].notna() & ~g["fiob_suspeito"]]
        return {"n": int(len(g)), "b3": g["total"].median(), "tusd": g["tusd"].median(), "te": g["te"].median(),
                "fiob": f["fiob"].median(), "fiob_efetivo": f["fiob_efetivo"].median(),
                "valor_comp": g["valor_comp"].median(), "pct_comp": g["pct_comp"].median(),
                **{f"acum_{x}": g[f"acum_{x}"].median() for x in ANOS_ACUMULADO},
                **{f"real_{x}": g[f"real_{x}"].median() for x in ANOS_ACUMULADO},
                "var_12m": g["var_12m"].median(), "dists": sorted(g.index)}
    return {"br": resumo(a),
            "regiao": {r: resumo(g) for r, g in a.groupby("regiao") if r},
            "uf": {u: resumo(g) for u, g in a.groupby("uf") if u}}


def indice_nacional(tl_t: pd.DataFrame, ipca: dict | None, hoje: date, base: int = ANOS_ACUMULADO[0]) -> dict:
    """Mediana nacional do indice da B3 total (valor em 01/01 de cada ano / valor em 01/01/base) x IPCA."""
    mensal = (ipca or {}).get("mensal") or {}
    datas = [f"{a}-01-01" for a in range(2010, hoje.year + 1)] + [hoje.isoformat()]
    rot = [str(a) for a in range(2010, hoje.year + 1)] + ["hoje"]
    series = []
    for _, g in tl_t.groupby("id"):
        g = g.sort_values("inicio")
        ini, fim, tot = g["inicio"].tolist(), g["fim"].tolist(), g["total"].tolist()
        v0 = _valor_em(ini, fim, tot, f"{base}-01-01")
        if not v0:
            continue
        vals = [_valor_em(ini, fim, tot, d) for d in datas[:-1]] + [tot[max(bisect_right(ini, datas[-1]) - 1, 0)]]
        series.append([v / v0 * 100 if v else None for v in vals])
    med = pd.DataFrame(series).median().tolist() if series else [None] * len(datas)
    ip = []
    mes_fim = mes_mais(hoje.strftime("%Y-%m"), -1)
    for d in datas:
        m = d[:7] if d != datas[-1] else mes_mais(mes_fim, 1)
        if m >= f"{base}-01":
            v = ipca_acumulado(mensal, f"{base}-01", mes_mais(m, -1))[0] if m > f"{base}-01" else 0.0
            ip.append(None if v is None else (1 + v) * 100)
        else:
            v = ipca_acumulado(mensal, m, f"{base - 1}-12")[0]
            ip.append(None if v is None else 100 / (1 + v))
    return {"base": base, "rotulos": rot, "b3": med, "ipca": ip, "n": len(series)}


def montar_visoes(tarifas: pd.DataFrame, fiob: pd.DataFrame, cat: pd.DataFrame | None = None,
                  ipca: dict | None = None, hoje: date | None = None, tarifas_a: pd.DataFrame | None = None,
                  fiob_a: pd.DataFrame | None = None, subgrupos_a: list[str] | None = None) -> dict:
    cat = cat if cat is not None else pd.DataFrame(columns=COLS_DISTRIBUIDORAS[1:])
    hoje = hoje or datetime.now(FUSO_BR).date()
    t = canonico(tarifas)
    t["total"] = t["tusd"] + t["te"]
    tl_t = linha_do_tempo(t, "total", cat)

    f_scee = linha_do_tempo(canonico(fiob, "SCEE"), "fiob", cat)
    f_scee["escalonamento"] = f_scee["ano"].map(ESCALONAMENTO_FIOB)
    f_scee["fiob_efetivo"] = f_scee["fiob"] * f_scee["escalonamento"]

    m_scee = matriz_anual(f_scee, "fiob")
    esc = pd.Series({a: ESCALONAMENTO_FIOB.get(a) for a in m_scee.columns}, dtype=float)
    met, ctx = metricas(tl_t, f_scee, cat, ipca, hoje)
    return {
        "tl_tarifa": tl_t, "tl_fiob_scee": f_scee,
        "m_tarifa": matriz_anual(tl_t, "total"),
        "m_fiob_scee": m_scee, "m_fiob_efetivo": m_scee.mul(esc, axis=1),
        "rank_fiob": ranking_fiob(f_scee),
        "fiob_suspeitos": f_scee[suspeito(f_scee["fiob"])].sort_values(["inicio", "distribuidora"]),
        "metricas": met, "ctx": ctx, "medianas": medianas(met), "indice": indice_nacional(tl_t, ipca, hoje),
        "ipca": ipca, "hoje": hoje, "catalogo": cat,
        "removidas_fonte": resumo_removidas([tarifas, fiob, tarifas_a, fiob_a]),
        "ga": visoes_grupo_a(tarifas_a, fiob_a, tl_t, met, cat, hoje, subgrupos_a or []),
    }


# ------------------------------------------------------------------ Grupo A (A4, A3...)

VAL_A = list(METRICAS_A) + ["tusd_efp", "te_efp", "tusd_ep", "te_ep"]
FB_A = ["fb_" + c for c in METRICAS_A]


def _id(df: pd.DataFrame) -> pd.Series:
    return df["cnpj"].where(df["cnpj"] != "", df["distribuidora"])


def _canon_a(df: pd.DataFrame) -> pd.DataFrame:
    """Linha canonica do Grupo A: classe, subclasse e acessante 'Nao se aplica' (o recorte ja vem assim)."""
    return df[(df["classe"] == NA) & (df["subclasse"] == NA) & (df["acessante"] == NA)]


def _codigos_a(df: pd.DataFrame) -> pd.DataFrame:
    """Marca cada linha com o codigo do componente (efp, ep, d, dfp); unidade 'R$/MWh' vira 'MWh'."""
    d = df.copy()
    d["id"] = _id(d)
    d["unid"] = d["unidade"].str.replace("R$/", "", regex=False).str.strip()
    d["cod"] = [COMP_A.get(m, {}).get((p, u)) for m, p, u in zip(d["modalidade"], d["posto"], d["unid"])]
    d = d[d["cod"].notna()]
    g = ["id", "subgrupo", "modalidade", "inicio"]
    # snapshot cumulativo: linha ativa prevalece sobre a removida da fonte com o mesmo inicio (retificacao)
    d = sem_substituidas(d, g)
    # uma resolucao por vigencia (mesmo criterio da B3: em retificacoes com o mesmo inicio, a ultima em ordem)
    return d[d["reh"] == d.groupby(g)["reh"].transform("max")]


def _nomes_a(d: pd.DataFrame, nomes_b3: dict, cat: pd.DataFrame) -> pd.DataFrame:
    """Rotulo de exibicao igual ao da B3 (mesmo CNPJ); sem B3, nome comercial do catalogo ou sigla."""
    sig = d.sort_values("inicio").groupby("id")["sigla"].last()
    nomes = {}
    for i, s in sig.items():
        if i in nomes_b3:
            nomes[i] = nomes_b3[i]
        elif len(cat) and i in cat.index and cat.at[i, "nome_comercial"]:
            nomes[i] = rotulo_dist(cat.at[i, "nome_comercial"], s)
        else:
            nomes[i] = s
    d["distribuidora"] = d["id"].map(nomes)
    return d


def linha_do_tempo_a(tar: pd.DataFrame | None, nomes_b3: dict, cat: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por distribuidora, subgrupo, modalidade e inicio de vigencia (formato largo):
    efp/ep = TUSD+TE da energia fora ponta/ponta (R$/MWh), d/dfp = demanda (R$/kW), var_* contra a
    vigencia anterior da mesma distribuidora, subgrupo e modalidade."""
    cols = (["id", "cnpj", "sigla", "distribuidora", "subgrupo", "modalidade", "inicio", "fim", "reh", "ano",
             "inicio_anterior"] + VAL_A + [p + c for c in METRICAS_A for p in ("ant_", "var_")])
    if tar is None or tar.empty:
        return pd.DataFrame(columns=cols)
    d = _codigos_a(_canon_a(tar[tar["detalhe"] == NA]))
    if d.empty:
        return pd.DataFrame(columns=cols)
    # demanda: so TUSD (a TE vem zerada); energia: TUSD + TE
    te = d["te"].where(d["unid"] != "kW", d["te"].fillna(0))
    d["v"] = d["tusd"] + te
    g = ["id", "subgrupo", "modalidade", "inicio"]
    base = (d.sort_values(g + ["cod"]).groupby(g, as_index=False)
            .agg(cnpj=("cnpj", "first"), sigla=("distribuidora", "last"), fim=("fim", "max"), reh=("reh", "first")))
    w = base.set_index(g).join(d.pivot_table(index=g, columns="cod", values="v", aggfunc="max"))
    e = d[d["cod"].isin(["efp", "ep"])]
    if len(e):
        w = w.join(e.pivot_table(index=g, columns="cod", values="tusd", aggfunc="max").add_prefix("tusd_"))
        w = w.join(e.pivot_table(index=g, columns="cod", values="te", aggfunc="max").add_prefix("te_"))
    w = w.reset_index()
    for c in VAL_A:
        if c not in w.columns:
            w[c] = float("nan")
    w = _nomes_a(w, nomes_b3, cat).sort_values(["id", "subgrupo", "modalidade", "inicio"])
    k = w.groupby(["id", "subgrupo", "modalidade"])
    w["inicio_anterior"] = k["inicio"].shift()
    for c in METRICAS_A:
        w["ant_" + c] = k[c].shift()
        w["var_" + c] = w[c] / w["ant_" + c].where(w["ant_" + c] != 0) - 1
    w["ano"] = w["inicio"].str[:4].astype(int)
    return w[cols].reset_index(drop=True)


def linha_do_tempo_fiob_a(fb: pd.DataFrame | None, nomes_b3: dict, cat: pd.DataFrame) -> pd.DataFrame:
    """Fio B do Grupo A por vigencia (formato largo): fb_efp/fb_ep em R$/MWh (energia; detalhe SCEE quando
    existir, que na base e igual ao normal), fb_d/fb_dfp em R$/kW (demanda, detalhe normal). Efetivo da
    energia = Fio B x escalonamento da Lei 14.300 do ano de inicio da vigencia."""
    cols = (["id", "cnpj", "sigla", "distribuidora", "subgrupo", "modalidade", "inicio", "fim", "reh", "ano",
             "escalonamento", "fbe_efp", "fbe_ep"] + FB_A + ["var_" + c for c in FB_A])
    if fb is None or fb.empty:
        return pd.DataFrame(columns=cols)
    d = _codigos_a(_canon_a(fb))
    d = d[(d["unid"] == "MWh") | (d["detalhe"] == NA)]
    if d.empty:
        return pd.DataFrame(columns=cols)
    d["pref"] = (d["detalhe"] == "SCEE").astype(int)
    g = ["id", "subgrupo", "modalidade", "inicio"]
    d = d.sort_values(g + ["cod", "pref"]).drop_duplicates(g + ["cod"], keep="last")
    base = (d.groupby(g, as_index=False)
            .agg(cnpj=("cnpj", "first"), sigla=("distribuidora", "last"), fim=("fim", "max"), reh=("reh", "first")))
    w = base.set_index(g).join(d.pivot_table(index=g, columns="cod", values="fiob", aggfunc="max").add_prefix("fb_"))
    w = w.reset_index()
    for c in FB_A:
        if c not in w.columns:
            w[c] = float("nan")
    w = _nomes_a(w, nomes_b3, cat).sort_values(["id", "subgrupo", "modalidade", "inicio"])
    w["ano"] = w["inicio"].str[:4].astype(int)
    w["escalonamento"] = w["ano"].map(ESCALONAMENTO_FIOB)
    w["fbe_efp"] = w["fb_efp"] * w["escalonamento"]
    w["fbe_ep"] = w["fb_ep"] * w["escalonamento"]
    k = w.groupby(["id", "subgrupo", "modalidade"])
    for c in FB_A:
        ant = k[c].shift()
        w["var_" + c] = w[c] / ant.where(ant != 0) - 1
    return w[cols].reset_index(drop=True)


def metricas_a(tl: pd.DataFrame, tlf: pd.DataFrame, ativas: dict, hoje: date) -> pd.DataFrame:
    """Vigencia atual de cada distribuidora, subgrupo e modalidade (ultima com inicio <= hoje).

    vu_*: variacao do ultimo reajuste (contra o patamar anterior; vigencias seguidas com os mesmos valores,
    como a divisao de 01/01 do Fio B, contam como um patamar so). Fio B vigente, valor do kWh compensado
    (energia menos Fio B efetivo da energia) e relacoes ponta / fora ponta.
    """
    if tl.empty:
        return pd.DataFrame()
    hoje_iso, lim_ativa = hoje.isoformat(), (hoje - timedelta(days=DIAS_ATIVA)).isoformat()
    fb_por = {k: g.sort_values("inicio").reset_index(drop=True)
              for k, g in tlf.groupby(["id", "subgrupo", "modalidade"])} if len(tlf) else {}
    linhas = []
    for (i, s, m), g in tl.groupby(["id", "subgrupo", "modalidade"]):
        g = g.sort_values("inicio").reset_index(drop=True)
        ini = g["inicio"].tolist()
        k = max(bisect_right(ini, hoje_iso) - 1, 0)
        a = g.iloc[k]
        tup = [tuple(None if pd.isna(x) else round(float(x), 6) for x in r)
               for r in g[list(METRICAS_A)].itertuples(index=False)]
        p = k
        while p > 0 and tup[p] == tup[p - 1]:
            p -= 1
        r = {"id": i, "cnpj": a["cnpj"], "distribuidora": a["distribuidora"], "subgrupo": s, "modalidade": m,
             "ativa": bool(ativas[i]) if i in ativas else bool((a["fim"] or "9999") >= lim_ativa or k + 1 < len(g)),
             "inicio": a["inicio"], "fim": a["fim"], "reh": a["reh"], "ultimo_reajuste": ini[p],
             "inicio_anterior": ini[p - 1] if p > 0 else None}
        for c in VAL_A:
            r[c] = a[c]
        for c in METRICAS_A:
            ant = g.iloc[p - 1][c] if p > 0 else None
            r["ant_" + c] = ant
            r["vu_" + c] = a[c] / ant - 1 if ant is not None and pd.notna(ant) and ant and pd.notna(a[c]) else None
        r["rel_e"] = a["ep"] / a["efp"] if pd.notna(a["efp"]) and a["efp"] else None
        r["rel_d"] = a["d"] / a["dfp"] if pd.notna(a["dfp"]) and a["dfp"] else None
        for c in ["fb_inicio", "fb_reh", "escalonamento", "fbe_efp", "fbe_ep"] + FB_A:
            r[c] = None
        gf = fb_por.get((i, s, m))
        if gf is not None:
            kf = bisect_right(gf["inicio"].tolist(), hoje_iso) - 1
            if kf >= 0:
                f = gf.iloc[kf]
                r.update({"fb_inicio": f["inicio"], "fb_reh": f["reh"], "escalonamento": f["escalonamento"],
                          "fbe_efp": f["fbe_efp"], "fbe_ep": f["fbe_ep"], **{c: f[c] for c in FB_A}})
        # Valor do kWh compensado: energia menos o Fio B efetivo da parcela de energia (so com Fio B na base)
        r["vc_efp"] = a["efp"] - r["fbe_efp"] if r["fbe_efp"] is not None and pd.notna(r["fbe_efp"]) else None
        r["vc_ep"] = a["ep"] - r["fbe_ep"] if r["fbe_ep"] is not None and pd.notna(r["fbe_ep"]) else None
        linhas.append(r)
    met = pd.DataFrame(linhas)
    num = (VAL_A + ["ant_" + c for c in METRICAS_A] + ["vu_" + c for c in METRICAS_A]
           + ["rel_e", "rel_d", "escalonamento", "fbe_efp", "fbe_ep", "vc_efp", "vc_ep"] + FB_A)
    for c in num:
        met[c] = pd.to_numeric(met[c], errors="coerce")
    return met.sort_values(["subgrupo", "modalidade", "distribuidora"]).reset_index(drop=True)


MED_A = list(METRICAS_A) + ["vu_" + c for c in METRICAS_A] + ["rel_e", "rel_d", "vc_efp", "vc_ep", "fbe_ep"] + FB_A


def visoes_grupo_a(tar: pd.DataFrame | None, fb: pd.DataFrame | None, tl_t: pd.DataFrame, met_b3: pd.DataFrame,
                   cat: pd.DataFrame, hoje: date, subgrupos: list[str]) -> dict:
    """Linhas do tempo, situacao atual, medianas (ativas) e matrizes anuais de cada subgrupo e modalidade."""
    nomes_b3 = dict(zip(tl_t["id"], tl_t["distribuidora"])) if len(tl_t) else {}
    ativas = {c: a for c, a in zip(met_b3["cnpj"], met_b3["ativa"]) if c} if len(met_b3) else {}
    tl = linha_do_tempo_a(tar, nomes_b3, cat)
    tlf = linha_do_tempo_fiob_a(fb, nomes_b3, cat)
    met = metricas_a(tl, tlf, ativas, hoje)
    ordem_s = {s: j for j, s in enumerate(subgrupos)}
    pares = sorted({(s, m) for s, m in zip(tl["subgrupo"], tl["modalidade"])},
                   key=lambda x: (ordem_s.get(x[0], 99), x[0], MODALIDADES_A.index(x[1])))
    med, mats = {}, {}
    for s, m in pares:
        x = met[(met["subgrupo"] == s) & (met["modalidade"] == m)]
        a = x[x["ativa"]]
        med[(s, m)] = {"n": int(len(a)), **{c: a[c].median() for c in MED_A},
                       "n_vc": int(a["vc_efp"].notna().sum())}
        t = tl[(tl["subgrupo"] == s) & (tl["modalidade"] == m)]
        mats[(s, m)] = {c: matriz_anual(t, c) for c in METRICAS_A if t[c].notna().any()}
    return {"tl": tl, "tlf": tlf, "met": met, "med": med, "mats": mats, "pares": pares,
            "subgrupos": list(subgrupos)}


# ------------------------------------------------------------------------- config

def ler_config(base: Path) -> dict:
    cfg = {"distribuidoras_portfolio": [], "limiar_alerta_pct": 0.0, "fiob_ano_inicial": 2026,
           "subgrupos_grupo_a": list(SUBGRUPOS_A_PADRAO), "grupo_a_ano_inicial": 2016}
    arq = base / "config.json"
    if arq.exists():
        cfg.update(json.loads(arq.read_text(encoding="utf-8")))
    cfg["fiob_ano_inicial"] = int(cfg["fiob_ano_inicial"])
    cfg["grupo_a_ano_inicial"] = int(cfg["grupo_a_ano_inicial"])
    # Subgrupos do Grupo A (ex.: ["A4", "A3", "A3a", "A2"]); lista vazia desliga o Grupo A
    cfg["subgrupos_grupo_a"] = [str(s).strip() for s in (cfg.get("subgrupos_grupo_a") or []) if str(s).strip()]
    return cfg


def corte_fiob(cfg: dict) -> str:
    """Data minima de inicio de vigencia do Fio B ('AAAA-01-01')."""
    return f"{cfg['fiob_ano_inicial']}-01-01"


def corte_grupo_a(cfg: dict) -> str:
    """Data minima de inicio de vigencia das tarifas do Grupo A ('AAAA-01-01')."""
    return f"{cfg['grupo_a_ano_inicial']}-01-01"


def escopo_grupo_a(cfg: dict) -> dict:
    """O que o snapshot do Grupo A cobre; fica em estado.json para detectar ampliacao do escopo."""
    return {"subgrupos": list(cfg["subgrupos_grupo_a"]), "inicio_min": corte_grupo_a(cfg),
            "fiob_inicio_min": corte_fiob(cfg)}


def no_escopo_a(df: pd.DataFrame, subgrupos: list[str], inicio_min: str) -> pd.DataFrame:
    return df[df["subgrupo"].isin(subgrupos) & (df["inicio"] >= inicio_min)].reset_index(drop=True)


def eh_portfolio(nome: str, cfg: dict) -> bool:
    return any(p.upper() in nome.upper() for p in cfg["distribuidoras_portfolio"])


# --------------------------------------------------------------------------- alerta

def br(v, casas=2) -> str:
    if pd.isna(v):
        return "-"
    return f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(v) -> str:
    return "-" if pd.isna(v) else f"{v * 100:+.1f}%".replace(".", ",")


def data_br(iso: str) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if len(iso) >= 10 else iso


def reh_curta(reh: str) -> str:
    m = re.search(r"(RESOLU\S* HOMOLOGAT\S*|DESPACHO)\s*N\S*\s*([\d.]+)", reh.upper())
    if not m:
        return reh[:40]
    tipo = "REH" if m.group(1).startswith("RESOLU") else "Despacho"
    return f"{tipo} {m.group(2)}"


def filtrar_alerta(tl: pd.DataFrame, chaves: set, cfg: dict) -> pd.DataFrame:
    d = tl[tl["chave"].isin(chaves)].copy()
    lim = float(cfg.get("limiar_alerta_pct") or 0) / 100
    if lim > 0:
        d = d[d["var_pct"].isna() | (d["var_pct"].abs() >= lim)]
    return d.sort_values(["inicio", "distribuidora"])


def alerta_grupo_a(visoes: dict, mudancas: dict, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Vigencias do Grupo A com mudanca: (tarifas, Fio B), uma linha por distribuidora, subgrupo,
    modalidade e vigencia. O limiar vale para a maior variacao entre energia FP, energia P e demanda."""
    ga = visoes.get("ga") or {}
    tl, tlf = ga.get("tl", pd.DataFrame()), ga.get("tlf", pd.DataFrame())

    def sel(df, serie):
        grupos = (mudancas.get(serie) or {}).get("grupos") or set()
        if df is None or df.empty or not grupos:
            return pd.DataFrame(columns=getattr(df, "columns", []))
        m = [(i, s, mo, ini) in grupos for i, s, mo, ini in zip(df["id"], df["subgrupo"], df["modalidade"], df["inicio"])]
        return df[m].copy()
    at, af = sel(tl, "tarifa_a"), sel(tlf, "fiob_a")
    lim = float(cfg.get("limiar_alerta_pct") or 0) / 100
    if lim > 0 and len(at):
        v = at[["var_" + c for c in METRICAS_A]].abs()
        at = at[v.isna().all(axis=1) | (v.max(axis=1) >= lim)]
    ordem = ["inicio", "distribuidora", "subgrupo", "modalidade"]
    return at.sort_values(ordem), af.sort_values(ordem)


def montar_alerta(visoes: dict, mudancas: dict, cfg: dict, agora: datetime) -> str | None:
    chaves_t = set(mudancas["tarifa"]["chaves"])
    chaves_f = set(mudancas["fiob"]["chaves"])
    at = filtrar_alerta(visoes["tl_tarifa"], chaves_t, cfg)
    af = filtrar_alerta(visoes["tl_fiob_scee"], chaves_f, cfg)
    aa, afa = alerta_grupo_a(visoes, mudancas, cfg)
    if at.empty and af.empty and aa.empty and afa.empty:
        return None

    dists = sorted(set(at["distribuidora"]) | set(af["distribuidora"]) | set(aa["distribuidora"])
                   | set(afa["distribuidora"]))
    linhas = [
        f"# Tarifas ANEEL: mudanca em {len(dists)} distribuidora(s) - {agora:%d/%m/%Y}",
        "",
        f"Detectado em {agora:%d/%m/%Y %H:%M} (horario de Brasilia). "
        f"Distribuidoras: {', '.join(dists)}.",
        "",
    ]
    estrela = lambda n: f"★ {n}" if eh_portfolio(n, cfg) else n  # noqa: E731
    antes_t, antes_f = mudancas["tarifa"].get("antes", {}), mudancas["fiob"].get("antes", {})

    def situacao(r, antes: dict) -> str:
        # "revisada": mesma resolucao/vigencia ja existia no snapshot com outro valor
        return f"revisada (antes {br(antes[r['chave']])})" if r["chave"] in antes else "nova"

    def anterior(r) -> str:
        if pd.isna(r["anterior"]):
            return "-"
        return f"{br(r['anterior'])} ({data_br(r['inicio_anterior'])})"
    if not at.empty:
        linhas += ["## B3 Convencional, Tarifa de Aplicacao (R$/MWh)", "",
                   "| Distribuidora | Resolucao | Vigencia | Situacao | TUSD | TE | Total | Vigencia anterior | Var. |",
                   "|---|---|---|---|---:|---:|---:|---:|---:|"]
        for _, r in at.iterrows():
            linhas.append(f"| {estrela(r['distribuidora'])} | {reh_curta(r['reh'])} | "
                          f"{data_br(r['inicio'])} a {data_br(r['fim'])} | {situacao(r, antes_t)} | "
                          f"{br(r['tusd'])} | {br(r['te'])} | "
                          f"**{br(r['total'])}** | {anterior(r)} | **{pct(r['var_pct'])}** |")
        linhas.append("")
    if not af.empty:
        linhas += ["## TUSD Fio B, detalhe SCEE (R$/MWh)", "",
                   "| Distribuidora | Resolucao | Vigencia | Situacao | Fio B | Vigencia anterior | Var. "
                   "| % Lei 14.300 | Fio B efetivo |",
                   "|---|---|---|---|---:|---:|---:|---:|---:|"]
        for _, r in af.iterrows():
            esc = "-" if pd.isna(r["escalonamento"]) else f"{r['escalonamento'] * 100:.0f}%"
            linhas.append(f"| {estrela(r['distribuidora'])} | {reh_curta(r['reh'])} | "
                          f"{data_br(r['inicio'])} a {data_br(r['fim'])} | {situacao(r, antes_f)} | "
                          f"**{br(r['fiob'])}** | {anterior(r)} | **{pct(r['var_pct'])}** | {esc} | "
                          f"{br(r['fiob_efetivo'])} |")
        linhas.append("")
    if not aa.empty or not afa.empty:
        subs = "/".join(cfg.get("subgrupos_grupo_a") or []) or "Grupo A"
        linhas += [f"## Grupo A ({subs}), Tarifa de Aplicacao", ""]
    if not aa.empty:
        antes_a = mudancas["tarifa_a"].get("antes_grupos", set())

        def vv(r, c, casas=2):
            return "-" if pd.isna(r[c]) else f"{br(r[c], casas)} ({pct(r['var_' + c])})"
        linhas += ["Energia = TUSD+TE (R$/MWh); demanda = TUSD (R$/kW). Variacao contra a vigencia anterior.", "",
                   "| Distribuidora | Subgrupo | Modalidade | Resolucao | Vigencia | Situacao | Energia FP | Energia P "
                   "| Demanda |",
                   "|---|---|---|---|---|---|---:|---:|---:|"]
        for _, r in aa.iterrows():
            dem = vv(r, "d") if r["modalidade"] != "Azul" else f"P {vv(r, 'd')} / FP {vv(r, 'dfp')}"
            sit = "revisada" if (r["id"], r["subgrupo"], r["modalidade"], r["inicio"]) in antes_a else "nova"
            linhas.append(f"| {estrela(r['distribuidora'])} | {r['subgrupo']} | {r['modalidade']} | "
                          f"{reh_curta(r['reh'])} | {data_br(r['inicio'])} a {data_br(r['fim'])} | {sit} | "
                          f"**{vv(r, 'efp')}** | {vv(r, 'ep')} | {dem} |")
        linhas.append("")
    if not afa.empty:
        itens = sorted({f"{r.distribuidora} {r.subgrupo} {r.modalidade} ({data_br(r.inicio)})"
                        for r in afa.itertuples()})
        linhas += [f"TUSD Fio B do Grupo A: {len(afa)} vigencia(s) nova(s) ou alterada(s): " + "; ".join(itens)
                   + ". Valores por posto e unidade na aba Grupo A do painel.", ""]
    outras = sum(m["outras"] for m in mudancas.values())
    if outras:
        linhas += [f"Alem disso, {outras} linha(s) de classes, postos ou detalhes especificos "
                   "mudaram (ver aba Log Mudancas do painel).", ""]
    # Link do painel no GitHub Pages: env PAINEL_URL (workflow) tem prioridade; senao config.json "painel_url".
    url = os.environ.get("PAINEL_URL", "").strip() or str(cfg.get("painel_url") or "").strip()
    linhas += [f"Painel atualizado: {url} (Excel: {url.rstrip('/')}/{PAINEL}.xlsx)." if url
               else f"Paineis atualizados: `{PAINEL}.html` e `{PAINEL}.xlsx`.",
               "", "Fonte: dadosabertos.aneel.gov.br (tarifas-distribuidoras-energia-eletrica "
               "e componentes-tarifarias)."]
    return "\n".join(linhas) + "\n"


TIPO_REMOVIDA = "removida_na_fonte"   # sumiu da base da ANEEL; continua no snapshot e no painel
TIPO_VOLTOU = "voltou_na_fonte"       # estava marcada como removida e reapareceu com o mesmo valor


def registrar_log(caminho: Path, serie: str, novos, alterados, removidos, valores, agora, voltaram=None) -> int:
    def total(df, sufixo=""):
        cols = [c + sufixo for c in valores]
        return df[cols].sum(axis=1, min_count=1) if len(df) else pd.Series(dtype=float)

    voltaram = voltaram if voltaram is not None else pd.DataFrame()
    partes = []
    for tipo, df, ant, novo in [
        ("nova", novos, None, total(novos)),
        ("alterada", alterados, total(alterados, "_anterior"), total(alterados)),
        (TIPO_REMOVIDA, removidos, total(removidos), None),
        (TIPO_VOLTOU, voltaram, None, total(voltaram)),
    ]:
        if df.empty:
            continue
        p = df[["distribuidora", "cnpj", "reh", "inicio", "fim", "classe", "subclasse",
                "detalhe", "posto", "unidade"]].copy()
        p.insert(0, "tipo", tipo)
        if SERIES[serie].get("grupo_a"):
            p.insert(0, "serie", SERIES[serie]["rotulo"] + " " + df["subgrupo"].values + " " + df["modalidade"].values)
        else:
            p.insert(0, "serie", SERIES[serie]["rotulo"])
        p.insert(0, "detectado_em", agora.strftime("%Y-%m-%d %H:%M"))
        p["valor_anterior"] = ant.values if ant is not None else float("nan")
        p["valor_novo"] = novo.values if novo is not None else float("nan")
        partes.append(p)
    if not partes:
        return 0
    log = pd.concat(partes, ignore_index=True)
    log.to_csv(caminho, mode="a", header=not caminho.exists(), index=False,
               encoding="utf-8", float_format="%.6f")
    return len(log)


# --------------------------------------------------------------------------- painel

def textos_metodologia(visoes: dict, cfg: dict) -> list[str]:
    """Metodologia comum ao Leia-me do Excel e ao rodape do painel HTML."""
    a0 = cfg["fiob_ano_inicial"]
    return [
        "Fonte: arquivos Parquet de dados abertos da ANEEL (mesma base do painel Luz na Tarifa): "
        "tarifas-distribuidoras-energia-eletrica (B3) e componentes-tarifarias (Fio B, um arquivo por ano). "
        "Atualizacao diaria (coleta agendada no PC); cada arquivo so e baixado de novo quando a ANEEL o atualiza.",
        "Recorte: subgrupo B3, modalidade Convencional, base Tarifa de Aplicacao, valores em R$/MWh.",
        "Linha considerada: classe, subclasse, posto e acessante = 'Nao se aplica'.",
        "Historico cumulativo: o painel guarda tudo o que ja foi coletado. Quando a ANEEL tira linhas da base, elas "
        "ficam no snapshot com a data em que sumiram (coluna removida_fonte_em) e continuam no historico, nas "
        "matrizes, nas linhas do tempo e nos graficos; vao para o log como removida_na_fonte, sem alerta. Se voltarem "
        "a base, a marca e limpa. Retificacao: com uma vigencia ativa e uma removida para a mesma distribuidora e o "
        "mesmo inicio (Grupo A: tambem subgrupo e modalidade), vale a ativa, tambem na vigencia atual.",
        "Guarda contra falha da fonte: a coleta nao grava nada se o recorte novo tiver menos de 80% das linhas ativas "
        f"do snapshot ou se mais de 20% das linhas ativas sumirem de uma vez (remocoes com menos de "
        f"{REMOCAO_MIN_BLOQUEIO} linhas nunca bloqueiam).",
        "",
        "B3 total (TUSD+TE): historico completo, desde 2010.",
        f"Fio B SCEE: TUSD_FioB com detalhe SCEE (energia compensada, Lei 14.300), so vigencias iniciadas "
        f"a partir de 01/01/{a0} (config.json fiob_ano_inicial). Os arquivos de "
        f"{a0 - ANOS_ARQUIVO_ANTES} a {a0 - 1} tambem sao lidos, "
        f"porque trazem vigencias que comecam em 01/01/{a0} (virada do escalonamento da Lei 14.300) ou depois.",
        "Fio B efetivo: Fio B SCEE x escalonamento do ano de inicio da vigencia "
        "(2023 15%, 2024 30%, 2025 45%, 2026 60%, 2027 75%, 2028 90%; 2029+ regra ANEEL, nao modelado).",
        "",
        "Matrizes anuais: valor da ULTIMA vigencia iniciada em cada ano, por distribuidora "
        f"(B3 desde 2010, Fio B desde {a0}).",
        "Var % B3: contra o ano anterior (matriz e ranking B3) ou contra a vigencia anterior (abas Vigencias).",
        f"Ranking Fio B SCEE: vigencia mais recente contra a anterior da mesma distribuidora, dentro do periodo "
        f"desde {a0}. Sem vigencia anterior no periodo, a distribuidora fica fora do ranking.",
        f"Qualidade: Fio B SCEE <= {br(FIOB_SUSPEITO_MAX)} R$/MWh e tratado como suspeito (aviso no painel) "
        "e fica fora do ranking, da mediana e da media do Fio B.",
        "",
        "Nomes: distribuidoras.csv (versionado, editavel) liga o CNPJ ao nome comercial atual, grupo, UF e regiao. "
        "Quando o nome comercial difere da sigla da ANEEL, o painel mostra os dois, ex.: ENEL SP (ELETROPAULO). "
        "UF: estado com mais unidades consumidoras ativas no INDGER (ANEEL, dados comerciais por municipio IBGE); "
        "distribuidoras extintas ou incorporadas foram preenchidas manualmente.",
        f"Vigencia atual: a ultima com inicio ate a data do painel. Distribuidora ativa: vigencia atual ainda valida "
        f"ou terminada ha no maximo {DIAS_ATIVA} dias. Rankings, medianas e posicao relativa usam so as ativas.",
        "Valor do kWh compensado (GD II): B3 total (TUSD+TE) menos o Fio B efetivo (Fio B SCEE x % da Lei 14.300 "
        "do ano de inicio da vigencia do Fio B). E a economia bruta por MWh compensado, sem tributos (ICMS, PIS e "
        "COFINS), sem bandeira e sem custo de disponibilidade. Para GD III (art. 27, par. 1o, da Lei 14.300) o "
        "valor e menor: paga 100% do Fio B, 40% do Fio A, TFSEE e P&D/EE, o que nao esta modelado.",
        "Ultimo reajuste: tarifa atual contra o patamar anterior (vigencias seguidas com a mesma B3, como a divisao "
        "de 01/01 do escalonamento do Fio B, contam como um patamar so). IPCA do periodo: meses do inicio do "
        "patamar anterior ate o mes anterior ao reajuste.",
        "Acumulado: B3 atual contra a B3 vigente em 01/01/" + " e 01/01/".join(map(str, ANOS_ACUMULADO))
        + " e 12 meses antes da data do painel. CAGR = (1 + acumulado) ^ (1 / anos) - 1, anos ate a data do painel. "
        "IPCA: Banco Central, SGS serie 433, do mes inicial ate o ultimo mes divulgado (12 meses: os 12 ultimos "
        "meses divulgados). Aumento real = (1 + B3) / (1 + IPCA) - 1.",
        f"Calendario: distribuidoras ativas cuja vigencia atual termina nos proximos {DIAS_CALENDARIO} dias (ou ja "
        "terminou sem nova vigencia na base). 'Nova homologacao publicada' = ja existe vigencia com inicio futuro.",
        "Observacao TE SCEE: a base de tarifas tambem traz, para B3, uma linha com detalhe SCEE em que a TUSD e igual "
        "a normal e a TE e bem menor (ex.: ENEL SP 2026: TE 316,96 x TE SCEE 37,36). Pela base de componentes, a TE "
        "SCEE e a TE sem as parcelas de custo de energia (TE_ENERGIA, TE_ANGRA, TE_TRANSPORTE_ITAIPU, "
        "TE_TUST_ITAIPU): sobram encargos (ESS/EER, P&D, CDE), perdas da Rede Basica e subsidio. Ou seja, TUSD + "
        "TE SCEE = componentes nao associadas ao custo da energia (Lei 14.300, art. 17, regra apos a transicao). "
        "Essa linha nao entra nos calculos do painel (a GD II na transicao paga so o % do Fio B).",
        "",
        *textos_impostos(visoes),
        "",
        *textos_arrendamento(),
        "",
        *textos_grupo_a(cfg),
        "",
        "Distribuidoras do portfolio (config.json): " + (", ".join(cfg["distribuidoras_portfolio"]) or "todas"),
    ]


def textos_grupo_a(cfg: dict) -> list[str]:
    subs = ", ".join(cfg.get("subgrupos_grupo_a") or []) or "nenhum (desligado)"
    a0, ga0 = cfg["fiob_ano_inicial"], cfg["grupo_a_ano_inicial"]
    return [
        f"GRUPO A ({subs}; config.json subgrupos_grupo_a): mesma base de tarifas, modalidades Verde e Azul, base "
        f"Tarifa de Aplicacao, vigencias iniciadas desde 01/01/{ga0} (config.json grupo_a_ano_inicial). Linha "
        "considerada: classe, subclasse e acessante = 'Nao se aplica' e detalhe normal (o recorte tambem guarda o "
        "detalhe SCEE; APE, cooperativas e '<500GWh' ficam de fora). A modalidade Verde so existe para A3a, A4 e AS: "
        "o A3 tem so a Azul.",
        "Grupo A, energia: TUSD + TE em R$/MWh, na ponta (P) e fora ponta (FP). Demanda: TUSD em R$/kW (a TE vem "
        "zerada). Verde: demanda unica (posto 'Nao se aplica'); Azul: demanda na ponta e fora ponta. Na Verde, a "
        "TUSD da energia na ponta embute a demanda da ponta, por isso fica muito acima da Azul.",
        "Grupo A, variacoes: nas vigencias e no alerta, contra a vigencia anterior da mesma distribuidora, "
        "subgrupo e modalidade; nos KPIs e rankings, ultimo reajuste (patamar anterior com valores diferentes, "
        "como na B3). Matrizes: ultima vigencia iniciada em cada ano.",
        f"Grupo A, Fio B: TUSD_FioB por posto e unidade, vigencias desde 01/01/{a0} (mesmo corte do Fio B da B3). "
        "Na base, o Fio B da energia fora ponta e zero (Verde e Azul) e o da energia na ponta e zero na Azul; na "
        "Verde (que nao tem demanda na ponta), o Fio B da energia na ponta carrega o custo de rede da ponta em "
        "R$/MWh. O Fio B da demanda vem em R$/kW. O detalhe SCEE so existe para a energia e tem o mesmo valor "
        "do normal; o painel usa o SCEE na energia e o normal na demanda.",
        "Grupo A, valor do kWh compensado: energia (TUSD+TE) menos o Fio B efetivo da parcela de energia (Fio B "
        "R$/MWh SCEE x % da Lei 14.300 do ano de inicio da vigencia). Como o Fio B da energia fora ponta e zero, "
        "no A4 Verde fora ponta o valor compensado e a propria energia. A demanda (R$/kW) nao e compensada pelo "
        "SCEE: e paga integralmente pela unidade, com ou sem geracao. Sem tributos e sem bandeira.",
        "Simulador Verde x Azul (painel): custo mensal estimado = demanda x tarifa de demanda (Azul: a mesma "
        "demanda contratada na ponta e fora ponta) + consumo fora ponta x energia FP + consumo na ponta x energia "
        "P. Estimativa sem impostos, sem bandeiras, sem ultrapassagem e sem reativos.",
    ]


# ------------------------------------------------------------ impostos e calculadora GD

ARQ_IMPOSTOS = "impostos_distribuidoras.csv"          # aliquotas medias das faturas, por distribuidora e subgrupo
ARQ_ICMS_GD = "icms_gd_distribuidoras.csv"            # flags de ICMS na energia compensada e repasse s
ARQ_REF_PLANILHA = "referencia_planilha_tarifas.csv"  # TUSD e TE da planilha Tarifas.xlsx (referencia fixa)
ARQ_IMPOSTOS_META = "impostos_meta.json"             # data de geracao e janela de meses das faturas
ARQS_IMPOSTOS = (ARQ_IMPOSTOS, ARQ_ICMS_GD, ARQ_REF_PLANILHA, ARQ_IMPOSTOS_META, "produtividade_uf.csv")
ORIGEM_FATURAS = "médias de faturas de UCs consumidoras da base Thopen, set-nov/2026"  # sem impostos_meta.json
ICMS_PADRAO, PIS_PADRAO, COFINS_PADRAO = 0.18, 0.01, 0.04  # PIS+COFINS padrao 5%
REPASSE_PADRAO = 0.5
FONTE_FLAG_PADRAO = "padrão, revisar"


def escalonamento_ano(ano: int) -> float:
    """% do Fio B pago na energia compensada (GD II/III, Lei 14.300) no ano. Antes de 2023: 0;
    2029 em diante: regra ANEEL nao modelada, aproximada por 100%."""
    if ano < min(ESCALONAMENTO_FIOB):
        return 0.0
    return ESCALONAMENTO_FIOB.get(ano, 1.0)


def calc_gd(tusd: float, te: float, icms: float, pc: float, flag_te: int = 1, flag_tusd: int = 1,
            s: float = REPASSE_PADRAO, fb_ef: float = 0.0, preco: float | None = None) -> dict:
    """Tarifa com tributos e valor da energia compensada, como na planilha Tarifas.xlsx (gross-up em cascata).

    D = TUSD, E = TE, i = ICMS, pc = PIS + COFINS; k_PC = 1/(1-pc); k_full = 1/((1-i)(1-pc)).
    U = D + E; V = U.k_full; W = (D - FB_ef).k_TUSD + E.k_TE (k = k_full se o ICMS da parcela entra no
    beneficio, senao k_PC); Y = W - (U - FB_ef); X = s.Y; Z = (U - FB_ef) + X. GD I: FB_ef = 0 (planilha 1:1).
    Com preco P (R$/MWh, a parte da tarifa vendida com desagio, P = (1 - d).U): desagio = 1 - P / U (sobre a tarifa
    sem tributos, mesma base do Arrendamento) e economia do cliente = W - P (credito da compensada menos o preco).
    """
    k_pc = 1 / (1 - pc)
    k_full = 1 / ((1 - icms) * (1 - pc))
    k_te = k_full if flag_te else k_pc
    k_tusd = k_full if flag_tusd else k_pc
    u = tusd + te
    w = (tusd - fb_ef) * k_tusd + te * k_te
    y = w - (u - fb_ef)
    x = s * y
    r = {"k_pc": k_pc, "k_full": k_full, "k_te": k_te, "k_tusd": k_tusd, "U": u, "V": u * k_full,
         "fb_ef": fb_ef, "W": w, "Y": y, "X": x, "Z": (u - fb_ef) + x, "fb_res": fb_ef * k_full,
         "desagio": None, "economia": None}
    if preco is not None and u:
        r["desagio"] = 1 - preco / u
        r["economia"] = w - preco
    return r


# ------------------------------------------------------------ arrendamento de usinas

ARQ_PRODUTIVIDADE = "produtividade_uf.csv"   # kWh/kWp/ano tipico de UFV fixa por UF (referencia aproximada)
PRODUTIVIDADE_PADRAO = 1450.0                # UF sem linha no CSV
ARR_DESAGIO, ARR_VB, ARR_DESCONTO = 0.30, 0.50, 0.0
ARR_FC_CGH, ARR_HORAS_MES = 0.50, 730.0
ARR_GRADE_D = (0.20, 0.25, 0.30, 0.35, 0.40)
ARR_GRADE_S = (0.30, 0.40, 0.50, 0.60, 0.70)


def calc_arrendamento(tusd: float, te: float, icms: float, pc: float, flag_te: int = 1, flag_tusd: int = 1,
                      s: float = ARR_VB, d: float = ARR_DESAGIO, c: float = ARR_DESCONTO, fb_ef: float = 0.0,
                      fb_com_impostos: bool = True) -> dict:
    """Arrendamento de usina GD, por MWh compensado (opcao A: desagio so sobre a tarifa, beneficio dividido pelo VB).

    W_I = D.k_TUSD + E.k_TE (forma da planilha, GD I, sem Fio B); U = D + E; Y = W_I - U (beneficio fiscal).
    Custo do Fio B (pago pelo dono) = FB_ef.k_full (com impostos, padrao) ou FB_ef (sem impostos).
    Dono = (1 - d).U + s.Y - custo_FioB; Thopen = d.U + (1 - s).Y; dono + Thopen + custo_FioB = W_I.
    Desconto ao cliente = c.W_I; margem liquida = Thopen - c.W_I.
    Desagio nominal = d (so sobre U). Desagio implicito = 1 - dono / U (sobre a tarifa sem tributos, liquido do VB
    e do Fio B): cai quando s sobe, sobe com o Fio B e fica negativo se o dono recebe mais que U.
    """
    g = calc_gd(tusd, te, icms, pc, flag_te, flag_tusd, s)
    u, w, y = g["U"], g["W"], g["Y"]
    fb_com, fb_sem = fb_ef * g["k_full"], fb_ef
    custo = fb_com if fb_com_impostos else fb_sem
    dono = (1 - d) * u + s * y - custo
    thopen = d * u + (1 - s) * y
    desc = c * w
    return {"k_pc": g["k_pc"], "k_full": g["k_full"], "k_te": g["k_te"], "k_tusd": g["k_tusd"], "U": u, "W_I": w,
            "Y": y, "fb_ef": fb_ef, "custo_fb_com": fb_com, "custo_fb_sem": fb_sem, "custo_fb": custo,
            "dono": dono, "thopen": thopen, "desconto": desc, "margem": thopen - desc,
            "desagio_impl": 1 - dono / u if u else None, "d": d, "s": s, "c": c}


# ------------------------------------------------------------ visao de negociacao (arrendamento)

NEG_ABERTURA_PP = 0.10      # abertura da faixa: desagio atual + 10 p.p.
NEG_MARGEM_MIN = 0.20       # margem minima padrao da Thopen, em % de W_I (liquida do desconto ao cliente)
NEG_PRAZO, NEG_TAXA = 10, 0.12
NEG_DEGRADACAO = {"ufv": 0.005, "cgh": 0.0, "direto": 0.0}


def resolver_arrendamento(r: dict, var: str = "d", dono: float | None = None, margem: float | None = None) -> dict:
    """Calculo reverso do arrendamento (opcao A), linear em d e em s, por MWh compensado.

    r = resultado de calc_arrendamento com a configuracao atual (U, W_I, Y, custo_fb, d, s, c).
    var = "d" resolve o desagio com o s atual; var = "s" resolve o VB com o d atual.
    Alvo: dono (R$/MWh pedido pelo dono) ou margem (margem liquida minima da Thopen em R$/MWh, ja descontado c.W_I).
      pedido:  d = 1 - (dono + custo_FioB - s.Y) / U        s = (dono + custo_FioB - (1 - d).U) / Y
      margem:  d = (margem + c.W_I - (1 - s).Y) / U          s = 1 - (margem + c.W_I - d.U) / Y
    Com margem, o pagamento maximo ao dono = W_I - custo_FioB - c.W_I - margem (nao depende de d nem de s).
    Retorna ok, motivo (sem solucao: U = 0, Y = 0 ou valor fora de [0, 1]), valor (d ou s resolvido, mesmo fora da
    faixa), d, s, dono, thopen, margem, desagio_impl e dono_max (so com margem).
    """
    if (dono is None) == (margem is None):
        raise ValueError("informe dono ou margem")
    u, w, y, cfb, c = r["U"], r["W_I"], r["Y"], r["custo_fb"], r["c"]
    d, s = r["d"], r["s"]
    out = {"var": var, "ok": False, "motivo": "", "valor": None, "dono_max": None}
    if margem is not None:
        out["dono_max"] = w - cfb - c * w - margem
    if var == "d":
        if not u:
            out["motivo"] = "U = 0: o desagio nao altera o resultado"
            return out
        v = 1 - (dono + cfb - s * y) / u if dono is not None else (margem + c * w - (1 - s) * y) / u
        d = v
    elif var == "s":
        if abs(y) < 1e-12:
            out["motivo"] = "Y = 0: sem beneficio fiscal, o VB nao altera o resultado"
            return out
        v = (dono + cfb - (1 - d) * u) / y if dono is not None else 1 - (margem + c * w - d * u) / y
        s = v
    else:
        raise ValueError(f"var invalida: {var}")
    out["valor"] = v
    dn = (1 - d) * u + s * y - cfb
    th = d * u + (1 - s) * y
    out.update({"d": d, "s": s, "dono": dn, "thopen": th, "margem": th - c * w,
                "desagio_impl": 1 - dn / u if u else None})
    if v < -1e-12 or v > 1 + 1e-12:
        out["motivo"] = f"{'desagio d' if var == 'd' else 'VB s'} fora de 0 a 100%"
    else:
        out["ok"] = True
    return out


def projecao_arrendamento(tusd: float, te: float, icms: float, pc: float, flag_te: int = 1, flag_tusd: int = 1,
                          s: float = ARR_VB, d: float = ARR_DESAGIO, c: float = ARR_DESCONTO, fiob: float = 0.0,
                          gd: int = 2, fb_com_impostos: bool = True, mwh_mes: float = 1.0, prazo: int = NEG_PRAZO,
                          ano_ini: int = 2026, reajuste: float = 0.0, degradacao: float = 0.0, taxa: float = NEG_TAXA,
                          escalonar: bool = True, pct_fb: float | None = None) -> dict:
    """Projecao do contrato de arrendamento ano a ano (ano t = 0 .. prazo - 1, ano civil ano_ini + t).

    D, E e o Fio B SCEE sao reajustados por (1 + reajuste)^t (aliquotas fixas, entao U, W_I, Y e o custo do Fio B
    sobem no mesmo indice). % do Fio B: GD I = 0; GD II/III com escalonar = Lei 14.300 do ano (60/75/90%, 2029 em
    diante 100% como aproximacao); sem escalonar = pct_fb fixo (padrao: o % do ano inicial). Geracao do ano =
    mwh_mes x (1 - degradacao)^t x 12. VPL com fluxos anuais no fim de cada ano: soma fluxo_t / (1 + taxa)^(t + 1).
    """
    anos, p_fixo = [], escalonamento_ano(ano_ini) if pct_fb is None else pct_fb
    for t in range(int(prazo)):
        ano, f = ano_ini + t, (1 + reajuste) ** t
        p = 0.0 if gd == 1 else (escalonamento_ano(ano) if escalonar else p_fixo)
        r = calc_arrendamento(tusd * f, te * f, icms, pc, flag_te, flag_tusd, s, d, c, fiob * f * p, fb_com_impostos)
        q = mwh_mes * (1 - degradacao) ** t * 12
        anos.append({"ano": ano, "t": t, "fator": f, "pct_fb": p, "U": r["U"], "W_I": r["W_I"], "Y": r["Y"],
                     "fb_scee": fiob * f, "custo_fb": r["custo_fb"], "dono_mwh": r["dono"], "mwh_ano": q,
                     "dono": r["dono"] * q, "thopen": r["thopen"] * q, "margem": r["margem"] * q,
                     "custo_fb_ano": r["custo_fb"] * q, "desagio_impl": r["desagio_impl"]})
    vp = lambda k: sum(a[k] / (1 + taxa) ** (a["t"] + 1) for a in anos)  # noqa: E731
    tot = lambda k: sum(a[k] for a in anos)  # noqa: E731
    return {"anos": anos, "total": {k: tot(k) for k in ("dono", "thopen", "margem", "mwh_ano", "custo_fb_ano")},
            "vpl": {k: vp(k) for k in ("dono", "thopen", "margem")}, "taxa": taxa}


def geracao_mwh_mes(tipo: str, potencia: float = 0.0, produtividade_ano: float = PRODUTIVIDADE_PADRAO,
                    perdas: float = 0.0, fc: float = ARR_FC_CGH, mwh_mes: float = 0.0,
                    pct_compensado: float = 1.0) -> float:
    """Energia compensada media (MWh/mes). UFV: kWp x kWh/kWp/ano / 12 x (1 - perdas); CGH/outra: kW x FC x 730 h;
    direto: MWh/mes informado. Tudo x % da geracao que vira credito compensado."""
    if tipo == "ufv":
        g = potencia * produtividade_ano / 12 * (1 - perdas) / 1000
    elif tipo == "cgh":
        g = potencia * fc * ARR_HORAS_MES / 1000
    else:
        g = mwh_mes
    return g * pct_compensado


def ler_produtividade(base: Path) -> dict[str, tuple[float, str]]:
    """UF -> (kWh/kWp/ano, fonte) de produtividade_uf.csv."""
    arq = _arq_base(base, ARQ_PRODUTIVIDADE)
    if arq is None:
        return {}
    # separador fixo ';' (a fonte tem virgulas, o que confunde o sniffer do ler_csv_pv)
    df = pd.read_csv(arq, dtype=str, keep_default_na=False, encoding="utf-8-sig", sep=";")
    df.columns = [c.strip().lower() for c in df.columns]
    out = {}
    for _, r in df.iterrows():
        uf, v = str(r["uf"]).strip().upper(), para_numero(r["kwh_kwp_ano"])
        if uf and pd.notna(v):
            out[uf] = (float(v), str(r.get("fonte", "")).strip())
    return out


def _arq_base(base: Path, nome: str) -> Path | None:
    for arq in (base / nome, Path(__file__).resolve().parent / nome):
        if arq.exists():
            return arq
    return None


def ler_csv_pv(base: Path, nome: str) -> pd.DataFrame | None:
    """CSV com ';' (ou ','), texto; numeros sao lidos com para_numero (aceita 0,18 e 0.18)."""
    arq = _arq_base(base, nome)
    if arq is None:
        return None
    df = pd.read_csv(arq, dtype=str, keep_default_na=False, encoding="utf-8-sig", sep=None, engine="python")
    df.columns = [c.strip().lower() for c in df.columns]
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip()
    if "cnpj" in df.columns:
        df["cnpj"] = df["cnpj"].str.replace(r"\D", "", regex=True).str.zfill(14)
    return df


def hash_impostos(base: Path) -> str:
    """Assinatura dos CSVs de impostos (regera o painel quando mudam)."""
    h = hashlib.sha1()
    for nome in ARQS_IMPOSTOS:
        arq = _arq_base(base, nome)
        h.update(arq.read_bytes() if arq else b"-")
    return h.hexdigest()


MESES_ABREV = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")


def meta_impostos(base: Path) -> dict:
    """Le impostos_meta.json (gerado junto com o CSV de impostos). Devolve origem (texto da fonte das aliquotas),
    selo ("impostos atualizados em DD/MM, faturas de MM/AA a MM/AA") e as datas; sem o arquivo, a origem fixa."""
    out = {"origem": ORIGEM_FATURAS, "selo": "", "atualizado": "", "ini": "", "fim": ""}
    arq = _arq_base(base, ARQ_IMPOSTOS_META)
    if arq is None:
        return out
    try:
        m = json.loads(arq.read_text(encoding="utf-8"))
        ger = datetime.fromisoformat(str(m["gerado_em"]))
        ini = datetime.strptime(str(m["mes_ref_inicio"]), "%Y-%m")
        fim = datetime.strptime(str(m["mes_ref_fim"]), "%Y-%m")
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"Aviso: {ARQ_IMPOSTOS_META} ilegivel ({e}); usando a origem fixa das aliquotas.")
        return out
    mm = lambda d: f"{d:%m/%y}"  # noqa: E731
    per = (f"{MESES_ABREV[ini.month - 1]}/{ini.year}" if ini.year != fim.year else MESES_ABREV[ini.month - 1]) + \
        f"-{MESES_ABREV[fim.month - 1]}/{fim.year}"
    out.update({
        "origem": f"médias de faturas de UCs consumidoras da base Thopen, {per} (atualizadas em {ger:%d/%m/%Y})",
        "selo": f"impostos atualizados em {ger:%d/%m}, faturas de {mm(ini)} a {mm(fim)}",
        "atualizado": f"{ger:%d/%m/%Y}", "ini": mm(ini), "fim": mm(fim),
    })
    return out


def _aliquotas_faturas(imp: pd.DataFrame | None) -> dict[str, dict]:
    """CNPJ -> aliquotas medias das faturas: subgrupo B3 se houver, senao TOTAL (linha com mais faturas)."""
    if imp is None or not len(imp):
        return {}
    d = imp.copy()
    for c in ("n_faturas", "icms_media", "pis_media", "cofins_media"):
        d[c] = d[c].map(para_numero)
    d = d[d["icms_media"].notna() & d["pis_media"].notna() & d["cofins_media"].notna()]
    out = {}
    for cnpj, g in d.groupby("cnpj"):
        for sub in ("B3", "TOTAL"):
            x = g[g["subgrupo_classe"].str.upper() == sub]
            if len(x):
                r = x.sort_values("n_faturas", ascending=False).iloc[0]
                nominal = "nominal" in r["observacao"].lower()
                n_fat = f"{int(r['n_faturas']):,}".replace(",", ".")
                fonte = f"faturas {sub} ({n_fat} faturas, {r['mes_ref_inicio']} a {r['mes_ref_fim']})"
                out[cnpj] = {"icms": r["icms_media"], "pis": r["pis_media"], "cofins": r["cofins_media"],
                             "fonte": fonte, "nominal": nominal, "nome_fatura": r["distribuidora"]}
                break
    return out


def tabela_impostos(visoes: dict, base: Path) -> pd.DataFrame:
    """Uma linha por distribuidora do painel (indice = rotulo): aliquotas (com a fonte), flags de ICMS na energia
    compensada, repasse s, valores da calculadora em GD I (planilha) e GD II (Fio B efetivo da vigencia atual) e a
    referencia da planilha Tarifas.xlsx."""
    met = visoes["metricas"]
    fat = _aliquotas_faturas(ler_csv_pv(base, ARQ_IMPOSTOS))
    gd = ler_csv_pv(base, ARQ_ICMS_GD)
    gd = gd.drop_duplicates("cnpj", keep="last").set_index("cnpj") if gd is not None else pd.DataFrame()
    ref = ler_csv_pv(base, ARQ_REF_PLANILHA)
    ref = ({r["sigla"]: (para_numero(r["tusd"]), para_numero(r["te"])) for _, r in ref.iterrows()}
           if ref is not None else {})
    linhas = []
    for d, m in met.iterrows():
        c = m["cnpj"]
        g = gd.loc[c] if c in gd.index else None
        num = lambda col: para_numero(g[col]) if g is not None and col in g.index else float("nan")  # noqa: E731
        da_planilha = g is not None and g["fonte"].startswith("planilha")
        f = fat.get(c)
        if f:
            icms, pis, cofins = f["icms"], f["pis"], f["cofins"]
            fonte_i = fonte_pc = f["fonte"]
            if f["nominal"]:
                fonte_pc += "; PIS/COFINS nominal na fatura"
        elif pd.notna(num("icms_planilha")) and pd.notna(num("pis_planilha")):
            icms, pis, cofins = num("icms_planilha"), num("pis_planilha"), num("cofins_planilha")
            fonte_i = fonte_pc = "planilha Tarifas.xlsx (sem dado de fatura)"
        else:
            icms, pis, cofins = ICMS_PADRAO, PIS_PADRAO, COFINS_PADRAO
            fonte_i, fonte_pc = "padrão 18% (sem fatura nem planilha)", "padrão 5% (sem fatura nem planilha)"
        pc = pis + cofins
        if g is not None:
            fte, ftu, s = int(num("flag_icms_te")), int(num("flag_icms_tusd")), num("repasse_s")
            fonte_f, obs = g["fonte"], g.get("observacao", "")
        else:
            fte, ftu, s, fonte_f, obs = 0, 0, REPASSE_PADRAO, FONTE_FLAG_PADRAO, "fora de icms_gd_distribuidoras.csv"
        s = REPASSE_PADRAO if pd.isna(s) else s
        g1 = calc_gd(m["tusd"], m["te"], icms, pc, fte, ftu, s)
        ok_fb = pd.notna(m["fiob_efetivo"]) and not m["fiob_suspeito"]
        g2 = calc_gd(m["tusd"], m["te"], icms, pc, fte, ftu, s, m["fiob_efetivo"]) if ok_fb else None
        sigla = g["distribuidora"] if da_planilha else ""
        rt, re_ = ref.get(sigla, (None, None)) if sigla else (None, None)
        ref_tot = rt + re_ if rt is not None and re_ is not None else None
        linhas.append({
            "distribuidora": d, "cnpj": c, "icms": icms, "pis": pis, "cofins": cofins, "pc": pc,
            "fonte_icms": fonte_i, "fonte_pc": fonte_pc, "flag_te": fte, "flag_tusd": ftu, "s": s,
            "fonte_flags": fonte_f, "obs": obs,
            "U": g1["U"], "V": g1["V"], "W1": g1["W"], "Y1": g1["Y"], "X1": g1["X"], "Z1": g1["Z"],
            "fb_ef": m["fiob_efetivo"] if ok_fb else None,
            "W2": g2["W"] if g2 else None, "Y2": g2["Y"] if g2 else None, "X2": g2["X"] if g2 else None,
            "Z2": g2["Z"] if g2 else None, "fb_res": g2["fb_res"] if g2 else None,
            "ref_sigla": sigla, "ref_tusd": rt, "ref_te": re_, "ref_total": ref_tot,
            "ref_dif": g1["U"] - ref_tot if ref_tot is not None else None,
        })
    t = pd.DataFrame(linhas).set_index("distribuidora")
    for c in ("fb_ef", "W2", "Y2", "X2", "Z2", "fb_res", "ref_tusd", "ref_te", "ref_total", "ref_dif"):
        t[c] = pd.to_numeric(t[c], errors="coerce")
    return t


def impostos_faturas_excel(base: Path) -> pd.DataFrame:
    imp = ler_csv_pv(base, ARQ_IMPOSTOS)
    if imp is None:
        return pd.DataFrame()
    num = ["n_faturas", "n_ucs", "icms_media", "icms_mediana", "icms_min", "icms_max", "pis_media",
           "cofins_media", "pis_cofins_ultimo_mes", "descartadas"]
    for c in num:
        if c in imp.columns:
            imp[c] = imp[c].map(para_numero)
    return imp


def textos_impostos(visoes: dict) -> list[str]:
    imp = visoes.get("imp")
    meta = visoes.get("imp_meta") or meta_impostos(Path("."))
    atual = (f"Impostos atualizados em {meta['atualizado']}, com faturas de {meta['ini']} a {meta['fim']} (janela de 3 "
             "meses de referencia; 6 se a distribuidora tem menos de 100 faturas). " if meta["atualizado"] else "")
    cont = ""
    if imp is not None and len(imp):
        n = imp["fonte_flags"].map(lambda f: "planilha" if f.startswith("planilha") else
                                   ("uf" if f.startswith("inferido") else "padrao")).value_counts()
        nf = imp["fonte_icms"].str.startswith("faturas").sum()
        cont = (f" Neste painel: aliquotas de faturas para {nf} de {len(imp)} distribuidoras; flags da planilha "
                f"{n.get('planilha', 0)}, inferidas pela UF {n.get('uf', 0)}, padrao {n.get('padrao', 0)}.")
    esc = ", ".join(f"{a} {int(v * 100)}%" for a, v in ESCALONAMENTO_FIOB.items())
    return [
        "IMPOSTOS E CALCULADORA GD (aba Calculadora, Tabela Thopen e colunas com tributos): mesma regra da planilha "
        "Tarifas.xlsx do usuario, com gross-up em cascata. D = TUSD, E = TE, i = ICMS, pc = PIS + COFINS; "
        "k_PC = 1/(1-pc); k_full = 1/((1-i)(1-pc)).",
        "U (tarifa sem tributos) = D + E. V (tarifa cheia, com tributos) = U x k_full. W (valor da energia compensada, "
        "com os tributos evitados) = (D - FB_ef) x k_TUSD + E x k_TE, com k_TE = k_full se o ICMS da TE entra no "
        "beneficio (flag 1) e k_PC se nao entra (flag 0); k_TUSD idem. Y (beneficio tributario) = W - (U - FB_ef). "
        "X (VB) = s x Y, s = repasse (50%; 70% na Cemig, como na planilha). Z (COMP) = (U - FB_ef) + X. "
        "Preco de venda P = (1 - d) x U (desagio d sobre a tarifa sem tributos, mesma base do Arrendamento); "
        "desagio implicito do preco = 1 - P / U. Economia do cliente = W - P (credito da energia compensada menos o "
        "preco). Pagamento ao dono (opcao A, igual ao Arrendamento, Fio B com impostos) = P + s x Y_I - FB_ef x k_full, "
        "com Y_I = W_I - U (W_I sem Fio B); desagio implicito do dono = 1 - dono / U. Fio B residual com tributos = "
        "FB_ef x k_full.",
        f"FB_ef: GD I = 0 (reproduz a planilha 1:1); GD II e GD III = Fio B SCEE x % do ano da Lei 14.300 ({esc}; "
        "2029 em diante aproximado por 100%). Na calculadora o % vem da data de referencia; nas demais abas, "
        "W GD II usa o Fio B efetivo da vigencia atual. GD III pela Lei 14.300 (art. 27, par. 1o) paga ainda 40% do "
        "Fio A, TFSEE e P&D/EE, o que nao esta modelado.",
        f"Aliquotas: {meta['origem']} ({ARQ_IMPOSTOS}): media do subgrupo B3 se houver, senao TOTAL. "
        f"{atual}"
        "Os impostos sao atualizados por um script privado da Thopen a partir das faturas; sem ele o painel usa o "
        "ultimo CSV publicado. Sem dado de "
        "fatura: ICMS e PIS/COFINS da planilha Tarifas.xlsx; sem os dois: ICMS 18% e PIS+COFINS 5%. A fonte de cada "
        "aliquota aparece na calculadora e no Excel (aba Impostos). PIS/COFINS variam mes a mes (credito apurado "
        "pela distribuidora); as Energisa MT, TO, MS e Minas Rio informam na fatura a aliquota nominal (1,65% e "
        "7,6%), nao a efetiva, entao o PIS/COFINS delas fica superestimado.",
        f"Flags de ICMS na energia compensada e repasse ({ARQ_ICMS_GD}): extraidas das formulas da coluna W da "
        "planilha, linha a linha (W = V: 1/1; so ICMS TE: 1/0; so TUSD: 0/1; sem ICMS: 0/0). Distribuidoras fora "
        "da planilha: regra mais comum entre as da mesma UF na planilha ('inferido pela UF'); UF sem nenhuma: 0/0 "
        "('padrao, revisar'). Incoerencias da planilha foram mantidas e anotadas (ex.: RGE 0/0 x CEEE 1/1 no RS; "
        "EDP ES 0/1; V34 da CEEE sem ICMS, erro da planilha, aqui com a formula padrao)." + cont,
        f"Tabela Thopen: replica a planilha com a tarifa ANEEL vigente; para as 48 distribuidoras da planilha mostra "
        f"tambem TUSD+TE da planilha ({ARQ_REF_PLANILHA}, referencia fixa) e a diferenca, para ver o que estava "
        "desatualizado.",
        "Simulacao de fatura (calculadora): estimativa com regras simplificadas (energia compensada limitada ao "
        "consumo menos o custo de disponibilidade; bandeira com tributos sobre a energia nao compensada; CIP fixa; "
        "sem demais itens da fatura).",
    ]


def textos_arrendamento() -> list[str]:
    return [
        "ARRENDAMENTO DE USINAS (aba Arrendamento e Excel, aba Arrendamento (referencia)): a Thopen arrenda a usina, "
        "recebe o valor da energia compensada (tarifa mais o beneficio fiscal dos tributos evitados) e paga o dono "
        "com desagio. Por MWh compensado: U = D + E; W_I = D x k_TUSD + E x k_TE (forma da planilha, GD I, sem Fio B); "
        "Y = W_I - U (beneficio fiscal); FB_ef = Fio B SCEE x % da Lei 14.300 do ano (0 em GD I).",
        "Opcao A: o desagio d incide so sobre a tarifa e o beneficio fiscal e dividido pelo VB s (parte do dono; "
        "padrao 50%, Cemig 70%). Pagamento ao dono = (1 - d) x U + s x Y - custo do Fio B. Parte da Thopen = "
        "d x U + (1 - s) x Y. Conferencia: dono + Thopen + custo do Fio B = W_I. O dono paga o Fio B: padrao com "
        "impostos (FB_ef x k_full, o custo que aparece na fatura), ou sem impostos (FB_ef), por seletor.",
        "Desconto ao cliente final = c x W_I (c em % do valor ao cliente, padrao 0%). Margem liquida da Thopen = "
        "parte da Thopen - desconto. Desagio nominal = d (parametro, so sobre U). Desagio implicito = 1 - "
        "pagamento ao dono / U (sobre a tarifa sem tributos, liquido do VB e do Fio B): diminui quando o VB s "
        "aumenta, aumenta com o Fio B e fica negativo quando o dono recebe mais que a tarifa U.",
        f"Geracao: UFV = kWp x produtividade (kWh/kWp/ano, padrao da UF em {ARQ_PRODUTIVIDADE}, referencia "
        "aproximada para sistema fixo derivada do Atlas Brasileiro de Energia Solar, INPE/LABREN; editavel) / 12 x "
        f"(1 - perdas); CGH ou outra = kW x fator de capacidade (padrao {int(ARR_FC_CGH * 100)}%) x "
        f"{int(ARR_HORAS_MES)} h; ou MWh/mes informado. Energia compensada = geracao x % que vira credito.",
        "Premissas: estimativa; sem bandeiras, sem CIP, sem custo de disponibilidade, sem inadimplencia, O&M, "
        "seguros ou impostos sobre a receita do arrendamento. GD III segue a mesma regra de FB_ef da calculadora; "
        "pela Lei 14.300 (art. 27, par. 1o) ela paga 100% do Fio B mais 40% do Fio A, TFSEE e P&D/EE, o que nao "
        "esta modelado (o % do Fio B e editavel). Tabela de referencia no Excel: usina de 1 MWh/mes compensado, "
        "d = 30% e s = 50% para todas, Fio B com impostos, GD II e GD III com o Fio B efetivo da vigencia atual.",
        "VISAO DE NEGOCIACAO (aba Arrendamento): proposta ao dono (R$/MWh, R$/mes, R$/ano; dono / W_I; dono / U; "
        "desagio nominal e implicito; Fio B pago pelo dono). Calculo reverso, linear em d e em s, com custo_FioB = "
        "FB_ef x k_full (ou FB_ef): pedido do dono em R$/MWh (R$/mes / MWh compensado no mes) -> d = 1 - (dono + "
        "custo_FioB - s x Y) / U com o s atual, ou s = (dono + custo_FioB - (1 - d) x U) / Y com o d atual. Margem "
        "minima da Thopen m (R$/MWh, R$/mes ou % de W_I, ja liquida do desconto c x W_I) -> d_min = (m + c x W_I - "
        "(1 - s) x Y) / U, ou s_max = 1 - (m + c x W_I - d x U) / Y; pagamento maximo ao dono (teto da negociacao) = "
        "W_I - custo_FioB - c x W_I - m. Sem solucao quando o valor resolvido sai de 0 a 100%, U = 0 (resolver d) "
        "ou Y = 0 (resolver s, o VB nao muda nada). Faixa de negociacao: Abertura (padrao d atual + 10 p.p.), Alvo "
        "(configuracao atual) e Limite (d_min ou s_max da margem minima), editaveis; coluna em vermelho quando a "
        "margem fica abaixo da minima.",
        f"Projecao do contrato (padrao {NEG_PRAZO} anos a partir do ano da data de referencia): no ano t (t = 0 no "
        "primeiro ano) D, E e o Fio B SCEE sao multiplicados por (1 + reajuste)^t (padrao: IPCA acumulado dos ultimos "
        "12 meses divulgados), com aliquotas, flags, d, s e c fixos; % do Fio B do ano pela Lei 14.300 (2026 60%, 2027 "
        "75%, 2028 90%; de 2029 em diante a regra sera definida pela ANEEL, aqui aproximada por 100%), ou o % fixo "
        "informado se o escalonamento for desligado; GD I = 0 em todos os anos. Geracao do ano = MWh/mes x (1 - "
        "degradacao)^t x 12 (padrao 0,5% a.a. para UFV e 0% para CGH). VPL = soma do fluxo do ano / (1 + taxa)^(t + 1), "
        f"fluxos no fim de cada ano, taxa padrao {int(NEG_TAXA * 100)}% a.a. Limitacoes: reajuste unico para TUSD, TE "
        "e Fio B (os reajustes reais da ANEEL diferem por componente e por distribuidora), tributos constantes, sem "
        "bandeiras, CIP, custo de disponibilidade, inadimplencia, O&M e impostos sobre a receita; GD III segue a "
        "mesma regra do Fio B, sem o Fio A, TFSEE e P&D/EE. Excel, aba Negociacao (referencia): 10 anos para as "
        "distribuidoras com aliquotas de faturas, 1 MWh/mes, d = 30%, s = 50%, GD II, reajuste IPCA 12 meses, sem "
        "degradacao, com Total e VPL por distribuidora. 'Copiar proposta' gera o texto para o proprietario (sem a "
        "margem da Thopen); 'Copiar analise interna' inclui margem, limite e VPL.",
    ]


def avisos_qualidade(visoes: dict, cfg: dict, estado: dict) -> list[str]:
    """Avisos de qualidade dos dados, comuns ao Leia-me do Excel e ao painel HTML."""
    mt, ms = visoes["m_tarifa"], visoes["m_fiob_scee"]
    ano = int(max(mt.columns))
    avisos = list(estado.get("avisos_fonte") or [])
    sus = visoes["fiob_suspeitos"]
    if len(sus):
        itens = [f"{r.distribuidora} {br(r.fiob)} ({reh_curta(r.reh)}, vigencia {data_br(r.inicio)})"
                 for r in sus.itertuples()]
        avisos.append(f"{len(sus)} vigencia(s) de Fio B SCEE com valor <= {br(FIOB_SUSPEITO_MAX)} R$/MWh, "
                      "provavel erro de carga na ANEEL; ficam fora do ranking, da mediana e da media: "
                      + "; ".join(itens) + ".")
    com_tarifa = set(mt[ano].dropna().index) if ano in mt.columns else set()
    com_fiob = set(ms[ano].dropna().index) if ano in ms.columns else set()
    sem_fiob = sorted(com_tarifa - com_fiob)
    if sem_fiob and ano >= cfg["fiob_ano_inicial"]:
        avisos.append(f"{len(sem_fiob)} distribuidora(s) com tarifa B3 {ano} e sem Fio B SCEE {ano}: "
                      + ", ".join(sem_fiob) + ".")
    met = visoes.get("metricas")
    if met is not None and len(met):
        fora = met[~met["no_catalogo"]]
        if len(fora):
            avisos.append(f"{len(fora)} distribuidora(s) sem linha em {ARQ_DISTRIBUIDORAS} (aparecem com o nome da "
                          "ANEEL, sem UF): " + ", ".join(f"{d} (CNPJ {c})" for d, c in fora["cnpj"].items()) + ".")
    ga = visoes.get("ga") or {}
    gm = ga.get("met")
    if cfg.get("subgrupos_grupo_a") and (gm is None or not len(gm)):
        avisos.append("Grupo A: sem dados no snapshot (data/tarifas_grupo_a.csv); a aba Grupo A fica vazia.")
    elif gm is not None and len(gm) and ano >= cfg["fiob_ano_inicial"]:
        sem = gm[gm["ativa"] & gm["fb_inicio"].isna()]
        if len(sem):
            itens = sorted({f"{r.distribuidora} ({r.subgrupo} {r.modalidade})" for r in sem.itertuples()})
            avisos.append(f"Grupo A: {len(itens)} combinacao(oes) ativa(s) sem TUSD Fio B desde "
                          f"{cfg['fiob_ano_inicial']} (sem valor do kWh compensado): " + ", ".join(itens) + ".")
    hoje = visoes.get("hoje") or datetime.now(FUSO_BR).date()
    lim = (hoje - timedelta(days=DIAS_AVISO_REMOCAO)).isoformat()
    for r in visoes.get("removidas_fonte") or []:
        if r["data"] < lim:
            continue
        anos = r["ano_min"] if r["ano_min"] == r["ano_max"] else f"{r['ano_min']} a {r['ano_max']}"
        avisos.append(f"A ANEEL removeu da base {br(r['vigencias'], 0)} vigências antigas em {data_br(r['data'])} "
                      f"(inícios de {anos}; {br(r['linhas'], 0)} linhas de B3, Fio B e Grupo A); mantidas no "
                      "histórico do painel. Onde a ANEEL publicou retificação com o mesmo início, vale a vigência "
                      f"ativa. Detalhe no log (tipo {TIPO_REMOVIDA}).")
    if not ((visoes.get("ipca") or {}).get("mensal")) and not any(a.startswith("IPCA") for a in avisos):
        avisos.append("IPCA indisponivel (sem cache em data/ipca_bcb.json): comparacoes com a inflacao ficam vazias.")
    return avisos


def ler_log(log_path: Path) -> pd.DataFrame:
    if log_path.exists():
        return pd.read_csv(log_path, encoding="utf-8", dtype={"cnpj": str})
    return pd.DataFrame(columns=["detectado_em", "serie", "tipo", "distribuidora"])


def rotulos_por_cnpj(visoes: dict) -> dict[str, str]:
    """CNPJ -> rotulo de exibicao (para o log, que guarda a sigla da ANEEL da epoca)."""
    m = {}
    ga = visoes.get("ga") or {}
    for tl in (ga.get("tlf"), ga.get("tl"), visoes["tl_tarifa"], visoes["tl_fiob_scee"]):
        if tl is not None and len(tl):
            m.update(dict(zip(tl["cnpj"], tl["distribuidora"])))
    return m


# Formatos do Excel pelo cabecalho da coluna
CABEC_PCT = {"Var %", "% Lei 14.300", "Fio B / TUSD", "% da tarifa", "Var ultimo reajuste", "IPCA no periodo do reajuste",
             "Real ultimo reajuste", "Var 12 meses", "IPCA 12 meses", "Real 12 meses", "Var nova vigencia",
             "Var proximo reajuste", "Dif. vs mediana nacional", "Dif. vs mediana UF",
             "Mediana var 12 meses"} | {f"{p} {a}" for a in ANOS_ACUMULADO
                                        for p in ("Acumulado desde", "CAGR desde", "IPCA desde", "Real desde",
                                                  "Mediana acumulado", "Mediana real")}
CABEC_RS = {"TUSD", "TE", "Total TUSD+TE", "Fio B", "Fio B SCEE", "Vigencia anterior", "Fio B efetivo", "valor_anterior",
            "valor_novo", "Valor kWh compensado", "Total anterior", "B3 12 meses antes", "Novo total",
            "Proximo total", "Mediana B3 total", "Mediana TUSD", "Mediana TE", "Mediana Fio B SCEE",
            "Mediana Fio B efetivo", "Mediana valor kWh compensado", "Mediana nacional B3",
            "Mediana UF B3"} | {f"B3 em 01/01/{a}" for a in ANOS_ACUMULADO}
# Grupo A
CABEC_PCT |= {"Var energia FP", "Var energia P", "Var demanda", "Var demanda FP", "Reajuste energia FP",
              "Reajuste energia P", "Reajuste demanda", "Reajuste demanda FP", "Var Fio B energia FP",
              "Var Fio B energia P", "Var Fio B demanda", "Var Fio B demanda FP"}
CABEC_RS |= {"TUSD energia FP", "TE energia FP", "Energia FP", "TUSD energia P", "TE energia P", "Energia P",
             "Demanda", "Demanda FP", "Fio B energia FP", "Fio B energia P", "Fio B demanda", "Fio B demanda FP",
             "Fio B efetivo energia FP", "Fio B efetivo energia P", "Valor kWh compensado FP",
             "Valor kWh compensado P", "Relacao P/FP energia", "Relacao P/FP demanda"}


# Calculadora e impostos
CABEC_PCT |= {"ICMS", "PIS", "COFINS", "PIS+COFINS", "Repasse s", "Diferenca % vs planilha",
              "icms_media", "icms_mediana", "icms_min", "icms_max", "pis_media", "cofins_media",
              "pis_cofins_ultimo_mes"}
CABEC_RS |= {"U tarifa sem tributos", "V tarifa cheia", "W GD I", "Y GD I", "X GD I", "Z GD I", "W GD II",
             "Y GD II", "X GD II", "Z GD II", "Fio B residual com tributos", "TUSD planilha", "TE planilha",
             "TUSD+TE na planilha", "Diferenca vs planilha"}
# Arrendamento (referencia)
CABEC_PCT |= {"Desagio d", "VB s"} | {f"Desagio implicito {g}" for g in ("GD I", "GD II", "GD III")}
CABEC_RS |= {f"{p} {g}" for p in ("Custo Fio B", "Dono", "Thopen") for g in ("GD I", "GD II", "GD III")}
# Negociacao (referencia)
CABEC_PCT |= {"% Fio B (Lei 14.300)", "Desagio implicito"}
CABEC_RS |= {"Y beneficio fiscal", "Custo Fio B (R$/MWh)", "Dono (R$/MWh)", "Geracao (MWh/ano)", "Dono (R$/ano)",
             "Thopen (R$/ano)", "Margem liquida (R$/ano)"}


def abas_impostos(visoes: dict) -> list[tuple]:
    """Abas do Excel: Calculadora (tabela) = Tabela Thopen (GD I como na planilha + GD II), Impostos (aliquotas e
    flags usadas por distribuidora) e Impostos faturas (agregados por distribuidora e subgrupo)."""
    imp, met = visoes.get("imp"), visoes["metricas"]
    if imp is None or not len(imp):
        return []
    m = met.join(imp.drop(columns=["cnpj"]), how="left")
    m = m.assign(Distribuidora=m.index).sort_values(["ativa", "Distribuidora"], ascending=[False, True])
    data = lambda s: s.map(lambda v: data_br(v) if isinstance(v, str) else v)  # noqa: E731
    flag = lambda s: s.map(lambda v: "Sim" if v == 1 else "Nao")  # noqa: E731
    calc = pd.DataFrame({
        "Distribuidora": m["Distribuidora"], "UF": m["uf"], "Ativa": m["ativa"].map(lambda v: "Sim" if v else "Nao"),
        "Inicio vigencia": data(m["inicio"]), "TUSD": m["tusd"], "TE": m["te"], "ICMS": m["icms"],
        "PIS+COFINS": m["pc"], "ICMS da TE na compensada": flag(m["flag_te"]),
        "ICMS da TUSD na compensada": flag(m["flag_tusd"]), "Repasse s": m["s"],
        "U tarifa sem tributos": m["U"], "V tarifa cheia": m["V"], "W GD I": m["W1"], "Y GD I": m["Y1"],
        "X GD I": m["X1"], "Z GD I": m["Z1"], "Fio B SCEE": m["fiob"].where(~m["fiob_suspeito"]),
        "% Lei 14.300": m["escalonamento"], "Fio B efetivo": m["fb_ef"], "W GD II": m["W2"], "Y GD II": m["Y2"],
        "X GD II": m["X2"], "Z GD II": m["Z2"], "Fio B residual com tributos": m["fb_res"],
        "Sigla na planilha": m["ref_sigla"], "TUSD+TE na planilha": m["ref_total"],
        "Diferenca vs planilha": m["ref_dif"], "Diferenca % vs planilha": m["ref_dif"] / m["ref_total"],
        "Fonte aliquotas": m["fonte_icms"], "Fonte flags": m["fonte_flags"], "Observacao": m["obs"],
        "CNPJ": m["cnpj"]})
    aliq = pd.DataFrame({
        "Distribuidora": m["Distribuidora"], "CNPJ": m["cnpj"], "UF": m["uf"], "ICMS": m["icms"],
        "Fonte ICMS": m["fonte_icms"], "PIS": m["pis"], "COFINS": m["cofins"], "PIS+COFINS": m["pc"],
        "Fonte PIS/COFINS": m["fonte_pc"], "ICMS da TE na compensada": flag(m["flag_te"]),
        "ICMS da TUSD na compensada": flag(m["flag_tusd"]), "Repasse s": m["s"], "Fonte flags": m["fonte_flags"],
        "Observacao": m["obs"]})
    abas = [("Calculadora (tabela)", calc, False, "tl"), ("Impostos", aliq, False, "tl")]
    fat = visoes.get("imp_faturas")
    if fat is not None and len(fat):
        abas.append(("Impostos faturas", fat, False, "tl"))
    abas.append(("Arrendamento (referência)",tabela_arrendamento(m, visoes.get("prod") or {}), False, "tl"))
    abas.append(("Negociação (referência)", tabela_negociacao(m, visoes), False, "tl"))
    return abas


def ipca_12m(visoes: dict) -> float | None:
    """IPCA acumulado dos ultimos 12 meses divulgados (fracao), padrao do reajuste anual na projecao."""
    v = ((visoes.get("ctx") or {}).get("periodos") or {}).get("12m", (None,))[0]
    return None if v is None or pd.isna(v) else float(v)


def tabela_negociacao(m: pd.DataFrame, visoes: dict) -> pd.DataFrame:
    """Projecao de 10 anos de uma usina-padrao de 1 MWh/mes compensado, d = 30%, s = 50%, c = 0, GD II, Fio B com
    impostos e escalonamento da Lei 14.300, reajuste = IPCA 12 meses, sem degradacao, VPL a 12% a.a., para as
    distribuidoras com aliquotas de faturas. Uma linha por ano, mais Total e VPL por distribuidora."""
    hoje = visoes.get("hoje") or date.today()
    reaj = ipca_12m(visoes) or 0.0
    linhas = []
    sel = m[m["fonte_icms"].fillna("").astype(str).str.startswith("faturas")] if "fonte_icms" in m.columns else m.iloc[0:0]
    for _, r in sel.iterrows():
        if pd.isna(r.get("tusd")) or pd.isna(r.get("te")) or pd.isna(r.get("icms")):
            continue
        fb = r.get("fiob")
        fb = 0.0 if pd.isna(fb) or r.get("fiob_suspeito") else float(fb)
        p = projecao_arrendamento(r["tusd"], r["te"], r["icms"], r["pc"], int(r["flag_te"]), int(r["flag_tusd"]),
                                  s=ARR_VB, d=ARR_DESAGIO, c=0.0, fiob=fb, gd=2, mwh_mes=1.0, prazo=NEG_PRAZO,
                                  ano_ini=hoje.year, reajuste=reaj, degradacao=0.0, taxa=NEG_TAXA)
        base = {"Distribuidora": r["Distribuidora"], "UF": str(r.get("uf") or "")}
        for a in p["anos"]:
            linhas.append({**base, "Ano": a["ano"], "% Fio B (Lei 14.300)": a["pct_fb"], "U tarifa sem tributos": a["U"],
                           "Y beneficio fiscal": a["Y"], "Custo Fio B (R$/MWh)": a["custo_fb"],
                           "Dono (R$/MWh)": a["dono_mwh"], "Geracao (MWh/ano)": a["mwh_ano"],
                           "Dono (R$/ano)": a["dono"], "Thopen (R$/ano)": a["thopen"],
                           "Margem liquida (R$/ano)": a["margem"], "Desagio implicito": a["desagio_impl"]})
        linhas.append({**base, "Ano": f"Total {NEG_PRAZO} anos", "Geracao (MWh/ano)": p["total"]["mwh_ano"],
                       "Dono (R$/ano)": p["total"]["dono"], "Thopen (R$/ano)": p["total"]["thopen"],
                       "Margem liquida (R$/ano)": p["total"]["margem"]})
        linhas.append({**base, "Ano": f"VPL {int(NEG_TAXA * 100)}% a.a.", "Dono (R$/ano)": p["vpl"]["dono"],
                       "Thopen (R$/ano)": p["vpl"]["thopen"], "Margem liquida (R$/ano)": p["vpl"]["margem"]})
    t = pd.DataFrame(linhas)
    if len(t):
        t["Premissas"] = ""
        t.loc[t.index[0], "Premissas"] = (f"1 MWh/mes compensado, d = {int(ARR_DESAGIO * 100)}%, s = {int(ARR_VB * 100)}%, "
                                          f"c = 0, GD II, Fio B com impostos e escalonamento da Lei 14.300 (2029+ 100%), "
                                          f"reajuste anual {br(reaj * 100)}% (IPCA 12 meses), sem degradacao, "
                                          f"VPL a {int(NEG_TAXA * 100)}% a.a. com fluxos no fim de cada ano")
    return t


def tabela_arrendamento(m: pd.DataFrame, prod: dict) -> pd.DataFrame:
    """Usina-padrao de 1 MWh/mes compensado, d = 30%, s = 50%, c = 0, Fio B com impostos; GD I, II e III
    (GD II/III com o Fio B efetivo da vigencia atual, como na Tabela Thopen). R$/mes = R$/MWh."""
    linhas = []
    for d, r in m.iterrows():
        if pd.isna(r.get("tusd")) or pd.isna(r.get("te")) or pd.isna(r.get("icms")):
            continue
        base = dict(tusd=r["tusd"], te=r["te"], icms=r["icms"], pc=r["pc"], flag_te=int(r["flag_te"]),
                    flag_tusd=int(r["flag_tusd"]), s=ARR_VB, d=ARR_DESAGIO, c=0.0)
        g1 = calc_arrendamento(**base)
        fb = r["fb_ef"]
        g2 = calc_arrendamento(**base, fb_ef=fb) if pd.notna(fb) else None
        uf = str(r.get("uf") or "")
        linha = {"Distribuidora": r["Distribuidora"], "UF": uf, "Ativa": "Sim" if r["ativa"] else "Nao",
                 "Inicio vigencia": data_br(r["inicio"]) if isinstance(r["inicio"], str) else r["inicio"],
                 "TUSD": r["tusd"], "TE": r["te"], "ICMS": r["icms"], "PIS+COFINS": r["pc"],
                 "ICMS da TE na compensada": "Sim" if r["flag_te"] == 1 else "Nao",
                 "ICMS da TUSD na compensada": "Sim" if r["flag_tusd"] == 1 else "Nao",
                 "U tarifa sem tributos": g1["U"], "W GD I": g1["W_I"], "Y GD I": g1["Y"],
                 "Desagio d": ARR_DESAGIO, "VB s": ARR_VB,
                 "Fio B efetivo": fb if pd.notna(fb) else None}
        for nome, g in (("GD I", g1), ("GD II", g2), ("GD III", g2)):
            linha[f"Custo Fio B {nome}"] = g["custo_fb"] if g else None
            linha[f"Dono {nome}"] = g["dono"] if g else None
            linha[f"Thopen {nome}"] = g["thopen"] if g else None
            linha[f"Desagio implicito {nome}"] = g["desagio_impl"] if g else None  # 1 - dono / U
        linha["Produtividade UFV UF (kWh/kWp/ano)"] = prod.get(uf, (None, ""))[0]
        linhas.append(linha)
    t = pd.DataFrame(linhas)
    if len(t):
        for c in t.columns:
            if c.startswith(("Custo Fio B", "Dono ", "Thopen ", "Desagio implicito")) or c == "Fio B efetivo":
                t[c] = pd.to_numeric(t[c], errors="coerce")
    return t


def abas_grupo_a(visoes: dict) -> list[tuple]:
    """Abas do Grupo A no Excel, consolidadas com colunas de subgrupo e modalidade:
    Grupo A Atual, Grupo A Anual (distribuidora x ano, uma linha por metrica), Grupo A Vigencias e Grupo A Fio B."""
    ga = visoes.get("ga") or {}
    met, tl, tlf = ga.get("met"), ga.get("tl"), ga.get("tlf")
    if met is None or not len(met):
        return []
    subs = ga["subgrupos"]
    ordem = lambda df: df.assign(  # noqa: E731
        _s=df["subgrupo"].map(lambda s: subs.index(s) if s in subs else 99),
        _m=df["modalidade"].map(lambda m: MODALIDADES_A.index(m) if m in MODALIDADES_A else 9))
    data = lambda s: s.map(lambda v: data_br(v) if isinstance(v, str) else v)  # noqa: E731
    m = ordem(met).sort_values(["_s", "_m", "ativa", "distribuidora"], ascending=[True, True, False, True])
    atual = pd.DataFrame({
        "Distribuidora": m["distribuidora"], "Subgrupo": m["subgrupo"], "Modalidade": m["modalidade"],
        "Ativa": m["ativa"].map(lambda v: "Sim" if v else "Nao"), "Inicio vigencia": data(m["inicio"]),
        "Fim vigencia": data(m["fim"]), "Resolucao": m["reh"],
        "TUSD energia FP": m["tusd_efp"], "TE energia FP": m["te_efp"], "Energia FP": m["efp"],
        "TUSD energia P": m["tusd_ep"], "TE energia P": m["te_ep"], "Energia P": m["ep"],
        "Demanda": m["d"], "Demanda FP": m["dfp"],
        "Relacao P/FP energia": m["rel_e"], "Relacao P/FP demanda": m["rel_d"],
        "Ultimo reajuste": data(m["ultimo_reajuste"]), "Reajuste energia FP": m["vu_efp"],
        "Reajuste energia P": m["vu_ep"], "Reajuste demanda": m["vu_d"], "Reajuste demanda FP": m["vu_dfp"],
        "Fio B energia FP": m["fb_efp"], "Fio B energia P": m["fb_ep"], "Fio B demanda": m["fb_d"],
        "Fio B demanda FP": m["fb_dfp"], "% Lei 14.300": m["escalonamento"],
        "Fio B efetivo energia FP": m["fbe_efp"], "Fio B efetivo energia P": m["fbe_ep"],
        "Valor kWh compensado FP": m["vc_efp"], "Valor kWh compensado P": m["vc_ep"], "CNPJ": m["cnpj"]})

    linhas = []
    for (s, mo) in ga["pares"]:
        for c, mat in ga["mats"][(s, mo)].items():
            nome, unid = METRICAS_A[c]
            if mo == "Azul" and c == "d":
                nome = "Demanda P"
            for dist, vals in mat.iterrows():
                linhas.append({"Distribuidora": dist, "Subgrupo": s, "Modalidade": mo, "Metrica": nome,
                               "Unidade": unid, **{int(a): v for a, v in vals.items()}})
    anual = pd.DataFrame(linhas)
    if len(anual):
        anos = sorted(c for c in anual.columns if isinstance(c, int))
        anual = anual[["Distribuidora", "Subgrupo", "Modalidade", "Metrica", "Unidade"] + anos]

    t = ordem(tl).sort_values(["_s", "_m", "distribuidora", "inicio"])
    vig = pd.DataFrame({
        "Distribuidora": t["distribuidora"], "Subgrupo": t["subgrupo"], "Modalidade": t["modalidade"],
        "Inicio vigencia": data(t["inicio"]), "Fim vigencia": data(t["fim"]), "Resolucao": t["reh"],
        "TUSD energia FP": t["tusd_efp"], "TE energia FP": t["te_efp"], "Energia FP": t["efp"],
        "Var energia FP": t["var_efp"], "TUSD energia P": t["tusd_ep"], "TE energia P": t["te_ep"],
        "Energia P": t["ep"], "Var energia P": t["var_ep"], "Demanda": t["d"], "Var demanda": t["var_d"],
        "Demanda FP": t["dfp"], "Var demanda FP": t["var_dfp"], "CNPJ": t["cnpj"]})

    f = ordem(tlf).sort_values(["_s", "_m", "distribuidora", "inicio"])
    fio = pd.DataFrame({
        "Distribuidora": f["distribuidora"], "Subgrupo": f["subgrupo"], "Modalidade": f["modalidade"],
        "Inicio vigencia": data(f["inicio"]), "Fim vigencia": data(f["fim"]), "Resolucao": f["reh"],
        "Fio B energia FP": f["fb_efp"], "Var Fio B energia FP": f["var_fb_efp"],
        "Fio B energia P": f["fb_ep"], "Var Fio B energia P": f["var_fb_ep"],
        "Fio B demanda": f["fb_d"], "Var Fio B demanda": f["var_fb_d"],
        "Fio B demanda FP": f["fb_dfp"], "Var Fio B demanda FP": f["var_fb_dfp"],
        "% Lei 14.300": f["escalonamento"], "Fio B efetivo energia FP": f["fbe_efp"],
        "Fio B efetivo energia P": f["fbe_ep"], "CNPJ": f["cnpj"]})
    return [("Grupo A Atual", atual, False, "tl"), ("Grupo A Anual", anual, False, "rs"),
            ("Grupo A Vigencias", vig, False, "tl"), ("Grupo A Fio B", fio, False, "tl")]


def abas_comparacao(visoes: dict) -> list[tuple]:
    """Abas novas do Excel: Comparativo, Valor kWh Compensado, Acumulado x IPCA, Regioes UF, Calendario,
    Distribuidoras. Mesmas contas do painel HTML (tabela de metricas)."""
    met = visoes["metricas"].copy()
    med = visoes["medianas"]
    hoje = visoes["hoje"]
    met["Distribuidora"] = met.index
    ord_ = met.sort_values(["ativa", "Distribuidora"], ascending=[False, True])
    med_uf = {u: v["b3"] for u, v in med["uf"].items()}
    sim = lambda s: s.map(lambda v: "Sim" if v else "Nao")  # noqa: E731
    data = lambda s: s.map(lambda v: data_br(v) if isinstance(v, str) else v)  # noqa: E731

    comp = pd.DataFrame({
        "Distribuidora": ord_.index, "Sigla ANEEL": ord_["sigla"], "UF": ord_["uf"], "Regiao": ord_["regiao"],
        "Grupo": ord_["grupo"], "Ativa": sim(ord_["ativa"]),
        "Inicio vigencia": data(ord_["inicio"]), "Fim vigencia": data(ord_["fim"]), "Resolucao": ord_["reh"],
        "TUSD": ord_["tusd"], "TE": ord_["te"], "Total TUSD+TE": ord_["total"],
        "Fio B SCEE": ord_["fiob"], "% Lei 14.300": ord_["escalonamento"], "Fio B efetivo": ord_["fiob_efetivo"],
        "Fio B / TUSD": ord_["fiob_tusd"], "Valor kWh compensado": ord_["valor_comp"], "% da tarifa": ord_["pct_comp"],
        "Ultimo reajuste": data(ord_["ultimo_reajuste"]), "Var ultimo reajuste": ord_["var_ult"],
        **{f"Acumulado desde {a}": ord_[f"acum_{a}"] for a in ANOS_ACUMULADO},
        **{f"CAGR desde {a}": ord_[f"cagr_{a}"] for a in ANOS_ACUMULADO},
        "Var 12 meses": ord_["var_12m"],
        "Proximo reajuste": data(ord_["prox_inicio"]), "Nova homologacao publicada": sim(ord_["prox_publicada"]),
        "Proximo total": ord_["prox_total"], "Var proximo reajuste": ord_["prox_var"],
        "Ranking B3 (1 = mais cara)": ord_["rank_b3"], "Ranking valor compensado (1 = maior)": ord_["rank_comp"],
        "Dif. vs mediana nacional": ord_["total"] / med["br"]["b3"] - 1 if med["br"]["b3"] else None,
        "Dif. vs mediana UF": ord_["total"] / ord_["uf"].map(med_uf) - 1,
        "CNPJ": ord_["cnpj"],
    })

    v = met[met["ativa"] & met["valor_comp"].notna()].sort_values("valor_comp", ascending=False)
    valor = pd.DataFrame({
        "Posicao": range(1, len(v) + 1), "Distribuidora": v.index, "UF": v["uf"], "Regiao": v["regiao"],
        "Inicio vigencia": data(v["inicio"]), "Total TUSD+TE": v["total"], "Fio B SCEE": v["fiob"],
        "% Lei 14.300": v["escalonamento"], "Fio B efetivo": v["fiob_efetivo"],
        "Valor kWh compensado": v["valor_comp"], "% da tarifa": v["pct_comp"]})

    a = met[met["ativa"]].sort_values("Distribuidora")
    acum = pd.DataFrame({"Distribuidora": a.index, "UF": a["uf"], "Total TUSD+TE": a["total"]})
    for ano in ANOS_ACUMULADO:
        acum[f"B3 em 01/01/{ano}"] = a[f"b3_{ano}"]
        acum[f"Acumulado desde {ano}"] = a[f"acum_{ano}"]
        acum[f"CAGR desde {ano}"] = a[f"cagr_{ano}"]
        acum[f"IPCA desde {ano}"] = a[f"ipca_{ano}"]
        acum[f"Real desde {ano}"] = a[f"real_{ano}"]
    acum["B3 12 meses antes"] = a["b3_12m"]
    acum["Var 12 meses"] = a["var_12m"]
    acum["IPCA 12 meses"] = a["ipca_12m"]
    acum["Real 12 meses"] = a["real_12m"]
    acum["Ultimo reajuste"] = data(a["ultimo_reajuste"])
    acum["Total anterior"] = a["anterior"]
    acum["Var ultimo reajuste"] = a["var_ult"]
    acum["IPCA no periodo do reajuste"] = a["ipca_ult"]
    acum["Real ultimo reajuste"] = a["real_ult"]

    linhas = []
    for nivel, chave, grupos in [("Brasil", None, {"Brasil": med["br"]}),
                                 ("Regiao", "regiao", {r: med["regiao"][r] for r in ORDEM_REGIOES if r in med["regiao"]}),
                                 ("UF", "uf", dict(sorted(med["uf"].items(), key=lambda x: (
                                     ORDEM_REGIOES.index(REGIAO_UF.get(x[0], "Sul")), x[0]))))]:
        for nome, g in grupos.items():
            linhas.append({"Nivel": nivel, "Regiao": REGIAO_UF.get(nome, nome) if nivel == "UF" else nome,
                           "UF": nome if nivel == "UF" else "", "Qtd distribuidoras": g["n"],
                           "Mediana B3 total": g["b3"], "Mediana TUSD": g["tusd"], "Mediana TE": g["te"],
                           "Mediana Fio B SCEE": g["fiob"], "Mediana Fio B efetivo": g["fiob_efetivo"],
                           "Mediana valor kWh compensado": g["valor_comp"],
                           **{f"Mediana acumulado {x}": g[f"acum_{x}"] for x in ANOS_ACUMULADO},
                           **{f"Mediana real {x}": g[f"real_{x}"] for x in ANOS_ACUMULADO},
                           "Mediana var 12 meses": g["var_12m"], "Distribuidoras": ", ".join(g["dists"])})
    regioes = pd.DataFrame(linhas)

    cal = calendario(met, hoje)
    calendario_df = pd.DataFrame({
        "Data prevista": data(cal["prox_inicio"]), "Situacao": cal["situacao"], "Distribuidora": cal.index,
        "UF": cal["uf"], "Inicio vigencia": data(cal["inicio"]), "Fim vigencia": data(cal["fim"]),
        "Total TUSD+TE": cal["total"], "Fio B SCEE": cal["fiob"], "Valor kWh compensado": cal["valor_comp"],
        "Nova resolucao": cal["prox_reh"].map(lambda r: reh_curta(r) if isinstance(r, str) else ""),
        "Novo total": cal["prox_total"], "Var nova vigencia": cal["prox_var"]})

    cat = visoes["catalogo"].reset_index()
    rot = rotulos_por_cnpj(visoes)
    cat.insert(1, "rotulo_no_painel", cat["cnpj"].map(rot).fillna(""))
    return [("Comparativo", comp, False, "tl"), ("Valor kWh Compensado", valor, False, "tl"),
            ("Acumulado x IPCA", acum, False, "tl"), ("Regioes UF", regioes, False, "tl"),
            ("Calendario", calendario_df, False, "tl"), ("Distribuidoras", cat, False, "tl")]


def calendario(met: pd.DataFrame, hoje: date) -> pd.DataFrame:
    """Ativas cuja vigencia atual termina nos proximos DIAS_CALENDARIO dias, ou ja terminou sem nova vigencia."""
    lim = (hoje + timedelta(days=DIAS_CALENDARIO)).isoformat()
    a = met[met["ativa"] & met["fim"].notna()]
    c = a[a["fim"] <= lim].copy()
    hoje_iso = hoje.isoformat()
    c["situacao"] = [("Nova homologação publicada" if p else
                      ("Vigência vencida, sem nova homologação na base" if f < hoje_iso else "Aguardando homologação"))
                     for p, f in zip(c["prox_publicada"], c["fim"])]
    return c.sort_values(["prox_inicio", "fim"]).copy()


def gerar_painel(caminho: Path, visoes: dict, log_path: Path, cfg: dict, agora: datetime, estado: dict) -> None:
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    def tl_cols(d, cols):
        return d[cols].sort_values(["distribuidora", "inicio"]).rename(columns={
            "distribuidora": "Distribuidora", "cnpj": "CNPJ", "reh": "Resolucao", "inicio": "Inicio vigencia",
            "fim": "Fim vigencia", "tusd": "TUSD", "te": "TE", "total": "Total TUSD+TE", "fiob": "Fio B",
            "anterior": "Vigencia anterior", "var_pct": "Var %", "escalonamento": "% Lei 14.300",
            "fiob_efetivo": "Fio B efetivo", "inicio_anterior": "Inicio vigencia anterior"})

    log = ler_log(log_path)
    rotulos = rotulos_por_cnpj(visoes)
    if len(log) and "cnpj" in log.columns:
        log["distribuidora"] = [rotulos.get(str(c).zfill(14), d) for c, d in zip(log["cnpj"], log["distribuidora"])]
    avisos = avisos_qualidade(visoes, cfg, estado)

    leiame = pd.DataFrame({"Monitor de Tarifas ANEEL": [
        f"Atualizado em {agora:%d/%m/%Y %H:%M} (Brasilia)", ""] + textos_metodologia(visoes, cfg)
        + (["", "AVISOS DE QUALIDADE DOS DADOS"] + ["- " + a for a in avisos] if avisos else [])})
    rank = tl_cols(visoes["rank_fiob"], ["distribuidora", "reh", "inicio", "fiob", "inicio_anterior",
                                         "anterior", "var_pct"]).sort_values("Var %", ascending=False)
    abas_extra = abas_comparacao(visoes)

    abas = [
        ("Leia-me", leiame, False, None),
        *abas_extra,
        *abas_impostos(visoes),
        ("B3 Total Anual", visoes["m_tarifa"], True, "rs"),
        ("B3 Var % Anual", variacao(visoes["m_tarifa"]), True, "pct"),
        ("FioB SCEE Anual", visoes["m_fiob_scee"], True, "rs"),
        ("FioB SCEE Efetivo", visoes["m_fiob_efetivo"], True, "rs"),
        ("FioB SCEE Ranking", rank, False, "tl"),
        ("B3 Vigencias", tl_cols(visoes["tl_tarifa"], ["distribuidora", "cnpj", "reh", "inicio", "fim", "tusd",
                                                       "te", "total", "anterior", "var_pct"]), False, "tl"),
        ("FioB SCEE Vigencias", tl_cols(visoes["tl_fiob_scee"], [
            "distribuidora", "cnpj", "reh", "inicio", "fim", "fiob", "anterior", "var_pct",
            "escalonamento", "fiob_efetivo"]), False, "tl"),
        *abas_grupo_a(visoes),
        ("Log Mudancas", log.iloc[::-1], False, "tl"),
    ]
    var_a = {h for h in CABEC_PCT if h.startswith(("Var energia", "Var demanda", "Reajuste ", "Var Fio B"))}

    cabec_fill = PatternFill("solid", fgColor="1F3864")
    cabec_font = Font(bold=True, color="FFFFFF")
    estrela_fill = PatternFill("solid", fgColor="FFF2CC")
    with pd.ExcelWriter(caminho, engine="openpyxl") as xw:
        for nome, df, com_indice, tipo in abas:
            df.to_excel(xw, sheet_name=nome, index=com_indice)
            ws = xw.sheets[nome]
            if nome == "Leia-me":
                ws.column_dimensions["A"].width = 120
                ws["A1"].font = Font(bold=True, size=14)
                continue
            for c in ws[1]:
                c.fill, c.font = cabec_fill, cabec_font
                c.alignment = Alignment(horizontal="center", wrap_text=True)
            ws.freeze_panes = "B2" if com_indice else "A2"
            nlin, ncol = ws.max_row, ws.max_column
            cabecalhos = [c.value for c in ws[1]]
            for j in range(1, ncol + 1):
                letra = get_column_letter(j)
                h = str(cabecalhos[j - 1] or "")
                largura = 34 if j == 1 or h in ("Resolucao", "Distribuidora") else 14
                if h in ("Resolucao", "Razao social", "Distribuidoras", "Observacao", "observacao", "Fonte aliquotas",
                         "Fonte ICMS", "Fonte PIS/COFINS", "Fonte flags"):
                    largura = 60
                ws.column_dimensions[letra].width = largura
                if j == 1 and com_indice:
                    continue
                if tipo == "pct" or h in CABEC_PCT:
                    fmt = "0.0%"
                elif tipo == "rs" or h in CABEC_RS:
                    fmt = "#,##0.00"
                else:
                    continue
                for i in range(2, nlin + 1):
                    ws.cell(i, j).number_format = fmt
            if nlin > 1 and (tipo == "pct" or "Var %" in cabecalhos):
                if tipo == "pct":
                    rng = f"B2:{get_column_letter(ncol)}{nlin}"
                else:
                    col = get_column_letter(cabecalhos.index("Var %") + 1)
                    rng = f"{col}2:{col}{nlin}"
                ws.conditional_formatting.add(rng, ColorScaleRule(
                    start_type="num", start_value=-0.15, start_color="63BE7B",
                    mid_type="num", mid_value=0, mid_color="FFFFFF",
                    end_type="num", end_value=0.15, end_color="F8696B"))
            if nlin > 1 and nome.startswith("Grupo A"):
                for j, h in enumerate(cabecalhos, start=1):
                    if h in var_a:
                        col = get_column_letter(j)
                        ws.conditional_formatting.add(f"{col}2:{col}{nlin}", ColorScaleRule(
                            start_type="num", start_value=-0.15, start_color="63BE7B",
                            mid_type="num", mid_value=0, mid_color="FFFFFF",
                            end_type="num", end_value=0.15, end_color="F8696B"))
            if nome.startswith("Grupo A") and cfg["distribuidoras_portfolio"] and cabecalhos[0] == "Distribuidora":
                for i in range(2, nlin + 1):
                    c = ws.cell(i, 1)
                    if c.value and eh_portfolio(str(c.value), cfg):
                        c.fill = estrela_fill
                        c.value = f"★ {c.value}"
            if com_indice and cfg["distribuidoras_portfolio"]:
                for i in range(2, nlin + 1):
                    c = ws.cell(i, 1)
                    if c.value and eh_portfolio(str(c.value), cfg):
                        c.fill = estrela_fill
                        c.value = f"★ {c.value}"
            ws.auto_filter.ref = ws.dimensions


# ---------------------------------------------------------------------- painel HTML

# Selecao padrao do comparador (por CNPJ): ENEL SP, LIGHT, ENEL RJ, CPFL PIRATININGA
DIST_PADRAO_CNPJ = ["61695227000193", "60444437000146", "33050071000158", "04172213000151"]
MAX_SELECAO = 4
CHARTJS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"


def _num(v, casas: int = 4):
    """Numero JSON-seguro: NaN/Infinity/None viram null."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return round(f, casas)


def _txt(v) -> str:
    return "" if v is None or (isinstance(v, float) and v != v) else str(v)


def _mat_json(m: pd.DataFrame, rotulo: str) -> dict:
    anos = [int(a) for a in m.columns]
    return {"rotulo": rotulo, "anos": anos,
            "v": {str(d): [_num(x) for x in linha] for d, linha in zip(m.index, m.values)}}


def _busca(*partes) -> str:
    """Texto de busca: maiusculas, sem acento, so letras/numeros separados por espaco."""
    s = unicodedata.normalize("NFKD", " ".join(_txt(p) for p in partes)).encode("ascii", "ignore").decode().upper()
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", s).split())


def _med_json(g: dict) -> dict:
    return {k: (v if k in ("n", "dists") else _num(v, 6)) for k, v in g.items()}


def dados_grupo_a_html(visoes: dict) -> dict:
    """JSON da aba Grupo A, por par 'subgrupo modalidade' (ex.: 'A4 Verde')."""
    ga = visoes.get("ga") or {}
    met, tl, tlf = ga.get("met"), ga.get("tl"), ga.get("tlf")
    if met is None or not len(met):
        return {"pares": [], "subs": ga.get("subgrupos", []), "s": {}}
    s_out = {}
    for (s, mo) in ga["pares"]:
        k = f"{s} {mo}"
        x = met[(met["subgrupo"] == s) & (met["modalidade"] == mo)]
        atual = {}
        for r in x.itertuples():
            atual[r.distribuidora] = {
                "a": bool(r.ativa), "ini": r.inicio, "fim": _txt(r.fim), "reh": reh_curta(_txt(r.reh)),
                "ur": _txt(r.ultimo_reajuste),
                **{c: _num(getattr(r, c)) for c in VAL_A},
                **{"vu_" + c: _num(getattr(r, "vu_" + c), 6) for c in METRICAS_A},
                **{"ant_" + c: _num(getattr(r, "ant_" + c)) for c in METRICAS_A},
                "rel_e": _num(r.rel_e, 6), "rel_d": _num(r.rel_d, 6),
                "fbi": _txt(r.fb_inicio), "fbr": reh_curta(_txt(r.fb_reh)) if _txt(r.fb_reh) else "",
                "esc": _num(r.escalonamento), **{c: _num(getattr(r, c)) for c in FB_A},
                "fbe_efp": _num(r.fbe_efp), "fbe_ep": _num(r.fbe_ep), "vc_efp": _num(r.vc_efp), "vc_ep": _num(r.vc_ep),
            }
        t = tl[(tl["subgrupo"] == s) & (tl["modalidade"] == mo)]
        hist = {}
        # [inicio, fim, reh, efp, ep, d, dfp] (vigencias com TUSD e TE separadas ficam no Excel)
        for d, g in t.groupby("distribuidora"):
            hist[d] = [[r.inicio, _txt(r.fim), reh_curta(_txt(r.reh)), _num(r.efp, 2), _num(r.ep, 2), _num(r.d, 2),
                        _num(r.dfp, 2)] for r in g.sort_values("inicio", ascending=False).itertuples()]
        med = ga["med"][(s, mo)]
        s_out[k] = {"sub": s, "mod": mo, "atual": atual, "hist": hist,
                    "med": {c: (v if c in ("n", "n_vc") else _num(v, 6)) for c, v in med.items()},
                    "mat": {c: _mat_json(m, METRICAS_A[c][0]) for c, m in ga["mats"][(s, mo)].items()}}
    return {"pares": [f"{s} {mo}" for s, mo in ga["pares"]], "subs": ga["subgrupos"], "s": s_out}


def dados_html(visoes: dict, log_path: Path, cfg: dict, agora: datetime, estado: dict) -> dict:
    mt, ms, me = (visoes[k] for k in ("m_tarifa", "m_fiob_scee", "m_fiob_efetivo"))
    tl_t, tl_f, rk = visoes["tl_tarifa"], visoes["tl_fiob_scee"], visoes["rank_fiob"]
    met, med, ctx = visoes["metricas"], visoes["medianas"], visoes["ctx"]
    ipca = visoes.get("ipca") or {}
    hoje = visoes["hoje"]
    ano = int(max(mt.columns))
    col = lambda m, a: m[a] if a in m.columns else pd.Series(dtype=float)  # noqa: E731

    var_b3 = (col(mt, ano) / col(mt, ano - 1) - 1).dropna()
    ult = tl_t.sort_values(["inicio", "distribuidora"]).iloc[-1]
    com_tarifa = set(col(mt, ano).dropna().index)
    # Mediana e media do Fio B sem os valores suspeitos (<= FIOB_SUSPEITO_MAX)
    fiob_ano = col(ms, ano).dropna()
    validos = fiob_ano[~suspeito(fiob_ano)].index
    efet_ano = col(me, ano).reindex(validos).dropna()

    gm = (visoes.get("ga") or {}).get("met")
    dists = sorted(set(mt.index) | set(ms.index) | set(met.index)
                   | (set(gm["distribuidora"]) if gm is not None and len(gm) else set()))
    info, mj, ref = {}, {}, {}
    imp = visoes.get("imp")
    for d in dists:
        if d not in met.index:
            info[d] = {"s": d, "n": d, "uf": "", "r": "", "g": "", "rz": "", "ufs": "", "a": False, "b": _busca(d)}
            continue
        r = met.loc[d]
        info[d] = {"s": r["sigla"], "n": r["nome"], "uf": r["uf"], "r": r["regiao"], "g": r["grupo"],
                   "rz": r["razao_social"], "ufs": r["ufs"], "a": bool(r["ativa"]), "cnpj": r["cnpj"],
                   "h": _txt(r["siglas_antigas"]),
                   "b": _busca(d, r["sigla"], r["nome"], r["uf"], r["regiao"], r["grupo"], r["razao_social"],
                               r["siglas_antigas"])}
        rank = lambda c: ([int(r[c]), int(r["n_" + c])] if pd.notna(r[c]) else None)  # noqa: E731
        mj[d] = {
            "ini": r["inicio"], "fim": _txt(r["fim"]), "reh": reh_curta(_txt(r["reh"])),
            "tusd": _num(r["tusd"]), "te": _num(r["te"]), "tot": _num(r["total"]),
            "fb": _num(r["fiob"]), "fbi": _txt(r["fiob_inicio"]), "fbr": reh_curta(_txt(r["fiob_reh"])),
            "esc": _num(r["escalonamento"]), "fbe": _num(r["fiob_efetivo"]), "sus": bool(r["fiob_suspeito"]),
            "fbt": _num(r["fiob_tusd"], 6), "vc": _num(r["valor_comp"]), "pvc": _num(r["pct_comp"], 6),
            "ur": _txt(r["ultimo_reajuste"]), "ant": _num(r["anterior"]), "vu": _num(r["var_ult"], 6),
            "iu": _num(r["ipca_ult"], 6), "ru": _num(r["real_ult"], 6),
            "iud": [_txt(r["ipca_ult_de"]), _txt(r["ipca_ult_ate"])],
            "acum": {str(a): [_num(r[f"b3_{a}"]), _num(r[f"acum_{a}"], 6), _num(r[f"cagr_{a}"], 6),
                              _num(r[f"ipca_{a}"], 6), _num(r[f"real_{a}"], 6)] for a in ANOS_ACUMULADO},
            "m12": [_num(r["b3_12m"]), _num(r["var_12m"], 6), _num(r["ipca_12m"], 6), _num(r["real_12m"], 6)],
            "px": _txt(r["prox_inicio"]), "pp": bool(r["prox_publicada"]), "pt": _num(r["prox_total"]),
            "pv": _num(r["prox_var"], 6), "preh": reh_curta(_txt(r["prox_reh"])) if _txt(r["prox_reh"]) else "",
            "rk": rank("rank_b3"), "rf": rank("rank_fiob"), "rc": rank("rank_comp"),
        }
        if imp is not None and d in imp.index:
            x = imp.loc[d]
            mj[d]["tx"] = {"i": _num(x["icms"], 6), "pis": _num(x["pis"], 6), "cof": _num(x["cofins"], 6),
                           "fi": _txt(x["fonte_icms"]), "fp": _txt(x["fonte_pc"]), "te": int(x["flag_te"]),
                           "tu": int(x["flag_tusd"]), "s": _num(x["s"], 4), "ff": _txt(x["fonte_flags"]),
                           "ob": _txt(x["obs"])}
            if x["ref_sigla"]:
                ref[d] = [x["ref_sigla"], _num(x["ref_tusd"], 2), _num(x["ref_te"], 2)]

    tl = {}
    for d, g in tl_t.groupby("distribuidora"):
        tl.setdefault(d, {"b3": [], "fiob": []})["b3"] = [
            [r.inicio, r.fim, reh_curta(r.reh), _num(r.tusd), _num(r.te), _num(r.total), _num(r.var_pct, 6)]
            for r in g.sort_values("inicio", ascending=False).itertuples()]
    for d, g in tl_f.groupby("distribuidora"):
        tl.setdefault(d, {"b3": [], "fiob": []})["fiob"] = [
            [r.inicio, r.fim, reh_curta(r.reh), _num(r.fiob), _num(r.var_pct, 6), _num(r.escalonamento),
             _num(r.fiob_efetivo), bool(suspeito(r.fiob))]
            for r in g.sort_values("inicio", ascending=False).itertuples()]

    rotulos = rotulos_por_cnpj(visoes)
    log = ler_log(log_path)
    # remocoes da fonte (podem ser milhares numa coleta) ficam no aviso de qualidade e no Excel, nao nas 50 ultimas
    log = log[log["tipo"] != TIPO_REMOVIDA].iloc[::-1].head(50)
    def det_log(r) -> str:
        # Grupo A: o que distingue a linha e o posto e a unidade (ex.: "Ponta, kW"); detalhe so quando nao e o normal
        if not str(r.get("serie", "")).startswith(("Grupo A", "Fio B Grupo A")):
            return _txt(r.get("detalhe"))
        partes = [_txt(r.get("detalhe")), _txt(r.get("posto")), _txt(r.get("unidade"))]
        return ", ".join(p for p in partes if p and p != NA)
    log_json = [{
        "em": str(r.get("detectado_em", "")), "serie": str(r.get("serie", "")), "tipo": str(r.get("tipo", "")),
        "dist": rotulos.get(str(r.get("cnpj", "") or "").zfill(14), str(r.get("distribuidora", ""))),
        "reh": reh_curta(str(r.get("reh", "") or "")),
        "inicio": str(r.get("inicio", "") or ""), "detalhe": det_log(r),
        "ant": _num(r.get("valor_anterior")), "novo": _num(r.get("valor_novo")),
    } for _, r in log.iterrows()]

    # IPCA: indice acumulado no fim de cada mes (1 = antes do primeiro mes da serie)
    mensal = ipca.get("mensal") or {}
    meses, idx, f = [], [], 1.0
    for m, v in mensal.items():
        f *= 1 + v / 100
        meses.append(m)
        idx.append(round(f, 8))
    per = {k: {"v": _num(v[0], 6), "de": v[1], "ate": v[2]} for k, v in ctx["periodos"].items()}

    # KPIs novos
    at = met[met["ativa"]]
    vc = at[at["valor_comp"].notna()].sort_values("valor_comp")
    cal = calendario(met, hoje)
    a0 = str(ANOS_ACUMULADO[0])
    kpis_novos = {
        "vc_max": [vc.index[-1], _num(vc["valor_comp"].iloc[-1]), _num(vc["pct_comp"].iloc[-1], 6)] if len(vc) else None,
        "vc_min": [vc.index[0], _num(vc["valor_comp"].iloc[0]), _num(vc["pct_comp"].iloc[0], 6)] if len(vc) else None,
        "vc_med": _num(med["br"]["valor_comp"]), "pvc_med": _num(med["br"]["pct_comp"], 6), "n_vc": int(len(vc)),
        "acum_med": _num(med["br"][f"acum_{a0}"], 6), "real_med": _num(med["br"][f"real_{a0}"], 6),
        "ipca_acum": per.get(a0, {}).get("v"),
        "n_cal": int(len(cal)), "n_cal_pub": int(cal["prox_publicada"].sum()) if len(cal) else 0,
        "n_ativas": int(met["ativa"].sum()),
    }
    cal_json = [[d, r["situacao"], _txt(r["prox_inicio"]), r["inicio"], _txt(r["fim"]), _num(r["total"]),
                 _num(r["fiob"]), _num(r["valor_comp"]), bool(r["prox_publicada"]),
                 reh_curta(_txt(r["prox_reh"])) if _txt(r["prox_reh"]) else "", _num(r["prox_total"]),
                 _num(r["prox_var"], 6)] for d, r in cal.iterrows()]

    por_cnpj = {v["cnpj"]: k for k, v in info.items() if v.get("cnpj")}
    padrao = [por_cnpj[c] for c in DIST_PADRAO_CNPJ if c in por_cnpj] or dists[:3]
    uv = estado.get("ultima_verificacao") or agora.strftime("%Y-%m-%d")
    ind = visoes["indice"]
    return {
        "meta": {"ultima_verificacao": data_br(uv), "gerado_em": agora.strftime("%d/%m/%Y %H:%M"), "ano": ano,
                 "hoje": hoje.isoformat(), "fiob_ano_inicial": cfg["fiob_ano_inicial"],
                 "ultima_vigencia_b3": data_br(estado.get("ultima_vigencia_b3") or ""),
                 "ultima_vigencia_fiob": data_br(estado.get("ultima_vigencia_fiob") or ""),
                 "anos_acum": [str(a) for a in ANOS_ACUMULADO], "dias_cal": DIAS_CALENDARIO,
                 "dias_ativa": DIAS_ATIVA, "max_sel": MAX_SELECAO, "regioes": ORDEM_REGIOES,
                 "esc": {str(k): v for k, v in ESCALONAMENTO_FIOB.items()}},
        "kpis": {
            "n_dist": int(len(mt.index)), "n_dist_ano": len(com_tarifa),
            "vig_dist": str(ult["distribuidora"]), "vig_data": data_br(ult["inicio"]), "vig_reh": reh_curta(ult["reh"]),
            "med_var_b3": _num(var_b3.median() if len(var_b3) else None, 6), "n_var_b3": int(len(var_b3)),
            "med_fiob": _num(fiob_ano.loc[validos].median() if len(validos) else None), "n_fiob": int(len(validos)),
            "n_suspeitos": int(len(visoes["fiob_suspeitos"])),
            "media_efetivo": _num(efet_ano.mean() if len(efet_ano) else None), "esc": ESCALONAMENTO_FIOB.get(ano),
            "med_var_fiob": _num(rk["var_pct"].median() if len(rk) else None, 6), "n_var_fiob": int(len(rk)),
            **kpis_novos,
        },
        "avisos": avisos_qualidade(visoes, cfg, estado),
        "portfolio": [d for d in dists if eh_portfolio(d, cfg)] if cfg["distribuidoras_portfolio"] else [],
        "dists": info,
        "lista": dists,
        "padrao": padrao,
        "padrao_dist": padrao[0] if padrao else (dists[0] if dists else ""),
        "m": mj,
        "med": {"br": _med_json(med["br"]), "reg": {k: _med_json(v) for k, v in med["regiao"].items()},
                "uf": {k: _med_json(v) for k, v in med["uf"].items()}},
        "ipca": {"meses": meses, "idx": idx, "ult": ctx["ipca_ultimo_mes"], "per": per,
                 "baixado_em": data_br(ipca.get("baixado_em") or "")},
        "indice": {"base": ind["base"], "rotulos": ind["rotulos"], "b3": [_num(x) for x in ind["b3"]],
                   "ipca": [_num(x) for x in ind["ipca"]], "n": ind["n"]},
        "cal": cal_json,
        "matrizes": {
            "b3": _mat_json(mt, "B3 total TUSD+TE"), "scee": _mat_json(ms, "Fio B SCEE"),
            "efetivo": _mat_json(me, "Fio B efetivo"),
        },
        # [distribuidora, var, valor atual, inicio atual, valor anterior, inicio anterior]
        "rank_fiob": [[str(r.distribuidora), _num(r.var_pct, 6), _num(r.fiob), r.inicio, _num(r.anterior),
                       r.inicio_anterior] for r in rk.itertuples()],
        "tl": tl,
        "log": log_json,
        "metodologia": textos_metodologia(visoes, cfg),
        "ga": dados_grupo_a_html(visoes),
        "ref": ref,
        "imp": {"origem": (visoes.get("imp_meta") or {}).get("origem", ORIGEM_FATURAS),
                "selo": (visoes.get("imp_meta") or {}).get("selo", ""), "padrao": {"i": ICMS_PADRAO, "pis": PIS_PADRAO, "cof": COFINS_PADRAO,
                                                     "s": REPASSE_PADRAO}},
        "arr": {"prod": {uf: [_num(v, 1), _txt(f)] for uf, (v, f) in sorted((visoes.get("prod") or {}).items())},
                "prod_padrao": PRODUTIVIDADE_PADRAO, "d": ARR_DESAGIO, "s": ARR_VB, "c": ARR_DESCONTO,
                "fc": ARR_FC_CGH, "horas": ARR_HORAS_MES, "grade_d": list(ARR_GRADE_D), "grade_s": list(ARR_GRADE_S),
                "neg": {"abertura": NEG_ABERTURA_PP, "mmin": NEG_MARGEM_MIN, "prazo": NEG_PRAZO, "taxa": NEG_TAXA,
                        "degr": NEG_DEGRADACAO, "ipca12": _num(ipca_12m(visoes), 6)}},
    }


def gerar_html(caminho: Path, visoes: dict, log_path: Path, cfg: dict, agora: datetime, estado: dict) -> None:
    dados = dados_html(visoes, log_path, cfg, agora, estado)
    # allow_nan=False garante JSON valido; "</" escapado para nao fechar o <script> antes da hora
    js = json.dumps(dados, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("</", "<\\/")
    html = HTML_TEMPLATE.replace("__CHARTJS__", CHARTJS_CDN).replace("__DADOS__", js)
    caminho.write_text(html, encoding="utf-8")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Monitor de Tarifas ANEEL</title>
<style>
:root{--azul:#1F3864;--azul2:#2F5597;--fundo:#F4F6FA;--card:#FFFFFF;--borda:#E1E6EF;--texto:#1E2533;
--suave:#5B6577;--verde:#2E7D4F;--vermelho:#C0392B;--amarelo:#FFF2CC;--sombra:0 1px 3px rgba(16,30,60,.08),0 4px 14px rgba(16,30,60,.05)}
*{box-sizing:border-box}
body{margin:0;background:var(--fundo);color:var(--texto);font:14px/1.45 "Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{background:var(--azul);color:#fff;padding:22px 28px 0}
header h1{margin:0 0 6px;font-size:22px;font-weight:600;letter-spacing:.2px}
header .meta{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:13px;opacity:.92}
.status{display:inline-flex;align-items:center;gap:6px}
.status i{width:9px;height:9px;border-radius:50%;background:#4ADE80;display:inline-block;box-shadow:0 0 0 3px rgba(74,222,128,.25)}
nav.abas{display:flex;gap:2px;overflow-x:auto;margin-top:16px;scrollbar-width:thin}
nav.abas button{flex:0 0 auto;border:0;background:transparent;color:#D6DEEE;font:inherit;font-size:13.5px;padding:10px 14px;cursor:pointer;
border-radius:8px 8px 0 0;white-space:nowrap}
nav.abas button:hover{background:rgba(255,255,255,.08);color:#fff}
nav.abas button.ativo{background:var(--fundo);color:var(--azul);font-weight:600}
main{max-width:1320px;margin:0 auto;padding:20px 16px 40px}
.aba{display:none}.aba.ativo{display:block}
.grid{display:grid;gap:16px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.dois{grid-template-columns:repeat(auto-fit,minmax(min(520px,100%),1fr))}
.tres{grid-template-columns:repeat(auto-fit,minmax(min(340px,100%),1fr))}
.card{background:var(--card);border:1px solid var(--borda);border-radius:10px;box-shadow:var(--sombra);padding:16px 18px;min-width:0}
.kpi .rot{font-size:12px;color:var(--suave);text-transform:uppercase;letter-spacing:.4px}
.kpi .val{font-size:24px;font-weight:650;color:var(--azul);margin:4px 0 2px;font-variant-numeric:tabular-nums}
.kpi .sub{font-size:12px;color:var(--suave)}
h2{font-size:16px;color:var(--azul);margin:0 0 12px;font-weight:600}
h3{font-size:14px;color:var(--azul);margin:14px 0 8px;font-weight:600}
section{margin-top:16px}
.avisos{background:#FFF8E6;border:1px solid #F3D27A;border-left:4px solid #E0A800;border-radius:8px;padding:10px 14px;margin-top:16px}
.avisos b{color:#7A5600}
.avisos ul{margin:6px 0 0;padding-left:18px}
.seletor{position:relative}
.seletor input[type=search],.filtro{width:100%;max-width:460px;padding:8px 10px;border:1px solid var(--borda);border-radius:7px;font:inherit}
.lista{position:absolute;z-index:20;top:40px;left:0;width:100%;max-width:460px;max-height:320px;overflow:auto;background:#fff;
border:1px solid var(--borda);border-radius:8px;box-shadow:var(--sombra);display:none}
.lista label,.lista .op{display:flex;gap:8px;align-items:baseline;padding:6px 10px;cursor:pointer}
.lista label:hover,.lista .op:hover{background:#EEF2F9}
.lista .det{color:var(--suave);font-size:12px;margin-left:auto;white-space:nowrap}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:4px 6px 4px 10px;border-radius:999px;background:#EAF0FA;color:var(--azul);font-size:13px;border:1px solid #CFDBEF}
.chip .cor{width:10px;height:10px;border-radius:50%}
.chip button{border:0;background:none;cursor:pointer;color:var(--suave);font-size:15px;line-height:1;padding:0 4px}
.btn{border:1px solid var(--borda);background:#fff;color:var(--azul);padding:6px 12px;border-radius:7px;cursor:pointer;font:inherit;font-size:13px}
.btn.ativo{background:var(--azul);color:#fff;border-color:var(--azul)}
.barra{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.barra .esp{flex:1}
.graf{position:relative;height:340px}
.graf.alto{height:560px}
.graf.baixo{height:260px}
.tab{overflow:auto;max-height:620px;border:1px solid var(--borda);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid #EDF0F5;white-space:nowrap}
th{background:var(--azul);color:#fff;font-weight:600;position:sticky;top:0;z-index:2;text-align:center}
th.ord{cursor:pointer;user-select:none}
th.ord:hover{background:var(--azul2)}
td.n{text-align:right}
td.nome,th.nome{text-align:left;position:sticky;left:0;background:#fff;z-index:1}
th.nome{z-index:3;background:var(--azul)}
tr.port td.nome{background:var(--amarelo)}
tr.sub td{background:#F7F9FC;font-weight:600;color:var(--azul)}
table.lado td:first-child{font-weight:600;color:var(--azul);text-align:left;position:sticky;left:0;background:#fff}
table.lado th:first-child{position:sticky;left:0;z-index:3}
.legenda{display:flex;gap:10px;align-items:center;font-size:12px;color:var(--suave);margin-top:8px;flex-wrap:wrap}
.escala{width:160px;height:10px;border-radius:5px;background:linear-gradient(90deg,#63BE7B,#fff,#F8696B);border:1px solid var(--borda)}
.pos{color:var(--vermelho)}.neg{color:var(--verde)}
.mut{color:var(--suave);font-size:12px}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;font-weight:600}
.tag.ok{background:#E3F4EA;color:var(--verde)}.tag.esp{background:#EAF0FA;color:var(--azul)}.tag.alerta{background:#FDECEA;color:var(--vermelho)}
.ficha{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:13px;color:var(--suave);margin:4px 0 0}
.ficha b{color:var(--texto);font-weight:600}
.nomegrande{font-size:20px;font-weight:650;color:var(--azul)}
select{padding:6px 8px;border:1px solid var(--borda);border-radius:7px;font:inherit;max-width:100%}
.metodo p{margin:6px 0}
.erro{padding:30px;text-align:center;color:var(--suave)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.grupo{display:inline-flex;gap:4px;align-items:center;margin-right:10px}
.campos{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:flex-end;margin-bottom:12px}
.campos label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--suave)}
.campos input{width:130px;padding:7px 9px;border:1px solid var(--borda);border-radius:7px;font:inherit}
.campos select{min-width:240px}
.sim-res{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(min(260px,100%),1fr));margin-bottom:12px}
.sim-res .card{box-shadow:none}
.sim-res .melhor{border:2px solid var(--verde)}
.selo-imp{display:inline-block;font-size:11.5px;font-weight:500;color:var(--suave);border:1px solid var(--borda);border-radius:10px;padding:1px 8px;margin-left:6px;vertical-align:middle}
.selo-imp:empty{display:none}
.nota-sim{background:#F7F9FC;border:1px solid var(--borda);border-radius:8px;padding:8px 12px;font-size:12.5px;color:var(--suave);margin-top:10px}
.kpi.negativo{border:2px solid var(--vermelho);background:#FDECEA}.kpi.negativo .val{color:var(--vermelho)}
td.negativo{color:var(--vermelho);font-weight:600}
table.sens td.n{text-align:center;min-width:92px}
table.sens td.atual{outline:3px solid var(--azul);outline-offset:-3px;font-weight:700}
table.sens th.lin{background:#2F5597}
table.faixa td.abaixo{background:#FDECEA;color:var(--vermelho);font-weight:600}
table.faixa th.abaixo{background:var(--vermelho)}
table.faixa input{width:76px;padding:4px 6px;border:1px solid var(--borda);border-radius:6px;font:inherit;text-align:right}
.n-tit{color:var(--azul);font-size:13px}
td.sem-sol{color:var(--vermelho);font-weight:600}
tr.tot td{background:#F7F9FC;font-weight:650;color:var(--azul)}
.campos input.largo{width:220px}
a{color:var(--azul2)}
@media (max-width:640px){header{padding:18px 16px 0}header h1{font-size:18px}.graf{height:280px}.graf.alto{height:480px}.kpi .val{font-size:21px}}
</style>
</head>
<body>
<header>
  <h1 id="m-tit">Monitor de Tarifas ANEEL: B3 Convencional, Fio B SCEE e Grupo A</h1>
  <div class="meta">
    <span>Última verificação: <b id="m-verif"></b></span>
    <span>Painel gerado em <span id="m-gerado"></span> (Brasília)</span>
    <span>Fontes: dadosabertos.aneel.gov.br e Banco Central (IPCA)</span>
    <span><a href="Painel_Tarifas_ANEEL.xlsx" download style="color:#fff;font-weight:600">Baixar Excel</a></span>
    <span class="status"><i></i>Agente ativo: verificação automática diária</span>
  </div>
  <nav class="abas" id="abas">
    <button data-aba="geral" class="ativo">Visão geral</button>
    <button data-aba="distribuidora">Distribuidora</button>
    <button data-aba="comparador">Comparador</button>
    <button data-aba="calculadora">Calculadora</button>
    <button data-aba="arrendamento">Arrendamento</button>
    <button data-aba="thopen">Tabela Thopen</button>
    <button data-aba="grupoa">Grupo A</button>
    <button data-aba="rankings">Rankings</button>
    <button data-aba="regioes">Regiões</button>
    <button data-aba="calendario">Calendário</button>
    <button data-aba="mudancas">Mudanças</button>
    <button data-aba="metodologia">Metodologia</button>
  </nav>
</header>
<main>

<!-- ============ VISAO GERAL ============ -->
<div class="aba ativo" id="aba-geral">
  <div class="grid kpis" id="kpis"></div>
  <div id="avisos"></div>
  <section class="grid dois">
    <div class="card"><h2 id="ind-tit">B3 total x IPCA</h2><div class="graf"><canvas id="g-indice"></canvas></div>
      <div class="mut" id="ind-nota"></div></div>
    <div class="card"><div class="barra"><h2 style="margin:0">Próximos reajustes</h2><span class="esp"></span>
      <button class="btn" data-ir="calendario">Ver calendário</button></div>
      <div class="tab" id="geral-cal" style="max-height:340px"></div></div>
  </section>
  <section class="card">
    <h2>Valor do kWh compensado (GD II): 10 maiores e 10 menores</h2>
    <div class="graf alto"><canvas id="g-geral-vc"></canvas></div>
    <div class="mut">B3 total menos o Fio B efetivo (Fio B SCEE x % da Lei 14.300). R$/MWh, sem tributos. Barras: valor compensado; em cinza, o Fio B efetivo pago.</div>
  </section>
</div>

<!-- ============ DISTRIBUIDORA ============ -->
<div class="aba" id="aba-distribuidora">
  <section class="card" style="margin-top:0">
    <div class="seletor"><input type="search" id="d-busca" placeholder="Buscar por nome, sigla da ANEEL, UF, região ou grupo (ex.: Enel SP, Eletropaulo, SP, Equatorial)" autocomplete="off">
      <div class="lista" id="d-lista"></div></div>
    <div style="margin-top:12px"><div class="nomegrande" id="d-nome"></div><div class="ficha" id="d-ficha"></div></div>
  </section>
  <div class="grid kpis" id="d-kpis" style="margin-top:16px"></div>
  <section class="grid dois">
    <div class="card"><h2>Posição relativa (distribuidoras ativas)</h2><div class="tab" id="d-pos"></div>
      <div class="mut" style="margin-top:6px">Diferença contra a mediana entre parênteses. Posição 1 = maior valor.</div></div>
    <div class="card"><h2>Composição da tarifa (R$/MWh)</h2><div class="graf baixo"><canvas id="g-d-comp"></canvas></div>
      <div class="mut" id="d-comp-nota"></div></div>
  </section>
  <section class="card"><h2>Grupo A: vigência atual (Tarifa de Aplicação)</h2><div class="tab" id="d-ga"></div>
    <div class="mut" style="margin-top:6px">Energia = TUSD+TE (R$/MWh); demanda = TUSD (R$/kW). Detalhes, simulador Verde x Azul e Fio B na aba Grupo A.</div></section>
  <section class="grid dois">
    <div class="card"><h2>B3 total acumulada x IPCA</h2><div class="tab" id="d-acum"></div>
      <div class="mut" id="d-acum-nota" style="margin-top:6px"></div></div>
    <div class="card"><h2 id="d-ipca-tit">B3 total x B3 corrigida pelo IPCA (R$/MWh)</h2><div class="graf"><canvas id="g-d-ipca"></canvas></div></div>
  </section>
  <section class="grid dois">
    <div class="card"><h2>Vigências B3 Convencional, Tarifa de Aplicação</h2><div class="tab" id="tl-b3" style="max-height:320px"></div></div>
    <div class="card"><h2 id="tl-fiob-tit">TUSD Fio B, detalhe SCEE</h2><div class="tab" id="tl-fiob" style="max-height:320px"></div></div>
  </section>
</div>

<!-- ============ COMPARADOR ============ -->
<div class="aba" id="aba-comparador">
  <section class="card" style="margin-top:0">
    <h2>Distribuidoras comparadas (2 a 4)</h2>
    <div class="seletor"><input type="search" id="busca" placeholder="Buscar distribuidora para adicionar ou remover (nome, sigla, UF, grupo)" autocomplete="off">
      <div class="lista" id="lista"></div></div>
    <div class="chips" id="chips"></div>
  </section>
  <section class="card"><h2>Lado a lado</h2><div class="tab" id="lado"></div>
    <div class="mut" style="margin-top:6px">Valores em R$/MWh, sem tributos, exceto as linhas "com tributos" (V e W, gross-up em cascata como na planilha Tarifas.xlsx; detalhes na aba Calculadora e na Metodologia). Vigência atual: a última iniciada até a data do painel.</div></section>
  <section class="grid dois">
    <div class="card"><h2>Composição TUSD x TE (R$/MWh)</h2><div class="graf"><canvas id="g-comp"></canvas></div>
      <div class="mut">Fio B (SCEE) é parte da TUSD; a participação dele na TUSD aparece no rótulo.</div></div>
    <div class="card"><h2>Valor do kWh compensado (GD II, R$/MWh)</h2><div class="graf"><canvas id="g-vc"></canvas></div>
      <div class="mut">Valor compensado + Fio B efetivo = B3 total.</div></div>
  </section>
  <section class="grid dois">
    <div class="card"><h2>B3 total TUSD+TE (R$/MWh) por ano</h2><div class="graf"><canvas id="g-b3"></canvas></div></div>
    <div class="card"><h2 id="fiob-tit">Fio B SCEE (R$/MWh) por vigência</h2><div class="graf"><canvas id="g-fiob"></canvas></div>
      <div class="mut">Linha cheia: Fio B SCEE homologado. Tracejada: efetivo (x % da Lei 14.300). Cada degrau é uma vigência.</div></div>
  </section>
</div>

<!-- ============ CALCULADORA ============ -->
<div class="aba" id="aba-calculadora">
  <section class="card" style="margin-top:0">
    <div class="barra"><h2 style="margin:0">Calculadora GD: tarifa com tributos e valor da energia compensada</h2><span class="esp"></span>
      <button class="btn" id="c-reset">Restaurar padrões</button></div>
    <div class="seletor"><input type="search" id="c-busca" placeholder="Buscar distribuidora por nome comercial ou sigla (ex.: Enel SP, Eletropaulo, CPFL)" autocomplete="off">
      <div class="lista" id="c-lista"></div></div>
    <div style="margin-top:10px"><div class="nomegrande" id="c-nome"></div><div class="ficha" id="c-ficha"></div></div>
    <h3>Tarifa e enquadramento</h3>
    <div class="campos">
      <label>Data de referência<input type="date" id="c-data"></label>
      <label>Enquadramento<select id="c-enq" style="min-width:140px"><option value="1">GD I</option><option value="2">GD II</option><option value="3">GD III</option></select></label>
      <label>% do Fio B pago (Lei 14.300)<input type="text" inputmode="decimal" id="c-pfb"></label>
      <label>TUSD D (R$/MWh)<input type="text" inputmode="decimal" id="c-tusd"></label>
      <label>TE E (R$/MWh)<input type="text" inputmode="decimal" id="c-te"></label>
      <label>Fio B SCEE (R$/MWh)<input type="text" inputmode="decimal" id="c-fb"></label>
    </div>
    <h3>Tributos e repasse <span class="selo-imp" id="c-selo"></span></h3>
    <div class="campos">
      <label>ICMS (%)<input type="text" inputmode="decimal" id="c-icms"></label>
      <label>PIS (%)<input type="text" inputmode="decimal" id="c-pis"></label>
      <label>COFINS (%)<input type="text" inputmode="decimal" id="c-cof"></label>
      <label>ICMS da TE na energia compensada<select id="c-fte" style="min-width:200px"><option value="1">Sim (entra no benefício)</option><option value="0">Não</option></select></label>
      <label>ICMS da TUSD na energia compensada<select id="c-ftu" style="min-width:200px"><option value="1">Sim (entra no benefício)</option><option value="0">Não</option></select></label>
      <label>Repasse s (%)<input type="text" inputmode="decimal" id="c-s"></label>
    </div>
    <h3>Preço</h3>
    <div class="campos">
      <label>Informar<select id="c-modo" style="min-width:200px"><option value="d">Deságio (%) sobre U (tarifa sem tributos)</option><option value="p">Preço de venda (R$/MWh)</option></select></label>
      <label><span id="c-preco-rot">Deságio (%)</span><input type="text" inputmode="decimal" id="c-preco"></label>
    </div>
    <div class="nota-sim" id="c-fontes"></div>
  </section>
  <div class="grid kpis" id="c-kpis" style="margin-top:16px"></div>
  <section class="card"><h2>Memória de cálculo</h2><div class="tab" id="c-mem"></div><div class="nota-sim" id="c-nota"></div></section>
  <section class="card">
    <h2>Simulação de fatura (estimativa)</h2>
    <div class="campos">
      <label>Consumo do mês (kWh)<input type="text" inputmode="decimal" id="c-cons" value="1000"></label>
      <label>Energia compensada (kWh)<input type="text" inputmode="decimal" id="c-comp" value="900"></label>
      <label>Tipo de ligação<select id="c-lig" style="min-width:200px"><option value="30">Monofásica (30 kWh)</option><option value="50">Bifásica (50 kWh)</option><option value="100" selected>Trifásica (100 kWh)</option></select></label>
      <label>Bandeira (R$/MWh, sem tributos)<input type="text" inputmode="decimal" id="c-band" value="0"></label>
      <label>CIP / iluminação pública (R$)<input type="text" inputmode="decimal" id="c-cip" value="0"></label>
    </div>
    <div class="sim-res" id="c-fat-res"></div>
    <div class="tab" id="c-fat-tab"></div>
    <div class="avisos"><b>Estimativa, não é a fatura real.</b> Regras simplificadas: energia compensada limitada ao consumo menos o custo de disponibilidade;
      energia não compensada e bandeira pela tarifa cheia com tributos; na energia compensada a unidade paga V - W (tributos não evitados e Fio B residual);
      CIP fixa; sem multas, juros, ajustes, outros créditos, demanda ou tarifa social. Para GD III, falta o Fio A, TFSEE e P&amp;D/EE.</div>
  </section>
</div>

<!-- ============ ARRENDAMENTO ============ -->
<div class="aba" id="aba-arrendamento">
  <section class="card" style="margin-top:0">
    <div class="barra"><h2 style="margin:0">Arrendamento de usinas: pagamento ao dono e margem da Thopen</h2><span class="esp"></span>
      <a href="#calculadora" id="a-calc" class="btn" style="text-decoration:none">Abrir na Calculadora</a>
      <button class="btn" id="a-copiar">Copiar resumo</button>
      <button class="btn" id="a-reset">Restaurar padrões</button></div>
    <div class="seletor"><input type="search" id="a-busca" placeholder="Buscar distribuidora por nome comercial ou sigla (ex.: Enel SP, Eletropaulo, CPFL)" autocomplete="off">
      <div class="lista" id="a-lista"></div></div>
    <div style="margin-top:10px"><div class="nomegrande" id="a-nome"></div><div class="ficha" id="a-ficha"></div></div>
    <h3>Tarifa e enquadramento</h3>
    <div class="campos">
      <label>Data de referência<input type="date" id="a-data"></label>
      <label>Enquadramento<select id="a-enq" style="min-width:140px"><option value="1">GD I</option><option value="2">GD II</option><option value="3">GD III</option></select></label>
      <label>% do Fio B pago (Lei 14.300)<input type="text" inputmode="decimal" id="a-pfb"></label>
      <label>TUSD D (R$/MWh)<input type="text" inputmode="decimal" id="a-tusd"></label>
      <label>TE E (R$/MWh)<input type="text" inputmode="decimal" id="a-te"></label>
      <label>Fio B SCEE (R$/MWh)<input type="text" inputmode="decimal" id="a-fb"></label>
    </div>
    <h3>Tributos <span class="selo-imp" id="a-selo"></span></h3>
    <div class="campos">
      <label>ICMS (%)<input type="text" inputmode="decimal" id="a-icms"></label>
      <label>PIS (%)<input type="text" inputmode="decimal" id="a-pis"></label>
      <label>COFINS (%)<input type="text" inputmode="decimal" id="a-cof"></label>
      <label>ICMS da TE na energia compensada<select id="a-fte" style="min-width:200px"><option value="1">Sim (entra no benefício)</option><option value="0">Não</option></select></label>
      <label>ICMS da TUSD na energia compensada<select id="a-ftu" style="min-width:200px"><option value="1">Sim (entra no benefício)</option><option value="0">Não</option></select></label>
      <label>Custo do Fio B para o dono<select id="a-fbmodo" style="min-width:260px"><option value="1">Com impostos (FB_ef x k_full, como na fatura)</option><option value="0">Sem impostos (FB_ef)</option></select></label>
    </div>
    <h3>Condições comerciais</h3>
    <div class="campos">
      <label>Deságio d sobre a tarifa (%)<input type="text" inputmode="decimal" id="a-d"></label>
      <label>VB s: parte do benefício para o dono (%)<input type="text" inputmode="decimal" id="a-s"></label>
      <label>Desconto ao cliente c (% de W_I)<input type="text" inputmode="decimal" id="a-c"></label>
    </div>
    <h3>Usina e geração</h3>
    <div class="campos">
      <label>Nome da usina (opcional)<input type="text" id="a-usina" class="largo" placeholder="ex.: UFV Exemplo"></label>
      <label>Tipo<select id="a-tipo" style="min-width:200px"><option value="ufv">UFV (kWp x produtividade)</option><option value="cgh">CGH ou outra (kW x FC)</option><option value="direto">MWh/mês direto</option></select></label>
      <label class="a-ufv">Potência (kWp)<input type="text" inputmode="decimal" id="a-kwp"></label>
      <label class="a-ufv">Produtividade (kWh/kWp/ano)<input type="text" inputmode="decimal" id="a-prod"></label>
      <label class="a-ufv">Perdas / degradação (%)<input type="text" inputmode="decimal" id="a-perdas"></label>
      <label class="a-cgh">Potência (kW)<input type="text" inputmode="decimal" id="a-kw"></label>
      <label class="a-cgh">Fator de capacidade (%)<input type="text" inputmode="decimal" id="a-fc"></label>
      <label class="a-direto">Geração média (MWh/mês)<input type="text" inputmode="decimal" id="a-mwh"></label>
      <label>% da geração compensada<input type="text" inputmode="decimal" id="a-pcomp"></label>
    </div>
    <div class="nota-sim" id="a-fontes"></div>
  </section>
  <div class="grid kpis" id="a-kpis" style="margin-top:16px"></div>
  <section class="card"><h2>Resultados</h2><div class="tab" id="a-res"></div></section>
  <section class="card"><h2>Memória de cálculo (por MWh compensado)</h2><div class="tab" id="a-mem"></div><div class="nota-sim" id="a-nota"></div></section>
  <section class="card">
    <div class="barra"><h2 style="margin:0">Sensibilidade: deságio x VB</h2><span class="esp"></span>
      <select id="a-smet"><option value="dono">Pagamento ao dono (R$/mês)</option><option value="margem">Margem líquida Thopen (R$/mês)</option><option value="dimp">Deságio implícito do dono (sobre U)</option></select></div>
    <div class="tab" id="a-sens"></div>
    <div class="mut" id="a-sens-nota" style="margin-top:6px"></div>
  </section>
  <section class="card" id="n-sec">
    <div class="barra"><h2 style="margin:0">Visão de negociação</h2><span class="esp"></span>
      <button class="btn" id="n-copiar">Copiar proposta</button>
      <button class="btn" id="n-copiar-int">Copiar análise interna</button></div>
    <div class="mut">Usa a configuração acima (distribuidora, enquadramento, tributos, deságio, VB, desconto ao cliente e geração). Valores por MWh compensado, convertidos para mês e ano com a energia compensada.</div>
    <h3>Proposta ao dono (configuração atual)</h3>
    <div class="grid kpis" id="n-kpis"></div>
    <h3>Cálculo reverso</h3>
    <div class="campos">
      <label>Variável a ajustar<select id="n-var" style="min-width:260px"><option value="d">Deságio d (mantém o VB s atual)</option><option value="s">VB s (mantém o deságio d atual)</option></select></label>
      <label>Pedido do dono<input type="text" inputmode="decimal" id="n-ped"></label>
      <label>Unidade do pedido<select id="n-ped-u" style="min-width:120px"><option value="mes">R$/mês</option><option value="mwh">R$/MWh</option></select></label>
      <label>Margem mínima da Thopen (líquida de c)<input type="text" inputmode="decimal" id="n-mmin"></label>
      <label>Unidade da margem<select id="n-mmin-u" style="min-width:120px"><option value="pct">% de W_I</option><option value="mes">R$/mês</option><option value="mwh">R$/MWh</option></select></label>
    </div>
    <div class="grid dois">
      <div><b class="n-tit" id="n-ped-tit">Pedido do dono</b><div class="tab" id="n-rev-ped" style="margin-top:6px"></div></div>
      <div><b class="n-tit" id="n-mg-tit">Margem mínima da Thopen</b><div class="tab" id="n-rev-mg" style="margin-top:6px"></div></div>
    </div>
    <div class="barra" style="margin-top:14px"><h3 style="margin:0">Faixa de negociação</h3><span class="esp"></span>
      <button class="btn" id="n-faixa-reset">Recalcular faixa</button></div>
    <div class="tab" id="n-faixa"></div>
    <div class="mut" id="n-faixa-nota" style="margin-top:6px"></div>
    <h3>Projeção do contrato ano a ano</h3>
    <div class="campos">
      <label>Prazo (anos)<input type="text" inputmode="numeric" id="n-prazo"></label>
      <label>Ano de início<input type="text" inputmode="numeric" id="n-ano"></label>
      <label>Reajuste anual da tarifa (%)<input type="text" inputmode="decimal" id="n-reaj"></label>
      <label>Degradação anual da geração (%)<input type="text" inputmode="decimal" id="n-degr"></label>
      <label>Taxa de desconto anual (%)<input type="text" inputmode="decimal" id="n-taxa"></label>
      <label>Fio B pelo escalonamento da Lei 14.300<select id="n-esc" style="min-width:200px"><option value="1">Sim (60, 75, 90 e 100%)</option><option value="0">Não (% fixo informado acima)</option></select></label>
    </div>
    <div class="grid kpis" id="n-proj-kpis"></div>
    <div class="graf" style="margin-top:12px"><canvas id="g-neg"></canvas></div>
    <div class="tab" id="n-proj" style="margin-top:12px"></div>
    <div class="nota-sim" id="n-proj-nota"></div>
  </section>
  <div class="avisos"><b>Estimativa.</b> Sem bandeiras, sem CIP e sem custo de disponibilidade; sem inadimplência, O&amp;M, seguros ou tributos sobre a receita do arrendamento.
    Para GD III, pela Lei 14.300 (art. 27, par. 1o) a usina paga 100% do Fio B, 40% do Fio A, TFSEE e P&amp;D/EE: só o % do Fio B é modelado (editável).</div>
</div>

<!-- ============ TABELA THOPEN ============ -->
<div class="aba" id="aba-thopen">
  <section class="card" style="margin-top:0">
    <div class="barra"><h2 style="margin:0" id="t-tit">Tabela Thopen</h2><span class="esp"></span>
      <span class="grupo"><span class="mut">Enquadramento</span>
        <button class="btn ativo" data-tenq="1">GD I (planilha)</button><button class="btn" data-tenq="2">GD II</button></span></div>
    <div class="barra"><input type="search" class="filtro" id="t-busca" placeholder="Filtrar por nome, sigla, UF ou grupo">
      <label class="mut"><input type="checkbox" id="t-ativas" checked> Só ativas</label>
      <label class="mut"><input type="checkbox" id="t-plan"> Só as da planilha</label></div>
    <div class="tab" id="t-tab" style="max-height:680px"></div>
    <div class="mut" id="t-nota" style="margin-top:6px"></div>
  </section>
</div>

<!-- ============ GRUPO A ============ -->
<div class="aba" id="aba-grupoa">
  <section class="card" style="margin-top:0">
    <div class="barra"><h2 style="margin:0" id="ga-tit">Grupo A</h2><span class="esp"></span>
      <span class="grupo"><span class="mut">Subgrupo</span><span id="ga-subs"></span></span>
      <span class="grupo"><span class="mut">Modalidade</span><span id="ga-mods"></span></span></div>
    <div class="mut" id="ga-nota"></div>
  </section>
  <div id="ga-corpo">
  <div class="grid kpis" id="ga-kpis" style="margin-top:16px"></div>
  <section class="card">
    <div class="barra"><h2 style="margin:0" id="ga-mat-tit"></h2><span class="esp"></span><span id="ga-mets"></span></div>
    <div class="barra"><input type="search" class="filtro" id="ga-mat-busca" placeholder="Filtrar por nome, sigla, UF ou grupo">
      <label class="mut"><input type="checkbox" id="ga-mat-sel"> Somente as do comparador</label></div>
    <div class="tab" id="ga-mat"></div>
    <div class="legenda"><span>Variação contra o ano anterior:</span><span>queda</span><span class="escala"></span><span>alta</span>
      <span>(cores saturam em ±15%; passe o mouse para ver a variação)</span></div>
  </section>
  <section class="grid dois">
    <div class="card"><h2 id="ga-hist-tit">Histórico</h2><div class="graf"><canvas id="g-ga-hist"></canvas></div>
      <div class="mut">Distribuidoras escolhidas no Comparador (até 4). Cada degrau é uma vigência; a métrica é a mesma da matriz.</div></div>
    <div class="card"><h2>Relação ponta / fora ponta (energia, R$/MWh)</h2><div class="graf"><canvas id="g-ga-rel"></canvas></div>
      <div class="mut" id="ga-rel-nota"></div></div>
  </section>
  <section class="card">
    <div class="barra"><h2 style="margin:0" id="ga-rank-tit"></h2><span class="esp"></span><select id="ga-rank-met"></select>
      <button class="btn ativo" data-garord="desc">Maiores</button><button class="btn" data-garord="asc">Menores</button></div>
    <div class="graf alto" id="ga-rank-caixa"><canvas id="g-ga-rank"></canvas></div>
    <div class="mut" id="ga-rank-nota" style="margin:6px 0 10px"></div>
    <div class="tab" id="ga-rank-tab" style="max-height:420px"></div>
  </section>
  <section class="card">
    <h2 id="ga-sim-tit">Simulador Verde x Azul</h2>
    <div class="campos">
      <label>Distribuidora<select id="ga-sim-dist"></select></label>
      <label>Demanda contratada (kW)<input type="number" id="ga-sim-dem" min="0" step="10" value="500"></label>
      <label>Consumo mensal (MWh)<input type="number" id="ga-sim-cons" min="0" step="1" value="150"></label>
      <label>Consumo na ponta (%)<input type="number" id="ga-sim-pp" min="0" max="100" step="0.5" value="10"></label>
    </div>
    <div class="sim-res" id="ga-sim-res"></div>
    <div class="tab" id="ga-sim-tab" style="max-height:420px"></div>
    <div class="nota-sim" id="ga-sim-nota"></div>
  </section>
  <section class="grid dois">
    <div class="card"><div class="barra"><h2 style="margin:0" id="ga-vc-tit">Valor do kWh compensado</h2><span class="esp"></span>
      <button class="btn ativo" data-gavc="efp">Fora ponta</button><button class="btn" data-gavc="ep">Ponta</button></div>
      <div class="graf alto"><canvas id="g-ga-vc"></canvas></div><div class="mut" id="ga-vc-nota"></div></div>
    <div class="card"><h2 id="ga-fb-tit">TUSD Fio B do Grupo A</h2>
      <div class="barra"><input type="search" class="filtro" id="ga-fb-busca" placeholder="Filtrar por nome, sigla, UF ou grupo"></div>
      <div class="tab" id="ga-fb" style="max-height:560px"></div><div class="mut" id="ga-fb-nota" style="margin-top:6px"></div></div>
  </section>
  </div>
</div>

<!-- ============ RANKINGS ============ -->
<div class="aba" id="aba-rankings">
  <section class="card" style="margin-top:0">
    <div class="barra"><h2 style="margin:0" id="rank-tit"></h2></div>
    <div class="barra" id="rank-met"></div>
    <div class="barra"><button class="btn ativo" data-rord="desc">Maiores</button><button class="btn" data-rord="asc">Menores</button>
      <span class="esp"></span><label class="mut"><input type="checkbox" id="rank-todas"> Mostrar todas no gráfico</label></div>
    <div class="graf alto" id="rank-caixa"><canvas id="g-rank"></canvas></div>
    <div class="mut" id="rank-nota" style="margin:6px 0 10px"></div>
    <div class="tab" id="rank-tab" style="max-height:420px"></div>
  </section>
  <section class="card">
    <div class="barra"><h2 style="margin:0" id="mat-tit"></h2><span class="esp"></span>
      <button class="btn ativo" data-mat="b3">B3 total</button><button class="btn" data-mat="scee">Fio B SCEE</button>
      <button class="btn" data-mat="efetivo">Fio B efetivo</button></div>
    <div class="barra"><input type="search" class="filtro" id="mat-busca" placeholder="Filtrar por nome, sigla, UF ou grupo">
      <label class="mut"><input type="checkbox" id="mat-sel"> Somente as do comparador</label></div>
    <div class="tab" id="mat"></div>
    <div class="legenda"><span>Variação contra o ano anterior:</span><span>queda</span><span class="escala"></span><span>alta</span>
      <span>(cores saturam em ±15%; passe o mouse para ver a variação)</span></div>
  </section>
</div>

<!-- ============ REGIOES ============ -->
<div class="aba" id="aba-regioes">
  <section class="grid dois" style="margin-top:0">
    <div class="card"><h2>Medianas por região (R$/MWh)</h2><div class="graf"><canvas id="g-reg"></canvas></div></div>
    <div class="card"><div class="barra"><h2 style="margin:0" id="uf-tit">Mediana por UF</h2><span class="esp"></span>
      <select id="uf-met"></select></div><div class="graf"><canvas id="g-uf"></canvas></div><div class="legenda" id="uf-leg"></div></div>
  </section>
  <section class="card"><h2>Mapa de calor por UF (medianas das distribuidoras ativas)</h2><div class="tab" id="uf-tab"></div>
    <div class="mut" style="margin-top:6px">Cor por coluna: mais escuro = maior valor. Passe o mouse sobre a UF para ver as distribuidoras.</div></section>
</div>

<!-- ============ CALENDARIO ============ -->
<div class="aba" id="aba-calendario">
  <section class="card" style="margin-top:0"><h2 id="cal-tit">Próximos reajustes</h2><div class="tab" id="cal"></div>
    <div class="mut" style="margin-top:6px" id="cal-nota"></div></section>
</div>

<!-- ============ MUDANCAS ============ -->
<div class="aba" id="aba-mudancas">
  <section class="card" style="margin-top:0"><h2>Últimas mudanças detectadas pelo agente</h2><div class="tab" id="log" style="max-height:640px"></div></section>
</div>

<!-- ============ METODOLOGIA ============ -->
<div class="aba" id="aba-metodologia">
  <section class="card metodo" style="margin-top:0"><h2>Metodologia</h2><div id="metodo"></div></section>
</div>
</main>

<script type="application/json" id="dados">__DADOS__</script>
<script src="__CHARTJS__"></script>
<script>
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("dados").textContent);
  var M = D.m, I = D.dists;
  var f2 = new Intl.NumberFormat("pt-BR", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  var f1 = new Intl.NumberFormat("pt-BR", {minimumFractionDigits: 1, maximumFractionDigits: 1});
  var f0 = new Intl.NumberFormat("pt-BR", {maximumFractionDigits: 0});
  var CORES = ["#1F3864", "#E07B39", "#2E9E6B", "#C0392B", "#7B5EA7", "#1B9AAA", "#B8860B", "#D35D90", "#5C6B7A", "#6E9B3A"];
  var COR_REG = {"Norte": "#2E9E6B", "Nordeste": "#E07B39", "Centro-Oeste": "#B8860B", "Sudeste": "#1F3864", "Sul": "#1B9AAA"};

  function num(v) { return v == null ? "-" : f2.format(v); }
  function pct(v) { return v == null ? "-" : (v > 0 ? "+" : "") + f1.format(v * 100) + "%"; }
  function pctS(v) { return v == null ? "-" : f1.format(v * 100) + "%"; }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); }
  function $(id) { return document.getElementById(id); }
  function ehPort(d) { return D.portfolio.indexOf(d) >= 0; }
  function rot(d) { return (ehPort(d) ? "★ " : "") + d; }
  function cls(v) { return v == null ? "" : (v > 0 ? "pos" : (v < 0 ? "neg" : "")); }
  function dataBr(s) { return s && s.length >= 10 ? s.slice(8, 10) + "/" + s.slice(5, 7) + "/" + s.slice(0, 4) : (s || ""); }
  function mesBr(s) { return s ? s.slice(5, 7) + "/" + s.slice(0, 4) : "?"; }
  function ms(s) { return Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)); }
  function mesAno(v) { var d = new Date(v); return ("0" + (d.getUTCMonth() + 1)).slice(-2) + "/" + d.getUTCFullYear(); }
  function varAt(arr, j) { var a = arr[j], b = j > 0 ? arr[j - 1] : null; return (a != null && b) ? a / b - 1 : null; }
  function mediana(a) { a = a.filter(function (x) { return x != null; }).sort(function (x, y) { return x - y; });
    if (!a.length) return null; var k = Math.floor(a.length / 2); return a.length % 2 ? a[k] : (a[k - 1] + a[k]) / 2; }
  function norm(s) { return String(s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toUpperCase().replace(/[^A-Z0-9]+/g, " ").trim(); }
  // Busca: cada termo precisa aparecer; termos de 1 ou 2 letras (ex.: UF) so como palavra inteira
  function casa(d, q) {
    var b = " " + ((I[d] || {}).b || norm(d)) + " ";
    return norm(q).split(" ").every(function (t) { return !t || (t.length <= 2 ? b.indexOf(" " + t + " ") >= 0 : b.indexOf(t) >= 0); });
  }
  function det(d) { var i = I[d] || {}; return [i.uf, i.g].filter(Boolean).join(" · ") + (i.a === false ? " · inativa" : ""); }
  function tabela(cab, linhas, vazio, cl) {
    if (!linhas.length) return '<div class="erro">' + esc(vazio) + "</div>";
    return "<table" + (cl ? ' class="' + cl + '"' : "") + "><thead><tr>" + cab.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") +
      "</tr></thead><tbody>" + linhas.join("") + "</tbody></table>";
  }
  function td(v, c) { return '<td class="n' + (c ? " " + c : "") + '">' + v + "</td>"; }

  var sel = D.padrao.slice(0, D.meta.max_sel);
  var distAtual = D.padrao_dist;
  function cor(d) { var i = sel.indexOf(d); return CORES[(i < 0 ? 0 : i) % CORES.length]; }
  var A = D.meta.ano, A0 = D.meta.fiob_ano_inicial, AC = D.meta.anos_acum, K = D.kpis;

  // ---- cabecalho, avisos, metodologia
  $("m-verif").textContent = D.meta.ultima_verificacao;
  $("m-gerado").textContent = D.meta.gerado_em;
  $("tl-fiob-tit").textContent = "TUSD Fio B, detalhe SCEE (vigências desde " + A0 + ")";
  $("fiob-tit").textContent = "Fio B SCEE (R$/MWh) por vigência, desde " + A0;
  if (D.avisos.length) {
    $("avisos").innerHTML = '<div class="avisos"><b>Qualidade dos dados</b><ul>' +
      D.avisos.map(function (a) { return "<li>" + esc(a) + "</li>"; }).join("") + "</ul></div>";
  }
  $("metodo").innerHTML = D.metodologia.filter(function (t) { return t; })
    .map(function (t) { return "<p>" + esc(t) + "</p>"; }).join("");

  // ---- graficos
  var temChart = typeof window.Chart !== "undefined";
  var graficos = {};
  if (temChart) { Chart.defaults.font.family = '"Segoe UI",Roboto,Helvetica,Arial,sans-serif'; Chart.defaults.color = "#5B6577"; }
  function semChart(id) { var c = $(id); if (c) c.parentNode.innerHTML = '<div class="erro">Gráfico indisponível: não foi possível carregar o Chart.js (sem internet?).</div>'; }
  function grafico(id, cfg) {
    if (!temChart) { semChart(id); return; }
    if (graficos[id]) { graficos[id].destroy(); }
    graficos[id] = new Chart($(id), cfg);
  }
  function eixoY(fmt) { return {ticks: {callback: function (v) { return fmt ? fmt(v) : f0.format(v); }}, grid: {color: "#EDF0F5"}}; }
  function opcoesLinha() {
    return {responsive: true, maintainAspectRatio: false, locale: "pt-BR", interaction: {mode: "nearest", intersect: false},
      plugins: {legend: {position: "bottom", labels: {boxWidth: 18}},
        tooltip: {callbacks: {label: function (c) { return c.dataset.label + ": " + num(c.parsed.y); }}}},
      scales: {y: eixoY(), x: {grid: {display: false}}}};
  }
  function opcoesBarraH(fmt, empilhado) {
    return {indexAxis: "y", responsive: true, maintainAspectRatio: false, locale: "pt-BR",
      plugins: {legend: {display: !!empilhado, position: "bottom", labels: {boxWidth: 14}},
        tooltip: {callbacks: {label: function (c) { return (empilhado ? c.dataset.label + ": " : "") + fmt(c.parsed.x); }}}},
      scales: {x: {stacked: !!empilhado, ticks: {callback: function (v) { return fmt(v); }}, grid: {color: "#EDF0F5"}},
        y: {stacked: !!empilhado, grid: {display: false}, ticks: {autoSkip: false}}}};
  }

  // ---- seletor generico (busca por nome, sigla, UF, regiao, grupo)
  function seletor(inputId, listaId, multi, aoEscolher, marcados) {
    var busca = $(inputId), lista = $(listaId);
    function render() {
      var q = busca.value.trim();
      var itens = D.lista.filter(function (d) { return !q || casa(d, q); });
      itens.sort(function (x, y) { var ax = (I[x] || {}).a ? 0 : 1, ay = (I[y] || {}).a ? 0 : 1; return ax - ay || x.localeCompare(y, "pt-BR"); });
      lista.innerHTML = itens.length ? itens.map(function (d) {
        var m = marcados().indexOf(d) >= 0;
        return multi ? '<label><input type="checkbox" data-d="' + esc(d) + '"' + (m ? " checked" : "") + "> <span>" + esc(rot(d)) +
          '</span><span class="det">' + esc(det(d)) + "</span></label>"
          : '<div class="op" data-d="' + esc(d) + '"><span>' + esc(rot(d)) + '</span><span class="det">' + esc(det(d)) + "</span></div>";
      }).join("") : '<div class="mut" style="padding:8px 10px">Nenhuma distribuidora encontrada</div>';
      lista.style.display = "block";
    }
    busca.addEventListener("focus", render);
    busca.addEventListener("input", render);
    lista.addEventListener(multi ? "change" : "click", function (e) {
      var el = e.target.closest("[data-d]"); if (!el) return;
      aoEscolher(el.getAttribute("data-d"), multi ? e.target.checked : true, el);
      if (multi) render(); else { lista.style.display = "none"; busca.value = ""; busca.blur(); }
    });
    document.addEventListener("click", function (e) { if (!e.target.closest || !lista.parentNode.contains(e.target)) lista.style.display = "none"; });
    return render;
  }

  // ---- abas
  var feitos = {}, abaAtual = "geral";
  var RENDER = {
    geral: renderGeral, distribuidora: renderDist, comparador: renderComp, calculadora: renderCalc, arrendamento: renderArr, thopen: renderThopen, grupoa: renderGA, rankings: function () { renderRank(); renderMatriz(); },
    regioes: renderRegioes, calendario: renderCal, mudancas: renderLog, metodologia: function () {}
  };
  function mostrar(aba) {
    if (!RENDER[aba]) aba = "geral";
    abaAtual = aba;
    document.querySelectorAll("#abas button").forEach(function (b) { b.classList.toggle("ativo", b.getAttribute("data-aba") === aba); });
    document.querySelectorAll(".aba").forEach(function (s) { s.classList.toggle("ativo", s.id === "aba-" + aba); });
    if (!feitos[aba]) { feitos[aba] = true; RENDER[aba](); }
    if (location.hash !== "#" + aba) history.replaceState(null, "", "#" + aba);
  }
  $("abas").addEventListener("click", function (e) { var b = e.target.closest("button[data-aba]"); if (b) mostrar(b.getAttribute("data-aba")); });
  document.querySelectorAll("[data-ir]").forEach(function (b) { b.addEventListener("click", function () { mostrar(b.getAttribute("data-ir")); }); });
  function sujar(abas) { abas.forEach(function (a) { if (a === abaAtual) RENDER[a](); else feitos[a] = false; }); }

  // ================= VISAO GERAL
  function renderGeral() {
    var semSus = K.n_suspeitos ? ", sem " + K.n_suspeitos + " valor(es) suspeito(s)" : "";
    var a16 = AC[0], p16 = (D.ipca.per[a16] || {});
    var kp = [
      ["Distribuidoras", f0.format(K.n_dist), K.n_ativas + " ativas; " + K.n_dist_ano + " com tarifa B3 em " + A],
      ["Vigência mais recente", K.vig_data, rot(K.vig_dist) + " (" + K.vig_reh + ")"],
      ["B3 total atual (mediana)", num(D.med.br.b3), "R$/MWh, " + D.med.br.n + " distribuidoras ativas; TUSD " + num(D.med.br.tusd) + ", TE " + num(D.med.br.te)],
      ["Valor do kWh compensado (mediana)", num(K.vc_med), "R$/MWh, " + pctS(K.pvc_med) + " da B3 total; " + K.n_vc + " distribuidoras (GD II)"],
      ["Maior valor do kWh compensado", K.vc_max ? num(K.vc_max[1]) : "-", K.vc_max ? rot(K.vc_max[0]) + ", " + pctS(K.vc_max[2]) + " da tarifa" : ""],
      ["Menor valor do kWh compensado", K.vc_min ? num(K.vc_min[1]) : "-", K.vc_min ? rot(K.vc_min[0]) + ", " + pctS(K.vc_min[2]) + " da tarifa" : ""],
      ["B3 acumulada desde " + a16 + " (mediana)", pct(K.acum_med), "IPCA no período " + pct(K.ipca_acum) + " (" + mesBr(p16.de) + " a " + mesBr(p16.ate) + "); real " + pct(K.real_med)],
      ["Mediana var. B3 total " + (A - 1) + " a " + A, pct(K.med_var_b3), K.n_var_b3 + " distribuidoras com os dois anos"],
      ["Mediana Fio B SCEE " + A, num(K.med_fiob), "R$/MWh, " + K.n_fiob + " distribuidoras" + semSus],
      ["Fio B efetivo médio " + A, num(K.media_efetivo), "R$/MWh, " + (K.esc == null ? "sem escalonamento" : f0.format(K.esc * 100) + "% da Lei 14.300") + semSus],
      ["Mediana var. Fio B SCEE", pct(K.med_var_fiob), "vigência mais recente contra a anterior, desde " + A0 + "; " + K.n_var_fiob + " distribuidoras"],
      ["Reajustes nos próximos " + D.meta.dias_cal + " dias", f0.format(K.n_cal), K.n_cal_pub + " já com nova homologação publicada"]
    ];
    $("kpis").innerHTML = kp.map(function (k) {
      return '<div class="card kpi"><div class="rot">' + esc(k[0]) + '</div><div class="val">' + esc(k[1]) +
        '</div><div class="sub">' + esc(k[2]) + "</div></div>"; }).join("");
    // indice B3 x IPCA
    var X = D.indice;
    $("ind-tit").textContent = "Mediana nacional da B3 total x IPCA (índice, 01/01/" + X.base + " = 100)";
    $("ind-nota").textContent = "B3: mediana do índice de cada distribuidora (valor vigente em 01/01 de cada ano e hoje, contra 01/01/" + X.base +
      "), " + X.n + " distribuidoras. IPCA: Banco Central, até " + mesBr(D.ipca.ult) + ".";
    var op = opcoesLinha();
    op.plugins.tooltip.callbacks.label = function (c) { return c.dataset.label + ": " + f1.format(c.parsed.y); };
    grafico("g-indice", {type: "line", data: {labels: X.rotulos, datasets: [
      {label: "B3 total (mediana nacional)", data: X.b3, borderColor: "#1F3864", backgroundColor: "#1F3864", tension: 0.2, pointRadius: 2.5, borderWidth: 2, spanGaps: true},
      {label: "IPCA", data: X.ipca, borderColor: "#E07B39", backgroundColor: "#E07B39", borderDash: [6, 4], tension: 0.2, pointRadius: 2, borderWidth: 2, spanGaps: true}]},
      options: op});
    $("geral-cal").innerHTML = tabelaCal(D.cal.slice(0, 12), true);
    // valor compensado: 10 maiores e 10 menores
    var v = D.lista.filter(function (d) { return M[d] && M[d].vc != null && I[d].a; })
      .sort(function (x, y) { return M[y].vc - M[x].vc; });
    var r = v.length > 20 ? v.slice(0, 10).concat(v.slice(-10)) : v;
    grafico("g-geral-vc", {type: "bar", data: {labels: r.map(rot), datasets: [
      {label: "Valor do kWh compensado", data: r.map(function (d) { return M[d].vc; }), backgroundColor: r.map(function (d, j) { return j < 10 ? "#2E7D4F" : "#C0392B"; }), borderRadius: 3},
      {label: "Fio B efetivo (pago)", data: r.map(function (d) { return M[d].fbe; }), backgroundColor: "#C9D1DE", borderRadius: 3}]},
      options: opcoesBarraH(num, true)});
  }

  function tabelaCal(linhas, curta) {
    var cab = curta ? ["Data prevista", "Distribuidora", "UF", "B3 atual", "Situação"]
      : ["Data prevista", "Distribuidora", "UF", "Vigência atual", "B3 atual", "Fio B SCEE", "Valor kWh compensado", "Situação", "Nova vigência", "Novo total", "Var."];
    return tabela(cab, linhas.map(function (c) {
      var d = c[0], tag = c[8] ? '<span class="tag ok">' + esc(c[1]) + "</span>" :
        (c[1].indexOf("vencida") >= 0 ? '<span class="tag alerta">' + esc(c[1]) + "</span>" : '<span class="tag esp">' + esc(c[1]) + "</span>");
      var nome = '<td class="nome">' + esc(rot(d)) + "</td><td>" + esc((I[d] || {}).uf || "") + "</td>";
      if (curta) return "<tr><td>" + dataBr(c[2]) + "</td>" + nome + td(num(c[5])) + "<td>" + tag + "</td></tr>";
      return "<tr><td>" + dataBr(c[2]) + "</td>" + nome + "<td>" + dataBr(c[3]) + " a " + dataBr(c[4]) + "</td>" + td("<b>" + num(c[5]) + "</b>") +
        td(num(c[6])) + td(num(c[7])) + "<td>" + tag + "</td><td>" + (c[8] ? dataBr(c[2]) + " (" + esc(c[9]) + ")" : "-") + "</td>" +
        td(num(c[10])) + td(pct(c[11]), cls(c[11])) + "</tr>";
    }), "Nenhuma vigência termina nos próximos " + D.meta.dias_cal + " dias.");
  }

  // ================= DISTRIBUIDORA
  var renderListaDist = seletor("d-busca", "d-lista", false, function (d) { distAtual = d; renderDist(); }, function () { return [distAtual]; });
  function valorEm(b3, data) {  // b3: vigencias do mais recente para o mais antigo
    for (var k = 0; k < b3.length; k++) { if (b3[k][0] <= data) return (!b3[k][1] || b3[k][1] >= data) ? b3[k][5] : null; }
    return null;
  }
  function difMed(v, m) { return (v == null || m == null || !m) ? "" : " (" + pct(v / m - 1) + ")"; }
  function posTxt(p) { return p ? p[0] + "ª de " + p[1] : "-"; }
  function renderDist() {
    var d = distAtual, i = I[d] || {}, m = M[d], t = D.tl[d] || {b3: [], fiob: []};
    $("d-nome").textContent = rot(d);
    $("d-ficha").innerHTML = [["Sigla ANEEL", i.s], ["Nomes anteriores", i.h], ["UF", i.uf + (i.ufs && i.ufs !== i.uf ? " (atende " + i.ufs + ")" : "")], ["Região", i.r],
      ["Grupo", i.g], ["Razão social", i.rz], ["CNPJ", i.cnpj], ["Situação", i.a ? "ativa" : "inativa (sem vigência recente)"]]
      .filter(function (x) { return x[1]; }).map(function (x) { return "<span>" + esc(x[0]) + ": <b>" + esc(x[1]) + "</b></span>"; }).join("");
    $("d-ga").innerHTML = tabela(["Subgrupo e modalidade", "Vigência atual", "Energia FP", "Energia P", "Demanda (Azul: P / FP)",
      "Último reajuste (energia FP)", "Fio B demanda (Azul: P / FP)", "Valor kWh compensado FP"], GA.pares.map(function (k) {
      var a = GA.s[k].atual[d]; if (!a) return ""; var az = GA.s[k].mod === "Azul";
      return '<tr><td class="nome">' + esc(k) + "</td><td>" + dataBr(a.ini) + " a " + dataBr(a.fim) + " (" + esc(a.reh) + ")</td>" +
        td("<b>" + num(a.efp) + "</b>") + td(num(a.ep)) + td(num(a.d) + (az ? " / " + num(a.dfp) : "")) +
        td(pct(a.vu_efp) + (a.ur ? ' <span class="mut">(' + dataBr(a.ur) + ")</span>" : ""), cls(a.vu_efp)) +
        td(num(a.fb_d) + (az ? " / " + num(a.fb_dfp) : "")) + td(num(a.vc_efp)) + "</tr>";
    }).filter(Boolean), "Sem tarifa do Grupo A para esta distribuidora.");
    if (!m) { $("d-kpis").innerHTML = '<div class="card erro">Sem tarifa B3 para esta distribuidora.</div>'; return; }
    var escTxt = m.esc == null ? "" : f0.format(m.esc * 100) + "% da Lei 14.300";
    var v1 = txCalcM(m, false), v2 = txCalcM(m, true);
    var kp = [
      ["B3 total atual", num(m.tot), "R$/MWh, vigência " + dataBr(m.ini) + " a " + dataBr(m.fim) + " (" + m.reh + ")"],
      ["TUSD | TE", num(m.tusd) + " | " + num(m.te), "R$/MWh; TUSD = " + pctS(m.tot ? m.tusd / m.tot : null) + " da B3"],
      ["Fio B SCEE", num(m.fb), m.sus ? "valor suspeito, fora das medianas" : "R$/MWh; " + pctS(m.fbt) + " da TUSD; efetivo " + num(m.fbe) + " (" + escTxt + ")"],
      ["Valor do kWh compensado", num(m.vc), m.vc == null ? "sem Fio B SCEE válido" : "R$/MWh (GD II), " + pctS(m.pvc) + " da B3 total"],
      ["Tarifa B3 com tributos (V)", num(v1 && v1.V), m.tx ? "R$/MWh; ICMS " + pct2(m.tx.i) + " e PIS+COFINS " + pct2(m.tx.pis + m.tx.cof) + " (" + fonteCurta(m.tx.fi) + ")" : "sem alíquotas"],
      ["Valor compensado com tributos (W)", num(v2 && v2.W), m.tx ? "R$/MWh, GD II; GD I " + num(v1 && v1.W) + "; ICMS na compensada: TE " + sn(m.tx.te) + ", TUSD " + sn(m.tx.tu) + " (" + m.tx.ff + ")" : ""],
      ["Posição B3 total", m.rk ? m.rk[0] + "ª mais cara" : "-", m.rk ? "de " + m.rk[1] + " distribuidoras ativas" : "distribuidora inativa"],
      ["Último reajuste", pct(m.vu), m.ur ? "em " + dataBr(m.ur) + "; de " + num(m.ant) + " para " + num(m.tot) + "; IPCA " + pct(m.iu) + ", real " + pct(m.ru) : "sem reajuste anterior na base"],
      ["Próximo reajuste", m.px ? dataBr(m.px) : "-", m.pp ? "nova homologação publicada: " + num(m.pt) + " (" + pct(m.pv) + ", " + m.preh + ")" : "previsto (dia seguinte ao fim da vigência atual)"]
    ];
    $("d-kpis").innerHTML = kp.map(function (k) {
      return '<div class="card kpi"><div class="rot">' + esc(k[0]) + '</div><div class="val">' + esc(k[1]) + '</div><div class="sub">' + esc(k[2]) + "</div></div>"; }).join("");
    // posicao relativa
    var br = D.med.br, rg = D.med.reg[i.r] || {}, uf = D.med.uf[i.uf] || {};
    function linhaPos(nome, v, k, p) {
      return '<tr><td class="nome">' + esc(nome) + "</td>" + td("<b>" + num(v) + "</b>") + td(num(br[k]) + difMed(v, br[k])) +
        td(num(rg[k]) + difMed(v, rg[k])) + td(num(uf[k]) + difMed(v, uf[k])) + td(posTxt(p)) + "</tr>";
    }
    $("d-pos").innerHTML = tabela(["Indicador", "Distribuidora", "Mediana Brasil (" + (br.n || 0) + ")", "Mediana " + (i.r || "região") + " (" + (rg.n || 0) + ")",
      "Mediana " + (i.uf || "UF") + " (" + (uf.n || 0) + ")", "Posição"], [
      linhaPos("B3 total", m.tot, "b3", m.rk), linhaPos("Fio B SCEE", m.sus ? null : m.fb, "fiob", m.rf),
      linhaPos("Fio B efetivo", m.sus ? null : m.fbe, "fiob_efetivo", m.rf), linhaPos("Valor kWh compensado", m.vc, "valor_comp", m.rc)], "");
    // composicao: distribuidora x medianas
    var lab = [d, "Mediana " + (i.uf || "UF"), "Mediana Brasil"], fontes = [{tusd: m.tusd, te: m.te, fb: m.sus ? null : m.fb},
      {tusd: uf.tusd, te: uf.te, fb: uf.fiob}, {tusd: br.tusd, te: br.te, fb: br.fiob}];
    grafico("g-d-comp", {type: "bar", data: {labels: lab, datasets: dsComposicao(fontes)}, options: opcoesComp()});
    $("d-comp-nota").textContent = "Medianas calculadas componente a componente (a soma pode diferir da mediana da B3 total).";
    // acumulado x IPCA
    var linhas = AC.map(function (a) { var x = m.acum[a] || [], p = D.ipca.per[a] || {};
      return '<tr><td class="nome">Desde 01/01/' + a + "</td>" + td(num(x[0])) + td(num(m.tot)) + td(pct(x[1]), cls(x[1])) + td(pct(x[2])) +
        td(pct(x[3]) + ' <span class="mut">(' + mesBr(p.de) + " a " + mesBr(p.ate) + ")</span>") + td("<b>" + pct(x[4]) + "</b>", cls(x[4])) + "</tr>"; });
    var p12 = D.ipca.per["12m"] || {};
    linhas.push('<tr><td class="nome">12 meses</td>' + td(num(m.m12[0])) + td(num(m.tot)) + td(pct(m.m12[1]), cls(m.m12[1])) + td("-") +
      td(pct(m.m12[2]) + ' <span class="mut">(' + mesBr(p12.de) + " a " + mesBr(p12.ate) + ")</span>") + td("<b>" + pct(m.m12[3]) + "</b>", cls(m.m12[3])) + "</tr>");
    linhas.push('<tr><td class="nome">Último reajuste' + (m.ur ? " (" + dataBr(m.ur) + ")" : "") + "</td>" + td(num(m.ant)) + td(num(m.tot)) + td(pct(m.vu), cls(m.vu)) + td("-") +
      td(pct(m.iu) + (m.iud && m.iud[0] ? ' <span class="mut">(' + mesBr(m.iud[0]) + " a " + mesBr(m.iud[1]) + ")</span>" : "")) + td("<b>" + pct(m.ru) + "</b>", cls(m.ru)) + "</tr>");
    $("d-acum").innerHTML = tabela(["Período", "B3 inicial", "B3 atual", "Acumulado", "CAGR", "IPCA", "Aumento real"], linhas, "");
    $("d-acum-nota").textContent = "Aumento real = (1 + B3) / (1 + IPCA) - 1. IPCA até " + mesBr(D.ipca.ult) + " (Banco Central, série 433).";
    // B3 x B3 corrigida pelo IPCA desde a base
    var base = AC[0], ini = base + "-01-01", v0 = valorEm(t.b3, ini);
    $("d-ipca-tit").textContent = "B3 total x B3 de 01/01/" + base + " corrigida pelo IPCA (R$/MWh)";
    var b3 = t.b3.slice().reverse(), pts = [];
    b3.forEach(function (r, j) {
      var fim = r[1] || D.meta.hoje;
      if (fim < ini || r[5] == null) return;
      pts.push({x: ms(r[0] < ini ? ini : r[0]), y: r[5]});
      if (j === b3.length - 1) pts.push({x: ms(fim < D.meta.hoje ? fim : D.meta.hoje), y: r[5]});
    });
    var corr = [], meses = D.ipca.meses, idx = D.ipca.idx, k0 = meses.indexOf((base - 1) + "-12");
    if (v0 != null && k0 >= 0) {
      corr.push({x: ms(ini), y: v0});
      for (var k = k0 + 1; k < meses.length; k++) {
        var mm = meses[k], prox = (+mm.slice(5, 7) === 12) ? (+mm.slice(0, 4) + 1) + "-01-01" : mm.slice(0, 5) + ("0" + (+mm.slice(5, 7) + 1)).slice(-2) + "-01";
        corr.push({x: ms(prox), y: v0 * idx[k] / idx[k0]});
      }
    }
    var op = opcoesLinha();
    op.scales.x = {type: "linear", grid: {display: false}, min: ms(ini), ticks: {maxTicksLimit: 9, callback: function (v) { return mesAno(v); }}};
    op.plugins.tooltip.callbacks.title = function (it) { return it.length ? dataBr(new Date(it[0].parsed.x).toISOString()) : ""; };
    grafico("g-d-ipca", {type: "line", data: {datasets: [
      {label: "B3 total homologada", data: pts, stepped: "before", borderColor: "#1F3864", backgroundColor: "#1F3864", pointRadius: 2, borderWidth: 2},
      {label: "B3 de 01/01/" + base + " corrigida pelo IPCA", data: corr, borderColor: "#E07B39", backgroundColor: "#E07B39", borderDash: [6, 4], pointRadius: 0, borderWidth: 2}]},
      options: op});
    // vigencias
    $("tl-b3").innerHTML = tabela(["Início", "Fim", "Resolução", "TUSD", "TE", "Total", "Var."], t.b3.map(function (r) {
      return "<tr><td>" + dataBr(r[0]) + "</td><td>" + dataBr(r[1]) + "</td><td>" + esc(r[2]) + "</td>" + td(num(r[3])) + td(num(r[4])) +
        td("<b>" + num(r[5]) + "</b>") + td(pct(r[6]), cls(r[6])) + "</tr>"; }), "Sem vigências de B3.");
    $("tl-fiob").innerHTML = tabela(["Início", "Fim", "Resolução", "Fio B", "Var.", "% Lei 14.300", "Efetivo"], t.fiob.map(function (r) {
      return "<tr" + (r[7] ? ' title="Valor suspeito: fora do ranking e das medianas" style="background:#FFF8E6"' : "") + "><td>" +
        dataBr(r[0]) + "</td><td>" + dataBr(r[1]) + "</td><td>" + esc(r[2]) + "</td>" + td("<b>" + num(r[3]) + (r[7] ? " (suspeito)" : "") + "</b>") +
        td(pct(r[4]), cls(r[4])) + td(r[5] == null ? "-" : f0.format(r[5] * 100) + "%") + td(num(r[6])) + "</tr>"; }),
      "Sem vigências de Fio B SCEE desde " + A0 + ".");
  }

  // ================= COMPARADOR
  function dsComposicao(fontes) {
    function v(f, k) { return f[k] == null ? null : f[k]; }
    return [
      {label: "Fio B (SCEE)", data: fontes.map(function (f) { return v(f, "fb"); }), backgroundColor: "#C0392B", stack: "s"},
      {label: "TUSD sem Fio B", data: fontes.map(function (f) { return f.tusd == null ? null : f.tusd - (f.fb || 0); }), backgroundColor: "#2F5597", stack: "s"},
      {label: "TE", data: fontes.map(function (f) { return v(f, "te"); }), backgroundColor: "#9DB3D9", stack: "s"}];
  }
  function opcoesComp() {
    var o = opcoesBarraH(num, true);
    o.plugins.tooltip.callbacks.footer = function (it) {
      if (!it.length) return ""; var c = it[0].chart.data.datasets, j = it[0].dataIndex;
      var fb = c[0].data[j], tusd = (c[1].data[j] || 0) + (fb || 0), te = c[2].data[j] || 0;
      return "Total " + num(tusd + te) + "; TUSD " + num(tusd) + (fb != null && tusd ? "; Fio B = " + pctS(fb / tusd) + " da TUSD" : "");
    };
    return o;
  }
  var renderListaComp = seletor("busca", "lista", true, function (d, marcado, el) {
    var i = sel.indexOf(d);
    if (marcado && i < 0) {
      if (sel.length >= D.meta.max_sel) { el.querySelector("input").checked = false; alert("Compare no máximo " + D.meta.max_sel + " distribuidoras. Remova uma antes."); return; }
      sel.push(d);
    } else if (!marcado && i >= 0) sel.splice(i, 1);
    sujar(["comparador", "rankings", "grupoa"]);
  }, function () { return sel; });
  $("chips").addEventListener("click", function (e) {
    var d = e.target.getAttribute("data-rm"); if (d == null) return;
    sel.splice(sel.indexOf(d), 1); sujar(["comparador", "rankings", "grupoa"]);
  });
  function renderComp() {
    $("chips").innerHTML = (sel.length ? sel.map(function (d) {
      return '<span class="chip"><span class="cor" style="background:' + cor(d) + '"></span>' + esc(rot(d)) +
        '<button title="Remover" data-rm="' + esc(d) + '">×</button></span>'; }).join("") : "") +
      (sel.length < 2 ? '<span class="mut">Selecione de 2 a ' + D.meta.max_sel + " distribuidoras.</span>" : "");
    // tabela lado a lado
    var a16 = AC[0], a20 = AC[AC.length - 1];
    var L = [
      ["Vigência atual", function (m) { return dataBr(m.ini) + " a " + dataBr(m.fim); }],
      ["Resolução", function (m) { return esc(m.reh); }],
      ["UF / região", function (m, d) { return esc((I[d].uf || "-") + " / " + (I[d].r || "-")); }],
      ["Grupo", function (m, d) { return esc(I[d].g || "-"); }],
      ["TUSD", function (m) { return num(m.tusd); }],
      ["TE", function (m) { return num(m.te); }],
      ["B3 total (TUSD+TE)", function (m) { return "<b>" + num(m.tot) + "</b>"; }],
      ["ICMS", function (m) { return m.tx ? pct2(m.tx.i) + ' <span class="mut">(' + fonteCurta(m.tx.fi) + ")</span>" : "-"; }],
      ["PIS+COFINS", function (m) { return m.tx ? pct2(m.tx.pis + m.tx.cof) + ' <span class="mut">(' + fonteCurta(m.tx.fp) + ")</span>" : "-"; }],
      ["Tarifa com tributos (V)", function (m) { var r = txCalcM(m, false); return "<b>" + num(r && r.V) + "</b>"; }],
      ["Valor compensado com tributos (W, GD II)", function (m) { var r = txCalcM(m, true); return "<b>" + num(r && r.W) + "</b>"; }],
      ["Valor compensado com tributos (W, GD I)", function (m) { var r = txCalcM(m, false); return num(r && r.W) +
        (m.tx ? ' <span class="mut">(ICMS TE ' + sn(m.tx.te) + ", TUSD " + sn(m.tx.tu) + ")</span>" : ""); }],
      ["Posição B3 (1 = mais cara)", function (m) { return posTxt(m.rk); }],
      ["Fio B SCEE", function (m) { return num(m.fb) + (m.sus ? " (suspeito)" : ""); }],
      ["Fio B / TUSD", function (m) { return pctS(m.fbt); }],
      ["Fio B efetivo", function (m) { return num(m.fbe) + (m.esc == null ? "" : ' <span class="mut">(' + f0.format(m.esc * 100) + "%)</span>"); }],
      ["Valor do kWh compensado", function (m) { return "<b>" + num(m.vc) + "</b>" + (m.pvc == null ? "" : ' <span class="mut">(' + pctS(m.pvc) + ")</span>"); }],
      ["Posição valor compensado", function (m) { return posTxt(m.rc); }],
      ["Último reajuste", function (m) { return '<span class="' + cls(m.vu) + '">' + pct(m.vu) + "</span>" + (m.ur ? ' <span class="mut">(' + dataBr(m.ur) + ")</span>" : ""); }],
      ["Acumulado desde " + a20, function (m) { var x = m.acum[a20] || []; return pct(x[1]) + ' <span class="mut">(real ' + pct(x[4]) + ")</span>"; }],
      ["Acumulado desde " + a16, function (m) { var x = m.acum[a16] || []; return pct(x[1]) + ' <span class="mut">(real ' + pct(x[4]) + ")</span>"; }],
      ["CAGR desde " + a16, function (m) { return pct((m.acum[a16] || [])[2]); }],
      ["CAGR desde " + a20, function (m) { return pct((m.acum[a20] || [])[2]); }],
      ["Var. 12 meses", function (m) { return pct(m.m12[1]) + ' <span class="mut">(IPCA ' + pct(m.m12[2]) + ")</span>"; }],
      ["Próximo reajuste", function (m) { return m.px ? dataBr(m.px) + (m.pp ? ' <span class="tag ok">publicado: ' + num(m.pt) + " (" + pct(m.pv) + ")</span>" : ' <span class="tag esp">previsto</span>') : "-"; }]
    ];
    if (GA.s["A4 Verde"]) {
      var av = function (d) { return GA.s["A4 Verde"].atual[d]; };
      L.push(["A4 Verde: energia FP (R$/MWh)", function (m, d) { var a = av(d); return a ? "<b>" + num(a.efp) + "</b>" : "-"; }],
        ["A4 Verde: energia P (R$/MWh)", function (m, d) { var a = av(d); return a ? num(a.ep) + ' <span class="mut">(' + (a.rel_e == null ? "-" : f2.format(a.rel_e) + "x FP") + ")</span>" : "-"; }],
        ["A4 Verde: demanda (R$/kW)", function (m, d) { var a = av(d); return a ? num(a.d) : "-"; }],
        ["A4 Verde: último reajuste energia FP", function (m, d) { var a = av(d); return a ? '<span class="' + cls(a.vu_efp) + '">' + pct(a.vu_efp) + "</span>" + (a.ur ? ' <span class="mut">(' + dataBr(a.ur) + ")</span>" : "") : "-"; }]);
    }
    var ds = sel.filter(function (d) { return M[d]; });
    $("lado").innerHTML = ds.length ? tabela(["Indicador"].concat(ds.map(rot)), L.map(function (l) {
      return "<tr><td>" + esc(l[0]) + "</td>" + ds.map(function (d) { return td(l[1](M[d], d)); }).join("") + "</tr>"; }), "", "lado")
      : '<div class="erro">Selecione distribuidoras acima.</div>';
    // composicao e valor compensado
    var labs = ds.map(rot).concat(["Mediana Brasil"]);
    var fontes = ds.map(function (d) { var m = M[d]; return {tusd: m.tusd, te: m.te, fb: m.sus ? null : m.fb}; })
      .concat([{tusd: D.med.br.tusd, te: D.med.br.te, fb: D.med.br.fiob}]);
    grafico("g-comp", {type: "bar", data: {labels: labs, datasets: dsComposicao(fontes)}, options: opcoesComp()});
    grafico("g-vc", {type: "bar", data: {labels: labs, datasets: [
      {label: "Valor do kWh compensado", data: ds.map(function (d) { return M[d].vc; }).concat([D.med.br.valor_comp]), backgroundColor: "#2E7D4F", stack: "s"},
      {label: "Fio B efetivo (pago)", data: ds.map(function (d) { return M[d].sus ? null : M[d].fbe; }).concat([D.med.br.fiob_efetivo]), backgroundColor: "#C9D1DE", stack: "s"}]},
      options: opcoesBarraH(num, true)});
    // linhas
    grafico("g-b3", {type: "line", data: {labels: D.matrizes.b3.anos,
      datasets: ds.map(function (d) { return serie("b3", d, rot(d), false); })}, options: opcoesLinha()});
    var dsF = [];
    ds.forEach(function (d) {
      var v = ((D.tl[d] || {}).fiob || []).slice().reverse();
      dsF.push(degraus(v, 3, d, rot(d) + " homologado", false));
      dsF.push(degraus(v, 6, d, rot(d) + " efetivo", true));
    });
    var op = opcoesLinha();
    op.scales.x = {type: "linear", grid: {display: false}, min: Date.UTC(A0, 0, 1),
      ticks: {maxTicksLimit: 8, callback: function (v) { return mesAno(v); }}};
    op.plugins.tooltip.callbacks.title = function (it) { return it.length ? dataBr(new Date(it[0].parsed.x).toISOString()) : ""; };
    grafico("g-fiob", {type: "line", data: {datasets: dsF}, options: op});
  }
  function serie(mat, d, label, tracejada) {
    var m = D.matrizes[mat], v = m.v[d] || [];
    return {label: label, data: m.anos.map(function (a, j) { return v[j] == null ? null : v[j]; }),
      borderColor: cor(d), backgroundColor: cor(d), borderDash: tracejada ? [6, 4] : [], spanGaps: true,
      tension: 0.2, pointRadius: 2.5, borderWidth: 2};
  }
  // Serie em degraus por vigencia: v = [[inicio, fim, reh, fiob, var, esc, efetivo, suspeito], ...] (antigo para recente)
  function degraus(v, k, d, label, tracejada) {
    var pts = [];
    v.forEach(function (r) { if (r[k] != null) pts.push({x: ms(r[0]), y: r[k]}); });
    var u = v.length ? v[v.length - 1] : null;
    if (u && u[k] != null && u[1]) pts.push({x: ms(u[1]), y: u[k]});
    return {label: label, data: pts, stepped: "before", borderColor: cor(d), backgroundColor: cor(d),
      borderDash: tracejada ? [6, 4] : [], pointRadius: 2.5, borderWidth: 2};
  }

  // ================= RANKINGS
  var RANKS = [
    {k: "vc", n: "Valor do kWh compensado", u: "R$/MWh", f: num, v: function (m) { return m.vc; }, ex: function (m) { return "B3 " + num(m.tot) + " menos Fio B efetivo " + num(m.fbe); }},
    {k: "pvc", n: "Valor compensado / B3 total", u: "%", f: pctS, v: function (m) { return m.pvc; }},
    {k: "tot", n: "B3 total", u: "R$/MWh", f: num, v: function (m) { return m.tot; }, ex: function (m) { return "TUSD " + num(m.tusd) + " + TE " + num(m.te); }},
    {k: "vt", n: "Tarifa B3 com impostos", u: "R$/MWh", f: num, v: function (m) { var r = txCalcM(m, false); return r ? r.V : null; },
      ex: function (m) { return "B3 " + num(m.tot) + "; ICMS " + pct2(m.tx.i) + ", PIS+COFINS " + pct2(m.tx.pis + m.tx.cof) + " (" + fonteCurta(m.tx.fi) + ")"; }},
    {k: "wt", n: "Valor compensado com tributos (W)", u: "R$/MWh", f: num, v: function (m) { var r = txCalcM(m, true); return r ? r.W : null; },
      ex: function (m) { var r = txCalcM(m, false); return "GD II; GD I " + num(r && r.W) + "; ICMS TE " + sn(m.tx.te) + ", TUSD " + sn(m.tx.tu); }},
    {k: "fb", n: "Fio B SCEE", u: "R$/MWh", f: num, v: function (m) { return m.sus ? null : m.fb; }},
    {k: "fbt", n: "Fio B / TUSD", u: "%", f: pctS, v: function (m) { return m.fbt; }},
    {k: "vu", n: "Variação do último reajuste", u: "%", f: pct, v: function (m) { return m.vu; }, ex: function (m) { return num(m.ant) + " para " + num(m.tot) + " em " + dataBr(m.ur); }},
    {k: "a0", n: "B3 acumulada desde " + AC[0], u: "%", f: pct, v: function (m) { return (m.acum[AC[0]] || [])[1]; }},
    {k: "r0", n: "Aumento real desde " + AC[0] + " (x IPCA)", u: "%", f: pct, v: function (m) { return (m.acum[AC[0]] || [])[4]; }},
    {k: "a1", n: "B3 acumulada desde " + AC[AC.length - 1], u: "%", f: pct, v: function (m) { return (m.acum[AC[AC.length - 1]] || [])[1]; }},
    {k: "vfb", n: "Variação do Fio B SCEE (vigência anterior)", u: "%", f: pct, v: null}
  ];
  var rankAtual = RANKS[0], rankOrd = "desc";
  $("rank-met").innerHTML = RANKS.map(function (r, j) { return '<button class="btn' + (j ? "" : " ativo") + '" data-rk="' + r.k + '">' + esc(r.n) + "</button>"; }).join("");
  $("rank-met").addEventListener("click", function (e) {
    var b = e.target.closest("[data-rk]"); if (!b) return;
    rankAtual = RANKS.filter(function (r) { return r.k === b.getAttribute("data-rk"); })[0];
    document.querySelectorAll("[data-rk]").forEach(function (x) { x.classList.toggle("ativo", x === b); });
    renderRank(); });
  document.querySelectorAll("[data-rord]").forEach(function (b) { b.addEventListener("click", function () {
    rankOrd = b.getAttribute("data-rord");
    document.querySelectorAll("[data-rord]").forEach(function (x) { x.classList.toggle("ativo", x === b); }); renderRank(); }); });
  $("rank-todas").addEventListener("change", renderRank);
  function renderRank() {
    var R = rankAtual, itens;
    if (R.k === "vfb") {
      itens = D.rank_fiob.map(function (x) { return {d: x[0], v: x[1], ex: num(x[4]) + " (" + dataBr(x[5]) + ") para " + num(x[2]) + " (" + dataBr(x[3]) + ")"}; });
    } else {
      itens = D.lista.filter(function (d) { return M[d] && I[d].a && R.v(M[d]) != null; })
        .map(function (d) { return {d: d, v: R.v(M[d]), ex: R.ex ? R.ex(M[d]) : ""}; });
    }
    itens.sort(function (x, y) { return y.v - x.v; });
    itens.forEach(function (x, j) { x.p = j + 1; });
    var ord = rankOrd === "asc" ? itens.slice().reverse() : itens;
    var todas = $("rank-todas").checked, g = todas ? ord : ord.slice(0, 20);
    $("rank-tit").textContent = "Ranking: " + R.n + " (" + (todas ? "todas" : (rankOrd === "asc" ? "20 menores" : "20 maiores")) + ", " + itens.length + " distribuidoras ativas)";
    $("rank-caixa").style.height = Math.max(360, g.length * 22 + 60) + "px";
    var porc = R.u === "%", med = mediana(itens.map(function (x) { return x.v; }));
    $("rank-nota").textContent = "Mediana: " + R.f(med) + (porc ? "" : " " + R.u) + ". Posição 1 = maior valor. Distribuidoras do comparador destacadas em azul.";
    var tt = {label: function (c) { return R.f(porc ? c.parsed.x / 100 : c.parsed.x); }, afterLabel: function (c) { return g[c.dataIndex].ex || ""; }};
    grafico("g-rank", {type: "bar", data: {labels: g.map(function (x) { return x.p + "ª " + rot(x.d); }),
      datasets: [{label: R.n, data: g.map(function (x) { return porc ? x.v * 100 : x.v; }),
        backgroundColor: g.map(function (x) { return sel.indexOf(x.d) >= 0 ? "#2F5597" : (R.k === "vu" || R.k === "vfb" || R.k.charAt(0) === "a" || R.k.charAt(0) === "r" ? (x.v >= 0 ? "#D9534F" : "#3C9D63") : "#9DB3D9"); }), borderRadius: 3}]},
      options: {indexAxis: "y", responsive: true, maintainAspectRatio: false, locale: "pt-BR",
        plugins: {legend: {display: false}, tooltip: {callbacks: tt}},
        scales: {x: {ticks: {callback: function (v) { return porc ? f1.format(v) + "%" : f0.format(v); }}, grid: {color: "#EDF0F5"}},
          y: {grid: {display: false}, ticks: {autoSkip: false}}}}});
    $("rank-tab").innerHTML = tabela(["Posição", "Distribuidora", "UF", "Região", R.n, "Detalhe"], ord.map(function (x) {
      var i = I[x.d] || {};
      return '<tr class="' + (ehPort(x.d) ? "port" : "") + '"><td class="n">' + x.p + 'ª</td><td class="nome">' + esc(rot(x.d)) + "</td><td>" + esc(i.uf || "") +
        "</td><td>" + esc(i.r || "") + "</td>" + td("<b>" + R.f(x.v) + "</b>", porc && R.f === pct ? cls(x.v) : "") + "<td>" + esc(x.ex || "") + "</td></tr>"; }), "Sem dados.");
  }

  // matriz com heatmap
  var matAtual = "b3", ordM = {col: -1, asc: true};
  function fundo(v) {
    if (v == null) return "";
    var t = Math.max(-1, Math.min(1, v / 0.15)), alvo = t >= 0 ? [248, 105, 107] : [99, 190, 123], k = Math.abs(t);
    var c = alvo.map(function (x) { return Math.round(255 + (x - 255) * k); });
    return "background:rgb(" + c.join(",") + ")";
  }
  function renderMatriz() {
    var m = D.matrizes[matAtual], q = $("mat-busca").value.trim(), so = $("mat-sel").checked;
    $("mat-tit").textContent = "Matriz distribuidora x ano: " + m.rotulo + " (R$/MWh)";
    var linhas = Object.keys(m.v).filter(function (d) { return (!q || casa(d, q)) && (!so || sel.indexOf(d) >= 0); });
    linhas.sort(function (x, y) {
      var r;
      if (ordM.col < 0) r = x.localeCompare(y, "pt-BR");
      else { var a = m.v[x][ordM.col], b = m.v[y][ordM.col];
        if (a == null && b == null) r = 0; else if (a == null) return 1; else if (b == null) return -1; else r = a - b; }
      return ordM.asc ? r : -r; });
    var seta = function (c) { return ordM.col === c ? (ordM.asc ? " ▲" : " ▼") : ""; };
    var h = '<table><thead><tr><th class="nome ord" data-c="-1">Distribuidora' + seta(-1) + '</th><th>UF</th>' +
      m.anos.map(function (a, j) { return '<th class="ord" data-c="' + j + '">' + a + seta(j) + "</th>"; }).join("") + "</tr></thead><tbody>";
    h += linhas.map(function (d) {
      var v = m.v[d];
      return '<tr class="' + (ehPort(d) ? "port" : "") + '"><td class="nome">' + esc(rot(d)) + "</td><td>" + esc((I[d] || {}).uf || "") + "</td>" + v.map(function (x, j) {
        var vr = varAt(v, j);
        return '<td class="n" style="' + fundo(vr) + '"' + (vr == null ? "" : ' title="Var. ' + pct(vr) + '"') + ">" + num(x) + "</td>";
      }).join("") + "</tr>"; }).join("");
    h += "</tbody></table>";
    if (!linhas.length) h = '<div class="erro">Nenhuma distribuidora para o filtro.</div>';
    $("mat").innerHTML = h;
  }
  $("mat").addEventListener("click", function (e) {
    var th = e.target.closest("th[data-c]"); if (!th) return;
    var c = parseInt(th.getAttribute("data-c"), 10);
    if (ordM.col === c) ordM.asc = !ordM.asc; else { ordM.col = c; ordM.asc = c < 0; }
    renderMatriz(); });
  $("mat-busca").addEventListener("input", renderMatriz);
  $("mat-sel").addEventListener("change", renderMatriz);
  document.querySelectorAll("[data-mat]").forEach(function (b) {
    b.addEventListener("click", function () {
      matAtual = b.getAttribute("data-mat");
      document.querySelectorAll("[data-mat]").forEach(function (x) { x.classList.toggle("ativo", x === b); });
      renderMatriz(); }); });

  // ================= REGIOES
  var UFMET = [["b3", "B3 total", num], ["valor_comp", "Valor do kWh compensado", num], ["fiob", "Fio B SCEE", num],
    ["fiob_efetivo", "Fio B efetivo", num], ["acum_" + AC[0], "B3 acumulada desde " + AC[0], pct], ["real_" + AC[0], "Aumento real desde " + AC[0], pct]];
  $("uf-met").innerHTML = UFMET.map(function (u, j) { return '<option value="' + j + '">' + esc(u[1]) + "</option>"; }).join("");
  $("uf-met").addEventListener("change", renderUF);
  function ufsOrdenadas() {
    return Object.keys(D.med.uf).sort(function (a, b) {
      var ra = D.meta.regioes.indexOf(regDe(a)), rb = D.meta.regioes.indexOf(regDe(b)); return ra - rb || a.localeCompare(b); });
  }
  function regDe(uf) { var d = (D.med.uf[uf].dists || [])[0]; return d && I[d] ? I[d].r : ""; }
  function renderRegioes() {
    var R = D.meta.regioes.filter(function (r) { return D.med.reg[r]; });
    $("uf-leg").innerHTML = "<span>Cor por região:</span>" + R.map(function (r) {
      return '<span><span class="cor" style="display:inline-block;width:10px;height:10px;border-radius:2px;background:' + COR_REG[r] + '"></span> ' + esc(r) + "</span>"; }).join("");
    grafico("g-reg", {type: "bar", data: {labels: R.map(function (r) { return r + " (" + D.med.reg[r].n + ")"; }).concat(["Brasil (" + D.med.br.n + ")"]), datasets: [
      {label: "B3 total", data: R.map(function (r) { return D.med.reg[r].b3; }).concat([D.med.br.b3]), backgroundColor: "#1F3864", borderRadius: 3},
      {label: "Valor do kWh compensado", data: R.map(function (r) { return D.med.reg[r].valor_comp; }).concat([D.med.br.valor_comp]), backgroundColor: "#2E7D4F", borderRadius: 3},
      {label: "Fio B SCEE", data: R.map(function (r) { return D.med.reg[r].fiob; }).concat([D.med.br.fiob]), backgroundColor: "#C0392B", borderRadius: 3}]},
      options: {responsive: true, maintainAspectRatio: false, locale: "pt-BR", plugins: {legend: {position: "bottom"},
        tooltip: {callbacks: {label: function (c) { return c.dataset.label + ": " + num(c.parsed.y); }}}}, scales: {y: eixoY(), x: {grid: {display: false}}}}});
    renderUF();
    // mapa de calor
    var ufs = ufsOrdenadas(), cols = UFMET.concat([["n", "Distribuidoras", f0.format]]);
    var ext = cols.map(function (c) { var v = ufs.map(function (u) { return D.med.uf[u][c[0]]; }).filter(function (x) { return x != null; });
      return [Math.min.apply(null, v), Math.max.apply(null, v)]; });
    function calor(v, e) { if (v == null || e[1] === e[0]) return ""; var t = (v - e[0]) / (e[1] - e[0]);
      return "background:rgba(47,85,151," + (0.06 + 0.5 * t).toFixed(3) + ")" + (t > 0.75 ? ";color:#fff" : ""); }
    var linhas = [], regAnt = null;
    ufs.forEach(function (u) {
      var r = regDe(u), g = D.med.uf[u];
      if (r !== regAnt) { var mr = D.med.reg[r] || {};
        linhas.push('<tr class="sub"><td>' + esc(r) + "</td><td>mediana da região</td>" + cols.map(function (c) { return td(c[2](mr[c[0]])); }).join("") + "</tr>"); regAnt = r; }
      linhas.push('<tr><td title="' + esc((g.dists || []).join(", ")) + '"><b>' + esc(u) + "</b></td><td>" + esc(r) + "</td>" +
        cols.map(function (c, j) { return '<td class="n" style="' + calor(g[c[0]], ext[j]) + '">' + c[2](g[c[0]]) + "</td>"; }).join("") + "</tr>");
    });
    linhas.push('<tr class="sub"><td>Brasil</td><td>mediana nacional</td>' + cols.map(function (c) { return td(c[2](D.med.br[c[0]])); }).join("") + "</tr>");
    $("uf-tab").innerHTML = tabela(["UF", "Região"].concat(cols.map(function (c) { return c[1]; })), linhas, "Sem dados por UF.");
  }
  function renderUF() {
    var u = UFMET[+$("uf-met").value || 0], porc = u[2] === pct;
    var ufs = Object.keys(D.med.uf).filter(function (x) { return D.med.uf[x][u[0]] != null; })
      .sort(function (a, b) { return D.med.uf[b][u[0]] - D.med.uf[a][u[0]]; });
    $("uf-tit").textContent = "Mediana por UF: " + u[1];
    grafico("g-uf", {type: "bar", data: {labels: ufs.map(function (x) { return x + " (" + D.med.uf[x].n + ")"; }), datasets: [{label: u[1],
      data: ufs.map(function (x) { var v = D.med.uf[x][u[0]]; return porc ? v * 100 : v; }),
      backgroundColor: ufs.map(function (x) { return COR_REG[regDe(x)] || "#5C6B7A"; }), borderRadius: 3}]},
      options: {responsive: true, maintainAspectRatio: false, locale: "pt-BR", plugins: {legend: {display: false},
        tooltip: {callbacks: {label: function (c) { return u[2](porc ? c.parsed.y / 100 : c.parsed.y); },
          afterLabel: function (c) { return regDe(ufs[c.dataIndex]); }}}},
        scales: {y: eixoY(porc ? function (v) { return f0.format(v) + "%"; } : null), x: {grid: {display: false}, ticks: {autoSkip: false}}}}});
  }

  // ================= CALENDARIO E MUDANCAS
  function renderCal() {
    $("cal-tit").textContent = "Reajustes nos próximos " + D.meta.dias_cal + " dias (a partir de " + dataBr(D.meta.hoje) + ")";
    $("cal").innerHTML = tabelaCal(D.cal, false);
    $("cal-nota").textContent = "Data prevista: início da nova vigência, se já publicada; senão, o dia seguinte ao fim da vigência atual. " +
      "Distribuidoras ativas com vigência vencida e sem nova vigência na base também aparecem.";
  }
  function renderLog() {
    $("log").innerHTML = tabela(["Detectado em", "Série", "Tipo", "Distribuidora", "Resolução", "Início", "Detalhe", "Anterior", "Novo", "Var."],
      D.log.map(function (r) {
        var vr = (r.ant && r.novo != null) ? r.novo / r.ant - 1 : null;
        return "<tr><td>" + esc(r.em) + "</td><td>" + esc(r.serie) + "</td><td>" + esc(r.tipo) + "</td><td>" + esc(rot(r.dist)) +
          "</td><td>" + esc(r.reh) + "</td><td>" + dataBr(r.inicio) + "</td><td>" + esc(r.detalhe) + "</td>" + td(num(r.ant)) +
          td(num(r.novo)) + td(pct(vr), cls(vr)) + "</tr>"; }),
      "Nenhuma mudança registrada ainda.");
  }

  // ================= GRUPO A
  var GA = D.ga || {pares: [], subs: [], s: {}};
  var GAMET = {efp: ["Energia fora ponta", "R$/MWh"], ep: ["Energia ponta", "R$/MWh"], d: ["Demanda", "R$/kW"], dfp: ["Demanda fora ponta", "R$/kW"]};
  var gaSub = null, gaMod = null, gaMet = "efp", gaRank = "efp", gaROrd = "desc", gaVc = "efp", gaOrdM = {col: -1, asc: true};
  (function () { var p0 = GA.s["A4 Verde"] ? "A4 Verde" : GA.pares[0]; if (p0) { gaSub = GA.s[p0].sub; gaMod = GA.s[p0].mod; } })();
  function gaS() { return GA.s[gaSub + " " + gaMod]; }
  function gaModsDe(s) { return GA.pares.filter(function (k) { return GA.s[k].sub === s; }).map(function (k) { return GA.s[k].mod; }); }
  function gaNome(c) { return c === "d" && gaMod === "Azul" ? "Demanda ponta" : GAMET[c][0]; }
  function gaMets() { var s = gaS(); return ["efp", "ep", "d", "dfp"].filter(function (c) { return s && s.mat[c]; }); }
  function vezes(v) { return v == null ? "-" : f2.format(v) + "x"; }
  // Metricas do ranking: [codigo, nome, formato, eh variacao]
  function gaRanks() {
    var az = gaMod === "Azul", r = [["efp", "Energia fora ponta (R$/MWh)", num], ["ep", "Energia ponta (R$/MWh)", num],
      ["d", (az ? "Demanda ponta" : "Demanda") + " (R$/kW)", num]];
    if (az) r.push(["dfp", "Demanda fora ponta (R$/kW)", num]);
    r.push(["rel_e", "Relação ponta / fora ponta (energia)", vezes]);
    if (az) r.push(["rel_d", "Relação ponta / fora ponta (demanda)", vezes]);
    r.push(["vu_efp", "Último reajuste: energia fora ponta", pct, 1], ["vu_ep", "Último reajuste: energia ponta", pct, 1],
      ["vu_d", "Último reajuste: " + (az ? "demanda ponta" : "demanda"), pct, 1],
      ["vc_efp", "Valor do kWh compensado fora ponta (R$/MWh)", num], ["vc_ep", "Valor do kWh compensado ponta (R$/MWh)", num],
      ["fb_d", "Fio B " + (az ? "demanda ponta" : "demanda") + " (R$/kW)", num]);
    return r;
  }
  function gaBotoes() {
    $("ga-subs").innerHTML = GA.subs.filter(function (s) { return gaModsDe(s).length; }).map(function (s) {
      return '<button class="btn' + (s === gaSub ? " ativo" : "") + '" data-gasub="' + esc(s) + '">' + esc(s) + "</button>"; }).join(" ");
    var mods = gaModsDe(gaSub);
    $("ga-mods").innerHTML = ["Verde", "Azul"].map(function (m) {
      var ok = mods.indexOf(m) >= 0;
      return '<button class="btn' + (m === gaMod ? " ativo" : "") + '" data-gamod="' + m + '"' +
        (ok ? "" : ' disabled title="Sem a modalidade ' + m + " no subgrupo " + esc(gaSub) + ' na base"') + ">" + m + "</button>"; }).join(" ");
    var mets = gaMets(); if (mets.indexOf(gaMet) < 0) gaMet = mets[0] || "efp";
    $("ga-mets").innerHTML = mets.map(function (c) { return '<button class="btn' + (c === gaMet ? " ativo" : "") + '" data-gamet="' + c + '">' + esc(gaNome(c)) + "</button>"; }).join(" ");
    var rk = gaRanks(); if (!rk.some(function (r) { return r[0] === gaRank; })) gaRank = "efp";
    $("ga-rank-met").innerHTML = rk.map(function (r) { return '<option value="' + r[0] + '"' + (r[0] === gaRank ? " selected" : "") + ">" + esc(r[1]) + "</option>"; }).join("");
  }
  $("ga-subs").addEventListener("click", function (e) {
    var b = e.target.closest("[data-gasub]"); if (!b) return;
    gaSub = b.getAttribute("data-gasub"); var mods = gaModsDe(gaSub);
    if (mods.indexOf(gaMod) < 0) gaMod = mods.indexOf("Verde") >= 0 ? "Verde" : mods[0];
    renderGA(); });
  $("ga-mods").addEventListener("click", function (e) {
    var b = e.target.closest("[data-gamod]"); if (!b || b.disabled) return; gaMod = b.getAttribute("data-gamod"); renderGA(); });
  $("ga-mets").addEventListener("click", function (e) {
    var b = e.target.closest("[data-gamet]"); if (!b) return; gaMet = b.getAttribute("data-gamet"); gaBotoes(); gaMatriz(); gaHist(); });
  $("ga-rank-met").addEventListener("change", function () { gaRank = $("ga-rank-met").value; gaRanking(); });
  document.querySelectorAll("[data-garord]").forEach(function (b) { b.addEventListener("click", function () {
    gaROrd = b.getAttribute("data-garord");
    document.querySelectorAll("[data-garord]").forEach(function (x) { x.classList.toggle("ativo", x === b); }); gaRanking(); }); });
  document.querySelectorAll("[data-gavc]").forEach(function (b) { b.addEventListener("click", function () {
    gaVc = b.getAttribute("data-gavc");
    document.querySelectorAll("[data-gavc]").forEach(function (x) { x.classList.toggle("ativo", x === b); }); gaValor(); }); });
  $("ga-mat-busca").addEventListener("input", function () { gaMatriz(); });
  $("ga-mat-sel").addEventListener("change", function () { gaMatriz(); });
  $("ga-fb-busca").addEventListener("input", function () { gaFioB(); });
  $("ga-mat").addEventListener("click", function (e) {
    var th = e.target.closest("th[data-c]"); if (!th) return;
    var c = parseInt(th.getAttribute("data-c"), 10);
    if (gaOrdM.col === c) gaOrdM.asc = !gaOrdM.asc; else { gaOrdM.col = c; gaOrdM.asc = c < 0; }
    gaMatriz(); });
  ["ga-sim-dist", "ga-sim-dem", "ga-sim-cons", "ga-sim-pp"].forEach(function (id) { $(id).addEventListener("input", gaSim); });

  function renderGA() {
    if (!GA.pares.length || !gaS()) {
      $("ga-corpo").innerHTML = '<div class="card erro" style="margin-top:16px">Sem dados do Grupo A no painel (subgrupos_grupo_a vazio no config.json ou snapshot ausente).</div>';
      return;
    }
    gaBotoes();
    var s = gaS(), med = s.med, az = gaMod === "Azul";
    $("ga-tit").textContent = "Grupo A: " + gaSub + " " + gaMod + ", Tarifa de Aplicação (vigência atual)";
    $("ga-nota").textContent = "Energia = TUSD+TE em R$/MWh; demanda = TUSD em R$/kW. " +
      (gaModsDe(gaSub).indexOf("Verde") < 0 ? "O " + gaSub + " não tem modalidade Verde na base (a Verde só existe para A3a, A4 e AS). " : "") +
      "Medianas e rankings: distribuidoras ativas. Último reajuste: contra o patamar anterior com valores diferentes.";
    var kp = [
      ["Energia fora ponta (mediana)", num(med.efp), "R$/MWh, TUSD+TE; " + med.n + " distribuidoras ativas"],
      ["Energia ponta (mediana)", num(med.ep), "R$/MWh; relação ponta / fora ponta mediana " + vezes(med.rel_e)],
      [az ? "Demanda ponta (mediana)" : "Demanda (mediana)", num(med.d), "R$/kW" + (az ? "; fora ponta " + num(med.dfp) + " (relação " + vezes(med.rel_d) + ")" : ", demanda única")],
      ["Var. mediana do último reajuste", pct(med.vu_efp), "energia fora ponta; energia ponta " + pct(med.vu_ep) + "; " + (az ? "demanda ponta " : "demanda ") + pct(med.vu_d)],
      ["Valor do kWh compensado FP (mediana)", num(med.vc_efp), "R$/MWh; energia FP menos Fio B efetivo da energia; " + med.n_vc + " distribuidoras com Fio B"],
      ["Fio B " + (az ? "demanda ponta" : "demanda") + " (mediana)", num(med.fb_d), "R$/kW, desde " + A0 + (az ? "; fora ponta " + num(med.fb_dfp) : "") + "; Fio B energia P " + num(med.fb_ep) + " R$/MWh"]
    ];
    $("ga-kpis").innerHTML = kp.map(function (k) {
      return '<div class="card kpi"><div class="rot">' + esc(k[0]) + '</div><div class="val">' + esc(k[1]) + '</div><div class="sub">' + esc(k[2]) + "</div></div>"; }).join("");
    gaMatriz(); gaHist(); gaRel(); gaRanking(); gaSimOpcoes(); gaSim(); gaValor(); gaFioB();
  }

  function gaMatriz() {
    var s = gaS(), m = s.mat[gaMet]; if (!m) { $("ga-mat").innerHTML = '<div class="erro">Sem dados.</div>'; return; }
    var q = $("ga-mat-busca").value.trim(), so = $("ga-mat-sel").checked;
    $("ga-mat-tit").textContent = "Matriz distribuidora x ano: " + gaSub + " " + gaMod + ", " + gaNome(gaMet) + " (" + GAMET[gaMet][1] + ")";
    var linhas = Object.keys(m.v).filter(function (d) { return (!q || casa(d, q)) && (!so || sel.indexOf(d) >= 0); });
    linhas.sort(function (x, y) {
      var r;
      if (gaOrdM.col < 0) r = x.localeCompare(y, "pt-BR");
      else { var a = m.v[x][gaOrdM.col], b = m.v[y][gaOrdM.col];
        if (a == null && b == null) r = 0; else if (a == null) return 1; else if (b == null) return -1; else r = a - b; }
      return gaOrdM.asc ? r : -r; });
    var seta = function (c) { return gaOrdM.col === c ? (gaOrdM.asc ? " ▲" : " ▼") : ""; };
    var h = '<table><thead><tr><th class="nome ord" data-c="-1">Distribuidora' + seta(-1) + "</th><th>UF</th>" +
      m.anos.map(function (a, j) { return '<th class="ord" data-c="' + j + '">' + a + seta(j) + "</th>"; }).join("") + "</tr></thead><tbody>";
    h += linhas.map(function (d) {
      var v = m.v[d];
      return '<tr class="' + (ehPort(d) ? "port" : "") + '"><td class="nome">' + esc(rot(d)) + "</td><td>" + esc((I[d] || {}).uf || "") + "</td>" + v.map(function (x, j) {
        var vr = varAt(v, j);
        return '<td class="n" style="' + fundo(vr) + '"' + (vr == null ? "" : ' title="Var. ' + pct(vr) + '"') + ">" + num(x) + "</td>"; }).join("") + "</tr>"; }).join("");
    $("ga-mat").innerHTML = linhas.length ? h + "</tbody></table>" : '<div class="erro">Nenhuma distribuidora para o filtro.</div>';
  }

  function gaHist() {
    var s = gaS(), k = {efp: 3, ep: 4, d: 5, dfp: 6}[gaMet];
    $("ga-hist-tit").textContent = "Histórico: " + gaSub + " " + gaMod + ", " + gaNome(gaMet) + " (" + GAMET[gaMet][1] + ")";
    var ds = sel.filter(function (d) { return (s.hist[d] || []).length; });
    var op = opcoesLinha();
    op.scales.x = {type: "linear", grid: {display: false}, ticks: {maxTicksLimit: 9, callback: function (v) { return mesAno(v); }}};
    op.plugins.tooltip.callbacks.title = function (it) { return it.length ? dataBr(new Date(it[0].parsed.x).toISOString()) : ""; };
    grafico("g-ga-hist", {type: "line", data: {datasets: ds.map(function (d) {
      return degraus(s.hist[d].slice().reverse(), k, d, rot(d), false); })}, options: op});
  }

  function gaRel() {
    var s = gaS(), ds = sel.filter(function (d) { return s.atual[d]; });
    var labs = ds.map(rot).concat(["Mediana Brasil"]);
    var fp = ds.map(function (d) { return s.atual[d].efp; }).concat([s.med.efp]);
    var p = ds.map(function (d) { return s.atual[d].ep; }).concat([s.med.ep]);
    var o = {responsive: true, maintainAspectRatio: false, locale: "pt-BR", plugins: {legend: {position: "bottom"},
      tooltip: {callbacks: {label: function (c) { return c.dataset.label + ": " + num(c.parsed.y); },
        footer: function (it) { if (!it.length) return ""; var j = it[0].dataIndex; return fp[j] ? "Ponta / fora ponta: " + vezes(p[j] / fp[j]) : ""; }}}},
      scales: {y: eixoY(), x: {grid: {display: false}}}};
    grafico("g-ga-rel", {type: "bar", data: {labels: labs, datasets: [
      {label: "Energia fora ponta", data: fp, backgroundColor: "#9DB3D9", borderRadius: 3},
      {label: "Energia ponta", data: p, backgroundColor: "#1F3864", borderRadius: 3}]}, options: o});
    $("ga-rel-nota").textContent = "Relação ponta / fora ponta: " + ds.map(function (d) { return rot(d) + " " + vezes(s.atual[d].rel_e); }).concat(["mediana " + vezes(s.med.rel_e)]).join("; ") + "." +
      (gaMod === "Verde" ? " Na Verde a TUSD da ponta embute a demanda da ponta, por isso a relação é bem maior que na Azul." :
        " Na Azul a TUSD da energia é igual nos dois postos; a diferença vem da TE. Muitas permissionárias (cooperativas) têm TE igual na ponta e fora ponta (relação 1,00x).");
  }

  function gaRanking() {
    var s = gaS(), R = gaRanks().filter(function (r) { return r[0] === gaRank; })[0] || gaRanks()[0];
    var itens = Object.keys(s.atual).filter(function (d) { return s.atual[d].a && s.atual[d][R[0]] != null; })
      .map(function (d) { return {d: d, v: s.atual[d][R[0]]}; });
    itens.sort(function (x, y) { return y.v - x.v; });
    itens.forEach(function (x, j) { x.p = j + 1; });
    var ord = gaROrd === "asc" ? itens.slice().reverse() : itens, g = ord.slice(0, 20), porc = !!R[3];
    $("ga-rank-tit").textContent = "Ranking " + gaSub + " " + gaMod + ": " + R[1] + " (" + (gaROrd === "asc" ? "20 menores" : "20 maiores") + ", " + itens.length + " distribuidoras ativas)";
    $("ga-rank-caixa").style.height = Math.max(360, g.length * 22 + 60) + "px";
    $("ga-rank-nota").textContent = "Mediana: " + R[2](mediana(itens.map(function (x) { return x.v; }))) + ". Posição 1 = maior valor. Distribuidoras do comparador destacadas em azul.";
    grafico("g-ga-rank", {type: "bar", data: {labels: g.map(function (x) { return x.p + "ª " + rot(x.d); }),
      datasets: [{label: R[1], data: g.map(function (x) { return porc ? x.v * 100 : x.v; }),
        backgroundColor: g.map(function (x) { return sel.indexOf(x.d) >= 0 ? "#2F5597" : (porc ? (x.v >= 0 ? "#D9534F" : "#3C9D63") : "#9DB3D9"); }), borderRadius: 3}]},
      options: {indexAxis: "y", responsive: true, maintainAspectRatio: false, locale: "pt-BR",
        plugins: {legend: {display: false}, tooltip: {callbacks: {label: function (c) { return R[2](porc ? c.parsed.x / 100 : c.parsed.x); }}}},
        scales: {x: {ticks: {callback: function (v) { return porc ? f1.format(v) + "%" : f0.format(v); }}, grid: {color: "#EDF0F5"}},
          y: {grid: {display: false}, ticks: {autoSkip: false}}}}});
    $("ga-rank-tab").innerHTML = tabela(["Posição", "Distribuidora", "UF", "Vigência", R[1], "Energia FP", "Energia P", gaMod === "Azul" ? "Demanda P / FP" : "Demanda"], ord.map(function (x) {
      var a = s.atual[x.d], i = I[x.d] || {};
      return '<tr class="' + (ehPort(x.d) ? "port" : "") + '"><td class="n">' + x.p + 'ª</td><td class="nome">' + esc(rot(x.d)) + "</td><td>" + esc(i.uf || "") + "</td><td>" +
        dataBr(a.ini) + " (" + esc(a.reh) + ")</td>" + td("<b>" + R[2](x.v) + "</b>", porc ? cls(x.v) : "") + td(num(a.efp)) + td(num(a.ep)) +
        td(num(a.d) + (gaMod === "Azul" ? " / " + num(a.dfp) : "")) + "</tr>"; }), "Sem dados.");
  }

  // ---- simulador Verde x Azul (mesmo subgrupo)
  function gaCusto(a, mod, dem, cons, pp) {
    if (!a || a.efp == null || a.ep == null || a.d == null || (mod === "Azul" && a.dfp == null)) return null;
    var cd = dem * (a.d + (mod === "Azul" ? a.dfp : 0)), cf = cons * (1 - pp) * a.efp, cp = cons * pp * a.ep;
    return {dem: cd, efp: cf, ep: cp, tot: cd + cf + cp};
  }
  function gaSimOpcoes() {
    var V = GA.s[gaSub + " Verde"], Z = GA.s[gaSub + " Azul"], base = V || Z;
    var ds = Object.keys(base.atual).filter(function (d) { return (!V || V.atual[d]) && (!Z || Z.atual[d]); })
      .sort(function (x, y) { var ax = base.atual[x].a ? 0 : 1, ay = base.atual[y].a ? 0 : 1; return ax - ay || x.localeCompare(y, "pt-BR"); });
    var atual = $("ga-sim-dist").value, alvo = ds.indexOf(atual) >= 0 ? atual : (ds.indexOf(distAtual) >= 0 ? distAtual : (ds.indexOf(D.padrao_dist) >= 0 ? D.padrao_dist : ds[0]));
    $("ga-sim-dist").innerHTML = ds.map(function (d) { return '<option value="' + esc(d) + '"' + (d === alvo ? " selected" : "") + ">" + esc(rot(d)) + (base.atual[d].a ? "" : " (inativa)") + "</option>"; }).join("");
  }
  function gaSim() {
    if (!gaS()) return;
    var V = GA.s[gaSub + " Verde"], Z = GA.s[gaSub + " Azul"];
    var dem = Math.max(0, +$("ga-sim-dem").value || 0), cons = Math.max(0, +$("ga-sim-cons").value || 0);
    var pp = Math.min(100, Math.max(0, +$("ga-sim-pp").value || 0)) / 100, d = $("ga-sim-dist").value;
    $("ga-sim-tit").textContent = "Simulador Verde x Azul: " + gaSub + ", custo mensal estimado";
    function brl(v) { return v == null ? "-" : "R$ " + f2.format(v); }
    function caixa(nome, c, a, mod, melhor) {
      if (!c) return '<div class="card"><div class="rot mut">' + esc(nome) + '</div><div class="erro" style="padding:12px">' +
        (a ? "Tarifa incompleta na base." : "Modalidade sem tarifa para esta distribuidora no " + esc(gaSub) + ".") + "</div></div>";
      return '<div class="card kpi' + (melhor ? " melhor" : "") + '"><div class="rot">' + esc(nome) + (melhor ? " · mais barata" : "") + '</div><div class="val">' + brl(c.tot) + "</div>" +
        '<div class="sub">Demanda ' + brl(c.dem) + " (" + f0.format(dem) + " kW x " + (mod === "Azul" ? num(a.d) + " P + " + num(a.dfp) + " FP" : num(a.d)) + ")<br>" +
        "Energia FP " + brl(c.efp) + " (" + f1.format(cons * (1 - pp)) + " MWh x " + num(a.efp) + ")<br>" +
        "Energia P " + brl(c.ep) + " (" + f1.format(cons * pp) + " MWh x " + num(a.ep) + ")<br>Vigência " + dataBr(a.ini) + " (" + esc(a.reh) + ")</div></div>";
    }
    var av = V ? V.atual[d] : null, az = Z ? Z.atual[d] : null;
    var cv = gaCusto(av, "Verde", dem, cons, pp), ca = gaCusto(az, "Azul", dem, cons, pp);
    var mv = !!(cv && ca && cv.tot <= ca.tot), ma = !!(cv && ca && ca.tot < cv.tot);
    var res = caixa("Verde", cv, av, "Verde", mv) + caixa("Azul", ca, az, "Azul", ma);
    function equilibrio(a, b) {  // % do consumo na ponta em que Verde = Azul (mesmos kW e MWh)
      if (!a || !b) return null;
      var c0 = gaCusto(a, "Verde", dem, cons, 0), c1 = gaCusto(a, "Verde", dem, cons, 1), z0 = gaCusto(b, "Azul", dem, cons, 0), z1 = gaCusto(b, "Azul", dem, cons, 1);
      if (!c0 || !c1 || !z0 || !z1) return null;
      var a0 = c0.tot - z0.tot, a1 = c1.tot - z1.tot; if (a0 === a1) return null;
      var p = a0 / (a0 - a1); return p >= 0 && p <= 1 ? p : null;
    }
    if (cv && ca) {
      var dif = Math.abs(cv.tot - ca.tot), maior = Math.max(cv.tot, ca.tot), eq = equilibrio(av, az);
      res += '<div class="card kpi"><div class="rot">Resultado</div><div class="val">' + (mv ? "Verde" : "Azul") + " mais barata</div>" +
        '<div class="sub">Economia de ' + brl(dif) + " por mês (" + pctS(maior ? dif / maior : null) + ") contra a " + (mv ? "Azul" : "Verde") + ".<br>" +
        (eq == null ? "Sem ponto de equilíbrio entre 0% e 100% do consumo na ponta para esta demanda e este consumo." :
          "Ponto de equilíbrio: " + f1.format(eq * 100) + "% do consumo na ponta (abaixo disso a Verde sai mais barata).") + "</div></div>";
    }
    $("ga-sim-res").innerHTML = res;
    // tabela com todas as distribuidoras ativas do subgrupo, mesmo perfil
    var base0 = V || Z, linhas = Object.keys(base0.atual).filter(function (x) { return base0.atual[x].a; }).sort(function (x, y) { return x.localeCompare(y, "pt-BR"); });
    $("ga-sim-tab").innerHTML = tabela(["Distribuidora", "UF", "Verde (R$/mês)", "Azul (R$/mês)", "Mais barata", "Diferença (R$/mês)", "Equilíbrio (% na ponta)"], linhas.map(function (x) {
      var a1 = V ? V.atual[x] : null, a2 = Z ? Z.atual[x] : null, c1 = gaCusto(a1, "Verde", dem, cons, pp), c2 = gaCusto(a2, "Azul", dem, cons, pp);
      var best = c1 && c2 ? (c1.tot <= c2.tot ? "Verde" : "Azul") : (c1 ? "Verde" : (c2 ? "Azul" : "-")), e = equilibrio(a1, a2);
      return '<tr class="' + (ehPort(x) ? "port" : "") + '"' + (x === d ? ' style="font-weight:600"' : "") + '><td class="nome">' + esc(rot(x)) + "</td><td>" + esc((I[x] || {}).uf || "") + "</td>" +
        td(brl(c1 && c1.tot)) + td(brl(c2 && c2.tot)) + "<td>" + (c1 && c2 ? '<span class="tag ' + (best === "Verde" ? "ok" : "esp") + '">' + best + "</span>" : best) + "</td>" +
        td(c1 && c2 ? brl(Math.abs(c1.tot - c2.tot)) : "-") + td(e == null ? "-" : f1.format(e * 100) + "%") + "</tr>"; }), "Sem distribuidoras.");
    $("ga-sim-nota").textContent = "Estimativa do custo mensal SEM impostos (ICMS, PIS/COFINS), SEM bandeiras tarifárias, SEM ultrapassagem de demanda e sem reativos, " +
      "com as tarifas de aplicação da vigência atual. Verde: demanda única x tarifa de demanda + energia fora ponta + energia ponta. Azul: a mesma demanda contratada " +
      "na ponta e fora ponta (cada uma com sua tarifa) + energia. Na prática, a demanda da ponta na Azul pode ser contratada menor. Tabela: distribuidoras ativas, mesmo perfil." +
      (V ? "" : " O " + gaSub + " não tem modalidade Verde na base: só a Azul é calculada.");
  }

  function gaValor() {
    var s = gaS(), k = gaVc, kv = "vc_" + k, kf = "fbe_" + k;
    var v = Object.keys(s.atual).filter(function (d) { return s.atual[d].a && s.atual[d][kv] != null; })
      .sort(function (x, y) { return s.atual[y][kv] - s.atual[x][kv]; });
    var r = v.length > 20 ? v.slice(0, 10).concat(v.slice(-10)) : v;
    $("ga-vc-tit").textContent = "Valor do kWh compensado " + gaSub + " " + gaMod + ", " + (k === "efp" ? "fora ponta" : "ponta") + (v.length > 20 ? ": 10 maiores e 10 menores" : "");
    grafico("g-ga-vc", {type: "bar", data: {labels: r.map(rot), datasets: [
      {label: "Valor do kWh compensado", data: r.map(function (d) { return s.atual[d][kv]; }), backgroundColor: r.map(function (d, j) { return v.length > 20 && j >= 10 ? "#C0392B" : "#2E7D4F"; }), borderRadius: 3},
      {label: "Fio B efetivo da energia (pago)", data: r.map(function (d) { return s.atual[d][kf]; }), backgroundColor: "#C9D1DE", borderRadius: 3}]},
      options: opcoesBarraH(num, true)});
    $("ga-vc-nota").textContent = "Energia (TUSD+TE, R$/MWh) menos o Fio B efetivo da parcela de energia (Fio B R$/MWh x % da Lei 14.300). Mediana: " +
      num(mediana(v.map(function (d) { return s.atual[d][kv]; }))) + " R$/MWh, " + v.length + " distribuidoras ativas com Fio B. " +
      "O Fio B da energia fora ponta é zero na base, então no fora ponta o valor compensado é a própria energia. A demanda (R$/kW) não é compensada: é paga integralmente. Sem tributos e sem bandeira.";
  }

  function gaFioB() {
    var s = gaS(), az = gaMod === "Azul", q = $("ga-fb-busca").value.trim();
    $("ga-fb-tit").textContent = "TUSD Fio B " + gaSub + " " + gaMod + " por posto e unidade (vigência atual, desde " + A0 + ")";
    var ds = Object.keys(s.atual).filter(function (d) { return s.atual[d].fbi && (!q || casa(d, q)); })
      .sort(function (x, y) { var ax = s.atual[x].a ? 0 : 1, ay = s.atual[y].a ? 0 : 1; return ax - ay || x.localeCompare(y, "pt-BR"); });
    var cab = ["Distribuidora", "Vigência Fio B", "Energia FP (R$/MWh)", "Energia P (R$/MWh)"].concat(az ? ["Demanda P (R$/kW)", "Demanda FP (R$/kW)"] : ["Demanda (R$/kW)"])
      .concat(["% Lei 14.300", "Efetivo energia P", "Valor compensado FP", "Valor compensado P"]);
    $("ga-fb").innerHTML = tabela(cab, ds.map(function (d) {
      var a = s.atual[d];
      return '<tr class="' + (ehPort(d) ? "port" : "") + '"><td class="nome">' + esc(rot(d)) + (a.a ? "" : ' <span class="mut">(inativa)</span>') + "</td><td>" + dataBr(a.fbi) + " (" + esc(a.fbr) + ")</td>" +
        td(num(a.fb_efp)) + td(num(a.fb_ep)) + td("<b>" + num(a.fb_d) + "</b>") + (az ? td(num(a.fb_dfp)) : "") +
        td(a.esc == null ? "-" : f0.format(a.esc * 100) + "%") + td(num(a.fbe_ep)) + td(num(a.vc_efp)) + td(num(a.vc_ep)) + "</tr>"; }), "Sem Fio B do Grupo A para o filtro.");
    $("ga-fb-nota").textContent = "Fio B da energia: detalhe SCEE (igual ao normal na base); da demanda: detalhe normal. Efetivo = Fio B x % da Lei 14.300 do ano de início da vigência. " +
      "Valor compensado = energia (TUSD+TE) menos o Fio B efetivo da energia. Histórico por vigência no Excel (aba Grupo A Fio B).";
  }

  // ================= CALCULADORA GD (mesma formula de calc_gd no Python; ver Metodologia)
  function calcGD(tusd, te, i, pc, fte, ftu, s, fb) {
    var kpc = 1 / (1 - pc), kf = 1 / ((1 - i) * (1 - pc)), kte = fte ? kf : kpc, ktu = ftu ? kf : kpc;
    var u = tusd + te, w = (tusd - fb) * ktu + te * kte, y = w - (u - fb), x = s * y;
    return {kpc: kpc, kf: kf, kte: kte, ktu: ktu, U: u, V: u * kf, fb: fb, W: w, Y: y, X: x, Z: (u - fb) + x, fbr: fb * kf};
  }
  function escAno(a) {
    var e = D.meta.esc, anos = Object.keys(e).map(Number);
    if (a < Math.min.apply(null, anos)) return 0;
    return e[String(a)] != null ? e[String(a)] : 1;
  }
  // V e W de uma distribuidora com as aliquotas e flags padrao do painel (GD II: Fio B efetivo da vigencia atual)
  function txCalcM(m, gd2) {
    var t = m && m.tx; if (!t || m.tusd == null || m.te == null) return null;
    var fb = 0; if (gd2) { if (m.sus || m.fbe == null) return null; fb = m.fbe; }
    return calcGD(m.tusd, m.te, t.i, t.pis + t.cof, t.te, t.tu, t.s, fb);
  }
  var f4 = new Intl.NumberFormat("pt-BR", {minimumFractionDigits: 4, maximumFractionDigits: 4});
  var f6 = new Intl.NumberFormat("pt-BR", {minimumFractionDigits: 6, maximumFractionDigits: 6});
  function pct2(v) { return v == null || !isFinite(v) ? "-" : f2.format(v * 100) + "%"; }
  function sn(v) { return v ? "sim" : "não"; }
  function fonteCurta(f) { f = f || ""; return f.indexOf("faturas") === 0 ? "faturas" : (f.indexOf("planilha") === 0 ? "planilha" : (f ? "padrão" : "-")); }
  function lerN(id) {
    var s = String($(id).value || "").trim().replace(/\s/g, "").replace("%", ""); if (!s) return null;
    if (s.indexOf(",") >= 0) s = s.replace(/\./g, "").replace(",", ".");
    var v = parseFloat(s); return isFinite(v) ? v : null;
  }
  function escN(v, casas) {
    if (v == null || !isFinite(v)) return "";
    var s = (+v).toFixed(casas); if (casas > 0) s = s.replace(/0+$/, "").replace(/\.$/, "");
    return s.replace(".", ",");
  }
  var calcDist = D.padrao_dist, calcIni = false;
  seletor("c-busca", "c-lista", false, function (d) { calcDist = d; calcPadroes(false); calcRender(); }, function () { return [calcDist]; });
  function calcPfb() {
    var enq = +$("c-enq").value, a = +String($("c-data").value || D.meta.hoje).slice(0, 4);
    $("c-pfb").value = enq === 1 ? "0" : escN(escAno(a) * 100, 2);
  }
  function calcPadroes(tudo) {  // campos da distribuidora; tudo = tambem data, enquadramento e preco
    var m = M[calcDist] || {}, t = m.tx || {}, P = D.imp.padrao;
    var v = function (x, p) { return x != null ? x : p; };
    if (tudo) { $("c-data").value = D.meta.hoje; $("c-enq").value = "2"; $("c-modo").value = "d"; $("c-preco").value = "20";
      $("c-preco-rot").textContent = "Deságio (%)"; }
    $("c-tusd").value = escN(m.tusd, 4); $("c-te").value = escN(m.te, 4); $("c-fb").value = escN(m.sus ? null : m.fb, 4);
    $("c-icms").value = escN(v(t.i, P.i) * 100, 4); $("c-pis").value = escN(v(t.pis, P.pis) * 100, 4);
    $("c-cof").value = escN(v(t.cof, P.cof) * 100, 4);
    $("c-fte").value = String(v(t.te, 0)); $("c-ftu").value = String(v(t.tu, 0)); $("c-s").value = escN(v(t.s, P.s) * 100, 2);
    calcPfb();
  }
  var calcUlt = null;
  function renderCalc() { if (!calcIni) { calcIni = true; calcPadroes(true); $("c-selo").textContent = D.imp.selo || ""; } calcRender(); }
  function calcRender() {
    var d = calcDist, m = M[d], i = I[d] || {}, t = (m || {}).tx || {};
    $("c-nome").textContent = rot(d);
    $("c-ficha").innerHTML = [["UF", i.uf], ["Sigla ANEEL", i.s],
      ["Vigência atual B3", m ? dataBr(m.ini) + " a " + dataBr(m.fim) + " (" + m.reh + ")" : "sem tarifa B3"],
      ["Fio B SCEE", m && m.fbi ? "vigência desde " + dataBr(m.fbi) + " (" + m.fbr + ")" + (m.sus ? ", valor suspeito" : "") : "sem Fio B SCEE"]]
      .filter(function (x) { return x[1]; }).map(function (x) { return "<span>" + esc(x[0]) + ": <b>" + esc(x[1]) + "</b></span>"; }).join("");
    $("c-fontes").innerHTML = m && m.tx ? "<b>Fontes dos padrões.</b> ICMS: " + esc(t.fi) + ". PIS/COFINS: " + esc(t.fp) + "." +
      (fonteCurta(t.fi) === "faturas" ? " (" + esc(D.imp.origem) + ")" : "") + " Flags de ICMS e repasse: " + esc(t.ff) + (t.ob ? " (" + esc(t.ob) + ")" : "") +
      ". Todos os campos são editáveis; use Restaurar padrões para voltar." : "Distribuidora sem tarifa B3 no painel: informe TUSD, TE e alíquotas à mão.";
    var Dv = lerN("c-tusd"), Ev = lerN("c-te"), fbS = lerN("c-fb") || 0, pfb = (lerN("c-pfb") || 0) / 100, enq = +$("c-enq").value;
    var ic = (lerN("c-icms") || 0) / 100, pc = ((lerN("c-pis") || 0) + (lerN("c-cof") || 0)) / 100;
    var fte = +$("c-fte").value, ftu = +$("c-ftu").value, s = (lerN("c-s") || 0) / 100;
    if (Dv == null || Ev == null || ic >= 1 || pc >= 1 || (1 - ic) * (1 - pc) <= 0) {
      calcUlt = null;
      $("c-kpis").innerHTML = '<div class="card erro">Informe TUSD e TE e alíquotas abaixo de 100%.</div>';
      $("c-mem").innerHTML = ""; $("c-nota").textContent = ""; $("c-fat-res").innerHTML = ""; $("c-fat-tab").innerHTML = ""; return;
    }
    var fb = enq === 1 ? 0 : fbS * pfb, r = calcGD(Dv, Ev, ic, pc, fte, ftu, s, fb);
    var modo = $("c-modo").value, pv = lerN("c-preco"), P = null;
    if (pv != null) P = modo === "p" ? pv : r.U * (1 - pv / 100);
    // desagio sobre U (mesma base do Arrendamento); dono pela opcao A do Arrendamento (calcArr, Fio B com impostos)
    var des = P != null && r.U ? 1 - P / r.U : null, eco = P != null ? r.W - P : null;
    var ra = des != null ? calcArr(Dv, Ev, ic, pc, fte, ftu, s, des, 0, fb, true) : null;
    calcUlt = {r: r, P: P};
    var gd = ["", "GD I", "GD II", "GD III"][enq];
    var kp = [
      ["U: tarifa sem tributos", num(r.U), "D + E, R$/MWh"],
      ["V: tarifa cheia", num(r.V), "U x k_full; ICMS " + pct2(ic) + ", PIS+COFINS " + pct2(pc)],
      ["FB_ef: Fio B pago", num(r.fb), enq === 1 ? "GD I: não paga Fio B" : gd + ": " + num(fbS) + " x " + pct2(pfb)],
      ["W: energia compensada", num(r.W), "com os tributos evitados (" + gd + "); ICMS TE " + sn(fte) + ", TUSD " + sn(ftu)],
      ["Y: benefício tributário", num(r.Y), "W - (U - FB_ef)"],
      ["X: VB", num(r.X), "s x Y, s = " + pct2(s)],
      ["Z: COMP", num(r.Z), "(U - FB_ef) + X"],
      ["Deságio nominal (sobre a tarifa)", pct2(des), P == null ? "informe o preço ou o deságio" : "1 - P / U; preço de venda P = " + num(P) + " R$/MWh",
        "Deságio sobre U = TUSD + TE (tarifa sem tributos): P = (1 - d) x U. Mesma base do Arrendamento."],
      ["Economia do cliente", num(eco), eco == null ? "" : "R$/MWh compensado (W - P); " + pct2(eco / r.V) + " da tarifa cheia",
        "Crédito da energia compensada (W, com os tributos evitados) menos o preço P pago pela energia."],
      ["Pagamento ao dono (opção A)", ra ? num(ra.dono) : "-", ra ? "P + s x Y_I - Fio B com impostos, R$/MWh (como no Arrendamento)" : "",
        "Opção A do Arrendamento: (1 - d) x U + s x Y_I - FB_ef x k_full, com Y_I = W_I - U (benefício fiscal sem Fio B)."],
      ["Deságio implícito (líquido do VB e do Fio B, sobre a tarifa sem tributos)", ra ? pct2(ra.dimp) : "-", ra ? "1 - dono / U; U = " + num(r.U) + " R$/MWh" : "",
        "Deságio implícito = 1 - pagamento ao dono / U, com U = TUSD + TE (tarifa sem tributos). Considera o VB (s x Y soma ao dono) e o Fio B (subtrai do dono): diminui quando s aumenta e aumenta com o Fio B. Fica negativo quando o dono recebe mais que a tarifa U, o que é possível com VB alto e deságio baixo."],
      ["Fio B residual com tributos", num(r.fbr), "FB_ef x k_full, pago à distribuidora por MWh compensado"]
    ];
    $("c-kpis").innerHTML = kp.map(function (k) {
      return '<div class="card kpi"' + (k[3] ? ' title="' + esc(k[3]) + '"' : "") + '><div class="rot">' + esc(k[0]) + (k[3] ? ' <span class="mut">(?)</span>' : "") + '</div><div class="val">' + esc(k[1]) + '</div><div class="sub">' + esc(k[2]) + "</div></div>"; }).join("");
    var L = [
      ["k_PC", "1 / (1 - pc)", "1 / (1 - " + f4.format(pc) + ")", f6.format(r.kpc)],
      ["k_full", "1 / ((1 - i) x (1 - pc))", "1 / ((1 - " + f4.format(ic) + ") x (1 - " + f4.format(pc) + "))", f6.format(r.kf)],
      ["k_TE", fte ? "k_full (ICMS da TE entra no benefício)" : "k_PC (ICMS da TE não entra)", "", f6.format(r.kte)],
      ["k_TUSD", ftu ? "k_full (ICMS da TUSD entra no benefício)" : "k_PC (ICMS da TUSD não entra)", "", f6.format(r.ktu)],
      ["U (tarifa sem tributos)", "D + E", num(Dv) + " + " + num(Ev), num(r.U)],
      ["V (tarifa cheia)", "U x k_full", num(r.U) + " x " + f6.format(r.kf), num(r.V)],
      ["FB_ef (Fio B pago)", enq === 1 ? "GD I: 0" : "Fio B SCEE x % Lei 14.300", enq === 1 ? "" : num(fbS) + " x " + pct2(pfb), num(r.fb)],
      ["W (energia compensada)", "(D - FB_ef) x k_TUSD + E x k_TE", "(" + num(Dv) + " - " + num(r.fb) + ") x " + f6.format(r.ktu) + " + " + num(Ev) + " x " + f6.format(r.kte), num(r.W)],
      ["Y (benefício tributário)", "W - (U - FB_ef)", num(r.W) + " - (" + num(r.U) + " - " + num(r.fb) + ")", num(r.Y)],
      ["X (VB)", "s x Y", pct2(s) + " x " + num(r.Y), num(r.X)],
      ["Z (COMP)", "(U - FB_ef) + X", "(" + num(r.U) + " - " + num(r.fb) + ") + " + num(r.X), num(r.Z)],
      ["P (preço de venda)", modo === "p" ? "informado" : "(1 - d) x U", modo === "p" ? "" : "(1 - " + pct2(pv == null ? null : pv / 100) + ") x " + num(r.U), num(P)],
      ["Deságio nominal d (sobre a tarifa)", "1 - P / U", "1 - " + num(P) + " / " + num(r.U), pct2(des)],
      ["Economia do cliente", "W - P", num(r.W) + " - " + num(P), num(eco)],
      ["W_I (sem Fio B, como no Arrendamento)", "D x k_TUSD + E x k_TE", ra ? num(Dv) + " x " + f6.format(ra.ktu) + " + " + num(Ev) + " x " + f6.format(ra.kte) : "", ra ? num(ra.WI) : "-"],
      ["Y_I (benefício fiscal sem Fio B)", "W_I - U", ra ? num(ra.WI) + " - " + num(r.U) : "", ra ? num(ra.Y) : "-"],
      ["Custo do Fio B com impostos (dono)", "FB_ef x k_full", num(r.fb) + " x " + f6.format(r.kf), ra ? num(ra.cfb) : "-"],
      ["Pagamento ao dono (opção A)", "(1 - d) x U + s x Y_I - custo do Fio B", ra ? "(1 - " + pct2(des) + ") x " + num(r.U) + " + " + pct2(s) + " x " + num(ra.Y) + " - " + num(ra.cfb) : "", ra ? num(ra.dono) : "-"],
      ["Deságio implícito (líquido do VB e do Fio B)", "1 - dono / U", ra ? "1 - " + num(ra.dono) + " / " + num(r.U) : "", ra ? pct2(ra.dimp) : "-"],
      ["Fio B residual com tributos", "FB_ef x k_full", num(r.fb) + " x " + f6.format(r.kf), num(r.fbr)]
    ];
    $("c-mem").innerHTML = tabela(["Passo", "Fórmula", "Cálculo", "Valor"], L.map(function (l) {
      return "<tr><td>" + esc(l[0]) + "</td><td>" + esc(l[1]) + "</td><td>" + esc(l[2]) + "</td>" + td("<b>" + esc(l[3]) + "</b>") + "</tr>"; }), "", "lado");
    var ano = +String($("c-data").value || D.meta.hoje).slice(0, 4), notas = ["Valores em R$/MWh. TUSD, TE e Fio B vêm da vigência atual; a data de referência só define o % da Lei 14.300."];
    if (enq === 3) notas.push("GD III: pela Lei 14.300 (art. 27, par. 1o) paga também 40% do Fio A, TFSEE e P&D/EE, o que não está modelado; W e Z ficam superestimados.");
    if (enq !== 1 && ano > 2028) notas.push("A partir de 2029 a regra é definida pela ANEEL e não está modelada; usado 100% do Fio B.");
    if (enq !== 1 && !fbS) notas.push("Sem Fio B SCEE válido: FB_ef = 0.");
    notas.push("Deságio sobre U = TUSD + TE (tarifa sem tributos), como no Arrendamento: P = (1 - d) x U. Deságio implícito = 1 - dono / U, com o dono pela opção A (VB s sobre Y_I e Fio B com impostos descontado); muda com s e com o Fio B e pode ficar negativo.");
    if (ra && ra.dimp < 0) notas.push("Deságio implícito negativo: o dono recebe mais que a tarifa U.");
    $("c-nota").textContent = notas.join(" ");
    calcFatura(r, P);
  }
  function calcFatura(r, P) {
    var C = Math.max(0, lerN("c-cons") || 0), Ec = Math.max(0, lerN("c-comp") || 0), mn = +$("c-lig").value;
    var band = lerN("c-band") || 0, cip = lerN("c-cip") || 0;
    var fat = Math.max(C, mn), Ece = Math.min(Ec, Math.max(C - mn, 0)), liq = C - Ece;
    var eSem = fat * r.V / 1000, bSem = C * band * r.kf / 1000, sem = eSem + bSem + cip;
    var cred = Ece * r.W / 1000, bCom = liq * band * r.kf / 1000, dist = eSem - cred + bCom + cip;
    var ger = P != null ? Ece * P / 1000 : 0, tot = dist + ger, eco = sem - tot;
    function brl(v) { return v == null ? "-" : "R$ " + f2.format(v); }
    $("c-fat-res").innerHTML = [["Fatura sem GD", brl(sem), f0.format(fat) + " kWh x tarifa cheia + bandeira + CIP"],
      ["Fatura com GD (distribuidora)", brl(dist), f0.format(Ece) + " kWh compensados" + (Ece < Ec ? " (limitado a consumo menos " + mn + " kWh)" : "")],
      ["Pagamento ao gerador", brl(ger), P == null ? "sem preço informado" : f0.format(Ece) + " kWh x " + num(P) + " R$/MWh"],
      ["Economia estimada", brl(eco), sem ? pct2(eco / sem) + " da fatura sem GD" : ""]].map(function (k, j) {
      return '<div class="card kpi' + (j === 3 && eco > 0 ? " melhor" : "") + '"><div class="rot">' + esc(k[0]) + '</div><div class="val">' + esc(k[1]) + '</div><div class="sub">' + esc(k[2]) + "</div></div>"; }).join("");
    $("c-fat-tab").innerHTML = tabela(["Item", "Sem GD", "Com GD"], [
      ["Energia faturada (" + f0.format(fat) + " kWh x V)", eSem, eSem], ["Crédito da energia compensada (" + f0.format(Ece) + " kWh x W)", 0, -cred],
      ["Bandeira com tributos", bSem, bCom], ["CIP", cip, cip], ["<b>Total da fatura</b>", sem, dist],
      ["Pagamento ao gerador", 0, ger], ["<b>Custo total</b>", sem, tot]].map(function (l) {
      return "<tr><td>" + l[0] + "</td>" + td(brl(l[1])) + td(brl(l[2])) + "</tr>"; }), "", "lado");
  }
  ["c-tusd", "c-te", "c-fb", "c-pfb", "c-icms", "c-pis", "c-cof", "c-s", "c-preco", "c-cons", "c-comp", "c-band", "c-cip"].forEach(function (id) {
    $(id).addEventListener("input", calcRender); });
  ["c-fte", "c-ftu", "c-lig"].forEach(function (id) { $(id).addEventListener("change", calcRender); });
  ["c-data", "c-enq"].forEach(function (id) { $(id).addEventListener("change", function () { calcPfb(); calcRender(); }); });
  $("c-modo").addEventListener("change", function () {  // converte o valor para o outro modo
    var p = $("c-modo").value === "p", v = lerN("c-preco");
    if (calcUlt && v != null && calcUlt.r.U) $("c-preco").value = p ? escN(calcUlt.r.U * (1 - v / 100), 2) : escN((1 - v / calcUlt.r.U) * 100, 2);
    $("c-preco-rot").textContent = p ? "Preço de venda (R$/MWh)" : "Deságio (%)";
    calcRender(); });
  $("c-reset").addEventListener("click", function () { calcPadroes(true); calcRender(); });

  // ================= ARRENDAMENTO (mesma formula de calc_arrendamento no Python; ver Metodologia)
  // Opcao A: desagio d so sobre a tarifa U; beneficio fiscal Y dividido pelo VB s; o dono paga o Fio B.
  function calcArr(tusd, te, i, pc, fte, ftu, s, d, c, fb, fbCom) {
    var g = calcGD(tusd, te, i, pc, fte, ftu, s, 0), u = g.U, w = g.W, y = g.Y;
    var cfbC = fb * g.kf, cfbS = fb, cfb = fbCom ? cfbC : cfbS;
    var dono = (1 - d) * u + s * y - cfb, th = d * u + (1 - s) * y, desc = c * w;
    return {kpc: g.kpc, kf: g.kf, kte: g.kte, ktu: g.ktu, U: u, WI: w, Y: y, fb: fb, cfbC: cfbC, cfbS: cfbS, cfb: cfb,
      dono: dono, th: th, desc: desc, mg: th - desc, dimp: u ? 1 - dono / u : null};  // desagio implicito sobre U
  }
  var AR = D.arr, arrDist = D.padrao_dist, arrIni = false, arrUlt = null;
  seletor("a-busca", "a-lista", false, function (d) { arrDist = d; arrPadroes(false); arrRender(); }, function () { return [arrDist]; });
  function arrProd(d) { var uf = (I[d] || {}).uf, p = uf && AR.prod[uf]; return p ? {v: p[0], f: p[1], uf: uf} : {v: AR.prod_padrao, f: "padrão do painel (UF sem linha em produtividade_uf.csv)", uf: uf || ""}; }
  function arrPfb() {
    var enq = +$("a-enq").value, a = +String($("a-data").value || D.meta.hoje).slice(0, 4);
    $("a-pfb").value = enq === 1 ? "0" : escN(escAno(a) * 100, 2);
  }
  function arrPadroes(tudo) {  // campos da distribuidora; tudo = tambem data, enquadramento, condicoes e usina
    var m = M[arrDist] || {}, t = m.tx || {}, P = D.imp.padrao, v = function (x, p) { return x != null ? x : p; };
    if (tudo) {
      $("a-data").value = D.meta.hoje; $("a-enq").value = "2"; $("a-fbmodo").value = "1";
      $("a-d").value = escN(AR.d * 100, 2); $("a-c").value = escN(AR.c * 100, 2);
      $("a-tipo").value = "ufv"; $("a-kwp").value = "1000"; $("a-perdas").value = "0"; $("a-kw").value = "1000";
      $("a-fc").value = escN(AR.fc * 100, 2); $("a-mwh").value = "100"; $("a-pcomp").value = "100"; $("a-usina").value = "";
    }
    $("a-tusd").value = escN(m.tusd, 4); $("a-te").value = escN(m.te, 4); $("a-fb").value = escN(m.sus ? null : m.fb, 4);
    $("a-icms").value = escN(v(t.i, P.i) * 100, 4); $("a-pis").value = escN(v(t.pis, P.pis) * 100, 4);
    $("a-cof").value = escN(v(t.cof, P.cof) * 100, 4);
    $("a-fte").value = String(v(t.te, 0)); $("a-ftu").value = String(v(t.tu, 0)); $("a-s").value = escN(v(t.s, AR.s) * 100, 2);
    $("a-prod").value = escN(arrProd(arrDist).v, 0);
    arrPfb();
  }
  function arrTipo() { var tp = $("a-tipo").value;
    ["ufv", "cgh", "direto"].forEach(function (k) { document.querySelectorAll(".a-" + k).forEach(function (el) { el.style.display = k === tp ? "" : "none"; }); }); }
  function renderArr() { if (!arrIni) { arrIni = true; arrPadroes(true); $("a-selo").textContent = D.imp.selo || ""; } arrTipo(); arrRender(); }
  function brl(v) { return v == null || !isFinite(v) ? "-" : "R$ " + f2.format(v); }
  function arrGeracao() {
    var tp = $("a-tipo").value, pc = (lerN("a-pcomp") == null ? 100 : lerN("a-pcomp")) / 100, g, calc;
    if (tp === "ufv") { var kwp = lerN("a-kwp") || 0, pr = lerN("a-prod") || 0, pe = (lerN("a-perdas") || 0) / 100;
      g = kwp * pr / 12 * (1 - pe) / 1000; calc = f0.format(kwp) + " kWp x " + f0.format(pr) + " kWh/kWp/ano / 12" + (pe ? " x (1 - " + pct2(pe) + ")" : "") + " / 1.000"; }
    else if (tp === "cgh") { var kw = lerN("a-kw") || 0, fc = (lerN("a-fc") || 0) / 100;
      g = kw * fc * AR.horas / 1000; calc = f0.format(kw) + " kW x " + pct2(fc) + " x " + f0.format(AR.horas) + " h / 1.000"; }
    else { g = lerN("a-mwh") || 0; calc = "informado"; }
    return {ger: g, comp: g * pc, pc: pc, calc: calc, tp: tp};
  }
  function arrLer() {
    var Dv = lerN("a-tusd"), Ev = lerN("a-te"), fbS = lerN("a-fb") || 0, pfb = (lerN("a-pfb") || 0) / 100, enq = +$("a-enq").value;
    var ic = (lerN("a-icms") || 0) / 100, pc = ((lerN("a-pis") || 0) + (lerN("a-cof") || 0)) / 100;
    return {Dv: Dv, Ev: Ev, fbS: fbS, pfb: pfb, enq: enq, ic: ic, pc: pc, fte: +$("a-fte").value, ftu: +$("a-ftu").value,
      fbCom: $("a-fbmodo").value === "1", d: (lerN("a-d") || 0) / 100, s: (lerN("a-s") || 0) / 100, c: (lerN("a-c") || 0) / 100,
      fb: enq === 1 ? 0 : fbS * pfb};
  }
  function arrRender() {
    var dn = arrDist, m = M[dn], i = I[dn] || {}, t = (m || {}).tx || {}, pr = arrProd(dn);
    $("a-nome").textContent = rot(dn);
    $("a-ficha").innerHTML = [["UF", i.uf], ["Sigla ANEEL", i.s],
      ["Vigência atual B3", m ? dataBr(m.ini) + " a " + dataBr(m.fim) + " (" + m.reh + ")" : "sem tarifa B3"],
      ["Fio B SCEE", m && m.fbi ? "vigência desde " + dataBr(m.fbi) + " (" + m.fbr + ")" + (m.sus ? ", valor suspeito" : "") : "sem Fio B SCEE"]]
      .filter(function (x) { return x[1]; }).map(function (x) { return "<span>" + esc(x[0]) + ": <b>" + esc(x[1]) + "</b></span>"; }).join("");
    $("a-fontes").innerHTML = (m && m.tx ? "<b>Fontes dos padrões.</b> ICMS: " + esc(t.fi) + ". PIS/COFINS: " + esc(t.fp) + ". Flags de ICMS e VB s: " + esc(t.ff) + (t.ob ? " (" + esc(t.ob) + ")" : "") + ". "
      : "Distribuidora sem tarifa B3 no painel: informe TUSD, TE e alíquotas à mão. ") +
      "Produtividade UFV" + (pr.uf ? " (" + esc(pr.uf) + ")" : "") + ": " + f0.format(pr.v) + " kWh/kWp/ano (" + f1.format(pr.v / 12) + " kWh/kWp/mês), " + esc(pr.f) +
      ". Todos os campos são editáveis; use Restaurar padrões para voltar.";
    var x = arrLer();
    if (x.Dv == null || x.Ev == null || x.ic >= 1 || x.pc >= 1 || (1 - x.ic) * (1 - x.pc) <= 0) {
      arrUlt = null;
      $("a-kpis").innerHTML = '<div class="card erro">Informe TUSD e TE e alíquotas abaixo de 100%.</div>';
      ["a-res", "a-mem", "a-sens"].forEach(function (id) { $(id).innerHTML = ""; }); $("a-nota").textContent = ""; $("a-sens-nota").textContent = ""; negRender(); return;
    }
    var r = calcArr(x.Dv, x.Ev, x.ic, x.pc, x.fte, x.ftu, x.s, x.d, x.c, x.fb, x.fbCom), G = arrGeracao(), q = G.comp;
    arrUlt = {r: r, x: x, G: G};
    var gd = ["", "GD I", "GD II", "GD III"][x.enq];
    var kp = [
      ["Pagamento ao dono", brl(r.dono * q) + "/mês", num(r.dono) + " R$/MWh; " + brl(r.dono * q * 12) + "/ano", r.dono < 0],
      ["Parte da Thopen", brl(r.th * q) + "/mês", num(r.th) + " R$/MWh; " + brl(r.th * q * 12) + "/ano", false],
      ["Margem líquida da Thopen", brl(r.mg * q) + "/mês", num(r.mg) + " R$/MWh, após desconto ao cliente de " + pct2(x.c) + " de W_I", r.mg < 0],
      ["Deságio nominal (sobre a tarifa)", pct2(x.d), "parâmetro d, só sobre U = " + num(r.U) + " R$/MWh", false,
        "Deságio negociado d, aplicado só sobre U = TUSD + TE (tarifa sem tributos): o dono recebe (1 - d) x U, mais s x Y, menos o Fio B."],
      ["Deságio implícito (líquido do VB e do Fio B, sobre a tarifa sem tributos)", pct2(r.dimp), "1 - dono / U = 1 - " + num(r.dono) + " / " + num(r.U) + " (" + gd + ")", false,
        "Deságio implícito = 1 - pagamento ao dono / U, com U = TUSD + TE (tarifa sem tributos). Considera o VB (s x Y soma ao dono) e o Fio B (subtrai do dono): diminui quando s aumenta e aumenta com o Fio B. Fica negativo quando o dono recebe mais que a tarifa U, o que é possível com VB alto e deságio baixo."]
    ];
    $("a-kpis").innerHTML = kp.map(function (k) {
      return '<div class="card kpi' + (k[3] ? " negativo" : "") + '"' + (k[4] ? ' title="' + esc(k[4]) + '"' : "") + '><div class="rot">' + esc(k[0]) + (k[4] ? ' <span class="mut">(?)</span>' : "") + '</div><div class="val">' + esc(k[1]) + '</div><div class="sub">' + esc(k[2]) + "</div></div>"; }).join("");
    var f3 = function (v) { return v == null || !isFinite(v) ? "-" : f2.format(v); };
    var R = [
      ["Geração média", null, f3(G.ger) + " MWh", f3(G.ger * 12) + " MWh", false],
      ["Energia compensada (" + pct2(G.pc) + " da geração)", null, f3(q) + " MWh", f3(q * 12) + " MWh", false],
      ["W_I: valor ao cliente com tributos evitados", r.WI, brl(r.WI * q), brl(r.WI * q * 12), false],
      ["Pagamento ao dono", r.dono, brl(r.dono * q), brl(r.dono * q * 12), r.dono < 0],
      ["Parte da Thopen", r.th, brl(r.th * q), brl(r.th * q * 12), false],
      ["Custo do Fio B (pago pelo dono, " + (x.fbCom ? "com" : "sem") + " impostos)", r.cfb, brl(r.cfb * q), brl(r.cfb * q * 12), false],
      ["Desconto ao cliente final", r.desc, brl(r.desc * q), brl(r.desc * q * 12), false],
      ["Margem líquida da Thopen", r.mg, brl(r.mg * q), brl(r.mg * q * 12), r.mg < 0]
    ];
    $("a-res").innerHTML = tabela(["Item", "R$/MWh", "Por mês", "Por ano"], R.map(function (l) {
      var c = l[4] ? "negativo" : "";
      return "<tr><td>" + esc(l[0]) + "</td>" + td(l[1] == null ? "-" : num(l[1]), c) + td(esc(l[2]), c) + td(esc(l[3]), c) + "</tr>"; }), "", "lado");
    var L = [
      ["k_PC", "1 / (1 - pc)", "1 / (1 - " + f4.format(x.pc) + ")", f6.format(r.kpc)],
      ["k_full", "1 / ((1 - i) x (1 - pc))", "1 / ((1 - " + f4.format(x.ic) + ") x (1 - " + f4.format(x.pc) + "))", f6.format(r.kf)],
      ["k_TE", x.fte ? "k_full (ICMS da TE entra)" : "k_PC (ICMS da TE não entra)", "", f6.format(r.kte)],
      ["k_TUSD", x.ftu ? "k_full (ICMS da TUSD entra)" : "k_PC (ICMS da TUSD não entra)", "", f6.format(r.ktu)],
      ["U (tarifa sem tributos)", "D + E", num(x.Dv) + " + " + num(x.Ev), num(r.U)],
      ["W_I (valor com tributos evitados, sem Fio B)", "D x k_TUSD + E x k_TE", num(x.Dv) + " x " + f6.format(r.ktu) + " + " + num(x.Ev) + " x " + f6.format(r.kte), num(r.WI)],
      ["Y (benefício fiscal)", "W_I - U", num(r.WI) + " - " + num(r.U), num(r.Y)],
      ["FB_ef (Fio B efetivo)", x.enq === 1 ? "GD I: 0" : "Fio B SCEE x % Lei 14.300", x.enq === 1 ? "" : num(x.fbS) + " x " + pct2(x.pfb), num(r.fb)],
      ["Custo do Fio B com impostos" + (x.fbCom ? " (usado)" : ""), "FB_ef x k_full", num(r.fb) + " x " + f6.format(r.kf), num(r.cfbC)],
      ["Custo do Fio B sem impostos" + (x.fbCom ? "" : " (usado)"), "FB_ef", "", num(r.cfbS)],
      ["Pagamento ao dono", "(1 - d) x U + s x Y - custo do Fio B", "(1 - " + pct2(x.d) + ") x " + num(r.U) + " + " + pct2(x.s) + " x " + num(r.Y) + " - " + num(r.cfb), num(r.dono)],
      ["Parte da Thopen", "d x U + (1 - s) x Y", pct2(x.d) + " x " + num(r.U) + " + (1 - " + pct2(x.s) + ") x " + num(r.Y), num(r.th)],
      ["Conferência", "dono + Thopen + custo do Fio B = W_I", num(r.dono) + " + " + num(r.th) + " + " + num(r.cfb), num(r.dono + r.th + r.cfb)],
      ["Desconto ao cliente", "c x W_I", pct2(x.c) + " x " + num(r.WI), num(r.desc)],
      ["Margem líquida da Thopen", "Thopen - desconto", num(r.th) + " - " + num(r.desc), num(r.mg)],
      ["Deságio nominal d (sobre a tarifa)", "parâmetro", "", pct2(x.d)],
      ["Deságio implícito (líquido do VB e do Fio B)", "1 - dono / U", "1 - " + num(r.dono) + " / " + num(r.U), pct2(r.dimp)],
      ["Geração média (MWh/mês)", G.tp === "ufv" ? "kWp x kWh/kWp/ano / 12 x (1 - perdas)" : (G.tp === "cgh" ? "kW x FC x " + f0.format(AR.horas) + " h" : "informada"), G.calc, f3(G.ger)],
      ["Energia compensada (MWh/mês)", "geração x % compensada", f3(G.ger) + " x " + pct2(G.pc), f3(q)]
    ];
    $("a-mem").innerHTML = tabela(["Passo", "Fórmula", "Cálculo", "Valor"], L.map(function (l) {
      return "<tr><td>" + esc(l[0]) + "</td><td>" + esc(l[1]) + "</td><td>" + esc(l[2]) + "</td>" + td("<b>" + esc(l[3]) + "</b>") + "</tr>"; }), "", "lado");
    var ano = +String($("a-data").value || D.meta.hoje).slice(0, 4), notas = ["Valores por MWh compensado. TUSD, TE e Fio B vêm da vigência atual; a data de referência só define o % da Lei 14.300."];
    if (x.enq === 3) notas.push("GD III: pela Lei 14.300 a usina paga 100% do Fio B, 40% do Fio A, TFSEE e P&D/EE; aqui só o % do Fio B informado.");
    if (x.enq !== 1 && ano > 2028) notas.push("A partir de 2029 a regra é definida pela ANEEL e não está modelada; usado 100% do Fio B.");
    if (x.enq !== 1 && !x.fbS) notas.push("Sem Fio B SCEE válido: FB_ef = 0.");
    if (r.mg < 0) notas.push("Margem líquida negativa: o desconto ao cliente supera a parte da Thopen.");
    if (r.dimp < 0) notas.push("Deságio implícito negativo: o dono recebe mais que a tarifa sem tributos U (VB alto e deságio baixo).");
    $("a-nota").textContent = notas.join(" ");
    arrSens();
    negRender();
  }
  function arrSens() {
    if (!arrUlt) return;
    var x = arrUlt.x, q = arrUlt.G.comp, met = $("a-smet").value, vals = [], mn = Infinity, mx = -Infinity;
    AR.grade_d.forEach(function (d) { vals.push(AR.grade_s.map(function (s) {
      var r = calcArr(x.Dv, x.Ev, x.ic, x.pc, x.fte, x.ftu, s, d, x.c, x.fb, x.fbCom);
      var v = met === "dono" ? r.dono * q : (met === "margem" ? r.mg * q : r.dimp);
      if (v != null && isFinite(v)) { mn = Math.min(mn, v); mx = Math.max(mx, v); } return v; })); });
    var achou = false, perto = function (a, b) { return Math.abs(a - b) < 1e-9; };
    function corDe(v) { if (mx <= mn) return "#EAF0FA"; var k = (v - mn) / (mx - mn);
      var a = [234, 240, 250], b = [47, 85, 151]; return "rgb(" + a.map(function (c, j) { return Math.round(c + (b[j] - c) * k); }).join(",") + ")"; }
    var h = "<table class=\"sens\"><thead><tr><th>Deságio d \\ VB s</th>" + AR.grade_s.map(function (s) { return "<th>" + pct2(s) + "</th>"; }).join("") + "</tr></thead><tbody>";
    AR.grade_d.forEach(function (d, a) {
      h += '<tr><th class="lin">' + pct2(d) + "</th>" + AR.grade_s.map(function (s, b) {
        var v = vals[a][b], at = perto(d, x.d) && perto(s, x.s); if (at) achou = true;
        var k = mx > mn ? (v - mn) / (mx - mn) : 0, txt = met === "dimp" ? pct2(v) : brl(v);
        var neg = v < 0 && met !== "dimp";  // implicito negativo e normal (dono recebe mais que U)  // negativo: fundo vermelho claro, texto vermelho (legivel em qualquer tom)
        var st = neg ? "background:#FDECEA;color:#C0392B;font-weight:600" : "background:" + corDe(v) + ";color:" + (k > 0.55 ? "#fff" : "#1E2533");
        return '<td class="n' + (at ? " atual" : "") + '" style="' + st + '" title="d ' + pct2(d) + ", s " + pct2(s) + '">' + esc(txt) + "</td>"; }).join("") + "</tr>"; });
    $("a-sens").innerHTML = h + "</tbody></table>";
    $("a-sens-nota").textContent = (met === "dimp" ? "Deságio implícito do dono (1 - dono / U, U = R$ " + f2.format(arrUlt.r.U) + "/MWh): com d fixo, cai quando o VB s sobe. " : "R$/mês com " + f2.format(q) + " MWh/mês compensados. ") +
      "Demais entradas como acima (enquadramento, Fio B, desconto ao cliente " + pct2(x.c) + "). Cor mais escura = maior valor. " +
      (achou ? "Contorno: combinação atual." : "A combinação atual (d " + pct2(x.d) + ", s " + pct2(x.s) + ") não está na grade.");
  }
  function arrResumo() {
    if (!arrUlt) return "";
    var r = arrUlt.r, x = arrUlt.x, G = arrUlt.G, q = G.comp, i = I[arrDist] || {};
    var nome = $("a-usina").value.trim(), tp = G.tp;
    var usina = tp === "ufv" ? "UFV de " + f2.format((lerN("a-kwp") || 0) / 1000) + " MWp" : (tp === "cgh" ? "CGH/usina de " + f2.format((lerN("a-kw") || 0) / 1000) + " MW (FC " + pct2((lerN("a-fc") || 0) / 100) + ")" : "Usina com geração informada");
    var gd = ["", "GD I", "GD II", "GD III"][x.enq];
    return [
      "Usina: " + (nome ? nome + ", " : "") + usina,
      "Distribuidora: " + arrDist + (i.uf ? " (" + i.uf + ")" : "") + ", " + gd + ", referência " + dataBr($("a-data").value || D.meta.hoje),
      "Geração média: " + f2.format(G.ger) + " MWh/mês (" + f2.format(G.ger * 12) + " MWh/ano); energia compensada " + f2.format(q) + " MWh/mês",
      "Deságio nominal sobre a tarifa: " + pct2(x.d) + "; VB (parte do benefício fiscal para o dono): " + pct2(x.s),
      "Valor ao dono: R$ " + f2.format(r.dono) + "/MWh compensado (Fio B de R$ " + f2.format(r.cfb) + "/MWh já descontado, " + (x.fbCom ? "com" : "sem") + " impostos)",
      "Pagamento estimado ao dono: R$ " + f2.format(r.dono * q) + "/mês (R$ " + f2.format(r.dono * q * 12) + "/ano)",
      "Deságio implícito, líquido do VB e do Fio B, sobre a tarifa sem tributos (U = R$ " + f2.format(r.U) + "/MWh): " + pct2(r.dimp),
      "Estimativa sem bandeiras, CIP e custo de disponibilidade; tarifas ANEEL vigentes e tributos médios das faturas."
    ].join("\n");
  }
  $("a-copiar").addEventListener("click", function () {
    var txt = arrResumo(), b = $("a-copiar"); if (!txt) return;
    function ok() { b.textContent = "Resumo copiado"; setTimeout(function () { b.textContent = "Copiar resumo"; }, 1800); }
    function alt() { var ta = document.createElement("textarea"); ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); ok(); } catch (e) { window.prompt("Copie o resumo:", txt); } document.body.removeChild(ta); }
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(ok, alt); else alt();
  });
  ["a-tusd", "a-te", "a-fb", "a-pfb", "a-icms", "a-pis", "a-cof", "a-d", "a-s", "a-c", "a-kwp", "a-prod", "a-perdas", "a-kw", "a-fc", "a-mwh", "a-pcomp", "a-usina"].forEach(function (id) {
    $(id).addEventListener("input", arrRender); });
  ["a-fte", "a-ftu", "a-fbmodo"].forEach(function (id) { $(id).addEventListener("change", arrRender); });
  $("a-tipo").addEventListener("change", function () { arrTipo(); arrRender(); });
  $("a-smet").addEventListener("change", arrSens);
  ["a-data", "a-enq"].forEach(function (id) { $(id).addEventListener("change", function () { arrPfb(); arrRender(); }); });
  $("a-reset").addEventListener("click", function () { arrPadroes(true); arrTipo(); arrRender(); });
  $("a-calc").addEventListener("click", function (e) { e.preventDefault(); calcDist = arrDist; if (calcIni) calcPadroes(false); feitos.calculadora = false; mostrar("calculadora"); });

  // ================= VISAO DE NEGOCIACAO (mesmas formulas de resolver_arrendamento e projecao_arrendamento no Python)
  var NG = AR.neg, negIni = false, negPedAuto = true, negFaixaEd = {}, negUlt = null;
  var NFX = [["ab", "Abertura"], ["al", "Alvo"], ["li", "Limite"]];
  function negPadroes(tudo) {
    if (tudo) {
      $("n-var").value = "d"; $("n-ped-u").value = "mes"; $("n-mmin-u").value = "pct"; $("n-mmin").value = escN(NG.mmin * 100, 2);
      $("n-prazo").value = String(NG.prazo); $("n-taxa").value = escN(NG.taxa * 100, 2); $("n-esc").value = "1";
      $("n-reaj").value = escN((NG.ipca12 != null ? NG.ipca12 : 0) * 100, 4); $("n-ped").value = ""; negPedAuto = true;
    }
    $("n-ano").value = String($("a-data").value || D.meta.hoje).slice(0, 4);
    $("n-degr").value = escN((NG.degr[$("a-tipo").value] || 0) * 100, 2);
    negFaixaEd = {};
  }
  function negMontaFaixa() {
    var L = [["d", "Deságio nominal d (%)"], ["s", "VB s (%)"], ["dmwh", "Dono (R$/MWh)"], ["dmes", "Dono (R$/mês)"], ["dimp", "Deságio implícito (1 - dono / U)"],
      ["mmes", "Margem líquida Thopen (R$/mês)"], ["mpct", "Margem líquida Thopen (% de W_I)"], ["sit", "Situação"]];
    var h = '<table class="faixa lado"><thead><tr><th>Item</th>' + NFX.map(function (c) { return '<th id="n-fh-' + c[0] + '">' + c[1] + "</th>"; }).join("") + "</tr></thead><tbody>";
    L.forEach(function (l) {
      h += "<tr><td>" + esc(l[1]) + "</td>" + NFX.map(function (c) {
        var id = "n-f-" + c[0] + "-" + l[0];
        return l[0] === "d" || l[0] === "s" ? '<td class="n"><input type="text" inputmode="decimal" id="' + id + '"></td>' : '<td class="n" id="' + id + '"></td>'; }).join("") + "</tr>"; });
    $("n-faixa").innerHTML = h + "</tbody></table>";
    NFX.forEach(function (c) { ["d", "s"].forEach(function (k) { var id = "n-f-" + c[0] + "-" + k;
      $(id).addEventListener("input", function () { negFaixaEd[id] = true; negRender(); }); }); });
  }
  // Calculo reverso linear: resolve d (s fixo) ou s (d fixo) para um pedido do dono ou uma margem minima (R$/MWh)
  function negResolver(r, x, vr, dono, mg) {
    var u = r.U, w = r.WI, y = r.Y, cfb = r.cfb, c = x.c, d = x.d, s = x.s, o = {ok: false, motivo: "", v: null, dmax: null, vr: vr};
    if (mg != null) o.dmax = w - cfb - c * w - mg;
    if (vr === "d") {
      if (!u) { o.motivo = "U = 0: o deságio não altera o resultado"; return o; }
      d = dono != null ? 1 - (dono + cfb - s * y) / u : (mg + c * w - (1 - s) * y) / u; o.v = d;
    } else {
      if (Math.abs(y) < 1e-12) { o.motivo = "Y = 0: sem benefício fiscal, o VB não altera o resultado"; return o; }
      s = dono != null ? (dono + cfb - (1 - d) * u) / y : 1 - (mg + c * w - d * u) / y; o.v = s;
    }
    var dn = (1 - d) * u + s * y - cfb, th = d * u + (1 - s) * y;
    o.d = d; o.s = s; o.dono = dn; o.th = th; o.mg = th - c * w; o.dimp = u ? 1 - dn / u : null;
    if (o.v < -1e-12 || o.v > 1 + 1e-12) o.motivo = (vr === "d" ? "deságio d" : "VB s") + " fora de 0 a 100%"; else o.ok = true;
    return o;
  }
  // Projecao ano a ano: D, E e Fio B x (1 + reajuste)^t; % do Fio B pela Lei 14.300 (2029+ 100%) ou fixo; GD I = 0
  function negProjecao(x, q, P) {
    var anos = [], tot = {dono: 0, th: 0, mg: 0, mwh: 0, cfb: 0}, vpl = {dono: 0, th: 0, mg: 0};
    for (var t = 0; t < P.prazo; t++) {
      var ano = P.ano + t, f = Math.pow(1 + P.reaj, t), p = x.enq === 1 ? 0 : (P.esc ? escAno(ano) : x.pfb);
      var r = calcArr(x.Dv * f, x.Ev * f, x.ic, x.pc, x.fte, x.ftu, x.s, x.d, x.c, x.fbS * f * p, x.fbCom);
      var qa = q * Math.pow(1 - P.degr, t) * 12, a = {ano: ano, t: t, p: p, U: r.U, WI: r.WI, Y: r.Y, cfb: r.cfb, donoMwh: r.dono, mwh: qa,
        dono: r.dono * qa, th: r.th * qa, mg: r.mg * qa, cfbAno: r.cfb * qa, dimp: r.dimp}, dsc = Math.pow(1 + P.taxa, t + 1);
      anos.push(a);
      tot.dono += a.dono; tot.th += a.th; tot.mg += a.mg; tot.mwh += qa; tot.cfb += a.cfbAno;
      vpl.dono += a.dono / dsc; vpl.th += a.th / dsc; vpl.mg += a.mg / dsc;
    }
    return {anos: anos, tot: tot, vpl: vpl};
  }
  function negParams() {
    var pr = Math.round(lerN("n-prazo") || 0), an = Math.round(lerN("n-ano") || 0);
    return {prazo: Math.max(1, Math.min(50, pr || NG.prazo)), ano: an >= 2000 && an <= 2100 ? an : +String(D.meta.hoje).slice(0, 4),
      reaj: (lerN("n-reaj") || 0) / 100, degr: (lerN("n-degr") || 0) / 100, taxa: (lerN("n-taxa") || 0) / 100, esc: $("n-esc").value === "1"};
  }
  function negLimpa(msg) {
    negUlt = null;
    ["n-kpis", "n-rev-ped", "n-rev-mg", "n-proj", "n-proj-kpis"].forEach(function (id) { $(id).innerHTML = ""; });
    $("n-kpis").innerHTML = '<div class="card erro">' + esc(msg) + "</div>";
    $("n-faixa-nota").textContent = ""; $("n-proj-nota").textContent = "";
    if (graficos["g-neg"]) { graficos["g-neg"].destroy(); delete graficos["g-neg"]; }
  }
  function negRender() {
    if (!negIni) { negIni = true; negMontaFaixa(); negPadroes(true); }
    if (!arrUlt) { negLimpa("Informe TUSD e TE e alíquotas abaixo de 100% na configuração acima."); return; }
    var r = arrUlt.r, x = arrUlt.x, G = arrUlt.G, q = G.comp, gd = ["", "GD I", "GD II", "GD III"][x.enq];
    if (negPedAuto && !$("n-ped").value) { $("n-ped").value = escN(Math.round(r.dono * q * 1.1 / 100) * 100, 0); negPedAuto = false; }
    var mes = function (v) { return brl(v * q) + "/mês"; };
    // 1. proposta ao dono
    var kp = [
      ["Recebimento do dono", mes(r.dono), "R$ " + f2.format(r.dono) + "/MWh; " + brl(r.dono * q * 12) + "/ano", r.dono < 0],
      ["% do valor total (dono / W_I)", pct2(r.WI ? r.dono / r.WI : null), "W_I = R$ " + f2.format(r.WI) + "/MWh (valor ao cliente com tributos evitados)", false],
      ["% da tarifa sem tributos (dono / U)", pct2(r.U ? r.dono / r.U : null), "U = TUSD + TE = R$ " + f2.format(r.U) + "/MWh", false],
      ["Deságio nominal / implícito", pct2(x.d) + " / " + pct2(r.dimp), "nominal só sobre U; implícito = 1 - dono / U, líquido do VB e do Fio B", false],
      ["Fio B pago pelo dono", mes(r.cfb), "R$ " + f2.format(r.cfb) + "/MWh (" + gd + (x.enq === 1 ? ", sem Fio B" : ", " + pct2(x.pfb) + " do Fio B SCEE, " + (x.fbCom ? "com" : "sem") + " impostos") + ")", false]
    ];
    $("n-kpis").innerHTML = kp.map(function (k) { return '<div class="card kpi' + (k[3] ? " negativo" : "") + '"><div class="rot">' + esc(k[0]) + '</div><div class="val">' + esc(k[1]) + '</div><div class="sub">' + esc(k[2]) + "</div></div>"; }).join("");
    // 2. calculo reverso
    var vr = $("n-var").value, fixo = vr === "d" ? "VB s = " + pct2(x.s) : "deságio d = " + pct2(x.d);
    var pv = lerN("n-ped"), pu = $("n-ped-u").value, mv = lerN("n-mmin"), mu = $("n-mmin-u").value;
    var pedMwh = pv == null ? null : (pu === "mwh" ? pv : (q > 0 ? pv / q : null));
    var mgMwh = mv == null ? null : (mu === "pct" ? mv / 100 * r.WI : (mu === "mwh" ? mv : (q > 0 ? mv / q : null)));
    var linha = function (a, b, c) { return "<tr><td>" + esc(a) + "</td>" + td(b, c) + "</tr>"; };
    var mgTxt = function (v) { return "R$ " + f2.format(v) + "/MWh; " + mes(v) + "; " + pct2(r.WI ? v / r.WI : null) + " de W_I"; };
    var P1 = null, P2 = null;
    $("n-ped-tit").textContent = "Pedido do dono: " + (vr === "d" ? "deságio necessário" : "VB necessário") + " (" + fixo + ")";
    if (pedMwh == null) $("n-rev-ped").innerHTML = '<div class="erro">' + (pv != null && q <= 0 ? "Sem energia compensada: informe o pedido em R$/MWh." : "Informe o valor pedido pelo dono.") + "</div>";
    else {
      P1 = negResolver(r, x, vr, pedMwh, null);
      var L1 = [linha("Pedido do dono", esc("R$ " + f2.format(pedMwh) + "/MWh; " + mes(pedMwh)))];
      if (P1.v == null) L1.push(linha("Resultado", "Sem solução: " + esc(P1.motivo), "sem-sol"));
      else {
        L1.push(linha((vr === "d" ? "Deságio d necessário" : "VB s necessário"), "<b>" + esc(pct2(P1.v)) + "</b>" + (P1.ok ? "" : " (sem solução: " + esc(P1.motivo) + ")"), P1.ok ? "" : "sem-sol"));
        L1.push(linha("Deságio implícito resultante", esc(pct2(P1.dimp))));
        L1.push(linha("Margem líquida da Thopen", esc(mgTxt(P1.mg)), P1.mg < 0 ? "negativo" : ""));
        var viav = P1.ok && (mgMwh == null || P1.mg >= mgMwh - 1e-9);
        L1.push(linha("Viável?", P1.ok ? (mgMwh == null ? "sem margem mínima informada" : (viav ? "<b>Sim</b>, margem igual ou acima da mínima" :
          "<b>Não</b>, margem abaixo da mínima em " + esc(mes(mgMwh - P1.mg)))) : "<b>Não</b>, sem solução", viav ? "" : "sem-sol"));
      }
      $("n-rev-ped").innerHTML = tabela(["Item", "Valor"], L1, "", "lado");
    }
    $("n-mg-tit").textContent = "Margem mínima da Thopen: " + (vr === "d" ? "deságio mínimo" : "VB máximo") + " e teto ao dono (" + fixo + ")";
    if (mgMwh == null) $("n-rev-mg").innerHTML = '<div class="erro">' + (mv != null && q <= 0 ? "Sem energia compensada: informe a margem em R$/MWh ou % de W_I." : "Informe a margem mínima.") + "</div>";
    else {
      P2 = negResolver(r, x, vr, null, mgMwh);
      var L2 = [linha("Margem mínima (líquida de c = " + pct2(x.c) + ")", esc(mgTxt(mgMwh)))];
      if (P2.v == null) L2.push(linha("Resultado", "Sem solução: " + esc(P2.motivo), "sem-sol"));
      else L2.push(linha(vr === "d" ? "Deságio d mínimo" : "VB s máximo", "<b>" + esc(pct2(P2.v)) + "</b>" + (P2.ok ? "" : " (sem solução: " + esc(P2.motivo) + ")"), P2.ok ? "" : "sem-sol"));
      L2.push(linha("Pagamento máximo ao dono (teto)", "<b>" + esc("R$ " + f2.format(P2.dmax) + "/MWh; " + mes(P2.dmax) + "; " + brl(P2.dmax * q * 12) + "/ano") + "</b>", P2.dmax < 0 ? "negativo" : ""));
      L2.push(linha("Deságio implícito correspondente", esc(pct2(r.U ? 1 - P2.dmax / r.U : null))));
      L2.push(linha("Proposta atual x teto", esc("atual " + mes(r.dono) + "; " + (P2.dmax - r.dono >= 0 ? "folga de " + mes(P2.dmax - r.dono) : "acima do teto em " + mes(r.dono - P2.dmax)))));
      $("n-rev-mg").innerHTML = tabela(["Item", "Valor"], L2, "", "lado");
    }
    // 3. faixa de negociacao
    var lim = {d: x.d, s: x.s};
    // limite arredondado a 0,01 p.p. para o lado seguro (d para cima, s para baixo): a margem fica >= a minima
    if (P2 && P2.v != null) { var vl = Math.max(0, Math.min(1, P2.v));
      if (vr === "d") lim.d = Math.min(1, Math.ceil(vl * 10000 - 1e-7) / 10000); else lim.s = Math.max(0, Math.floor(vl * 10000 + 1e-7) / 10000); }
    var padr = {ab: {d: Math.min(1, x.d + NG.abertura), s: x.s}, al: {d: x.d, s: x.s}, li: lim}, FX = {};
    NFX.forEach(function (c) {
      var k = c[0], o = {};
      ["d", "s"].forEach(function (z) { var id = "n-f-" + k + "-" + z;
        if (!negFaixaEd[id]) $(id).value = escN(padr[k][z] * 100, 2);
        var v = lerN(id); o[z] = v == null ? padr[k][z] : v / 100; });
      var rr = calcArr(x.Dv, x.Ev, x.ic, x.pc, x.fte, x.ftu, o.s, o.d, x.c, x.fb, x.fbCom), abx = mgMwh != null && rr.mg < mgMwh - 1e-9;
      FX[k] = {d: o.d, s: o.s, r: rr, abx: abx};
      var put = function (z, v) { var e = $("n-f-" + k + "-" + z); e.textContent = v; e.className = "n" + (abx ? " abaixo" : ""); };
      put("dmwh", num(rr.dono)); put("dmes", brl(rr.dono * q)); put("dimp", pct2(rr.dimp)); put("mmes", brl(rr.mg * q));
      put("mpct", pct2(rr.WI ? rr.mg / rr.WI : null)); put("sit", mgMwh == null ? "-" : (abx ? "abaixo da margem mínima" : "ok"));
      $("n-fh-" + k).className = abx ? "abaixo" : "";
      ["d", "s"].forEach(function (z) { $("n-f-" + k + "-" + z).parentNode.className = "n" + (abx ? " abaixo" : ""); });
    });
    $("n-faixa-nota").textContent = "Abertura: deságio atual + " + f0.format(NG.abertura * 100) + " p.p.; Alvo: configuração atual; Limite: " +
      (vr === "d" ? "deságio mínimo" : "VB máximo") + " da margem mínima, arredondado a 0,01 p.p. para o lado seguro" + (P2 && P2.v != null && !P2.ok ? " (sem solução em 0 a 100%: valor limitado à faixa)" : "") +
      ". Edite d e s em qualquer coluna; em vermelho, margem abaixo da mínima. Recalcular faixa volta aos padrões.";
    // 4. projecao
    var P = negParams(), PJ = negProjecao(x, q, P), A = PJ.anos, ult = A[A.length - 1];
    var kq = [
      ["Dono no ano 1 (" + A[0].ano + ")", brl(A[0].dono / 12) + "/mês", brl(A[0].dono) + "/ano; R$ " + f2.format(A[0].donoMwh) + "/MWh"],
      ["Dono no último ano (" + ult.ano + ")", brl(ult.dono / 12) + "/mês", brl(ult.dono) + "/ano; R$ " + f2.format(ult.donoMwh) + "/MWh"],
      ["Total do contrato ao dono", brl(PJ.tot.dono), P.prazo + " anos; Thopen (margem líquida) " + brl(PJ.tot.mg)],
      ["VPL do dono", brl(PJ.vpl.dono), "taxa de " + pct2(P.taxa) + " a.a., fluxos no fim de cada ano"],
      ["VPL da Thopen (margem líquida)", brl(PJ.vpl.mg), "parte da Thopen antes do desconto: " + brl(PJ.vpl.th)]
    ];
    $("n-proj-kpis").innerHTML = kq.map(function (k) { return '<div class="card kpi"><div class="rot">' + esc(k[0]) + '</div><div class="val">' + esc(k[1]) + '</div><div class="sub">' + esc(k[2]) + "</div></div>"; }).join("");
    var lin = A.map(function (a) { return "<tr><td>" + a.ano + "</td>" + td(pct2(a.p)) + td(num(a.U)) + td(num(a.Y)) + td(num(a.cfb)) + td(num(a.donoMwh), a.donoMwh < 0 ? "negativo" : "") +
      td(f2.format(a.mwh)) + td(brl(a.dono), a.dono < 0 ? "negativo" : "") + td(brl(a.th)) + td(brl(a.mg), a.mg < 0 ? "negativo" : "") + td(pct2(a.dimp)) + "</tr>"; });
    lin.push('<tr class="tot"><td>Total</td>' + td("") + td("") + td("") + td("") + td("") + td(f2.format(PJ.tot.mwh)) + td(brl(PJ.tot.dono)) + td(brl(PJ.tot.th)) + td(brl(PJ.tot.mg)) + td("") + "</tr>");
    lin.push('<tr class="tot"><td>VPL ' + esc(pct2(P.taxa)) + " a.a.</td>" + td("") + td("") + td("") + td("") + td("") + td("") + td(brl(PJ.vpl.dono)) + td(brl(PJ.vpl.th)) + td(brl(PJ.vpl.mg)) + td("") + "</tr>");
    $("n-proj").innerHTML = tabela(["Ano", "% Fio B", "U (R$/MWh)", "Y (R$/MWh)", "Fio B do dono (R$/MWh)", "Dono (R$/MWh)", "Energia compensada (MWh/ano)",
      "Dono (R$/ano)", "Thopen (R$/ano)", "Margem líquida (R$/ano)", "Deságio implícito"], lin, "", "lado");
    var nt = ["Ano a ano: D, E e Fio B SCEE x (1 + " + pct2(P.reaj) + ")^t" + (NG.ipca12 != null ? " (padrão: IPCA 12 meses, " + pct2(NG.ipca12) + ")" : "") +
      "; tributos, flags, d = " + pct2(x.d) + ", s = " + pct2(x.s) + " e c = " + pct2(x.c) + " constantes; energia compensada x (1 - " + pct2(P.degr) + ")^t."];
    if (x.enq === 1) nt.push("GD I: sem Fio B em todos os anos.");
    else if (P.esc) nt.push("Fio B pela Lei 14.300: 2026 60%, 2027 75%, 2028 90%; a partir de 2029 a regra será definida pela ANEEL, aqui aproximada por 100%." + (A.length > 1 && A[1].donoMwh < A[0].donoMwh ? " A subida do % do Fio B reduz o recebimento do dono por MWh." : ""));
    else nt.push("Fio B fixo em " + pct2(x.pfb) + " (escalonamento desligado).");
    if (x.enq === 3) nt.push("GD III: só o Fio B é modelado; pela Lei 14.300 a usina paga ainda 40% do Fio A, TFSEE e P&D/EE.");
    $("n-proj-nota").textContent = nt.join(" ");
    grafico("g-neg", {type: "bar", data: {labels: A.map(function (a) { return String(a.ano); }), datasets: [
      {label: "Dono (R$/ano)", data: A.map(function (a) { return a.dono; }), backgroundColor: "#2F5597"},
      {label: "Thopen, margem líquida (R$/ano)", data: A.map(function (a) { return a.mg; }), backgroundColor: "#E07B39"}]},
      options: {responsive: true, maintainAspectRatio: false, locale: "pt-BR", plugins: {legend: {position: "bottom", labels: {boxWidth: 14}},
        tooltip: {callbacks: {label: function (c) { return c.dataset.label + ": " + brl(c.parsed.y); }}}},
        scales: {y: eixoY(function (v) { return "R$ " + f0.format(v); }), x: {grid: {display: false}}}}});
    negUlt = {r: r, x: x, G: G, q: q, P1: P1, P2: P2, pedMwh: pedMwh, mgMwh: mgMwh, vr: vr, FX: FX, P: P, PJ: PJ, gd: gd};
  }
  function negUsina() {
    var G = arrUlt.G, tp = G.tp, nome = $("a-usina").value.trim();
    var u = tp === "ufv" ? "UFV de " + f2.format((lerN("a-kwp") || 0) / 1000) + " MWp" : (tp === "cgh" ? "CGH de " + f2.format((lerN("a-kw") || 0) / 1000) + " MW" : "usina com geração informada");
    return (nome ? nome + ", " : "") + u + ", geração média de " + f2.format(G.ger) + " MWh/mês";
  }
  function negAnoTxt(A, k) { var a = A[k - 1]; return a ? "ano " + k + " (" + a.ano + "): R$ " + f2.format(a.dono) + "/ano (R$ " + f2.format(a.dono / 12) + "/mês)" : null; }
  function negProposta() {
    if (!negUlt) return "";
    var n = negUlt, x = n.x, i = I[arrDist] || {}, A = n.PJ.anos, P = n.P;
    var ev = [negAnoTxt(A, 1), negAnoTxt(A, 3), negAnoTxt(A, 5)].filter(Boolean);
    var fb = x.enq === 1 ? "sem Fio B (GD I)" : (P.esc ? "Fio B pago pelo proprietário conforme o escalonamento da Lei 14.300 (60% em 2026, 75% em 2027, 90% em 2028 e 100% a partir de 2029, aproximação)" : "Fio B pago pelo proprietário fixo em " + pct2(x.pfb));
    return [
      "Proposta de arrendamento",
      "Usina: " + negUsina(),
      "Distribuidora: " + arrDist + (i.uf ? " (" + i.uf + ")" : "") + "; modalidade " + n.gd,
      "Condições: deságio de " + pct2(x.d) + " sobre a tarifa sem tributos (TUSD + TE) e VB de " + pct2(x.s) + " do benefício fiscal ao proprietário",
      "Valor ao proprietário no ano 1 (" + A[0].ano + "): R$ " + f2.format(A[0].donoMwh) + "/MWh compensado, cerca de R$ " + f2.format(A[0].dono / 12) + "/mês",
      "Evolução prevista: " + ev.join("; ") + "; total em " + P.prazo + " anos: R$ " + f2.format(n.PJ.tot.dono),
      "Premissas: estimativa com as tarifas ANEEL vigentes e tributos médios das faturas, sem bandeiras, CIP nem custo de disponibilidade; reajuste anual da tarifa de " + pct2(P.reaj) +
        "; " + fb + (P.degr ? "; degradação da geração de " + pct2(P.degr) + " ao ano" : "") + ". Os valores dependem da energia efetivamente compensada."
    ].join("\n");
  }
  function negInterna() {
    if (!negUlt) return "";
    var n = negUlt, x = n.x, r = n.r, q = n.q, P2 = n.P2, P1 = n.P1, PJ = n.PJ, nv = n.vr === "d" ? "deságio d" : "VB s";
    var m = function (v) { return "R$ " + f2.format(v * q) + "/mês"; };
    var L = [negProposta(), "", "Análise interna (não enviar ao proprietário)",
      "Configuração atual: d " + pct2(x.d) + ", s " + pct2(x.s) + ", c " + pct2(x.c) + "; dono R$ " + f2.format(r.dono) + "/MWh (" + m(r.dono) + "); margem líquida Thopen R$ " + f2.format(r.mg) + "/MWh (" + m(r.mg) + ", " + pct2(r.WI ? r.mg / r.WI : null) + " de W_I)"];
    if (P2) L.push("Margem mínima: R$ " + f2.format(n.mgMwh) + "/MWh (" + m(n.mgMwh) + ", " + pct2(r.WI ? n.mgMwh / r.WI : null) + " de W_I); " +
      (P2.v == null ? "sem solução (" + P2.motivo + ")" : (n.vr === "d" ? "deságio mínimo " : "VB máximo ") + pct2(P2.v) + (P2.ok ? "" : " (sem solução: " + P2.motivo + ")")) +
      "; pagamento máximo ao dono R$ " + f2.format(P2.dmax) + "/MWh (" + m(P2.dmax) + ")");
    if (P1) L.push("Pedido do dono: R$ " + f2.format(n.pedMwh) + "/MWh (" + m(n.pedMwh) + "); " +
      (P1.v == null ? "sem solução (" + P1.motivo + ")" : nv + " necessário " + pct2(P1.v) + (P1.ok ? "" : " (sem solução)") + "; margem Thopen " + m(P1.mg) + " (" + pct2(r.WI ? P1.mg / r.WI : null) + " de W_I)" +
        (n.mgMwh != null && P1.ok ? (P1.mg >= n.mgMwh - 1e-9 ? "; viável" : "; abaixo da margem mínima") : "")));
    L.push("Faixa: " + NFX.map(function (c) { var f = n.FX[c[0]]; return c[1] + " d " + pct2(f.d) + ", s " + pct2(f.s) + ", dono " + m(f.r.dono) + ", margem " + m(f.r.mg) + (f.abx ? " (abaixo da mínima)" : ""); }).join("; "));
    L.push("Projeção " + n.P.prazo + " anos: total dono R$ " + f2.format(PJ.tot.dono) + ", total margem Thopen R$ " + f2.format(PJ.tot.mg) + "; VPL a " + pct2(n.P.taxa) + " a.a.: dono R$ " + f2.format(PJ.vpl.dono) + ", Thopen (margem líquida) R$ " + f2.format(PJ.vpl.mg));
    return L.join("\n");
  }
  function copiarTexto(b, txt, rotOk) {
    if (!txt) return; var orig = b.textContent;
    function ok() { b.textContent = rotOk; setTimeout(function () { b.textContent = orig; }, 1800); }
    function alt() { var ta = document.createElement("textarea"); ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); ok(); } catch (e) { window.prompt("Copie o texto:", txt); } document.body.removeChild(ta); }
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(ok, alt); else alt();
  }
  $("n-copiar").addEventListener("click", function () { copiarTexto($("n-copiar"), negProposta(), "Proposta copiada"); });
  $("n-copiar-int").addEventListener("click", function () { copiarTexto($("n-copiar-int"), negInterna(), "Análise copiada"); });
  ["n-ped", "n-mmin", "n-prazo", "n-ano", "n-reaj", "n-degr", "n-taxa"].forEach(function (id) { $(id).addEventListener("input", negRender); });
  ["n-var", "n-ped-u", "n-mmin-u", "n-esc"].forEach(function (id) { $(id).addEventListener("change", negRender); });
  $("n-faixa-reset").addEventListener("click", function () { negFaixaEd = {}; negRender(); });
  $("a-data").addEventListener("change", function () { $("n-ano").value = String($("a-data").value || D.meta.hoje).slice(0, 4); negRender(); });
  $("a-tipo").addEventListener("change", function () { $("n-degr").value = escN((NG.degr[$("a-tipo").value] || 0) * 100, 2); negRender(); });
  $("a-reset").addEventListener("click", function () { negPadroes(true); negRender(); });

  // ================= TABELA THOPEN (replica a planilha Tarifas.xlsx com a tarifa ANEEL vigente)
  var tEnq = 1, tOrd = {k: 0, asc: true};
  var TCOLS = [
    ["Distribuidora", function (x) { return x.d; }, null],
    ["UF", function (x) { return (I[x.d] || {}).uf || ""; }, null],
    ["Vigência", function (x) { return x.m.ini; }, dataBr],
    ["D (TUSD)", function (x) { return x.m.tusd; }, num], ["E (TE)", function (x) { return x.m.te; }, num],
    ["ICMS", function (x) { return x.m.tx.i; }, pct2], ["PIS+COFINS", function (x) { return x.m.tx.pis + x.m.tx.cof; }, pct2],
    ["ICMS na compensada (TE/TUSD)", function (x) { return x.m.tx.te * 2 + x.m.tx.tu; }, function (v, x) { return sn(x.m.tx.te) + " / " + sn(x.m.tx.tu); }],
    ["s", function (x) { return x.m.tx.s; }, pct2],
    ["U", function (x) { return x.r ? x.r.U : null; }, num], ["V", function (x) { return x.r ? x.r.V : null; }, num],
    ["FB_ef", function (x) { return x.r ? x.r.fb : null; }, num, 2],
    ["W", function (x) { return x.r ? x.r.W : null; }, num], ["Y", function (x) { return x.r ? x.r.Y : null; }, num],
    ["X", function (x) { return x.r ? x.r.X : null; }, num], ["Z", function (x) { return x.r ? x.r.Z : null; }, num],
    ["TUSD+TE na planilha", function (x) { return x.rt; }, num],
    ["Diferença (U - planilha)", function (x) { return x.dif; }, function (v, x) { return v == null ? "-" : '<span class="' + cls(v) + '">' + (v > 0 ? "+" : "") + num(v) + " (" + pct(x.rt ? v / x.rt : null) + ")</span>"; }]
  ];
  function renderThopen() {
    var q = $("t-busca").value.trim(), so = $("t-ativas").checked, sp = $("t-plan").checked, gd2 = tEnq === 2;
    var cols = TCOLS.filter(function (c) { return !c[3] || c[3] === tEnq; });
    var itens = D.lista.filter(function (d) { return M[d] && M[d].tx && (!q || casa(d, q)) && (!so || I[d].a) && (!sp || D.ref[d]); })
      .map(function (d) { var m = M[d], ref = D.ref[d], rt = ref && ref[1] != null && ref[2] != null ? ref[1] + ref[2] : null;
        return {d: d, m: m, r: txCalcM(m, gd2), rt: rt, dif: rt != null && m.tot != null ? m.tot - rt : null}; });
    var c = cols[tOrd.k] || cols[0];
    itens.sort(function (x, y) {
      var a = c[1](x), b = c[1](y), r;
      if (a == null && b == null) r = 0; else if (a == null) return 1; else if (b == null) return -1;
      else r = typeof a === "string" ? a.localeCompare(b, "pt-BR") : a - b;
      return tOrd.asc ? r : -r; });
    $("t-tit").textContent = "Tabela Thopen: " + (gd2 ? "GD II (FB_ef = Fio B efetivo da vigência atual)" : "GD I, como na planilha (FB_ef = 0)") + ", " + itens.length + " distribuidoras";
    var seta = function (j) { return tOrd.k === j ? (tOrd.asc ? " ▲" : " ▼") : ""; };
    var h = "<table><thead><tr>" + cols.map(function (c, j) { return '<th class="ord' + (j ? "" : " nome") + '" data-tk="' + j + '">' + esc(c[0]) + seta(j) + "</th>"; }).join("") + "</tr></thead><tbody>";
    h += itens.map(function (x) {
      return '<tr class="' + (ehPort(x.d) ? "port" : "") + '">' + cols.map(function (c, j) {
        var v = c[1](x);
        if (!j) return '<td class="nome"><a href="#calculadora" data-calc="' + esc(x.d) + '" title="Abrir na Calculadora">' + esc(rot(x.d)) + "</a>" + (I[x.d].a ? "" : ' <span class="mut">(inativa)</span>') + "</td>";
        if (j === 1) return "<td>" + esc(v) + "</td>";
        var txt = c[2] ? c[2](v, x) : esc(v);
        return td(c[0] === "V" || c[0] === "W" ? "<b>" + txt + "</b>" : txt);
      }).join("") + "</tr>"; }).join("");
    $("t-tab").innerHTML = itens.length ? h + "</tbody></table>" : '<div class="erro">Nenhuma distribuidora para o filtro.</div>';
    $("t-nota").textContent = "R$/MWh. Alíquotas, flags de ICMS e repasse s: padrões do painel (fonte de cada um na Calculadora e no Excel, aba Impostos). " +
      "TUSD+TE na planilha: referência fixa da planilha Tarifas.xlsx do usuário (" + Object.keys(D.ref).length + " distribuidoras); diferença = U (ANEEL vigente) menos a planilha. " +
      (gd2 ? "GD II: sem Fio B SCEE válido, W, Y, X e Z ficam em branco. " : "") + "Clique no nome para abrir na Calculadora. Mesma tabela no Excel (aba Calculadora (tabela)).";
  }
  $("t-tab").addEventListener("click", function (e) {
    var a = e.target.closest("[data-calc]");
    if (a) { e.preventDefault(); calcDist = a.getAttribute("data-calc"); if (calcIni) calcPadroes(false); feitos.calculadora = false; mostrar("calculadora"); return; }
    var th = e.target.closest("th[data-tk]"); if (!th) return;
    var k = parseInt(th.getAttribute("data-tk"), 10);
    if (tOrd.k === k) tOrd.asc = !tOrd.asc; else { tOrd.k = k; tOrd.asc = k < 2; }
    renderThopen(); });
  $("t-busca").addEventListener("input", renderThopen);
  $("t-ativas").addEventListener("change", renderThopen);
  $("t-plan").addEventListener("change", renderThopen);
  document.querySelectorAll("[data-tenq]").forEach(function (b) { b.addEventListener("click", function () {
    tEnq = +b.getAttribute("data-tenq"); tOrd.k = Math.min(tOrd.k, 9);
    document.querySelectorAll("[data-tenq]").forEach(function (x) { x.classList.toggle("ativo", x === b); }); renderThopen(); }); });

  var h = (location.hash || "").replace("#", "");
  mostrar(RENDER[h] ? h : "geral");
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------- main

def gerar_paineis(base: Path, visoes: dict, log_path: Path, cfg: dict, agora: datetime, estado: dict) -> None:
    visoes["imp_meta"] = meta_impostos(base)
    visoes["imp"] = tabela_impostos(visoes, base)
    visoes["imp_faturas"] = impostos_faturas_excel(base)
    visoes["prod"] = ler_produtividade(base)
    painel = base / f"{PAINEL}.xlsx"
    gerar_painel(painel, visoes, log_path, cfg, agora, estado)
    print(f"Painel gerado: {painel}")
    painel_html = base / f"{PAINEL}.html"
    gerar_html(painel_html, visoes, log_path, cfg, agora, estado)
    print(f"Painel HTML gerado: {painel_html}")


def no_escopo_fiob(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """So vigencias de Fio B iniciadas a partir de fiob_ano_inicial."""
    return df[df["inicio"] >= corte_fiob(cfg)].reset_index(drop=True)


def serie_vazia(valores: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=DIMS + valores + ["chave"])


def escopo_serie(serie: str, df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Linhas da serie dentro do escopo atual do config.json (Fio B: desde fiob_ano_inicial; Grupo A: subgrupos
    configurados e vigencias desde grupo_a_ano_inicial)."""
    if serie == "fiob":
        return no_escopo_fiob(df, cfg)
    if serie in ("tarifa_a", "fiob_a"):
        esc = escopo_grupo_a(cfg)
        return no_escopo_a(df, esc["subgrupos"], esc["fiob_inicio_min" if serie == "fiob_a" else "inicio_min"])
    return df


def canonicas(serie: str, df: pd.DataFrame) -> pd.DataFrame:
    """Linhas 'cheias' de cada serie (as que aparecem no alerta); o resto conta como 'outras'."""
    if serie == "tarifa":
        return canonico(df)
    if serie == "fiob":
        return canonico(df, "SCEE")
    return _canon_a(df[df["detalhe"] == NA])


def processar_serie(serie: str, antigo: pd.DataFrame, novo: pd.DataFrame, cfg: dict, esc_ant: dict,
                    log_path: Path | None, agora: datetime, aceitar_queda: bool = False) -> dict:
    """Compara uma serie com o snapshot cumulativo, aplica a guarda de queda e grava o log (se log_path; o main
    passa None e grava o log de todas as series so depois que nenhuma guarda bloqueou).

    Retorna {"snapshot", "mudancas" (para o alerta: so novas e alteradas), "n_log", "fora", "salvar",
    "novos", "alterados", "removidos", "voltaram"}. Remocoes da fonte nao entram no alerta nem na contagem
    de "linhas que mudaram"; vao para o log como removida_na_fonte e ficam no snapshot.
    """
    cfg_s = SERIES[serie]
    valores = cfg_s["valores"]
    antigo = com_marca(antigo)
    fora = 0
    if serie != "tarifa":
        # Fio B: vigencias anteriores a fiob_ano_inicial saem do escopo sem virar "removidas" (ex.: snapshot da
        # versao com historico completo do Fio B). Grupo A: idem para subgrupos tirados do config.json.
        n0 = len(antigo)
        antigo = adicionar_chave(escopo_serie(serie, antigo, cfg))
        fora = n0 - len(antigo)
        if fora:
            print(f"  {cfg_s['rotulo']}: {fora} linhas do snapshot fora do escopo do config.json (sem alerta)")
    if cfg_s.get("grupo_a"):
        # Escopo anterior do Grupo A (estado.json): linhas que so entram porque o config.json ampliou o escopo
        # (subgrupo novo, ano inicial menor) viram linha de base, sem alerta.
        ini_ant = esc_ant["fiob_inicio_min" if serie == "fiob_a" else "inicio_min"]
        amplia = novo[~novo["subgrupo"].isin(esc_ant["subgrupos"]) | (novo["inicio"] < ini_ant)]
        amplia = amplia[~amplia["chave"].isin(antigo["chave"])]
        if len(amplia):
            antigo = com_marca(pd.concat([antigo, amplia], ignore_index=True))
            fora += len(amplia)
            print(f"  {cfg_s['rotulo']}: {len(amplia)} linhas entram por ampliacao do escopo "
                  "(linha de base, sem alerta)")
    n_ativas = len(ativas(antigo))
    novos, alterados, removidos, voltaram, snap = comparar(antigo, novo, valores, agora.strftime("%Y-%m-%d"))
    motivo = queda_suspeita(n_ativas, len(novo), len(removidos))
    if motivo:
        if not aceitar_queda:
            raise RuntimeError(f"{cfg_s['rotulo']}: {motivo}. Provavel arquivo incompleto na ANEEL; nada foi salvo. "
                               "Se a remocao for legitima, rode com --aceitar-queda.")
        print(f"  [aviso] {cfg_s['rotulo']}: {motivo}; aceito por --aceitar-queda")
    n_log = len(novos) + len(alterados) + len(removidos) + len(voltaram)
    if log_path is not None:
        registrar_log(log_path, serie, novos, alterados, removidos, valores, agora, voltaram)
    print(f"  {cfg_s['rotulo']}: {len(novos)} novas, {len(alterados)} alteradas, {len(removidos)} removidas da fonte "
          f"(mantidas no historico), {len(voltaram)} voltaram a fonte; {n_ativas} ativas antes, {len(novo)} agora")
    chaves = list(novos["chave"]) + list(alterados["chave"])
    mud = novo[novo["chave"].isin(chaves)]
    canon = canonicas(serie, mud)
    antes = {}
    if len(alterados):
        ant = alterados[[c + "_anterior" for c in valores]].sum(axis=1, min_count=1)
        antes = dict(zip(alterados["chave"], ant))
    mud_s = {"chaves": chaves, "antes": antes, "outras": max(len(chaves) - len(canon), 0)}
    if cfg_s.get("grupo_a"):
        # vigencias (distribuidora, subgrupo, modalidade, inicio) com mudanca, para a secao Grupo A do alerta
        grupos = lambda d: set(zip(_id(d), d["subgrupo"], d["modalidade"], d["inicio"]))  # noqa: E731
        mud_s["grupos"] = grupos(canon if serie == "tarifa_a" else _canon_a(mud))
        mud_s["antes_grupos"] = grupos(canon[canon["chave"].isin(list(antes))])
    return {"snapshot": snap, "mudancas": mud_s, "n_log": n_log, "fora": fora, "salvar": bool(n_log or fora),
            "novos": novos, "alterados": alterados, "removidos": removidos, "voltaram": voltaram}


def _snapshot_git(base: Path, ref: str, arquivo: str, valores: list[str]) -> pd.DataFrame | None:
    """Snapshot data/<arquivo> numa versao do git (None se o arquivo nao existia nela)."""
    import io
    import subprocess
    r = subprocess.run(["git", "show", f"{ref}:data/{arquivo}"], cwd=base, capture_output=True, timeout=120)
    if r.returncode != 0:
        return None
    df = pd.read_csv(io.BytesIO(r.stdout), dtype=str, keep_default_na=False, encoding="utf-8")
    for c in valores:
        df[c] = pd.to_numeric(df[c], errors="coerce").round(6)
    return adicionar_chave(com_marca(df))


def migrar_historico(base: Path, cfg: dict, ref: str, data_remocao: str) -> int:
    """Migracao unica: reinsere no snapshot as linhas que estavam na versao `ref` do git (ex.: 7fe4a35^) e nao
    estao no snapshot atual, marcadas com removida_fonte_em = data_remocao, e troca no log o tipo antigo
    "removida" por "removida_na_fonte". Idempotente (rodar de novo nao reinsere nada)."""
    dados = base / "data"
    print(f"Migracao do historico: snapshots de {ref}, removida_fonte_em = {data_remocao}")
    for serie, cfg_s in SERIES.items():
        caminho = dados / cfg_s["arquivo"]
        atual = carregar(caminho, cfg_s["valores"])
        velho = _snapshot_git(base, ref, cfg_s["arquivo"], cfg_s["valores"])
        if atual is None or velho is None:
            print(f"  {cfg_s['rotulo']}: sem snapshot atual ou em {ref}; nada a fazer")
            continue
        if serie != "tarifa":
            velho = escopo_serie(serie, velho, cfg)
        falta = velho[~velho["chave"].isin(atual["chave"])].copy()
        falta[COL_REMOVIDA] = falta[COL_REMOVIDA].where(falta[COL_REMOVIDA] != "", data_remocao)
        print(f"  {cfg_s['rotulo']}: {len(falta)} linhas reinseridas (snapshot {len(atual)} -> "
              f"{len(atual) + len(falta)}; anos de inicio: "
              + (", ".join(f"{a}: {n}" for a, n in falta["inicio"].str[:4].value_counts().sort_index().items())
                 or "-") + ")")
        if len(falta) or COL_REMOVIDA not in pd.read_csv(caminho, nrows=0).columns:
            salvar(ordenar_snapshot(pd.concat([atual, falta], ignore_index=True)), caminho, cfg_s["valores"])
    log_path = dados / "log_mudancas.csv"
    if log_path.exists():
        log = pd.read_csv(log_path, dtype=str, keep_default_na=False, encoding="utf-8")
        m = log["tipo"] == "removida"
        if m.any():
            log.loc[m, "tipo"] = TIPO_REMOVIDA
            log.to_csv(log_path, index=False, encoding="utf-8")
        print(f"  log: {int(m.sum())} linhas 'removida' -> '{TIPO_REMOVIDA}'")
    return 0


def so_painel(base: Path, cfg: dict, agora: datetime) -> int:
    """Regera Excel e HTML a partir dos CSVs salvos (offline). Nao toca snapshot, log, estado nem alerta."""
    dados = base / "data"
    series = {s: carregar(dados / c["arquivo"], c["valores"]) for s, c in SERIES.items()}
    faltando = [SERIES[s]["arquivo"] for s in ("tarifa", "fiob") if series[s] is None]
    if faltando:
        raise RuntimeError(f"--so-painel: snapshot ausente em {dados}: {', '.join(faltando)}")
    estado_path = dados / "estado.json"
    estado = json.loads(estado_path.read_text(encoding="utf-8")) if estado_path.exists() else {}
    ipca = ler_ipca(dados)
    print(f"Monitor Tarifas ANEEL - so painel (offline) - {agora:%d/%m/%Y %H:%M}")
    print(f"  IPCA: " + (f"cache ate {max(ipca['mensal'])}" if ipca else f"sem cache em data/{ARQ_IPCA}"))
    ga = {s: (escopo_serie(s, series[s], cfg) if series[s] is not None and cfg["subgrupos_grupo_a"] else None)
          for s in ("tarifa_a", "fiob_a")}
    if cfg["subgrupos_grupo_a"] and ga["tarifa_a"] is None:
        print(f"  [aviso] Grupo A: snapshot {SERIES['tarifa_a']['arquivo']} ausente; aba Grupo A vazia")
    visoes = montar_visoes(series["tarifa"], no_escopo_fiob(series["fiob"], cfg), ler_distribuidoras(base),
                           ipca, agora.date(), ga["tarifa_a"], ga["fiob_a"], cfg["subgrupos_grupo_a"])
    gerar_paineis(base, visoes, dados / "log_mudancas.csv", cfg, agora, estado)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=Path(__file__).resolve().parent,
                    help="pasta base (data/, config.json, painel). Padrao: pasta do script")
    ap.add_argument("--forcar-painel", action="store_true", help="regera Excel e HTML mesmo sem mudancas")
    ap.add_argument("--so-painel", action="store_true",
                    help="regera Excel e HTML a partir dos CSVs de data/, sem rede e sem alterar snapshot/log/estado")
    ap.add_argument("--so-ipca", action="store_true",
                    help="so atualiza o cache do IPCA (data/ipca_bcb.json), sem tocar no resto")
    ap.add_argument("--aceitar-queda", action="store_true",
                    help="grava mesmo se a guarda de queda bloquear (remocao grande e legitima na ANEEL)")
    ap.add_argument("--migrar-historico", metavar="REF",
                    help="migracao unica: reinsere no snapshot as linhas da versao REF do git (ex.: 7fe4a35^) que "
                         "sumiram, marcadas com removida_fonte_em = --data-remocao, e corrige o tipo no log")
    ap.add_argument("--data-remocao", metavar="AAAA-MM-DD",
                    help="data da marca removida_fonte_em na migracao (padrao: hoje)")
    args = ap.parse_args()
    global ACEITAR_QUEDA
    ACEITAR_QUEDA = args.aceitar_queda

    base: Path = args.dir.resolve()
    dados = base / "data"
    cfg = ler_config(base)
    agora = datetime.now(FUSO_BR)
    if args.migrar_historico:
        return migrar_historico(base, cfg, args.migrar_historico, args.data_remocao or agora.strftime("%Y-%m-%d"))
    if args.so_painel:
        return so_painel(base, cfg, agora)
    if args.so_ipca:
        avisos: list[str] = []
        print(f"Monitor Tarifas ANEEL - so IPCA - {agora:%d/%m/%Y %H:%M}")
        ipca, _ = atualizar_ipca(dados, agora.date(), avisos)
        return 0 if ipca else 1
    dados.mkdir(parents=True, exist_ok=True)
    alerta_path = base / "alerta.md"
    if alerta_path.exists():
        alerta_path.unlink()
    estado_path = dados / "estado.json"
    estado_ant = json.loads(estado_path.read_text(encoding="utf-8")) if estado_path.exists() else {}
    meta_ant = estado_ant.get("parquet", {})
    meta, avisos_fonte = {}, []
    a0, corte = cfg["fiob_ano_inicial"], corte_fiob(cfg)
    subs_a = cfg["subgrupos_grupo_a"]
    esc_a = escopo_grupo_a(cfg)
    fonte_ta, fonte_fa = fontes_grupo_a(subs_a)
    extra_a = {"subgrupos": ",".join(subs_a)}
    t_inicio = time.monotonic()

    print(f"Monitor Tarifas ANEEL - {agora:%d/%m/%Y %H:%M}")

    # ---- B3 e Grupo A TUSD+TE: Parquet unico do dataset de tarifas (um download para os dois recortes)
    print("Tarifas B3" + (f" e Grupo A ({', '.join(subs_a)})" if subs_a else "")
          + " (Parquet tarifas-distribuidoras-energia-eletrica) ...", flush=True)
    try:
        info_t = parquet_tarifas()
    except Exception as e:  # noqa: BLE001
        print(f"  [aviso] package_show de {PKG_TARIFAS} falhou ({e})", flush=True)
        info_t = None
    pedidos = {"tarifas_b3": (FONTES["tarifa"], None, None)}
    if subs_a:
        pedidos["tarifas_grupo_a"] = (fonte_ta, esc_a["inicio_min"], extra_a)
    rec = atualizar_recortes(info_t, pedidos, dados, meta_ant, avisos_fonte)
    for nome, (_, m) in rec.items():
        meta[nome] = m
    tarifas = normalizar(rec["tarifas_b3"][0], MAPA_TARIFAS, SERIES["tarifa"]["valores"])
    if tarifas.empty:
        raise RuntimeError("Recorte B3 com 0 linhas (filtro/encoding?). Nada foi salvo.")
    print(f"  B3: {len(tarifas)} linhas")
    tarifas_a = serie_vazia(SERIES["tarifa_a"]["valores"])
    if subs_a:
        bruto_a = rec["tarifas_grupo_a"][0]
        if bruto_a.empty:
            raise RuntimeError(f"Recorte do Grupo A ({', '.join(subs_a)}) com 0 linhas (filtro/encoding?). "
                               "Nada foi salvo.")
        tarifas_a = escopo_serie("tarifa_a", normalizar(bruto_a, MAPA_TARIFAS, SERIES["tarifa_a"]["valores"]), cfg)
        print(f"  Grupo A: {len(tarifas_a)} linhas (" + ", ".join(
            f"{s} {m}: {n}" for (s, m), n in tarifas_a.groupby(["subgrupo", "modalidade"]).size().items()) + ")")

    # ---- Fio B: Parquet anual de componentes-tarifarias, anos >= fiob_ano_inicial - ANOS_ARQUIVO_ANTES
    # (o arquivo de um ano traz vigencias que comecam em 01/01 do ano seguinte, ou mais tarde).
    # Um download por ano para os recortes da B3 (fiob_<ano>) e do Grupo A (fiob_a_<ano>).
    ano_arq = a0 - ANOS_ARQUIVO_ANTES
    print(f"Fio B (Parquets anuais de {PKG_COMPONENTES}, anos >= {ano_arq}, vigencias >= {data_br(corte)}) ...",
          flush=True)
    try:
        anos = parquets_componentes(ano_arq)
    except Exception as e:  # noqa: BLE001
        anos = {int(k[5:]): None for k in meta_ant if re.fullmatch(r"fiob_\d{4}", k) and int(k[5:]) >= ano_arq}
        print(f"  [aviso] package_show de {PKG_COMPONENTES} falhou ({e}); usados os recortes salvos de "
              f"{', '.join(map(str, anos)) or 'nenhum ano'}", flush=True)
        if not anos:
            raise
    partes, partes_a = [], []
    for ano, info in anos.items():
        pedidos = {f"fiob_{ano}": (FONTES["fiob"], corte, None)}
        if subs_a:
            pedidos[f"fiob_a_{ano}"] = (fonte_fa, corte, extra_a)
        rec = atualizar_recortes(info, pedidos, dados, meta_ant, avisos_fonte)
        for nome, (bruto, m) in rec.items():
            meta[nome] = m
            if len(bruto):
                (partes_a if nome.startswith("fiob_a_") else partes).append(bruto)
    # Recortes de anos que sairam do escopo (ou do formato antigo) nao servem mais.
    for arq in list(dados.glob("recorte_fiob_*.csv")) + list(dados.glob("fiob_parquet_*.csv")):
        if arq.stem.removeprefix("recorte_") not in meta:
            arq.unlink()
            print(f"  recorte antigo removido: {arq.name}")
    if not partes:
        raise RuntimeError(f"0 linhas de Fio B com vigencia a partir de {data_br(corte)}. Nada foi salvo.")
    fiob = no_escopo_fiob(normalizar(pd.concat(partes, ignore_index=True), MAPA_FIOB,
                                     SERIES["fiob"]["valores"]), cfg)
    por_ano = fiob["inicio"].str[:4].value_counts().sort_index()
    print(f"  Fio B: {len(fiob)} linhas (por ano de vigencia: "
          + ", ".join(f"{a}: {n}" for a, n in por_ano.items()) + ")")
    fiob_a = serie_vazia(SERIES["fiob_a"]["valores"])
    if subs_a and partes_a:
        fiob_a = escopo_serie("fiob_a", normalizar(pd.concat(partes_a, ignore_index=True), MAPA_FIOB,
                                                   SERIES["fiob_a"]["valores"]), cfg)
        print(f"  Fio B Grupo A: {len(fiob_a)} linhas (" + ", ".join(
            f"{s} {m}: {n}" for (s, m), n in fiob_a.groupby(["subgrupo", "modalidade"]).size().items()) + ")")
    baixado_mb = sum(m.pop("mb", 0.0) or 0.0 for m in meta.values())
    print(f"Download nesta execucao: {baixado_mb:.1f} MB")
    atuais = {"tarifa": tarifas, "fiob": fiob, "tarifa_a": tarifas_a, "fiob_a": fiob_a}

    # ---- IPCA (Banco Central) e nomes comerciais; falha no IPCA nao interrompe a execucao
    print("IPCA (Banco Central, SGS 433) ...", flush=True)
    ipca, ipca_mudou = atualizar_ipca(dados, agora.date(), avisos_fonte)
    catalogo = ler_distribuidoras(base)

    # ---- compara com o snapshot cumulativo (linhas que somem da fonte ficam, marcadas com removida_fonte_em)
    # Primeiro compara as 4 series e aplica as guardas; so depois grava log e snapshots (nada e salvo se uma
    # guarda bloquear).
    esc_ant = estado_ant.get("escopo_grupo_a") or esc_a
    mudancas, houve_mudanca, primeira_vez = {}, False, False
    log_path = dados / "log_mudancas.csv"
    snaps = {s: com_marca(serie_vazia(c["valores"])) for s, c in SERIES.items()}
    resultados, base_nova = {}, {}
    for serie, cfg_s in SERIES.items():
        caminho = dados / cfg_s["arquivo"]
        if cfg_s.get("grupo_a") and not subs_a:
            mudancas[serie] = {"chaves": [], "outras": 0}
            continue
        antigo = carregar(caminho, cfg_s["valores"])
        novo = atuais[serie]
        if antigo is None:
            primeira_vez = True
            mudancas[serie] = {"chaves": [], "outras": 0}
            print(f"  {cfg_s['rotulo']}: linha de base criada ({len(novo)} linhas, sem alerta)")
            base_nova[serie] = snaps[serie] = com_marca(novo)
            continue
        r = processar_serie(serie, antigo, novo, cfg, esc_ant, None, agora, args.aceitar_queda)
        resultados[serie] = r
        mudancas[serie] = r["mudancas"]
        snaps[serie] = r["snapshot"]
    for serie, df in base_nova.items():
        salvar(df, dados / SERIES[serie]["arquivo"], SERIES[serie]["valores"])
    for serie, r in resultados.items():
        registrar_log(log_path, serie, r["novos"], r["alterados"], r["removidos"], SERIES[serie]["valores"], agora,
                      r["voltaram"])
        if r["n_log"]:
            houve_mudanca = True
        if r["salvar"]:
            salvar(r["snapshot"], dados / SERIES[serie]["arquivo"], SERIES[serie]["valores"])

    # O painel usa o snapshot cumulativo (fonte atual + historico que a ANEEL tirou da base)
    visoes = montar_visoes(snaps["tarifa"], snaps["fiob"], catalogo, ipca, agora.date(), snaps["tarifa_a"],
                           snaps["fiob_a"], subs_a)
    # Assinatura do que o painel mostra como "atual" (vigencia vigente de cada distribuidora e calendario):
    # muda quando uma vigencia comeca ou termina, ou quando o catalogo de nomes muda; forca regerar o painel.
    met = visoes["metricas"]
    gm = visoes["ga"]["met"]
    assinatura = json.dumps([sorted(zip(met.index, met["inicio"], met["fiob_inicio"].fillna(""))),
                             list(calendario(met, agora.date()).index),
                             ipca.get("baixado_em", "")[:7] if ipca else "",
                             sorted(zip(gm["distribuidora"], gm["subgrupo"], gm["modalidade"], gm["inicio"],
                                        gm["fb_inicio"].fillna(""))) if len(gm) else [],
                             hash_impostos(base)],
                            ensure_ascii=False)

    # Estado com data (sem hora): no maximo 1 commit por dia como sinal de vida, o que
    # tambem evita o GitHub desativar o agendamento por inatividade.
    estado = {
        "assinatura_painel": hashlib.sha1(assinatura.encode("utf-8")).hexdigest(),
        "ultima_verificacao": agora.strftime("%Y-%m-%d"),
        "fiob_ano_inicial": a0,
        "escopo_grupo_a": esc_a,
        "linhas": {"tarifa_b3": len(tarifas), "fiob_b3": len(fiob), "tarifa_grupo_a": len(tarifas_a),
                   "fiob_grupo_a": len(fiob_a)},
        # linhas que a ANEEL tirou da base e o snapshot cumulativo guarda (removida_fonte_em preenchida)
        "linhas_removidas_fonte": {n: int(len(snaps[s]) - len(ativas(snaps[s]))) for s, n in
                                   (("tarifa", "tarifa_b3"), ("fiob", "fiob_b3"), ("tarifa_a", "tarifa_grupo_a"),
                                    ("fiob_a", "fiob_grupo_a"))},
        "linhas_fiob_por_ano_vigencia": {str(a): int(n) for a, n in por_ano.items()},
        "distribuidoras_grupo_a": {f"{s} {m}": int(n) for (s, m), n in
                                   tarifas_a.groupby(["subgrupo", "modalidade"])["cnpj"].nunique().items()},
        "parquet": meta,
        "avisos_fonte": avisos_fonte,
        "ultima_vigencia_b3": tarifas["inicio"].max(),
        "ultima_vigencia_fiob": fiob["inicio"].max(),
        "ultima_vigencia_grupo_a": tarifas_a["inicio"].max() if len(tarifas_a) else "",
    }

    painel = base / f"{PAINEL}.xlsx"
    painel_html = base / f"{PAINEL}.html"
    if (houve_mudanca or primeira_vez or args.forcar_painel or not painel.exists() or not painel_html.exists()
            or avisos_fonte != estado_ant.get("avisos_fonte", []) or ipca_mudou
            or estado["assinatura_painel"] != estado_ant.get("assinatura_painel")):
        gerar_paineis(base, visoes, log_path, cfg, agora, estado)

    alerta = None if primeira_vez and not houve_mudanca else montar_alerta(visoes, mudancas, cfg, agora)
    if alerta:
        alerta_path.write_text(alerta, encoding="utf-8")
        # Copia versionada em data/alertas/: entra no commit da coleta e o workflow abre a Issue no push.
        pasta_alertas = dados / "alertas"
        pasta_alertas.mkdir(parents=True, exist_ok=True)
        arq_alerta = pasta_alertas / f"{agora:%Y-%m-%d_%H%M}.md"
        n = 2
        while arq_alerta.exists():
            arq_alerta = pasta_alertas / f"{agora:%Y-%m-%d_%H%M}_{n}.md"
            n += 1
        arq_alerta.write_text(alerta, encoding="utf-8")
        print(f"ALERTA gerado ({arq_alerta.relative_to(base).as_posix()}):\n" + alerta)
    else:
        print("Sem mudancas relevantes.")

    estado_path.write_text(json.dumps(estado, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(f"Tempo total: {time.monotonic() - t_inicio:.0f} s")

    saida = os.environ.get("GITHUB_OUTPUT")
    if saida:
        with open(saida, "a", encoding="utf-8") as fh:
            fh.write(f"alerta={'true' if alerta else 'false'}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"ERRO: {e}", file=sys.stderr)
        if FALHAS_CONEXAO:
            print(f"ERRO: {MSG_BLOQUEIO} ({len(FALHAS_CONEXAO)} recusas: "
                  f"{', '.join(sorted(set(FALHAS_CONEXAO)))}). O portal dadosabertos.aneel.gov.br costuma "
                  "recusar IPs de data centers fora do Brasil, como os do GitHub Actions. Rode a coleta no PC "
                  "(rodar_local.ps1); ver README.",
                  file=sys.stderr)
            if os.environ.get("GITHUB_ACTIONS"):
                print(f"::error title=ANEEL bloqueou o runner::{MSG_BLOQUEIO}. Colete no PC com rodar_local.ps1 (README).")
        sys.exit(1)
