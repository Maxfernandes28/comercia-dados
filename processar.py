"""Separa a base aberta de CNPJ da Receita por estado e atividade.

Roda uma vez por mês num computador no Brasil (a Receita recusa conexões de fora);
no dia a dia quem roda é atualizar_local.py, em passos curtos.
Baixa os arquivos que a Receita publica, fica só com as empresas ATIVAS e
grava um arquivo por estado e por divisão de atividade (os 2 primeiros
dígitos do CNAE principal: 47 = comércio varejista, 86 = saúde...). Cada
arquivo vira um anexo de uma "release" do GitHub com o nome do estado.

Assim o sistema de vendas, na hora de uma busca, baixa só o pedaço que
interessa (ex.: GO / 86) — e não guarda nada disso no banco dele.

Só usa biblioteca padrão do Python.
"""
import csv
import datetime
import gzip
import io
import json
import os
import re
import subprocess
import sys
import urllib.request
import zipfile
from xml.etree import ElementTree

RAIZ = "https://arquivos.receitafederal.gov.br"
PASTA_CNPJ = "/Dados/Cadastros/CNPJ/"
MUNICIPIOS_COORD = "https://raw.githubusercontent.com/kelvins/municipios-brasileiros/main/csv/municipios.csv"

UF_POR_CODIGO_IBGE = {
    11: "RO", 12: "AC", 13: "AM", 14: "RR", 15: "PA", 16: "AP", 17: "TO", 21: "MA", 22: "PI",
    23: "CE", 24: "RN", 25: "PB", 26: "PE", 27: "AL", 28: "SE", 29: "BA", 31: "MG", 32: "ES",
    33: "RJ", 35: "SP", 41: "PR", 42: "SC", 43: "RS", 50: "MS", 51: "MT", 52: "GO", 53: "DF",
}
UFS = set(UF_POR_CODIGO_IBGE.values())

COLUNAS = ["cnpj", "razao", "fantasia", "cnae", "cnaes2", "logradouro", "numero", "complemento",
           "bairro", "cep", "municipio", "uf", "tel1", "tel2", "email", "inicio", "matriz"]

# O servidor da Receita derruba conexões que não parecem de navegador.
NAVEGADOR = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
TOKEN_CONHECIDO = "gn672Ad4CF8N6TK"

TRABALHO = os.environ.get("TRABALHO", "trabalho")
SAIDA = os.environ.get("SAIDA", "saida")


def log(*partes):
    print(datetime.datetime.now().strftime("%H:%M:%S"), *partes, flush=True)


# ------------------------------------------------------------ onde baixar


def _curl(url, *extras):
    """Pede via curl (com cara de navegador e tentativas) e devolve (código, corpo)."""
    comando = ["curl", "-sS", "-L", "--retry", "4", "--retry-delay", "8", "--retry-all-errors",
               "--max-time", "180", "-A", NAVEGADOR, "-w", "\n%{http_code} %{url_effective}", url] + list(extras)
    saida = subprocess.run(comando, capture_output=True, timeout=900)
    texto = saida.stdout.decode("utf-8", errors="replace")
    corpo, _, fim = texto.rpartition("\n")
    return fim, corpo


def descobrir_token():
    """O compartilhamento público da Receita tem um código que pode mudar.
    A página inicial redireciona pra ele: .../index.php/s/<código>."""
    if os.environ.get("RECEITA_TOKEN"):
        return os.environ["RECEITA_TOKEN"]
    try:
        fim, _ = _curl(RAIZ + "/", "-o", "/dev/null")
        achado = re.search(r"/s/([A-Za-z0-9]+)", fim)
        if achado:
            return achado.group(1)
        log("não achei o código na resposta (%s); usando o conhecido" % fim)
    except Exception as erro:  # noqa: BLE001
        log("falha ao descobrir o código (%s); usando o conhecido" % erro)
    return TOKEN_CONHECIDO


def _auth(token):
    import base64
    return "Basic " + base64.b64encode((token + ":").encode()).decode()


