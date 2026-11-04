"""Exercise the Windows updater's real file replacement against a held file."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(sys.platform == "win32", "Requires Windows file sharing")
class UpdaterLockTests(unittest.TestCase):
    def test_waits_for_exclusive_file_lock_before_replacing(self):
        updater = Path(__file__).resolve().parents[1] / "v8" / "updater.ps1"
        powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0"
        environment = {**os.environ, "PSModulePath": str(powershell / "Modules")}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.bin"
            replacement = root / "replacement.bin"
            waiting = root / "waiting"
            script = root / "test.ps1"
            target.write_bytes(b"old program")
            replacement.write_bytes(b"new program")
            script.write_text(
                r"""param($Updater, $Source, $Target, $Waiting)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Updater, [ref]$tokens, [ref]$errors)
if ($errors) { throw ($errors -join [Environment]::NewLine) }
$replace = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'ReplaceProgramFile'
}, $false)
$lock = [IO.File]::Open($Target, 'Open', 'Read', 'Read')
try {
    $child = Start-Job -ArgumentList $replace.Extent.Text, $Source, $Target, $Waiting -ScriptBlock {
        param($Function, $Source, $Target, $Waiting)
        $ErrorActionPreference = 'Stop'
        . ([scriptblock]::Create($Function))
        function WriteLog($message) { [IO.File]::WriteAllText($Waiting, $message) }
        ReplaceProgramFile $Source $Target
    }
    for ($i = 0; $i -lt 60 -and -not [IO.File]::Exists($Waiting); $i++) {
        if ($child.State -eq 'Failed') { Receive-Job $child }
        Start-Sleep -Milliseconds 150
    }
    if (-not [IO.File]::Exists($Waiting)) { throw 'Replacement did not wait for the lock' }
    if ([IO.File]::ReadAllText($Target) -ne 'old program') { throw 'Locked program changed' }
} finally { $lock.Dispose() }
Receive-Job $child -Wait -AutoRemoveJob
""",
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(powershell / "powershell.exe"), "-NoProfile", "-NonInteractive",
                 "-File", str(script), str(updater), str(replacement), str(target), str(waiting)],
                capture_output=True, text=True, errors="replace", env=environment, timeout=40,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(target.read_bytes(), b"new program")
            self.assertFalse(replacement.exists())
