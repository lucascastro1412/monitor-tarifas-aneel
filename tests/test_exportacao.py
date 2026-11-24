"""Exportacao da simulacao do arrendamento (Excel e PDF): botoes, carregador do SheetJS e a lista branca
da proposta ao proprietario, que nao pode levar dados internos (margem, parte da Thopen, limite, faixa...).

Uso:  py tests/test_exportacao.py   (ou pytest)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import monitor_tarifas as mt  # noqa: E402

H = mt.HTML_TEMPLATE


def _bloco_proposta() -> str:
    ini = H.index("// >>> PROPOSTA AO PROPRIETARIO")
    fim = H.index("// <<< PROPOSTA", ini)
    return H[ini:fim]


def test_botoes_de_exportacao():
    for bid, rot in (("n-xlsx", "Exportar Excel"), ("n-pdf-prop", "PDF proposta"), ("n-pdf-int", "PDF análise interna")):
        assert re.search(r'id="%s"[^>]*>%s</button>' % (bid, re.escape(rot)), H), bid
        assert '$("%s").addEventListener("click"' % bid in H, bid
    assert 'id="n-exp-msg"' in H


def test_carregador_sheetjs():
    assert "https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js" in H
    assert "XLSX.writeFile(" in H and "XLSX.utils.aoa_to_sheet(" in H
    for aba in ("Premissas", "Tarifas", "Resultado", "Negociação", "Projeção", "Memória de cálculo"):
        assert 'nome: "%s"' % aba in H, aba
    # formatos numericos: R$ 2 casas, R$/kWh 4 casas, % 2 casas
    assert '"R$" #,##0.00' in H and '"R$" #,##0.0000' in H and '"0.00%"' in H


def test_estado_de_erro_nao_exporta():
    assert "function expOk()" in H
    assert H.count("if (!expOk()) { expMsg(EXP_SEM); return; }") >= 3


def test_proposta_lista_branca_sem_campos_internos():
    b = _bloco_proposta()
    assert "PROP_CAMPOS.forEach(function (k) { o[k] = src[k]; })" in b
    proibidos = [r"\bmg\b", r"\.mg\b", r"\.th\b", r"\bth\b", r"\bdesc\b", r"\bdWI\b", r"\bWI\b", r"\bP1\b", r"\bP2\b",
                 r"\bFX\b", r"\bNFX\b", r"\bexc\b", r"\bmgMwh\b", r"\bacima\b", r"\bvpl\b", r"\bx\.c\b",
                 r"margem", r"Thopen", r"limite", r"faixa", r"desconto", r"interno", r"valor compensado", r"parte da"]
    for pat in proibidos:
        assert not re.search(pat, b, re.I), pat
    campos = re.search(r"PROP_CAMPOS = \[(.*?)\];", b, re.S).group(1)
    nomes = re.findall(r'"(\w+)"', campos)
    assert nomes and not set(nomes) & {"mg", "th", "desc", "margem", "limite", "faixa", "WI", "dWI", "c"}
    # a proposta nao chama o montador da analise interna nem le a faixa/negociacao interna
    assert "expDados" not in b and "intHtml" not in b


def test_cabecalho_unico_thopen_e_uso_interno():
    ini = H.index("function relDoc(")
    rel = H[ini:H.index("function relImprime(", ini)]
    assert "Thopen Energia" in rel and "Uso interno" in rel
    assert "propLimpa(h, o)" in H  # trava em tempo de execucao antes de imprimir a proposta


def test_sem_travessao_no_painel():
    assert "—" not in H


if __name__ == "__main__":
    for nome, f in list(globals().items()):
        if nome.startswith("test_"):
            f()
            print("ok", nome)
