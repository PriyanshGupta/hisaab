"""The operating files (tech-design §9.2, §9.4, §9.5): the systemd user service, its installer, the Windows
helpers, and OPERATIONS.md. Static checks: what they must say and must never do."""

import re
import subprocess
from pathlib import Path

from hisaab.config import PROJECT_ROOT

SCRIPTS: Path = PROJECT_ROOT / "scripts"


def test_the_unit_runs_run_sh_restarts_on_failure_and_starts_with_the_user_manager() -> None:
    unit: str = (SCRIPTS / "hisaab.service").read_text()
    assert re.search(r"^ExecStart=@ROOT@/scripts/run\.sh$", unit, re.M)
    assert re.search(r"^WorkingDirectory=@ROOT@$", unit, re.M)
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit
    assert "ANTHROPIC_API_KEY" not in unit and "EnvironmentFile" not in unit  # .env is read by the app, never exported


def test_the_installer_uses_the_user_manager_only_and_parses() -> None:
    installer: str = (SCRIPTS / "install-service.sh").read_text()
    assert "systemctl --user" in installer and "sudo" not in installer.replace("sudo loginctl enable-linger", "")
    for script in ("install-service.sh", "run.sh"):
        assert subprocess.run(["bash", "-n", str(SCRIPTS / script)], capture_output=True).returncode == 0, script


def test_the_wslconfig_turns_on_mirrored_networking() -> None:
    conf: str = (SCRIPTS / "windows" / "wslconfig.txt").read_text()
    assert re.search(r"^\[wsl2\]$", conf, re.M) and re.search(r"^networkingMode=mirrored$", conf, re.M)


def test_the_port_proxy_fallback_refreshes_the_address_and_opens_the_port() -> None:
    ps: str = (SCRIPTS / "windows" / "hisaab-portproxy.ps1").read_text()
    assert "netsh interface portproxy" in ps and "8765" in ps and "New-NetFirewallRule" in ps


def test_operations_md_documents_every_procedure() -> None:
    doc: str = (PROJECT_ROOT / "OPERATIONS.md").read_text()
    for heading in ("## Backups", "## Restore", "## CSV export", "## Running as a service", "## Phone access",
                    "## Security"):
        assert heading in doc, heading
    restore: str = doc.split("## Restore", 1)[1].split("\n## ", 1)[0]
    assert "systemctl --user stop hisaab" in restore and "systemctl --user start hisaab" in restore
    assert "-wal" in restore and "python -m hisaab backups" in restore


def test_a_failing_start_gives_up_instead_of_looping() -> None:
    """Review: with RestartSec=10 the default start limit never trips, so a failing start would retry forever."""
    unit: str = (SCRIPTS / "hisaab.service").read_text()
    head: str = unit.split("[Service]", 1)[0]
    assert re.search(r"^StartLimitIntervalSec=\d+$", head, re.M) and re.search(r"^StartLimitBurst=3$", head, re.M)


def test_the_restore_moves_the_replaced_database_with_its_wal_under_one_name() -> None:
    """Review: renaming the main file but not its -wal/-shm separated them, losing committed data if the restore
    had to be undone; and the copy line assumed the default backup folder."""
    restore: str = (PROJECT_ROOT / "OPERATIONS.md").read_text().split("## Restore", 1)[1].split("\n## ", 1)[0]
    assert 'ts=$(date +%Y%m%d-%H%M%S)' in restore
    for suffix in ("", "-wal", "-shm"):
        assert f'data/hisaab.db{suffix} "data/replaced/hisaab-$ts.db{suffix}"' in restore, suffix
    assert "the path `hisaab backups` printed" in restore
