"""Testes do formato e da CLI administrativa (offline, chaves geradas no teste)."""
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import nacl.pwhash
from nacl.signing import SigningKey

from admin import formato as f
from admin import licencas as L

AGORA = datetime(2026, 11, 24, 18, 0, 0, tzinfo=timezone.utc)

# Vetores fixos: os MESMOS conferidos pelo app (HUB_RECARGAS/tests/test_licenca.py).
VETOR_CHAVE = ("abcde fghjk-mnpqr-stuvw-xyz23",
               "a61c5497a35f0ea1c9e3ca93c9e54a1697c8c1861ab11ccc127e2fda59510605")
VETOR_TENANT = ("Acme.US.QlikCloud.com/hub",
                "169c92b2bcf797581bd099994f54f4b687a4b69ef4389d9a20425f1fcf0c6874")
VETOR_SEMENTE = bytes(range(32))
VETOR_PUBLICA = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
VETOR_ENVELOPE = (
    '{"formato": 1, "alg": "Ed25519", "kid": "56475aa75463474c", "dados": '
    '"{\\"emitido_em\\":\\"2026-11-24T18:00:00Z\\",\\"licencas\\":{},\\"produto\\":'
    '\\"hubqlik\\",\\"schema\\":1,\\"serial\\":1}", "assinatura": '
    '"gvYBAyLB3sVefekDC5V2oQ9dD1SFZrjLLN0CdpJoUqWI8Fvny105yyHfs8610TMfewB9mL/TB6suY4vUDxFRAw=="}')


class VetoresFixosTest(unittest.TestCase):
    def test_hashes(self):
        self.assertEqual(f.hash_chave(VETOR_CHAVE[0]), VETOR_CHAVE[1])
        self.assertEqual(f.hash_tenant(VETOR_TENANT[0]), VETOR_TENANT[1])

    def test_assinatura(self):
        sk = SigningKey(VETOR_SEMENTE)
        self.assertEqual(f.chave_publica_b64(sk.verify_key), VETOR_PUBLICA)
        dados = f.abrir_envelope(VETOR_ENVELOPE.encode(), [f.chave_publica_de_b64(VETOR_PUBLICA)])
        self.assertEqual(dados["serial"], 1)
        env = json.loads(f.montar_envelope(dados, sk))
        self.assertEqual(env, json.loads(VETOR_ENVELOPE))

    def test_codigo_ativacao(self):
        c = f.codigo_ativacao("xyz3", "0123456789abcdef0123")
        self.assertEqual(c, "XYZ3-01234-56789-ABCDE-F0123")
        self.assertEqual(f.ler_codigo_ativacao(c.lower()), ("XYZ3", "0123456789ABCDEF0123"))


class ChaveTest(unittest.TestCase):
    def test_formato_e_alfabeto(self):
        vistas = set()
        for _ in range(200):
            c = f.gerar_chave()
            self.assertRegex(c, r"^[2-9A-Z]{5}(-[2-9A-Z]{5}){4}$")
            self.assertFalse(set(c) & set("01OIL"))
            vistas.add(c)
        self.assertEqual(len(vistas), 200)

    def test_normalizacao(self):
        c = f.gerar_chave()
        self.assertEqual(f.hash_chave(c.lower().replace("-", " ")), f.hash_chave(c))
        self.assertEqual(f.final_chave(c), c[-4:])
        for ruim in ("", "ABCDE", "ABCDE-FGHJK-MNPQR-STUVW-XYZ2O", c + "2"):
            with self.assertRaises(f.ErroChave):
                f.normalizar_chave(ruim)

    def test_prefixo_de_produto_separa_hashes(self):
        c = f.gerar_chave()
        self.assertNotEqual(f.hash_chave(c, "hubqlik"), f.hash_chave(c, "outro"))

    def test_tenant(self):
        self.assertEqual(f.normalizar_tenant("https://acme.us.qlikcloud.com/x?y"),
                         "https://acme.us.qlikcloud.com")
        with self.assertRaises(f.ErroFormato):
            f.normalizar_tenant("semponto")


