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
