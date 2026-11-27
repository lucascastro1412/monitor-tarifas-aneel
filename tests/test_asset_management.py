"""Montador de proposta Asset Management: conta (asset_management.py) e travas da pagina proposta_asset.html.

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

PAGINA = RAIZ / "proposta_asset.html"
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
    h = _html() + (RAIZ / "asset_management.py").read_text(encoding="utf-8") + Path(__file__).read_text(encoding="utf-8")
    proibidos = [("334", "09"), ("279", "40"), ("70", "51"), ("27", "07"), ("233", "86"), ("43", "23"), ("314", "9"),
                 ("176", "5"), ("186", "5")]
    for a, b in proibidos:
        assert not re.search(a + r"[,.]" + b, h), a
    assert "liqu" + "idez" not in h.lower()


def test_workflow_publica_a_pagina():
    w = WF.read_text(encoding="utf-8")
    assert "proposta_asset.html" in w
    assert "cp proposta_asset.html site/proposta_asset.html" in w


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
