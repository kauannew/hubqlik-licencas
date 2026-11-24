# Licenças — Hub Recargas - Data Priority

Fonte da verdade das licenças da edição **tenant único** do Hub. O app baixa
`produtos/hubqlik.json` pelo raw do GitHub, confere a assinatura Ed25519 com a chave
pública embutida e decide se libera o uso. Detalhes da arquitetura e da política no
`LICENSING.md` do repositório do app.

**Este repositório é público.** O arquivo publicado tem só hashes e dados técnicos:
nada de nome, CNPJ, e-mail ou URL de tenant em claro.

| Caminho | Público? | Conteúdo |
|---|---|---|
| `produtos/<produto>.json` | sim | envelope assinado (um arquivo por produto) |
| `chave_publica.txt` | sim | chave pública Ed25519 (a mesma embutida no app) |
| `admin/` | sim | CLI administrativa e o formato |
| `privado/` | **não** (`.gitignore`) | cadastro de clientes e log de eventos |
| chave privada | **não**, fora do repo | `~/.hubqlik/<produto>_licencas_ed25519.json` (ou `HUBQLIK_LIC_CHAVE_PRIVADA`) |

Um arquivo por produto: cada produto tem seu `serial` e seu cache no app, um não
invalida o outro e o arquivo baixado fica pequeno. O campo `produto` e os prefixos dos
hashes (`<produto>:chave:v1:`) impedem que a licença de um sirva para outro.

## Preparar (uma vez)
```
py -3.12 -m pip install -r requirements.txt
py -3.12 -m admin.licencas gerar-chaves
```
- A chave privada é cifrada com senha (Argon2id + XSalsa20-Poly1305). Guarde a senha num
  cofre de senhas. `HUBQLIK_LIC_SENHA` evita digitá-la a cada comando.
- Copie a chave pública impressa para `CHAVES_PUBLICAS_LICENCA` em `hub_qlik/config.py` do
  app e gere uma versão nova do app.
- **Backup:** guarde o arquivo da chave privada e a pasta `privado/` fora deste computador
  (ex.: cofre de senhas com anexo, ou mídia cifrada). Sem a chave privada não dá para
  emitir nem alterar licenças. Se ela vazar, gere outra, publique uma versão do app com a
  nova chave pública e reassine o arquivo.

## Uso
```
py -3.12 -m admin.licencas criar --cliente "ACME" --contato "ti@acme" --tenant acme.us.qlikcloud.com --meses 1
py -3.12 -m admin.licencas listar
py -3.12 -m admin.licencas renovar 3fa9c1d2 --meses 1
py -3.12 -m admin.licencas suspender 3fa9c1d2 --mensagem "Fale com o financeiro"
py -3.12 -m admin.licencas reativar 3fa9c1d2
py -3.12 -m admin.licencas revogar 3fa9c1d2
py -3.12 -m admin.licencas tenant-adicionar 3fa9c1d2 outro.eu.qlikcloud.com
py -3.12 -m admin.licencas limite 3fa9c1d2 2
py -3.12 -m admin.licencas dispositivo-adicionar 3fa9c1d2 ABCD-1A2B3-C4D5E-6F708-192A3 --nome "notebook TI"
py -3.12 -m admin.licencas mostrar final:ABCD
py -3.12 -m admin.licencas validar
```
- O ID é o começo do hash da chave (coluna 1 do `listar`) ou `final:XXXX` (últimos 4 da
  chave, pelo cadastro privado).
- `criar` mostra a chave **uma única vez**; só o hash é gravado.
- Sem `--tenant`, a licença vale para qualquer tenant (o comando avisa).
- `--max-dispositivos 0` (padrão) = sem limite de máquinas.
- Cada comando incrementa `serial`, atualiza `emitido_em`, assina e confere antes de
  gravar. Depois: `git commit` e `git push` para publicar.

## Tempo até chegar ao cliente
O raw do GitHub fica em cache por alguns minutos (em geral até 5). O app revalida na
abertura e a cada 24 h. Uma suspensão pode levar até cerca de 24 h para bloquear um app
que fica aberto sem reiniciar.

## Testes
`py -3.12 -m unittest discover -s tests -t .` — o GitHub Actions roda os testes e
`validar` a cada push.
