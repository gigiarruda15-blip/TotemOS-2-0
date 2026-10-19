#!/bin/bash
# Gera totem-os.iso: Debian 12 netinst oficial + totem + todos os pacotes (offline).
set -euo pipefail
ROOT="$(pwd)"
WORK="$ROOT/work"
rm -rf "$WORK"; mkdir -p "$WORK/repo/partial" "$WORK/stage/totem"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y xorriso dpkg-dev apt-utils curl ca-certificates gzip coreutils

PKGS="python3 python3-flask python3-serial chromium xserver-xorg-core xserver-xorg-input-libinput xserver-xorg-legacy xinit x11-xserver-utils openbox curl libgl1-mesa-dri alsa-utils speech-dispatcher espeak-ng fonts-dejavu-core fonts-noto-color-emoji libpam-systemd dbus-user-session sudo"

echo "== 1/5 Baixando pacotes do totem e dependencias"
apt-get install -y --no-install-recommends --download-only \
  -o Dir::Cache::archives="$WORK/repo" $PKGS
rm -f "$WORK/repo/lock"
( cd "$WORK/repo" && apt-ftparchive packages . > Packages && gzip -kf Packages )
du -sh "$WORK/repo"

echo "== 2/5 Baixando Debian 12 netinst oficial"
ISO_URL=""
for BASE in \
  "https://cdimage.debian.org/cdimage/archive/latest-oldstable/amd64/iso-cd" \
  "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd"; do
  NAME=$(curl -fsSL "$BASE/" 2>/dev/null | grep -o 'debian-12\.[0-9.]*-amd64-netinst\.iso' | head -1 || true)
  if [ -n "$NAME" ]; then ISO_URL="$BASE/$NAME"; BASE_DIR="$BASE"; break; fi
done
[ -n "$ISO_URL" ] || { echo "ERRO: nao achei a ISO netinst do Debian 12. Verifique cdimage.debian.org"; exit 1; }
echo "$ISO_URL"
curl -fL -o "$WORK/netinst.iso" "$ISO_URL"
curl -fsSL "$BASE_DIR/SHA256SUMS" -o "$WORK/SHA256SUMS"
( cd "$WORK" && grep " $NAME\$" SHA256SUMS | sed "s# $NAME# netinst.iso#" | sha256sum -c - )

echo "== 3/5 Ajustando menus de boot (instalacao automatica)"
xorriso -osirrox on -indev "$WORK/netinst.iso" \
  -extract /isolinux "$WORK/isolinux" \
  -extract /boot/grub/grub.cfg "$WORK/grub.cfg"
chmod -R u+w "$WORK/isolinux" "$WORK/grub.cfg"
PARAMS="auto=true priority=critical locale=pt_BR.UTF-8 keymap=br file=/cdrom/preseed/totem.cfg"
for f in "$WORK"/isolinux/*.cfg "$WORK/grub.cfg"; do
  sed -i "s# --- quiet# $PARAMS --- quiet#" "$f"
done
grep -rn "totem.cfg" "$WORK/isolinux" "$WORK/grub.cfg" | head -5 || true
grep -rq "totem.cfg" "$WORK/isolinux" "$WORK/grub.cfg" || { echo "ERRO: nao consegui alterar os menus de boot"; exit 1; }

echo "== 4/5 Montando conteudo extra"
mkdir -p "$WORK/stage/preseed"
cp -r "$WORK/repo" "$WORK/stage/totem/repo"
rm -rf "$WORK/stage/totem/repo/partial"
cp -r "$ROOT/app" "$WORK/stage/totem/app"
cp "$ROOT/instalar_offline.sh" "$WORK/stage/totem/"
cp "$ROOT/preseed.cfg" "$WORK/stage/preseed/totem.cfg"

echo "== 5/5 Gravando totem-os.iso"
xorriso -indev "$WORK/netinst.iso" -outdev "$ROOT/totem-os.iso" \
  -boot_image any replay \
  -map "$WORK/stage/totem" /totem \
  -map "$WORK/stage/preseed" /preseed \
  -map "$WORK/isolinux" /isolinux \
  -map "$WORK/grub.cfg" /boot/grub/grub.cfg \
  -commit
ls -lh "$ROOT/totem-os.iso"
