# Maintainer: madky
pkgname=crisol
pkgver=0.1.0
pkgrel=1
pkgdesc="Mod manager for Steam and Umbral games on Arch/Hyprland, with Nexus Mods (GTK4/libadwaita)"
arch=('any')
url='https://github.com/madkyp/crisol'
license=('MIT')
depends=('python' 'python-gobject' 'python-requests' 'gtk4' 'libadwaita' 'libsecret' 'libarchive'
         'desktop-file-utils' 'xdg-utils')
optdepends=('umbral: detect games from the Umbral launcher'
            'steam: detect Steam games')
# Built from the project tree (clone the repo, then run makepkg -si).
source=()

check() {
  cd "$startdir"
  python -m unittest discover -s tests -q
}

package() {
  cd "$startdir"
  install -d "$pkgdir/usr/lib/crisol"
  cp -r crisol "$pkgdir/usr/lib/crisol/"
  find "$pkgdir/usr/lib/crisol" -name '__pycache__' -prune -exec rm -rf {} +
  python -m compileall -q -d /usr/lib/crisol "$pkgdir/usr/lib/crisol"
  install -Dm755 data/crisol "$pkgdir/usr/bin/crisol"
  install -Dm644 data/dev.madky.Crisol.desktop "$pkgdir/usr/share/applications/dev.madky.Crisol.desktop"
  install -Dm644 data/dev.madky.Crisol.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/dev.madky.Crisol.svg"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
