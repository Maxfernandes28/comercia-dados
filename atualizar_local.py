"""Atualização da base rodando no computador do Max, em passos curtos.

A Receita só aceita download de quem está no Brasil, então o trabalho roda
no computador dele (não no GitHub). O ambiente de lá interrompe qualquer
comando depois de ~3 minutos, então este script faz o serviço em pedaços:
cada chamada trabalha até TEMPO segundos, grava onde parou e sai com
código 3 ("chame de novo"). Quando termina tudo, sai com 0.

    python3 atualizar_local.py        # repetir enquanto sair com 3

Códigos de saída: 0 pronto (ou já estava atualizado), 3 continua,
2 falta a chave do GitHub, 1 erro.

Estado em $TRABALHO/estado.json. Nada disso vai pra pasta do usuário.
Mesmo que o comando seja morto no meio, o próximo volta do último ponto
salvo (os arquivos são cortados de volta pro tamanho daquele ponto).
"""
import csv
import datetime
import gzip
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import zipfile

import processar as P

TEMPO = float(os.environ.get("TEMPO", "120"))      # quando parar de pegar trabalho novo
LIMITE = float(os.environ.get("LIMITE", "165"))    # o ambiente mata em ~180s
PONTO = int(os.environ.get("PONTO", "400000"))     # grava o ponto a cada N registros
INICIO = time.time()
TRAB = os.environ.get("TRABALHO", os.path.expanduser("~/receita"))
SAIDA = os.path.join(TRAB, "saida")
BALDES = os.path.join(TRAB, "baldes")
ESTADO = os.path.join(TRAB, "estado.json")
P.TRABALHO = TRAB
P.SAIDA = SAIDA


def passou():
    return time.time() - INICIO


def acabou_tempo():
    return passou() > TEMPO


def ler_estado():
    if os.path.exists(ESTADO):
        with open(ESTADO) as f:
            return json.load(f)
    return {"fase": "inicio"}


def gravar(estado):
    tmp = ESTADO + ".tmp"
    with open(tmp, "w") as f:
        json.dump(estado, f)
    os.replace(tmp, ESTADO)


def sair_continua(estado):
    gravar(estado)
    print("CONTINUA %s" % estado.get("progresso", ""), flush=True)
    sys.exit(3)


def tamanhos(pasta):
    return {n: os.path.getsize(os.path.join(pasta, n)) for n in os.listdir(pasta)} if os.path.isdir(pasta) else {}


def cortar(pasta, guardado, extras=()):
    """Volta os arquivos pro tamanho do último ponto salvo (e apaga os novos)."""
    for n in os.listdir(pasta) if os.path.isdir(pasta) else []:
        c = os.path.join(pasta, n)
        if n not in guardado:
            os.remove(c)
        elif os.path.getsize(c) != guardado[n]:
            with open(c, "r+b") as f:
                f.truncate(guardado[n])
    for caminho, tam in extras:
        if os.path.exists(caminho) and os.path.getsize(caminho) != tam:
            with open(caminho, "r+b") as f:
                f.truncate(tam)


def registros(caminho, pos=0):
    """Lê o CSV de dentro do zip a partir de `pos` (bytes já descompactados),
    devolvendo (registro, posição logo depois dele). Assim a retomada pula
    direto pro ponto certo sem precisar interpretar as linhas de antes."""
    with zipfile.ZipFile(caminho) as z:
        base = 0
        for info in z.infolist():
            if pos >= base + info.file_size:
                base += info.file_size
                continue
            with z.open(info) as bruto:
                falta = pos - base
                while falta > 0:
                    pedaco = bruto.read(min(falta, 1 << 24))
                    if not pedaco:
                        break
                    falta -= len(pedaco)
                agora = [max(pos, base)]

                def linhas():
                    for b in bruto:
                        agora[0] += len(b)
                        yield b.decode("latin-1")

                for reg in csv.reader(linhas(), delimiter=";", quotechar='"'):
                    yield reg, agora[0]
            base += info.file_size


