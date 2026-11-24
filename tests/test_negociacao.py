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


# ------------------------------------------------ modo preco (R$/kWh) e desagios sobre U, V e W_I

def test_preco_kwh_cpfl_gd1():
    r = mt.calc_arrendamento(**CPFL, s=0.5, d=0.3)
    assert round(r["U"], 2) == 739.45 and round(r["V"], 2) == 949.23 and round(r["W_I"], 2) == 845.42
    d = mt.desagio_por_preco(r, 0.5706)
    assert abs(d - 0.30) < 1e-5, d
    assert round(r["preco_kwh"], 4) == 0.5706
    assert round(r["desagio_v"] * 100, 2) == 39.89 and abs(r["desagio_v"] - (1 - r["dono"] / r["V"])) < 1e-12
    assert round(r["desagio_wi"] * 100, 2) == 32.51 and abs(r["desagio_wi"] - (1 - r["dono"] / r["W_I"])) < 1e-12
    assert round(1 - 570.60 / 949.23, 4) == 0.3989 and round(1 - 570.60 / 845.42, 4) == 0.3251


def test_preco_kwh_enel_gd2_2026():
    r = mt.calc_arrendamento(**ENEL, s=0.5, d=0.3, fb_ef=ENEL_FIOB * 0.6)
    assert round(r["dono"], 2) == 447.57 and round(r["preco_kwh"], 5) == 0.44757
    assert round(r["U"], 2) == 789.38 and round(r["desagio_impl"] * 100, 2) == 43.30
    assert round(r["V"], 2) == 1016.69 and round(r["desagio_v"] * 100, 2) == 55.98
    U, W, Y, kf = ref_base(472.42, 316.96, 0.18, 0.00945 + 0.04369, 1, 0)
    assert abs(r["V"] - U * kf) < 1e-9


def test_ida_e_volta_modos():
    for kw in (dict(**CPFL), dict(**ENEL, fb_ef=ENEL_FIOB * 0.6)):
        for d0 in (0.0, 0.2, 0.3, 0.45):
            r = mt.calc_arrendamento(**kw, s=0.5, d=d0)
            d = mt.desagio_por_preco(r, r["preco_kwh"])  # desagio -> preco -> desagio
            assert abs(d - d0) < 1e-12
            r2 = mt.calc_arrendamento(**kw, s=0.5, d=d)  # preco -> desagio -> preco
            assert abs(r2["preco_kwh"] - r["preco_kwh"]) < 1e-12
    # preco 0,45 na Enel SP GD II: d equivalente bate com o calculo reverso do pedido de 450 R$/MWh
    r = mt.calc_arrendamento(**ENEL, s=0.5, d=0.3, fb_ef=ENEL_FIOB * 0.6)
    o = mt.resolver_arrendamento(r, "d", dono=450.0)
    assert abs(mt.desagio_por_preco(r, 0.45) - o["valor"]) < 1e-12
    # fora de [0, 1]: calcula mesmo assim
    assert mt.desagio_por_preco(r, 2.0) < 0 and mt.desagio_por_preco(r, -0.5) > 1


def test_projecao_preco_fixo():
    reaj = 0.045
    p = mt.projecao_arrendamento(**ENEL, s=0.5, d=0.3, fiob=ENEL_FIOB, gd=2, mwh_mes=1.0, prazo=6, ano_ini=2026,
                                 reajuste=reaj, fixar="preco", preco_kwh=0.45)
    ds = [a["d"] for a in p["anos"]]
    for a in p["anos"]:
        assert abs(a["preco_kwh"] - 0.45 * (1 + reaj) ** a["t"]) < 1e-12, a
        assert abs(a["dono_mwh"] - 450 * (1 + reaj) ** a["t"]) < 1e-9
    assert len({round(x, 9) for x in ds}) > 1 and ds[1] < ds[0]  # Fio B 60% -> 75%: d cai para manter o preco
    # sem preco informado: usa o preco do ano 1 com o d informado; ano 1 identico ao modo desagio
    pd_ = mt.projecao_arrendamento(**ENEL, s=0.5, d=0.3, fiob=ENEL_FIOB, gd=2, prazo=3, ano_ini=2026, reajuste=reaj)
    pp = mt.projecao_arrendamento(**ENEL, s=0.5, d=0.3, fiob=ENEL_FIOB, gd=2, prazo=3, ano_ini=2026, reajuste=reaj,
                                  fixar="preco")
    assert abs(pp["anos"][0]["d"] - 0.3) < 1e-12 and abs(pp["anos"][0]["dono"] - pd_["anos"][0]["dono"]) < 1e-9
    assert abs(pp["preco_kwh"] - pd_["anos"][0]["preco_kwh"]) < 1e-12
    assert all(a["d"] == 0.3 for a in pd_["anos"])


