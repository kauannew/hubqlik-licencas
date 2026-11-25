"""CLI administrativa das licenças (Hub Recargas - Data Priority e futuros produtos).

Exemplos (rode da raiz do repositório):

    python -m admin.licencas gerar-chaves
    python -m admin.licencas criar --cliente "ACME" --tenant acme.us.qlikcloud.com --meses 1
    python -m admin.licencas listar
    python -m admin.licencas renovar 3fa9c1 --meses 1
    python -m admin.licencas suspender 3fa9c1 --mensagem "Pagamento pendente"
    python -m admin.licencas dispositivo-adicionar 3fa9c1 ABCD-1A2B3-...
    python -m admin.licencas validar

Arquivos:
  produtos/<produto>.json   PÚBLICO (publicado no GitHub): só hashes e dados técnicos.
  chave_publica.txt         PÚBLICO: chave pública (embutida no app).
  privado/                  NÃO versionado: cadastro de clientes e log de eventos.
  chave privada             FORA do repositório (HUBQLIK_LIC_CHAVE_PRIVADA ou
                            ~/.hubqlik/<produto>_licencas_ed25519.json), cifrada
                            com senha (HUBQLIK_LIC_SENHA ou digitada).

Toda alteração incrementa `serial`, atualiza `emitido_em`, assina e confere a
assinatura antes de gravar.
"""
from __future__ import annotations

import argparse
import base64
import calendar
import getpass
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import nacl.pwhash
import nacl.secret
import nacl.utils
from nacl.exceptions import CryptoError
from nacl.signing import SigningKey, VerifyKey

from . import formato as f

RAIZ = Path(__file__).resolve().parents[1]
ENV_CHAVE = "HUBQLIK_LIC_CHAVE_PRIVADA"
ENV_SENHA = "HUBQLIK_LIC_SENHA"
PRODUTO_PADRAO = "hubqlik"


class ErroAdmin(Exception):
    """Erro de uso (mensagem para o operador)."""


# ------------------------------------------------------------ chave privada
def caminho_chave_padrao(produto: str) -> Path:
    if os.environ.get(ENV_CHAVE):
        return Path(os.environ[ENV_CHAVE])
    return Path.home() / ".hubqlik" / f"{produto}_licencas_ed25519.json"


def _kdf(senha: str, sal: bytes, ops: int, mem: int) -> bytes:
    return nacl.pwhash.argon2id.kdf(nacl.secret.SecretBox.KEY_SIZE, senha.encode("utf-8"),
                                    sal, opslimit=ops, memlimit=mem)


def salvar_chave_privada(sk: SigningKey, caminho: Path, senha: str | None,
                         ops: int = nacl.pwhash.argon2id.OPSLIMIT_MODERATE,
                         mem: int = nacl.pwhash.argon2id.MEMLIMIT_MODERATE) -> None:
    if caminho.exists():
        raise ErroAdmin(f"Já existe uma chave em {caminho}. Não sobrescrevo.")
    semente = bytes(sk)
    dados = {"formato": 1, "alg": f.ALG, "kid": f.kid(sk.verify_key)}
    if senha:
        sal = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
        caixa = nacl.secret.SecretBox(_kdf(senha, sal, ops, mem))
        dados.update(cifrada=True, sal=base64.b64encode(sal).decode(), ops=ops, mem=mem,
                     semente=base64.b64encode(caixa.encrypt(semente)).decode())
    else:
        dados.update(cifrada=False, semente=base64.b64encode(semente).decode())
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(dados, indent=2), encoding="utf-8")


def carregar_chave_privada(caminho: Path, senha: str | None = None) -> SigningKey:
    if not caminho.exists():
        raise ErroAdmin(f"Chave privada não encontrada em {caminho} "
                        f"(defina {ENV_CHAVE} ou rode gerar-chaves).")
    dados = json.loads(caminho.read_text(encoding="utf-8"))
    bruta = base64.b64decode(dados["semente"])
    if dados.get("cifrada"):
        if senha is None:
            senha = os.environ.get(ENV_SENHA) or getpass.getpass("Senha da chave privada: ")
        chave = _kdf(senha, base64.b64decode(dados["sal"]), dados["ops"], dados["mem"])
        try:
            bruta = nacl.secret.SecretBox(chave).decrypt(bruta)
        except CryptoError:
            raise ErroAdmin("Senha da chave privada incorreta.")
    sk = SigningKey(bruta)
    if f.kid(sk.verify_key) != dados.get("kid"):
        raise ErroAdmin("Arquivo da chave privada corrompido (kid não confere).")
    return sk


