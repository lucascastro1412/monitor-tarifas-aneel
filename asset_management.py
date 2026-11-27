"""Espelho em Python da conta do Montador de proposta: Asset Management (proposta_asset.html).

Modelo: a ACER arrecada do cliente, paga a fatura da distribuidora, paga a recorrencia do canal, paga o fee
da Thopen (senior na cascata) e repassa o restante ao Gerador. Valores por cliente ativo por mes (R$),
percentuais como fracao (0,15 = 15%).

    arrecadacao       = (energia faturada com desconto + fatura distribuidora) x (1 - inadimplencia)
    vazamento         = fatura distribuidora x inadimplencia (a ACER paga 100% da fatura e recebe 1 - inadimplencia)
    fee               = % x energia faturada com desconto + fixo
    gerador (antes SF)= arrecadacao - fatura distribuidora - recorrencia do canal - fee
    success fee       = % success fee x max(0, gerador antes SF - meta)            (so na Proposta B)
    meta por ano      = meta inicial x tarifa compensavel do ano / tarifa compensavel do ano de inicio (padrao, inclui o
                        Fio B), resultado do Gerador no cenario base do ano, reajuste da tarifa ou fixa (META_REAJUSTE)
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


def fee_fixo_efetivo(fixo: float, ocupacao: float, capacidade: bool = False) -> float:
    """Fee fixo por cliente ativo. Base capacidade: o fixo e devido sobre a capacidade da usina (N clientes contrataveis)
    mesmo com vagas, entao por cliente ativo = fixo / ocupacao (o Gerador paga tambem as vagas)."""
    return fixo / ocupacao if capacidade else fixo


def proposta(p: Premissas, pct: float = 0.0, fixo: float = 0.0, sigma: float = 0.0,
             meta: Optional[float] = None, capacidade: bool = False, sf: Optional[float] = None) -> dict:
    """Proposta com fee (% sobre a energia faturada com desconto + fixo) e success fee opcional.
    capacidade = fee fixo sobre a capacidade da usina (fixo / ocupacao por cliente ativo); sf = success fee ja apurado
    (apuracao com saldo acumulado), no lugar de % x excedente do mes."""
    arr = arrecadacao(p)
    fee_pct = pct * p.faturada
    fixo = fee_fixo_efetivo(fixo, p.ocupacao, capacidade)
    fee = fee_pct + fixo
    g0 = arr - p.fatura_dist - p.recorrencia_canal - fee
    excedente = max(0.0, g0 - meta) if (sigma and meta is not None) else 0.0
    sf = sigma * excedente if sf is None else sf
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


# ---------------------------------------------------------------- calculo reverso do fee

def receita_necessaria(p: Premissas, margem: float = 0.0, unidade: str = "rs", ocupacao: Optional[float] = None) -> Optional[float]:
    """Fee (receita) por cliente ativo que entrega a margem desejada da Thopen na ocupacao dada (padrao: a base).
    unidade "rs": margem em R$/cliente ativo/mes -> (margem + custo de operacao + G&A / ocupacao) / (1 - impostos).
    unidade "pct": margem como fracao do fee -> (custo de operacao + G&A / ocupacao) / (1 - impostos - margem);
    None se impostos + margem >= 100%."""
    w = p.ocupacao if ocupacao is None else ocupacao
    custos = p.custo_operacao + p.ga / w
    if unidade == "pct":
        den = 1.0 - p.impostos - margem
        return custos / den if den > 0 else None
    return (margem + custos) / (1.0 - p.impostos)


def fee_reverso(p: Premissas, margem: float = 0.0, unidade: str = "rs", pct: float = 0.0, fixo: float = 0.0,
                capacidade: bool = False, ocupacao: Optional[float] = None) -> Optional[dict]:
    """Calculo reverso do fee (sem success fee: na Proposta B, sobre o piso).
    fixo = receita necessaria - % x energia faturada (mantendo o %); % = (receita necessaria - fixo) / energia faturada
    (mantendo o fixo); so % e so fixo. Com fee fixo sobre a capacidade, o fixo contratual = fixo por ativo x ocupacao."""
    w = p.ocupacao if ocupacao is None else ocupacao
    r = receita_necessaria(p, margem, unidade, w)
    if r is None:
        return None
    k = w if capacidade else 1.0
    return {"receita": r, "fixo": (r - pct * p.faturada) * k, "pct": (r - fixo / k) / p.faturada,
            "so_pct": r / p.faturada, "so_fixo": r * k, "ocupacao": w}


def ocupacao_equilibrio_fee(p: Premissas, pct: float = 0.0, fixo: float = 0.0, capacidade: bool = False,
                            sf: float = 0.0) -> dict:
    """Ocupacao de equilibrio de uma proposta, com o fee fixo por cliente ativo ou sobre a capacidade.
    Resultado da Thopen por cliente ativo = a + b / ocupacao, com
      a = (% x energia faturada + fixo por ativo [so base clientes ativos] + success fee) x (1 - impostos) - custo de operacao
      b = fixo [so base capacidade] x (1 - impostos) - G&A.
    tipo: "acima" (positivo acima de ocupacao), "sempre" (positivo em qualquer ocupacao), "nunca", "ate" (positivo so
    abaixo de ocupacao: fixo sobre a capacidade cobre o G&A, mas a parte variavel nao cobre o custo de operacao)."""
    a = (pct * p.faturada + (0.0 if capacidade else fixo) + sf) * (1.0 - p.impostos) - p.custo_operacao
    b = (fixo if capacidade else 0.0) * (1.0 - p.impostos) - p.ga
    if b >= 0:
        if a >= 0:
            return {"tipo": "sempre", "ocupacao": None, "a": a, "b": b}
        return {"tipo": "ate", "ocupacao": b / -a, "a": a, "b": b}
    if a <= 0:
        return {"tipo": "nunca", "ocupacao": None, "a": a, "b": b}
    return {"tipo": "acima", "ocupacao": -b / a, "a": a, "b": b}


# ---------------------------------------------------------------- success fee com saldo acumulado

def success_fee_meses(g0s: list[float], metas: list[float], sigma: float, carry: bool = False,
                      saldo: float = 0.0) -> tuple[list[dict], float]:
    """Apuracao mensal do success fee (R$ por cliente ativo). Simples: sf = % x max(0, G0 - meta) em cada mes.
    Com saldo acumulado (carry-forward): o deficit do mes (meta - G0, quando G0 < meta) soma ao saldo; o excedente
    primeiro abate o saldo e so o restante paga o %. Retorna os meses e o saldo final (para o mes seguinte)."""
    out = []
    for g0, meta in zip(g0s, metas):
        exc, ini, abat, sf = g0 - meta, saldo, 0.0, 0.0
        if not carry:
            sf = sigma * max(0.0, exc)
        elif exc < 0:
            saldo += -exc
        else:
            abat = min(saldo, exc)
            saldo -= abat
            sf = sigma * (exc - abat)
        out.append({"g0": g0, "meta": meta, "excedente": exc, "saldo_ini": ini, "abatido": abat, "sf": sf,
                    "saldo_fim": saldo if carry else 0.0})
    return out, (saldo if carry else 0.0)


def perfil_alternado(base: float, pp: float, meses: int = 12) -> list[float]:
    """Perfil mensal de inadimplencia alternado: mes 1 = base + pp, mes 2 = base - pp, ... (limitado a 0 e 99,99%)."""
    return [min(0.9999, max(0.0, base + (pp if m % 2 == 0 else -pp))) for m in range(meses)]


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


META_REAJUSTE = ("comp", "base", "tarifa", "fixa")


def meta_ano(meta0: Optional[float], modo: str, ft: float, ff: float, ff0: float = 1.0,
             g0_base: Optional[float] = None) -> Optional[float]:
    """Meta do success fee no ano. modo "comp" (padrao): meta inicial x tarifa compensavel do ano / do ano de inicio
    (= (1 + reajuste da tarifa)^t x fator do Fio B do ano / fator do ano de inicio; GD I = reajuste da tarifa);
    "base": resultado do Gerador antes do success fee no cenario base do ano (g0_base: success fee so sobre o desempenho
    acima da base); "tarifa": meta inicial x (1 + reajuste da tarifa)^t (regra anterior); "fixa": meta inicial."""
    if modo == "base":
        return g0_base
    if meta0 is None:
        return None
    if modo == "fixa":
        return meta0
    if modo == "tarifa":
        return meta0 * ft
    if modo == "comp":
        return meta0 * ft * ff / ff0
    raise ValueError("reajuste da meta desconhecido: " + str(modo))


def projecao(p: Premissas, anos: int, take_rate: float, a: dict, b: dict, reaj_tarifa: float = 0.0,
             reaj_custos: float = 0.0, fatores_fio_b: Optional[list[float]] = None, ocupacao_ano1: Optional[float] = None,
             degradacao: float = 0.0, perfil_inad: Optional[list[float]] = None) -> list[dict]:
    """Projecao ano a ano de Hoje, Proposta A e Proposta B. a = {pct, fixo, cap}; b = {pct, fixo, sigma, meta, cap,
    carry, zerar, meta_reaj}: fee fixo pelo reajuste dos custos (cap = fee fixo sobre a capacidade); meta do success fee
    reajustada por meta_ano (meta_reaj, padrao "comp"; meta None = resultado do Gerador antes do success fee no ano 0,
    com a ocupacao base; "base" ignora a meta informada).
    Cada ano simula 12 meses com o perfil mensal de inadimplencia (padrao: a inadimplencia base em todos os meses);
    o success fee de B e apurado mes a mes, simples ou com saldo acumulado (carry = True; zerar = saldo zerado no
    inicio de cada ano). por_cliente = media mensal por cliente ativo; valores anuais = soma dos 12 meses x clientes
    ativos (sem capacidade: 1 cliente ativo)."""
    meta0 = b.get("meta")
    if b.get("sigma") and meta0 is None:
        meta0 = proposta(p, b["pct"], b["fixo"], capacidade=b.get("cap", False))["gerador_antes_sf"]
    modo = b.get("meta_reaj", "comp")
    ff0 = fatores_fio_b[0] if fatores_fio_b else 1.0
    out, saldo = [], 0.0
    for t in range(int(anos)):
        ff = fatores_fio_b[t] if fatores_fio_b else 1.0
        oc = ocupacao_ano1 if (t == 0 and ocupacao_ano1 is not None) else None
        pt = premissas_ano(p, t, reaj_tarifa, reaj_custos, ff, oc, degradacao)
        ft, fc = (1.0 + reaj_tarifa) ** t, (1.0 + reaj_custos) ** t
        meses = [replace(pt, inadimplencia=i) for i in (perfil_inad or [pt.inadimplencia] * 12)]
        g0b = proposta(pt, b["pct"], b["fixo"] * fc, capacidade=b.get("cap", False))["gerador_antes_sf"]
        meta_t = meta_ano(meta0, modo, ft, ff, ff0, g0b)
        rh = [hoje(pm, take_rate) for pm in meses]
        ra = [proposta(pm, a["pct"], a["fixo"] * fc, capacidade=a.get("cap", False)) for pm in meses]
        piso = [proposta(pm, b["pct"], b["fixo"] * fc, capacidade=b.get("cap", False)) for pm in meses]
        if b.get("zerar"):
            saldo = 0.0
        if b.get("sigma") and meta_t is not None:
            sfm, saldo = success_fee_meses([x["gerador_antes_sf"] for x in piso], [meta_t] * len(meses), b["sigma"],
                                           b.get("carry", False), saldo)
        else:
            sfm = [{"g0": x["gerador_antes_sf"], "meta": meta_t, "excedente": 0.0, "saldo_ini": 0.0, "abatido": 0.0,
                    "sf": 0.0, "saldo_fim": 0.0} for x in piso]
        rb = [proposta(pm, b["pct"], b["fixo"] * fc, b.get("sigma", 0.0), meta_t, b.get("cap", False), sf=x["sf"])
              for pm, x in zip(meses, sfm)]
        r = {"hoje": _media(rh), "a": _media(ra), "b": _media(rb)}
        ativos = pt.clientes * pt.ocupacao if pt.clientes else 1.0
        linha = {"t": t, "premissas": pt, "ativos": ativos, "por_cliente": r, "meta": meta_t, "sf_meses": sfm,
                 "success_fee_ano": sum(x["sf"] for x in sfm) * ativos}
        for k, x in r.items():
            linha[k] = {"gerador": x["gerador"] * ativos * 12, "thopen": x["thopen"] * ativos * 12,
                        "receita": x["receita"] * ativos * 12}
        out.append(linha)
    return out


def _media(rs: list[dict]) -> dict:
    """Media mensal dos campos numericos de uma lista de resultados mensais."""
    return {k: sum(r[k] for r in rs) / len(rs) for k in rs[0] if isinstance(rs[0][k], (int, float))}


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


# ---------------------------------------------------------------- comparacao com o arrendamento

def arrendamento_mwh(tusd: float, te: float, icms: float, pc: float, flag_te: int = 1, flag_tusd: int = 1,
                     s: float = 0.5, d: float = 0.3, c: float = 0.0, fb_ef: float = 0.0,
                     fb_com_impostos: bool = True, base_desconto: Optional[float] = None) -> dict:
    """Porte de calc_arrendamento (monitor_tarifas.py) usado na pagina, por MWh compensado (R$/MWh).
    k_PC = 1/(1-pc); k_full = 1/((1-icms)(1-pc)); W_I = TUSD.k_TUSD + TE.k_TE; U = TUSD + TE; Y = W_I - U.
    Custo do Fio B (pago pelo dono) = Fio B efetivo x k_full (com impostos) ou Fio B efetivo (sem impostos).
    dono = (1 - d).U + s.Y - custo do Fio B; Thopen = d.U + (1 - s).Y; desconto = c.W_I; margem = Thopen - desconto.
    O cliente paga valor bruto - desconto (= dono + margem): e a base da inadimplencia na versao comparavel.
    base_desconto (R$/MWh): None = convencao do painel (valor bruto = W_I - custo do Fio B, desconto = c x W_I);
    com a tarifa compensavel do Asset (R$/MWh): valor bruto = tarifa compensavel e desconto = c x tarifa compensavel,
    entao o cliente paga o mesmo que no Asset (energia faturada com desconto) e recebe o mesmo desconto em R$.
    margem = cliente paga - dono (no painel = parte da Thopen - desconto)."""
    k_pc = 1.0 / (1.0 - pc)
    k_full = 1.0 / ((1.0 - icms) * (1.0 - pc))
    k_te = k_full if flag_te else k_pc
    k_tusd = k_full if flag_tusd else k_pc
    u = tusd + te
    w = tusd * k_tusd + te * k_te
    y = w - u
    cfb = fb_ef * k_full if fb_com_impostos else fb_ef
    dono = (1.0 - d) * u + s * y - cfb
    th = d * u + (1.0 - s) * y
    desc = c * (w if base_desconto is None else base_desconto)
    bruto = w - cfb if base_desconto is None else base_desconto
    return {"U": u, "W_I": w, "Y": y, "V": u * k_full, "k_full": k_full, "custo_fb": cfb, "s": s, "d": d, "c": c,
            "dono": dono, "thopen": th, "desconto": desc, "margem": bruto - desc - dono, "bruto": bruto, "preco_kwh": dono / 1000.0,
            "desagio_impl": 1.0 - dono / u if u else None, "cliente_paga": bruto - desc,
            "base_desconto": w if base_desconto is None else base_desconto}


def desagio_por_preco_arr(r: dict, preco_kwh: float) -> Optional[float]:
    """Desagio d equivalente a um preco ao dono P (R$/kWh, liquido do Fio B), com s fixo (igual a desagio_por_preco)."""
    return 1.0 - (1000.0 * preco_kwh + r["custo_fb"] - r["s"] * r["Y"]) / r["U"] if r["U"] else None


def arrendamento_mes(p: Premissas, r: dict, q_usina: float, q_vendida: float, ativos: float) -> dict:
    """Arrendamento em um mes (R$/mes). r = arrendamento_mwh do ano; q_usina = energia compensada da usina (MWh/mes),
    paga ao dono; q_vendida = energia dos clientes ativos (MWh/mes). Como no painel: margem = margem por MWh x q_usina
    (sem custos da Thopen, sem inadimplencia, sem vacancia). Versao comparavel ao Asset: o cliente paga
    (energia + fatura da distribuidora) x (1 - inadimplencia); a Thopen paga a fatura da distribuidora, a recorrencia
    do canal e o dono (pela usina inteira); impostos sobre o que sobra (so se positivo, como no Hoje); custo de
    operacao e G&A por cliente ativo."""
    gerador = r["dono"] * q_usina
    energia = r["cliente_paga"] * q_vendida
    df = p.fatura_dist * ativos
    arr = (energia + df) * (1.0 - p.inadimplencia)
    receita = arr - df - p.recorrencia_canal * ativos - gerador
    imp = receita * p.impostos if receita > 0 else 0.0
    custos = (p.custo_operacao + ga_por_ativo(p)) * ativos
    return {"gerador": gerador, "margem_painel": r["margem"] * q_usina, "energia_cliente": energia, "arrecadacao": arr,
            "desconto_cliente": r["desconto"] * q_vendida,
            "receita": receita, "impostos": imp, "custos": custos, "thopen": receita - imp - custos,
            "q_usina": q_usina, "q_vendida": q_vendida}


def projecao_arrendamento_asset(p: Premissas, anos: int, tar: dict, pct_fb: list[float], consumo: float,
                                s: float = 0.5, c: float = 0.0, d: Optional[float] = 0.3,
                                preco_kwh: Optional[float] = None, fb_com_impostos: bool = True,
                                reaj_tarifa: float = 0.0, reaj_custos: float = 0.0,
                                q_usina_mes: Optional[float] = None, degradacao: float = 0.0,
                                ocupacao_ano1: Optional[float] = None, base_desconto: str = "painel",
                                fatores_fio_b: Optional[list[float]] = None) -> list[dict]:
    """Arrendamento ano a ano, na mesma linha do tempo da projecao do Asset. tar = {tusd, te, icms, pc, flag_te,
    flag_tusd, fio_b_scee} (R$/MWh, como tarifas_asset.json); pct_fb[t] = % do Fio B no ano civil (GD I = 0).
    TUSD, TE e Fio B SCEE x (1 + reajuste da tarifa)^t (como projecao_arrendamento do painel); preco_kwh (se dado)
    e reajustado pelo mesmo indice e o d de cada ano sai de desagio_por_preco. Energia vendida = clientes ativos x
    consumo; energia da usina = q_usina_mes x (1 - degradacao)^t (sem capacidade: = energia vendida). Valores anuais.
    base_desconto: "painel" (desconto = c x W_I, como calc_arrendamento) ou "asset" (desconto = c x tarifa compensavel
    do ano = credito do ano / consumo, com os fatores do Fio B da projecao do Asset; na pagina, "asset" e o padrao):
    o cliente recebe o mesmo desconto em R$ nos dois modelos. desconto_cliente = R$ por cliente ativo por mes."""
    out = []
    for t in range(int(anos)):
        f = (1.0 + reaj_tarifa) ** t
        fb = tar["fio_b_scee"] * f * pct_fb[t] if tar.get("fio_b_scee") else 0.0
        args = (tar["tusd"] * f, tar["te"] * f, tar["icms"], tar["pc"], tar["flag_te"], tar["flag_tusd"])
        oc = ocupacao_ano1 if (t == 0 and ocupacao_ano1 is not None) else None
        ff = fatores_fio_b[t] if fatores_fio_b else 1.0
        pt = premissas_ano(p, t, reaj_tarifa, reaj_custos, ff, oc, degradacao)
        bd = pt.credito / consumo * 1000.0 if base_desconto == "asset" else None
        dt = d
        if preco_kwh is not None:
            r0 = arrendamento_mwh(*args, s, 0.0, c, fb, fb_com_impostos, bd)
            dt = desagio_por_preco_arr(r0, preco_kwh * f)
        r = arrendamento_mwh(*args, s, dt, c, fb, fb_com_impostos, bd)
        ativos = pt.clientes * pt.ocupacao if pt.clientes else 1.0
        qv = ativos * consumo / 1000.0
        qu = q_usina_mes * (1.0 - degradacao) ** t if q_usina_mes is not None else qv
        m = arrendamento_mes(pt, r, qu, min(qv, qu), ativos)
        out.append({"t": t, "d": dt, "mwh": r, "mes": m, "ativos": ativos, "premissas": pt,
                    "desconto_cliente": r["desconto"] * consumo / 1000.0,
                    **{k: m[k] * 12 for k in ("gerador", "margem_painel", "thopen", "receita")}})
    return out
