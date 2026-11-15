#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Base de renda per capita por CEP a partir de dados públicos do IBGE (Censo 2022).

Gera, para cada CEP, duas medidas em colunas separadas:

  A) renda_responsavel_per_capita
     Universo do Censo 2022, por setor censitário:
       (rendimento médio do responsável  x  nº de responsáveis) / moradores
     É um PROXY: só existe a renda do responsável pelo domicílio, não a dos
     demais moradores. Serve para ordenar CEPs, não como renda per capita real.

  C) renda_domiciliar_per_capita_ap
     Amostra do Censo 2022, por área de ponderação (tabela oficial 8.3 dos
     "Resultados Gerais da Amostra por Áreas de Ponderação"):
       rendimento domiciliar mensal per capita médio dos moradores.
     É a medida "oficial", mas a área de ponderação é bem maior que um CEP:
     CEPs vizinhos recebem o mesmo valor.

A ponte CEP -> setor censitário vem do CNEFE 2022 (cadastro de endereços):
cada CEP recebe a média dos setores onde tem endereços, ponderada pelo número
de domicílios particulares do CNEFE em cada par CEP x setor.

Uso:
    pip install duckdb requests openpyxl
    python renda_per_capita_cep.py --municipio 3550308        # só São Paulo/SP
    python renda_per_capita_cep.py --uf SP                    # estado inteiro
    python renda_per_capita_cep.py --uf SP --sem-amostra      # só a opção A
    (no Spyder/IPython: %run renda_per_capita_cep.py --municipio 3550308)

Os downloads ficam em ./cache_ibge e não são repetidos. Em rede corporativa
que bloqueie o FTP do IBGE, baixe os arquivos à mão para essa pasta (os nomes
e endereços são impressos no log) e rode de novo: o script usa o que encontrar.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

import duckdb
import requests

# --------------------------------------------------------------------------
# Fontes (conferidas em nov/2026). Os nomes de arquivo do IBGE levam data e
# mudam a cada republicação, por isso o script lista a pasta e escolhe o mais
# recente em vez de fixar o nome.
# --------------------------------------------------------------------------
FTP = "https://ftp.ibge.gov.br"
URL_RENDA = f"{FTP}/Censos/Censo_Demografico_2022/Agregados_por_Setores_Censitarios_Rendimento_do_Responsavel/"
URL_CNEFE = f"{FTP}/Cadastro_Nacional_de_Enderecos_para_Fins_Estatisticos/Censo_Demografico_2022/Arquivos_CNEFE/CSV/"
URL_AP_TAB = f"{FTP}/Censos/Censo_Demografico_2022/Microdados_e_Areas_de_Ponderacao/Areas_de_Ponderacao/"
URL_AP_DOC = f"{FTP}/Censos/Censo_Demografico_2022/Microdados_e_Areas_de_Ponderacao/Documentacao/Áreas de ponderação/"
ARQ_COMPOSICAO = "Composição das Áreas de Ponderação.xlsx"

# Variáveis usadas. Confirme as descrições no dicionário que o script imprime.
V_RESPONSAVEIS = 6001   # responsáveis por domicílios particulares permanentes ocupados
V_MORADORES = 6002      # moradores nesses domicílios
V_RENDA_MEDIA = 6004    # rendimento nominal médio mensal dos responsáveis (R$)

# Opção C: tabela oficial por área de ponderação. O microdado PÚBLICO da amostra
# só identifica a UF, por isso não serve; a tabela 8.3 já traz o valor por área.
TITULO_TAB_RDPC = "rendimento domiciliar mensal per capita"
TAB_RDPC_PADRAO = "Tab8_3.xlsx"

# Filtro sugerido para uso do resultado por CEP
MIN_COBERTURA, MIN_DOMICILIOS, MAX_SETORES = 0.5, 10, 20   # 20+ setores = CEP genérico

UFS = {
    "RO": 11, "AC": 12, "AM": 13, "RR": 14, "PA": 15, "AP": 16, "TO": 17,
    "MA": 21, "PI": 22, "CE": 23, "RN": 24, "PB": 25, "PE": 26, "AL": 27,
    "SE": 28, "BA": 29, "MG": 31, "ES": 32, "RJ": 33, "SP": 35, "PR": 41,
    "SC": 42, "RS": 43, "MS": 50, "MT": 51, "GO": 52, "DF": 53,
}
SIGLA = {v: k for k, v in UFS.items()}

