"""Regressao da calculadora GD (calc_gd) contra a planilha Tarifas.xlsx do usuario.

Uso:  py tests/test_calculadora.py [caminho/Tarifas.xlsx]   (ou pytest; a planilha tambem pode vir em TARIFAS_XLSX)
Sem a planilha, roda os casos fixos e as checagens dos CSVs; com ela, confere as 48 linhas contra o cache.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import monitor_tarifas as mt  # noqa: E402


def _r2(v):
    return round(v + 1e-9, 2)


def test_cpfl_paulista():
    r = mt.calc_gd(449.25, 290.20, icms=0.18, pc=0.05, flag_te=1, flag_tusd=0, s=0.5)
    assert (_r2(r["V"]), _r2(r["W"]), _r2(r["Y"]), _r2(r["X"]), _r2(r["Z"])) == (949.23, 845.42, 105.97, 52.99, 792.44), r


def test_cemig():
    r = mt.calc_gd(593.08, 310.21, icms=0.18, pc=0.06, flag_te=1, flag_tusd=1, s=0.7)
    assert _r2(r["V"]) == _r2(r["W"]) == 1171.89, r
    assert (_r2(r["X"]), _r2(r["Z"])) == (188.02, 1091.31), r


def test_gd2_identidades():
    a = mt.calc_gd(472.42, 316.96, 0.18, 0.05313, 1, 0, 0.5)
    b = mt.calc_gd(472.42, 316.96, 0.18, 0.05313, 1, 0, 0.5, fb_ef=0.0)
    assert a == b  # GD I = FB_ef 0
    fb = 120.0 * 0.6
    c = mt.calc_gd(472.42, 316.96, 0.18, 0.05313, 1, 0, 0.5, fb_ef=fb, preco=500.0)
    assert abs(c["Y"] - (c["W"] - (c["U"] - fb))) < 1e-9
    assert abs(c["Z"] - ((c["U"] - fb) + 0.5 * c["Y"])) < 1e-9
    assert abs(c["fb_res"] - fb * c["k_full"]) < 1e-9
    assert abs(c["desagio"] - (1 - 500.0 / c["W"])) < 1e-12 and abs(c["economia"] - (c["W"] - 500.0)) < 1e-9
    assert mt.escalonamento_ano(2026) == 0.6 and mt.escalonamento_ano(2022) == 0 and mt.escalonamento_ano(2030) == 1.0


def test_leitor_aceita_virgula_e_ponto():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        (p / "x.csv").write_text("cnpj;a;b\n1;0,1800;0.18\n", encoding="utf-8")
        df = mt.ler_csv_pv(p, "x.csv")
        assert mt.para_numero(df.loc[0, "a"]) == mt.para_numero(df.loc[0, "b"]) == 0.18
        assert df.loc[0, "cnpj"] == "00000000000001"


def test_meta_impostos():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / mt.ARQ_IMPOSTOS_META).write_text(
            '{"gerado_em": "2026-11-24T11:34:46-03:00", "mes_ref_inicio": "2026-09", "mes_ref_fim": "2026-11"}',
            encoding="utf-8")
        m = mt.meta_impostos(base)
        assert m["selo"] == "impostos atualizados em 24/11, faturas de 09/26 a 11/26", m
        assert "set-nov/2026" in m["origem"] and "24/11/2026" in m["origem"], m
        (base / mt.ARQ_IMPOSTOS_META).write_text("{quebrado", encoding="utf-8")
        assert mt.meta_impostos(base)["selo"] == ""  # arquivo ilegivel: sem selo, origem fixa


def test_csvs_do_repositorio():
    ref = mt.ler_csv_pv(RAIZ, mt.ARQ_REF_PLANILHA)
    gd = mt.ler_csv_pv(RAIZ, mt.ARQ_ICMS_GD)
    imp = mt.ler_csv_pv(RAIZ, mt.ARQ_IMPOSTOS)
    assert len(ref) == 48 and ref["sigla"].is_unique
    assert gd["cnpj"].is_unique and (gd["fonte"].str.startswith("planilha").sum() == 48)
    assert set(ref["sigla"]) <= set(gd["distribuidora"])
    assert "fator_gross_up" not in imp.columns and imp["cnpj"].str.len().eq(14).all()


def _planilha() -> Path | None:
    for c in [*(a for a in sys.argv[1:] if a.lower().endswith(".xlsx")), os.environ.get("TARIFAS_XLSX", "")]:
        if c and Path(c).exists():
            return Path(c)
    return None


def test_48_linhas_contra_cache_da_planilha():
    """Entradas tiradas dos CSVs do repositorio (TUSD/TE, aliquotas, flags e s); esperado = cache da planilha."""
    xlsx = _planilha()
    if xlsx is None:
        print("  (pulado: informe a planilha Tarifas.xlsx como argumento ou em TARIFAS_XLSX)")
        return
    import openpyxl
    ws = openpyxl.load_workbook(xlsx, data_only=True)["Tarifas"]
    ref = mt.ler_csv_pv(RAIZ, mt.ARQ_REF_PLANILHA).set_index("sigla")
    gd = mt.ler_csv_pv(RAIZ, mt.ARQ_ICMS_GD).set_index("distribuidora")
    n = maior = 0
    for r in range(2, ws.max_row + 1):
        sig = ws[f"A{r}"].value
        if not sig:
            continue
        g, x = gd.loc[sig], ref.loc[sig]
        num = mt.para_numero
        res = mt.calc_gd(num(x["tusd"]), num(x["te"]), num(g["icms_planilha"]),
                         num(g["pis_planilha"]) + num(g["cofins_planilha"]), int(g["flag_icms_te"]),
                         int(g["flag_icms_tusd"]), num(g["repasse_s"]))
        for col, k in (("U", "U"), ("V", "V"), ("W", "W"), ("X", "X"), ("Y", "Y"), ("Z", "Z")):
            if col == "V" and r == 34:  # V34 (Equatorial CEEE) sem ICMS na planilha: erro mantido fora da regressao
                continue
            dif = abs(res[k] - ws[f"{col}{r}"].value)
            maior = max(maior, dif)
            assert dif < 1e-6, (r, sig, col, res[k], ws[f"{col}{r}"].value)
        n += 1
    assert n == 48, n
    print(f"  48 linhas conferidas; maior diferenca {maior:.2e}")


if __name__ == "__main__":
    falhas = 0
    for nome, f in list(globals().items()):
        if nome.startswith("test_") and callable(f):
            try:
                f()
                print(f"ok    {nome}")
            except AssertionError as e:
                falhas += 1
                print(f"FALHA {nome}: {e}")
    sys.exit(1 if falhas else 0)
