"""Visao de negociacao do arrendamento: calculo reverso (resolver_arrendamento) e projecao do contrato
(projecao_arrendamento), conferidos contra uma implementacao de referencia independente.

Uso:  py tests/test_negociacao.py   (ou pytest)
"""
from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import monitor_tarifas as mt  # noqa: E402

CPFL = dict(tusd=449.25, te=290.20, icms=0.18, pc=0.05, flag_te=1, flag_tusd=0)
ENEL = dict(tusd=472.42, te=316.96, icms=0.18, pc=0.00945 + 0.04369, flag_te=1, flag_tusd=0)
ENEL_FIOB = 212.0774


def ref_base(D, E, i, pc, fte, ftu):
    k_pc = 1 / (1 - pc)
    k_full = 1 / ((1 - i) * (1 - pc))
    W = D * (k_full if ftu else k_pc) + E * (k_full if fte else k_pc)
    return D + E, W, W - (D + E), k_full


def ref_d_pedido(dono, U, Y, cfb, s):
    return 1 - (dono + cfb - s * Y) / U


def ref_d_min(m, U, W, Y, s, c):
    return (m + c * W - (1 - s) * Y) / U


def test_ida_e_volta_pedido_cpfl():
    r = mt.calc_arrendamento(**CPFL, s=0.5, d=0.3)
    assert round(r["dono"], 2) == 570.60
    o = mt.resolver_arrendamento(r, "d", dono=r["dono"])
    assert o["ok"] and abs(o["valor"] - 0.30) < 1e-12, o
    # pedido arredondado de 570,60 R$/MWh devolve 30% (a menos do arredondamento do centavo)
    o2 = mt.resolver_arrendamento(r, "d", dono=570.60)
    assert o2["ok"] and round(o2["valor"] * 100, 2) == 30.00, o2["valor"]
    U, W, Y, _ = ref_base(449.25, 290.20, 0.18, 0.05, 1, 0)
    assert abs(o2["valor"] - ref_d_pedido(570.60, U, Y, 0.0, 0.5)) < 1e-12


def test_margem_minima_igual_atual_devolve_d():
    for c in (0.0, 0.05):
        r = mt.calc_arrendamento(**ENEL, s=0.5, d=0.3, c=c, fb_ef=ENEL_FIOB * 0.6)
        o = mt.resolver_arrendamento(r, "d", margem=r["margem"])
        assert o["ok"] and abs(o["valor"] - 0.30) < 1e-12, o
        assert abs(o["dono_max"] - r["dono"]) < 1e-9  # teto = dono atual quando a margem minima e a atual
        U, W, Y, kf = ref_base(472.42, 316.96, 0.18, 0.00945 + 0.04369, 1, 0)
        assert abs(o["valor"] - ref_d_min(r["margem"], U, W, Y, 0.5, c)) < 1e-12


def test_resolver_s_devolve_s_original():
    for s0 in (0.3, 0.5, 0.7):
        r = mt.calc_arrendamento(**ENEL, s=s0, d=0.25, fb_ef=ENEL_FIOB * 0.75)
        o = mt.resolver_arrendamento(r, "s", dono=r["dono"])
        assert o["ok"] and abs(o["valor"] - s0) < 1e-12, (s0, o)
        o2 = mt.resolver_arrendamento(r, "s", margem=r["margem"])
        assert o2["ok"] and abs(o2["valor"] - s0) < 1e-12, (s0, o2)


def test_linearidade_resultado_resolvido():
    r = mt.calc_arrendamento(**ENEL, s=0.5, d=0.3, fb_ef=ENEL_FIOB * 0.6)
    for alvo in (300.0, 400.0, 500.0):
        o = mt.resolver_arrendamento(r, "d", dono=alvo)
        chk = mt.calc_arrendamento(**ENEL, s=0.5, d=o["valor"], fb_ef=ENEL_FIOB * 0.6)
        assert abs(chk["dono"] - alvo) < 1e-9 and abs(o["margem"] - chk["margem"]) < 1e-9


def test_sem_solucao():
    r = mt.calc_arrendamento(**CPFL, s=0.5, d=0.3)
    o = mt.resolver_arrendamento(r, "d", dono=r["W_I"] * 2)  # pede mais que tudo: d negativo
    assert not o["ok"] and o["valor"] < 0 and "fora de 0 a 100%" in o["motivo"]
    o = mt.resolver_arrendamento(r, "d", dono=-100.0)  # d acima de 100%
    assert not o["ok"] and o["valor"] > 1
    o = mt.resolver_arrendamento(r, "s", dono=r["dono"] + 200)  # s acima de 100%
    assert not o["ok"] and o["valor"] > 1
    o = mt.resolver_arrendamento(r, "d", margem=r["W_I"])  # margem igual a W_I: d > 1
    assert not o["ok"] and o["valor"] > 1 and abs(o["dono_max"]) < 1e-9  # teto zero (GD I, c = 0)
    # Y = 0: sem ICMS e sem PIS/COFINS, o VB nao muda nada
    r0 = mt.calc_arrendamento(400.0, 300.0, 0.0, 0.0, 1, 1, s=0.5, d=0.3)
    assert abs(r0["Y"]) < 1e-12
    o = mt.resolver_arrendamento(r0, "s", dono=400.0)
    assert not o["ok"] and o["valor"] is None and "Y = 0" in o["motivo"]
    # U = 0
    ru = mt.calc_arrendamento(0.0, 0.0, 0.18, 0.05, 1, 1)
    o = mt.resolver_arrendamento(ru, "d", dono=10.0)
    assert not o["ok"] and o["valor"] is None and "U = 0" in o["motivo"]