SESSAO = requests.Session()
SESSAO.headers["User-Agent"] = "renda-cep/1.0 (uso estatístico; python-requests)"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Download e descompactação
# --------------------------------------------------------------------------
def listar(url_pasta: str) -> list[str]:
    """Nomes dos arquivos/pastas de uma listagem de diretório do FTP do IBGE."""
    r = SESSAO.get(quote(url_pasta, safe=":/"), timeout=60)
    r.raise_for_status()
    nomes = re.findall(r'href="([^"?#]+)"', r.text)
    return [unquote(n) for n in nomes if not n.startswith(("/", "http", ".."))]


def resolver(url_pasta: str, padrao: str, cache: Path) -> tuple[str, str]:
    """Acha o arquivo mais recente da pasta que casa com `padrao` (regex).
    Sem rede, cai para o que já existir no cache. Devolve (nome, url)."""
    rx = re.compile(padrao, re.IGNORECASE)
    try:
        achados = sorted(n for n in listar(url_pasta) if rx.match(n))
    except requests.RequestException as e:
        log(f"  aviso: não consegui listar {url_pasta} ({type(e).__name__}); procurando no cache")
        achados = []
    if not achados:
        achados = sorted(p.name for p in cache.iterdir() if p.is_file() and rx.match(p.name))
    if not achados:
        sys.exit(f"ERRO: nenhum arquivo casando com /{padrao}/ em {url_pasta} nem em {cache}.\n"
                 f"Baixe manualmente dessa pasta para {cache} e rode de novo.")
    return achados[-1], url_pasta + achados[-1]


def baixar(url: str, cache: Path, nome: str | None = None) -> Path:
    """Baixa para o cache (se ainda não estiver lá) e devolve o caminho local."""
    nome = nome or unquote(url.rsplit("/", 1)[-1])
    destino = cache / nome
    if destino.exists() and destino.stat().st_size > 0:
        log(f"  cache: {nome}")
        return destino
    log(f"  baixando: {url}")
    parcial = destino.with_name(destino.name + ".part")
    for tentativa in range(1, 4):
        try:
            with SESSAO.get(quote(url, safe=":/"), stream=True, timeout=120) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                feito, marco = 0, 0
                with open(parcial, "wb") as f:
                    for bloco in r.iter_content(chunk_size=1 << 20):
                        f.write(bloco)
                        feito += len(bloco)
                        if total and feito - marco >= 100 << 20:
                            marco = feito
                            log(f"    {feito / 2**20:,.0f} / {total / 2**20:,.0f} MB")
            parcial.rename(destino)
            return destino
        except requests.RequestException as e:
            log(f"  tentativa {tentativa}/3 falhou: {e}")
            time.sleep(5 * tentativa)
    sys.exit(f"ERRO: não consegui baixar {url}\nBaixe manualmente e salve como {destino}")


def extrair(arq_zip: Path, cache: Path) -> list[Path]:
    """Descompacta em cache/_ext/<nome>/ (uma vez) e devolve os arquivos."""
    pasta = cache / "_ext" / arq_zip.stem
    marca = pasta / ".ok"
    if not marca.exists():
        log(f"  descompactando {arq_zip.name}")
        pasta.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(arq_zip) as z:
            z.extractall(pasta)
        marca.touch()
    return sorted(p for p in pasta.rglob("*") if p.is_file() and p.name != ".ok")


# --------------------------------------------------------------------------
# Leitura tolerante de CSV (separador e codificação variam entre produtos)
# --------------------------------------------------------------------------
def sql_csv(con: duckdb.DuckDBPyConnection, caminho: Path) -> str:
    """Expressão read_csv(...) que funciona para o arquivo: tudo como texto,
    separador autodetectado, UTF-8 ou Latin-1."""
    base = f"read_csv('{caminho.as_posix()}', all_varchar=true, header=true, sample_size=50000"
    for extra in ("", ", encoding='latin-1'"):
        expr = f"{base}{extra})"
        try:
            con.execute(f"SELECT count(*) FROM {expr}").fetchone()
            return expr
        except duckdb.Error as e:
            erro = e
    sys.exit(f"ERRO ao ler {caminho}: {erro}")