def listar(token, caminho):
    fim, xml = _curl(RAIZ + "/public.php/webdav" + urllib.request.quote(caminho), "-X", "PROPFIND",
                     "-H", "Depth: 1", "-u", token + ":")
    if not fim.startswith("207"):
        raise SystemExit("A Receita não listou %s (resposta %s)" % (caminho, fim))
    arvore = ElementTree.fromstring(xml.encode("utf-8"))
    ns = {"d": "DAV:"}
    itens = []
    for resp in arvore.findall("d:response", ns):
        href = urllib.request.unquote(resp.find("d:href", ns).text)
        tamanho = resp.find(".//d:getcontentlength", ns)
        itens.append((href.split("/public.php/webdav", 1)[-1], int(tamanho.text) if tamanho is not None else None))
    return itens


def mes_mais_recente(token):
    pastas = [c for c, _ in listar(token, PASTA_CNPJ) if re.search(r"/\d{4}-\d{2}/$", c)]
    if not pastas:
        raise SystemExit("Nenhuma pasta de mês na base da Receita.")
    return sorted(pastas)[-1]


def baixar(token, caminho, destino):
    """curl com retomada: os arquivos têm centenas de MB e a conexão da Receita cai."""
    url = RAIZ + "/public.php/webdav" + urllib.request.quote(caminho)
    for tentativa in range(6):
        codigo = subprocess.call(["curl", "-fsSL", "--retry", "5", "--retry-delay", "10", "--retry-all-errors",
                                  "-A", NAVEGADOR, "-C", "-", "-u", token + ":", "-o", destino, url])
        if codigo in (0, 33):  # 33 = servidor não aceita retomar, mas o arquivo já veio inteiro
            return
        log("download falhou (%s), tentando de novo: %s" % (codigo, caminho))
    raise SystemExit("Não consegui baixar %s" % caminho)


# ------------------------------------------------------------ leitura


def linhas_do_zip(caminho):
    with zipfile.ZipFile(caminho) as z:
        for nome in z.namelist():
            with z.open(nome) as bruto:
                texto = io.TextIOWrapper(bruto, encoding="latin-1", newline="")
                for linha in csv.reader(texto, delimiter=";", quotechar='"'):
                    yield linha


def _limpo(texto):
    return " ".join((texto or "").split())


def _telefone(ddd, numero):
    ddd = re.sub(r"\D", "", ddd or "")
    numero = re.sub(r"\D", "", numero or "")
    if not numero or len(numero) < 8:
        return ""
    return (ddd + numero) if ddd else numero


class Baldes:
    """Um arquivo por (UF, divisão). Guarda em memória e despeja de tempos em
    tempos, pra não precisar de milhares de arquivos abertos ao mesmo tempo."""

    def __init__(self, pasta):
        self.pasta = pasta
        self.buffer = {}
        self.contagem = {}
        self.guardadas = 0
        os.makedirs(pasta, exist_ok=True)

    def caminho(self, uf, div):
        return os.path.join(self.pasta, "%s_%s.csv.gz" % (uf, div))

    def por(self, uf, div, linha):
        chave = (uf, div)
        self.buffer.setdefault(chave, []).append(linha)
        self.contagem[chave] = self.contagem.get(chave, 0) + 1
        self.guardadas += 1
        if self.guardadas >= 300000:
            self.despejar()

    def despejar(self):
        for (uf, div), linhas in self.buffer.items():
            # gzip em modo "a" junta um bloco novo no fim; quem lê vê um arquivo só.
            with gzip.open(self.caminho(uf, div), "at", encoding="utf-8", newline="") as f:
                csv.writer(f, delimiter=";").writerows(linhas)
        self.buffer = {}
        self.guardadas = 0


