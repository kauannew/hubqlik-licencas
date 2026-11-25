"""Painel de licenças: interface gráfica para a CLI administrativa.

    pyw -3.12 -m admin.painel        (ou abra o painel.bat)

Faz o mesmo que `admin.licencas` (as regras e a gravação ficam lá):
- lista e busca as licenças, com a situação que o app do cliente vai ver;
- cria, renova, suspende, reativa, revoga, muda mensagem, tenants e dispositivos;
- publica (commit + push só do arquivo público) e atualiza (git pull).

Abre só para leitura, sem senha. A chave privada é pedida na primeira
alteração, fica só na memória e é esquecida em "Bloquear", ao fechar ou
após 15 min sem alterações.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

import customtkinter as ctk

from . import formato as f
from . import licencas as L
from . import painel_dados as D

TITULO = "Licenças — Hub Recargas - Data Priority"
BLOQUEIO_MIN = 15  # esquece a chave privada após este tempo sem alterações

# Cores (as mesmas do Hub)
BG = "#0c110f"
SURFACE = "#131a17"
SURFACE_2 = "#19221e"
SURFACE_HOVER = "#1e2924"
BORDA = "#223029"
TEXTO = "#e6efe9"
TEXTO_2 = "#94a69d"
TEXTO_3 = "#5c6c64"
VERDE = "#22c55e"
VERDE_HOVER = "#16a34a"
VERDE_SUAVE = "#12291c"
VERMELHO = "#ef4444"
VERMELHO_HOVER = "#b91c1c"
VERMELHO_SUAVE = "#2e1416"
AMBAR = "#f59e0b"
AMBAR_SUAVE = "#2e230c"
CINZA = "#6b7280"
CINZA_SUAVE = "#1f2427"

COR_SITUACAO = {
    "ativa": (VERDE, VERDE_SUAVE), "vencendo": (AMBAR, AMBAR_SUAVE),
    "tolerancia": (AMBAR, AMBAR_SUAVE), "vencida": (VERMELHO, VERMELHO_SUAVE),
    "pendente": (CINZA, CINZA_SUAVE), "suspensa": (VERMELHO, VERMELHO_SUAVE),
    "revogada": (CINZA, CINZA_SUAVE),
}

# Filtros da tabela: rótulo -> situações
FILTROS = {
    "Todas": None,
    "Ativas": {"ativa"},
    "Atenção": {"vencendo", "tolerancia", "pendente"},
    "Bloqueadas": {"vencida", "suspensa", "revogada"},
}

ERROS_DE_USO = (L.ErroAdmin, ValueError)  # ErroFormato/ErroChave/ErroAssinatura são ValueError


def fonte(tamanho: int = 13, peso: str = "normal", mono: bool = False) -> ctk.CTkFont:
    return ctk.CTkFont(family="Consolas" if mono else None, size=tamanho, weight=peso)


def botao(pai, texto: str, comando, tipo: str = "secundario", **kw) -> ctk.CTkButton:
    cores = {
        "primario": dict(fg_color=VERDE, hover_color=VERDE_HOVER, text_color="#04130a"),
        "perigo": dict(fg_color=VERMELHO_SUAVE, hover_color=VERMELHO_HOVER,
                       text_color=VERMELHO, border_width=1, border_color=VERMELHO),
        "secundario": dict(fg_color=SURFACE_2, hover_color=SURFACE_HOVER, text_color=TEXTO,
                           border_width=1, border_color=BORDA),
        "link": dict(fg_color="transparent", hover_color=SURFACE_HOVER, text_color=VERDE),
    }[tipo]
    kw.setdefault("height", 32)
    kw.setdefault("corner_radius", 8)
    kw.setdefault("font", fonte(13, "bold" if tipo == "primario" else "normal"))
    return ctk.CTkButton(pai, text=texto, command=comando, **cores, **kw)


# ------------------------------------------------------------- formulários
@dataclass
class Campo:
    nome: str
    rotulo: str
    tipo: str = "texto"  # texto | senha | multilinha | opcoes | validade | marcar
    valor: object = ""
    opcoes: tuple = ()
    dica: str = ""


class Formulario(ctk.CTkToplevel):
    """Diálogo modal. `ao_confirmar(valores)` grava; se der erro, a mensagem
    aparece no próprio diálogo e ele continua aberto."""

    def __init__(self, pai, titulo: str, campos: list[Campo],
                 ao_confirmar: Callable[[dict], None], texto_ok: str = "Salvar",
                 intro: str = "", perigo: bool = False, colunas: int = 1):
        super().__init__(pai)
        self.title(titulo)
        self.configure(fg_color=BG)
        self.resizable(False, False)
        self.transient(pai)
        self.ok = False
        self._ao_confirmar = ao_confirmar
        self._campos: dict[str, tuple[Campo, object]] = {}
        self._largura = 440 if colunas == 1 else 320

        corpo = ctk.CTkFrame(self, fg_color="transparent")
        corpo.pack(padx=26, pady=(22, 8), fill="both", expand=True)
        ctk.CTkLabel(corpo, text=titulo, font=fonte(18, "bold"), text_color=TEXTO
                     ).pack(anchor="w")
        largura_total = self._largura * colunas + 24 * (colunas - 1)
        if intro:
            ctk.CTkLabel(corpo, text=intro, font=fonte(12), text_color=TEXTO_2,
                         wraplength=largura_total, justify="left").pack(anchor="w", pady=(4, 0))
        grade = ctk.CTkFrame(corpo, fg_color="transparent")
        grade.pack(anchor="w")
        primeiro = None
        for i, c in enumerate(campos):
            celula = ctk.CTkFrame(grade, fg_color="transparent")
            celula.grid(row=i // colunas, column=i % colunas, sticky="nw",
                        padx=(0 if i % colunas == 0 else 24, 0))
            w = self._criar(celula, c)
            primeiro = primeiro or w
        self._erro = ctk.CTkLabel(corpo, text="", font=fonte(12), text_color=VERMELHO,
                                  wraplength=largura_total, justify="left")
        self._erro.pack(anchor="w", pady=(10, 0))

        rodape = ctk.CTkFrame(self, fg_color="transparent")
        rodape.pack(fill="x", padx=26, pady=(4, 20))
        botao(rodape, texto_ok, self._confirmar, "perigo" if perigo else "primario",
              width=130).pack(side="right")
        botao(rodape, "Cancelar", self.destroy, width=100).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda e: self.destroy())
        self._primeiro = primeiro
        self.after(120, self._focar)

    def _criar(self, corpo, c: Campo):
        if c.tipo == "marcar":
            var = tk.BooleanVar(value=bool(c.valor))
            ctk.CTkCheckBox(corpo, text=c.rotulo, variable=var, font=fonte(13),
                            fg_color=VERDE, hover_color=VERDE_HOVER).pack(anchor="w", pady=(14, 0))
            self._campos[c.nome] = (c, var)
            if c.dica:
                self._dica(corpo, c.dica)
            return None
        ctk.CTkLabel(corpo, text=c.rotulo, font=fonte(12, "bold"), text_color=TEXTO_2
                     ).pack(anchor="w", pady=(14, 4))
        comum = dict(fg_color=SURFACE, border_color=BORDA, text_color=TEXTO)
        if c.tipo == "multilinha":
            w = ctk.CTkTextbox(corpo, width=self._largura, height=74, border_width=1, **comum)
            w.insert("1.0", str(c.valor or ""))
        elif c.tipo == "opcoes":
            w = ctk.CTkComboBox(corpo, values=list(c.opcoes), width=self._largura, button_color=BORDA,
                                dropdown_fg_color=SURFACE, **comum)
            w.set(str(c.valor))
        elif c.tipo == "validade":
            tipo, valor = c.valor or ("Meses", "1")
            w = ctk.CTkFrame(corpo, fg_color="transparent")
            tipo_w = ctk.CTkSegmentedButton(w, values=list(D.TIPOS_VALIDADE),
                                            selected_color=VERDE_SUAVE,
                                            selected_hover_color=VERDE_SUAVE,
                                            unselected_color=SURFACE, fg_color=SURFACE,
                                            text_color=TEXTO)
            tipo_w.set(tipo)
            tipo_w.pack(side="left")
            valor_w = ctk.CTkEntry(w, width=self._largura - 200, border_width=1, **comum)
            valor_w.insert(0, valor)
            valor_w.pack(side="left", padx=(10, 0))
            valor_w.bind("<Return>", lambda e: self._confirmar())
            w.pack(anchor="w")
            self._campos[c.nome] = (c, (tipo_w, valor_w))
            if c.dica:
                self._dica(corpo, c.dica)
            return valor_w
        else:
            w = ctk.CTkEntry(corpo, width=self._largura, border_width=1,
                             show="•" if c.tipo == "senha" else "", **comum)
            w.insert(0, str(c.valor or ""))
            w.bind("<Return>", lambda e: self._confirmar())
        w.pack(anchor="w")
        self._campos[c.nome] = (c, w)
        if c.dica:
            self._dica(corpo, c.dica)
        return w

    def _dica(self, corpo, texto):
        ctk.CTkLabel(corpo, text=texto, font=fonte(11), text_color=TEXTO_3,
                     wraplength=self._largura,
                     justify="left").pack(anchor="w", pady=(3, 0))

    def _focar(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
            if self._primeiro is not None:
                self._primeiro.focus_set()
        except tk.TclError:
            pass

    def valores(self) -> dict:
        v = {}
        for nome, (c, w) in self._campos.items():
            if c.tipo == "marcar":
                v[nome] = w.get()
            elif c.tipo == "multilinha":
                v[nome] = w.get("1.0", "end").strip()
            elif c.tipo == "validade":
                v[nome] = (w[0].get(), w[1].get())
            else:
                v[nome] = w.get().strip() if c.tipo != "senha" else w.get()
        return v

    def _confirmar(self):
        self._erro.configure(text="")
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            self._ao_confirmar(self.valores())
        except ERROS_DE_USO as e:
            self._erro.configure(text=str(e))
            return
        finally:
            if self.winfo_exists():
                self.configure(cursor="")
        self.ok = True
        self.destroy()

    def executar(self) -> bool:
        self.wait_window()
        return self.ok


class ChaveCriada(ctk.CTkToplevel):
    """Mostra a chave nova uma única vez."""

    def __init__(self, pai, chave: str, cliente: str):
        super().__init__(pai)
        self.title("Licença criada")
        self.configure(fg_color=BG)
        self.resizable(False, False)
        self.transient(pai)
        self._chave = chave
        self._copiou = False
        c = ctk.CTkFrame(self, fg_color="transparent")
        c.pack(padx=28, pady=24)
        ctk.CTkLabel(c, text="Licença criada", font=fonte(18, "bold"), text_color=TEXTO
                     ).pack(anchor="w")
        ctk.CTkLabel(c, text=f"Chave de {cliente}. Ela aparece só agora: só o hash fica "
                             "gravado. Copie e envie ao cliente por um canal seguro.",
                     font=fonte(12), text_color=TEXTO_2, wraplength=460, justify="left"
                     ).pack(anchor="w", pady=(4, 14))
        caixa = ctk.CTkFrame(c, fg_color=SURFACE, border_color=VERDE, border_width=1,
                             corner_radius=10)
        caixa.pack(fill="x")
        ctk.CTkLabel(caixa, text=chave, font=fonte(20, "bold", mono=True), text_color=VERDE
                     ).pack(padx=20, pady=16)
        ctk.CTkLabel(c, text="Lembre de publicar: a licença só vale depois do "
                             "\"Publicar\" (commit + push).",
                     font=fonte(11), text_color=AMBAR, wraplength=460, justify="left"
                     ).pack(anchor="w", pady=(12, 0))
        rod = ctk.CTkFrame(c, fg_color="transparent")
        rod.pack(fill="x", pady=(16, 0))
        botao(rod, "Fechar", self._fechar, width=100).pack(side="right")
        self._b_copiar = botao(rod, "Copiar chave", self._copiar, "primario", width=150)
        self._b_copiar.pack(side="right", padx=(0, 8))
        self.protocol("WM_DELETE_WINDOW", self._fechar)
        self.after(120, lambda: (self.lift(), self.focus_force(), self.grab_set()))

    def _copiar(self):
        self.clipboard_clear()
        self.clipboard_append(self._chave)
        self._copiou = True
        self._b_copiar.configure(text="Copiada ✓")

    def _fechar(self):
        if not self._copiou and not messagebox.askyesno(
                "Fechar", "Você ainda não copiou a chave. Ela não poderá ser vista de novo.\n\n"
                          "Fechar mesmo assim?", icon="warning", default="no", parent=self):
            return
        self.destroy()


class JanelaEventos(ctk.CTkToplevel):
    def __init__(self, pai, repo: L.Repositorio):
        super().__init__(pai)
        self.title("Histórico de alterações")
        self.configure(fg_color=BG)
        self.geometry("820x480")
        self.transient(pai)
        clientes = {h[:12]: p.get("cliente", "") for h, p in repo.privado["licencas"].items()}
        finais = {h[:12]: p.get("final", "") for h, p in repo.privado["licencas"].items()}
        ctk.CTkLabel(self, text="Histórico de alterações (cadastro privado)",
                     font=fonte(16, "bold"), text_color=TEXTO).pack(anchor="w", padx=18,
                                                                    pady=(16, 8))
        quadro = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=10)
        quadro.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        arvore = ttk.Treeview(quadro, columns=("quando", "cliente", "acao"), show="headings",
                              style="Painel.Treeview")
        for col, tit, larg in (("quando", "Quando", 140), ("cliente", "Licença", 220),
                               ("acao", "Alteração", 420)):
            arvore.heading(col, text=tit, anchor="w")
            arvore.column(col, width=larg, anchor="w", stretch=col == "acao")
        for ev in D.eventos(repo):
            lic = ev.get("licenca") or ""
            nome = f"{clientes.get(lic, '')} ({finais.get(lic, '') or lic[:6]})" if lic else "—"
            arvore.insert("", "end", values=(D.data_hora_br(ev.get("quando")), nome,
                                             D.descrever_evento(ev)))
        barra = ctk.CTkScrollbar(quadro, command=arvore.yview)
        arvore.configure(yscrollcommand=barra.set)
        barra.pack(side="right", fill="y", padx=(0, 4), pady=4)
        arvore.pack(fill="both", expand=True, padx=6, pady=6)
        self.after(120, lambda: (self.lift(), self.focus_force()))


# ------------------------------------------------------------------ painel
class Painel(ctk.CTk):
    def __init__(self, raiz: Path = L.RAIZ, produto: str = L.PRODUTO_PADRAO,
                 caminho_chave: Path | None = None, agora=f.agora_utc):
        super().__init__()
        self.title(TITULO)
        self.configure(fg_color=BG)
        self.geometry("1280x760")
        self.minsize(1040, 600)
        self._agora = agora
        self._caminho_chave = caminho_chave or L.caminho_chave_padrao(produto)
        self.repo = L.Repositorio(Path(raiz), produto)
        self.selecionado: str | None = None
        self.acoes: list[str] = []      # descrições para a mensagem do commit
        self._ultimo_uso = 0.0
        self._fila: queue.Queue = queue.Queue()
        self._ocupado = False
        self._git = D.EstadoGit(False, 0)

        self._estilo_tabela()
        self._montar()
        self.protocol("WM_DELETE_WINDOW", self._sair)
        self.bind("<Control-f>", lambda e: self._busca.focus_set())
        self.bind("<F5>", lambda e: self._atualizar_do_github())
        self.after(100, self._vigiar_fila)
        self.after(30_000, self._vigiar_bloqueio)
        self.recarregar_tela()

    # ---------------------------------------------------------- montagem
    def _estilo_tabela(self):
        try:
            escala = self._get_window_scaling()
        except Exception:
            escala = 1.0
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Painel.Treeview", background=SURFACE, fieldbackground=SURFACE,
                    foreground=TEXTO, bordercolor=SURFACE, borderwidth=0,
                    rowheight=int(34 * escala), font=("Segoe UI", 10))
        s.configure("Painel.Treeview.Heading", background=SURFACE_2, foreground=TEXTO_2,
                    bordercolor=BORDA, relief="flat", font=("Segoe UI", 9, "bold"),
                    padding=(8, 6))
        s.map("Painel.Treeview.Heading", background=[("active", SURFACE_HOVER)])
        s.map("Painel.Treeview", background=[("selected", VERDE_SUAVE)],
              foreground=[("selected", TEXTO)])
        s.layout("Painel.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    def _montar(self):
        # topo
        topo = ctk.CTkFrame(self, fg_color="transparent")
        topo.pack(fill="x", padx=22, pady=(18, 10))
        tit = ctk.CTkFrame(topo, fg_color="transparent")
        tit.pack(side="left")
        ctk.CTkLabel(tit, text="Licenças", font=fonte(24, "bold"), text_color=TEXTO
                     ).pack(anchor="w")
        self._subtitulo = ctk.CTkLabel(tit, text="", font=fonte(12), text_color=TEXTO_2)
        self._subtitulo.pack(anchor="w")

        acoes = ctk.CTkFrame(topo, fg_color="transparent")
        acoes.pack(side="right")
        botao(acoes, "+ Nova licença", self.nova_licenca, "primario", width=140
              ).pack(side="right")
        self._b_publicar = botao(acoes, "Publicar", self.publicar, width=110)
        self._b_publicar.pack(side="right", padx=(0, 8))
        self._b_atualizar = botao(acoes, "Atualizar do GitHub", self._atualizar_do_github,
                                  width=160)
        self._b_atualizar.pack(side="right", padx=(0, 8))
        botao(acoes, "Histórico", lambda: JanelaEventos(self, self.repo), width=100
              ).pack(side="right", padx=(0, 8))
        self._b_cadeado = botao(acoes, "", self._alternar_cadeado, width=150)
        self._b_cadeado.pack(side="right", padx=(0, 16))

        # faixa "falta publicar"
        self._faixa = ctk.CTkFrame(self, fg_color=AMBAR_SUAVE, corner_radius=10,
                                   border_color=AMBAR, border_width=1)
        self._faixa_texto = ctk.CTkLabel(self._faixa, text="", font=fonte(13),
                                         text_color=AMBAR, justify="left")
        self._faixa_texto.pack(side="left", padx=16, pady=10)
        botao(self._faixa, "Publicar agora", self.publicar, "primario", width=140
              ).pack(side="right", padx=10, pady=8)

        # corpo
        self._corpo = ctk.CTkFrame(self, fg_color="transparent")
        self._corpo.pack(fill="both", expand=True, padx=22, pady=(0, 8))
        self._corpo.grid_columnconfigure(0, weight=1)
        self._corpo.grid_columnconfigure(1, weight=0, minsize=430)
        self._corpo.grid_rowconfigure(0, weight=1)

        esq = ctk.CTkFrame(self._corpo, fg_color="transparent")
        esq.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        filtro = ctk.CTkFrame(esq, fg_color="transparent")
        filtro.pack(fill="x", pady=(0, 10))
        self._busca = ctk.CTkEntry(filtro, width=300, height=34,
                                   placeholder_text="Buscar cliente, contato, tenant, final…",
                                   fg_color=SURFACE, border_color=BORDA, text_color=TEXTO)
        self._busca.pack(side="left")
        self._busca.bind("<KeyRelease>", lambda e: self._preencher_tabela())
        self._filtro = ctk.CTkSegmentedButton(
            filtro, values=list(FILTROS), command=lambda v: self._preencher_tabela(),
            selected_color=VERDE_SUAVE, selected_hover_color=VERDE_SUAVE,
            unselected_color=SURFACE, unselected_hover_color=SURFACE_HOVER,
            fg_color=SURFACE, text_color=TEXTO, height=34)
        self._filtro.set("Todas")
        self._filtro.pack(side="left", padx=(10, 0))

        quadro = ctk.CTkFrame(esq, fg_color=SURFACE, corner_radius=12, border_color=BORDA,
                              border_width=1)
        quadro.pack(fill="both", expand=True)
        colunas = (("cliente", "Cliente", 170, True), ("final", "Final", 58, False),
                   ("situacao", "Situação", 118, False), ("vence", "Vence", 140, False),
                   ("plano", "Plano", 74, False), ("tenants", "Tenants", 70, False),
                   ("disp", "Dispositivos", 96, False))
        self._tabela = ttk.Treeview(quadro, columns=[c[0] for c in colunas], show="headings",
                                    style="Painel.Treeview", selectmode="browse")
        for col, tit, larg, estica in colunas:
            self._tabela.heading(col, text=tit, anchor="w")
            self._tabela.column(col, width=larg, minwidth=50, anchor="w", stretch=estica)
        for cod, (forte, _) in COR_SITUACAO.items():
            self._tabela.tag_configure(cod, foreground=TEXTO if cod == "ativa" else forte)
        barra = ctk.CTkScrollbar(quadro, command=self._tabela.yview)
        self._tabela.configure(yscrollcommand=barra.set)
        barra.pack(side="right", fill="y", padx=(0, 4), pady=8)
        self._tabela.pack(fill="both", expand=True, padx=(8, 0), pady=8)
        self._tabela.bind("<<TreeviewSelect>>", self._ao_selecionar)
        self._vazio = ctk.CTkLabel(quadro, text="", font=fonte(13), text_color=TEXTO_3,
                                   fg_color=SURFACE)

        self._detalhe = ctk.CTkScrollableFrame(self._corpo, fg_color=SURFACE, corner_radius=12,
                                               border_color=BORDA, border_width=1, width=400)
        self._detalhe.grid(row=0, column=1, sticky="nsew")

        # rodapé
        self._rodape = ctk.CTkLabel(self, text="", font=fonte(12), text_color=TEXTO_2,
                                    anchor="w")
        self._rodape.pack(fill="x", padx=24, pady=(0, 10))

    # ----------------------------------------------------------- atualizar
    def recarregar_tela(self):
        d = self.repo.dados
        c = D.contagem(self.repo, self._agora())
        self._subtitulo.configure(
            text=f"Produto {self.repo.produto}  ·  {len(d['licencas'])} licença(s)  ·  "
                 f"serial {d['serial']}, emitido em {D.data_hora_br(d['emitido_em'])}")
        rotulos = {}
        for nome, sits in FILTROS.items():
            n = len(d["licencas"]) if sits is None else sum(c[s] for s in sits)
            rotulos[nome] = f"{nome} ({n})"
        atual = self._filtro.get().split(" (")[0]
        self._filtro.configure(values=list(rotulos.values()))
        self._filtro.set(rotulos.get(atual, rotulos["Todas"]))
        self._atualizar_cadeado()
        self._atualizar_git()
        self._preencher_tabela()

    def _filtro_atual(self) -> set[str] | None:
        return FILTROS.get(self._filtro.get().split(" (")[0])

    def _preencher_tabela(self):
        agora = self._agora()
        linhas = D.linhas(self.repo, agora, self._busca.get(), self._filtro_atual())
        self._tabela.delete(*self._tabela.get_children())
        for l in linhas:
            if l.dias is None:
                vence = "sem vencimento"
            elif l.dias >= 0:
                vence = f"{l.vence}  ({l.dias} d)"
            else:
                vence = f"{l.vence}  (há {-l.dias} d)"
            self._tabela.insert("", "end", iid=l.id, tags=(l.situacao,), values=(
                l.cliente, l.final, "●  " + D.SITUACOES[l.situacao], vence, l.plano,
                l.tenants or "qualquer", l.dispositivos))
        if not linhas:
            texto = ("Nenhuma licença ainda. Clique em \"+ Nova licença\"."
                     if not self.repo.dados["licencas"] else "Nada encontrado com esse filtro.")
            self._vazio.configure(text=texto)
            self._vazio.place(relx=0.5, rely=0.4, anchor="center")
        else:
            self._vazio.place_forget()
        if self.selecionado and self._tabela.exists(self.selecionado):
            self._tabela.selection_set(self.selecionado)
            self._tabela.see(self.selecionado)
        elif linhas and not self.selecionado:
            self._tabela.selection_set(linhas[0].id)
        else:
            self._mostrar_detalhe()

    def _ao_selecionar(self, _e=None):
        sel = self._tabela.selection()
        self.selecionado = sel[0] if sel else None
        self._mostrar_detalhe()

    def _atualizar_cadeado(self):
        if self.repo.desbloqueado:
            self._b_cadeado.configure(text="Bloquear chave", text_color=VERDE,
                                      border_color=VERDE)
        else:
            self._b_cadeado.configure(text="Chave bloqueada", text_color=TEXTO_2,
                                      border_color=BORDA)

    def _atualizar_git(self):
        self._git = D.estado_git(self.repo)
        if self._git.erro:
            self._faixa.pack_forget()
            self.status(f"Git: {self._git.erro}", erro=True)
            return
        if self._git.pendente:
            partes = []
            if self._git.alterado:
                partes.append("alterações gravadas sem commit")
            if self._git.a_enviar:
                partes.append(f"{self._git.a_enviar} commit(s) sem push")
            self._faixa_texto.configure(
                text="Falta publicar (" + ", ".join(partes) + "). Os clientes ainda não veem "
                     "essas mudanças.")
            self._faixa.pack(fill="x", padx=22, pady=(0, 12), before=self._corpo)
            self._b_publicar.configure(text_color=AMBAR, border_color=AMBAR)
        else:
            self._faixa.pack_forget()
            self._b_publicar.configure(text_color=TEXTO, border_color=BORDA)

    def status(self, texto: str, erro: bool = False):
        self._rodape.configure(text=texto, text_color=VERMELHO if erro else TEXTO_2)

    # ------------------------------------------------------------- detalhe
    def _mostrar_detalhe(self):
        for w in self._detalhe.winfo_children():
            w.destroy()
        h = self.selecionado
        if not h or h not in self.repo.dados["licencas"]:
            ctk.CTkLabel(self._detalhe, text="Selecione uma licença.", font=fonte(13),
                         text_color=TEXTO_3).pack(pady=40)
            return
        lic = self.repo.lic(h)
        priv = self.repo.privado["licencas"].get(h, {})
        agora = self._agora()
        sit = D.situacao(lic, agora)
        forte, suave = COR_SITUACAO[sit]
        final = priv.get("final", "????")
        st = lic["status"]
        corpo = ctk.CTkFrame(self._detalhe, fg_color="transparent")
        corpo.pack(fill="both", expand=True, padx=14, pady=12)

        # cabeçalho
        cab = ctk.CTkFrame(corpo, fg_color="transparent")
        cab.pack(fill="x")
        ctk.CTkLabel(cab, text=priv.get("cliente") or "(sem cadastro)", font=fonte(19, "bold"),
                     text_color=TEXTO, wraplength=280, justify="left").pack(side="left")
        ctk.CTkLabel(cab, text=D.SITUACOES[sit], font=fonte(12, "bold"), text_color=forte,
                     fg_color=suave, corner_radius=12, height=24).pack(side="right", ipadx=8)
        if priv.get("contato"):
            ctk.CTkLabel(corpo, text=priv["contato"], font=fonte(12), text_color=TEXTO_2
                         ).pack(anchor="w")
        botao(corpo, "Editar cadastro", lambda: self.editar_cadastro(h), "link", height=24,
              width=10, font=fonte(12)).pack(anchor="w", pady=(2, 0))

        if lic.get("mensagem"):
            m = ctk.CTkFrame(corpo, fg_color=AMBAR_SUAVE, corner_radius=8)
            m.pack(fill="x", pady=(10, 0))
            ctk.CTkLabel(m, text="Mensagem para o cliente: " + lic["mensagem"], font=fonte(12),
                         text_color=AMBAR, wraplength=340, justify="left"
                         ).pack(anchor="w", padx=10, pady=8)

        # dados
        sec = self._secao(corpo, "Licença")
        dias = D.dias_para_vencer(lic, agora)
        vence = D.data_br(lic.get("expira_em"))
        if dias is not None:
            vence += f"  ({dias} dias)" if dias >= 0 else f"  (venceu há {-dias} dias)"
        self._par(sec, "Final da chave", final)
        self._par(sec, "ID", h[:10], copiar=h[:10])
        self._par(sec, "Status no arquivo", D.STATUS_ROTULO.get(st, st))
        self._par(sec, "Plano", lic.get("plano") or "—")
        self._par(sec, "Vence em", vence)
        self._par(sec, "Tolerância", f"{lic.get('tolerancia_dias', 0)} dia(s) após vencer")
        self._par(sec, "Criada em", D.data_hora_br(priv.get("criada_em")))

        # ações
        sec = self._secao(corpo, "Ações")
        linha = ctk.CTkFrame(sec, fg_color="transparent")
        linha.pack(fill="x")
        revogada = st == f.Status.REVOGADA.value
        if not revogada:
            botao(linha, "Renovar", lambda: self.renovar(h), "primario", width=110
                  ).pack(side="left")
            if st == f.Status.ATIVA.value:
                botao(linha, "Suspender", lambda: self.suspender(h), width=110
                      ).pack(side="left", padx=(8, 0))
            else:
                botao(linha, "Reativar" if st == f.Status.SUSPENSA.value else "Ativar",
                      lambda: self.reativar(h), width=110).pack(side="left", padx=(8, 0))
            botao(linha, "Mensagem", lambda: self.mensagem(h), width=110
                  ).pack(side="left", padx=(8, 0))
        else:
            ctk.CTkLabel(linha, text="Licença revogada: não volta. Crie outra se preciso.",
                         font=fonte(12), text_color=TEXTO_3).pack(anchor="w")

        # tenants
        sec = self._secao(corpo, "Tenants")
        urls = priv.get("tenants", [])
        if not lic["tenants"]:
            ctk.CTkLabel(sec, text="Sem restrição: vale para qualquer tenant.", font=fonte(12),
                         text_color=AMBAR).pack(anchor="w")
        for url in urls:
            self._item(sec, url.replace("https://", ""),
                       None if revogada else (lambda u=url: self.remover_tenant(h, u)))
        sem_cad = len(lic["tenants"]) - len(urls)
        if sem_cad > 0:
            ctk.CTkLabel(sec, text=f"{sem_cad} tenant(s) sem URL no cadastro privado.",
                         font=fonte(11), text_color=TEXTO_3).pack(anchor="w")
        if not revogada:
            botao(sec, "+ Adicionar tenant", lambda: self.adicionar_tenant(h), "link",
                  height=26, width=10).pack(anchor="w", pady=(4, 0))

        # dispositivos
        sec = self._secao(corpo, "Dispositivos")
        maxd = lic.get("max_dispositivos", 0)
        topo = ctk.CTkFrame(sec, fg_color="transparent")
        topo.pack(fill="x")
        ctk.CTkLabel(topo, text=(f"{len(lic['dispositivos'])} de {maxd} autorizados" if maxd
                                 else "Sem limite de computadores"),
                     font=fonte(12), text_color=TEXTO_2).pack(side="left")
        if not revogada:
            botao(topo, "Alterar limite", lambda: self.limite(h), "link", height=24, width=10,
                  font=fonte(12)).pack(side="right")
        nomes = priv.get("dispositivos", {})
        for d in lic["dispositivos"]:
            rot = f"{d[:5]}-{d[5:10]}…" + (f"  {nomes[d]}" if nomes.get(d) else "")
            self._item(sec, rot, None if revogada else (lambda d=d: self.remover_dispositivo(h, d)))
        if maxd and not revogada:
            botao(sec, "+ Autorizar dispositivo", lambda: self.adicionar_dispositivo(h), "link",
                  height=26, width=10).pack(anchor="w", pady=(4, 0))

        if priv.get("observacoes"):
            sec = self._secao(corpo, "Observações")
            ctk.CTkLabel(sec, text=priv["observacoes"], font=fonte(12), text_color=TEXTO_2,
                         wraplength=340, justify="left").pack(anchor="w")

        # histórico
        evs = D.eventos(self.repo, h, limite=8)
        if evs:
            sec = self._secao(corpo, "Últimas alterações")
            for ev in evs:
                ctk.CTkLabel(sec, text=f"{D.data_hora_br(ev.get('quando'))}   "
                                       f"{D.descrever_evento(ev)}",
                             font=fonte(11), text_color=TEXTO_2, wraplength=350,
                             justify="left").pack(anchor="w")

        if not revogada:
            zona = self._secao(corpo, "Zona de perigo")
            botao(zona, "Revogar licença", lambda: self.revogar(h), "perigo", width=150
                  ).pack(anchor="w")
            ctk.CTkLabel(zona, text="Bloqueia o cliente para sempre (não dá para desfazer).",
                         font=fonte(11), text_color=TEXTO_3).pack(anchor="w", pady=(4, 0))

    def _secao(self, pai, titulo):
        ctk.CTkFrame(pai, fg_color=BORDA, height=1).pack(fill="x", pady=(16, 10))
        ctk.CTkLabel(pai, text=titulo.upper(), font=fonte(11, "bold"), text_color=TEXTO_3
                     ).pack(anchor="w", pady=(0, 6))
        f_ = ctk.CTkFrame(pai, fg_color="transparent")
        f_.pack(fill="x")
        return f_

    def _par(self, pai, rotulo, valor, copiar: str | None = None):
        l = ctk.CTkFrame(pai, fg_color="transparent")
        l.pack(fill="x", pady=1)
        ctk.CTkLabel(l, text=rotulo, font=fonte(12), text_color=TEXTO_3, width=130,
                     anchor="w").pack(side="left")
        ctk.CTkLabel(l, text=valor, font=fonte(12), text_color=TEXTO, anchor="w"
                     ).pack(side="left")
        if copiar:
            botao(l, "copiar", lambda: self._copiar(copiar), "link", height=20, width=10,
                  font=fonte(11)).pack(side="left", padx=(6, 0))

    def _item(self, pai, texto, remover):
        l = ctk.CTkFrame(pai, fg_color=SURFACE_2, corner_radius=8)
        l.pack(fill="x", pady=2)
        ctk.CTkLabel(l, text=texto, font=fonte(12), text_color=TEXTO, anchor="w"
                     ).pack(side="left", padx=10, pady=5)
        if remover:
            ctk.CTkButton(l, text="Remover", command=remover, width=10, height=22,
                          fg_color="transparent", hover_color=VERMELHO_SUAVE,
                          text_color=VERMELHO, font=fonte(11)).pack(side="right", padx=6)

    def _copiar(self, texto: str):
        self.clipboard_clear()
        self.clipboard_append(texto)
        self.status(f"Copiado: {texto}")

    # ------------------------------------------------------------ chave
    def _desbloquear(self) -> bool:
        if self.repo.desbloqueado:
            return True
        caminho = self._caminho_chave
        if not caminho.exists():
            messagebox.showerror("Chave privada", f"Chave privada não encontrada em\n{caminho}"
                                 f"\n\nRode 'py -3.12 -m admin.licencas gerar-chaves' ou defina "
                                 f"{L.ENV_CHAVE}.", parent=self)
            return False
        try:
            cifrada = json.loads(caminho.read_text(encoding="utf-8")).get("cifrada", True)
        except (OSError, ValueError):
            cifrada = True
        if not cifrada or os.environ.get(L.ENV_SENHA):
            try:
                self.repo.desbloquear(L.carregar_chave_privada(caminho))
            except ERROS_DE_USO as e:
                messagebox.showerror("Chave privada", str(e), parent=self)
                return False
        else:
            def abrir(v):
                if not v["senha"]:
                    raise L.ErroAdmin("Digite a senha.")
                self.repo.desbloquear(L.carregar_chave_privada(caminho, v["senha"]))
            ok = Formulario(self, "Desbloquear chave privada",
                            [Campo("senha", "Senha da chave privada", "senha")], abrir,
                            texto_ok="Desbloquear",
                            intro="Para gravar alterações. A chave fica só na memória e é "
                                  f"esquecida ao bloquear, ao fechar ou após {BLOQUEIO_MIN} min "
                                  "sem alterações.").executar()
            if not ok:
                return False
        self._ultimo_uso = time.monotonic()
        self._atualizar_cadeado()
        self.status("Chave privada desbloqueada.")
        return True

    def _alternar_cadeado(self):
        if self.repo.desbloqueado:
            self.repo.bloquear()
            self._atualizar_cadeado()
            self.status("Chave privada bloqueada (esquecida da memória).")
        else:
            self._desbloquear()

    def _vigiar_bloqueio(self):
        if self.repo.desbloqueado and time.monotonic() - self._ultimo_uso > BLOQUEIO_MIN * 60:
            self.repo.bloquear()
            self._atualizar_cadeado()
            self.status(f"Chave bloqueada após {BLOQUEIO_MIN} min sem alterações.")
        self.after(30_000, self._vigiar_bloqueio)

    # ------------------------------------------------------------ gravação
    def _gravar(self, descricao: str, acao: Callable[[], object]):
        """Roda uma alteração; em erro, desfaz em memória (relê do disco) e propaga."""
        try:
            resultado = acao()
        except Exception:
            self.repo.recarregar()
            raise
        self._ultimo_uso = time.monotonic()
        self.acoes.append(descricao)
        self.recarregar_tela()
        self.status(f"{descricao}. Gravado (serial {self.repo.dados['serial']}): falta "
                    "publicar.")
        return resultado

    def _formulario(self, titulo, campos, ao_confirmar, **kw) -> bool:
        if not self._desbloquear():
            return False
        return Formulario(self, titulo, campos, ao_confirmar, **kw).executar()

    def _confirmar_e_gravar(self, titulo, pergunta, descricao, acao, perigo=False):
        if not messagebox.askyesno(titulo, pergunta, parent=self,
                                   icon="warning" if perigo else "question",
                                   default="no" if perigo else "yes"):
            return
        if not self._desbloquear():
            return
        try:
            self._gravar(descricao, acao)
        except ERROS_DE_USO as e:
            messagebox.showerror(titulo, str(e), parent=self)

    def _final(self, h) -> str:
        return self.repo.privado["licencas"].get(h, {}).get("final", h[:6])

    # ------------------------------------------------------------- ações
    def nova_licenca(self):
        criada = {}

        def criar(v):
            if not v["cliente"]:
                raise L.ErroAdmin("Informe o cliente.")
            tenants = [t.strip() for t in v["tenants"].replace(",", "\n").splitlines()
                       if t.strip()]
            for t in tenants:
                f.normalizar_tenant(t)  # valida antes de perguntar qualquer coisa
            validade = D.converter_validade(*v["validade"])
            tol = D.inteiro(v["tolerancia"], "Tolerância")
            maxd = D.inteiro(v["max_dispositivos"], "Limite de dispositivos")
            if not tenants and not messagebox.askyesno(
                    "Sem tenant", "Sem tenant, a licença vale para QUALQUER tenant.\n\n"
                                  "Criar assim mesmo?", icon="warning", default="no",
                    parent=self):
                raise L.ErroAdmin("Informe ao menos um tenant (um por linha).")
            chave, h = self._gravar(
                "Cria licença", lambda: self.repo.criar(
                    v["cliente"], tenants, v["plano"] or "mensal", tolerancia=tol,
                    max_dispositivos=maxd, mensagem=v["mensagem"], contato=v["contato"],
                    obs=v["obs"], pendente=v["pendente"], **validade))
            self.acoes[-1] = f"Cria licença {f.final_chave(chave)} ({v['cliente']})"
            criada.update(chave=chave, h=h, cliente=v["cliente"])

        ok = self._formulario("Nova licença", [
            Campo("cliente", "Cliente *", dica="Só no cadastro privado; não vai para o GitHub."),
            Campo("contato", "Contato", dica="E-mail ou telefone do responsável."),
            Campo("tenants", "Tenants (um por linha)", "multilinha",
                  dica="Ex.: acme.us.qlikcloud.com. Vai publicado só como hash."),
            Campo("plano", "Plano", "opcoes", "mensal", ("mensal", "trimestral", "anual",
                                                         "teste")),
            Campo("validade", "Validade", "validade", ("Meses", "1"),
                  dica="Meses ou dias a partir de hoje, ou até uma data (dd/mm/aaaa)."),
            Campo("tolerancia", "Tolerância após vencer (dias)", valor="3"),
            Campo("max_dispositivos", "Limite de computadores", valor="0",
                  dica="0 = sem limite. Com limite, cada computador precisa ser autorizado."),
            Campo("mensagem", "Mensagem para o cliente (opcional)"),
            Campo("obs", "Observações internas", "multilinha"),
            Campo("pendente", "Criar como pendente (ainda não liberada)", "marcar", False),
        ], criar, texto_ok="Criar licença", colunas=2)
        if ok and criada:
            self.selecionado = criada["h"]
            self._preencher_tabela()
            ChaveCriada(self, criada["chave"], criada["cliente"])

    def renovar(self, h):
        lic = self.repo.lic(h)
        self._formulario("Renovar licença", [
            Campo("validade", "Somar à validade", "validade", ("Meses", "1"),
                  dica="Meses ou dias contam a partir do vencimento atual (ou de hoje, se já "
                       "venceu). \"Até a data\" define a data final."),
        ], lambda v: self._gravar(f"Renova licença {self._final(h)}",
                                  lambda: self.repo.renovar(
                                      h, **D.converter_validade(*v["validade"]))),
            texto_ok="Renovar",
            intro=f"Vence hoje em {D.data_br(lic.get('expira_em'))}.")

    def suspender(self, h):
        self._formulario("Suspender licença", [
            Campo("mensagem", "Mensagem na tela de bloqueio (opcional)", "multilinha",
                  dica="Ex.: Pagamento em aberto. Fale com o financeiro."),
        ], lambda v: self._gravar(f"Suspende licença {self._final(h)}",
                                  lambda: self.repo.mudar_status(h, f.Status.SUSPENSA,
                                                                 v["mensagem"])),
            texto_ok="Suspender", perigo=True,
            intro="O Hub do cliente bloqueia na próxima validação (ao abrir, ou em até 24 h "
                  "se ficar aberto), depois de publicar. Os dados do cliente não são tocados.")

    def reativar(self, h):
        verbo = "Ativa" if self.repo.lic(h)["status"] == f.Status.PENDENTE.value else "Reativa"
        self._confirmar_e_gravar(
            f"{verbo}r licença", f"{verbo}r a licença {self._final(h)}?\n\nA mensagem de "
                                 "bloqueio, se houver, será apagada.",
            f"{verbo} licença {self._final(h)}",
            lambda: self.repo.mudar_status(h, f.Status.ATIVA, ""))

    def revogar(self, h):
        self._confirmar_e_gravar(
            "Revogar licença", f"Revogar a licença {self._final(h)} "
                               f"({self.repo.privado['licencas'].get(h, {}).get('cliente', '')})?"
                               "\n\nIsto NÃO pode ser desfeito: a chave nunca mais funciona.",
            f"Revoga licença {self._final(h)}",
            lambda: self.repo.mudar_status(h, f.Status.REVOGADA), perigo=True)

    def mensagem(self, h):
        self._formulario("Mensagem para o cliente", [
            Campo("mensagem", "Mensagem", "multilinha", self.repo.lic(h).get("mensagem", ""),
                  dica="Aparece no Hub do cliente (aviso ou tela de bloqueio). Vazio = nenhuma."),
        ], lambda v: self._gravar(f"Altera mensagem da licença {self._final(h)}",
                                  lambda: self.repo.definir_mensagem(h, v["mensagem"])))

    def adicionar_tenant(self, h):
        self._formulario("Adicionar tenant", [
            Campo("url", "URL do tenant", dica="Ex.: acme.us.qlikcloud.com"),
        ], lambda v: self._gravar(f"Adiciona tenant à licença {self._final(h)}",
                                  lambda: self.repo.tenant(h, v["url"], True)),
            texto_ok="Adicionar")

    def remover_tenant(self, h, url):
        self._confirmar_e_gravar(
            "Remover tenant", f"Remover {url} da licença {self._final(h)}?"
            + ("\n\nSem nenhum tenant, a licença passa a valer para QUALQUER tenant."
               if len(self.repo.lic(h)["tenants"]) == 1 else ""),
            f"Remove tenant da licença {self._final(h)}",
            lambda: self.repo.tenant(h, url, False), perigo=True)

    def limite(self, h):
        self._formulario("Limite de computadores", [
            Campo("maximo", "Máximo de computadores",
                  valor=str(self.repo.lic(h).get("max_dispositivos", 0)),
                  dica="0 = sem limite. Com limite, o Hub mostra um código de ativação que o "
                       "cliente envia para você autorizar."),
        ], lambda v: self._gravar(f"Altera limite de computadores da licença {self._final(h)}",
                                  lambda: self.repo.limite_dispositivos(
                                      h, D.inteiro(v["maximo"], "Limite"))))

    def adicionar_dispositivo(self, h):
        self._formulario("Autorizar dispositivo", [
            Campo("codigo", "Código de ativação",
                  dica="O cliente copia no Hub, na tela \"Dispositivo não autorizado\". "
                       f"Começa por {self._final(h)}-."),
            Campo("nome", "Apelido do computador (opcional)",
                  dica="Só no cadastro privado. Ex.: notebook do TI."),
        ], lambda v: self._gravar(f"Autoriza dispositivo na licença {self._final(h)}",
                                  lambda: self.repo.dispositivo(h, v["codigo"], True, v["nome"])),
            texto_ok="Autorizar")

    def remover_dispositivo(self, h, d):
        self._confirmar_e_gravar(
            "Remover dispositivo", f"Remover o dispositivo {d[:5]}-{d[5:10]}… da licença "
                                   f"{self._final(h)}?\n\nEsse computador deixa de abrir o Hub.",
            f"Remove dispositivo da licença {self._final(h)}",
            lambda: self.repo.dispositivo(h, d, False), perigo=True)

    def editar_cadastro(self, h):
        priv = self.repo.privado["licencas"].get(h, {})

        def salvar(v):
            if not v["cliente"]:
                raise L.ErroAdmin("Informe o cliente.")
            self.repo.editar_cadastro(h, v["cliente"], v["contato"], v["obs"])
            self.recarregar_tela()
            self.status("Cadastro privado salvo (não precisa publicar).")

        Formulario(self, "Editar cadastro", [
            Campo("cliente", "Cliente *", valor=priv.get("cliente", "")),
            Campo("contato", "Contato", valor=priv.get("contato", "")),
            Campo("obs", "Observações internas", "multilinha", priv.get("observacoes", "")),
        ], salvar, intro="Só no cadastro privado (pasta privado/): não vai para o GitHub e não "
                         "pede a senha.").executar()

    # --------------------------------------------------------------- git
    def _em_fundo(self, trabalho: Callable[[], object], ao_terminar: Callable[[bool, object], None]):
        self._ocupado = True
        self.configure(cursor="watch")

        def rodar():
            try:
                r = (True, trabalho())
            except Exception as e:  # o erro volta para a thread da interface
                r = (False, e)
            self._fila.put((ao_terminar, r))
        threading.Thread(target=rodar, daemon=True).start()

    def _vigiar_fila(self):
        try:
            while True:
                ao_terminar, (ok, valor) = self._fila.get_nowait()
                self._ocupado = False
                self.configure(cursor="")
                ao_terminar(ok, valor)
        except queue.Empty:
            pass
        self.after(100, self._vigiar_fila)

    def publicar(self):
        if self._ocupado:
            return
        self._atualizar_git()
        if self._git.erro:
            messagebox.showerror("Publicar", self._git.erro, parent=self)
            return
        if not self._git.pendente:
            messagebox.showinfo("Publicar", "Nada para publicar: o GitHub já tem a versão "
                                            "atual.", parent=self)
            return
        pronto = {}

        def confirmar(v):
            if not v["mensagem"]:
                raise L.ErroAdmin("Escreva a mensagem do commit.")
            pronto["msg"] = v["mensagem"]

        if not Formulario(self, "Publicar no GitHub", [
            Campo("mensagem", "Mensagem do commit", valor=D.mensagem_commit(self.acoes)),
        ], confirmar, texto_ok="Publicar",
                intro=f"Faz commit de {self.repo.arquivo.relative_to(self.repo.raiz).as_posix()} "
                      "(só o arquivo público; o cadastro privado nunca vai) e push. Os clientes "
                      "recebem em alguns minutos.").executar():
            return
        self.status("Publicando…")
        self._b_publicar.configure(state="disabled")

        def fim(ok, valor):
            self._b_publicar.configure(state="normal")
            if ok:
                self.acoes.clear()
                self.status(str(valor))
            else:
                self.status(f"Falha ao publicar: {valor}", erro=True)
                messagebox.showerror("Publicar", f"Falha ao publicar:\n\n{valor}", parent=self)
            self.recarregar_tela()
        self._em_fundo(lambda: D.publicar(self.repo, pronto["msg"]), fim)

    def _atualizar_do_github(self):
        if self._ocupado:
            return
        self.status("Buscando no GitHub…")
        self._b_atualizar.configure(state="disabled")

        def fim(ok, valor):
            self._b_atualizar.configure(state="normal")
            if ok:
                self.status(f"GitHub: {valor}")
            else:
                self.repo.recarregar()
                self.status(f"Falha ao atualizar: {valor}", erro=True)
            self.recarregar_tela()
        self._em_fundo(lambda: D.atualizar(self.repo), fim)

    # -------------------------------------------------------------- saída
    def _sair(self):
        try:
            pendente = D.estado_git(self.repo).pendente
        except Exception:
            pendente = False
        if pendente and not messagebox.askyesno(
                "Sair", "Há alterações não publicadas: os clientes ainda não as veem.\n\n"
                        "Sair mesmo assim? (Elas continuam gravadas; publique depois.)",
                icon="warning", default="no", parent=self):
            return
        self.repo.bloquear()
        self.destroy()

    def report_callback_exception(self, exc, val, tb):
        traceback.print_exception(exc, val, tb)
        messagebox.showerror("Erro inesperado", f"{exc.__name__}: {val}", parent=self)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="painel", description="Painel de licenças")
    p.add_argument("--produto", default=L.PRODUTO_PADRAO)
    p.add_argument("--raiz", default=str(L.RAIZ))
    p.add_argument("--chave", help="arquivo da chave privada")
    a = p.parse_args(argv)
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("green")
    try:
        app = Painel(Path(a.raiz), a.produto, Path(a.chave) if a.chave else None)
    except ERROS_DE_USO as e:
        raiz = tk.Tk()
        raiz.withdraw()
        messagebox.showerror(TITULO, f"Não foi possível abrir as licenças:\n\n{e}")
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
