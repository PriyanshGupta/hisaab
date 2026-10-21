# Operating Hisaab

How the app is kept safe and kept running: backups and restore, exporting, the service that starts it, and
reaching it from a phone. Commands run inside WSL from the project folder unless they say PowerShell.

## Backups

The app backs up its database by itself (tech-design §9.2):

- **daily**: checked at startup and every 6 hours, taken when the newest backup is a day old;
- **before every migration**: `scripts/run.sh` runs `python -m hisaab backup --before-migrating` first, and
  doesn't start the app if that backup fails;
- **before every re-sync**: the re-sync is refused if its backup can't be taken.

Each backup is a complete, consistent copy made with SQLite's `VACUUM INTO`, named for when and why it was taken:

    data/backups/hisaab-2026-10-21T073000Z-daily.db
    data/backups/hisaab-2026-10-21T081534Z-pre-resync.db

The newest 30 are kept and older ones deleted. Only files with this name pattern are ever deleted.

**Where:** `HISAAB_BACKUP_DIR` in `.env`, default `data/backups`. Point it at synced or external storage if you
can. A OneDrive folder such as `/mnt/c/Users/<you>/OneDrive/Hisaab backups` works: backups are finished files,
so they may live on the Windows side. The live database (`data/hisaab.db`) must stay on the Linux filesystem.

Take one by hand, or list them, newest first:

    .venv/bin/python -m hisaab backup
    .venv/bin/python -m hisaab backups

## Restore

Restore is manual on purpose: an automatic restore could happen by accident. It replaces everything since the
backup was taken.

1. List the backups and pick one:

       .venv/bin/python -m hisaab backups

2. Stop the app:

       systemctl --user stop hisaab

3. Move the current database aside together with its `-wal` and `-shm` files, under one name (they belong
   together: the `-wal` can hold committed changes, needed if you ever undo this restore). Then copy in the
   backup, using the path `hisaab backups` printed:

       mkdir -p data/replaced
       ts=$(date +%Y%m%d-%H%M%S)
       mv data/hisaab.db "data/replaced/hisaab-$ts.db"
       [ -f data/hisaab.db-wal ] && mv data/hisaab.db-wal "data/replaced/hisaab-$ts.db-wal"
       [ -f data/hisaab.db-shm ] && mv data/hisaab.db-shm "data/replaced/hisaab-$ts.db-shm"
       cp "<the backup path you picked>" data/hisaab.db

4. Start the app:

       systemctl --user start hisaab

If the backup is from an older version, the start migrates it (taking a pre-migration backup first). To undo the
restore, repeat these steps with the files in `data/replaced/` (all three of one timestamp). Delete
`data/replaced/` once you're sure.

## CSV export

On the Report page, **Download CSV** saves the period on screen: your own transactions, as the app shows them now
(edits applied, categories by today's rules). Two exports a month apart can differ if you changed a category
rule in between (tech-design §9.7).

Columns: Date, Time, Type, Amount (rupees, two decimals), From account, To account, Payee, Text from the bank,
Category, Status, Reference. The file is UTF-8 and opens in Excel, Google Sheets or LibreOffice.

## Running as a service

Hisaab runs as a systemd **user** service inside WSL, so it comes back after a reboot:

    scripts/install-service.sh            # install or update, enable, (re)start
    systemctl --user status hisaab        # is it running?
    journalctl --user -u hisaab -f        # its log
    systemctl --user restart hisaab       # after pulling an update

Three things make it start with no terminal open:

1. **Linger**, once, so the user service starts at boot without a login:

       sudo loginctl enable-linger "$USER"

2. **WSL starting when you log in to Windows.** One scheduled task, in PowerShell:

       schtasks /Create /TN "Hisaab WSL" /SC ONLOGON /TR "wsl.exe -d Ubuntu --exec /bin/true" /F

3. **WSL staying up with no terminal open:** `vmIdleTimeout=-1` and `instanceIdleTimeout=-1` in
   `C:\Users\<you>\.wslconfig` (template: `scripts/windows/wslconfig.txt`). If WSL still stops by itself, the
   fallback is a logon task that keeps one hidden `wsl.exe` running:

       schtasks /Create /TN "Hisaab WSL" /SC ONLOGON /TR "wsl.exe -d Ubuntu --exec sleep infinity" /F

## Phone access

WSL2 normally sits behind its own network address, so a phone can't reach it (tech-design §9.5). Two paths:

**1. Mirrored networking (Windows 11; tried first).** WSL shares the PC's addresses.

1. Put `scripts/windows/wslconfig.txt` at `C:\Users\<you>\.wslconfig`.
2. In PowerShell, run `wsl --shutdown`, then open WSL again (the service starts).
3. Let the phone in. In an **administrator** PowerShell, run this once. Mirrored mode has its own firewall for
   WSL, separate from the normal Windows firewall:

       New-NetFirewallHyperVRule -Name Hisaab -DisplayName "Hisaab 8765" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 8765

4. On the phone, on the same Wi-Fi, open `http://<the PC's LAN address>:8765`. `ipconfig` in PowerShell shows
   the address; with mirrored networking, `ip addr` in WSL shows the same one.

**2. Port proxy (fallback, Windows 10 too).** Undo mirrored mode, then forward port 8765 to WSL's address. That
address changes on every reboot, so `scripts/windows/hisaab-portproxy.ps1` refreshes the forward and opens the
firewall once. Run it now and at every logon, elevated:

    schtasks /Create /TN "Hisaab port proxy" /SC ONLOGON /RL HIGHEST /TR "powershell -ExecutionPolicy Bypass -File \\wsl$\Ubuntu\<path to this project>\scripts\windows\hisaab-portproxy.ps1" /F

**What worked on this PC:** to be filled in after the slice 25 phone check.

## Security

- The app listens on every network address (`0.0.0.0:8765`) so the phone can reach it. Signing in protects it:
  every screen and API needs a session, and passwords are stored as Argon2 hashes.
- It speaks plain HTTP. That is acceptable **only on a trusted home network** (tech-design §9.6). **Never forward
  port 8765 on your router** or expose it to the internet any other way.
- Gmail tokens and account numbers are encrypted with a key kept in `.env`, outside the database, so a copied
  database or backup alone doesn't reveal them. Keep `.env` out of synced folders and backups.
