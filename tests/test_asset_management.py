"""Montador de proposta Asset Management: conta (asset_management.py) e travas das paginas proposta_asset.html
(versao comercial simples) e proposta_asset_completa.html (analise detalhada).

Valores de teste inventados e redondos (credito 300, desconto 15%, ...), conferidos analiticamente.
Uso:  py -m pytest -q tests/test_asset_management.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import asset_management as am  # noqa: E402

PAGINA = RAIZ / "proposta_asset_completa.html"  # versao completa (analise detalhada)
PAGINA_SIMPLES = RAIZ / "proposta_asset.html"  # versao comercial simples
WF = RAIZ / ".github" / "workflows" / "monitor-tarifas-aneel.yml"


def _p(**kw) -> am.Premissas:
    base = dict(credito=300.0, faturada=am.faturada_por_desconto(300.0, 0.15), fatura_dist=50.0, inadimplencia=0.10,
                recorrencia_canal=10.0, custo_operacao=8.0, ga=20.0, impostos=0.10, ocupacao=1.0, clientes=1000)
    base.update(kw)
    return am.Premissas(**base)


def test_faturada_e_desconto():
    p = _p()
    assert p.faturada == pytest.approx(255.0)
    assert p.desconto == pytest.approx(0.15)


def test_arrecadacao_e_vazamento():
    p = _p()
    assert am.arrecadacao(p) == pytest.approx((255 + 50) * 0.9)  # 274,50
    assert am.vazamento(p) == pytest.approx(5.0)


def test_proposta_fee_gerador_thopen():
    r = am.proposta(_p(), pct=0.05, fixo=30)
    assert r["fee"] == pytest.approx(0.05 * 255 + 30)            # 42,75
    assert r["gerador"] == pytest.approx(274.5 - 50 - 10 - 42.75)  # 171,75
    assert r["impostos"] == pytest.approx(4.275)
    assert r["thopen"] == pytest.approx(42.75 * 0.9 - 8 - 20)     # 10,475
    assert r["success_fee"] == 0


def test_fee_independe_da_inadimplencia():
    a = am.proposta(_p(inadimplencia=0.0), pct=0.05, fixo=30)
    b = am.proposta(_p(inadimplencia=0.25), pct=0.05, fixo=30)
    assert a["fee"] == b["fee"] and a["thopen"] == pytest.approx(b["thopen"])
    assert b["gerador"] < a["gerador"]


def test_success_fee_so_acima_da_meta():
    meta = am.proposta(_p(), pct=0.05, fixo=20)["gerador_antes_sf"]  # 181,75
    assert meta == pytest.approx(181.75)
    base = am.proposta(_p(), pct=0.05, fixo=20, sigma=0.2, meta=meta)
    assert base["success_fee"] == 0 and base["gerador"] == pytest.approx(meta)
    pior = am.proposta(_p(inadimplencia=0.2), pct=0.05, fixo=20, sigma=0.2, meta=meta)
    assert pior["success_fee"] == 0
    melhor = am.proposta(_p(inadimplencia=0.0), pct=0.05, fixo=20, sigma=0.2, meta=meta)
    assert melhor["gerador_antes_sf"] == pytest.approx(212.25)
    assert melhor["success_fee"] == pytest.approx(0.2 * (212.25 - 181.75))  # 6,10
    assert melhor["gerador"] == pytest.approx(212.25 - 6.1)
    assert melhor["thopen"] == pytest.approx((32.75 + 6.1) * 0.9 - 28)


@pytest.mark.parametrize("i", [0.0, 0.07, 0.25])
@pytest.mark.parametrize("cen", ["a", "b", "hoje"])
def test_cascata_fecha(i, cen):
    p = _p(inadimplencia=i)
    if cen == "hoje":
        r = am.hoje(p, 0.6)
    else:
        r = am.proposta(p, pct=0.05, fixo=20, sigma=0.2 if cen == "b" else 0, meta=150)
    assert r["gerador"] + r["receita"] + p.fatura_dist + p.recorrencia_canal == pytest.approx(am.arrecadacao(p))


def test_equilibrio_e_equivalencias():
    p = _p()
    assert am.receita_equilibrio(p) == pytest.approx(28 / 0.9)
    assert am.multiplicador(p) == pytest.approx(1 / 0.9)
    assert am.fixo_necessario(p, 0.05) == pytest.approx(28 / 0.9 - 12.75)
    assert am.pct_necessario(p, 10) == pytest.approx((28 / 0.9 - 10) / 255)
    assert am.pct_necessario(p) == pytest.approx(28 / 0.9 / 255)
    # com ocupacao menor o G&A por cliente ativo sobe
    assert am.receita_equilibrio(_p(ocupacao=0.5)) == pytest.approx((8 + 40) / 0.9)
    # na receita de equilibrio o resultado e zero
    r = am.proposta(p, pct=0.0, fixo=am.receita_equilibrio(p))
    assert r["thopen"] == pytest.approx(0.0)


def test_ocupacao_de_equilibrio():
    p = _p()
    w = am.ocupacao_equilibrio(p, 42.75)
    assert w == pytest.approx(20 / (42.75 * 0.9 - 8))
    assert am.proposta(_p(ocupacao=w), pct=0.05, fixo=30)["thopen"] == pytest.approx(0.0)
    assert am.ocupacao_equilibrio(p, 8.0) is None  # receita liquida nao cobre o custo de operacao


def test_hoje_imposto_so_quando_positivo():
    pos = am.hoje(_p(), 0.6)
    assert pos["gerador"] == pytest.approx(180)
    assert pos["receita"] == pytest.approx(34.5)
    assert pos["impostos"] == pytest.approx(3.45)
    assert pos["thopen"] == pytest.approx(34.5 - 3.45 - 28)
    neg = am.hoje(_p(), 0.75)
    assert neg["receita"] == pytest.approx(-10.5)
    assert neg["impostos"] == 0
    assert neg["thopen"] == pytest.approx(-38.5)


def test_totais_e_variacao():
    p = _p(ocupacao=0.8)
    t = am.totais_usina(p, 100.0)
    assert t["mes"] == pytest.approx(80000) and t["ano"] == pytest.approx(960000)
    assert am.totais_usina(_p(clientes=None), 100.0) is None
    assert am.variacao(171.75, 180) == pytest.approx(171.75 / 180 - 1)


# ---------------- pagina estatica

def _html() -> str:
    return PAGINA.read_text(encoding="utf-8")


def test_pagina_sem_travessao():
    h = _html()
    assert "—" not in h
    assert " – " not in h


def test_pagina_inputs_vazios():
    h = _html()
    inputs = re.findall(r"<input\b[^>]*>", h)
    assert len(inputs) >= 20
    for tag in inputs:
        if re.search(r'type="(checkbox|file)"', tag):
            continue
        assert 'value=""' in tag, tag
        assert not re.search(r'value="[^"]+"', tag), tag


def test_pagina_trava_do_pdf_ao_gerador():
    h = _html()
    assert "GER_PROIBIDO" in h and "PDF proposta ao gerador" in h and "PDF análise interna" in h
    m = re.search(r"var GER_PROIBIDO = /(.+?)/i;", h)
    assert m, "regex da trava nao encontrada"
    rx = re.compile(m.group(1), re.I)
    for termo in ("custo de operação", "G&A", "break-even", "margem", "resultado da Thopen", "equilíbrio", "objeções"):
        assert rx.search(termo), termo
    assert "gerLimpo(" in h and "Exportar Excel" in h and "xlsx@0.18.5" in h


def test_pagina_sem_numeros_reais():
    h = _html() + PAGINA_SIMPLES.read_text(encoding="utf-8") + (RAIZ / "asset_management.py").read_text(encoding="utf-8") \
        + Path(__file__).read_text(encoding="utf-8")
    proibidos = [("334", "09"), ("279", "40"), ("70", "51"), ("27", "07"), ("233", "86"), ("43", "23"), ("314", "9"),
                 ("176", "5"), ("186", "5")]
    for a, b in proibidos:
        assert not re.search(a + r"[,.]" + b, h), a
    assert "liqu" + "idez" not in h.lower()


def test_workflow_publica_a_pagina():
    w = WF.read_text(encoding="utf-8")
    assert "proposta_asset.html" in w
    assert "cp proposta_asset.html site/proposta_asset.html" in w
    assert "cp proposta_asset_completa.html site/proposta_asset_completa.html" in w
    assert '- "proposta_asset_completa.html"' in w


# ---------------- capacidade da usina

def test_capacidade_piso_da_energia_compensada():
    c = am.capacidade_usina(consumo=400.0, potencia_kwp=2000.0, produtividade=1200.0, pct_compensado=0.9)
    assert c["geracao_mwh_mes"] == pytest.approx(200.0)          # 2.000 x 1.200 / 12 / 1.000
    assert c["compensada_mwh_mes"] == pytest.approx(180.0)
    assert c["clientes"] == 450                                   # 180.000 / 400
    c2 = am.capacidade_usina(consumo=333.0, geracao_mwh_mes=100.0)
    assert c2["clientes"] == 300                                  # piso(100.000 / 333) = 300,3
    assert am.capacidade_usina(consumo=None, geracao_mwh_mes=100.0)["clientes"] is None
    assert am.capacidade_usina(consumo=300.0)["clientes"] is None


# ---------------- projecao por ano

A_ = {"pct": 0.05, "fixo": 20.0}
B_ = {"pct": 0.05, "fixo": 10.0, "sigma": 0.2, "meta": None}


def test_projecao_ano0_igual_ao_modelo_estatico():
    p = _p(ocupacao=0.8)
    pr = am.projecao(p, 3, 0.6, A_, B_, reaj_tarifa=0.05, reaj_custos=0.04, degradacao=0.01)
    r0 = pr[0]["por_cliente"]
    assert r0["a"]["thopen"] == pytest.approx(am.proposta(p, 0.05, 20)["thopen"])
    assert r0["hoje"]["gerador"] == pytest.approx(am.hoje(p, 0.6)["gerador"])
    meta = am.proposta(p, 0.05, 10)["gerador_antes_sf"]
    assert r0["b"]["gerador"] == pytest.approx(am.proposta(p, 0.05, 10, 0.2, meta)["gerador"])
    assert pr[0]["a"]["gerador"] == pytest.approx(am.totais_usina(p, r0["a"]["gerador"])["ano"])


def test_reajuste_da_tarifa_composto():
    p = _p()
    pr = am.projecao(p, 4, 0.6, A_, B_, reaj_tarifa=0.10, reaj_custos=0.03)
    pt = pr[3]["premissas"]
    assert pt.credito == pytest.approx(300 * 1.1 ** 3)
    assert pt.faturada == pytest.approx(300 * 1.1 ** 3 * 0.85)
    assert pt.fatura_dist == pytest.approx(50 * 1.1 ** 3)
    assert pt.recorrencia_canal == pytest.approx(10 * 1.03 ** 3)
    assert pt.ga == pytest.approx(20 * 1.03 ** 3)
    assert pr[3]["por_cliente"]["a"]["fee"] == pytest.approx(0.05 * 300 * 1.1 ** 3 * 0.85 + 20 * 1.03 ** 3)
    assert pr[3]["por_cliente"]["hoje"]["gerador"] == pytest.approx(0.6 * 300 * 1.1 ** 3)


def test_fio_b_reduz_credito_gd2_e_nao_gd1():
    import monitor_tarifas as mt
    tusd, te, icms, pc = 400.0, 300.0, 0.18, 0.05
    anos = list(range(2026, 2032))
    w1 = [mt.calc_gd(tusd, te, icms, pc, 1, 1)["W"] / 1000 for _ in anos]
    w2 = [mt.calc_gd(tusd, te, icms, pc, 1, 1, fb_ef=200.0 * mt.escalonamento_ano(a))["W"] / 1000 for a in anos]
    base1, base2 = w1[0], w2[0]
    f1 = [am.credito_kwh_ano(base1, w1[0], w) / base1 for w in w1]
    f2 = [am.credito_kwh_ano(base2, w2[0], w) / base2 for w in w2]
    assert all(f == pytest.approx(1.0) for f in f1)
    assert f2[0] == pytest.approx(1.0) and f2[1] < 1.0 and f2[3] < f2[2] < f2[1]
    assert f2[4] == pytest.approx(f2[3])  # 2030 = 2029: 100% do Fio B
    p = _p(clientes=100)
    g1 = am.projecao(p, 6, 0.6, A_, B_, fatores_fio_b=f1)
    g2 = am.projecao(p, 6, 0.6, A_, B_, fatores_fio_b=f2)
    assert g2[0]["a"]["gerador"] == pytest.approx(g1[0]["a"]["gerador"])
    assert g2[5]["a"]["gerador"] < g1[5]["a"]["gerador"]
    assert g1[5]["a"]["gerador"] == pytest.approx(g1[0]["a"]["gerador"])  # sem reajuste e sem Fio B: igual


def test_degradacao_reduz_clientes():
    pr = am.projecao(_p(clientes=1000), 3, 0.6, A_, B_, degradacao=0.05)
    assert [x["premissas"].clientes for x in pr] == [1000, 950, 902]
    assert pr[2]["ativos"] < pr[0]["ativos"]
    assert pr[2]["a"]["gerador"] < pr[0]["a"]["gerador"]


def test_ocupacao_ano1_ramp_up():
    pr = am.projecao(_p(clientes=1000, ocupacao=0.9), 2, 0.6, A_, B_, ocupacao_ano1=0.5)
    assert pr[0]["ativos"] == pytest.approx(500) and pr[1]["ativos"] == pytest.approx(900)


def test_vpl_formula():
    assert am.vpl([100.0, 100.0], 0.10) == pytest.approx(100 / 1.1 + 100 / 1.21)
    assert am.vpl([50.0], 0.0) == pytest.approx(50.0)
    pr = am.projecao(_p(), 5, 0.6, A_, B_)
    c = am.cenario_prazo(pr, 3, 0.12)
    fl = [x["a"]["thopen"] for x in pr[:3]]
    assert c["a"]["thopen"]["total"] == pytest.approx(sum(fl))
    assert c["a"]["thopen"]["vpl"] == pytest.approx(sum(f / 1.12 ** (t + 1) for t, f in enumerate(fl)))
    assert am.cenario_prazo(pr, 3)["a"]["thopen"]["vpl"] is None


@pytest.mark.parametrize("cen", ["hoje", "a", "b"])
def test_cascata_fecha_todo_ano(cen):
    pr = am.projecao(_p(), 6, 0.6, A_, B_, reaj_tarifa=0.06, reaj_custos=0.03,
                     fatores_fio_b=[1, 0.97, 0.94, 0.92, 0.92, 0.92], degradacao=0.01)
    for x in pr:
        pt, r = x["premissas"], x["por_cliente"][cen]
        assert r["gerador"] + r["receita"] + pt.fatura_dist + pt.recorrencia_canal == pytest.approx(am.arrecadacao(pt))


def test_meta_do_success_fee_segue_a_tarifa():
    pr = am.projecao(_p(), 3, 0.6, A_, B_, reaj_tarifa=0.10, reaj_custos=0.10)
    # tudo sobe 10% a.a.: o excedente sobre a meta reajustada continua zero
    assert all(x["por_cliente"]["b"]["success_fee"] == pytest.approx(0.0, abs=1e-9) for x in pr)


# ---------------- tarifas_asset.json (dados publicos do painel)

ARQ_TAR = RAIZ / "tarifas_asset.json"


def _painel_dados() -> dict:
    import json
    h = (RAIZ / "Painel_Tarifas_ANEEL.html").read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="dados">(.*?)</script>', h, re.S)
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_tarifas_asset_chaves_e_consistencia_com_o_painel():
    import json
    import monitor_tarifas as mt
    j = json.loads(ARQ_TAR.read_text(encoding="utf-8"))
    for k in ("ano0", "anos", "pct_fb", "ipca12", "produtividade", "distribuidoras"):
        assert k in j, k
    assert len(j["anos"]) == mt.ASSET_ANOS + 1 and j["anos"][0] == j["ano0"]
    assert [j["pct_fb"]["II"][k] for k, a in enumerate(j["anos"])] == [mt.escalonamento_ano(a) for a in j["anos"]]
    assert j["pct_fb"]["II"][-1] == pytest.approx(1.0) and set(j["pct_fb"]["I"]) == {0.0}
    D = j["distribuidoras"]
    enel = [k for k in D if k.upper().startswith("ENEL SP")]
    assert enel and "CPFL PAULISTA" in D
    painel = _painel_dados()
    for nome in (enel[0], "CPFL PAULISTA"):
        e = D[nome]
        for k in ("uf", "ini", "reh", "tusd", "te", "icms", "pis", "cofins", "pc", "flag_icms_te", "flag_icms_tusd",
                  "fio_b_scee", "v_kwh", "gd"):
            assert k in e, (nome, k)
        for g in ("I", "II", "III"):
            assert len(e["gd"][g]["w"]) == len(j["anos"]) and len(e["gd"][g]["fbe"]) == len(j["anos"])
        m = painel["m"][nome]
        t = m["tx"]
        assert e["tusd"] == pytest.approx(m["tusd"]) and e["te"] == pytest.approx(m["te"])
        r = mt.calc_arrendamento(m["tusd"], m["te"], t["i"], t["pis"] + t["cof"], t["te"], t["tu"], t["s"])
        assert e["gd"]["I"]["w"][0] * 1000 == pytest.approx(r["W_I"], abs=0.01)   # GD I = W_I do Arrendamento
        assert e["v_kwh"] * 1000 == pytest.approx(r["V"], abs=0.01)
        # GD II: W = W_I - Fio B efetivo x fator TUSD; o Fio B crescente reduz o valor compensado
        w2 = e["gd"]["II"]["w"]
        assert w2[-1] < w2[0] <= e["gd"]["I"]["w"][0]
        k = len(j["anos"]) - 1
        assert w2[k] * 1000 == pytest.approx(r["W_I"] - e["gd"]["II"]["fbe"][k] * r["k_tusd"], abs=0.01)
    assert painel["arr"]["neg"]["ipca12"] == pytest.approx(j["ipca12"])


def test_tarifas_asset_gerado_pelas_funcoes_do_pipeline():
    from datetime import date

    import pandas as pd

    import monitor_tarifas as mt
    met = pd.DataFrame([{"ativa": True, "tusd": 400.0, "te": 300.0, "fiob": 200.0, "fiob_suspeito": False, "uf": "SP",
                         "sigla": "X", "inicio": "2026-01-01", "fim": "2027-01-01", "reh": "REH 1",
                         "fiob_inicio": "2026-01-01", "fiob_reh": "REH 1"}], index=["DIST TESTE"])
    imp = pd.DataFrame([{"icms": 0.18, "pis": 0.01, "cofins": 0.04, "pc": 0.05, "flag_te": 1, "flag_tusd": 1, "s": 0.5,
                         "fonte_icms": "padrão 18%"}], index=["DIST TESTE"])
    j = mt.dados_tarifas_asset({"metricas": met, "imp": imp, "hoje": date(2026, 6, 1), "prod": {"SP": (1500.0, "x")}})
    e = j["distribuidoras"]["DIST TESTE"]
    i28 = j["anos"].index(2028)
    g = mt.calc_gd(400, 300, 0.18, 0.05, 1, 1, 0.5, 200 * 0.9)
    assert e["gd"]["II"]["w"][i28] == pytest.approx(g["W"] / 1000, abs=1e-6)
    assert e["gd"]["II"]["fbe"][i28] == pytest.approx(180.0)
    assert e["gd"]["I"]["w"][i28] == pytest.approx(mt.calc_arrendamento(400, 300, 0.18, 0.05, 1, 1)["W_I"] / 1000, abs=1e-6)
    assert j["produtividade"] == {"SP": 1500.0} and e["fonte_aliquotas"] == "padrão"


def test_tarifas_asset_publicado_no_workflow():
    w = WF.read_text(encoding="utf-8")
    assert "cp tarifas_asset.json site/tarifas_asset.json" in w
    assert '"tarifas_asset.json"' in w


# ---------------- nomes descritivos e valores por kWh

def test_nome_proposta_automatico():
    assert am.nome_proposta(0.05, 40) == "Fee fixo: 5% + R$ 40/UC"
    assert am.nome_proposta(0.05, 30, 0.25) == "Fee reduzido + success fee: 5% + R$ 30/UC + 25% do excedente"
    assert am.nome_proposta(0.025, 12.5) == "Fee fixo: 2,5% + R$ 12,50/UC"
    assert am.nome_proposta(0.0, 1200) == "Fee fixo: R$ 1.200/UC"
    assert am.nome_proposta(0.08) == "Fee fixo: 8%"
    assert am.nome_proposta() == "Fee fixo: sem fee"
    assert "—" not in am.nome_proposta(0.05, 30, 0.2) and "–" not in am.nome_proposta(0.05, 30, 0.2)


@pytest.mark.parametrize("cen", ["hoje", "a", "b"])
def test_valores_kwh_consistentes_com_cliente(cen):
    p = _p()
    r = am.hoje(p, 0.6) if cen == "hoje" else am.proposta(p, 0.05, 20, 0.2 if cen == "b" else 0, meta=150)
    v = am.valores_kwh(p, r, consumo=400.0, tarifa_cheia=0.95)
    for k, x in v["cliente"].items():
        if x is None:
            assert v["kwh"][k] is None
        else:
            assert v["kwh"][k] * 400 == pytest.approx(x), k
    assert v["kwh"]["credito"] == pytest.approx(0.75)                 # 300 / 400
    assert v["kwh"]["faturada"] == pytest.approx(0.6375)              # 255 / 400
    assert v["kwh"]["tarifa_cheia"] == pytest.approx(0.95)
    assert v["cliente"]["economia"] == pytest.approx(45.0)
    assert v["desconto"] == pytest.approx(0.15)
    assert v["cliente"]["economia_cheia"] == pytest.approx(380 - 305)
    assert v["economia_cheia_pct"] == pytest.approx(75 / 380)
    # split de cada R$ 1 arrecadado fecha em 1 e em R$ fecha na arrecadacao
    s = v["split"]
    assert sum(s.values()) == pytest.approx(1.0)
    arr = am.arrecadacao(p)
    assert sum(x * arr for x in s.values()) == pytest.approx(arr)
    assert s["vazamento"] * arr == pytest.approx(5.0)
    # Thopen = fee total (ou sobra da cascata no Hoje)
    assert v["cliente"]["thopen"] == pytest.approx(r["receita"])
    if cen == "hoje":
        assert v["cliente"]["fee_pct"] is None and v["cliente"]["success_fee"] is None
        assert v["cliente"]["thopen"] == pytest.approx(34.5)
    else:
        c = v["cliente"]
        assert c["fee_pct"] + c["fee_fixo"] + c["success_fee"] == pytest.approx(c["thopen"])


def test_valores_kwh_sem_consumo():
    v = am.valores_kwh(_p(), am.proposta(_p(), 0.05, 20))
    assert v["kwh"] is None and v["cliente"]["tarifa_cheia"] is None and v["economia_cheia_pct"] is None
    assert v["cliente"]["gerador"] == pytest.approx(274.5 - 50 - 10 - 32.75)


def test_pagina_nomes_e_bloco_kwh():
    h = _html()
    for t in ('id="f-nome-a"', 'id="f-nome-b"', "Nome da proposta A", "Nome da proposta B", "Tarifa e valores por kWh",
              'id="t-kwh"', 'id="g-kwh"', "Fee reduzido + success fee", "Valores por kWh", "Fee de gestão Thopen",
              '"f-nome-a"', '"f-nome-b"'):
        assert t in h, t
    # nomes entram no cenario salvo; versao nova, arquivos antigos (sem os nomes) continuam carregando
    assert re.search(r'CAMPOS = \[[^\]]*"f-nome-a", "f-nome-b"', h)
    assert "versao: 3" in h


def test_pdf_gerador_lista_branca_sem_termos_internos():
    h = _html()
    rx = re.compile(re.search(r"var GER_PROIBIDO = /(.+?)/i;", h).group(1), re.I)
    for termo in ("receita da Thopen", "resultado da Thopen", "impostos", "margem", "break-even", "ocupação de equilíbrio"):
        assert rx.search(termo), termo
    for termo in ("Fee de gestão Thopen", "Tarifa que o cliente paga pela energia", "Tarifa cheia com tributos", "Repasse ao Gerador"):
        assert not rx.search(termo), termo
    campos = re.search(r"var GER_CAMPOS = \[(.*?)\];", h, re.S).group(1)
    assert '"kwh"' in campos
    for interno in ("thopen", "impostos", "custo", "ga", "breakEven", "vale", "objecoes", "split"):
        assert '"' + interno + '"' not in campos, interno
    # textos fixos do bloco ao gerador (sem comentarios) nao contem termos bloqueados
    bloco = h[h.index("// >>> PROPOSTA AO GERADOR"):h.index("// <<< PROPOSTA AO GERADOR")]
    bloco = "\n".join(l for l in bloco.splitlines() if not l.strip().startswith("//"))
    for s in re.findall(r'"([^"\\]{3,})"', bloco):
        assert not rx.search(s), s


# ---------------- calculo reverso do fee

@pytest.mark.parametrize("cap", [False, True])
@pytest.mark.parametrize("w", [1.0, 0.8, 0.6])
def test_fee_reverso_fixo_entrega_a_margem(cap, w):
    p = _p(ocupacao=w)
    for margem in (0.0, 10.0, -3.0):
        r = am.fee_reverso(p, margem, "rs", pct=0.05, fixo=12.0, capacidade=cap)
        assert am.proposta(p, 0.05, r["fixo"], capacidade=cap)["thopen"] == pytest.approx(margem)
        assert am.proposta(p, r["pct"], 12.0, capacidade=cap)["thopen"] == pytest.approx(margem)
        assert am.proposta(p, r["so_pct"], 0.0)["thopen"] == pytest.approx(margem)
        assert am.proposta(p, 0.0, r["so_fixo"], capacidade=cap)["thopen"] == pytest.approx(margem)
    # empate = receita de equilibrio do break-even
    assert am.fee_reverso(p, 0.0)["receita"] == pytest.approx(am.receita_equilibrio(p))


def test_fee_reverso_margem_em_pct_do_fee_e_ocupacao_de_calculo():
    p = _p()
    r = am.fee_reverso(p, 0.25, "pct", pct=0.05)
    x = am.proposta(p, 0.05, r["fixo"])
    assert x["thopen"] == pytest.approx(0.25 * x["fee"])
    assert r["receita"] == pytest.approx(28 / (1 - 0.10 - 0.25))
    assert am.fee_reverso(p, 0.95, "pct") is None                    # impostos + margem >= 100%
    r7 = am.fee_reverso(p, 10.0, ocupacao=0.7, pct=0.05, capacidade=True)
    assert am.proposta(_p(ocupacao=0.7), 0.05, r7["fixo"], capacidade=True)["thopen"] == pytest.approx(10.0)
    assert r7["so_fixo"] == pytest.approx(r7["receita"] * 0.7)       # fixo contratual sobre a capacidade


# ---------------- fee fixo sobre a capacidade

def test_fee_sobre_capacidade_receita_constante_e_gerador_paga():
    N = 1000
    receitas, geradores = [], []
    for w in (1.0, 0.9, 0.7, 0.5):
        p = _p(ocupacao=w, clientes=N)
        r = am.proposta(p, 0.05, 20.0, capacidade=True)
        assert r["fee_fixo"] == pytest.approx(20.0 / w)
        receitas.append(r["fee_fixo"] * N * w)                        # fee fixo da usina por mes
        geradores.append(r["gerador"])
        assert r["gerador"] + r["receita"] + p.fatura_dist + p.recorrencia_canal == pytest.approx(am.arrecadacao(p))
        assert r["gerador"] == pytest.approx(am.proposta(p, 0.05, 20.0)["gerador"] - (20.0 / w - 20.0))
    assert all(x == pytest.approx(20000.0) for x in receitas)
    assert geradores == sorted(geradores, reverse=True)              # Gerador paga as vagas
    # base clientes ativos: igual ao modelo anterior
    assert am.proposta(_p(ocupacao=0.7), 0.05, 20.0)["fee_fixo"] == 20.0


def test_ocupacao_de_equilibrio_com_fee_sobre_capacidade():
    p = _p()
    # fixo x (1 - impostos) cobre o G&A e o % cobre o custo de operacao: sempre positivo
    e = am.ocupacao_equilibrio_fee(p, 0.05, 30.0, capacidade=True)
    assert e["tipo"] == "sempre"
    for w in (0.3, 0.6, 1.0):
        assert am.proposta(_p(ocupacao=w), 0.05, 30.0, capacidade=True)["thopen"] > 0
    # base clientes ativos: mesma ocupacao de equilibrio do modelo anterior
    e2 = am.ocupacao_equilibrio_fee(p, 0.05, 30.0)
    assert e2["tipo"] == "acima" and e2["ocupacao"] == pytest.approx(am.ocupacao_equilibrio(p, 42.75))
    # capacidade com fixo pequeno: equilibrio recalculado e resultado zero nele
    e3 = am.ocupacao_equilibrio_fee(p, 0.05, 10.0, capacidade=True)
    assert e3["tipo"] == "acima"
    assert am.proposta(_p(ocupacao=e3["ocupacao"]), 0.05, 10.0, capacidade=True)["thopen"] == pytest.approx(0.0, abs=1e-9)
    assert am.ocupacao_equilibrio_fee(p, 0.0, 5.0)["tipo"] == "nunca"


def test_projecao_com_fee_sobre_capacidade():
    a = {"pct": 0.05, "fixo": 20.0, "cap": True}
    pr = am.projecao(_p(clientes=1000, ocupacao=0.8), 3, 0.6, a, B_, ocupacao_ano1=0.5, degradacao=0.05)
    for x in pr:
        fee_fixo_usina = x["por_cliente"]["a"]["fee_fixo"] * x["ativos"]
        assert fee_fixo_usina == pytest.approx(20.0 * x["premissas"].clientes)  # capacidade do ano x fixo


# ---------------- success fee com saldo acumulado

def test_success_fee_perfil_constante_igual_ao_simples():
    p = _p()
    b_s = dict(B_, meta=150.0)
    b_c = dict(b_s, carry=True)
    s = am.projecao(p, 3, 0.6, A_, b_s)
    c = am.projecao(p, 3, 0.6, A_, b_c, perfil_inad=[0.10] * 12)
    for x, y in zip(s, c):
        assert x["success_fee_ano"] > 0
        assert y["success_fee_ano"] == pytest.approx(x["success_fee_ano"])
        assert y["b"]["thopen"] == pytest.approx(x["b"]["thopen"])


def test_success_fee_alternado_carry_paga_menos():
    p = _p()
    perfil = am.perfil_alternado(0.10, 0.05)
    assert perfil[:4] == pytest.approx([0.15, 0.05, 0.15, 0.05])
    s = am.projecao(p, 2, 0.6, A_, B_, perfil_inad=perfil)
    c = am.projecao(p, 2, 0.6, A_, dict(B_, carry=True), perfil_inad=perfil)
    assert s[0]["success_fee_ano"] > 0
    assert c[0]["success_fee_ano"] < s[0]["success_fee_ano"]
    # meta = G0 base e perfil simetrico: cada mes bom so devolve o deficit do mes ruim anterior
    assert c[0]["success_fee_ano"] == pytest.approx(0.0, abs=1e-9)
    for x in c:  # cascata media do ano fecha com o success fee apurado
        pt, r = x["premissas"], x["por_cliente"]["b"]
        assert r["gerador"] + r["receita"] + pt.fatura_dist + pt.recorrencia_canal == pytest.approx(r["arrecadacao"])


def test_success_fee_deficit_acumula_e_zera_no_ano():
    meses, saldo = am.success_fee_meses([90, 80, 130, 125], [100] * 4, 0.2, carry=True)
    assert [m["saldo_fim"] for m in meses] == pytest.approx([10, 30, 0, 0])
    assert [m["sf"] for m in meses] == pytest.approx([0, 0, 0, 0.2 * 25])
    assert saldo == pytest.approx(0)
    simples, _ = am.success_fee_meses([90, 80, 130, 125], [100] * 4, 0.2)
    assert sum(m["sf"] for m in simples) == pytest.approx(0.2 * 55)
    # deficit no fim do ano: sem zerar, o saldo passa para o ano seguinte; zerando, nao
    perfil = [0.10] * 11 + [0.30]
    b = dict(B_, meta=None, carry=True)
    sem = am.projecao(_p(), 2, 0.6, A_, b, perfil_inad=perfil)
    com = am.projecao(_p(), 2, 0.6, A_, dict(b, zerar=True), perfil_inad=perfil)
    assert sem[0]["sf_meses"][-1]["saldo_fim"] > 0
    assert sem[1]["sf_meses"][0]["saldo_ini"] == pytest.approx(sem[0]["sf_meses"][-1]["saldo_fim"])
    assert com[1]["sf_meses"][0]["saldo_ini"] == 0


# ---------------- comparacao com o arrendamento

@pytest.mark.parametrize("args", [
    (400.0, 300.0, 0.18, 0.05, 1, 1, 0.5, 0.30, 0.0, 0.0, True),
    (420.0, 310.0, 0.12, 0.06, 1, 0, 0.0, 0.25, 0.20, 150.0, True),
    (500.0, 280.0, 0.20, 0.04, 0, 1, 0.7, 0.35, 0.10, 120.0, False),
])
def test_arrendamento_porte_igual_ao_painel(args):
    import monitor_tarifas as mt
    a = am.arrendamento_mwh(*args)
    b = mt.calc_arrendamento(*args)
    for k in ("U", "W_I", "Y", "V", "custo_fb", "dono", "thopen", "desconto", "margem", "preco_kwh", "desagio_impl"):
        assert a[k] == pytest.approx(b[k]), k
    assert a["cliente_paga"] == pytest.approx(b["dono"] + b["margem"])
    assert am.desagio_por_preco_arr(a, 0.40) == pytest.approx(mt.desagio_por_preco(b, 0.40))


@pytest.mark.parametrize("preco", [None, 0.42])
def test_arrendamento_projecao_igual_ao_painel(preco):
    import monitor_tarifas as mt
    tar = {"tusd": 400.0, "te": 300.0, "icms": 0.18, "pc": 0.05, "flag_te": 1, "flag_tusd": 1, "fio_b_scee": 200.0}
    anos = 5
    pfb = [mt.escalonamento_ano(2026 + t) for t in range(anos)]
    p = _p(clientes=1000, ocupacao=1.0, inadimplencia=0.0, recorrencia_canal=0.0, custo_operacao=0.0, ga=0.0,
           fatura_dist=0.0, impostos=0.0)
    q = 1000 * 300 / 1000.0  # MWh/mes = 1000 clientes x 300 kWh
    pr = am.projecao_arrendamento_asset(p, anos, tar, pfb, 300.0, s=0.4, c=0.1, d=0.3, preco_kwh=preco,
                                        reaj_tarifa=0.05, q_usina_mes=q)
    ref = mt.projecao_arrendamento(400.0, 300.0, 0.18, 0.05, 1, 1, 0.4, 0.3, 0.1, 200.0, 2, True, q, anos, 2026, 0.05,
                                   0.0, 0.12, fixar="preco" if preco is not None else "d", preco_kwh=preco)
    for x, y in zip(pr, ref["anos"]):
        assert x["gerador"] == pytest.approx(y["dono"])
        assert x["margem_painel"] == pytest.approx(y["margem"])
        assert x["d"] == pytest.approx(y["d"])
        # sem custos, sem inadimplencia e sem vacancia, o resultado comparavel = margem do painel
        assert x["thopen"] == pytest.approx(y["margem"])


def test_arrendamento_vacancia_e_inadimplencia_com_a_thopen():
    tar = {"tusd": 400.0, "te": 300.0, "icms": 0.18, "pc": 0.05, "flag_te": 1, "flag_tusd": 1, "fio_b_scee": 0.0}
    base = dict(s=0.0, c=0.15, d=0.3, q_usina_mes=300.0)
    cheio = am.projecao_arrendamento_asset(_p(clientes=1000, ocupacao=1.0), 1, tar, [0.0], 300.0, **base)[0]
    vago = am.projecao_arrendamento_asset(_p(clientes=1000, ocupacao=0.8), 1, tar, [0.0], 300.0, **base)[0]
    assert vago["gerador"] == pytest.approx(cheio["gerador"])         # dono recebe pela usina inteira
    assert vago["thopen"] < cheio["thopen"]
    inad = am.projecao_arrendamento_asset(_p(clientes=1000, inadimplencia=0.2), 1, tar, [0.0], 300.0, **base)[0]
    assert inad["gerador"] == pytest.approx(cheio["gerador"]) and inad["thopen"] < cheio["thopen"]


# ---------------- pagina: novos blocos, cenario versao 4 e trava do PDF ao gerador

def test_pagina_novos_blocos_e_cenario_v4():
    h = _html()
    for t in ("Cálculo reverso do fee", "Comparação com o arrendamento", "Apuração do success fee", "Base do fee fixo",
              'id="s-reverso"', 'id="s-arr"', 'id="s-sf"', "zerar saldo a cada ano", "versao: 4",
              "independente da ocupação", '"Cálculo reverso"', '"Comparação arrendamento"', '"Success fee mensal"'):
        assert t in h, t
    campos = re.search(r"var CAMPOS = \[(.*?)\];", h, re.S).group(1)
    for k in ("f-bfa", "f-bfb", "f-sfmodo", "f-sfperfil", "f-sfpp", "f-rv-m", "f-rv-unid", "f-rv-ocup", "f-arr-modo",
              "f-arr-d", "f-arr-p", "f-arr-s", "f-arr-c", "f-arr-fb"):
        assert '"' + k + '"' in campos, k
    # carregador de cenarios antigos (versoes 1 a 3): campo ausente = vazio ou o padrao do select; checkbox opcional
    sel = re.search(r"var SEL = \{(.*?)\};", h, re.S).group(1)
    for k in ("f-bfa", "f-bfb", "f-sfmodo", "f-rv-unid", "f-arr-modo", "f-arr-fb"):
        assert '"' + k + '"' in sel, k
    assert 'o && o[id] != null ? String(o[id]) : ""' in h
    assert '$("f-sfzera").checked = !!(o && o["f-sfzera"]);' in h
    assert "aplica(o && o.campos ? o.campos : o)" in h


def test_pdf_gerador_bloqueia_arrendamento_e_reverso():
    h = _html()
    rx = re.compile(re.search(r"var GER_PROIBIDO = /(.+?)/i;", h).group(1), re.I)
    for termo in ("Comparação com o arrendamento", "Cálculo reverso do fee", "arrendamento"):
        assert rx.search(termo), termo
    for termo in ("fee fixo devido sobre a capacidade contratada da usina, independente da ocupação",
                  "saldo acumulado", "Apuração do success fee"):
        assert not rx.search(termo), termo
    campos = re.search(r"var GER_CAMPOS = \[(.*?)\];", h, re.S).group(1)
    assert '"sfApuracao"' in campos and '"sfSens"' in campos
    assert '"arr' not in campos and '"reverso' not in campos


# ---------------- reajuste da meta do success fee (Proposta B)

FB_ = [1.0, 0.96, 0.92, 0.88, 0.88, 0.88]  # fatores do Fio B inventados (GD II): a tarifa compensavel cai


def _meta0(p, b):
    return am.proposta(p, b["pct"], b["fixo"], capacidade=b.get("cap", False))["gerador_antes_sf"]


def test_meta_valor_compensado_inclui_fio_b_e_gd1_igual_reajuste():
    p = _p()
    pr = am.projecao(p, 6, 0.6, A_, dict(B_, meta_reaj="comp"), reaj_tarifa=0.05, reaj_custos=0.04, fatores_fio_b=FB_)
    m0 = _meta0(p, B_)
    for x in pr:
        t = x["t"]
        assert x["meta"] == pytest.approx(m0 * 1.05 ** t * FB_[t])
        # meta / tarifa compensavel (credito por cliente) constante: acompanha o valor compensado com o Fio B
        assert x["meta"] / x["premissas"].credito == pytest.approx(m0 / p.credito)
    # GD I (sem Fio B): igual ao reajuste da tarifa
    g1 = am.projecao(p, 6, 0.6, A_, dict(B_, meta_reaj="comp"), reaj_tarifa=0.05, reaj_custos=0.04)
    g1t = am.projecao(p, 6, 0.6, A_, dict(B_, meta_reaj="tarifa"), reaj_tarifa=0.05, reaj_custos=0.04)
    assert [x["meta"] for x in g1] == pytest.approx([x["meta"] for x in g1t])
    # padrao da projecao = valor compensado
    pad = am.projecao(p, 6, 0.6, A_, B_, reaj_tarifa=0.05, reaj_custos=0.04, fatores_fio_b=FB_)
    assert [x["meta"] for x in pad] == pytest.approx([x["meta"] for x in pr])


def test_meta_valor_compensado_mantem_success_fee_com_fio_b():
    p = _p()
    perfil = am.perfil_alternado(0.10, 0.05)
    kw = dict(reaj_tarifa=0.05, reaj_custos=0.05, fatores_fio_b=FB_, perfil_inad=perfil)
    comp = am.projecao(p, 6, 0.6, A_, dict(B_, meta_reaj="comp"), **kw)
    old = am.projecao(p, 6, 0.6, A_, dict(B_, meta_reaj="tarifa"), **kw)
    assert comp[0]["success_fee_ano"] == pytest.approx(old[0]["success_fee_ano"])
    # com a meta pelo reajuste da tarifa o Fio B deixa a meta acima do repasse: success fee some
    assert old[5]["success_fee_ano"] == pytest.approx(0.0, abs=1e-9)
    assert comp[5]["success_fee_ano"] > 0


def test_meta_base_success_fee_zero_na_base_todo_ano():
    p = _p(clientes=1000, ocupacao=0.9)
    b = dict(B_, meta=150.0, meta_reaj="base", cap=True)  # meta informada e ignorada
    pr = am.projecao(p, 6, 0.6, A_, b, reaj_tarifa=0.06, reaj_custos=0.03, fatores_fio_b=FB_, ocupacao_ano1=0.5, degradacao=0.01)
    for x in pr:
        g0 = am.proposta(x["premissas"], 0.05, 10.0 * 1.03 ** x["t"], capacidade=True)["gerador_antes_sf"]
        assert x["meta"] == pytest.approx(g0)
        assert x["success_fee_ano"] == pytest.approx(0.0, abs=1e-6)
    # so o desempenho acima da base paga success fee
    alt = am.projecao(p, 3, 0.6, A_, b, fatores_fio_b=FB_[:3], perfil_inad=am.perfil_alternado(0.10, 0.05))
    assert all(x["success_fee_ano"] > 0 for x in alt)


def test_meta_tarifa_comportamento_anterior_e_fixa_constante():
    p = _p()
    m0 = _meta0(p, B_)
    t = am.projecao(p, 5, 0.6, A_, dict(B_, meta_reaj="tarifa"), reaj_tarifa=0.07, fatores_fio_b=FB_[:5])
    assert [x["meta"] for x in t] == pytest.approx([m0 * 1.07 ** k for k in range(5)])
    f = am.projecao(p, 5, 0.6, A_, dict(B_, meta_reaj="fixa"), reaj_tarifa=0.07, fatores_fio_b=FB_[:5])
    assert [x["meta"] for x in f] == pytest.approx([m0] * 5)
    assert am.meta_ano(100.0, "comp", 1.1, 0.9, 1.0) == pytest.approx(99.0)
    assert am.meta_ano(100.0, "base", 1.1, 0.9, 1.0, g0_base=80.0) == pytest.approx(80.0)
    with pytest.raises(ValueError):
        am.meta_ano(100.0, "outra", 1.0, 1.0)


def test_pagina_reajuste_da_meta_e_cenario_v5():
    h = _html()
    for t in ('id="f-sfmeta"', '<option value="comp" selected>pela variação do valor compensado (tarifa compensável, inclui o Fio B)</option>',
              '<option value="base">pelo resultado do Gerador no cenário base de cada ano</option>', '<option value="tarifa">pelo reajuste da tarifa</option>',
              '<option value="fixa">fixa (sem reajuste)</option>', "Reajuste da meta do success fee", "versao: 5", 'id="t-sf-meta"',
              'id="f-arr-cb"', '<option value="asset" selected>igual ao Asset (sobre o crédito do cliente)</option>',
              '<option value="painel">convenção do painel (sobre W_I)</option>', "Base do desconto ao cliente no arrendamento"):
        assert t in h, t
    campos = re.search(r"var CAMPOS = \[(.*?)\];", h, re.S).group(1)
    assert '"f-sfmeta"' in campos and '"f-arr-cb"' in campos
    sel = re.search(r"var SEL = \{(.*?)\};", h, re.S).group(1)
    assert '"f-sfmeta": "comp"' in sel and '"f-arr-cb": "asset"' in sel   # cenarios 1 a 4 sem o campo: novo padrao
    # o texto da meta entra na clausula e no PDF ao gerador sem termos bloqueados
    rx = re.compile(re.search(r"var GER_PROIBIDO = /(.+?)/i;", h).group(1), re.I)
    meta = re.search(r"var META_TXT = \{(.*?)\};", h, re.S).group(1)
    for k in ("comp", "base", "tarifa", "fixa"):
        txt = re.search(k + r': "([^"]+)"', meta).group(1)
        assert not rx.search(txt), txt


# ---------------- base do desconto ao cliente no arrendamento

TAR_ = {"tusd": 400.0, "te": 300.0, "icms": 0.18, "pc": 0.05, "flag_te": 1, "flag_tusd": 1, "fio_b_scee": 200.0}


def _arr_alinhado(base, c=0.15):
    import monitor_tarifas as mt
    anos = 5
    pfb = [mt.escalonamento_ano(2026 + t) for t in range(anos)]
    w2 = [mt.calc_gd(400, 300, 0.18, 0.05, 1, 1, fb_ef=200.0 * x)["W"] for x in pfb]  # R$/MWh, sem reajuste
    cons = 300.0
    p = _p(credito=w2[0] * cons / 1000, faturada=w2[0] * cons / 1000 * (1 - c), clientes=1000)
    ff = [w / w2[0] for w in w2]
    pr = am.projecao_arrendamento_asset(p, anos, TAR_, pfb, cons, s=0.0, c=c, d=0.30, reaj_tarifa=0.05, reaj_custos=0.04,
                                        q_usina_mes=1000 * cons / 1000, base_desconto=base, fatores_fio_b=ff)
    return p, pr, pfb, ff, cons


def test_arrendamento_desconto_igual_ao_asset():
    p, pr, pfb, ff, cons = _arr_alinhado("asset")
    asset = am.projecao(p, 5, 0.6, A_, B_, reaj_tarifa=0.05, reaj_custos=0.04, fatores_fio_b=ff)
    for x, y in zip(pr, asset):
        pt = y["premissas"]
        assert x["desconto_cliente"] == pytest.approx(pt.credito - pt.faturada)       # C - F do Asset
        assert x["premissas"].credito == pytest.approx(pt.credito)
        # valor bruto igual (W_I - Fio B = credito): o cliente paga a mesma energia com desconto nos dois modelos
        assert x["mes"]["energia_cliente"] / x["ativos"] == pytest.approx(pt.faturada)


def test_arrendamento_desconto_convencao_do_painel():
    import monitor_tarifas as mt
    p, pr, pfb, ff, cons = _arr_alinhado("painel")
    for t, x in enumerate(pr):
        f = 1.05 ** t
        ref = mt.calc_arrendamento(400 * f, 300 * f, 0.18, 0.05, 1, 1, 0.0, 0.30, 0.15, 200 * f * pfb[t])
        assert x["desconto_cliente"] == pytest.approx(ref["desconto"] * cons / 1000)
        assert x["mwh"]["margem"] == pytest.approx(ref["margem"])
    # bases diferentes: com o painel o desconto em R$ e maior que o do Asset (W_I > valor compensado GD II)
    _, pa, _, _, _ = _arr_alinhado("asset")
    assert all(a["desconto_cliente"] < b["desconto_cliente"] for a, b in zip(pa, pr))
    # mesmo pagamento ao dono; a diferenca de desconto vai para o resultado da Thopen
    for a, b in zip(pa, pr):
        assert a["gerador"] == pytest.approx(b["gerador"])
        assert a["thopen"] > b["thopen"]


def test_arrendamento_mwh_base_do_desconto():
    a = am.arrendamento_mwh(400.0, 300.0, 0.18, 0.05, 1, 1, 0.5, 0.3, 0.2, 100.0)
    b = am.arrendamento_mwh(400.0, 300.0, 0.18, 0.05, 1, 1, 0.5, 0.3, 0.2, 100.0, base_desconto=800.0)
    assert a["base_desconto"] == pytest.approx(a["W_I"])
    assert b["desconto"] == pytest.approx(160.0) and b["cliente_paga"] == pytest.approx(800.0 - 160.0)
    assert b["cliente_paga"] == pytest.approx(b["dono"] + b["margem"])
    assert a["margem"] == pytest.approx(a["thopen"] - a["desconto"]) and a["bruto"] == pytest.approx(a["W_I"] - a["custo_fb"])


def test_arrendamento_igual_ao_asset_cliente_paga_o_mesmo_com_bruto_diferente():
    # ICMS fora da TUSD compensada: valor compensado GD II (fator TUSD sem ICMS) != W_I - Fio B com impostos
    tar = dict(TAR_, flag_tusd=0)
    p = _p(credito=250.0, faturada=250.0 * 0.8, clientes=1000)
    pr = am.projecao_arrendamento_asset(p, 3, tar, [0.75, 0.9, 1.0], 300.0, s=0.0, c=0.2, d=0.30, q_usina_mes=300.0,
                                        base_desconto="asset", reaj_tarifa=0.05)
    for x in pr:
        pt = x["premissas"]
        assert x["mwh"]["bruto"] != pytest.approx(x["mwh"]["W_I"] - x["mwh"]["custo_fb"])
        assert x["mes"]["energia_cliente"] / x["ativos"] == pytest.approx(pt.faturada)
        assert x["desconto_cliente"] == pytest.approx(pt.credito - pt.faturada)


# ---------------- versao comercial simples (proposta_asset.html): fee % + custo da operacao

def _cs(**kw):
    base = dict(credito=300.0, desconto=0.15, fatura_dist=50.0, inadimplencia=0.10, fee_pct=0.05, custo_operacao=10.0,
                canal=4.0)
    base.update(kw)
    return am.cascata_simples(**base)


def test_simples_cascata_fecha():
    for i in (0.0, 0.10, 0.25):
        c = _cs(inadimplencia=i)
        assert c["arrecadacao"] == pytest.approx(c["fatura_dist"] + c["canal"] + c["fee"] + c["gerador"])
    c = _cs()
    assert c["faturada"] == pytest.approx(255.0)
    assert c["arrecadacao"] == pytest.approx(305.0 * 0.9)
    assert c["gerador"] == pytest.approx(274.5 - 50 - 4 - 22.75)


def test_simples_fee_pct_mais_custo_e_independe_da_inadimplencia():
    c = _cs()
    assert c["fee_pct"] == pytest.approx(0.05 * 255.0)
    assert c["fee"] == pytest.approx(0.05 * 255.0 + 10.0)
    assert _cs(inadimplencia=0.25)["fee"] == pytest.approx(c["fee"])
    assert _cs(inadimplencia=0.25)["gerador"] < c["gerador"]


def test_simples_por_kwh_igual_por_cliente_sobre_consumo():
    c, cons = _cs(), 400.0
    k = am.kwh_simples(c, cons)
    for chave in ("credito", "faturada", "fee", "fee_pct", "custo_operacao", "gerador", "arrecadacao"):
        assert k[chave] == pytest.approx(c[chave] / cons)


def test_simples_projecao_ano0_igual_estatico_e_totais():
    c = _cs()
    pr = am.projecao_simples(300.0, 0.15, 50.0, 0.10, 0.05, 10.0, canal=4.0, anos=4, reaj_tarifa=0.05, reaj_custo=0.04,
                             clientes=1000, ocupacao=0.8, degradacao=0.01, taxa=0.10)
    a0 = pr["anos"][0]
    assert a0["por_cliente"]["gerador"] == pytest.approx(c["gerador"])
    assert a0["por_cliente"]["fee"] == pytest.approx(c["fee"])
    assert a0["ativos"] == pytest.approx(800.0)
    assert a0["gerador"] == pytest.approx(c["gerador"] * 800 * 12)
    assert pr["anos"][1]["clientes"] == 990
    assert pr["anos"][1]["por_cliente"]["custo_operacao"] == pytest.approx(10.0 * 1.04)
    assert pr["anos"][1]["por_cliente"]["credito"] == pytest.approx(300.0 * 1.05)
    assert pr["total_gerador"] == pytest.approx(sum(x["gerador"] for x in pr["anos"]))
    assert pr["vpl_fee"] == pytest.approx(am.vpl([x["fee"] for x in pr["anos"]], 0.10))
    sem = am.projecao_simples(300.0, 0.15, 50.0, 0.10, 0.05, 10.0, anos=2)
    assert sem["anos"][0]["ativos"] == 1.0 and sem["vpl_fee"] is None


def test_simples_fio_b_reduz_credito_gd2_e_nao_gd1():
    base, w = 0.90, [0.80, 0.77, 0.74]           # valor compensado GD II inventado (R$/kWh), cai com o Fio B
    ff2 = [am.credito_kwh_ano(base, w[0], x) / base for x in w]
    ff1 = [am.credito_kwh_ano(base, 0.80, 0.80) / base for _ in w]   # GD I: valor compensado constante
    p2 = am.projecao_simples(300.0, 0.15, 50.0, 0.10, 0.05, 10.0, anos=3, fatores_fio_b=ff2)
    p1 = am.projecao_simples(300.0, 0.15, 50.0, 0.10, 0.05, 10.0, anos=3, fatores_fio_b=ff1)
    assert p2["anos"][2]["por_cliente"]["credito"] < p1["anos"][2]["por_cliente"]["credito"]
    assert p2["anos"][2]["gerador"] < p1["anos"][2]["gerador"]
    assert p1["anos"][2]["por_cliente"]["credito"] == pytest.approx(300.0)


def test_simples_sensibilidade_inadimplencia():
    s = am.sensibilidade_inadimplencia(300.0, 0.15, 50.0, 0.05, 10.0)
    assert [round(x["inadimplencia"], 2) for x in s] == [0.0, 0.05, 0.10, 0.15, 0.20, 0.25]
    assert len({round(x["fee"], 6) for x in s}) == 1
    assert all(a["gerador"] > b["gerador"] for a, b in zip(s, s[1:]))


def _simples() -> str:
    return PAGINA_SIMPLES.read_text(encoding="utf-8")


def test_simples_pagina_sem_travessao_e_inputs_vazios():
    h = _simples()
    assert "—" not in h and " – " not in h
    inputs = re.findall(r"<input\b[^>]*>", h)
    assert len(inputs) >= 20
    for tag in inputs:
        if re.search(r'type="(checkbox|file)"', tag):
            continue
        assert 'value=""' in tag, tag
        assert not re.search(r'value="[^"]+"', tag), tag


def test_simples_pagina_so_uma_proposta():
    h = re.sub(r"var GER_PROIBIDO = /.+?/i;", "", _simples()).lower()   # a trava do PDF lista os termos de proposito
    for termo in ("success fee", "proposta b", "take rate", "arrendamento", "break-even", "g&a"):
        assert termo not in h, termo


def test_simples_pagina_blocos_exportacao_e_link_completa():
    h = _simples()
    for t in ('id="f-fee"', 'id="f-custo"', "Custo da operação (R$/UC/mês)", "Tarifas e valores por kWh", 'id="t-cascata"',
              'id="t-proj"', 'id="t-sens"', "Cláusulas essenciais", "PDF proposta ao gerador", "Exportar Excel", "xlsx@0.18.5",
              '<a href="proposta_asset_completa.html">versão completa (análise detalhada)</a>', 'fetch("tarifas_asset.json"',
              "versao: 6"):
        assert t in h, t
    for aba in ('"Premissas"', '"Valores por kWh"', '"Cascata"', '"Projeção"', '"Sensibilidade"', '"Cláusulas"'):
        assert "nome: " + aba in h, aba
    rx = re.compile(re.search(r"var GER_PROIBIDO = /(.+?)/i;", h).group(1), re.I)
    for termo in ("margem", "G&A", "break-even", "resultado da Thopen"):
        assert rx.search(termo), termo
    assert not rx.search("custo da operação") and not rx.search("Fee Thopen total")


def test_simples_cenarios_antigos_mapeados():
    h = _simples()
    m = re.search(r"var MAPA_ANTIGO = \{(.*?)\};", h)
    assert m and '"f-pa": "f-fee"' in m.group(1) and '"f-op": "f-custo"' in m.group(1)
    assert "if (v >= 6) return {campos: o, antigo: false};" in h       # versao 6 carrega direto; 1 a 5 pelo mapa
    campos = re.search(r"var CAMPOS = \[(.*?)\];", h, re.S).group(1)
    for k in ("f-fee", "f-custo", "f-tarifa", "f-consumo", "f-desc", "f-df", "f-inad", "f-canal", "f-ocup", "f-prazo", "f-sla1", "f-sla2"):
        assert '"' + k + '"' in campos, k
    for velho in ('"f-pb"', '"f-sig"', '"f-fa"', '"f-ga"', '"f-tr"'):
        assert velho not in campos, velho
    # SLA aceita texto ("5 dias"): extrai o numero
    assert "function dias(id)" in h and 'dias("f-sla1")' in h and 'dias("f-sla2")' in h


# ---------------- comissao de parceiro (% da energia faturada com desconto) e remocao da economia vs tarifa cheia

@pytest.mark.parametrize("pagador", ["gerador", "thopen"])
def test_simples_comissao_cascata_fecha(pagador):
    for i in (0.0, 0.10, 0.25):
        c = _cs(inadimplencia=i, comissao_pct=0.02, comissao_pagador=pagador)
        assert c["comissao"] == pytest.approx(0.02 * c["faturada"])
        assert c["arrecadacao"] == pytest.approx(c["fatura_dist"] + c["canal"] + c["fee_liquido"] + c["comissao"] + c["gerador"])
        assert c["fee"] == pytest.approx(_cs(inadimplencia=i)["fee"])            # fee cobrado na cascata nao muda


def test_simples_comissao_quem_paga():
    sem, ger, tho = _cs(), _cs(comissao_pct=0.02), _cs(comissao_pct=0.02, comissao_pagador="thopen")
    assert ger["comissao"] == pytest.approx(0.02 * 255.0)
    assert ger["gerador"] == pytest.approx(sem["gerador"] - 0.02 * 255.0)
    assert ger["fee_liquido"] == pytest.approx(ger["fee"])
    assert tho["gerador"] == pytest.approx(sem["gerador"])
    assert tho["fee_liquido"] == pytest.approx(tho["fee"] - 0.02 * 255.0)
    assert sem["comissao"] == 0 and sem["fee_liquido"] == pytest.approx(sem["fee"])
    with pytest.raises(ValueError):
        _cs(comissao_pct=0.02, comissao_pagador="outro")
    k = am.kwh_simples(ger, 400.0)
    assert k["comissao"] == pytest.approx(ger["comissao"] / 400.0)


@pytest.mark.parametrize("pagador", ["gerador", "thopen"])
def test_simples_comissao_na_projecao_e_sensibilidade(pagador):
    ff = [1.0, 0.97, 0.94]
    pr = am.projecao_simples(300.0, 0.15, 50.0, 0.10, 0.05, 10.0, canal=4.0, anos=3, reaj_tarifa=0.05, fatores_fio_b=ff,
                             clientes=1000, ocupacao=0.8, taxa=0.10, comissao_pct=0.02, comissao_pagador=pagador)
    for t, a in enumerate(pr["anos"]):
        c = a["por_cliente"]
        assert c["comissao"] == pytest.approx(0.02 * 300.0 * ff[t] * 1.05 ** t * 0.85)   # segue F: reajuste e Fio B
        assert a["comissao"] == pytest.approx(c["comissao"] * a["ativos"] * 12)
    assert pr["total_comissao"] == pytest.approx(sum(a["comissao"] for a in pr["anos"]))
    assert pr["vpl_comissao"] == pytest.approx(am.vpl([a["comissao"] for a in pr["anos"]], 0.10))
    assert pr["total_fee_liquido"] == pytest.approx(pr["total_fee"] - (pr["total_comissao"] if pagador == "thopen" else 0))
    s = am.sensibilidade_inadimplencia(300.0, 0.15, 50.0, 0.05, 10.0, comissao_pct=0.02, comissao_pagador=pagador)
    assert len({round(x["comissao"], 6) for x in s}) == 1
    assert all(a["gerador"] > b["gerador"] for a, b in zip(s, s[1:]))


def test_simples_pagina_comissao_e_sem_economia_vs_tarifa_cheia():
    h = _simples()
    assert "Economia do cliente vs tarifa cheia" not in h
    assert "Economia do cliente (crédito menos energia com desconto)" in h
    assert "—" not in h and "–" not in h
    for t in ('id="f-com"', 'id="f-parceiro"', 'id="f-compag"', "Comissão do parceiro (% da energia faturada com desconto)",
              "Nome do parceiro", "Quem paga a comissão", '<option value="g" selected>Gerador (sai do repasse)</option>',
              '<option value="t">Thopen (sai do fee)</option>', "Fee líquido da Thopen", "Comissão de parceiro"):
        assert t in h, t
    campos = re.search(r"var CAMPOS = \[(.*?)\];", h, re.S).group(1)
    for k in ('"f-com"', '"f-parceiro"', '"f-compag"'):
        assert k in campos, k
    assert '"f-compag": "g"' in re.search(r"var SEL = \{(.*?)\};", h).group(1)
    rx = re.compile(re.search(r"var GER_PROIBIDO = /(.+?)/i;", h).group(1), re.I)
    assert not rx.search("Fee líquido da Thopen") and not rx.search("Comissão de parceiro")
