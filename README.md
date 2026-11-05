# WinProvision-PKGS

Os JSONs de produtos e versões estão na branch `manifests`. O sincronizador em `main` acompanha o catálogo WinGet e preserva enriquecimentos próprios, como ícones.

## Índice SQLite

Cada atualização publicada em `manifests` gera uma release imutável por commit, com:

- `winprovision-catalog.sqlite`: aplicativos, caminhos e hashes de ícones, metadados do catálogo e índice de busca FTS5;
- `catalog-manifest.json`: commit de origem, versão do formato, quantidade de aplicativos e versões, tamanho e SHA-256 do banco;
- `SHA256SUMS`: hashes do banco e do manifesto.

O índice é derivado do commit exato informado na release; os JSONs continuam sendo a fonte dos dados. A versão 2 do esquema acrescenta `apps.app_sha256`, com o SHA-256 dos bytes do JSON de produto, permitindo validar seus detalhes antes da leitura. A tabela `metadata` registra o commit e as contagens. Versões completas e instaladores continuam nos JSONs. Releases v2 usam a tag `catalog-v2-<commit completo>` para preservar releases v1 já existentes como `catalog-<commit completo>`; ambas são imutáveis. A atualização por push dispara o workflow presente na branch `manifests`; a sincronização por GitHub Actions também solicita o workflow explicitamente porque pushes feitos pelo `GITHUB_TOKEN` não disparam outro workflow de push. `workflow_dispatch` permite recuperar uma execução informando o SHA completo ou usando o head atual de `manifests`.

Para gerar e validar localmente, com a branch ou commit disponível no clone:

```powershell
$env:CATALOG_REF = 'origin/manifests'
$env:INDEX_DB_PATH = "$PWD/artifacts/winprovision-catalog.sqlite"
python scripts/build_release_index.py
$sha = git rev-parse "$env:CATALOG_REF^{commit}"
python scripts/create_release_manifest.py $env:INDEX_DB_PATH $sha
python scripts/verify_release_manifest.py (Split-Path $env:INDEX_DB_PATH) $sha
```

O hash detecta alterações nos arquivos, mas não assina a release. A geração não modifica os aplicativos clientes nem publica o banco no R2.
