"""Gera o relatório HTML estático (docs/index.html) a partir do cache Parquet criado por analise_urnas.ipynb.

Uso:  python3 gerar_relatorio.py
"""
import json
import math
from datetime import date
from pathlib import Path

import duckdb

BASE = Path(__file__).resolve().parent
CACHE = BASE / 'cache'
DOCS = BASE / 'docs'
LIMIAR = 0.10

con = duckdb.connect()
for t in (1, 2):
    for tabela in ('votos', 'secoes'):
        arq = CACHE / f'{tabela}_t{t}.parquet'
        if not arq.exists():
            raise SystemExit(f'{arq} não encontrado — execute antes o notebook analise_urnas.ipynb')
        con.execute(f"CREATE VIEW {tabela}_t{t} AS SELECT * FROM read_parquet('{arq}')")


def rows(sql):
    """Executa a consulta e devolve lista de dicts serializáveis."""
    df = con.sql(sql).df()
    return json.loads(df.to_json(orient='records', force_ascii=False))


con.execute('''
    CREATE TEMP TABLE pres AS
    SELECT turno, uf, cd_municipio, zona, secao, any_value(local_votacao) AS local_votacao,
           coalesce(sum(votos) FILTER (WHERE nr_votavel = 13), 0) AS lula,
           coalesce(sum(votos) FILTER (WHERE nr_votavel = 22), 0) AS flavio,
           sum(votos) FILTER (WHERE tipo_votavel = 'Nominal') AS validos,
           max(aptos) AS aptos, max(comparecimento) AS comparecimento, any_value(nr_urna) AS nr_urna
    FROM (SELECT 1 AS turno, * FROM votos_t1 UNION ALL BY NAME SELECT 2 AS turno, * FROM votos_t2)
    WHERE cargo = 'Presidente'
    GROUP BY ALL
''')
con.execute('''
    CREATE TEMP TABLE secoes AS
    SELECT 1 AS turno, * FROM secoes_t1 UNION ALL BY NAME SELECT 2 AS turno, * FROM secoes_t2
''')

d = {'gerado_em': date.today().strftime('%d/%m/%Y'), 'limiar': LIMIAR * 100}

# --- visão geral e resultados -------------------------------------------------
d['visao'] = rows('''
    SELECT turno, count(*) AS urnas, count(DISTINCT uf || cd_municipio) AS municipios,
           sum(aptos) AS aptos, sum(comparecimento) AS comparecimento,
           round(100.0 * sum(comparecimento) / sum(aptos), 2) AS pct_comparecimento
    FROM secoes GROUP BY turno ORDER BY turno
''')
for t in (1, 2):
    d[f'pres{t}'] = rows(f'''
        SELECT candidato, partido, sum(votos) AS votos,
               round(100.0 * sum(votos) / sum(sum(votos)) OVER (), 2) AS pct
        FROM votos_t{t} WHERE cargo = 'Presidente' AND tipo_votavel = 'Nominal'
        GROUP BY candidato, partido ORDER BY votos DESC
    ''')
    d[f'brancos_nulos{t}'] = rows(f'''
        SELECT tipo_votavel AS tipo, sum(votos) AS votos,
               round(100.0 * sum(votos) / sum(sum(votos)) OVER (), 2) AS pct
        FROM votos_t{t} WHERE cargo = 'Presidente' GROUP BY 1 ORDER BY votos DESC
    ''')
d['pres2_uf'] = rows('''
    SELECT uf, round(100.0 * sum(lula) / sum(validos), 2) AS lula,
           round(100.0 * sum(flavio) / sum(validos), 2) AS flavio
    FROM pres WHERE turno = 2 GROUP BY uf ORDER BY lula DESC
''')
d['gov'] = rows('''
    WITH g AS (
        SELECT turno, uf, candidato, partido, sum(votos) AS votos
        FROM (SELECT 1 AS turno, * FROM votos_t1 UNION ALL BY NAME SELECT 2 AS turno, * FROM votos_t2)
        WHERE cargo = 'Governador' AND tipo_votavel = 'Nominal' GROUP BY ALL
    ), r AS (
        SELECT *, round(100.0 * votos / sum(votos) OVER (PARTITION BY turno, uf), 2) AS pct,
               row_number() OVER (PARTITION BY turno, uf ORDER BY votos DESC) AS pos
        FROM g
    )
    SELECT uf, turno, candidato, partido, votos, pct FROM r
    WHERE pos = 1 AND (turno = 2 OR uf NOT IN (SELECT DISTINCT uf FROM votos_t2 WHERE cargo = 'Governador'))
    ORDER BY uf
''')