def colunas(con: duckdb.DuckDBPyConnection, expr: str) -> list[str]:
    return [d[0] for d in con.execute(f"SELECT * FROM {expr} LIMIT 0").description]


def achar_col(cols: list[str], *candidatos: str) -> str:
    mapa = {c.strip().upper(): c for c in cols}
    for cand in candidatos:
        if cand.upper() in mapa:
            return mapa[cand.upper()]
    sys.exit(f"ERRO: coluna {candidatos} não encontrada. Colunas do arquivo: {cols[:40]}")


def achar_v(cols: list[str], numero: int) -> str:
    """Acha V06004 mesmo que venha como V6004 ou V006004."""
    for c in cols:
        m = re.fullmatch(r"V0*(\d+)", c.strip(), re.IGNORECASE)
        if m and int(m.group(1)) == numero:
            return c
    sys.exit(f"ERRO: variável V{numero:05d} não encontrada. Colunas: {cols[:40]}")


def num(col: str) -> str:
    """Texto -> número. 'X' (dado suprimido), '.', vazio viram NULL."""
    return f"TRY_CAST(replace(trim(\"{col}\"), ',', '.') AS DOUBLE)"


def setor15(col: str) -> str:
    """Código de setor: os 15 primeiros dígitos (descarta sufixo como 'P')."""
    return f"regexp_extract(trim(\"{col}\"), '^(\\d{{15}})', 1)"


# --------------------------------------------------------------------------
# Etapas
# --------------------------------------------------------------------------
def etapa_cnefe(con, cache: Path, cod_uf: int, cod_mun: str | None) -> None:
    """Tabela cep_setor(cep, setor, n_dom): domicílios particulares do CNEFE."""
    log("1/4 CNEFE 2022 (ponte CEP -> setor censitário)")
    pasta_uf = f"{cod_uf}_{SIGLA[cod_uf]}"
    if cod_mun:
        nome, url = resolver(f"{URL_CNEFE}Municipio/{pasta_uf}/", rf"^{cod_mun}_.*\.zip$", cache)
    else:
        nome, url = f"{pasta_uf}.zip", f"{URL_CNEFE}UF/{pasta_uf}.zip"
        nome = f"CNEFE_{nome}"  # evita colisão com o zip de microdados, que tem o mesmo nome
    arqs = [p for p in extrair(baixar(url, cache, nome), cache) if p.suffix.lower() == ".csv"]
    if not arqs:
        sys.exit("ERRO: zip do CNEFE sem CSV dentro.")
    expr = sql_csv(con, arqs[0])
    cols = colunas(con, expr)
    c_setor, c_cep, c_esp = achar_col(cols, "COD_SETOR"), achar_col(cols, "CEP"), achar_col(cols, "COD_ESPECIE")
    con.execute(f"""
        CREATE OR REPLACE TABLE cep_setor AS
        SELECT cep, setor, count(*) AS n_dom
        FROM (
            SELECT lpad(regexp_replace("{c_cep}", '\\D', '', 'g'), 8, '0') AS cep,
                   {setor15(c_setor)} AS setor
            FROM {expr}
            WHERE TRY_CAST("{c_esp}" AS INTEGER) = 1          -- 1 = domicílio particular
        )
        WHERE length(cep) = 8 AND cep <> '00000000' AND setor <> ''
        GROUP BY 1, 2
    """)
    n_end, n_cep, n_set = con.execute(
        "SELECT sum(n_dom), count(DISTINCT cep), count(DISTINCT setor) FROM cep_setor").fetchone()
    log(f"  {n_end:,.0f} domicílios particulares | {n_cep:,} CEPs | {n_set:,} setores")


def imprimir_dicionario(cache: Path) -> None:
    """Mostra as descrições oficiais das variáveis usadas, para conferência."""
    try:
        import openpyxl
        nome, url = resolver(URL_RENDA, r"^dicionario_de_dados_renda_responsavel.*\.xlsx$", cache)
        wb = openpyxl.load_workbook(baixar(url, cache), read_only=True, data_only=True)
        alvos = {V_RESPONSAVEIS, V_MORADORES, V_RENDA_MEDIA}
        log("  dicionário do IBGE para as variáveis usadas:")
        for ws in wb.worksheets:
            for linha in ws.iter_rows(values_only=True):
                cel = [str(c).strip() for c in linha if c is not None]
                for c in cel:
                    m = re.fullmatch(r"V0*(\d+)", c, re.IGNORECASE)
                    if m and int(m.group(1)) in alvos:
                        log("    " + " | ".join(cel))
                        break
    except SystemExit:
        log("  aviso: dicionário não disponível; confira as variáveis manualmente.")
    except Exception as e:  # dicionário é só conferência; não derruba a execução
        log(f"  aviso: não consegui ler o dicionário ({e}).")


