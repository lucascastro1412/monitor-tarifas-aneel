"""Calculadora de arrendamento de usinas (calc_arrendamento), opcao A: desagio so sobre a tarifa, beneficio
fiscal dividido pelo VB, Fio B pago pelo dono.

Uso:  py tests/test_arrendamento.py   (ou pytest)
"""
from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import monitor_tarifas as mt  # noqa: E402


def referencia(D, E, i, pc, fte, ftu, d, s, c=0.0, fb_ef=0.0, fb_com_impostos=True):
    """Implementacao independente das regras aprovadas (por MWh compensado)."""
    k_pc = 1 / (1 - pc)
    k_full = 1 / ((1 - i) * (1 - pc))
    k_te = k_full if fte else k_pc
    k_tusd = k_full if ftu else k_pc
    U = D + E
    W_I = D * k_tusd + E * k_te
    Y = W_I - U
    custo_fb = fb_ef * k_full if fb_com_impostos else fb_ef
    dono = (1 - d) * U + s * Y - custo_fb
    thopen = d * U + (1 - s) * Y
    return {"U": U, "W_I": W_I, "Y": Y, "custo_fb": custo_fb, "dono": dono, "thopen": thopen,
            "margem": thopen - c * W_I, "desagio_impl": 1 - dono / U}


def _r2(v):
    return round(v + 1e-9, 2)


def test_cpfl_paulista_gd1():
    r = mt.calc_arrendamento(449.25, 290.20, icms=0.18, pc=0.05, flag_te=1, flag_tusd=0, s=0.5, d=0.3)
    assert _r2(r["U"]) == 739.45 and _r2(r["W_I"]) == 845.42 and _r2(r["Y"]) == 105.97, r
    assert r["custo_fb"] == 0
    assert _r2(r["dono"]) == 570.60, r["dono"]      # 0,7 x 739,45 + 52,99
    assert _r2(r["thopen"]) == 274.82, r["thopen"]  # 221,84 + 52,99
    assert abs(r["dono"] + r["thopen"] - r["W_I"]) < 1e-9
    assert round(_r2(r["dono"]) + _r2(r["thopen"]), 2) == 845.42
    ref = referencia(449.25, 290.20, 0.18, 0.05, 1, 0, 0.3, 0.5)
    for k in ("U", "W_I", "Y", "dono", "thopen", "desagio_impl"):
        assert abs(r[k] - ref[k]) < 1e-9, k


def test_gd1_sem_custo_fio_b():
    for fte, ftu in ((1, 1), (1, 0), (0, 1), (0, 0)):
        r = mt.calc_arrendamento(472.42, 316.96, 0.18, 0.05313, fte, ftu, s=0.7, d=0.25, fb_ef=0.0)
        assert r["custo_fb"] == r["custo_fb_com"] == r["custo_fb_sem"] == 0


def test_identidade_dono_thopen_fio_b():
    casos = [(472.42, 316.96, 0.18, 0.05313, 1, 0), (593.08, 310.21, 0.18, 0.06, 1, 1), (300.0, 250.0, 0.2, 0.0925, 0, 0)]
    for D, E, i, pc, fte, ftu in casos:
        for d in mt.ARR_GRADE_D:
            for s in mt.ARR_GRADE_S:
                for fb, com in ((0.0, True), (127.25, True), (127.25, False), (212.08, True)):
                    for c in (0.0, 0.1):
                        r = mt.calc_arrendamento(D, E, i, pc, fte, ftu, s=s, d=d, c=c, fb_ef=fb, fb_com_impostos=com)
                        assert abs(r["dono"] + r["thopen"] + r["custo_fb"] - r["W_I"]) < 1e-9
                        ref = referencia(D, E, i, pc, fte, ftu, d, s, c, fb, com)
                        for k in ref:
                            assert abs(r[k] - ref[k]) < 1e-9, (k, r[k], ref[k])
                        assert abs(r["desconto"] - c * r["W_I"]) < 1e-9
                        assert abs(r["custo_fb_com"] - fb * r["k_full"]) < 1e-9 and r["custo_fb_sem"] == fb


def test_w_i_igual_calculadora_gd1():
    g = mt.calc_gd(449.25, 290.20, 0.18, 0.05, 1, 0, 0.5)
    r = mt.calc_arrendamento(449.25, 290.20, 0.18, 0.05, 1, 0, s=0.5, d=0.3, fb_ef=50.0)
    assert abs(r["W_I"] - g["W"]) < 1e-12 and abs(r["Y"] - g["Y"]) < 1e-12  # W_I nao depende do Fio B


