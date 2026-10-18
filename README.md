# ServerDeck

**Run your game servers on a Windows PC from one web page.** ServerDeck keeps them online, installs
updates (and re-applies mods afterwards), restarts them on a schedule with in-game warnings, backs up
their configs and worlds - with a restore button - and tells your Discord what's happening.

Built-in templates (28):
- **Survival / crafting** - 7 Days to Die, ARK: Survival Ascended, ATLAS, Conan Exiles (Enhanced), Empyrion,
  Enshrouded, ICARUS, Myth of Empires, Palworld, Rust (Oxide), SCUM, Sons of the Forest, Sunkenland, The Front,
  Valheim
- **Sandbox / sim** - Space Engineers, Farming Simulator 22, Farming Simulator 25 (the server comes with the
  game and needs its own licence)
- **Shooters** - Counter-Strike 2 (Metamod + CounterStrikeSharp), Counter-Strike: Source, Team Fortress 2,
  Garry's Mod, Day of Defeat: Source, Half-Life 2: Deathmatch, Half-Life Deathmatch: Source,
  Natural Selection 2, Serious Sam 3: BFE, Serious Sam Fusion 2017

SCUM, Space Engineers, CS2 and Rust run on the author's PC; the other templates are checked against each
game's published server documentation and a simulated start, not long-running servers - report what breaks.
(Deadside has no template: its server files are only available to licensed hosting companies.)
Pure Python standard library - nothing to `pip install`.

## What it does

- **Dashboard** at `http://127.0.0.1:8787`: status, players, CPU/RAM, ports, server FPS or sim speed,
  start / stop / restart / update buttons, live logs (search, follow, download) and an RCON console.
- **Keeps servers online**: starts them at boot (no login needed), restarts crashed ones, pauses on a
  crash loop instead of hammering.
- **Updates with SteamCMD** on its own, re-installs Metamod/CounterStrikeSharp (CS2) or Oxide (Rust)
  after game updates, and protects config files a "validate" would reset.
- **Restart schedules** (e.g. every 6 h) with countdown warnings in chat (over RCON or telnet - Source
  games, Rust, ARK / ATLAS, Conan, 7 Days to Die, Empyrion - Space Engineers' Remote API, Palworld's REST
  API, SCUM's own notification file). Games with no way to message players (Valheim, Enshrouded, ...)
  restart without warnings, and their updates wait until nobody is on.
- **"Struggling" alarm**: the page turns red when a server stops answering or its tick rate drops, or
  the PC runs out of RAM/CPU.
- **Server burden**: every template estimates the memory and CPU a full server of its game needs.
  ServerDeck adds up the servers that are turned on and puts up a banner when that's more than the PC has
  left after Windows and your other programs - how much too much, and what each server needs. The
  Add-a-server dialog says whether a new one fits.
- **Configs & backups**: a link to every server's config files (view, edit, download). Config files are
  backed up before every start, restart, update and edit (when they changed), daily, and on demand;
  restore all of them or single files. Full backups (worlds + configs) before restarts/updates, with a
  restore button (the current folders are moved aside, never deleted). Keep backups for N days.
- **Players**: who is on (names on each card), how long each player has been around (first seen, time
  played), kick, ban - from one server or several at once, or their device through a sidecar - and
  admin / moderator roles, each through the game's own lists.
- **Player trends**: a graph on each card of how many were on over the last 24 hours, 48 hours or week,
  with the peak, the average, how many different players and how much of the time it was online.
- **Discord feed** through a channel webhook: restarts, outages, alarms, announcements (optional
  @everyone), SCUM quest completions and a weekly leaderboard.
- **Game chat to Discord** (SCUM, Space Engineers): players asking for help or an admin ping your admins'
  channel, and every chat message can go to a chat-log channel - each on a webhook of its own.
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
   It registers the `ServerDeck` scheduled task (starts at boot, elevated, normal priority, restarts
   itself if it stops), creates `config.json` and a desktop shortcut, and opens the page. (ServerDeck
   also lifts itself to normal priority when it starts: Task Scheduler's default is below normal, and the
   game servers it starts would inherit that.)
3. In the page, press **Servers**:
   - no SteamCMD yet? press **Download SteamCMD there**;
   - **Add a server**: pick a game, choose an install folder, adjust ports - ServerDeck opens its
     firewall ports (not the RCON port);
   - press **Install** on the new server's card - SteamCMD downloads it, and the card shows how far it is
     (percent and GB). Farming Simulator's server comes with the game: on its card, **Install with Steam**
     has your Steam client download it (it must be on the Steam account signed in on this PC), the card shows
     Steam's progress, and ServerDeck then runs it on your desktop next to Steam (Windows has to be logged on;
     ServerDeck registers a "ServerDeck desktop helper" scheduled task for that). Hosting counts as playing it
     on that account. A Giants copy: install it yourself and point the server's folder at it;
   - press **Configs** on the card for its config files.

