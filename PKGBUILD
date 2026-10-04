# Maintainer: madky
pkgname=forja
pkgver=0.1.0
pkgrel=1
pkgdesc="Mod manager for Steam and Umbral games on Arch/Hyprland, with Nexus Mods (GTK4/libadwaita)"
arch=('any')
url='https://github.com/madkyp/forja'
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
  install -d "$pkgdir/usr/lib/forja"
  cp -r forja "$pkgdir/usr/lib/forja/"
  find "$pkgdir/usr/lib/forja" -name '__pycache__' -prune -exec rm -rf {} +
  python -m compileall -q -d /usr/lib/forja "$pkgdir/usr/lib/forja"
  install -Dm755 data/forja "$pkgdir/usr/bin/forja"
  install -Dm644 data/dev.madky.Forja.desktop "$pkgdir/usr/share/applications/dev.madky.Forja.desktop"
  install -Dm644 data/dev.madky.Forja.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/dev.madky.Forja.svg"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