def etapa_renda_setor(con, cache: Path, prefixo: str) -> None:
    """Tabela setor_renda(setor, resp, mor, renda_media) - opção A."""
    log("2/4 Renda do responsável por setor (universo do Censo 2022)")
    nome, url = resolver(URL_RENDA, r"^Agregados_por_setores_renda_responsavel_BR.*csv\.zip$", cache)
    arqs = [p for p in extrair(baixar(url, cache), cache) if p.suffix.lower() == ".csv"]
    expr = sql_csv(con, arqs[0])
    cols = colunas(con, expr)
    c_setor = achar_col(cols, "CD_SETOR", "COD_SETOR", "CD_setor")
    con.execute(f"""
        CREATE OR REPLACE TABLE setor_renda AS
        SELECT {setor15(c_setor)} AS setor,
               {num(achar_v(cols, V_RESPONSAVEIS))} AS resp,
               {num(achar_v(cols, V_MORADORES))}    AS mor,
               {num(achar_v(cols, V_RENDA_MEDIA))}  AS renda_media
        FROM {expr}
        WHERE starts_with({setor15(c_setor)}, '{prefixo}')
    """)
    tot, ok = con.execute("""
        SELECT count(*), count(*) FILTER (renda_media IS NOT NULL AND resp > 0 AND mor > 0)
        FROM setor_renda""").fetchone()
    log(f"  {tot:,} setores no recorte; {ok:,} com renda divulgada "
        f"({tot - ok:,} suprimidos por sigilo ou sem domicílios)")
    imprimir_dicionario(cache)


def ler_composicao(caminho: Path) -> list[tuple[str, str]]:
    """Lê a planilha 'Composição das Áreas de Ponderação' e devolve pares
    (setor, área de ponderação). O layout não é fixado: a coluna de setor é a
    que tem códigos de 15 dígitos; a de AP é a que traz 'pondera' no título."""
    import openpyxl
    wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    pares: list[tuple[str, str]] = []
    for ws in wb.worksheets:
        cab, i_setor, i_ap = None, None, None
        for linha in ws.iter_rows(values_only=True):
            cel = ["" if c is None else str(c).strip() for c in linha]
            if cab is None:
                if any("pondera" in c.lower() for c in cel) and any("setor" in c.lower() for c in cel):
                    cab = cel
                continue
            if i_setor is None:  # primeira linha de dados: descobre as colunas
                for i, v in enumerate(cel):
                    if re.fullmatch(r"\d{15}[A-Za-z]?", v.replace(".0", "")):
                        i_setor = i
                        break
                cand = [i for i, h in enumerate(cab) if "pondera" in h.lower() and i != i_setor
                        and i < len(cel) and re.fullmatch(r"[\d.]+", cel[i] or "x")]
                # prefere a coluna de código (título com 'cd', 'cod', 'cód' ou 'geocod')
                cand.sort(key=lambda i: 0 if re.search(r"c[oó]d|^cd|geoc", cab[i].lower()) else 1)
                i_ap = cand[0] if cand else None
                if i_setor is None or i_ap is None:
                    break
                log(f"  composição: aba '{ws.title}', setor='{cab[i_setor]}', AP='{cab[i_ap]}'")
            if i_setor < len(cel) and i_ap < len(cel) and cel[i_setor] and cel[i_ap]:
                pares.append((re.sub(r"\D", "", cel[i_setor].replace(".0", ""))[:15],
                              re.sub(r"\D", "", cel[i_ap].replace(".0", ""))))
        if pares:
            break
    if not pares:
        sys.exit(f"ERRO: não reconheci as colunas de setor e área de ponderação em {caminho}.\n"
                 "Abra a planilha, veja os títulos e ajuste ler_composicao().")
    return pares


