"""Espelho em Python da conta do Montador de proposta: Asset Management (proposta_asset.html).

Modelo: a ACER arrecada do cliente, paga a fatura da distribuidora, paga a recorrencia do canal, paga o fee
da Thopen (senior na cascata) e repassa o restante ao Gerador. Valores por cliente ativo por mes (R$),
percentuais como fracao (0,15 = 15%).

    arrecadacao       = (energia faturada com desconto + fatura distribuidora) x (1 - inadimplencia)
    vazamento         = fatura distribuidora x inadimplencia (a ACER paga 100% da fatura e recebe 1 - inadimplencia)
    fee               = % x energia faturada com desconto + fixo
    gerador (antes SF)= arrecadacao - fatura distribuidora - recorrencia do canal - fee
    success fee       = % success fee x max(0, gerador antes SF - meta)            (so na Proposta B)
    resultado Thopen  = receita x (1 - impostos) - custo de operacao - G&A / ocupacao
    hoje (take rate)  : gerador = take rate x credito bruto; Thopen fica com o resto da cascata e
                        so paga imposto quando a receita e positiva.

Sem dados reais: as premissas ficam no navegador do usuario ou em arquivos de cenario fora deste repositorio.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Optional


@dataclass(frozen=True)
class Premissas:
    credito: float              # credito de energia compensada bruto (R$/cliente/mes)
    faturada: float             # energia faturada com desconto (R$/cliente/mes)
    fatura_dist: float          # fatura da distribuidora repassada ao cliente
    inadimplencia: float        # fracao
    recorrencia_canal: float    # R$/cliente/mes
    custo_operacao: float       # custo de operacao do cliente (Thopen), R$/cliente/mes
    ga: float                   # G&A Thopen por cliente a 100% de ocupacao
    impostos: float             # impostos sobre a receita da Thopen (fracao)
    ocupacao: float = 1.0       # fracao
    clientes: Optional[float] = None  # capacidade da usina em clientes

    @property
    def desconto(self) -> float:
        return 1.0 - self.faturada / self.credito if self.credito else 0.0


def faturada_por_desconto(credito: float, desconto: float) -> float:
    return credito * (1.0 - desconto)


def arrecadacao(p: Premissas) -> float:
    return (p.faturada + p.fatura_dist) * (1.0 - p.inadimplencia)


def vazamento(p: Premissas) -> float:
    return p.fatura_dist * p.inadimplencia


def ga_por_ativo(p: Premissas) -> float:
    return p.ga / p.ocupacao


def _thopen(p: Premissas, receita: float, imposto: float) -> float:
    return receita - imposto - p.custo_operacao - ga_por_ativo(p)


def proposta(p: Premissas, pct: float = 0.0, fixo: float = 0.0, sigma: float = 0.0,
             meta: Optional[float] = None) -> dict:
    """Proposta com fee (% sobre a energia faturada com desconto + fixo) e success fee opcional."""
    arr = arrecadacao(p)
    fee_pct = pct * p.faturada
    fee = fee_pct + fixo
    g0 = arr - p.fatura_dist - p.recorrencia_canal - fee
    excedente = max(0.0, g0 - meta) if (sigma and meta is not None) else 0.0
    sf = sigma * excedente
    receita = fee + sf
    imposto = receita * p.impostos
    return {
        "arrecadacao": arr, "vazamento": vazamento(p), "fee_pct": fee_pct, "fee_fixo": fixo, "fee": fee,
        "gerador_antes_sf": g0, "excedente": excedente, "success_fee": sf, "gerador": g0 - sf,
        "receita": receita, "impostos": imposto, "ga_ativo": ga_por_ativo(p), "thopen": _thopen(p, receita, imposto),
    }


def hoje(p: Premissas, take_rate: float) -> dict:
    """Modelo atual: o Gerador recebe take rate x credito bruto; a Thopen fica com o resto (imposto so se positivo)."""
    arr = arrecadacao(p)
    gerador = take_rate * p.credito
    receita = arr - p.fatura_dist - p.recorrencia_canal - gerador
    imposto = receita * p.impostos if receita > 0 else 0.0
    return {
        "arrecadacao": arr, "vazamento": vazamento(p), "fee": 0.0, "success_fee": 0.0, "gerador": gerador,
        "receita": receita, "impostos": imposto, "ga_ativo": ga_por_ativo(p), "thopen": _thopen(p, receita, imposto),
    }


def receita_equilibrio(p: Premissas) -> float:
    """Receita por cliente ativo que zera o resultado da Thopen."""
    return (p.custo_operacao + ga_por_ativo(p)) / (1.0 - p.impostos)


def multiplicador(p: Premissas) -> float:
    """Fee necessario para cada R$ 1 de resultado liquido."""
    return 1.0 / (1.0 - p.impostos)


def fixo_necessario(p: Premissas, pct: float) -> float:
    return receita_equilibrio(p) - pct * p.faturada


def pct_necessario(p: Premissas, fixo: float = 0.0) -> float:
    return (receita_equilibrio(p) - fixo) / p.faturada


def ocupacao_equilibrio(p: Premissas, receita: float) -> Optional[float]:
    """Ocupacao que zera o resultado da Thopen; None se nao fecha em nenhuma ocupacao."""
    den = receita * (1.0 - p.impostos) - p.custo_operacao
    if den <= 0:
        return None
    return p.ga / den


def totais_usina(p: Premissas, valor_por_ativo: float) -> Optional[dict]:
    if not p.clientes:
        return None
    mes = valor_por_ativo * p.clientes * p.ocupacao
    return {"mes": mes, "ano": mes * 12}


def variacao(gerador: float, gerador_hoje: float) -> Optional[float]:
    return gerador / gerador_hoje - 1.0 if gerador_hoje else None


def com(p: Premissas, **kw) -> Premissas:
    return replace(p, **kw)


# ---------------------------------------------------------------- capacidade da usina

def capacidade_usina(consumo: Optional[float], potencia_kwp: Optional[float] = None,
                     produtividade: Optional[float] = None, geracao_mwh_mes: Optional[float] = None,
                     pct_compensado: float = 1.0) -> dict:
    """Geracao media (MWh/mes) = kWp x kWh/kWp/ano / 12 / 1000 (mesma conta de geracao_mwh_mes do painel, UFV) ou a
    geracao direta informada; energia compensada = geracao x % compensado; capacidade (clientes) =
    piso(energia compensada em kWh/mes / consumo medio por cliente)."""
    if geracao_mwh_mes is not None:
        ger = geracao_mwh_mes
    elif potencia_kwp is not None and produtividade is not None:
        ger = potencia_kwp * produtividade / 12 / 1000
    else:
        ger = None
    comp = ger * pct_compensado if ger is not None else None
    clientes = math.floor(comp * 1000 / consumo + 1e-9) if comp is not None and consumo else None
    return {"geracao_mwh_mes": ger, "compensada_mwh_mes": comp, "clientes": clientes}


# ---------------------------------------------------------------- projecao por ano e cenarios de prazo

def credito_kwh_ano(tarifa_base: float, w_inicio: float, w_ano: float) -> float:
    """Tarifa compensavel do ano (antes do reajuste): a base perde o aumento do Fio B efetivo entre o ano de inicio e
    o ano civil (w = valor compensado do painel em R$/kWh no ano; GD I tem w constante, entao nao muda)."""
    return tarifa_base - (w_inicio - w_ano)


def vpl(fluxos: list[float], taxa: float) -> float:
    """Fluxos anuais no fim de cada ano (mesma convencao do painel): soma fluxo_t / (1 + taxa)^(t + 1)."""
    return sum(f / (1.0 + taxa) ** (t + 1) for t, f in enumerate(fluxos))


def premissas_ano(p: Premissas, t: int, reaj_tarifa: float = 0.0, reaj_custos: float = 0.0, fator_fio_b: float = 1.0,
                  ocupacao: Optional[float] = None, degradacao: float = 0.0) -> Premissas:
    """Premissas do ano t (0 = ano de inicio). Credito x fator do Fio B x (1 + reajuste da tarifa)^t, desconto constante;
    fatura da distribuidora pelo reajuste da tarifa; canal, custo de operacao e G&A pelo reajuste dos custos;
    capacidade = piso(clientes x (1 - degradacao)^t)."""
    ft, fc = (1.0 + reaj_tarifa) ** t, (1.0 + reaj_custos) ** t
    cred = p.credito * fator_fio_b * ft
    n = math.floor(p.clientes * (1.0 - degradacao) ** t + 1e-9) if p.clientes else None
    return replace(p, credito=cred, faturada=cred * (1.0 - p.desconto), fatura_dist=p.fatura_dist * ft,
                   recorrencia_canal=p.recorrencia_canal * fc, custo_operacao=p.custo_operacao * fc, ga=p.ga * fc,
                   ocupacao=p.ocupacao if ocupacao is None else ocupacao, clientes=n)


def projecao(p: Premissas, anos: int, take_rate: float, a: dict, b: dict, reaj_tarifa: float = 0.0,
             reaj_custos: float = 0.0, fatores_fio_b: Optional[list[float]] = None, ocupacao_ano1: Optional[float] = None,
             degradacao: float = 0.0) -> list[dict]:
    """Projecao ano a ano de Hoje, Proposta A e Proposta B. a = {pct, fixo}; b = {pct, fixo, sigma, meta}: fee fixo
    pelo reajuste dos custos; meta do success fee pelo reajuste da tarifa (meta None = resultado do Gerador antes do
    success fee no ano 0). Valores anuais = valor por cliente ativo x clientes ativos x 12 (sem capacidade: 1 cliente
    ativo)."""
    meta0 = b.get("meta")
    if b.get("sigma") and meta0 is None:
        meta0 = proposta(p, b["pct"], b["fixo"])["gerador_antes_sf"]
    out = []
    for t in range(int(anos)):
        ff = fatores_fio_b[t] if fatores_fio_b else 1.0
        oc = ocupacao_ano1 if (t == 0 and ocupacao_ano1 is not None) else None
        pt = premissas_ano(p, t, reaj_tarifa, reaj_custos, ff, oc, degradacao)
        ft, fc = (1.0 + reaj_tarifa) ** t, (1.0 + reaj_custos) ** t
        r = {"hoje": hoje(pt, take_rate), "a": proposta(pt, a["pct"], a["fixo"] * fc),
             "b": proposta(pt, b["pct"], b["fixo"] * fc, b.get("sigma", 0.0), meta0 * ft if meta0 is not None else None)}
        ativos = pt.clientes * pt.ocupacao if pt.clientes else 1.0
        linha = {"t": t, "premissas": pt, "ativos": ativos, "por_cliente": r}
        for k, x in r.items():
            linha[k] = {"gerador": x["gerador"] * ativos * 12, "thopen": x["thopen"] * ativos * 12,
                        "receita": x["receita"] * ativos * 12}
        out.append(linha)
    return out


def cenario_prazo(proj: list[dict], prazo: int, taxa: Optional[float] = None) -> dict:
    """Total e VPL (se houver taxa) por cenario nos primeiros `prazo` anos da projecao."""
    res = {}
    for k in ("hoje", "a", "b"):
        res[k] = {}
        for v in ("gerador", "thopen", "receita"):
            fl = [linha[k][v] for linha in proj[:prazo]]
            res[k][v] = {"total": sum(fl), "vpl": vpl(fl, taxa) if taxa is not None else None}
    return res


# ---------------------------------------------------------------- nomes das propostas e valores por kWh

def _num_br(v: float, casas: int) -> str:
    s = f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s


def fmt_pct(v: float) -> str:
    """Fracao em % com ate 2 casas, sem zeros a direita (0,05 -> '5%'; 0,025 -> '2,5%')."""
    s = _num_br(round(v * 100, 2), 2)
    if "," in s:
        s = s.rstrip("0").rstrip(",")
    return s + "%"


def fmt_brl_curto(v: float) -> str:
    """R$ sem centavos quando inteiro (40 -> 'R$ 40'; 40,5 -> 'R$ 40,50'; 1200 -> 'R$ 1.200')."""
    r = round(v, 2)
    return "R$ " + (_num_br(r, 0) if abs(r - round(r)) < 1e-9 else _num_br(r, 2))


def nome_proposta(pct: float = 0.0, fixo: float = 0.0, sigma: float = 0.0) -> str:
    """Nome automatico pela estrutura de fee: 'Fee fixo: 5% + R$ 40/UC' ou
    'Fee reduzido + success fee: 5% + R$ 30/UC + 25% do excedente'."""
    partes = []
    if pct:
        partes.append(fmt_pct(pct))
    if fixo:
        partes.append(fmt_brl_curto(fixo) + "/UC")
    if sigma:
        partes.append(fmt_pct(sigma) + " do excedente")
        return "Fee reduzido + success fee: " + (" + ".join(partes))
    return "Fee fixo: " + (" + ".join(partes) if partes else "sem fee")


def valores_kwh(p: Premissas, r: dict, consumo: Optional[float] = None, tarifa_cheia: Optional[float] = None) -> dict:
    """Valores por cliente/mes e por kWh de um cenario (r = proposta() ou hoje()).
    Tarifa compensavel = credito / consumo; tarifa do cliente = energia faturada com desconto / consumo; Thopen = fee total
    (no Hoje, a sobra da cascata); split = fracao de cada R$ 1 arrecadado (soma 1)."""
    eh_hoje = "fee_pct" not in r
    cli = {
        "tarifa_cheia": tarifa_cheia * consumo if (tarifa_cheia is not None and consumo) else None,
        "credito": p.credito, "faturada": p.faturada, "economia": p.credito - p.faturada,
        "custo_total": p.faturada + p.fatura_dist,
        "economia_cheia": tarifa_cheia * consumo - (p.faturada + p.fatura_dist) if (tarifa_cheia is not None and consumo) else None,
        "fee_pct": None if eh_hoje else r["fee_pct"], "fee_fixo": None if eh_hoje else r["fee_fixo"],
        "success_fee": None if eh_hoje else r["success_fee"], "thopen": r["receita"], "gerador": r["gerador"],
        "fatura_dist": p.fatura_dist, "canal": p.recorrencia_canal, "arrecadacao": r["arrecadacao"],
    }
    kwh = {k: (v / consumo if v is not None else None) for k, v in cli.items()} if consumo else None
    arr = r["arrecadacao"]
    split = {"gerador": r["gerador"] / arr, "thopen": r["receita"] / arr, "canal": p.recorrencia_canal / arr,
             "distribuidora": p.fatura_dist * (1.0 - p.inadimplencia) / arr, "vazamento": vazamento(p) / arr} if arr else None
    return {"cliente": cli, "kwh": kwh, "split": split, "desconto": p.desconto,
            "economia_cheia_pct": (cli["economia_cheia"] / cli["tarifa_cheia"]) if cli["tarifa_cheia"] else None}
