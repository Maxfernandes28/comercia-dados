"""Separa a base aberta de CNPJ da Receita por estado e atividade.

Roda uma vez por mês no GitHub Actions (ver .github/workflows/receita.yml).
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

TRABALHO = os.environ.get("TRABALHO", "trabalho")
SAIDA = os.environ.get("SAIDA", "saida")


def log(*partes):
    print(datetime.datetime.now().strftime("%H:%M:%S"), *partes, flush=True)


# ------------------------------------------------------------ onde baixar


def descobrir_token():
    """O compartilhamento público da Receita tem um código que pode mudar.
    A página inicial redireciona pra ele: .../index.php/s/<código>."""
    if os.environ.get("RECEITA_TOKEN"):
        return os.environ["RECEITA_TOKEN"]
    with urllib.request.urlopen(RAIZ + "/", timeout=60) as r:
        final = r.geturl()
    achado = re.search(r"/s/([A-Za-z0-9]+)", final)
    if not achado:
        raise SystemExit("Não achei o código do compartilhamento da Receita em %s" % final)
    return achado.group(1)


def _auth(token):
    import base64
    return "Basic " + base64.b64encode((token + ":").encode()).decode()


def listar(token, caminho):
    req = urllib.request.Request(RAIZ + "/public.php/webdav" + caminho, method="PROPFIND",
                                 headers={"Depth": "1", "Authorization": _auth(token)})
    with urllib.request.urlopen(req, timeout=120) as r:
        xml = r.read()
    arvore = ElementTree.fromstring(xml)
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
        codigo = subprocess.call(["curl", "-fsSL", "--retry", "5", "--retry-delay", "10",
                                  "-C", "-", "-u", token + ":", "-o", destino, url])
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
        return os.path.join(self.pasta, "%s_%s.csv" % (uf, div))

    def por(self, uf, div, linha):
        chave = (uf, div)
        self.buffer.setdefault(chave, []).append(linha)
        self.contagem[chave] = self.contagem.get(chave, 0) + 1
        self.guardadas += 1
        if self.guardadas >= 300000:
            self.despejar()

    def despejar(self):
        for (uf, div), linhas in self.buffer.items():
            with open(self.caminho(uf, div), "a", encoding="utf-8", newline="") as f:
                csv.writer(f, delimiter=";").writerows(linhas)
        self.buffer = {}
        self.guardadas = 0


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
    basicos = set()
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
            basicos.add(l[0])
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

    razoes = {}
    for caminho in empresas:
        local = os.path.join(TRABALHO, os.path.basename(caminho))
        log("baixando", caminho)
        baixar_arquivo(token, caminho, local)
        for l in linhas_do_zip(local):
            if l and l[0] in basicos:
                razoes[l[0]] = _limpo(l[1])
        os.remove(local)
        log("razões sociais: %d" % len(razoes))
    del basicos

    os.makedirs(SAIDA, exist_ok=True)
    meta = {"mes": mes.rstrip("/").rsplit("/", 1)[-1], "gerado_em": datetime.datetime.utcnow().isoformat() + "Z",
            "ativas": ativas, "ufs": {}}
    for (uf, div), n in sorted(baldes.contagem.items()):
        pasta = os.path.join(SAIDA, uf.lower())
        os.makedirs(pasta, exist_ok=True)
        with open(baldes.caminho(uf, div), encoding="utf-8", newline="") as ent, \
                gzip.open(os.path.join(pasta, "%s.csv.gz" % div), "wt", encoding="utf-8", newline="") as sai:
            escritor = csv.writer(sai, delimiter=";")
            escritor.writerow(COLUNAS)
            for linha in csv.reader(ent, delimiter=";"):
                linha[1] = razoes.get(linha[0][:8], "")
                escritor.writerow(linha)
        os.remove(baldes.caminho(uf, div))
        meta["ufs"].setdefault(uf, {})[div] = n
    del razoes

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


if __name__ == "__main__":
    token = descobrir_token()
    mes = sys.argv[1] if len(sys.argv) > 1 else mes_mais_recente(token)
    log("compartilhamento", token, "pasta", mes)
    processar(token, mes)