def achar_tabela_rdpc(arqs: list[Path]) -> Path:
    """Acha, entre as tabelas por área de ponderação, a de renda domiciliar per
    capita (procura pelo título, pois a numeração pode mudar em republicações)."""
    import openpyxl
    xlsx = [p for p in arqs if p.suffix.lower() == ".xlsx" and not p.name.startswith("~")]
    xlsx.sort(key=lambda p: p.name != TAB_RDPC_PADRAO)       # tenta primeiro a 8.3
    for p in xlsx:
        ws = openpyxl.load_workbook(p, read_only=True).worksheets[0]
        for linha in ws.iter_rows(min_row=1, max_row=4, values_only=True):
            if any(TITULO_TAB_RDPC in str(c).lower() for c in linha if c is not None):
                return p
    sys.exit(f"ERRO: nenhuma tabela com '{TITULO_TAB_RDPC}' no título entre {len(xlsx)} arquivos.")


def ler_tabela_rdpc(caminho: Path) -> list[tuple]:
    """Lê a tabela 8.3 -> [(ap, nome, pessoas, renda_media, renda_mediana)].
    Cabeçalho em duas linhas: grupos ('Pessoas', 'Renda média', 'Renda mediana')
    e, abaixo, 'Total | Homens | Mulheres'. Usa sempre a coluna 'Total'."""
    import openpyxl
    ws = openpyxl.load_workbook(caminho, read_only=True, data_only=True).worksheets[0]
    idx, saida = None, []
    for linha in ws.iter_rows(values_only=True):
        cel = ["" if c is None else str(c).strip() for c in linha]
        if idx is None:
            baixo = [c.lower() for c in cel]
            if any("pondera" in c for c in baixo) and any("renda m" in c for c in baixo):
                achar = lambda *t: next(i for i, c in enumerate(baixo) if any(c.startswith(x) for x in t))
                idx = {"ap": achar("área de pondera", "area de pondera"), "nome": achar("nome", ) ,
                       "pessoas": achar("pessoas"), "media": achar("renda média", "renda media"),
                       "mediana": achar("renda mediana")}
                # 'Nome do Município' vem antes de 'Nome': pega o nome logo após a coluna da AP
                idx["nome"] = idx["ap"] + 1
            continue
        ap = re.sub(r"\D", "", cel[idx["ap"]].replace(".0", "")) if len(cel) > idx["ap"] else ""
        if len(ap) < 8:                                   # linha 'Brasil', subcabeçalho, notas
            continue
        f = lambda k: (float(linha[idx[k]]) if isinstance(linha[idx[k]], (int, float)) else None)
        saida.append((ap, cel[idx["nome"]], f("pessoas"), f("media"), f("mediana")))
    if not saida:
        sys.exit(f"ERRO: não consegui ler áreas de ponderação em {caminho}.")
    return saida


def carregar(con, tabela: str, ddl: str, linhas: list[tuple], cache: Path) -> None:
    """Carga em lote via CSV temporário (bem mais rápido que INSERT linha a linha)."""
    import csv, tempfile
    tmp = Path(tempfile.mkdtemp()) / f"{tabela}.csv"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(linhas)
    con.execute(f"CREATE OR REPLACE TABLE {tabela} ({ddl})")
    con.execute(f"INSERT INTO {tabela} SELECT * FROM read_csv('{tmp.as_posix()}', header=false, "
                f"all_varchar=true, delim=',', quote='\"')")
    tmp.unlink(missing_ok=True)


