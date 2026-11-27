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