# --- candidato único -----------------------------------------------------------
con.execute('''
    CREATE TEMP TABLE unico AS
    WITH u AS (
        SELECT turno, uf, cd_municipio, zona, secao, cargo,
               count(*) FILTER (WHERE tipo_votavel = 'Nominal' AND votos > 0) AS n,
               sum(votos) FILTER (WHERE tipo_votavel = 'Nominal') AS votos,
               coalesce(sum(votos) FILTER (WHERE tipo_votavel IN ('Branco', 'Nulo')), 0) AS brancos_nulos,
               arg_max(candidato, votos) FILTER (WHERE tipo_votavel = 'Nominal') AS candidato
        FROM (SELECT 1 AS turno, * FROM votos_t1 UNION ALL BY NAME SELECT 2 AS turno, * FROM votos_t2)
        WHERE cargo IN ('Presidente', 'Governador', 'Senador')
        GROUP BY ALL
    )
    SELECT * FROM u WHERE n = 1
''')
for chave, cargo in (('unico_pres', 'Presidente'), ('unico_gov', 'Governador'), ('unico_sen', 'Senador')):
    uf_col = '' if cargo == 'Presidente' else 'uf, '
    d[chave] = rows(f'''
        SELECT turno, {uf_col} candidato, count(*) AS urnas, sum(votos) AS votos, sum(brancos_nulos) AS brancos_nulos
        FROM unico WHERE cargo = '{cargo}'
        GROUP BY ALL ORDER BY turno, {uf_col} urnas DESC
    ''')
d['unico_uf'] = rows('''
    SELECT uf, count(*) FILTER (WHERE candidato = 'LULA') AS lula,
           count(*) FILTER (WHERE candidato = 'FLAVIO BOLSONARO') AS flavio,
           coalesce(sum(votos) FILTER (WHERE candidato = 'LULA'), 0) AS votos_lula,
           coalesce(sum(votos) FILTER (WHERE candidato = 'FLAVIO BOLSONARO'), 0) AS votos_flavio
    FROM unico WHERE turno = 2 AND cargo = 'Presidente' GROUP BY uf ORDER BY lula + flavio DESC
''')
d['unico_local'] = rows('''
    WITH mun AS (SELECT uf, cd_municipio, 100.0 * sum(lula) / sum(validos) AS pct_mun FROM pres WHERE turno = 2 GROUP BY ALL),
         loc AS (SELECT uf, cd_municipio, zona, local_votacao, count(*) AS urnas_local,
                        100.0 * sum(lula) / sum(validos) AS pct_local FROM pres WHERE turno = 2 GROUP BY ALL)
    SELECT p.uf, s.municipio, p.zona, p.secao, p.validos,
           CASE WHEN p.lula = p.validos THEN 'LULA' ELSE 'FLAVIO BOLSONARO' END AS candidato,
           round(pct_mun, 1) AS pct_lula_municipio, round(pct_local, 1) AS pct_lula_local, urnas_local
    FROM pres p
    JOIN (SELECT uf, cd_municipio, zona, secao, municipio FROM secoes_t2) s USING (uf, cd_municipio, zona, secao)
    JOIN mun USING (uf, cd_municipio)
    JOIN loc USING (uf, cd_municipio, zona, local_votacao)
    WHERE p.turno = 2 AND p.validos >= 30 AND (p.lula = p.validos OR p.flavio = p.validos)
    ORDER BY pct_mun
''')