Space Engineers needs no configurator: its first start copies one of the scenarios that come with it (Star
System, Earth Planet, Empty World, ...) into the instance folder as a new world and writes
`SpaceEngineers-Dedicated.cfg` - name, ports, player cap and the Remote API (random key) ServerDeck uses. It runs
in console mode as a plain process and stops with Ctrl+C, which saves the world. (A server set up earlier as the
configurator's Windows service still works as one: type `win_service`.)

ATLAS needs no ServerGridEditor: the server files come without a `ServerGrid.json`, so its first start makes a
1x1 grid from Grapeshot's own sample (home cell 0,0) with this PC's public address, your ports and the Redis
password, and starts the Redis database that comes with the server.

Palworld and Enshrouded need nothing extra - ServerDeck writes their settings file before the first start:
Palworld's `PalWorldSettings.ini` (name, port, player cap, and its REST API switched on for this PC only),
Enshrouded's `enshrouded_server.json` (name, port, slots and a password per group). Change them later under
**Configs**. Palworld alone wants about 16 GB of RAM.

Run it without the installer: `python serverdeck.py` from an elevated prompt (it can show status without
admin rights, but not control servers).

## Using ServerDeck

Open `http://127.0.0.1:8787` (the desktop shortcut). Each server has a card; the buttons along the top
work across servers. Drag a card by its ⋮⋮ handle to move it, and use ▾ to collapse it to one line - the
layout is kept by ServerDeck, so every browser shows the same.

**On a server's card**
- **Start / Stop** - Start also means "keep it online" (it is brought back if it crashes); Stop keeps it
  off. The **keep online** and **auto-update** switches change that without starting or stopping it. A
  server that isn't installed yet isn't started (and isn't news for Discord) until it is.
- **Restart** - now, or after warning players for 1-30 minutes (skipped if nobody is on).
- **Update now** (**Install** for a new server) - backs it up, runs SteamCMD with file validation,
  re-applies its mods, starts it again if it was running. With auto-update on, ServerDeck checks
  every 30 minutes and does this by itself - but a server that can't warn players in game (SCUM,
  Enshrouded) isn't updated while people are on: the update waits until it has been empty for a
  minute, or for its next scheduled restart (which does warn them), 6 hours at most without a
  schedule. Players whose game has already updated can't join until then.
- **Players** (or click a name under **Online**) - see below.
- The **Players graph** shows how many were on over the last **24 h**, **48 h** or **Week** (the buttons
  next to it switch every card at once): the most players at a time in each 5 / 10 / 30 minutes, red where
  the server was offline, blank where nothing was recorded. Point at it for the details. Above it: the
  peak, the average while online, how many different players were on and how much of the time it was
  online. Bots don't count. ServerDeck notes the count every few seconds (`trends.json`, 8 days); SCUM's
  graph is filled in from its login log for the time before that.
- **Schedule** - restart every N hours from a start time, warning players N minutes ahead.
- **Logs** (or **Logs & console**) - every log of that server: search, errors-only filter, live follow,
  download. Servers with RCON get a command line under the log.
- **Configs** - its config files and backups (below).
- The chips show players, server FPS or sim speed, open ports, mods, the sidecar and upcoming events.
  A red card (and a red page) means it is struggling - the reason is written on it.
- **Burden** - the memory and CPU the server needs with every player slot taken (point at it for how
  that's worked out). "Uses more than estimated" means it really does, and that's what counts.

**Server burden** - the top bar shows what the servers that are turned on (keep online ticked, or running)
need of what this PC has for them; more than that brings up a banner with the difference and the list.
- A server's burden is its template's estimate for its player slots: memory = `ram_gb` +
  `ram_gb_per_player` x slots, CPU = `cpu_cores` + `cpu_cores_per_player` x slots (cores kept busy). The slots
  are what the server reports while it runs (remembered for while it's off), else its `max_players`, else
  the template's. A running server counts with what it really uses when that's more: its memory now, with
  any processes it started, and its CPU averaged over the last hour.
- What the PC has for servers: its RAM and logical processors minus what everything else uses - Windows,
  Steam, browsers - measured, the median of the last 30 minutes (so opening a program for a moment doesn't
  change it).
- The estimates are rough and assume a full server; worlds, maps and mods change them a lot. Put your own
  numbers in a server's config entry (`burden`, below) if you know better.

**Configs & backups**
- **Config files** - every file in the server's `config_files` list: **View / edit** (saving backs the
  files up first) or **Download**. Restart the server afterwards; servers read their config at start.
- **Config backups** - made automatically before every start, restart, update, edit and restore (only
  when something changed), every day, and with **Back up configs now**. **Restore...** lets you pick
  files and optionally stops and starts the server around it. A restore backs up the current files
  first, so it can be undone.
- **Full backups** - worlds + configs, made before restarts and updates (**Full backup now** makes one
  any time). **Restore...** stops the server, moves the current folders aside
  (`<folder>.before-restore-<time>`, nothing is deleted), unpacks the backup and starts it again -
  players lose what happened since that backup.
- **Retention** - keep backups for N days; the newest few of each kind always stay.

**Players**
- **Online** - who is on, this visit, how long they have been a member (since first seen, in years /
  months / days / hours) and their time played, with **Kick**, **Ban...** and their role.
- **All players** - everyone seen on that server, newest first. ServerDeck notes everyone it sees online
  (every 30 s, kept in `players.json`); SCUM's own database supplies first seen and time played from
  before that.
- **Admins & moderators** - who has a role; change it in the list or give one by SteamID64.
- **Bans** - bans made in ServerDeck on any server; **Lift** undoes one everywhere. **Ban...** asks for a
  reason and which servers - a ban for a server that is offline is applied as soon as it is back - and,
  where a sidecar offers it, a device ban (their PC, not just the account).

| game | who is on | ban | kick | admin / moderator |
|---|---|---|---|---|
| SCUM | its login log | `BannedUsers.ini` - from its next restart (it has no remote console) | - | `AdminUsers.ini` (admin `[SetGodMode, ShutdownServer]`, moderator the standard commands) - from its next restart |
| Space Engineers | Remote API | Remote API - right away | yes | admin: `<Administrators>` in its .cfg, written while it is stopped (it rewrites that file while running); moderators in-game |
| Counter-Strike 2 | RCON `status_json` | `banid` + `writeid` - right away, and again after every start (CS2 forgets them) | yes | CounterStrikeSharp `admins.json` (flags: `role_flags`) - from its next start |
| Rust | RCON `playerlist` | `banid` - right away | yes | `ownerid` / `moderatorid` - right away (`users.cfg` while stopped) |
| Palworld | REST API | REST API - right away (Steam players; crossplay console players in game) | Steam players | - (admins use the admin password in game) |
| Enshrouded | Steam query - names only | - (in game) | - | - (the group passwords) |
| CS:S, TF2, Garry's Mod, DoD:S, HL2:DM, HLDM:S | RCON `status` | `banid` + `writeid` - right away, and again after every start | yes | - (RCON password, or SourceMod / ULX files under Configs) |
| ARK: Survival Ascended | RCON `ListPlayers` (Epic ids) | in its RCON console (`BanPlayer <id>`) | in its RCON console | - (`AllowedCheaterAccountIDs.txt`) |
| ATLAS | RCON `ListPlayers` | `BanPlayer` - right away | yes | - (`AllowedCheaterSteamIDs.txt`) |
| 7 Days to Die | telnet `listplayers` | `ban add` - right away | yes | - (`serveradmin.xml`) |
| Conan Exiles | RCON `listplayers` | - (admin panel in game) | - | - (admin password) |
| Valheim, Sons of the Forest, ICARUS, Empyrion, NS2 | Steam query - names only | - (the game's own ban list / admin tools) | - | - (its admin list file, under Configs) |
| The Front, Myth of Empires, Sunkenland, Serious Sam 3 / Fusion, Farming Simulator | - (no query ServerDeck can use) | - | - | - |

**Servers** - add a server from a template, remove one (its files stay on disk), set the SteamCMD path
(or download SteamCMD) and the backups folder. ServerDeck restarts itself to load changes. Each game shows
its burden at the player slots you enter, and whether that fits next to the servers already on.

**Community**
- **Discord**: paste a channel webhook URL (channel settings > Integrations > Webhooks > New Webhook >
  Copy Webhook URL) and press Connect. Choose what gets posted - restarts and updates, struggling alarms,
  events, quest completions, the weekly leaderboard - and for which servers.
- **Announcement**: write a post (Discord formatting works), optionally ping @everyone, press Post.
- **Game chat → Discord** (SCUM reads its chat log, Space Engineers its Remote API):
  - *Admin alerts*: a chat message with one of the alert words (`admin*`, `help`, `stuck`, `cheat*`, ... -
    whole words, any case, `*` = any ending; edit the list and try messages in the dialog) is posted to
    your admins' channel with a ping (`@here` by default, or `@everyone`, a role `<@&id>`, a person
    `<@id>`; one ping per player per 5 minutes) and shows up in the Activity list. Messages from players on
    the server's admin list, SCUM's Admin channel and commands (`#...`, `/...`) don't raise alerts.
  - *Chat log*: every message, a few lines per post.
  - Each needs **its own webhook in its own channel**, apart from the server-news one: ServerDeck refuses a
    webhook another of the three uses, or one that posts into the same channel. Player text is posted as
    plain text - no pings, formatting tricks or link previews.
- **Blood Moon** (SCUM): turn it on and pick the night; ServerDeck restarts SCUM into the horde settings
  and back out at the end, warning players before.
- **Survival tips** (SCUM): rotating messages in game every N minutes - edit the list in config.json.

**Restart ServerDeck** reloads ServerDeck itself (game servers keep running); it waits if a server is in
the middle of something. The **Activity** list at the bottom shows everything ServerDeck did. An open
page reloads itself when ServerDeck's page has been updated (or offers to, if a dialog is open).

Handy links: `#logs=<server>`, `#backups=<server>`, `#players=<server>`, `#servers`, `#community` after
the address, e.g. `http://127.0.0.1:8787/#backups=scum`.

## Config

Everything lives in `config.json` (created on first start, edited by the Servers dialog). Each server is
one entry - the templates in `templates/` show every option. The most useful ones:

| key | meaning |
|---|---|
| `install_dir`, `exe`, `args` | where it is and how it starts (`{bat:file:VAR}`, `{toml:file:key}`, `{xml:file:Tag}` read secrets from other files) |
| `ports`, `query` | ports that must be listening, Steam query port - "online" needs all of them |
| `name_setting` | for servers without a Steam query (SCUM): where to read the server name shown on the card, e.g. `{"file": "...\\ServerSettings.ini", "key": "scum.ServerName"}` |
| `rcon` | `{"kind": "source", "web" or "telnet", "port", "password"}` for console, warnings and clean stops; a game's own commands go in `"say"` (default `"say {msg}"`, e.g. ARK `"ServerChat {msg}"`) and `"stop"` (default `["quit"]`, e.g. `["SaveWorld", "DoExit"]`) |
| `env` | extra environment variables for the server process, e.g. Valheim's `{"SteamAppId": "892970"}` |
| `rest_api` | Palworld: `{"port", "password"}` of its REST API - players, server FPS, warnings, save + clean stop (`stop_method`/`announce`/`perf` `palworld_rest`) |
| `stop_method` | `rcon` (quit), `ctrl_c`, `palworld_rest` or `terminate` (the default) |
| `schedule` | default restart cycle (`every_hours`, `start`, `warn_minutes`) - change it in the UI |
| `config_files` | globs (relative to `install_dir` or absolute) shown under Configs and backed up |
| `backup` | `{"paths": [...], "exclude": [...], "dest": ...}` full backups before restarts/updates |
| `pre_start`, `post_update` | hooks from `hooks.py`, e.g. `["cs2_addons"]`, `["rust_oxide"]`, or `["settings", {...}]` (below) |
| `appid` | the SteamCMD app; leave it out for a server installed some other way (Farming Simulator's dedicated server comes with the game) - ServerDeck then only runs it |
| `health` | floors for the struggling alarm (`min_fps_busy`, `min_sim_speed`, ...) |
| `sidecar` | a Python helper started only while the server is online (`dir`, `script`, `port`, `label`). If it has an admin API - `GET /players`, `POST /ban` `{steam_id, ban_type, scope, reason_code, details}`, `POST /unban` `{ban_id}`, header `X-Admin-Key` = `admin_api_key` in its `config.toml` or an `admin_key` setting here - **Ban...** offers a device ban |
| `role_flags` | CS2: CounterStrikeSharp flags per role, e.g. `{"admin": ["@css/root", "@myplugin/admin"]}` (default admin `@css/root`, moderator kick/ban/chat flags) |
| `players` | where the player list comes from, if not the obvious one: `scum_log`, `se_remote`, `cs2_rcon`, `srcds_rcon`, `rust_rcon`, `palworld_rest`, `ark_rcon`, `asa_rcon`, `7dtd_telnet`, `conan_rcon` or `a2s`; `player_notes` (`{"ban", "roles"}`) says how the game does bans and admins, `max_players` the cap when there is no Steam query |
| `process` | the server's real process when `exe` is only a launcher that starts it (Conan Exiles, ICARUS) - an image name or pattern inside `install_dir` |
| `exe_fallback` | started instead of `exe` when that isn't there (`srcds.exe` for `srcds_win64.exe`) |
| `cwd` | working folder relative to `install_dir` (default: the exe's folder) |
| `raw_args` | `true`: `args` go on the command line exactly as written - Unreal's `-Key="Value with spaces"` needs that |
| `extra_ports` | ports opened in the firewall but not required for "online" (Farming Simulator's game port) |
| `burden` | your own numbers for its server burden, over the template's (all or some): `{"ram_gb": 10, "ram_gb_per_player": 0.05, "cpu_cores": 2, "cpu_cores_per_player": 0.03}`; `max_players` is the slot count used until the server has reported its own |

`backups` (top level) sets the backup folder and retention, `community` the Discord name and
leaderboard; both are also editable in the UI.

**Write your own template**: copy one in `templates/`, change `fields` (asked in the UI) and the `server`
block - `{field}` placeholders are filled in, `{backup_root}`, `{userprofile}` and `{documents}` too, and
`{port+1}` adds to a number field. In `args`, an empty optional setting is left out (`"-password", ""` or
`"-Name={field}"`). A field can be `"required": true`. `category` puts the template in a group of the
Add-a-server list. `burden` estimates a full server of the game (`ram_gb`, `ram_gb_per_player`,
`cpu_cores`, `cpu_cores_per_player`, `players` = slots when nothing else says, optional `note`); put the slot
field into the server block as `"max_players": "{max_players}"` so the server records it.

**Settings files before a start** - `["settings", {"file", "format", "seed", "enforce", "create" | "copy"}]` in
`pre_start` edits a game's settings file in place (comments, order and encoding stay as they are):
`format` is `ini` (`"Section/Key"`), `cfg` (Source `name "value"`), `xmlprop` (7 Days to Die
`<property name= value=>`), `xml` (`"a/b"` text, `"a/b@attr"`), `json` (`"a.b"`) or `yaml` (`Key: value`,
`"Section/Key"` to add one). `seed` is written once, on the first start (server name, slots, world - the file
is yours afterwards); `enforce` before every start (what ServerDeck relies on: ports, RCON). `create` gives
the text of a file that doesn't exist yet, `copy` a default file to start from.

## Security

- The page has **no login** and listens on `127.0.0.1` only - keep it that way (use Remote Desktop or an
  SSH tunnel to reach it from elsewhere). Don't change `ui.host` to `0.0.0.0` on an open network.
- `config.json` (RCON passwords, tokens), `community.json` (your Discord webhooks), `players.json` (players'
  names, Steam IDs, bans), `trends.json` and `state.json` stay on your PC - `.gitignore` keeps them out of
  git. Never paste them into issues.

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
| `chat.py` | game chat: admin alerts and a chat log, each to its own Discord webhook |
| `players.py` | who is on, first seen / time played, kicks, bans, admin and moderator roles |
| `trends.py` | player counts over time for the graphs |
| `burden.py` | server burden: the memory and CPU the servers that are on need, against what the PC has |
| `setupwiz.py`, `templates/` | adding and removing servers |
| `hooks.py` | per-game maintenance (mods after updates, SCUM notifications, Palworld/Enshrouded settings files, the generic `settings` step) |
| `cfgedit.py` | in-place editors for settings files (ini, Source cfg, xml, json, yaml) |
| `steam.py`, `a2s.py`, `rcon.py`, `se_api.py`, `palworld.py`, `winproc.py` | SteamCMD, Steam queries, RCON, Space Engineers and Palworld APIs, Windows process control |
| `install.ps1` | installer / uninstaller |

## License

MIT - see [LICENSE](LICENSE).