class Razoes:
    """Razão social de quem está ativo, sem estourar a memória.

    O computador que roda isso pode ter pouca memória (2-3 GB), e são uns
    25 milhões de empresas. Então: os códigos que interessam vão pra um
    arquivo de números (4 bytes cada), ordenados com numpy; as razões
    encontradas vão pra um SQLite em disco, consultado no fim.
    """

    def __init__(self, pasta):
        import sqlite3
        self.pasta = pasta
        self.lista_caminho = os.path.join(pasta, "basicos.u32")
        self.lista = open(self.lista_caminho, "wb")
        self.buffer = []
        self.db_caminho = os.path.join(pasta, "razoes.db")
        if os.path.exists(self.db_caminho):
            os.remove(self.db_caminho)
        self.db = sqlite3.connect(self.db_caminho)
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute("CREATE TABLE r (b INTEGER PRIMARY KEY, razao TEXT)")
        self.ordenados = None

    def quero(self, basico):
        self.buffer.append(int(basico))
        if len(self.buffer) >= 500000:
            self._despejar()

    def _despejar(self):
        import array
        array.array("I", self.buffer).tofile(self.lista)
        self.buffer = []

    def fechar_lista(self):
        import numpy
        self._despejar()
        self.lista.close()
        self.ordenados = numpy.unique(numpy.fromfile(self.lista_caminho, dtype=numpy.uint32))
        os.remove(self.lista_caminho)
        log("empresas ativas distintas: %d" % len(self.ordenados))

    def guardar(self, pares):
        import numpy
        lote = []
        for basico, razao in pares:
            if basico.isdigit():
                lote.append((int(basico), razao))
            if len(lote) >= 500000:
                self._guardar_lote(numpy, lote)
                lote = []
        if lote:
            self._guardar_lote(numpy, lote)

    def _guardar_lote(self, numpy, lote):
        codigos = numpy.fromiter((b for b, _ in lote), dtype=numpy.uint32, count=len(lote))
        pos = numpy.searchsorted(self.ordenados, codigos)
        pos[pos >= len(self.ordenados)] = 0
        achou = self.ordenados[pos] == codigos
        self.db.executemany("INSERT OR REPLACE INTO r VALUES (?, ?)",
                            (lote[i] for i in numpy.nonzero(achou)[0]))
        self.db.commit()

    def quantas(self):
        return self.db.execute("SELECT COUNT(*) FROM r").fetchone()[0]

    def de(self, basico):
        linha = self.db.execute("SELECT razao FROM r WHERE b = ?", (int(basico),)).fetchone()
        return linha[0] if linha else ""

    def apagar(self):
        self.db.close()
        os.remove(self.db_caminho)


