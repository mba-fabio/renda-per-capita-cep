# Renda per capita por CEP (IBGE, Censo 2022)

Script em Python que monta uma base de renda per capita por CEP usando apenas dados públicos do IBGE, sem login. Ele baixa os arquivos, faz os cruzamentos e grava o resultado em CSV e Parquet.

O IBGE não publica renda por CEP. A base é **derivada**: a renda é publicada por setor censitário e por área de ponderação, e o script a leva para o CEP usando o cadastro de endereços do Censo (CNEFE). Leia a seção [Limitações](#limitações) antes de usar os números.

## O que o script calcula

Para cada CEP, duas medidas em colunas separadas, para comparação:

| Coluna | Fonte | O que é |
|---|---|---|
| `renda_responsavel_per_capita` (medida A) | Universo do Censo 2022, por setor censitário | Renda dos responsáveis pelos domicílios dividida pelo total de moradores. É um **proxy**: só a renda do responsável é publicada nesse nível. Granularidade fina, valor subestimado. |
| `renda_domiciliar_per_capita_ap` (medida C) | Amostra do Censo 2022, por área de ponderação (Tabela 8.3) | Rendimento domiciliar mensal per capita médio, o conceito oficial. Granularidade grossa: CEPs vizinhos da mesma área recebem o mesmo valor. |

Valores em reais nominais de 2022.

## Fontes

Todas no FTP público do IBGE (`https://ftp.ibge.gov.br`):

| Dado | Pasta |
|---|---|
| CNEFE 2022 (endereços com CEP e setor) | `Cadastro_Nacional_de_Enderecos_para_Fins_Estatisticos/Censo_Demografico_2022/Arquivos_CNEFE/CSV/` |
| Renda do responsável por setor | `Censos/Censo_Demografico_2022/Agregados_por_Setores_Censitarios_Rendimento_do_Responsavel/` |
| Tabelas por área de ponderação (usa a 8.3) | `Censos/Censo_Demografico_2022/Microdados_e_Areas_de_Ponderacao/Areas_de_Ponderacao/` |
| Composição das áreas de ponderação (setor → área) | `Censos/Censo_Demografico_2022/Microdados_e_Areas_de_Ponderacao/Documentacao/Áreas de ponderação/` |

Os nomes de arquivo do IBGE levam data e mudam a cada republicação. O script lista a pasta e pega o mais recente, em vez de fixar o nome.

## Passo a passo

### 1. Pré-requisitos

- Python 3.9 ou superior
- Acesso à internet para `ftp.ibge.gov.br` (HTTPS)
- Espaço em disco: alguns GB livres para um município grande (os arquivos do IBGE são descompactados no cache); bem mais para um estado inteiro

### 2. Baixar o projeto e instalar as dependências

```bash
git clone https://github.com/SEU_USUARIO/renda-per-capita-cep.git
cd renda-per-capita-cep
pip install -r requirements.txt
```

### 3. Executar

Por município (código IBGE de 7 dígitos):

```bash
python renda_per_capita_cep.py --municipio 3550308
```

Por estado:

```bash
python renda_per_capita_cep.py --uf SP
```

No Spyder ou IPython, use `%run` no console (o botão Run não passa os argumentos):

```python
%cd /caminho/para/renda-per-capita-cep
%run renda_per_capita_cep.py --municipio 3550308
```

Opções:

| Opção | Efeito |
|---|---|
| `--municipio 3550308` | Processa um município |
| `--uf SP` | Processa o estado inteiro |
| `--sem-amostra` | Calcula só a medida A |
| `--cache PASTA` | Pasta dos downloads (padrão `./cache_ibge`) |
| `--saida PASTA` | Pasta dos resultados (padrão `./saida`) |

### 4. O que acontece na execução

1. **CNEFE:** baixa o cadastro de endereços e conta os domicílios particulares por par CEP × setor censitário.
2. **Renda por setor:** baixa a renda do responsável e calcula a medida A de cada setor. Imprime as descrições oficiais das variáveis usadas, para conferência.
3. **Renda por área de ponderação:** baixa as tabelas da amostra, localiza a de rendimento domiciliar per capita e liga cada setor à sua área.
4. **Agregação por CEP:** grava os arquivos e imprime um resumo de validação.

Os downloads ficam em `cache_ibge/` e não se repetem nas execuções seguintes.

### 5. Conferir o resumo

O log termina com um resumo. Para São Paulo (`--municipio 3550308`), em execução de novembro de 2026:

```
CEPs: 45,892 | com medida A: 45,291 | com medida C: 45,304 | domicílios CNEFE: 4,992,162
A  (p10 / mediana / p90 entre CEPs): R$ 640 / R$ 1,120 / R$ 4,059
C  (p10 / mediana / p90 entre CEPs): R$ 1,097 / R$ 1,808 / R$ 7,001
Correlação A x C entre CEPs: Pearson 0.823 | Spearman 0.840
Razão C/A: agregada 1.47 | mediana dos CEPs 1.63
```

Se os seus números baterem com esses, a execução reproduziu o resultado. Diferenças pequenas podem aparecer se o IBGE republicar algum arquivo.

## Arquivos gerados

Em `saida/`, com o código do município ou a sigla da UF no nome:

| Arquivo | Conteúdo |
|---|---|
| `renda_per_capita_cep_<recorte>.csv` e `.parquet` | Base por CEP. CSV com `;` e ponto decimal; CEP com 8 dígitos. |
| `renda_per_capita_cep_<recorte>_planilha.csv` | A mesma base para abrir no Excel ou Numbers: vírgula decimal e CEP como `04552-060`. |
| `renda_per_capita_setor_<recorte>.csv` e `.parquet` | Base por setor censitário, para auditoria e mapas. |

Colunas da base por CEP:

| Coluna | Significado |
|---|---|
| `cep` | CEP registrado pelo recenseador no CNEFE |
| `cod_municipio` | Município predominante do CEP |
| `n_domicilios_cnefe` | Domicílios particulares do CEP no CNEFE |
| `n_setores` | Setores censitários em que o CEP aparece |
| `renda_responsavel_per_capita` | Medida A |
| `cobertura_a` | Fração dos domicílios do CEP em setores com renda divulgada |
| `renda_domiciliar_per_capita_ap` | Medida C |
| `cobertura_c` | Fração dos domicílios do CEP com área de ponderação identificada |
| `n_areas_ponderacao` | Áreas de ponderação em que o CEP aparece |
| `area_ponderacao_principal` | Nome da área com mais domicílios do CEP |
| `razao_c_sobre_a` | Medida C dividida pela medida A |
| `base_suficiente` | `true` se cobertura A ≥ 50%, 10 ou mais domicílios e menos de 20 setores |

Recomenda-se usar apenas os CEPs com `base_suficiente = true`.

## Método

**Ponte CEP → setor.** Cada endereço do CNEFE traz o CEP e o setor censitário. O script usa só os domicílios particulares (`COD_ESPECIE = 1`) e conta quantos há em cada par CEP × setor. Essa contagem é o peso.

**Medida A.** O arquivo do IBGE traz, por setor, o número de responsáveis (`V06001`), o de moradores (`V06002`) e o rendimento médio dos responsáveis com rendimento (`V06004`). Por setor: `V06004 × V06001 / V06002`. Por CEP, razão de somas ponderada pelos domicílios:

```
A(cep) = Σ n × V06004  /  Σ n × (V06002 / V06001)
```

onde `n` é o número de domicílios do CEP em cada setor. Setores com dado suprimido pelo IBGE ficam de fora e reduzem a `cobertura_a`.

**Medida C.** Cada setor herda a renda média da sua área de ponderação (Tabela 8.3). Por CEP, média ponderada pelos moradores estimados (`n × moradores por domicílio do setor`).

## Limitações

- **A medida A não é renda per capita real.** Ela ignora a renda dos demais moradores, por isso fica abaixo da medida C (em São Paulo, C é 1,47 vez A no agregado). Além disso, `V06004` é a média só dos responsáveis com rendimento, e o arquivo não informa quantos são; o script multiplica pelo total de responsáveis, o que infla um pouco o numerador.
- **A medida C é uma média de área grande.** Uma área de ponderação reúne dezenas de setores. Um CEP rico dentro de uma área mista aparece com a média da área.
- **Extremos são instáveis.** Em setores pequenos, poucos responsáveis de renda muito alta puxam a medida A (há CEPs com A acima de R$ 40 mil e C em torno de R$ 15 mil).
- **O CEP é o do CNEFE, não o dos Correios.** É o que o recenseador registrou. CEPs genéricos aparecem espalhados por muitos setores (o 01001-000 aparece em 168 setores de São Paulo) e ficam com `base_suficiente = false`. Municípios pequenos com CEP único recebem a média da cidade.
- **Dados de 2022.** Valores nominais da data de referência do Censo, sem correção monetária.
- **Execução por UF.** O script foi validado com o município de São Paulo. O estado inteiro usa o mesmo código, mas ainda não foi testado; o CNEFE de SP tem cerca de 1 GB compactado.

## Problemas comuns

| Sintoma | Causa e solução |
|---|---|
| `SystemExit: Informe --municipio 3550308 ou --uf SP` | O script rodou sem argumentos. Veja o passo 3. |
| `ModuleNotFoundError` | Faltou `pip install -r requirements.txt` no mesmo ambiente Python. |
| Erro de download ou rede bloqueada | Baixe os arquivos à mão das pastas da seção Fontes para `cache_ibge/` e rode de novo; o script usa o que encontrar lá. Outra saída é copiar a pasta `cache_ibge/` de uma máquina onde a execução funcionou. |
| CEP sem zero à esquerda ou números estranhos na planilha | Abra o arquivo `_planilha.csv`, não o CSV principal. |

## Dados

Os dados são do IBGE e de uso público. Este repositório contém só o código; nenhum arquivo do IBGE é redistribuído aqui.