def ler_chaves_publicas(raiz: Path) -> list[VerifyKey]:
    """Chaves públicas de chave_publica.txt (linhas com # são comentários)."""
    arq = raiz / "chave_publica.txt"
    if not arq.exists():
        return []
    linhas = [l.strip() for l in arq.read_text(encoding="utf-8").splitlines()
              if l.strip() and not l.strip().startswith("#")]
    return [f.chave_publica_de_b64(l) for l in linhas]


# ------------------------------------------------------------------ datas
def somar_meses(d: datetime, meses: int) -> datetime:
    m = d.month - 1 + meses
    ano, mes = d.year + m // 12, m % 12 + 1
    dia = min(d.day, calendar.monthrange(ano, mes)[1])
    return d.replace(year=ano, month=mes, day=dia)


def fim_do_dia(data: str) -> datetime:
    try:
        d = datetime.strptime(data, "%Y-%m-%d")
    except ValueError:
        raise ErroAdmin(f"Data inválida: {data!r} (use AAAA-MM-DD)")
    return d.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)


# ------------------------------------------------------------ repositório
@dataclass
class Repositorio:
    """Arquivo público + cadastro privado de um produto.

    Sem `chave` (privada) o repositório abre só para leitura: confere a
    assinatura com as chaves de chave_publica.txt. Gravar exige `desbloquear`.
    """
    raiz: Path
    produto: str
    chave: SigningKey | None = None
    agora: Callable[[], datetime] = f.agora_utc
    dados: dict = field(init=False)
    privado: dict = field(init=False)

    @property
    def arquivo(self) -> Path:
        return self.raiz / "produtos" / f"{self.produto}.json"

    @property
    def pasta_privada(self) -> Path:
        return self.raiz / "privado"

    @property
    def arquivo_privado(self) -> Path:
        return self.pasta_privada / f"{self.produto}_clientes.json"

    @property
    def arquivo_eventos(self) -> Path:
        return self.pasta_privada / f"{self.produto}_eventos.jsonl"

    def __post_init__(self):
        self.recarregar()

    @property
    def desbloqueado(self) -> bool:
        return self.chave is not None

    def desbloquear(self, chave: SigningKey) -> None:
        """Passa a gravar com esta chave; recusa chave que o app não conhece."""
        publicas = ler_chaves_publicas(self.raiz)
        if publicas and f.kid(chave.verify_key) not in {f.kid(c) for c in publicas}:
            raise ErroAdmin("Esta chave privada não corresponde a chave_publica.txt.")
        self.chave = chave

    def bloquear(self) -> None:
        self.chave = None

    def recarregar(self) -> None:
        """Lê de novo o arquivo público e o cadastro privado do disco."""
        if self.arquivo.exists():
            chaves = ([self.chave.verify_key] if self.chave else [])                 + ler_chaves_publicas(self.raiz)
            self.dados = f.abrir_envelope(self.arquivo.read_bytes(), chaves, self.produto)
        else:
            self.dados = {"schema": f.SCHEMA, "produto": self.produto, "serial": 0,
                          "emitido_em": f.para_iso(self.agora()), "licencas": {}}
        if self.arquivo_privado.exists():
            self.privado = json.loads(self.arquivo_privado.read_text(encoding="utf-8"))
        else:
            self.privado = {"produto": self.produto, "licencas": {}}

    # ---- busca ----
    def resolver(self, ident: str) -> str:
        """Hash completo a partir de um prefixo do hash (>= 6) ou final:ABCD."""
        ident = (ident or "").strip()
        if ident.lower().startswith("final:"):
            fim = ident.split(":", 1)[1].upper()
            achados = [h for h, p in self.privado["licencas"].items() if p.get("final") == fim]
        else:
            if len(ident) < 6:
                raise ErroAdmin("Use pelo menos 6 caracteres do ID (veja 'listar').")
            achados = [h for h in self.dados["licencas"] if h.startswith(ident.lower())]
        if not achados:
            raise ErroAdmin(f"Licença não encontrada: {ident}")
        if len(achados) > 1:
            raise ErroAdmin(f"ID ambíguo ({len(achados)} licenças): use mais caracteres.")
        return achados[0]

    def lic(self, h: str) -> dict:
        return self.dados["licencas"][h]

    # ---- gravação ----
    def evento(self, acao: str, h: str | None, **extra) -> None:
        self.pasta_privada.mkdir(parents=True, exist_ok=True)
        reg = {"quando": f.para_iso(self.agora()), "acao": acao,
               "licenca": h[:12] if h else None, **extra}
        with self.arquivo_eventos.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(reg, ensure_ascii=False) + "\n")

    def salvar(self) -> None:
        if self.chave is None:
            raise ErroAdmin("Chave privada bloqueada: desbloqueie para gravar.")
        self.dados["serial"] += 1
        self.dados["emitido_em"] = f.para_iso(self.agora())
        conteudo = f.montar_envelope(self.dados, self.chave)
        # Confere antes de gravar: o que vai ao ar abre com a chave pública.
        conferido = f.abrir_envelope(conteudo, [self.chave.verify_key], self.produto)
        if conferido != self.dados:
            raise ErroAdmin("Conferência do arquivo assinado falhou; nada foi gravado.")
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.arquivo.with_suffix(".tmp")
        tmp.write_bytes(conteudo)
        os.replace(tmp, self.arquivo)
        self.salvar_privado()

    def salvar_privado(self) -> None:
        """Grava só o cadastro privado (não muda o arquivo público nem o serial)."""
        self.pasta_privada.mkdir(parents=True, exist_ok=True)
        tmp = self.arquivo_privado.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.privado, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.arquivo_privado)

    # ---- operações ----
    def _expiracao(self, base: datetime | None, expira: str | None, meses: int | None,
                   dias: int | None) -> str:
        if sum(x is not None for x in (expira, meses, dias)) != 1:
            raise ErroAdmin("Informe exatamente um entre --expira, --meses e --dias.")
        if expira:
            return f.para_iso(fim_do_dia(expira))
        base = max(base or self.agora(), self.agora())
        d = somar_meses(base, meses) if meses is not None else base + timedelta(days=dias)
        return f.para_iso(d.replace(hour=23, minute=59, second=59))

    def criar(self, cliente: str, tenants: list[str], plano: str = "mensal",
              expira: str | None = None, meses: int | None = None, dias: int | None = None,
              tolerancia: int = 3, max_dispositivos: int = 0, mensagem: str = "",
              contato: str = "", obs: str = "", pendente: bool = False) -> tuple[str, str]:
        """Cria a licença; devolve (chave em claro, hash). A chave não é guardada."""
        if tolerancia < 0 or max_dispositivos < 0:
            raise ErroAdmin("Tolerância e limite de dispositivos não podem ser negativos.")
        tenants_norm = [f.normalizar_tenant(t) for t in tenants]
        while True:
            chave = f.gerar_chave()
            h = f.hash_chave(chave, self.produto)
            if h not in self.dados["licencas"]:
                break
        status = f.Status.PENDENTE if pendente else f.Status.ATIVA
        self.dados["licencas"][h] = {
            "status": status.value,
            "plano": plano,
            "expira_em": self._expiracao(None, expira, meses, dias),
            "tolerancia_dias": tolerancia,
            "tenants": sorted({f.hash_tenant(t, self.produto) for t in tenants_norm}),
            "max_dispositivos": max_dispositivos,
            "dispositivos": [],
            "mensagem": mensagem,
        }
        self.privado["licencas"][h] = {
            "cliente": cliente, "contato": contato, "tenants": tenants_norm,
            "final": f.final_chave(chave), "observacoes": obs,
            "criada_em": f.para_iso(self.agora()), "dispositivos": {},
        }
        self.evento("criar", h, plano=plano, status=status.value)
        self.salvar()
        return chave, h

    def editar_cadastro(self, h: str, cliente: str | None = None, contato: str | None = None,
                        obs: str | None = None) -> None:
        """Dados só do cadastro privado: não precisa de chave nem de publicar."""
        self.lic(h)
        priv = self.privado["licencas"].setdefault(h, {})
        for campo, valor in (("cliente", cliente), ("contato", contato), ("observacoes", obs)):
            if valor is not None:
                priv[campo] = valor.strip()
        self.evento("cadastro", h)
        self.salvar_privado()

    def renovar(self, h: str, expira=None, meses=None, dias=None) -> str:
        lic = self.lic(h)
        atual = f.de_iso(lic["expira_em"]) if lic.get("expira_em") else None
        lic["expira_em"] = self._expiracao(atual, expira, meses, dias)
        self.evento("renovar", h, expira_em=lic["expira_em"])
        self.salvar()
        return lic["expira_em"]

    def mudar_status(self, h: str, status: f.Status, mensagem: str | None = None) -> None:
        lic = self.lic(h)
        if lic["status"] == f.Status.REVOGADA.value and status != f.Status.REVOGADA:
            raise ErroAdmin("Licença revogada não volta (crie outra).")
        lic["status"] = status.value
        if mensagem is not None:
            lic["mensagem"] = mensagem
        self.evento("status", h, status=status.value)
        self.salvar()

    def definir_mensagem(self, h: str, mensagem: str) -> None:
        self.lic(h)["mensagem"] = mensagem
        self.evento("mensagem", h)
        self.salvar()

    def tenant(self, h: str, url: str, adicionar: bool) -> None:
        lic, priv = self.lic(h), self.privado["licencas"].setdefault(h, {"tenants": []})
        norm = f.normalizar_tenant(url)
        th = f.hash_tenant(norm, self.produto)
        if adicionar:
            if th in lic["tenants"]:
                raise ErroAdmin("Tenant já está na licença.")
            lic["tenants"] = sorted(lic["tenants"] + [th])
            priv.setdefault("tenants", []).append(norm)
        else:
            if th not in lic["tenants"]:
                raise ErroAdmin("Tenant não está na licença.")
            lic["tenants"].remove(th)
            priv["tenants"] = [t for t in priv.get("tenants", []) if t != norm]
        self.evento("tenant-" + ("adicionar" if adicionar else "remover"), h)
        self.salvar()

    def limite_dispositivos(self, h: str, maximo: int) -> None:
        lic = self.lic(h)
        if maximo < 0:
            raise ErroAdmin("Limite não pode ser negativo.")
        if maximo and len(lic["dispositivos"]) > maximo:
            raise ErroAdmin(f"A licença já tem {len(lic['dispositivos'])} dispositivos; "
                            "remova alguns antes.")
        lic["max_dispositivos"] = maximo
        self.evento("limite-dispositivos", h, maximo=maximo)
        self.salvar()

    def dispositivo(self, h: str, codigo_ou_id: str, adicionar: bool, nome: str = "") -> str:
        lic = self.lic(h)
        priv = self.privado["licencas"].setdefault(h, {})
        if "-" in codigo_ou_id and len(codigo_ou_id.split("-")[0]) == 4:
            final, disp = f.ler_codigo_ativacao(codigo_ou_id)
            if priv.get("final") and final != priv["final"]:
                raise ErroAdmin("O código de ativação é de outra chave (final não confere).")
        else:
            disp = f.normalizar_dispositivo(codigo_ou_id)
        if adicionar:
            if not lic["max_dispositivos"]:
                raise ErroAdmin("Licença sem limite de dispositivos; defina 'limite' antes.")
            if disp in lic["dispositivos"]:
                raise ErroAdmin("Dispositivo já autorizado.")
            if len(lic["dispositivos"]) >= lic["max_dispositivos"]:
                raise ErroAdmin(f"Limite de {lic['max_dispositivos']} dispositivo(s) atingido.")
            lic["dispositivos"] = sorted(lic["dispositivos"] + [disp])
            priv.setdefault("dispositivos", {})[disp] = nome
        else:
            if disp not in lic["dispositivos"]:
                raise ErroAdmin("Dispositivo não está na licença.")
            lic["dispositivos"].remove(disp)
            priv.get("dispositivos", {}).pop(disp, None)
        self.evento("dispositivo-" + ("adicionar" if adicionar else "remover"), h,
                    dispositivo=disp[:6])
        self.salvar()
        return disp


