"""Formato do arquivo de licenças: chaves, hashes, envelope assinado (Ed25519).

ATENÇÃO: as funções de normalização, hash e verificação têm uma cópia no app
(HUB_RECARGAS/hub_qlik/licenca/). As duas precisam dar exatamente o mesmo
resultado; os testes dos dois lados conferem os mesmos vetores fixos
(tests/test_licencas.py :: VetoresFixosTest e tests/test_licenca.py no app).

Envelope (um único arquivo JSON por produto, ex.: produtos/hubqlik.json):

    {
      "formato": 1,
      "alg": "Ed25519",
      "kid": "<16 hex: sha256 da chave pública>",
      "dados": "<JSON compacto dos dados, como texto>",
      "assinatura": "<base64 da assinatura>"
    }

A assinatura cobre exatamente os bytes UTF-8 de `dados` precedidos de
PREFIXO_ASSINATURA (separação de domínio). O app verifica a assinatura sobre o
texto e só então interpreta esse mesmo texto: o que é verificado é o que é lido.
Um arquivo só (e não .json + .sig) evita que o cache do CDN entregue um dado
novo com uma assinatura velha (ou o contrário).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import urlparse

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

SCHEMA = 1
FORMATO_ENVELOPE = 1
ALG = "Ed25519"
PREFIXO_ASSINATURA = b"hubqlik-licencas:v1\n"

# Sem caracteres ambíguos: 0/O, 1/I/L.
ALFABETO = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
GRUPOS, TAM_GRUPO = 5, 5
TAM_CHAVE = GRUPOS * TAM_GRUPO
TAM_DISPOSITIVO = 20  # hex do ID de dispositivo (80 bits)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEXDISP = re.compile(rf"^[0-9A-F]{{{TAM_DISPOSITIVO}}}$")


class Status(str, Enum):
    PENDENTE = "pending"
    ATIVA = "active"
    TOLERANCIA = "grace_period"   # calculado pelo app (expira_em + tolerância)
    EXPIRADA = "expired"          # calculado pelo app
    SUSPENSA = "suspended"
    REVOGADA = "revoked"


class ErroFormato(ValueError):
    """Arquivo ou campo fora do formato."""


class ErroAssinatura(ValueError):
    """Assinatura inválida ou de chave desconhecida."""


class ErroChave(ValueError):
    """Chave de licença com formato inválido."""


# ------------------------------------------------------------------ chaves
def gerar_chave() -> str:
    """Chave nova (aleatória, nunca sequencial): XXXXX-XXXXX-XXXXX-XXXXX-XXXXX."""
    bruta = "".join(secrets.choice(ALFABETO) for _ in range(TAM_CHAVE))
    return formatar_chave(bruta)


def normalizar_chave(chave: str) -> str:
    """Maiúsculas, sem hífens nem espaços; erro se o formato não bater."""
    n = re.sub(r"[\s\-]", "", chave or "").upper()
    if len(n) != TAM_CHAVE or any(c not in ALFABETO for c in n):
        raise ErroChave("Chave de licença inválida. Confira os 25 caracteres.")
    return n


def formatar_chave(chave: str) -> str:
    n = normalizar_chave(chave)
    return "-".join(n[i:i + TAM_GRUPO] for i in range(0, TAM_CHAVE, TAM_GRUPO))


def final_chave(chave: str) -> str:
    """Últimos 4 caracteres: o único pedaço da chave que aparece em log/tela."""
    return normalizar_chave(chave)[-4:]


def _sha256(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def hash_chave(chave: str, produto: str = "hubqlik") -> str:
    return _sha256(f"{produto}:chave:v1:{normalizar_chave(chave)}")


# ----------------------------------------------------------------- tenant
def normalizar_tenant(url: str) -> str:
    """Mesma regra de hub_qlik.qlik_client.normalize_tenant_url: https://host."""
    bruto = (url or "").strip()
    if not bruto:
        raise ErroFormato("URL do tenant não informada.")
    if "://" not in bruto:
        bruto = "https://" + bruto
    host = urlparse(bruto).hostname
    if not host or "." not in host:
        raise ErroFormato(f"URL do tenant inválida: {url!r}")
    return f"https://{host}"


def hash_tenant(url: str, produto: str = "hubqlik") -> str:
    return _sha256(f"{produto}:tenant:v1:{normalizar_tenant(url)}")


# ------------------------------------------------------------ dispositivo
def normalizar_dispositivo(ident: str) -> str:
    n = re.sub(r"[\s\-]", "", ident or "").upper()
    if not _HEXDISP.match(n):
        raise ErroFormato("ID de dispositivo inválido.")
    return n


def codigo_ativacao(final4: str, dispositivo: str) -> str:
    """Código que o app mostra quando o dispositivo não está autorizado:
    ABCD-XXXXX-XXXXX-XXXXX-XXXXX (final da chave + ID do dispositivo)."""
    d = normalizar_dispositivo(dispositivo)
    return "-".join([final4.upper()] + [d[i:i + 5] for i in range(0, TAM_DISPOSITIVO, 5)])


def ler_codigo_ativacao(codigo: str) -> tuple[str, str]:
    """(final4, dispositivo) a partir do código de ativação."""
    partes = re.sub(r"\s", "", codigo or "").upper().split("-")
    if len(partes) != 5 or len(partes[0]) != 4:
        raise ErroFormato("Código de ativação inválido.")
    return partes[0], normalizar_dispositivo("".join(partes[1:]))