def etapa_amostra(con, cache: Path, prefixo: str) -> bool:
    """Tabelas ap_renda(ap, nome, rdpc, rdpc_mediana) e setor_ap(setor, ap) - opção C."""
    log("3/4 Renda domiciliar per capita por área de ponderação (amostra do Censo 2022)")
    nome, url = resolver(URL_AP_TAB, r"^tabelas_xlsx.*\.zip$", cache)
    tab = achar_tabela_rdpc(extrair(baixar(url, cache), cache))
    linhas = [l for l in ler_tabela_rdpc(tab) if l[0].startswith(prefixo)]
    log(f"  tabela: {tab.name} | {len(linhas):,} áreas de ponderação no recorte")
    carregar(con, "ap_bruta", "ap VARCHAR, nome VARCHAR, pessoas VARCHAR, media VARCHAR, mediana VARCHAR",
             linhas, cache)
    con.execute("""
        CREATE OR REPLACE TABLE ap_renda AS
        SELECT ap, nome, TRY_CAST(pessoas AS DOUBLE) AS pessoas,
               TRY_CAST(media AS DOUBLE) AS rdpc, TRY_CAST(mediana AS DOUBLE) AS rdpc_mediana
        FROM ap_bruta""")
    con.execute("DROP TABLE ap_bruta")

    pares = [p for p in ler_composicao(baixar(URL_AP_DOC + ARQ_COMPOSICAO, cache)) if p[0].startswith(prefixo)]
    carregar(con, "setor_ap", "setor VARCHAR, ap VARCHAR", pares, cache)

    taxa, pop, media = con.execute("""
        SELECT (SELECT count(*) FILTER (ap IN (SELECT ap FROM ap_renda)) / greatest(count(*), 1) FROM setor_ap),
               sum(pessoas), sum(pessoas * rdpc) / sum(pessoas) FROM ap_renda""").fetchone()
    log(f"  {len(pares):,} setores na composição; {taxa:.1%} com área de ponderação na tabela de renda")
    log(f"  recorte: {pop or 0:,.0f} moradores | renda domiciliar per capita média R$ {media or 0:,.2f}")
    if taxa < 0.90:
        log("  ATENÇÃO: casamento baixo entre a composição e a tabela de renda; confira os códigos.")
    return bool(linhas)