# -------------------------------------------------------------------- CLI
def _repo(a, leitura: bool = False) -> Repositorio:
    if leitura:  # listar/mostrar: confere com a chave pública, sem pedir senha
        return Repositorio(Path(a.raiz), a.produto)
    sk = carregar_chave_privada(Path(a.chave) if a.chave else caminho_chave_padrao(a.produto))
    return Repositorio(Path(a.raiz), a.produto, sk)


def _validade(sp):
    g = sp.add_argument_group("validade (um dos três)")
    g.add_argument("--expira", help="data final AAAA-MM-DD (fim do dia, UTC)")
    g.add_argument("--meses", type=int)
    g.add_argument("--dias", type=int)


def _linha(h: str, lic: dict, priv: dict) -> str:
    return (f"{h[:10]}  {priv.get('final', '????')}  {lic['status']:<12} "
            f"{(lic.get('expira_em') or '-')[:10]}  {lic.get('plano', ''):<8} "
            f"tenants={len(lic['tenants'])} disp={len(lic['dispositivos'])}/"
            f"{lic['max_dispositivos'] or 'sem limite'}  {priv.get('cliente', '(sem cadastro)')}")


def montar_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="licencas", description="Licenças (CLI administrativa)")
    p.add_argument("--produto", default=PRODUTO_PADRAO)
    p.add_argument("--raiz", default=str(RAIZ), help="raiz do repositório de licenças")
    p.add_argument("--chave", help=f"arquivo da chave privada (padrão: {ENV_CHAVE} "
                                   "ou ~/.hubqlik/<produto>_licencas_ed25519.json)")
    s = p.add_subparsers(dest="cmd", required=True)

    sp = s.add_parser("gerar-chaves", help="gera o par Ed25519 (uma vez por produto)")
    sp.add_argument("--sem-senha", action="store_true", help="NÃO cifra a chave (não recomendado)")

    sp = s.add_parser("criar", help="cria licença e mostra a chave UMA vez")
    sp.add_argument("--cliente", required=True)
    sp.add_argument("--tenant", action="append", default=[], help="URL do tenant (repita)")
    sp.add_argument("--plano", default="mensal")
    _validade(sp)
    sp.add_argument("--tolerancia", type=int, default=3, help="dias após vencer (padrão 3)")
    sp.add_argument("--max-dispositivos", type=int, default=0, help="0 = sem limite")
    sp.add_argument("--mensagem", default="")
    sp.add_argument("--contato", default="")
    sp.add_argument("--obs", default="")
    sp.add_argument("--pendente", action="store_true", help="cria com status pending")

    sp = s.add_parser("renovar"); sp.add_argument("id"); _validade(sp)
    for nome in ("suspender", "revogar", "reativar"):
        sp = s.add_parser(nome); sp.add_argument("id"); sp.add_argument("--mensagem")
    sp = s.add_parser("mensagem"); sp.add_argument("id"); sp.add_argument("texto")
    for nome in ("tenant-adicionar", "tenant-remover"):
        sp = s.add_parser(nome); sp.add_argument("id"); sp.add_argument("url")
    sp = s.add_parser("limite", help="máximo de dispositivos (0 = sem limite)")
    sp.add_argument("id"); sp.add_argument("maximo", type=int)
    for nome in ("dispositivo-adicionar", "dispositivo-remover"):
        sp = s.add_parser(nome); sp.add_argument("id")
        sp.add_argument("codigo", help="código de ativação mostrado no app (ou ID de 20 hex)")
        sp.add_argument("--nome", default="", help="apelido do computador (só no cadastro privado)")
    s.add_parser("listar")
    sp = s.add_parser("mostrar"); sp.add_argument("id")
    sp = s.add_parser("validar", help="confere a assinatura do arquivo publicado "
                                      "(só com a chave pública; usado no GitHub Actions)")
    sp.add_argument("--arquivo")
    return p