# ------------------------------------------------------------------ datas
def agora_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def para_iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def de_iso(texto: str) -> datetime:
    try:
        d = datetime.strptime(texto, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        raise ErroFormato(f"Data inválida: {texto!r} (use AAAA-MM-DDTHH:MM:SSZ)")
    return d.replace(tzinfo=timezone.utc)


# --------------------------------------------------------- validação dados
def validar_dados(dados: dict, produto: str | None = None) -> dict:
    """Confere o esquema dos dados; devolve os próprios dados."""
    if not isinstance(dados, dict):
        raise ErroFormato("Dados não são um objeto JSON.")
    if dados.get("schema") != SCHEMA:
        raise ErroFormato(f"Esquema não suportado: {dados.get('schema')!r}")
    if not isinstance(dados.get("produto"), str) or not dados["produto"]:
        raise ErroFormato("Campo 'produto' ausente.")
    if produto is not None and dados["produto"] != produto:
        raise ErroFormato(f"Arquivo de outro produto: {dados['produto']!r}")
    serial = dados.get("serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 1:
        raise ErroFormato("Campo 'serial' inválido.")
    de_iso(dados.get("emitido_em"))
    licencas = dados.get("licencas")
    if not isinstance(licencas, dict):
        raise ErroFormato("Campo 'licencas' inválido.")
    validos = {s.value for s in Status}
    for h, lic in licencas.items():
        if not _HEX64.match(h) or not isinstance(lic, dict):
            raise ErroFormato(f"Licença inválida: {h[:12]!r}")
        if lic.get("status") not in validos:
            raise ErroFormato(f"Status inválido em {h[:12]}: {lic.get('status')!r}")
        if lic.get("expira_em") is not None:
            de_iso(lic["expira_em"])
        tol = lic.get("tolerancia_dias", 0)
        if not isinstance(tol, int) or isinstance(tol, bool) or tol < 0:
            raise ErroFormato(f"tolerancia_dias inválida em {h[:12]}")
        maxd = lic.get("max_dispositivos", 0)
        if not isinstance(maxd, int) or isinstance(maxd, bool) or maxd < 0:
            raise ErroFormato(f"max_dispositivos inválido em {h[:12]}")
        tenants = lic.get("tenants", [])
        if not isinstance(tenants, list) or not all(
                isinstance(t, str) and _HEX64.match(t) for t in tenants):
            raise ErroFormato(f"tenants inválidos em {h[:12]}")
        disps = lic.get("dispositivos", [])
        if not isinstance(disps, list) or not all(
                isinstance(d, str) and _HEXDISP.match(d) for d in disps):
            raise ErroFormato(f"dispositivos inválidos em {h[:12]}")
        if maxd and len(disps) > maxd:
            raise ErroFormato(f"Mais dispositivos que o limite em {h[:12]}")
        if not isinstance(lic.get("mensagem", ""), str):
            raise ErroFormato(f"mensagem inválida em {h[:12]}")
    return dados


# ------------------------------------------------------------- assinatura
def kid(chave_publica: VerifyKey) -> str:
    return hashlib.sha256(bytes(chave_publica)).hexdigest()[:16]


def chave_publica_b64(chave_publica: VerifyKey) -> str:
    return base64.b64encode(bytes(chave_publica)).decode("ascii")


def chave_publica_de_b64(texto: str) -> VerifyKey:
    try:
        bruta = base64.b64decode(texto.strip(), validate=True)
    except (ValueError, TypeError):
        raise ErroFormato("Chave pública inválida (base64).")
    if len(bruta) != 32:
        raise ErroFormato("Chave pública inválida (tamanho).")
    return VerifyKey(bruta)


def texto_dados(dados: dict) -> str:
    """JSON compacto e determinístico (é este texto que é assinado)."""
    return json.dumps(dados, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def montar_envelope(dados: dict, chave_privada: SigningKey) -> bytes:
    validar_dados(dados)
    texto = texto_dados(dados)
    assinatura = chave_privada.sign(PREFIXO_ASSINATURA + texto.encode("utf-8")).signature
    env = {
        "formato": FORMATO_ENVELOPE,
        "alg": ALG,
        "kid": kid(chave_privada.verify_key),
        "dados": texto,
        "assinatura": base64.b64encode(assinatura).decode("ascii"),
    }
    return (json.dumps(env, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def abrir_envelope(conteudo: bytes, chaves_publicas: list[VerifyKey],
                   produto: str | None = None) -> dict:
    """Verifica a assinatura e devolve os dados validados.
    Erros: ErroFormato (estrutura) ou ErroAssinatura (assinatura)."""
    try:
        env = json.loads(conteudo.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ErroFormato("Arquivo de licenças não é JSON válido.")
    if not isinstance(env, dict) or env.get("formato") != FORMATO_ENVELOPE \
            or env.get("alg") != ALG:
        raise ErroFormato("Envelope de licenças em formato desconhecido.")
    texto, assinatura_b64 = env.get("dados"), env.get("assinatura")
    if not isinstance(texto, str) or not isinstance(assinatura_b64, str):
        raise ErroFormato("Envelope sem dados ou assinatura.")
    try:
        assinatura = base64.b64decode(assinatura_b64, validate=True)
    except (ValueError, TypeError):
        raise ErroAssinatura("Assinatura em base64 inválido.")
    chaves = [c for c in chaves_publicas if kid(c) == env.get("kid")]
    if not chaves:
        raise ErroAssinatura("Arquivo assinado por chave desconhecida.")
    mensagem = PREFIXO_ASSINATURA + texto.encode("utf-8")
    try:
        chaves[0].verify(mensagem, assinatura)
    except (BadSignatureError, ValueError):
        raise ErroAssinatura("Assinatura do arquivo de licenças inválida.")
    try:
        dados = json.loads(texto)
    except ValueError:
        raise ErroFormato("Dados assinados não são JSON válido.")
    return validar_dados(dados, produto)