def etapa_final(con, saida: Path, rotulo: str, com_amostra: bool) -> None:
    log("4/4 Agregando por CEP")
    if not com_amostra:
        con.execute("CREATE OR REPLACE TABLE setor_ap (setor VARCHAR, ap VARCHAR)")
        con.execute("CREATE OR REPLACE TABLE ap_renda (ap VARCHAR, nome VARCHAR, rdpc DOUBLE, rdpc_mediana DOUBLE)")

    # Base por setor (útil para auditoria e para mapas)
    con.execute("""
        CREATE OR REPLACE TABLE base_setor AS
        SELECT s.setor, s.resp AS responsaveis, s.mor AS moradores, s.renda_media AS renda_media_responsavel,
               CASE WHEN s.renda_media IS NOT NULL AND s.resp > 0 AND s.mor > 0
                    THEN s.renda_media * s.resp / s.mor END AS renda_responsavel_per_capita,
               sa.ap AS area_ponderacao, a.nome AS nome_area_ponderacao,
               a.rdpc AS renda_domiciliar_per_capita_ap, a.rdpc_mediana AS renda_domiciliar_per_capita_mediana_ap
        FROM setor_renda s
        LEFT JOIN setor_ap sa USING (setor)
        LEFT JOIN ap_renda a ON a.ap = sa.ap
    """)
    # moradores por domicílio médio, para setores sem dado divulgado
    mpd = con.execute("SELECT sum(moradores) / sum(responsaveis) FROM base_setor "
                      "WHERE responsaveis > 0 AND moradores > 0").fetchone()[0] or 2.8

    # Cada domicílio do CNEFE "carrega" os valores médios do seu setor.
    #   A = soma(renda dos responsáveis) / soma(moradores)          -> razão de somas
    #   C = média da renda da AP ponderada por moradores
    con.execute(f"""
        CREATE OR REPLACE TABLE base_cep AS
        WITH j AS (
            SELECT c.cep, c.setor, c.n_dom, b.renda_media_responsavel AS rm,
                   b.moradores / nullif(b.responsaveis, 0) AS mpd,
                   b.renda_responsavel_per_capita IS NOT NULL AS tem_a,
                   b.area_ponderacao AS ap, b.nome_area_ponderacao AS nome_ap,
                   b.renda_domiciliar_per_capita_ap AS rdpc
            FROM cep_setor c LEFT JOIN base_setor b USING (setor)
        ),
        mun AS (  -- município predominante do CEP
            SELECT cep, arg_max(m, n) AS cod_municipio
            FROM (SELECT cep, left(setor, 7) AS m, sum(n_dom) AS n FROM cep_setor GROUP BY 1, 2)
            GROUP BY 1
        )
        SELECT j.cep,
               mun.cod_municipio,
               sum(n_dom)::BIGINT                                   AS n_domicilios_cnefe,
               count(DISTINCT setor)                                AS n_setores,
               round(sum(n_dom * rm) FILTER (tem_a)
                     / nullif(sum(n_dom * mpd) FILTER (tem_a), 0), 2) AS renda_responsavel_per_capita,
               round(coalesce(sum(n_dom) FILTER (tem_a), 0) / sum(n_dom), 3) AS cobertura_a,
               round(sum(n_dom * coalesce(mpd, {mpd}) * rdpc) FILTER (rdpc IS NOT NULL)
                     / nullif(sum(n_dom * coalesce(mpd, {mpd})) FILTER (rdpc IS NOT NULL), 0), 2)
                                                                    AS renda_domiciliar_per_capita_ap,
               round(coalesce(sum(n_dom) FILTER (rdpc IS NOT NULL), 0) / sum(n_dom), 3) AS cobertura_c,
               count(DISTINCT ap)                                   AS n_areas_ponderacao,
               arg_max(nome_ap, n_dom)                              AS area_ponderacao_principal
        FROM j JOIN mun USING (cep)
        GROUP BY j.cep, mun.cod_municipio
        ORDER BY j.cep
    """)
    con.execute("""ALTER TABLE base_cep ADD COLUMN razao_c_sobre_a DOUBLE""")
    con.execute("""UPDATE base_cep SET razao_c_sobre_a =
                   round(renda_domiciliar_per_capita_ap / nullif(renda_responsavel_per_capita, 0), 3)""")
    # CEPs com base suficiente para a medida A (poucos domicílios ou baixa cobertura = ruído)
    con.execute("ALTER TABLE base_cep ADD COLUMN base_suficiente BOOLEAN")
    con.execute(f"""UPDATE base_cep SET base_suficiente =
                    (cobertura_a >= {MIN_COBERTURA} AND n_domicilios_cnefe >= {MIN_DOMICILIOS}
                     AND n_setores < {MAX_SETORES})""")

    saida.mkdir(parents=True, exist_ok=True)
    for tabela in ("base_cep", "base_setor"):
        nome = tabela.replace("base", "renda_per_capita") + f"_{rotulo}"
        con.execute(f"COPY {tabela} TO '{(saida / (nome + '.parquet')).as_posix()}' (FORMAT parquet)")
        con.execute(f"COPY {tabela} TO '{(saida / (nome + '.csv')).as_posix()}' (HEADER, DELIMITER ';')")
        log(f"  gravado: {saida / nome}.csv e .parquet")
    # Versão para abrir em Excel/Numbers em pt-BR: vírgula decimal e CEP com hífen
    # (o hífen impede a planilha de tratar o CEP como número e cortar o zero inicial).
    arq = saida / f"renda_per_capita_cep_{rotulo}_planilha.csv"
    cols = []
    for nome_col, tipo, *_ in con.execute("DESCRIBE base_cep").fetchall():
        if nome_col == "cep":
            cols.append("left(cep, 5) || '-' || right(cep, 3) AS cep")
        elif tipo in ("DOUBLE", "FLOAT") or tipo.startswith("DECIMAL"):
            cols.append(f"replace(CAST({nome_col} AS VARCHAR), '.', ',') AS {nome_col}")
        else:
            cols.append(nome_col)
    con.execute(f"COPY (SELECT {', '.join(cols)} FROM base_cep) "
                f"TO '{arq.as_posix()}' (HEADER, DELIMITER ';')")
    log(f"  gravado: {arq} (formato brasileiro, para planilha)")

    # ---------------- validação ----------------
    n, n_a, n_c, dom = con.execute("""
        SELECT count(*), count(renda_responsavel_per_capita),
               count(renda_domiciliar_per_capita_ap), sum(n_domicilios_cnefe) FROM base_cep""").fetchone()
    print("\n=== Resumo ===")
    print(f"CEPs: {n:,} | com medida A: {n_a:,} | com medida C: {n_c:,} | domicílios CNEFE: {dom:,.0f}")
    q = con.execute("""
        SELECT quantile_cont(renda_responsavel_per_capita, [0.1, 0.5, 0.9]),
               quantile_cont(renda_domiciliar_per_capita_ap, [0.1, 0.5, 0.9]) FROM base_cep""").fetchone()
    fmt = lambda v: "n/d" if not v else " / ".join(f"R$ {x:,.0f}" for x in v)
    print(f"A  (p10 / mediana / p90 entre CEPs): {fmt(q[0])}")
    print(f"C  (p10 / mediana / p90 entre CEPs): {fmt(q[1])}")
    if n_a and n_c:
        r = con.execute("""
            WITH t AS (SELECT renda_responsavel_per_capita a, renda_domiciliar_per_capita_ap c,
                              n_domicilios_cnefe w FROM base_cep
                       WHERE renda_responsavel_per_capita IS NOT NULL
                         AND renda_domiciliar_per_capita_ap IS NOT NULL),
                 k AS (SELECT *, rank() OVER (ORDER BY a) ra, rank() OVER (ORDER BY c) rc FROM t)
            SELECT corr(a, c), corr(ra, rc), sum(w * c) / sum(w * a), median(c / a) FROM k""").fetchone()
        print(f"Correlação A x C entre CEPs: Pearson {r[0]:.3f} | Spearman {r[1]:.3f}")
        print(f"Razão C/A: agregada {r[2]:.2f} | mediana dos CEPs {r[3]:.2f} "
              "(esperado > 1: C inclui a renda de todos os moradores)")
    suf = con.execute("SELECT count(*) FROM base_cep WHERE base_suficiente").fetchone()[0]
    print(f"CEPs com base suficiente (cobertura A >= {MIN_COBERTURA:.0%}, {MIN_DOMICILIOS}+ domicílios, menos de {MAX_SETORES} setores): {suf:,}")
    baixa = con.execute("SELECT count(*) FROM base_cep WHERE cobertura_a < 0.5").fetchone()[0]
    grandes = con.execute("SELECT count(*) FROM base_cep WHERE n_setores >= 20").fetchone()[0]
    print(f"CEPs com cobertura A < 50%: {baixa:,} | CEPs espalhados por 20+ setores "
          f"(CEP geral de município ou de grande área): {grandes:,}")


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Renda per capita por CEP (IBGE, Censo 2022).")
    ap.add_argument("--uf", help="sigla da UF, ex.: SP (processa o estado inteiro)")
    ap.add_argument("--municipio", help="código IBGE de 7 dígitos, ex.: 3550308 (São Paulo)")
    ap.add_argument("--sem-amostra", action="store_true", help="pula a opção C (tabela por área de ponderação)")
    ap.add_argument("--cache", default="cache_ibge", help="pasta dos downloads (padrão: ./cache_ibge)")
    ap.add_argument("--saida", default="saida", help="pasta dos resultados (padrão: ./saida)")
    a = ap.parse_args()

    if a.municipio:
        if not re.fullmatch(r"\d{7}", a.municipio):
            sys.exit("--municipio deve ter 7 dígitos (código IBGE), ex.: 3550308")
        cod_uf, prefixo, rotulo = int(a.municipio[:2]), a.municipio, a.municipio
    elif a.uf and a.uf.upper() in UFS:
        cod_uf = UFS[a.uf.upper()]
        prefixo, rotulo = str(cod_uf), a.uf.upper()
    else:
        sys.exit("Informe --municipio 3550308 ou --uf SP")
    if cod_uf not in SIGLA:
        sys.exit(f"UF {cod_uf} inválida.")

    cache, saida = Path(a.cache), Path(a.saida)
    cache.mkdir(parents=True, exist_ok=True)
    # Banco de trabalho em disco (aguenta UF grande). É descartável: recriado a cada
    # execução, porque versões diferentes do DuckDB não leem o arquivo umas das outras.
    banco = cache / f"trabalho_{rotulo}.duckdb"
    try:
        banco.unlink(missing_ok=True)
        con = duckdb.connect(str(banco))
    except (OSError, duckdb.Error):
        import tempfile
        banco = Path(tempfile.mkdtemp()) / banco.name
        log(f"  aviso: usando banco de trabalho temporário em {banco}")
        con = duckdb.connect(str(banco))

    etapa_cnefe(con, cache, cod_uf, a.municipio)
    etapa_renda_setor(con, cache, prefixo)
    com_amostra = False
    if not a.sem_amostra:
        try:
            com_amostra = etapa_amostra(con, cache, prefixo)
        except SystemExit as e:
            log(f"  opção C indisponível nesta execução: {e}")
        if not com_amostra:
            log("  seguindo só com a opção A; a coluna C ficará vazia.")
    etapa_final(con, saida, rotulo, com_amostra)


if __name__ == "__main__":
    main()
