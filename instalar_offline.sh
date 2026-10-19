#!/bin/bash
# Roda DENTRO do sistema recem-instalado (in-target), sem internet.
set -e
export DEBIAN_FRONTEND=noninteractive
TUSER=totem
PKGS="python3 python3-flask python3-serial chromium xserver-xorg-core xserver-xorg-input-libinput xserver-xorg-legacy xinit x11-xserver-utils openbox curl libgl1-mesa-dri alsa-utils speech-dispatcher espeak-ng fonts-dejavu-core fonts-noto-color-emoji libpam-systemd dbus-user-session sudo"

echo "[totem] instalando pacotes do repositorio local"
echo 'deb [trusted=yes] file:/opt/totem-repo ./' > /etc/apt/totem-local.list
APT="-o Dir::Etc::sourcelist=/etc/apt/totem-local.list -o Dir::Etc::sourceparts=/dev/null"
apt-get $APT update
apt-get $APT install -y --no-install-recommends $PKGS

echo "[totem] usuario e arquivos"
id "$TUSER" >/dev/null 2>&1 || adduser --disabled-password --gecos "Totem" "$TUSER"
usermod -aG dialout,video,audio,input,plugdev "$TUSER" 2>/dev/null || true
mkdir -p /opt/totem /var/lib/totem/chromium
rm -rf /opt/totem/app && cp -r /opt/totem-app-src /opt/totem/app
chown -R root:root /opt/totem
chown -R "$TUSER:$TUSER" /var/lib/totem

cat > /etc/systemd/system/totem.service <<'UNIT'
[Unit]
Description=Servidor do Totem de Armarios
After=local-fs.target

[Service]
User=totem
Environment=TOTEM_DATA_DIR=/var/lib/totem
Environment=TOTEM_HOST=127.0.0.1
WorkingDirectory=/opt/totem/app
ExecStart=/usr/bin/python3 /opt/totem/app/app.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable totem.service

mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<'CONF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin totem --noclear %I $TERM
CONF

cat > "/home/$TUSER/.bash_profile" <<'PROF'
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
  exec startx -- -nocursor >/dev/null 2>&1
fi
PROF

cat > "/home/$TUSER/.xinitrc" <<'XRC'
#!/bin/sh
xset s off
xset -dpms
xset s noblank
openbox-session &
until curl -fs http://127.0.0.1:5000/ >/dev/null 2>&1; do sleep 1; done
PROFILE=/var/lib/totem/chromium
while true; do
  chromium --kiosk --no-first-run --noerrdialogs --disable-infobars \
    --disable-translate --disable-features=TranslateUI \
    --password-store=basic --no-default-browser-check \
    --use-fake-ui-for-media-stream \
    --autoplay-policy=no-user-gesture-required \
    --user-data-dir="$PROFILE" \
    http://127.0.0.1:5000
  sleep 2
done
XRC
chmod +x "/home/$TUSER/.xinitrc"
chown "$TUSER:$TUSER" "/home/$TUSER/.bash_profile" "/home/$TUSER/.xinitrc"

cat > /etc/X11/Xwrapper.config <<'XW'
allowed_users=anybody
needs_root_rights=yes
XW

mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\n' > /etc/systemd/journald.conf.d/volatile.conf
if [ -f /etc/default/grub ]; then
  sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=2/' /etc/default/grub
  update-grub >/dev/null 2>&1 || true
fi

echo "[totem] limpeza e repositorios para atualizacoes futuras"
rm -rf /opt/totem-repo /opt/totem-app-src /etc/apt/totem-local.list /root/instalar_offline.sh
rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb
cat > /etc/apt/sources.list <<'SRC'
deb http://deb.debian.org/debian bookworm main
deb http://deb.debian.org/debian bookworm-updates main
deb http://security.debian.org/debian-security bookworm-security main
SRC
echo "[totem] concluido"