def total_descompactado(caminho):
    with zipfile.ZipFile(caminho) as z:
        return sum(i.file_size for i in z.infolist())


def baixar_um(token, caminho, local):
    """Baixa com retomada, sem passar do tempo."""
    import subprocess
    url = P.RAIZ + "/public.php/webdav" + urllib.request.quote(caminho)
    resto = int(max(15, LIMITE - 10 - passou()))
    subprocess.call(["curl", "-fsSL", "-A", P.NAVEGADOR, "-C", "-", "-u", token + ":",
                     "--max-time", str(resto), "-o", local, url])


def limpar_trabalho():
    for n in os.listdir(TRAB):
        c = os.path.join(TRAB, n)
        if c == ESTADO:
            continue
        shutil.rmtree(c) if os.path.isdir(c) and not os.path.islink(c) else os.remove(c)


def main():
    os.makedirs(TRAB, exist_ok=True)
    e = ler_estado()

    if e["fase"] in ("inicio", "pronto"):
        token = P.descobrir_token()
        mes = os.environ.get("PASTA") or P.mes_mais_recente(token)
        if e["fase"] == "pronto" and e.get("mes") == mes:
            print("JA_ATUALIZADO mes=%s" % mes.rstrip("/").rsplit("/", 1)[-1], flush=True)
            sys.exit(0)
        limpar_trabalho()
        arquivos = P.listar(token, mes)
        tam = {c: t for c, t in arquivos}
        e = {"fase": "trabalhando", "token": token, "mes": mes,
             "estab": sorted(c for c in tam if re.search(r"/Estabelecimentos\d+\.zip$", c)),
             "empresas": sorted(c for c in tam if re.search(r"/Empresas\d+\.zip$", c)),
             "extras": sorted(c for c in tam if re.search(r"/(Cnaes|Municipios)\.zip$", c)),
             "tamanhos": tam, "i": 0, "pos": 0, "ativas": 0, "contagem": {},
             "arquivos_baldes": {}, "basicos_tam": 0}
        if not e["estab"] or not e["empresas"]:
            print("ERRO faltam arquivos na pasta %s" % mes, flush=True)
            sys.exit(1)
        gravar(e)
        print("pasta %s: %d arquivos" % (mes, len(tam)), flush=True)

    fila = e["estab"] + ["__unicos__"] + e["empresas"] + ["__saida__"] + e["extras"] + ["__base__", "__publicar__"]

    while e["i"] < len(fila):
        item = fila[e["i"]]
        if acabou_tempo():
            sair_continua(e)

        if item.endswith(".zip"):
            nome = os.path.basename(item)
            local = os.path.join(TRAB, nome)
            esperado = e["tamanhos"].get(item)
            if not (os.path.exists(local) and os.path.getsize(local) == esperado):
                baixar_um(e["token"], item, local)
                atual = os.path.getsize(local) if os.path.exists(local) else 0
                if atual != esperado:
                    e["progresso"] = "baixando %s (%d%%) [%d de %d]" % (
                        nome, 100 * atual // max(1, esperado or 1), e["i"] + 1, len(fila))
                    sair_continua(e)
                if acabou_tempo():
                    e["progresso"] = "baixado %s" % nome
                    sair_continua(e)
            if nome.startswith("Estabelecimentos"):
                processar_estab(e, local)
            elif nome.startswith("Empresas"):
                processar_empresas(e, local)
            if nome.startswith(("Estabelecimentos", "Empresas")):
                os.remove(local)
        elif item == "__unicos__":
            import numpy
            import sqlite3
            lista = os.path.join(TRAB, "basicos.u32")
            u = numpy.unique(numpy.fromfile(lista, dtype=numpy.uint32)) if os.path.exists(lista) \
                else numpy.array([], dtype=numpy.uint32)
            u.tofile(os.path.join(TRAB, "unicos.u32"))
            if os.path.exists(os.path.join(TRAB, "razoes.db")):
                os.remove(os.path.join(TRAB, "razoes.db"))
            db = sqlite3.connect(os.path.join(TRAB, "razoes.db"))
            db.execute("CREATE TABLE r (b INTEGER PRIMARY KEY, razao TEXT)")
            db.commit()
            db.close()
            if os.path.exists(lista):
                os.remove(lista)
            print("empresas ativas distintas: %d" % len(u), flush=True)
        elif item == "__saida__":
            if not montar_saida(e):
                sair_continua(e)
        elif item == "__base__":
            montar_base(e)
        elif item == "__publicar__":
            token = P._ler_token()
            if not token:
                print("FALTA_CHAVE", flush=True)
                gravar(e)
                sys.exit(2)
            if not publicar_em_passos(e, token):
                sair_continua(e)
        e["i"] += 1
        e["pos"] = 0
        e["progresso"] = ""
        gravar(e)

    resumo = "PRONTO mes=%s ativas=%d" % (e["mes"].rstrip("/").rsplit("/", 1)[-1], e["ativas"])
    limpar_trabalho()
    gravar({"fase": "pronto", "mes": e["mes"], "ativas": e["ativas"],
            "terminou_em": datetime.datetime.now().isoformat(timespec="seconds")})
    print(resumo, flush=True)
    sys.exit(0)


# ------------------------------------------------------------ etapas


def processar_estab(e, local):
    import array
    os.makedirs(BALDES, exist_ok=True)
    lista = os.path.join(TRAB, "basicos.u32")
    cortar(BALDES, e["arquivos_baldes"], [(lista, e["basicos_tam"])])
    baldes = P.Baldes(BALDES)
    baldes.contagem = {tuple(k.split("_")): v for k, v in e["contagem"].items()}
    basicos = []
    total = total_descompactado(local)
    ativas = e["ativas"]
    desde = 0
    pos = e["pos"]

    def salvar():
        baldes.despejar()
        with open(lista, "ab") as f:
            array.array("I", basicos).tofile(f)
        basicos.clear()
        e["arquivos_baldes"] = tamanhos(BALDES)
        e["basicos_tam"] = os.path.getsize(lista) if os.path.exists(lista) else 0
        e["contagem"] = {"%s_%s" % k: v for k, v in baldes.contagem.items()}
        e["ativas"] = ativas
        e["pos"] = pos
        e["progresso"] = "lendo %s (%d%%)" % (os.path.basename(local), 100 * pos // max(1, total))
        gravar(e)

    for l, pos in registros(local, e["pos"]):
        if len(l) >= 28 and l[5] == "02":
            uf = (l[19] or "").strip().upper()
            cnae = (l[11] or "").strip()
            if uf in P.UFS and len(cnae) >= 7 and l[0].isdigit():
                ativas += 1
                basicos.append(int(l[0]))
                baldes.por(uf, cnae[:2], [
                    l[0] + l[1] + l[2], "", P._limpo(l[4]), cnae, (l[12] or "").strip(),
                    P._limpo((l[13] or "") + " " + (l[14] or "")), P._limpo(l[15]), P._limpo(l[16]),
                    P._limpo(l[17]), (l[18] or "").strip(), (l[20] or "").strip(), uf,
                    P._telefone(l[21], l[22]), P._telefone(l[23], l[24]), (l[27] or "").strip().lower(),
                    (l[10] or "").strip(), "1" if l[3] == "1" else "0",
                ])
        desde += 1
        if desde >= PONTO:
            desde = 0
            salvar()
            if acabou_tempo():
                sair_continua(e)
    salvar()
    print("lido %s: %d ativas até aqui" % (os.path.basename(local), ativas), flush=True)


def processar_empresas(e, local):
    import numpy
    import sqlite3
    u = numpy.fromfile(os.path.join(TRAB, "unicos.u32"), dtype=numpy.uint32)
    db = sqlite3.connect(os.path.join(TRAB, "razoes.db"))
    db.execute("PRAGMA journal_mode=WAL")  # aguenta o comando ser morto no meio
    db.execute("PRAGMA synchronous=OFF")
    total = total_descompactado(local)
    lote = []
    pos = e["pos"]

    def gravar_lote():
        if lote and len(u):
            codigos = numpy.fromiter((b for b, _ in lote), dtype=numpy.uint32, count=len(lote))
            p = numpy.searchsorted(u, codigos)
            p[p >= len(u)] = 0
            achou = u[p] == codigos
            db.executemany("INSERT OR REPLACE INTO r VALUES (?, ?)", (lote[i] for i in numpy.nonzero(achou)[0]))
            db.commit()
        lote.clear()
        e["pos"] = pos
        e["progresso"] = "razões %s (%d%%)" % (os.path.basename(local), 100 * pos // max(1, total))
        gravar(e)

    for l, pos in registros(local, e["pos"]):
        if len(l) >= 2 and l[0].isdigit():
            lote.append((int(l[0]), P._limpo(l[1])))
        if len(lote) >= PONTO:
            gravar_lote()
            if acabou_tempo():
                db.close()
                sair_continua(e)
    gravar_lote()
    db.close()
    print("razões de %s ok" % os.path.basename(local), flush=True)


def montar_saida(e):
    """Junta a razão social em cada arquivo (UF, divisão). Um arquivo por vez."""
    import sqlite3
    db = sqlite3.connect(os.path.join(TRAB, "razoes.db"))
    feitos = set(e.get("saida_feitos", []))
    for chave in sorted(e["contagem"]):
        uf, div = chave.split("_")
        origem = os.path.join(BALDES, "%s_%s.csv.gz" % (uf, div))
        if chave in feitos:
            if os.path.exists(origem):
                os.remove(origem)
            continue
        if acabou_tempo():
            db.close()
            return False
        destino = os.path.join(SAIDA, uf.lower())
        os.makedirs(destino, exist_ok=True)
        with gzip.open(origem, "rt", encoding="utf-8", newline="") as ent, \
                gzip.open(os.path.join(destino, "%s.csv.gz" % div), "wt", encoding="utf-8", newline="") as sai:
            w = csv.writer(sai, delimiter=";")
            w.writerow(P.COLUNAS)
            for linha in csv.reader(ent, delimiter=";"):
                r = db.execute("SELECT razao FROM r WHERE b = ?", (int(linha[0][:8]),)).fetchone()
                linha[1] = r[0] if r else ""
                w.writerow(linha)
        feitos.add(chave)
        e["saida_feitos"] = sorted(feitos)
        e["progresso"] = "arquivos prontos: %d de %d" % (len(feitos), len(e["contagem"]))
        gravar(e)
        os.remove(origem)
    db.close()
    return True


def montar_base(e):
    base = os.path.join(SAIDA, "base")
    os.makedirs(base, exist_ok=True)
    cnaes = os.path.join(TRAB, "Cnaes.zip")
    if os.path.exists(cnaes):
        with gzip.open(os.path.join(base, "cnaes.csv.gz"), "wt", encoding="utf-8", newline="") as sai:
            w = csv.writer(sai, delimiter=";")
            w.writerow(["codigo", "descricao"])
            for l, _ in registros(cnaes):
                if len(l) >= 2:
                    w.writerow([l[0].strip(), P._limpo(l[1])])
    nomes = {}
    mun = os.path.join(TRAB, "Municipios.zip")
    if os.path.exists(mun):
        for l, _ in registros(mun):
            if len(l) >= 2:
                nomes[l[0].strip().zfill(4)] = P._limpo(l[1])
    P.municipios(nomes, os.path.join(base, "municipios.csv.gz"))
    meta = {"mes": e["mes"].rstrip("/").rsplit("/", 1)[-1],
            "gerado_em": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "ativas": e["ativas"], "ufs": {}}
    for chave, n in sorted(e["contagem"].items()):
        uf, div = chave.split("_")
        meta["ufs"].setdefault(uf, {})[div] = n
    with open(os.path.join(base, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    for n in ("Cnaes.zip", "Municipios.zip"):
        if os.path.exists(os.path.join(TRAB, n)):
            os.remove(os.path.join(TRAB, n))
    if os.path.exists(os.path.join(TRAB, "razoes.db")):
        os.remove(os.path.join(TRAB, "razoes.db"))
    for resto in ("razoes.db-wal", "razoes.db-shm", "unicos.u32"):
        if os.path.exists(os.path.join(TRAB, resto)):
            os.remove(os.path.join(TRAB, resto))


# ------------------------------------------------------------ publicar


def _api():
    return os.environ.get("GH_API", "https://api.github.com") + "/repos/" + P.REPO


def _uploads():
    return os.environ.get("GH_UPLOADS", "https://uploads.github.com") + "/repos/" + P.REPO


def publicar_em_passos(e, token):
    """Troca arquivo por arquivo dentro da release de cada estado (apaga o
    antigo e sobe o novo na hora), assim a busca nunca fica sem dados.
    A "base" (com o meta.json) vai por último."""
    pastas = sorted(p for p in os.listdir(SAIDA) if os.path.isdir(os.path.join(SAIDA, p)) and p != "base") + ["base"]
    pub = e.setdefault("pub", {})
    velocidade = e.get("velocidade", 300000.0)  # bytes/s, medida nos envios
    for tag in pastas:
        st = pub.setdefault(tag, {"feitos": [], "fim": False})
        if st["fim"]:
            continue
        if not st.get("id"):
            rel = P._gh("GET", _api() + "/releases/tags/" + tag, token)
            if not rel:
                rel = P._gh("POST", _api() + "/releases", token,
                            {"tag_name": tag, "name": tag, "body": P.NOTA, "target_commitish": "main"})
            st["id"] = rel["id"]
            gravar(e)
        remotos = {}
        pagina = 1
        while True:
            lote = P._gh("GET", _api() + "/releases/%d/assets?per_page=100&page=%d" % (st["id"], pagina), token) or []
            for a in lote:
                remotos[a["name"]] = a["id"]
            if len(lote) < 100:
                break
            pagina += 1
        locais = sorted(os.listdir(os.path.join(SAIDA, tag)), key=lambda n: (n == "meta.json", n))
        for nome in locais:
            if nome in st["feitos"]:
                continue
            caminho = os.path.join(SAIDA, tag, nome)
            tam = os.path.getsize(caminho)
            if passou() + 10 + tam / velocidade * 1.5 > LIMITE and passou() > 5:
                e["velocidade"] = velocidade
                e["progresso"] = "publicando %s (%d de %d estados)" % (
                    tag, sum(1 for s in pub.values() if s.get("fim")), len(pastas))
                return False
            if nome in remotos:
                P._gh("DELETE", _api() + "/releases/assets/%d" % remotos[nome], token)
            with open(caminho, "rb") as f:
                conteudo = f.read()
            tipo = "application/json" if nome.endswith(".json") else "application/gzip"
            t0 = time.time()
            P._gh("POST", "%s/releases/%d/assets?name=%s" % (_uploads(), st["id"], urllib.request.quote(nome)),
                  token, conteudo, tipo)
            if tam > 200000:
                velocidade = max(20000.0, 0.5 * velocidade + 0.5 * tam / max(0.05, time.time() - t0))
            st["feitos"].append(nome)
            e["velocidade"] = velocidade
            gravar(e)
        for nome, ident in remotos.items():
            if nome not in locais:
                P._gh("DELETE", _api() + "/releases/assets/%d" % ident, token)
        st["fim"] = True
        shutil.rmtree(os.path.join(SAIDA, tag), ignore_errors=True) if tag != "base" else None
        e["progresso"] = "publicado %s" % tag
        gravar(e)
        print("publicado %s: %d arquivos" % (tag, len(locais)), flush=True)
        if acabou_tempo():
            return False
    return True


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as erro:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("ERRO %s" % erro, flush=True)
        sys.exit(1)
