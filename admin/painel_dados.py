"""Dados do painel de licenças, sem interface (testável sem Tk).

Situação "efetiva" de cada licença (a que o app do cliente vai enxergar),
linhas da tabela, log de eventos e o git do repositório (o que falta publicar,
publicar e atualizar).
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import formato as f
from . import licencas as L
from .licencas import Repositorio

DIAS_AVISO = 7  # o app mostra a faixa de aviso a partir de 7 dias do vencimento

# código -> rótulo (ordem = ordem do filtro)
SITUACOES = {
    "ativa": "Ativa",
    "vencendo": "Vencendo",
    "tolerancia": "Em tolerância",
    "vencida": "Vencida",
    "pendente": "Pendente",
    "suspensa": "Suspensa",
    "revogada": "Revogada",
}

# status gravado no arquivo -> rótulo
STATUS_ROTULO = {f.Status.ATIVA.value: "Ativa", f.Status.SUSPENSA.value: "Suspensa",
                 f.Status.REVOGADA.value: "Revogada", f.Status.PENDENTE.value: "Pendente"}


def situacao(lic: dict, agora: datetime) -> str:
    """Código de SITUACOES, com a mesma regra de datas do app."""
    st = lic.get("status")
    if st == f.Status.REVOGADA.value:
        return "revogada"
    if st == f.Status.SUSPENSA.value:
        return "suspensa"
    if st == f.Status.PENDENTE.value:
        return "pendente"
    if not lic.get("expira_em"):
        return "ativa"
    expira = f.de_iso(lic["expira_em"])
    if agora <= expira:
        return "vencendo" if expira - agora <= timedelta(days=DIAS_AVISO) else "ativa"
    if agora <= expira + timedelta(days=lic.get("tolerancia_dias", 0)):
        return "tolerancia"
    return "vencida"


def dias_para_vencer(lic: dict, agora: datetime) -> int | None:
    if not lic.get("expira_em"):
        return None
    return (f.de_iso(lic["expira_em"]).date() - agora.date()).days


def data_br(iso: str | None) -> str:
    if not iso:
        return "—"
    return f.de_iso(iso).strftime("%d/%m/%Y")


def data_hora_br(iso: str | None) -> str:
    if not iso:
        return "—"
    return f.de_iso(iso).astimezone().strftime("%d/%m/%Y %H:%M")


@dataclass(frozen=True)
class Linha:
    id: str            # hash completo
    cliente: str
    final: str
    situacao: str      # código de SITUACOES
    vence: str         # dd/mm/aaaa
    dias: int | None   # dias até vencer (negativo = vencida)
    plano: str
    tenants: int
    dispositivos: str  # "1/2" ou "0/∞"

    @property
    def id_curto(self) -> str:
        return self.id[:10]


def linhas(repo: Repositorio, agora: datetime, busca: str = "",
           situacoes: set[str] | None = None) -> list[Linha]:
    """Linhas da tabela, filtradas e ordenadas por cliente."""
    busca = busca.strip().lower()
    saida = []
    for h, lic in repo.dados["licencas"].items():
        priv = repo.privado["licencas"].get(h, {})
        sit = situacao(lic, agora)
        if situacoes is not None and sit not in situacoes:
            continue
        cliente = priv.get("cliente") or "(sem cadastro)"
        if busca:
            alvo = " ".join([cliente, priv.get("contato", ""), priv.get("final", ""), h,
                             priv.get("observacoes", ""), *priv.get("tenants", [])]).lower()
            if busca not in alvo:
                continue
        maxd = lic.get("max_dispositivos", 0)
        saida.append(Linha(
            id=h, cliente=cliente, final=priv.get("final", "????"), situacao=sit,
            vence=data_br(lic.get("expira_em")), dias=dias_para_vencer(lic, agora),
            plano=lic.get("plano", ""), tenants=len(lic.get("tenants", [])),
            dispositivos=f"{len(lic.get('dispositivos', []))}/{maxd or '∞'}"))
    saida.sort(key=lambda l: (l.cliente.lower(), l.id))
    return saida


def contagem(repo: Repositorio, agora: datetime) -> dict[str, int]:
    c = {k: 0 for k in SITUACOES}
    for lic in repo.dados["licencas"].values():
        c[situacao(lic, agora)] += 1
    return c


ACOES = {
    "criar": "Licença criada", "renovar": "Renovada", "status": "Status alterado",
    "mensagem": "Mensagem alterada", "tenant-adicionar": "Tenant adicionado",
    "tenant-remover": "Tenant removido", "limite-dispositivos": "Limite de dispositivos",
    "dispositivo-adicionar": "Dispositivo autorizado",
    "dispositivo-remover": "Dispositivo removido", "cadastro": "Cadastro editado",
}


def eventos(repo: Repositorio, h: str | None = None, limite: int = 500) -> list[dict]:
    """Eventos do log privado, mais recentes primeiro (só da licença `h`, se dada)."""
    if not repo.arquivo_eventos.exists():
        return []
    saida = []
    for linha in repo.arquivo_eventos.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(linha)
        except ValueError:
            continue
        if h is None or (ev.get("licenca") and h.startswith(ev["licenca"])):
            saida.append(ev)
    return list(reversed(saida))[:limite]


def descrever_evento(ev: dict) -> str:
    texto = ACOES.get(ev.get("acao"), ev.get("acao", "?"))
    extra = {k: v for k, v in ev.items() if k not in ("quando", "acao", "licenca")}
    if "status" in extra:
        extra["status"] = STATUS_ROTULO.get(extra["status"], extra["status"])
    if "expira_em" in extra:
        extra["expira_em"] = data_br(extra["expira_em"])
    if extra:
        texto += " (" + ", ".join(f"{k}: {v}" for k, v in extra.items()) + ")"
    return texto


# ------------------------------------------------------------------- git
class ErroGit(Exception):
    pass


def _git(raiz: Path, *args: str, timeout: int = 60) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        r = subprocess.run(["git", *args], cwd=raiz, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=flags)
    except FileNotFoundError:
        raise ErroGit("git não encontrado no PATH.")
    except subprocess.TimeoutExpired:
        raise ErroGit(f"git {args[0]} demorou demais.")
    if r.returncode != 0:
        raise ErroGit((r.stderr or r.stdout).strip() or f"git {args[0]} falhou.")
    return r.stdout


@dataclass(frozen=True)
class EstadoGit:
    alterado: bool       # arquivo público com mudança ainda sem commit
    a_enviar: int        # commits ainda sem push
    erro: str = ""

    @property
    def pendente(self) -> bool:
        return self.alterado or self.a_enviar > 0


def estado_git(repo: Repositorio) -> EstadoGit:
    rel = repo.arquivo.relative_to(repo.raiz).as_posix()
    try:
        alterado = bool(_git(repo.raiz, "status", "--porcelain", "--", rel).strip())
        try:
            a_enviar = int(_git(repo.raiz, "rev-list", "--count", "@{u}..HEAD").strip() or 0)
        except ErroGit:
            a_enviar = 0  # sem upstream configurado
        return EstadoGit(alterado, a_enviar)
    except ErroGit as e:
        return EstadoGit(False, 0, str(e))


def publicar(repo: Repositorio, mensagem: str) -> str:
    """Commit do arquivo público (só ele) e push. Devolve um resumo."""
    rel = repo.arquivo.relative_to(repo.raiz).as_posix()
    if _git(repo.raiz, "status", "--porcelain", "--", rel).strip():
        _git(repo.raiz, "add", "--", rel)
        _git(repo.raiz, "commit", "-m", mensagem, "--", rel)
    _git(repo.raiz, "push", timeout=120)
    return f"Publicado (serial {repo.dados['serial']})."


def atualizar(repo: Repositorio) -> str:
    """git pull --ff-only (outra máquina pode ter publicado)."""
    saida = _git(repo.raiz, "pull", "--ff-only", timeout=120).strip()
    repo.recarregar()
    return saida.splitlines()[-1] if saida else "Atualizado."


def mensagem_commit(acoes: list[str]) -> str:
    if not acoes:
        return "Atualiza licenças"
    unicas = list(dict.fromkeys(acoes))
    if len(unicas) == 1:
        return unicas[0]
    return "Atualiza licenças: " + "; ".join(unicas)


# ------------------------------------------------------ campos de formulário
TIPOS_VALIDADE = ("Meses", "Dias", "Até a data")


def inteiro(texto: str, nome: str, minimo: int = 0) -> int:
    try:
        n = int(str(texto).strip())
    except ValueError:
        raise L.ErroAdmin(f"{nome}: use um número inteiro.")
    if n < minimo:
        raise L.ErroAdmin(f"{nome}: mínimo {minimo}.")
    return n


def converter_validade(tipo: str, texto: str) -> dict:
    """Campo "validade" do painel -> argumentos de Repositorio.criar/renovar."""
    texto = (texto or "").strip()
    if tipo == "Até a data":
        try:
            d = datetime.strptime(texto, "%d/%m/%Y")
        except ValueError:
            raise L.ErroAdmin("Data inválida: use dd/mm/aaaa.")
        return {"expira": d.strftime("%Y-%m-%d")}
    if tipo not in ("Meses", "Dias"):
        raise L.ErroAdmin("Escolha meses, dias ou uma data.")
    n = inteiro(texto, tipo, minimo=1)
    return {"meses": n} if tipo == "Meses" else {"dias": n}
