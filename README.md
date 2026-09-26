# comercia-dados

Dados públicos usados pela prospecção do ComercIA.

- **Empresas ativas** da [base aberta de CNPJ da Receita Federal](https://www.gov.br/receitafederal/pt-br/assuntos/orientacao-tributaria/cadastros/consultas/dados-publicos-cnpj),
  separadas por estado (uma release por UF) e por divisão de atividade (um arquivo por divisão do CNAE principal).
- **Municípios** com coordenadas do centro, para a busca por raio (fonte: kelvins/municipios-brasileiros).

Nada aqui é dado de clientes ou da empresa: só informação que a Receita já publica.

## Como a base é atualizada

O servidor da Receita só aceita downloads de conexões no Brasil, então o GitHub Actions
(que roda nos EUA) não consegue baixar. A atualização roda uma vez por mês num computador
no Brasil, com `atualizar_local.py`, que trabalha em passos curtos e retoma de onde parou:

    TOKEN_ARQUIVO=/caminho/chave-github.txt python3 atualizar_local.py   # repetir enquanto sair com 3

Saídas: `0` pronto (ou o mês já estava publicado), `3` continua, `2` falta a chave do GitHub.
A chave precisa só de acesso de escrita a "Contents" deste repositório.

Os arquivos de cada estado são trocados um a um dentro da release, então a busca não fica sem dados
durante a atualização. O `meta.json` (na release `base`) é o último a subir.

`processar.py` faz o mesmo serviço de uma vez só, para quem tem uma máquina sem limite de tempo.

## Avisos da agenda

O workflow `avisos.yml` chama o sistema a cada 5 minutos para disparar os avisos da agenda.