# ---------------------------------------------------------------- faixa de negociacao (Abertura, Alvo, Limite)

def _cpfl_gd1_preco(preco_kwh, s=0.0, c=0.20):
    """CPFL Paulista GD I configurada por preco ao dono (R$/kWh), VB do dono s, desconto ao cliente c."""
    r0 = mt.calc_arrendamento(**CPFL, s=s, d=0.0, c=c)
    return mt.calc_arrendamento(**CPFL, s=s, d=mt.desagio_por_preco(r0, preco_kwh), c=c)


def test_faixa_alvo_acima_do_limite():
    # caso real: pedido R$ 0,65/kWh, VB 0%, desconto 20%, margem minima 20% do valor compensado
    r = _cpfl_gd1_preco(0.65)
    mg = 0.20 * r["W_I"]
    f = mt.faixa_negociacao(r, mg)
    ab, al, li = f["abertura"], f["alvo"], f["limite"]
    assert f["acima_limite"] and al["margem"] < mg
    assert ab["preco_kwh"] < li["preco_kwh"] < al["preco_kwh"]  # faixa nao inverte
    assert ab["margem"] >= mg - 1e-9 and li["margem"] >= mg - 1e-9
    assert abs(ab["d"] - min(1.0, li["d"] + mt.NEG_ABERTURA_PP)) < 1e-12  # abertura parte do limite
    assert f["ordem"] == ["abertura", "limite", "alvo"] and "acima do limite" in f["rotulos"]["alvo"]
    e = f["excesso"]
    assert abs(e["mwh"] - (al["dono"] - li["dono"])) < 1e-9 and abs(e["kwh"] - e["mwh"] / 1000) < 1e-12
    assert abs(e["pp"] - (li["d"] - al["d"]) * 100) < 1e-9 and e["mwh"] > 0
    # resolvendo o VB: VB maximo abaixo do atual, abertura com VB do limite e deságio + 10 p.p.
    r2 = _cpfl_gd1_preco(0.65, s=0.5)
    f2 = mt.faixa_negociacao(r2, 0.20 * r2["W_I"], var="s")
    if f2["acima_limite"]:
        assert f2["abertura"]["preco_kwh"] <= f2["limite"]["preco_kwh"] < f2["alvo"]["preco_kwh"]


def test_faixa_alvo_dentro_do_limite():
    for preco in (0.40, 0.45, 0.50):
        r = _cpfl_gd1_preco(preco)
        mg = 0.20 * r["W_I"]
        f = mt.faixa_negociacao(r, mg)
        ab, al, li = f["abertura"], f["alvo"], f["limite"]
        assert not f["acima_limite"] and f["excesso"] is None
        assert ab["preco_kwh"] <= al["preco_kwh"] <= li["preco_kwh"], preco
        assert abs(ab["d"] - min(1.0, al["d"] + mt.NEG_ABERTURA_PP)) < 1e-12  # regra antiga mantida
        assert f["ordem"] == ["abertura", "alvo", "limite"] and f["rotulos"]["alvo"] == "Alvo"
    # deságio alto: abertura limitada a 100%, nunca acima do alvo em preco
    r = mt.calc_arrendamento(**CPFL, s=0.0, d=0.95, c=0.2)
    f = mt.faixa_negociacao(r, 0.20 * r["W_I"])
    assert f["abertura"]["d"] == 1.0 and f["abertura"]["preco_kwh"] <= f["alvo"]["preco_kwh"]


def test_faixa_desagio_minimo_igual_ao_limite():
    r = _cpfl_gd1_preco(0.65)
    mg = 0.20 * r["W_I"]
    f = mt.faixa_negociacao(r, mg)
    exato = mt.resolver_arrendamento(r, "d", margem=mg)["valor"]
    # o deságio mínimo mostrado e o d da coluna Limite sao o mesmo numero (arredondado para cima a 0,01 p.p.)
    assert f["valor_limite"] == f["limite"]["d"]
    assert f"{f['valor_limite'] * 100:.2f}" == f"{f['limite']['d'] * 100:.2f}"
    assert exato <= f["valor_limite"] < exato + 1e-4 + 1e-12
    assert abs(f["teto"] - f["limite"]["dono"]) < 1e-12 and f["teto"] <= f["dono_max"] + 1e-9


if __name__ == "__main__":
    import inspect
    n = 0
    for nome, f in list(globals().items()):
        if nome.startswith("test_") and inspect.isfunction(f):
            f()
            n += 1
    print(f"{n} testes OK")
