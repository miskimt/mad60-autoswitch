# MAD60 HE auto profile switcher

Picks your keyboard profile automatically based on which game is running.

**How it works.** The MadLions driver is a web page, so only the browser can talk to the keyboard.
`kb_autoswitch.py` watches for game processes; a tiny browser extension (in `extension/`) sits inside
the driver page and clicks the matching profile in its sidebar. No mouse hijacking, no focus stealing,
works with the driver window minimised.

| Game | Process | Profile selected |
|---|---|---|
| Valorant | `VALORANT-Win64-Shipping.exe` | `valo2` |
| League of Legends | `League of Legends.exe` (in-match) | `lol` |
| Valheim | `valheim.exe` | `valheim` |
| anything else | - | `normal` (15 s after a game closes) |

Profile names must match the sidebar in the driver (case doesn't matter). Edit `config.json` if yours differ.

## One-time setup

1. **Import** `lol` and `valheim` into the driver so they appear under *Device Profiles*.
2. **Python 3.9+** from python.org (tick "Add python.exe to PATH"). No extra packages needed.
3. **Load the extension** in the same browser the driver app is installed from:
   `chrome://extensions` (or `edge://extensions`) -> Developer mode -> **Load unpacked** -> pick the `extension` folder.
4. **Tell it the driver's address.** In the driver window, click the lion icon at the top-left of the title bar
   (or the three-dot menu) to see the site address, then run:
   `python kb_autoswitch.py --set-driver-url https://hub.fgg.com.cn
   Then click the refresh icon on the extension's card, and close/reopen the driver window.
5. **Test it.** Run `python kb_autoswitch.py --force lol`. Within a few seconds the driver window should show a
   small "MAD60 auto-profile: lol" toast, the sidebar selection should move to `lol`, and your keyboard lighting
   should change (gold QWER, teal D/F). Then `--force normal` to go back. Ctrl+C stops it.
   - Toast appears but the keyboard doesn't change? Selecting a profile may only *load* it in the page. Put the
     text of the driver's apply/save button in `"apply_button_text"` in `config.json`.
6. **Run it for real.** Double-click `run.bat`, or run `python kb_autoswitch.py --install-startup`
   to start it hidden every time you sign in (`--remove-startup` undoes that).

## Day to day

Keep the driver window open (minimised is fine) with the keyboard plugged in. Launch a game and the profile
follows. If a game is launched while the driver is closed, the profile is applied the next time you open it.
Opening the driver to tweak settings never overrides your manual choice.

## Adding a game

Launch it, run `python kb_autoswitch.py --list`, find its `.exe`, and add an entry to `config.json`:

    { "name": "Elden Ring", "profile": "normal", "processes": ["eldenring.exe"] }

If two games are open, the most recently launched one wins.
Want the LoL profile during champion select too? Add `"LeagueClientUx.exe"` to its processes.

## Troubleshooting

- Open `http://127.0.0.1:47831/status` - shows the wanted profile, whether the driver page is connected, and the
  last result. Log file: `kb_autoswitch.log`.
- `driver_page_connected: false` -> extension not loaded, wrong address in step 4, or the driver window wasn't reopened.
- `no "xyz" entry found` -> the profile isn't imported, or its sidebar name differs from `config.json`.
- Port 47831 busy -> change `port` in `config.json` **and** `HELPER` in `extension/background.js`.