# Enel SP (vigencia atual do painel em 24/11/2026): TUSD, TE, Fio B SCEE, ICMS, PIS+COFINS e flags 1/0
ENEL = dict(tusd=472.42, te=316.96, icms=0.18, pc=0.00945 + 0.04369, flag_te=1, flag_tusd=0)
ENEL_FB_EF = 212.0774 * 0.6  # GD II 2026: 60% do Fio B SCEE


def test_desagio_implicito_cpfl_s50_s70():
    a = mt.calc_arrendamento(449.25, 290.20, icms=0.18, pc=0.05, flag_te=1, flag_tusd=0, s=0.5, d=0.3)
    b = mt.calc_arrendamento(449.25, 290.20, icms=0.18, pc=0.05, flag_te=1, flag_tusd=0, s=0.7, d=0.3)
    assert _r2(a["dono"]) == 570.60 and _r2(b["dono"]) == 591.80, (a["dono"], b["dono"])
    assert abs(a["desagio_impl"] - (1 - a["dono"] / a["U"])) < 1e-12
    assert round(a["desagio_impl"] * 100, 2) == 22.83, a["desagio_impl"]   # 1 - 570,60 / 739,45
    assert round(b["desagio_impl"] * 100, 2) == 19.97, b["desagio_impl"]   # 1 - 591,80 / 739,45 (Y exato 105,9733)
    assert b["desagio_impl"] < a["desagio_impl"]  # mais VB para o dono, menor desagio implicito
    assert a["d"] == b["d"] == 0.3  # o nominal nao muda


def test_desagio_implicito_enel_sp_gd2():
    a = mt.calc_arrendamento(**ENEL, s=0.5, d=0.3, fb_ef=ENEL_FB_EF)
    b = mt.calc_arrendamento(**ENEL, s=0.7, d=0.3, fb_ef=ENEL_FB_EF)
    assert _r2(a["U"]) == 789.38
    assert _r2(a["dono"]) == 447.57 and _r2(b["dono"]) == 471.13, (a["dono"], b["dono"])
    assert round(a["desagio_impl"] * 100, 2) == 43.30, a["desagio_impl"]
    assert round(b["desagio_impl"] * 100, 2) == 40.32, b["desagio_impl"]
    g1 = mt.calc_arrendamento(**ENEL, s=0.5, d=0.3)  # sem Fio B: implicito menor
    assert g1["desagio_impl"] < a["desagio_impl"]


def test_desagio_implicito_negativo():
    r = mt.calc_arrendamento(449.25, 290.20, icms=0.18, pc=0.05, flag_te=1, flag_tusd=0, s=1.0, d=0.0)
    assert abs(r["dono"] - r["W_I"]) < 1e-9  # dono fica com tudo
    assert r["desagio_impl"] < 0 and abs(r["desagio_impl"] - (1 - r["W_I"] / r["U"])) < 1e-12
    assert round(r["desagio_impl"] * 100, 2) == -14.33, r["desagio_impl"]  # 1 - 845,42 / 739,45


def test_desagio_implicito_varia_com_s_na_grade():
    for d in mt.ARR_GRADE_D:
        v = [mt.calc_arrendamento(**ENEL, s=s, d=d, fb_ef=ENEL_FB_EF)["desagio_impl"] for s in mt.ARR_GRADE_S]
        assert all(x > y for x, y in zip(v, v[1:])), (d, v)


def test_u_zero_nao_quebra():
    r = mt.calc_arrendamento(0.0, 0.0, 0.18, 0.05, 1, 1)
    assert r["desagio_impl"] is None


def test_geracao():
    assert abs(mt.geracao_mwh_mes("ufv", 1000, 1440) - 120.0) < 1e-9
    assert abs(mt.geracao_mwh_mes("ufv", 1000, 1440, perdas=0.1, pct_compensado=0.5) - 54.0) < 1e-9
    assert abs(mt.geracao_mwh_mes("cgh", 1000) - 365.0) < 1e-9  # 1.000 kW x 50% x 730 h
    assert abs(mt.geracao_mwh_mes("direto", mwh_mes=80.0, pct_compensado=0.9) - 72.0) < 1e-9


def test_csv_produtividade():
    p = mt.ler_produtividade(RAIZ)
    assert len(p) == 27, sorted(p)
    assert all(1200 <= v <= 1800 and f for v, f in p.values())
    assert p["RS"][0] < p["SP"][0] < p["BA"][0]


if __name__ == "__main__":
    falhas = 0
    for nome, f in list(globals().items()):
        if nome.startswith("test_") and callable(f):
            try:
                f()
                print(f"ok    {nome}")
            except AssertionError as e:
                falhas += 1
                print(f"FALHA {nome}: {e!r}")
    sys.exit(1 if falhas else 0)
