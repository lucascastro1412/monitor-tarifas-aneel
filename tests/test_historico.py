"""Snapshot cumulativo: linhas que somem da fonte ficam no historico, sem alerta.

Uso:  py tests/test_historico.py   (ou pytest)
Casos: (a) removida fica no snapshot e no historico; (b) linha que volta; (c) retificacao, a ativa prevalece;
(d) remocao nao gera alerta; (e) guarda de queda.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import monitor_tarifas as mt  # noqa: E402

NA = mt.NA
HOJE = date(2026, 11, 24)
AGORA = datetime(2026, 11, 24, 10, 0, tzinfo=mt.FUSO_BR)
CNPJ_X, CNPJ_Y = "11111111000111", "22222222000122"


def b3(cnpj, sigla, reh, inicio, fim, tusd, te, detalhe=NA):
    """Linha canonica da B3 (classe, subclasse, posto e acessante 'Nao se aplica')."""
    return {"cnpj": cnpj, "distribuidora": sigla, "reh": reh, "inicio": inicio, "fim": fim,
            "base": "Tarifa de Aplicação", "subgrupo": "B3", "modalidade": "Convencional", "classe": NA,
            "subclasse": NA, "detalhe": detalhe, "posto": NA, "unidade": "MWh", "acessante": NA,
            "tusd": tusd, "te": te}


def ga(cnpj, sigla, reh, inicio, fim, posto, unidade, tusd, te):
    return {"cnpj": cnpj, "distribuidora": sigla, "reh": reh, "inicio": inicio, "fim": fim,
            "base": "Tarifa de Aplicação", "subgrupo": "A4", "modalidade": "Verde", "classe": NA, "subclasse": NA,
            "detalhe": NA, "posto": posto, "unidade": unidade, "acessante": NA, "tusd": tusd, "te": te}


def ga_vig(cnpj, sigla, reh, inicio, fim, efp):
    return [ga(cnpj, sigla, reh, inicio, fim, "Fora ponta", "MWh", efp, 0.0),
            ga(cnpj, sigla, reh, inicio, fim, "Ponta", "MWh", efp * 2, 0.0),
            ga(cnpj, sigla, reh, inicio, fim, NA, "kW", 20.0, 0.0)]


def df(linhas, marcas=None):
    d = mt.adicionar_chave(pd.DataFrame(linhas))
    if marcas is not None:
        d[mt.COL_REMOVIDA] = marcas
    return d


def visoes(tarifas, tarifas_a=None):
    vazio = mt.com_marca(mt.serie_vazia(["fiob"]))
    return mt.montar_visoes(tarifas, vazio, None, None, HOJE, tarifas_a, None, ["A4"])


def cfg():
    with tempfile.TemporaryDirectory() as tmp:
        return mt.ler_config(Path(tmp))


# Historico de X: 2015, 2020, 2026; de Y: 2026
HIST = [b3(CNPJ_X, "DX", "REH 1", "2015-07-01", "2016-06-30", 200.0, 100.0),
        b3(CNPJ_X, "DX", "REH 2", "2020-07-01", "2021-06-30", 250.0, 150.0),
        b3(CNPJ_X, "DX", "REH 3", "2026-07-01", "2027-06-30", 300.0, 200.0),
        b3(CNPJ_Y, "DY", "REH 9", "2026-03-01", "2027-02-28", 310.0, 190.0)]


def test_a_removida_fica_no_snapshot_e_no_historico():
    antigo = df(HIST)
    novo = df(HIST[2:])  # a ANEEL tirou 2015 e 2020 da base
    novos, alt, rem, volt, snap = mt.comparar(antigo, novo, ["tusd", "te"], "2026-11-24")
    assert (len(novos), len(alt), len(rem), len(volt)) == (0, 0, 2, 0)
    assert len(snap) == 4
    marcas = dict(zip(snap["inicio"], snap[mt.COL_REMOVIDA]))
    assert marcas["2015-07-01"] == marcas["2020-07-01"] == "2026-11-24"
    assert marcas["2026-07-01"] == marcas["2026-03-01"] == ""
    # gravar e ler de novo preserva a marca
    with tempfile.TemporaryDirectory() as tmp:
        arq = Path(tmp) / "tarifas_b3.csv"
        mt.salvar(snap, arq, ["tusd", "te"])
        lido = mt.carregar(arq, ["tusd", "te"])
    assert sorted(lido[mt.COL_REMOVIDA]) == ["", "", "2026-11-24", "2026-11-24"]
    v = visoes(lido)
    m = v["m_tarifa"]
    assert m.loc["DX", 2015] == 300.0 and m.loc["DX", 2020] == 400.0 and m.loc["DX", 2026] == 500.0
    tl = v["tl_tarifa"]
    assert list(tl[tl["id"] == CNPJ_X]["inicio"]) == ["2015-07-01", "2020-07-01", "2026-07-01"]
    # a marca antiga nao muda numa coleta seguinte
    _, _, rem2, _, snap2 = mt.comparar(snap, novo, ["tusd", "te"], "2026-12-01")
    assert len(rem2) == 0 and sorted(snap2[mt.COL_REMOVIDA]) == ["", "", "2026-11-24", "2026-11-24"]
    # aviso do painel
    av = mt.avisos_qualidade(v, cfg(), {})
    assert any("A ANEEL removeu da base 2 vigências antigas em 24/11/2026" in a for a in av), av


def test_b_linha_que_volta():
    snap = df(HIST, ["2026-11-24", "", "", ""])
    novos, alt, rem, volt, snap2 = mt.comparar(snap, df(HIST), ["tusd", "te"], "2026-12-01")
    assert (len(novos), len(alt), len(rem), len(volt)) == (0, 0, 0, 1)
    assert set(snap2[mt.COL_REMOVIDA]) == {""}
    # voltou com outro valor: e alteracao (gera alerta), e a marca tambem sai
    outra = [dict(HIST[0], tusd=210.0)] + HIST[1:]
    novos, alt, rem, volt, snap3 = mt.comparar(snap, df(outra), ["tusd", "te"], "2026-12-01")
    assert (len(novos), len(alt), len(rem), len(volt)) == (0, 1, 0, 0)
    assert set(snap3[mt.COL_REMOVIDA]) == {""}
    assert snap3.loc[snap3["inicio"] == "2015-07-01", "tusd"].item() == 210.0


def test_c_retificacao_ativa_prevalece():
    # a ANEEL troca a REH de 2026 de X ("REH Z", que ordenaria depois) por "REH A" com outro valor
    retif = b3(CNPJ_X, "DX", "REH A", "2026-07-01", "2027-06-30", 280.0, 190.0)
    novos, alt, rem, volt, snap = mt.comparar(df(HIST[:2] + [dict(HIST[2], reh="REH Z")] + HIST[3:]),
                                              df(HIST[:2] + [retif] + HIST[3:]), ["tusd", "te"], "2026-11-24")
    assert (len(novos), len(rem)) == (1, 1)
    v = visoes(snap)
    tl = v["tl_tarifa"]
    x26 = tl[(tl["id"] == CNPJ_X) & (tl["inicio"] == "2026-07-01")]
    assert len(x26) == 1 and x26["reh"].item() == "REH A" and x26["total"].item() == 470.0
    assert v["m_tarifa"].loc["DX", 2026] == 470.0
    assert v["metricas"].loc["DX", "reh"] == "REH A" and v["metricas"].loc["DX", "total"] == 470.0
    # mesma REH com outro fim (outra chave): a ativa tambem prevalece
    fim = dict(HIST[2], fim="2027-07-31", tusd=301.0)
    _, _, _, _, snap_f = mt.comparar(df(HIST), df(HIST[:2] + [fim] + HIST[3:]), ["tusd", "te"], "2026-11-24")
    assert visoes(snap_f)["m_tarifa"].loc["DX", 2026] == 501.0
    # removida sem substituta continua valendo (2015)
    _, _, _, _, snap_r = mt.comparar(df(HIST), df(HIST[1:]), ["tusd", "te"], "2026-11-24")
    assert visoes(snap_r)["m_tarifa"].loc["DX", 2015] == 300.0
    # Grupo A: mesma regra por distribuidora, subgrupo, modalidade e inicio
    velho = ga_vig(CNPJ_X, "DX", "REH Z", "2026-07-01", "2027-06-30", 400.0)
    novo_a = ga_vig(CNPJ_X, "DX", "REH A", "2026-07-01", "2027-06-30", 350.0)
    antigo_a = ga_vig(CNPJ_X, "DX", "REH 2", "2020-07-01", "2021-06-30", 300.0)
    _, _, rem_a, _, snap_a = mt.comparar(df(antigo_a + velho), df(novo_a), ["tusd", "te"], "2026-11-24")
    assert len(rem_a) == 6
    g = visoes(df(HIST), snap_a)["ga"]
    t = g["tl"]
    assert list(t["inicio"]) == ["2020-07-01", "2026-07-01"] and list(t["efp"]) == [300.0, 350.0]
    assert g["met"]["reh"].item() == "REH A" and g["met"]["efp"].item() == 350.0


def test_d_remocao_nao_gera_alerta():
    c = cfg()
    esc = mt.escopo_grupo_a(c)
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "log_mudancas.csv"
        r = mt.processar_serie("tarifa", df(HIST), df(HIST[2:]), c, esc, log, AGORA)
        assert r["mudancas"]["chaves"] == [] and r["mudancas"]["outras"] == 0
        assert r["salvar"] and len(r["snapshot"]) == 4
        v = visoes(r["snapshot"])
        mud = {"tarifa": r["mudancas"], "fiob": {"chaves": [], "outras": 0},
               "tarifa_a": {"chaves": [], "outras": 0}, "fiob_a": {"chaves": [], "outras": 0}}
        assert mt.montar_alerta(v, mud, c, AGORA) is None
        lg = pd.read_csv(log, dtype=str)
        assert list(lg["tipo"]) == [mt.TIPO_REMOVIDA] * 2
        # nova vigencia junto com remocoes: o alerta so traz a nova e nao conta as removidas
        nova = b3(CNPJ_Y, "DY", "REH 10", "2026-11-22", "2027-11-21", 320.0, 200.0)
        nao_canon = dict(nova, classe="Residencial")
        r2 = mt.processar_serie("tarifa", df(HIST), df(HIST[2:] + [nova, nao_canon]), c, esc, log, AGORA)
        mud["tarifa"] = r2["mudancas"]
        assert r2["mudancas"]["outras"] == 1  # so a linha nao canonica nova, nao as 2 removidas
        al = mt.montar_alerta(visoes(r2["snapshot"]), mud, c, AGORA)
        assert al and "| DY | REH 10 | 22/11/2026" in al, al
        assert "1 linha(s) de classes" in al, al
        assert "2015" not in al and "2020" not in al


def test_e_guarda_de_queda():
    q = mt.queda_suspeita
    assert q(1000, 500, 500)                 # metade dos dados sumiu: bloqueia
    assert q(1000, 1000, 250)                # 25% sumiu, compensado por linhas novas: bloqueia
    assert q(2750, 2532, 220) is None        # 24/11/2026, B3: 8% removido, passa
    assert q(14649, 13178, 1482) is None     # 24/11/2026, Grupo A: 10% removido, passa
    assert q(100, 60, 40) is None            # menos de REMOCAO_MIN_BLOQUEIO linhas: nunca bloqueia
    assert q(0, 10, 0) is None
    # a base sao as linhas ATIVAS: 400 ja removidas antes nao contam
    linhas = [b3(f"{i:014d}", f"D{i}", "REH 1", "2026-01-01", "2026-12-31", 100.0 + i, 50.0) for i in range(1000)]
    marcas = ["2026-01-01"] * 400 + [""] * 600
    c = cfg()
    esc = mt.escopo_grupo_a(c)
    r = mt.processar_serie("tarifa", df(linhas, marcas), df(linhas[400:]), c, esc, None, AGORA)
    assert len(r["removidos"]) == 0 and r["n_log"] == 0
    # fonte devolve metade das ativas: erro, a menos de --aceitar-queda
    try:
        mt.processar_serie("tarifa", df(linhas, marcas), df(linhas[400:700]), c, esc, None, AGORA)
        raise AssertionError("guarda nao bloqueou")
    except RuntimeError as e:
        assert "nada foi salvo" in str(e)
    r = mt.processar_serie("tarifa", df(linhas, marcas), df(linhas[400:700]), c, esc, None, AGORA,
                           aceitar_queda=True)
    assert len(r["removidos"]) == 300 and len(r["snapshot"]) == 1000


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