# --- variação 1º × 2º turno ----------------------------------------------------
con.execute('''
    CREATE TEMP TABLE var_urna AS
    SELECT a.uf, a.cd_municipio, a.municipio, a.zona, a.secao, a.aptos AS aptos_t1, b.aptos AS aptos_t2,
           a.comparecimento AS votos_t1, b.comparecimento AS votos_t2,
           (b.comparecimento - a.comparecimento) / nullif(a.comparecimento, 0) AS variacao
    FROM secoes_t1 a JOIN secoes_t2 b USING (uf, cd_municipio, zona, secao)
''')
con.execute(f'''
    CREATE TEMP TABLE var_cidade AS
    SELECT uf, municipio, count(*) AS urnas, sum(votos_t1) AS votos_t1, sum(votos_t2) AS votos_t2,
           round(100.0 * (sum(votos_t2) - sum(votos_t1)) / sum(votos_t1), 2) AS variacao_pct,
           count(*) FILTER (WHERE abs(variacao) > {LIMIAR}) AS urnas_acima
    FROM var_urna GROUP BY uf, cd_municipio, municipio
''')
d['var_resumo'] = rows(f'''
    SELECT (SELECT count(*) FROM var_cidade) AS cidades,
           (SELECT count(*) FROM var_cidade WHERE abs(variacao_pct) > {LIMIAR * 100}) AS cidades_acima,
           (SELECT round(median(variacao_pct), 2) FROM var_cidade) AS mediana_cidades,
           (SELECT count(*) FROM var_urna WHERE variacao IS NOT NULL) AS urnas,
           (SELECT count(*) FROM var_urna WHERE abs(variacao) > {LIMIAR}) AS urnas_acima
''')[0]
d['var_hist'] = rows('''
    SELECT CAST(floor(greatest(least(variacao_pct, 39.99), -30) / 2) * 2 AS INTEGER) AS faixa, count(*) AS cidades
    FROM var_cidade GROUP BY 1 ORDER BY 1
''')
d['var_cidades'] = rows(f'''
    SELECT * FROM var_cidade WHERE abs(variacao_pct) > {LIMIAR * 100} ORDER BY abs(variacao_pct) DESC
''')
d['var_urnas_uf'] = rows(f'''
    SELECT uf, count(*) FILTER (WHERE abs(variacao) > {LIMIAR}) AS urnas,
           round(100.0 * count(*) FILTER (WHERE abs(variacao) > {LIMIAR}) / count(*), 2) AS pct
    FROM var_urna GROUP BY uf ORDER BY pct DESC
''')
d['var_urnas_top'] = rows('''
    SELECT uf, municipio, zona, secao, aptos_t1, aptos_t2, votos_t1, votos_t2,
           round(100.0 * variacao, 1) AS variacao_pct
    FROM var_urna WHERE variacao IS NOT NULL ORDER BY abs(variacao) DESC LIMIT 30
''')

# --- testes de indícios --------------------------------------------------------
d['hist_lula'] = rows('''
    WITH f AS (
        SELECT CAST(least(floor(100.0 * lula / validos / 5) * 5, 100) AS INTEGER) AS f_lula,
               CAST(least(floor(100.0 * flavio / validos / 5) * 5, 100) AS INTEGER) AS f_flavio
        FROM pres WHERE turno = 2 AND validos >= 50
    )
    SELECT faixa, coalesce(l.urnas, 0) AS urnas_lula, coalesce(b.urnas, 0) AS urnas_flavio
    FROM range(0, 105, 5) r(faixa)
    LEFT JOIN (SELECT f_lula AS faixa, count(*) AS urnas FROM f GROUP BY 1) l USING (faixa)
    LEFT JOIN (SELECT f_flavio AS faixa, count(*) AS urnas FROM f GROUP BY 1) b USING (faixa)
    ORDER BY faixa
''')
d['alto_comp'] = rows('''
    SELECT turno, count(*) FILTER (WHERE comparecimento >= 0.97 * aptos) AS urnas_97,
           count(*) FILTER (WHERE comparecimento = aptos) AS urnas_100,
           sum(comparecimento) FILTER (WHERE comparecimento >= 0.97 * aptos) AS votos,
           round(100.0 * sum(lula) FILTER (WHERE comparecimento >= 0.97 * aptos)
                 / sum(validos) FILTER (WHERE comparecimento >= 0.97 * aptos), 1) AS pct_lula,
           round(100.0 * sum(lula) / sum(validos), 1) AS pct_lula_geral
    FROM pres WHERE aptos >= 50 GROUP BY turno ORDER BY turno
''')
base_fp = "FROM pres WHERE turno = 2 AND aptos >= 50 AND validos > 0 AND uf <> 'ZZ'"
d['corr_nacional'] = round(con.sql(f'SELECT corr(1.0 * comparecimento / aptos, 1.0 * lula / validos) {base_fp}').fetchall()[0][0], 3)
d['corr_uf'] = rows(f'''
    SELECT uf, round(corr(1.0 * comparecimento / aptos, 1.0 * lula / validos), 3) AS corr
    {base_fp} GROUP BY uf ORDER BY corr
''')
d['heatmap'] = rows(f'''
    SELECT CAST(least(greatest(floor(20.0 * comparecimento / aptos), 10), 19) AS INTEGER) AS lin,
           CAST(least(floor(10.0 * lula / validos), 9) AS INTEGER) AS col, count(*) AS urnas
    {base_fp} GROUP BY ALL
''')
d['digitos'] = rows('''
    SELECT turno, CAST(votos % 10 AS INTEGER) AS digito,
           round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY turno), 3) AS pct
    FROM (SELECT turno, lula AS votos FROM pres UNION ALL SELECT turno, flavio FROM pres)
    WHERE votos >= 10 GROUP BY 1, 2 ORDER BY turno, digito
''')
def qui2_p(x, gl):
    """p-valor do qui-quadrado (cauda superior), pela série da gama incompleta."""
    a, z = gl / 2, x / 2
    termo = soma = 1 / a
    for n in range(1, 500):
        termo *= z / (a + n)
        soma += termo
    return max(0.0, 1 - soma * math.exp(-z + a * math.log(z) - math.lgamma(a)))


