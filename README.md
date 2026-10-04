# ServerDeck

**Run your game servers on a Windows PC from one web page.** ServerDeck keeps them online, installs
updates (and re-applies mods afterwards), restarts them on a schedule with in-game warnings, backs up
their configs and worlds - with a restore button - and tells your Discord what's happening.

Built-in templates: **SCUM**, **Space Engineers**, **Counter-Strike 2** (Metamod + CounterStrikeSharp),
**Rust** (Oxide). Pure Python standard library - nothing to `pip install`.

## What it does

- **Dashboard** at `http://127.0.0.1:8787`: status, players, CPU/RAM, ports, server FPS or sim speed,
  start / stop / restart / update buttons, live logs (search, follow, download) and an RCON console.
- **Keeps servers online**: starts them at boot (no login needed), restarts crashed ones, pauses on a
  crash loop instead of hammering.
- **Updates with SteamCMD** on its own, re-installs Metamod/CounterStrikeSharp (CS2) or Oxide (Rust)
  after game updates, and protects config files a "validate" would reset.
- **Restart schedules** (e.g. every 6 h) with countdown warnings in chat (CS2/Rust over RCON, Space
  Engineers over its Remote API, SCUM through its own notification file).
- **"Struggling" alarm**: the page turns red when a server stops answering or its tick rate drops, or
  the PC runs out of RAM/CPU.
- **Configs & backups**: a link to every server's config files (view, edit, download). Config files are
  backed up before every start, restart, update and edit (when they changed), daily, and on demand;
  restore all of them or single files. Full backups (worlds + configs) before restarts/updates, with a
  restore button (the current folders are moved aside, never deleted). Keep backups for N days.
- **Discord feed** through a channel webhook: restarts, outages, alarms, announcements (optional
  @everyone), SCUM quest completions and a weekly leaderboard.
- **SCUM extras**: a weekly "Blood Moon" event (horde-heavy settings for a night, switched back at
  dawn), rotating survival tips, player counts and server FPS from its log.

## Install

You need Windows 10/11 or Windows Server, admin rights, and Python 3.11+ (the installer offers to
install it).

1. Download this repository (Code > Download ZIP, or `git clone`) to a folder such as `C:\ServerDeck`.
2. Open PowerShell **as administrator** in that folder and run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```
   It registers the `ServerDeck` scheduled task (starts at boot, elevated, restarts itself if it stops),
   creates `config.json` and a desktop shortcut, and opens the page.
3. In the page, press **Servers**:
   - no SteamCMD yet? press **Download SteamCMD there**;
   - **Add a server**: pick a game, choose an install folder, adjust ports - ServerDeck opens its
     firewall ports (not the RCON port);
   - press **Install** on the new server's card - SteamCMD downloads it;
   - press **Configs** on the card for its config files.

Space Engineers is different: install it, then run `DedicatedServer64\SpaceEngineersDedicated.exe` once to
create your world and its Windows service, and enable the Remote API in its settings.

Run it without the installer: `python serverdeck.py` from an elevated prompt (it can show status without
admin rights, but not control servers).

## Config

Everything lives in `config.json` (created on first start, edited by the Servers dialog). Each server is
one entry - the templates in `templates/` show every option. The most useful ones:

| key | meaning |
|---|---|
| `install_dir`, `exe`, `args` | where it is and how it starts (`{bat:file:VAR}`, `{toml:file:key}`, `{xml:file:Tag}` read secrets from other files) |
| `ports`, `query` | ports that must be listening, Steam query port - "online" needs all of them |
| `rcon` | `{"kind": "source" or "web", "port", "password"}` for console, warnings and clean stops |
| `schedule` | default restart cycle (`every_hours`, `start`, `warn_minutes`) - change it in the UI |
| `config_files` | globs (relative to `install_dir` or absolute) shown under Configs and backed up |
| `backup` | `{"paths": [...], "exclude": [...], "dest": ...}` full backups before restarts/updates |
| `pre_start`, `post_update` | hooks from `hooks.py`, e.g. `["cs2_addons"]`, `["rust_oxide"]` |
| `health` | floors for the struggling alarm (`min_fps_busy`, `min_sim_speed`, ...) |
| `sidecar` | a Python helper started only while the server is online (`dir`, `script`, `port`, `label`) |

`backups` (top level) sets the backup folder and retention, `community` the Discord name and
leaderboard; both are also editable in the UI.

**Write your own template**: copy one in `templates/`, change `fields` (asked in the UI) and the `server`
block - `{field}` placeholders are filled in, `{backup_root}` too.

## Security

- The page has **no login** and listens on `127.0.0.1` only - keep it that way (use Remote Desktop or an
  SSH tunnel to reach it from elsewhere). Don't change `ui.host` to `0.0.0.0` on an open network.
- `config.json` (RCON passwords, tokens), `community.json` (your Discord webhook) and `state.json` stay on
  your PC - `.gitignore` keeps them out of git. Never paste them into issues.

## Updating ServerDeck

`git pull` (or download the ZIP again over the old files - your `config.json` is not in the repository),
then press **Restart ServerDeck** in the page.

## Files

| file | what |
|---|---|
| `serverdeck.py` | the service and web API |
| `web/index.html` | the page |
| `backups.py` | config/full backups, restore, retention |
| `community.py` | Discord feed, events (Blood Moon), SCUM notifications and tips |
| `setupwiz.py`, `templates/` | adding and removing servers |
| `hooks.py` | per-game maintenance (mods after updates, SCUM notifications) |
| `steam.py`, `a2s.py`, `rcon.py`, `se_api.py`, `winproc.py` | SteamCMD, Steam queries, RCON, Space Engineers API, Windows process control |
| `install.ps1` | installer / uninstaller |

## License

MIT - see [LICENSE](LICENSE).