class EnvelopeTest(unittest.TestCase):
    def setUp(self):
        self.sk = SigningKey.generate()
        self.dados = {"schema": 1, "produto": "hubqlik", "serial": 3,
                      "emitido_em": "2026-11-24T18:00:00Z", "licencas": {}}
        self.env = f.montar_envelope(self.dados, self.sk)

    def test_ida_e_volta(self):
        self.assertEqual(f.abrir_envelope(self.env, [self.sk.verify_key], "hubqlik"), self.dados)

    def test_adulterado(self):
        env = json.loads(self.env)
        env["dados"] = env["dados"].replace('"serial":3', '"serial":9')
        with self.assertRaises(f.ErroAssinatura):
            f.abrir_envelope(json.dumps(env).encode(), [self.sk.verify_key])

    def test_chave_errada(self):
        with self.assertRaises(f.ErroAssinatura):
            f.abrir_envelope(self.env, [SigningKey.generate().verify_key])

    def test_outro_produto(self):
        with self.assertRaises(f.ErroFormato):
            f.abrir_envelope(self.env, [self.sk.verify_key], "outro")

    def test_lixo(self):
        for ruim in (b"", b"nao json", b"[]", b'{"formato": 2}'):
            with self.assertRaises(f.ErroFormato):
                f.abrir_envelope(ruim, [self.sk.verify_key])

    def test_status_fora_do_enum(self):
        self.dados["licencas"] = {"a" * 64: {"status": "ativo"}}
        with self.assertRaises(f.ErroFormato):
            f.montar_envelope(self.dados, self.sk)


class RepositorioTest(unittest.TestCase):
    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.raiz, True)
        self.sk = SigningKey.generate()
        self.agora = AGORA
        self.r = self.novo()

    def novo(self):
        return L.Repositorio(self.raiz, "hubqlik", self.sk, agora=lambda: self.agora)

    def publicado(self):
        return f.abrir_envelope((self.raiz / "produtos/hubqlik.json").read_bytes(),
                                [self.sk.verify_key], "hubqlik")

    def test_criar(self):
        chave, h = self.r.criar("ACME Ltda", ["acme.us.qlikcloud.com"], meses=1,
                                contato="ti@acme.example")
        self.assertEqual(h, f.hash_chave(chave))
        d = self.publicado()
        self.assertEqual(d["serial"], 1)
        lic = d["licencas"][h]
        self.assertEqual(lic["status"], "active")
        self.assertEqual(lic["expira_em"], "2026-12-24T23:59:59Z")
        self.assertEqual(lic["tolerancia_dias"], 1)
        self.assertEqual(lic["tenants"], [f.hash_tenant("https://acme.us.qlikcloud.com")])
        # Nada pessoal nem a chave no arquivo público.
        texto = (self.raiz / "produtos/hubqlik.json").read_text(encoding="utf-8")
        for proibido in ("ACME", "acme", "ti@", chave, chave.replace("-", "")):
            self.assertNotIn(proibido, texto)
        # Cadastro privado e log de eventos.
        priv = json.loads((self.raiz / "privado/hubqlik_clientes.json").read_text("utf-8"))
        self.assertEqual(priv["licencas"][h]["cliente"], "ACME Ltda")
        self.assertEqual(priv["licencas"][h]["final"], chave[-4:])
        eventos = (self.raiz / "privado/hubqlik_eventos.jsonl").read_text("utf-8")
        self.assertIn('"acao": "criar"', eventos)
        self.assertNotIn(chave, eventos)

    def test_serial_cresce_e_recarrega(self):
        _, h = self.r.criar("A", ["a.qlikcloud.com"], dias=10)
        self.r.mudar_status(h, f.Status.SUSPENSA, "Pagamento pendente")
        r2 = self.novo()
        self.assertEqual(r2.dados["serial"], 2)
        self.assertEqual(r2.lic(h)["status"], "suspended")
        self.assertEqual(r2.lic(h)["mensagem"], "Pagamento pendente")

    def test_renovar_soma_a_partir_do_vencimento(self):
        _, h = self.r.criar("A", [], expira="2027-01-31")
        self.assertEqual(self.r.renovar(h, meses=1), "2027-02-28T23:59:59Z")
        self.assertEqual(self.r.renovar(h, expira="2027-06-30"), "2027-06-30T23:59:59Z")
        with self.assertRaises(L.ErroAdmin):
            self.r.renovar(h, meses=1, dias=3)

    def test_renovar_vencida_conta_de_hoje(self):
        _, h = self.r.criar("A", [], expira="2026-10-01")
        self.assertEqual(self.r.renovar(h, dias=10), "2026-12-04T23:59:59Z")

    def test_suspender_revogar_reativar(self):
        _, h = self.r.criar("A", [], meses=1)
        self.r.mudar_status(h, f.Status.SUSPENSA)
        self.r.mudar_status(h, f.Status.ATIVA)
        self.r.mudar_status(h, f.Status.REVOGADA)
        self.assertEqual(self.publicado()["licencas"][h]["status"], "revoked")
        with self.assertRaises(L.ErroAdmin):
            self.r.mudar_status(h, f.Status.ATIVA)

    def test_tenants(self):
        _, h = self.r.criar("A", ["a.qlikcloud.com"], meses=1)
        self.r.tenant(h, "https://B.qlikcloud.com/x", True)
        self.assertEqual(len(self.publicado()["licencas"][h]["tenants"]), 2)
        with self.assertRaises(L.ErroAdmin):
            self.r.tenant(h, "b.qlikcloud.com", True)
        self.r.tenant(h, "a.qlikcloud.com", False)
        self.assertEqual(self.publicado()["licencas"][h]["tenants"],
                         [f.hash_tenant("b.qlikcloud.com")])

    def test_dispositivos_e_limite(self):
        chave, h = self.r.criar("A", [], meses=1)
        d1, d2 = "AAAAABBBBBCCCCCDDDDD", "1111122222333334444F"
        with self.assertRaises(L.ErroAdmin):  # sem limite definido
            self.r.dispositivo(h, d1, True)
        self.r.limite_dispositivos(h, 1)
        self.r.dispositivo(h, f.codigo_ativacao(chave[-4:], d1), True, nome="notebook")
        with self.assertRaises(L.ErroAdmin):  # limite atingido
            self.r.dispositivo(h, d2, True)
        with self.assertRaises(L.ErroAdmin):  # código de outra chave
            self.r.dispositivo(h, f.codigo_ativacao("ZZZZ", d2), True)
        self.assertEqual(self.publicado()["licencas"][h]["dispositivos"], [d1])
        with self.assertRaises(L.ErroAdmin):
            self.r.limite_dispositivos(h, -1)
        self.r.dispositivo(h, d1, False)
        self.assertEqual(self.publicado()["licencas"][h]["dispositivos"], [])

    def test_resolver(self):
        chave, h = self.r.criar("A", [], meses=1)
        self.assertEqual(self.r.resolver(h[:8]), h)
        self.assertEqual(self.r.resolver("final:" + chave[-4:].lower()), h)
        with self.assertRaises(L.ErroAdmin):
            self.r.resolver("abc")

    def test_arquivo_adulterado_nao_abre(self):
        self.r.criar("A", [], meses=1)
        arq = self.raiz / "produtos/hubqlik.json"
        arq.write_text(arq.read_text("utf-8").replace("active", "revoked"), "utf-8")
        with self.assertRaises(f.ErroAssinatura):
            self.novo()


class ChavePrivadaTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.rapido = dict(ops=nacl.pwhash.argon2id.OPSLIMIT_MIN,
                           mem=nacl.pwhash.argon2id.MEMLIMIT_MIN)

    def test_cifrada(self):
        sk = SigningKey.generate()
        arq = self.dir / "k.json"
        L.salvar_chave_privada(sk, arq, "senha-bem-comprida", **self.rapido)
        self.assertNotIn(__import__("base64").b64encode(bytes(sk)).decode(), arq.read_text())
        self.assertEqual(bytes(L.carregar_chave_privada(arq, "senha-bem-comprida")), bytes(sk))
        with self.assertRaises(L.ErroAdmin):
            L.carregar_chave_privada(arq, "errada")
        with self.assertRaises(L.ErroAdmin):  # não sobrescreve
            L.salvar_chave_privada(sk, arq, None)


class CliTest(unittest.TestCase):
    def test_fluxo(self):
        raiz = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, raiz, True)
        chave = raiz / "fora" / "k.json"
        base = ["--raiz", str(raiz), "--chave", str(chave)]
        out = []
        L.executar(base + ["gerar-chaves", "--sem-senha"], out.append)
        self.assertTrue((raiz / "chave_publica.txt").exists())
        L.executar(base + ["criar", "--cliente", "ACME", "--tenant", "acme.qlikcloud.com",
                           "--meses", "1", "--max-dispositivos", "2"], out.append)
        linha = next(l for l in out if l.startswith("CHAVE"))
        lic = linha.split(": ")[1]
        f.normalizar_chave(lic)
        h = f.hash_chave(lic)[:8]
        L.executar(base + ["suspender", h, "--mensagem", "Fale com o financeiro"], out.append)
        L.executar(base + ["listar"], out.append)
        self.assertTrue(any("suspended" in l and "ACME" in l for l in out))
        out.clear()
        L.executar(["--raiz", str(raiz), "validar"], out.append)
        self.assertIn("serial 2", out[0])


if __name__ == "__main__":
    unittest.main()
