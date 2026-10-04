# Forja

Mod manager for **Steam** and **[Umbral](https://github.com/madkyp/umbral-project)** games on Arch / CachyOS, with **Nexus Mods** as the source. GTK4 + libadwaita. Personal project, alpha.

- **Library**: detects installed Steam games (every library in `libraryfolders.vdf`) and Umbral games (`~/.config/umbral/config.json`), and shows the ones that have mods on Nexus Mods.
- **Search**: Nexus Mods search per game (relevance, most downloaded, top endorsed, updated, newest), with mod details, requirements and files.
- **Install**: Premium accounts download directly. Free accounts start from the website (*Mod Manager Download* / *Slow download*) and the browser hands the `nxm://` link to Forja, which downloads, verifies (size + md5 against Nexus) and extracts it. Archives downloaded by hand can be imported too.
- **Load order**: drag and drop, enable/disable, profiles per game, file conflicts showing which mod wins (and duplicates inside KCD2 `.pak` files), update checks that keep each mod's position.
- **Reversible**: mods are applied with reflinks (btrfs/xfs; hard links or copies elsewhere). Overwritten originals go to a backup and *Restore game without mods* removes exactly what Forja placed.

## Game types

| Type | Games | How mods are applied and ordered |
|---|---|---|
| Kingdom Come: Deliverance II | KCD2 | One folder per mod in `mods/`; order written to `mods/mod_order.txt` (also a whitelist; hand-installed mods are kept at the end) |
| Unreal Engine | Lies of P, Khazan, Lords of the Fallen… | `.pak/.ucas/.utoc` go to `<Project>/Content/Paks/~mods` renamed `001_`, `002_`… by order; other files relative to the game root |
| Loose files | Unity / BepInEx / MelonLoader and the rest | Files on top of the game folder; the lower mod in the list wins a conflict |

The type is detected automatically and can be changed in *⋯ → Game settings* (installed mods are remapped without downloading again).

## Install

```sh
git clone https://github.com/madkyp/forja && cd forja
makepkg -si
```

Dependencies: `python python-gobject python-requests gtk4 libadwaita libsecret libarchive xdg-utils`.

## Setup

1. *Menu → Preferences → Nexus Mods*: paste your personal API key (nexusmods.com → Preferences → API). It is stored in the system keyring (libsecret), never in a file.
2. Same dialog: *nxm:// links → Use Forja*, so the browser sends download links to Forja.
3. Open a game, search a mod, press *Download*, then *Slow download* on the website. When it finishes, press **Apply mods**.

## Command line

| Command | |
|---|---|
| `forja --list` | Detected games, their Nexus game and mod count, as JSON |
| `forja --game <key>` | Open a game's page directly |
| `forja --restore <key>` | Remove a game's mods without opening the window |
| `forja --debug` | Detailed log in the terminal (always written to `~/.local/state/forja/logs/forja.log`) |

## Files

| Path | |
|---|---|
| `~/.config/forja/config.json` | Settings, manual game ↔ Nexus links |
| `~/.local/share/forja/games/` | Installed mods, profiles and load order per game |
| `~/.local/share/forja/staging/` | Extracted mods |
| `~/.local/share/forja/downloads/` | Downloaded archives |
| `~/.local/share/forja/deploy/`, `backups/` | What is applied to each game and the originals it replaced |

## Mod sources

| Source | Status | Why |
|---|---|---|
| Nexus Mods | ✅ Integrated | Official API: GraphQL v2 (search, no key) + REST v1 (downloads, personal key; non-Premium via `nxm://` links) |
| Thunderstore | ⏸ Not added | Public API, but none of the detected games has a Thunderstore community |
| Steam Workshop | ⏸ Not added | None of the detected games uses the Workshop |
| CurseForge | ⏸ Not added | Requires an approved API key; only relevant for WoW addons |
| ModDB | ❌ Discarded | No public API (only scraping) |

Forja follows the [Nexus Mods API acceptable use policy](https://help.nexusmods.com/article/114-api-acceptable-use-policy): it sends `Application-Name`/`Application-Version`, uses the user's own key and does not mirror data. A public release would need registering the app with Nexus Mods (SSO).

## License

MIT
