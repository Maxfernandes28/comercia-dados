# comercia-dados

Robô de apoio do ComercIA.

O workflow `avisos.yml` chama o sistema a cada 5 minutos (das 7h às 23h) para disparar os avisos da agenda
no celular e no computador de quem ligou as notificações.

A busca de empresas da prospecção não usa mais base guardada: ela consulta o Google Maps e o OpenStreetMap
na hora da pesquisa, e só as empresas que a pessoa escolhe entram no sistema.
