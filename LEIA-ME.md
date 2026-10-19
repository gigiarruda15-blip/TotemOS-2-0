# Totem OS - ISO unica (instala tudo, inclusive o GRUB)

1. Suba o CONTEUDO desta pasta (build_iso.sh, preseed.cfg, instalar_offline.sh, app, .github) na raiz do repositorio.
   (Crie .github/workflows/build-iso.yml pelo site do GitHub se a pasta oculta nao subir.)
2. Actions > Build Totem OS ISO > Run workflow. Baixe o artefato totem-os-iso (totem-os.iso).
3. Boot pela ISO. A instalacao e automatica; unica pergunta: confirmar a gravacao no disco.
4. Ao terminar, remova a ISO. O computador reinicia direto no totem.
Usuario do sistema: admin / admin1234 (troque com passwd). Dados em /var/lib/totem.
