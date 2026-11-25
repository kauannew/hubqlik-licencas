"""Testes do painel: modo leitura do repositório e a camada sem Tk (painel_dados)."""
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nacl.signing import SigningKey

from admin import formato as f
from admin import licencas as L
from admin import painel_dados as D

AGORA = datetime(2026, 11, 24, 18, 0, 0, tzinfo=timezone.utc)


def _repo_com_chave(raiz: Path, sk: SigningKey) -> L.Repositorio:
    (raiz / "chave_publica.txt").write_text(
        f"# teste\n{f.chave_publica_b64(sk.verify_key)}\n", encoding="utf-8")
    return L.Repositorio(raiz, "hubqlik", sk, agora=lambda: AGORA)


class LeituraTest(unittest.TestCase):
    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.raiz, True)
        self.sk = SigningKey.generate()
        self.r = _repo_com_chave(self.raiz, self.sk)
        self.chave, self.h = self.r.criar("ACME", ["acme.us.qlikcloud.com"], meses=1)

    def test_abre_sem_chave_privada(self):
        r = L.Repositorio(self.raiz, "hubqlik")
        self.assertFalse(r.desbloqueado)
        self.assertIn(self.h, r.dados["licencas"])
        with self.assertRaises(L.ErroAdmin):
            r.renovar(self.h, meses=1)
        r.recarregar()  # o erro não gravou nada
        self.assertEqual(r.dados["serial"], 1)

    def test_desbloquear_confere_chave_publica(self):
        r = L.Repositorio(self.raiz, "hubqlik")
        with self.assertRaises(L.ErroAdmin):
            r.desbloquear(SigningKey.generate())
        r.desbloquear(self.sk)
        r.renovar(self.h, meses=1)
        self.assertEqual(r.dados["serial"], 2)
        r.bloquear()
        self.assertFalse(r.desbloqueado)

    def test_editar_cadastro_nao_publica(self):
        r = L.Repositorio(self.raiz, "hubqlik")
        antes = (self.raiz / "produtos/hubqlik.json").read_bytes()
        r.editar_cadastro(self.h, cliente="ACME S.A.", obs="cliente desde 2026")
        self.assertEqual((self.raiz / "produtos/hubqlik.json").read_bytes(), antes)
        r2 = L.Repositorio(self.raiz, "hubqlik")
        self.assertEqual(r2.privado["licencas"][self.h]["cliente"], "ACME S.A.")
        self.assertEqual(r2.privado["licencas"][self.h]["observacoes"], "cliente desde 2026")

    def test_cli_listar_sem_senha(self):
        saida = []
        L.executar(["--raiz", str(self.raiz), "--chave", str(self.raiz / "nao_existe.json"),
                    "listar"], saida.append)
        self.assertIn("ACME", saida[0])


class SituacaoTest(unittest.TestCase):
    def lic(self, status="active", dias=30, tol=3):
        expira = AGORA + timedelta(days=dias)
        return {"status": status, "expira_em": f.para_iso(expira), "tolerancia_dias": tol}

    def test_regras(self):
        self.assertEqual(D.situacao(self.lic(dias=30), AGORA), "ativa")
        self.assertEqual(D.situacao(self.lic(dias=5), AGORA), "vencendo")
        self.assertEqual(D.situacao(self.lic(dias=-2), AGORA), "tolerancia")
        self.assertEqual(D.situacao(self.lic(dias=-4), AGORA), "vencida")
        self.assertEqual(D.situacao(self.lic("suspended", dias=-40), AGORA), "suspensa")
        self.assertEqual(D.situacao(self.lic("revoked"), AGORA), "revogada")
        self.assertEqual(D.situacao(self.lic("pending"), AGORA), "pendente")
        self.assertEqual(D.situacao({"status": "active"}, AGORA), "ativa")

    def test_validade(self):
        self.assertEqual(D.converter_validade("Meses", " 2 "), {"meses": 2})
        self.assertEqual(D.converter_validade("Dias", "15"), {"dias": 15})
        self.assertEqual(D.converter_validade("Até a data", "31/12/2026"),
                         {"expira": "2026-12-31"})
        for tipo, valor in (("Meses", "0"), ("Dias", "x"), ("Até a data", "2026-12-31")):
            with self.assertRaises(L.ErroAdmin):
                D.converter_validade(tipo, valor)

    def test_mensagem_commit(self):
        self.assertEqual(D.mensagem_commit([]), "Atualiza licenças")
        self.assertEqual(D.mensagem_commit(["Suspende licença A8MZ"]), "Suspende licença A8MZ")
        self.assertEqual(D.mensagem_commit(["A", "B", "A"]), "Atualiza licenças: A; B")