def test_projecao_gd2_enel_escalonamento():
    p = mt.projecao_arrendamento(**ENEL, s=0.5, d=0.3, fiob=ENEL_FIOB, gd=2, mwh_mes=1.0, prazo=10, ano_ini=2026,
                                 reajuste=0.0, degradacao=0.0, taxa=0.12)
    a = {x["ano"]: x for x in p["anos"]}
    assert [x["pct_fb"] for x in p["anos"]] == [0.6, 0.75, 0.9] + [1.0] * 7
    U, W, Y, kf = ref_base(472.42, 316.96, 0.18, 0.00945 + 0.04369, 1, 0)
    for ano, pct in ((2026, 0.6), (2027, 0.75), (2028, 0.9), (2030, 1.0)):
        esperado = (0.7 * U + 0.5 * Y - ENEL_FIOB * pct * kf) * 12
        assert abs(a[ano]["dono"] - esperado) < 1e-9, (ano, a[ano]["dono"], esperado)
    assert a[2027]["dono"] < a[2026]["dono"]
    assert abs((a[2026]["dono"] - a[2027]["dono"]) - ENEL_FIOB * 0.15 * kf * 12) < 1e-9
    assert round(a[2026]["dono_mwh"], 2) == 447.57  # igual ao card do Arrendamento (GD II 2026)
    assert all(abs(x["thopen"] - a[2026]["thopen"]) < 1e-9 for x in p["anos"])  # Fio B nao afeta a Thopen


def test_projecao_gd1_constante():
    p = mt.projecao_arrendamento(**CPFL, s=0.5, d=0.3, fiob=150.0, gd=1, mwh_mes=100.0, prazo=10, ano_ini=2026,
                                 reajuste=0.0, degradacao=0.0)
    v = [x["dono"] for x in p["anos"]]
    assert all(abs(x - v[0]) < 1e-9 for x in v) and all(x["custo_fb"] == 0 for x in p["anos"])
    r = mt.calc_arrendamento(**CPFL, s=0.5, d=0.3)
    assert abs(v[0] - r["dono"] * 100 * 12) < 1e-6 and round(v[0] / 1200, 2) == 570.60  # 570,60 R$/MWh x 1.200 MWh


def test_projecao_reajuste_degradacao_e_fio_b_fixo():
    p = mt.projecao_arrendamento(**ENEL, s=0.5, d=0.3, fiob=ENEL_FIOB, gd=2, mwh_mes=10.0, prazo=3, ano_ini=2026,
                                 reajuste=0.05, degradacao=0.01, escalonar=False, pct_fb=0.6)
    U, W, Y, kf = ref_base(472.42, 316.96, 0.18, 0.00945 + 0.04369, 1, 0)
    for x in p["anos"]:
        f = 1.05 ** x["t"]
        esperado = (0.7 * U + 0.5 * Y - ENEL_FIOB * 0.6 * kf) * f * 10 * 0.99 ** x["t"] * 12
        assert x["pct_fb"] == 0.6 and abs(x["dono"] - esperado) < 1e-6


def test_vpl_a_mao():
    # GD I CPFL, 1 MWh/mes, 3 anos, sem reajuste: fluxo anual do dono = 570,6025... x 12
    p = mt.projecao_arrendamento(**CPFL, s=0.5, d=0.3, gd=1, mwh_mes=1.0, prazo=3, ano_ini=2026, taxa=0.10)
    f = p["anos"][0]["dono"]
    assert abs(p["vpl"]["dono"] - (f / 1.1 + f / 1.21 + f / 1.331)) < 1e-9
    assert abs(p["total"]["dono"] - 3 * f) < 1e-9
    # com numeros redondos: 1.000 por ano a 10% em 3 anos = 2.486,85
    assert round(1000 / 1.1 + 1000 / 1.21 + 1000 / 1.331, 2) == 2486.85
    assert abs(p["vpl"]["dono"] / f * 1000 - 2486.852) < 1e-3


def test_identidade_anual():
    p = mt.projecao_arrendamento(**ENEL, s=0.6, d=0.35, c=0.05, fiob=ENEL_FIOB, gd=3, mwh_mes=50.0, prazo=10,
                                 ano_ini=2026, reajuste=0.04, degradacao=0.005)
    for x in p["anos"]:
        q = x["mwh_ano"]
        assert abs(x["dono"] + x["thopen"] + x["custo_fb_ano"] - x["W_I"] * q) < 1e-6
        assert abs(x["margem"] - (x["thopen"] - 0.05 * x["W_I"] * q)) < 1e-6


if __name__ == "__main__":
    import inspect
    n = 0
    for nome, f in list(globals().items()):
        if nome.startswith("test_") and inspect.isfunction(f):
            f()
            n += 1
    print(f"{n} testes OK")