def processar(token, mes, baixar_arquivo=baixar):
    os.makedirs(TRABALHO, exist_ok=True)
    arquivos = listar(token, mes)
    estab = sorted(c for c, _ in arquivos if re.search(r"/Estabelecimentos\d+\.zip$", c))
    empresas = sorted(c for c, _ in arquivos if re.search(r"/Empresas\d+\.zip$", c))
    extras = {n: next((c for c, _ in arquivos if c.endswith("/%s.zip" % n)), None)
              for n in ("Cnaes", "Municipios")}
    if not estab or not empresas:
        raise SystemExit("Faltam arquivos na pasta %s" % mes)

    baldes = Baldes(os.path.join(TRABALHO, "baldes"))
    razoes = Razoes(TRABALHO)
    total = ativas = 0
    for caminho in estab:
        local = os.path.join(TRABALHO, os.path.basename(caminho))
        log("baixando", caminho)
        baixar_arquivo(token, caminho, local)
        log("lendo", local)
        for l in linhas_do_zip(local):
            total += 1
            if len(l) < 28 or l[5] != "02":  # 02 = ATIVA
                continue
            uf = (l[19] or "").strip().upper()
            cnae = (l[11] or "").strip()
            if uf not in UFS or len(cnae) < 7:
                continue
            ativas += 1
            razoes.quero(l[0])
            baldes.por(uf, cnae[:2], [
                l[0] + l[1] + l[2], "", _limpo(l[4]), cnae, (l[12] or "").strip(),
                _limpo(((l[13] or "") + " " + (l[14] or ""))), _limpo(l[15]), _limpo(l[16]),
                _limpo(l[17]), (l[18] or "").strip(), (l[20] or "").strip(), uf,
                _telefone(l[21], l[22]), _telefone(l[23], l[24]), (l[27] or "").strip().lower(),
                (l[10] or "").strip(), "1" if l[3] == "1" else "0",
            ])
        baldes.despejar()
        os.remove(local)
        log("até aqui: %d lidas, %d ativas" % (total, ativas))

    razoes.fechar_lista()
    for caminho in empresas:
        local = os.path.join(TRABALHO, os.path.basename(caminho))
        log("baixando", caminho)
        baixar_arquivo(token, caminho, local)
        razoes.guardar((l[0], _limpo(l[1])) for l in linhas_do_zip(local) if len(l) >= 2)
        os.remove(local)
        log("razões sociais: %d" % razoes.quantas())

    os.makedirs(SAIDA, exist_ok=True)
    meta = {"mes": mes.rstrip("/").rsplit("/", 1)[-1], "gerado_em": datetime.datetime.utcnow().isoformat() + "Z",
            "ativas": ativas, "ufs": {}}
    for (uf, div), n in sorted(baldes.contagem.items()):
        pasta = os.path.join(SAIDA, uf.lower())
        os.makedirs(pasta, exist_ok=True)
        with gzip.open(baldes.caminho(uf, div), "rt", encoding="utf-8", newline="") as ent, \
                gzip.open(os.path.join(pasta, "%s.csv.gz" % div), "wt", encoding="utf-8", newline="") as sai:
            escritor = csv.writer(sai, delimiter=";")
            escritor.writerow(COLUNAS)
            for linha in csv.reader(ent, delimiter=";"):
                linha[1] = razoes.de(linha[0][:8])
                escritor.writerow(linha)
        os.remove(baldes.caminho(uf, div))
        meta["ufs"].setdefault(uf, {})[div] = n
    razoes.apagar()

    base = os.path.join(SAIDA, "base")
    os.makedirs(base, exist_ok=True)
    if extras["Cnaes"]:
        local = os.path.join(TRABALHO, "Cnaes.zip")
        baixar_arquivo(token, extras["Cnaes"], local)
        with gzip.open(os.path.join(base, "cnaes.csv.gz"), "wt", encoding="utf-8", newline="") as sai:
            escritor = csv.writer(sai, delimiter=";")
            escritor.writerow(["codigo", "descricao"])
            for l in linhas_do_zip(local):
                if len(l) >= 2:
                    escritor.writerow([l[0].strip(), _limpo(l[1])])
    nomes = {}
    if extras["Municipios"]:
        local = os.path.join(TRABALHO, "Municipios.zip")
        baixar_arquivo(token, extras["Municipios"], local)
        for l in linhas_do_zip(local):
            if len(l) >= 2:
                nomes[l[0].strip().zfill(4)] = _limpo(l[1])
    municipios(nomes, os.path.join(base, "municipios.csv.gz"))
    with open(os.path.join(base, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    log("pronto: %d ativas em %d arquivos" % (ativas, len(baldes.contagem)))
    return meta


def municipios(nomes_receita, destino, fonte=None):
    """Código da Receita (SIAFI) -> nome, UF e coordenadas do centro da cidade."""
    if fonte is None:
        with urllib.request.urlopen(MUNICIPIOS_COORD, timeout=120) as r:
            fonte = r.read().decode("utf-8")
    with gzip.open(destino, "wt", encoding="utf-8", newline="") as sai:
        escritor = csv.writer(sai, delimiter=";")
        escritor.writerow(["codigo", "nome", "uf", "lat", "lon", "ibge"])
        for l in csv.DictReader(io.StringIO(fonte)):
            codigo = (l.get("siafi_id") or "").strip().zfill(4)
            uf = UF_POR_CODIGO_IBGE.get(int(l.get("codigo_uf") or 0))
            if not uf or not codigo.strip("0"):
                continue
            escritor.writerow([codigo, nomes_receita.get(codigo) or l["nome"].upper(), uf,
                               l["latitude"], l["longitude"], l["codigo_ibge"]])


REPO = os.environ.get("DADOS_REPO", "Maxfernandes28/comercia-dados")
NOTA = "Empresas ativas da base aberta da Receita Federal, separadas por atividade (divisão do CNAE). Gerado automaticamente."


def _gh(metodo, url, token, corpo=None, tipo="application/json"):
    import json as _json
    dados = None
    if corpo is not None:
        dados = corpo if isinstance(corpo, bytes) else _json.dumps(corpo).encode("utf-8")
    req = urllib.request.Request(url, data=dados, method=metodo, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": tipo, "User-Agent": "comercia-dados"})
    for tentativa in range(5):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                texto = r.read()
                return _json.loads(texto) if texto else {}
        except urllib.error.HTTPError as erro:
            if erro.code == 404:
                return None
            if erro.code in (500, 502, 503, 504) and tentativa < 4:
                import time
                time.sleep(10 * (tentativa + 1))
                continue
            raise SystemExit("GitHub respondeu %s em %s: %s" % (erro.code, url, erro.read()[:300]))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as erro:
            if tentativa < 4:
                import time
                time.sleep(10 * (tentativa + 1))
                continue
            raise SystemExit("Sem conexão com o GitHub: %s" % erro)


def publicar(token, saida=None):
    """Troca as releases pelas novas: apaga a do estado e cria de novo (uma
    chamada por arquivo), pra ficar dentro do limite de chamadas do GitHub."""
    import time
    saida = saida or SAIDA
    api = os.environ.get("GH_API", "https://api.github.com") + "/repos/" + REPO
    pastas = sorted(p for p in os.listdir(saida) if os.path.isdir(os.path.join(saida, p)))
    # "base" por último: o meta.json novo só aparece quando todo o resto já subiu.
    pastas = [p for p in pastas if p != "base"] + (["base"] if "base" in pastas else [])
    for tag in pastas:
        antiga = _gh("GET", api + "/releases/tags/" + tag, token)
        if antiga:
            _gh("DELETE", api + "/releases/%d" % antiga["id"], token)
        if _gh("GET", api + "/git/refs/tags/" + tag, token):
            _gh("DELETE", api + "/git/refs/tags/" + tag, token)
        nova = _gh("POST", api + "/releases", token, {"tag_name": tag, "name": tag, "body": NOTA,
                                                      "target_commitish": "main"})
        arquivos = sorted(os.listdir(os.path.join(saida, tag)), key=lambda n: (n == "meta.json", n))
        for nome in arquivos:
            with open(os.path.join(saida, tag, nome), "rb") as f:
                conteudo = f.read()
            tipo = "application/json" if nome.endswith(".json") else "application/gzip"
            url = "%s/repos/%s/releases/%d/assets?name=%s" % (os.environ.get("GH_UPLOADS", "https://uploads.github.com"),
                                                            REPO, nova["id"], urllib.request.quote(nome))
            _gh("POST", url, token, conteudo, tipo)
        log("publicado %s: %d arquivos" % (tag, len(arquivos)))
        time.sleep(1)


def _ler_token():
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"].strip()
    arquivo = os.environ.get("TOKEN_ARQUIVO")
    if arquivo and os.path.exists(arquivo):
        return open(arquivo, encoding="utf-8").read().strip()
    return None


if __name__ == "__main__":
    import argparse
    args = argparse.ArgumentParser()
    args.add_argument("pasta", nargs="?", help="pasta do mês na Receita (vazio = a mais recente)")
    args.add_argument("--publicar", action="store_true", help="publica no GitHub no fim")
    args.add_argument("--so-publicar", action="store_true", help="só publica o que já está em SAIDA")
    a = args.parse_args()
    if not a.so_publicar:
        token = descobrir_token()
        mes = a.pasta or mes_mais_recente(token)
        log("compartilhamento", token, "pasta", mes)
        processar(token, mes)
    if a.publicar or a.so_publicar:
        gh = _ler_token()
        if not gh:
            raise SystemExit("Falta a chave do GitHub (GITHUB_TOKEN ou TOKEN_ARQUIVO).")
        publicar(gh)
        log("tudo publicado")