def executar(argv: list[str], saida=print) -> int:
    a = montar_parser().parse_args(argv)
    raiz = Path(a.raiz)
    if a.cmd == "gerar-chaves":
        caminho = Path(a.chave) if a.chave else caminho_chave_padrao(a.produto)
        senha = None
        if not a.sem_senha:
            senha = os.environ.get(ENV_SENHA)
            if not senha:
                senha = getpass.getpass("Senha para cifrar a chave privada: ")
                if senha != getpass.getpass("Repita a senha: "):
                    raise ErroAdmin("As senhas não conferem.")
            if len(senha) < 12:
                raise ErroAdmin("Use uma senha com pelo menos 12 caracteres.")
        sk = SigningKey.generate()
        salvar_chave_privada(sk, caminho, senha)
        pub = f.chave_publica_b64(sk.verify_key)
        (raiz / "chave_publica.txt").write_text(
            f"# Chave pública Ed25519 do produto {a.produto} (kid {f.kid(sk.verify_key)})\n"
            f"{pub}\n", encoding="utf-8")
        saida(f"Chave privada: {caminho}  (faça BACKUP; não versione)")
        saida(f"kid: {f.kid(sk.verify_key)}")
        saida(f"Chave pública (embutir no app, hub_qlik/config.py): {pub}")
        return 0
    if a.cmd == "validar":
        arq = Path(a.arquivo) if a.arquivo else raiz / "produtos" / f"{a.produto}.json"
        dados = f.abrir_envelope(arq.read_bytes(), ler_chaves_publicas(raiz), a.produto)
        saida(f"OK: {arq.name} serial {dados['serial']}, emitido em {dados['emitido_em']}, "
              f"{len(dados['licencas'])} licença(s).")
        return 0

    r = _repo(a, leitura=a.cmd in ("listar", "mostrar"))
    if a.cmd == "criar":
        if not a.tenant:
            saida("AVISO: licença sem tenant vale para qualquer tenant.")
        chave, h = r.criar(a.cliente, a.tenant, a.plano, a.expira, a.meses, a.dias,
                           a.tolerancia, a.max_dispositivos, a.mensagem, a.contato, a.obs,
                           a.pendente)
        saida(f"Licença criada: {h[:10]}  (serial {r.dados['serial']})")
        saida(f"CHAVE (mostrada só agora, envie ao cliente): {chave}")
    elif a.cmd == "renovar":
        h = r.resolver(a.id)
        saida(f"{h[:10]} vence em {r.renovar(h, a.expira, a.meses, a.dias)}")
    elif a.cmd in ("suspender", "revogar", "reativar"):
        h = r.resolver(a.id)
        st = {"suspender": f.Status.SUSPENSA, "revogar": f.Status.REVOGADA,
              "reativar": f.Status.ATIVA}[a.cmd]
        # Reativar limpa a mensagem de bloqueio, salvo se outra for informada.
        msg = "" if (a.cmd == "reativar" and a.mensagem is None) else a.mensagem
        r.mudar_status(h, st, msg)
        saida(f"{h[:10]}: {st.value}")
    elif a.cmd == "mensagem":
        r.definir_mensagem(r.resolver(a.id), a.texto)
    elif a.cmd in ("tenant-adicionar", "tenant-remover"):
        r.tenant(r.resolver(a.id), a.url, a.cmd.endswith("adicionar"))
    elif a.cmd == "limite":
        r.limite_dispositivos(r.resolver(a.id), a.maximo)
    elif a.cmd in ("dispositivo-adicionar", "dispositivo-remover"):
        d = r.dispositivo(r.resolver(a.id), a.codigo, a.cmd.endswith("adicionar"), a.nome)
        saida(f"Dispositivo {d[:6]}… {'autorizado' if a.cmd.endswith('adicionar') else 'removido'}.")
    elif a.cmd == "listar":
        for h, lic in sorted(r.dados["licencas"].items(),
                             key=lambda x: r.privado["licencas"].get(x[0], {}).get("cliente", "")):
            saida(_linha(h, lic, r.privado["licencas"].get(h, {})))
        saida(f"serial {r.dados['serial']}, emitido em {r.dados['emitido_em']}")
        return 0
    elif a.cmd == "mostrar":
        h = r.resolver(a.id)
        saida(json.dumps({"id": h, "publico": r.lic(h),
                          "privado": r.privado["licencas"].get(h, {})},
                         ensure_ascii=False, indent=2))
        return 0
    if a.cmd not in ("listar", "mostrar"):
        saida(f"Gravado {r.arquivo.relative_to(raiz)} (serial {r.dados['serial']}). "
              "Faça commit e push para publicar.")
    return 0


def main() -> int:
    # Console do Windows (cp1252): não quebrar por causa de um caractere.
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(errors="replace")
        except AttributeError:
            pass
    try:
        return executar(sys.argv[1:])
    except (ErroAdmin, f.ErroFormato, f.ErroAssinatura, f.ErroChave) as e:
        print(f"ERRO: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
