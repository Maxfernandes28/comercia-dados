# comercia-dados

Dados públicos usados pela prospecção do ComercIA.

- **Empresas ativas** da [base aberta de CNPJ da Receita Federal](https://www.gov.br/receitafederal/pt-br/assuntos/orientacao-tributaria/cadastros/consultas/dados-publicos-cnpj),
  separadas por estado (uma release por UF) e por divisão de atividade (um arquivo por divisão do CNAE principal).
  Atualizado todo mês pelo workflow `receita.yml`.
- **Municípios** com coordenadas do centro, para a busca por raio (fonte: kelvins/municipios-brasileiros).

Nada aqui é dado de clientes ou da empresa: só informação que a Receita já publica.

O workflow `avisos.yml` chama o sistema a cada 5 minutos para disparar os avisos da agenda.