d['digitos_qui2'] = []
for t, x2, n in con.sql('''
    WITH c AS (
        SELECT turno, votos % 10 AS digito, count(*) AS n
        FROM (SELECT turno, lula AS votos FROM pres UNION ALL SELECT turno, flavio FROM pres)
        WHERE votos >= 10 GROUP BY 1, 2
    )
    SELECT turno, sum((n - tot / 10.0) ^ 2 / (tot / 10.0)), any_value(tot)
    FROM (SELECT *, sum(n) OVER (PARTITION BY turno) AS tot FROM c) GROUP BY turno ORDER BY turno
''').fetchall():
    d['digitos_qui2'].append({'turno': t, 'contagens': int(n), 'qui2': round(x2, 1), 'limite': 16.9,
                              'p': round(qui2_p(x2, 9), 2),
                              'leitura': 'dentro do acaso' if x2 < 16.9 else 'acima do esperado'})

pares = "FROM (SELECT * FROM pres WHERE turno = 1) a JOIN (SELECT * FROM pres WHERE turno = 2) b USING (uf, cd_municipio, zona, secao)"
d['swing'] = rows(f'''
    WITH g AS (
        SELECT uf, cd_municipio, zona, secao,
               CASE WHEN abs(b.comparecimento - a.comparecimento) > {LIMIAR} * a.comparecimento
                    THEN 'Variação > 10%' ELSE 'Demais urnas' END AS grupo
        {pares} WHERE a.comparecimento > 0
    )
    SELECT grupo, turno, count(*) AS urnas, sum(comparecimento) AS votos,
           round(100.0 * (sum(aptos) - sum(comparecimento)) / sum(aptos), 2) AS abstencao_pct,
           sum(lula) AS lula, round(100.0 * sum(lula) / sum(validos), 2) AS lula_pct,
           sum(flavio) AS flavio, round(100.0 * sum(flavio) / sum(validos), 2) AS flavio_pct,
           sum(validos - lula - flavio) AS demais, round(100.0 * sum(validos - lula - flavio) / sum(validos), 2) AS demais_pct,
           round(100.0 * sum(lula) / sum(lula + flavio), 2) AS lula_confronto
    FROM g JOIN pres p USING (uf, cd_municipio, zona, secao)
    GROUP BY grupo, turno ORDER BY grupo, turno
''')
d['substituidas'] = rows(f'''
    SELECT CASE WHEN a.nr_urna <> b.nr_urna THEN 'Urna substituída' ELSE 'Mesma urna' END AS grupo,
           count(*) AS urnas,
           round(100.0 * sum(a.lula) / sum(a.lula + a.flavio), 2) AS lula_1t,
           round(100.0 * sum(b.lula) / sum(b.validos), 2) AS lula_2t
    {pares} GROUP BY 1 ORDER BY 1
''')

DOCS.mkdir(exist_ok=True)
html = (BASE / 'relatorio_template.html').read_text(encoding='utf-8')
html = html.replace('/*__DADOS__*/null', json.dumps(d, ensure_ascii=False))
(DOCS / 'index.html').write_text(html, encoding='utf-8')
print(f'Relatório gravado em {DOCS / "index.html"}')