class LinhasEventosTest(unittest.TestCase):
    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.raiz, True)
        self.r = _repo_com_chave(self.raiz, SigningKey.generate())
        _, self.a = self.r.criar("Beta", ["beta.eu.qlikcloud.com"], dias=3, contato="ti@beta")
        _, self.b = self.r.criar("Alfa", [], meses=2, max_dispositivos=2)
        self.r.mudar_status(self.a, f.Status.SUSPENSA, "Fale com o financeiro")

    def test_ordem_filtros_e_busca(self):
        ls = D.linhas(self.r, AGORA)
        self.assertEqual([l.cliente for l in ls], ["Alfa", "Beta"])
        self.assertEqual(ls[0].dispositivos, "0/2")
        self.assertEqual(ls[1].situacao, "suspensa")
        self.assertEqual([l.cliente for l in D.linhas(self.r, AGORA, situacoes={"ativa"})],
                         ["Alfa"])
        self.assertEqual([l.cliente for l in D.linhas(self.r, AGORA, "beta.eu")], ["Beta"])
        self.assertEqual([l.cliente for l in D.linhas(self.r, AGORA, "TI@BETA")], ["Beta"])
        self.assertEqual(D.contagem(self.r, AGORA)["suspensa"], 1)

    def test_eventos(self):
        todos = D.eventos(self.r)
        self.assertEqual(todos[0]["acao"], "status")  # mais recente primeiro
        self.assertEqual(len(D.eventos(self.r, self.b)), 1)
        self.assertIn("Suspensa", D.descrever_evento(todos[0]))


@unittest.skipIf(shutil.which("git") is None, "git não instalado")
class GitTest(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        remoto, self.raiz = base / "remoto.git", base / "local"
        self._git(base, "init", "--bare", "-q", str(remoto))
        self._git(base, "clone", "-q", str(remoto), str(self.raiz))
        self._git(self.raiz, "config", "user.email", "teste@example.invalid")
        self._git(self.raiz, "config", "user.name", "Teste")
        (self.raiz / ".gitignore").write_text("privado/\n", encoding="utf-8")
        self.r = _repo_com_chave(self.raiz, SigningKey.generate())
        self.r.criar("ACME", ["acme.us.qlikcloud.com"], meses=1)
        self._git(self.raiz, "add", ".")
        self._git(self.raiz, "commit", "-q", "-m", "inicio")
        self._git(self.raiz, "push", "-q", "-u", "origin", "HEAD")

    @staticmethod
    def _git(cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    def test_estado_e_publicar(self):
        self.assertFalse(D.estado_git(self.r).pendente)
        h = next(iter(self.r.dados["licencas"]))
        self.r.renovar(h, meses=1)
        e = D.estado_git(self.r)
        self.assertTrue(e.alterado)
        self.assertEqual(e.erro, "")
        D.publicar(self.r, "Renova licença")
        self.assertFalse(D.estado_git(self.r).pendente)
        log = subprocess.run(["git", "log", "-1", "--name-only", "--format=%s"], cwd=self.raiz,
                             capture_output=True, text=True, encoding="utf-8").stdout
        self.assertIn("Renova licença", log)
        self.assertIn("produtos/hubqlik.json", log)
        self.assertNotIn("privado", log)


if __name__ == "__main__":
    unittest.main()
