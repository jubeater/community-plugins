"""Run with python3 -m unittest discover -s screen-toolkit/tests.

Uses the real capture script and ImageMagick with simulated Noctalia IPC.
The shim follows screenshot_service.cpp's filename suffix and routing rules;
it does not render Wayland selections or the annotation editor.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/shell-capture.sh"
FLAGS = (
    "--save-to-file --no-copy-to-clipboard --no-pipe-to-command "
    "--no-annotate --no-confirm-region --directory --filename-pattern --format"
)
SHIM = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, subprocess, sys
a = sys.argv[1:]
root = pathlib.Path(os.environ['TEST_ROOT'])
with (root/'calls.jsonl').open('a') as f:
    f.write(json.dumps(a)+'\n')
if a == ['config', 'export', 'full']:
    if 'EXPORTED_DIR' not in os.environ:
        sys.exit(1)
    print('[shell.screenshot]\ndirectory = '+json.dumps(os.environ['EXPORTED_DIR']))
    sys.exit(0)
if '--help' in a:
    if a == ['msg', '--help']:
        print('  annotate  Edit an image')
    elif os.environ.get('LEGACY') != '1':
        print(os.environ['HELP_FLAGS'])
    sys.exit(0)
cmd = a[1]
if cmd == 'annotate':
    sys.exit(0 if pathlib.Path(a[2]).is_file() else 1)
def val(flag, fallback):
    return a[a.index(flag)+1] if flag in a else fallback
if os.environ.get('CONFIRM_COPY') == '1' and '--no-confirm-region' not in a:
    print('ok')  # Core ForceClipboard disables saving despite --save-to-file.
    sys.exit(0)
if os.environ.get('NO_SAVE') == '1' and '--save-to-file' not in a:
    print('ok')
    sys.exit(0)
directory = val('--directory', os.environ['SHOT_DIR'])
pattern = val('--filename-pattern', 'screenshot')
# Core formatFilenameStem appends labelBase unless it is 'screenshot'.
label = '-region' if cmd == 'screenshot-region' else ''
out = pathlib.Path(directory)/(pattern+label+'.png')
delay = os.environ.get('CAPTURE_DELAY')
if delay:
    code = ('import time,pathlib,shutil,sys; time.sleep(float(sys.argv[1])); '
            'p=pathlib.Path(sys.argv[2]); p.parent.mkdir(parents=True,exist_ok=True); '
            'shutil.copyfile(sys.argv[3],p)')
    subprocess.Popen([sys.executable, '-c', code, delay, str(out), str(root/'fixture.png')],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
else:
    out.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(root/'fixture.png', out)
print('ok')  # ACK does not wait for an asynchronous capture.
'''


@unittest.skipUnless(shutil.which("magick"), "ImageMagick is required")
class ShellCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="screen-toolkit-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.tmp = self.root / "tmp"
        self.bin = self.root / "bin"
        for path in (self.home, self.tmp, self.bin):
            path.mkdir()
        self.shots = self.home / "Custom Shots"
        self.settings = self.home / ".local/state/noctalia/settings.toml"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text('[shell.screenshot]\ndirectory = '+json.dumps(str(self.shots))+'\n')
        shim = self.bin / "noctalia"
        shim.write_text(SHIM)
        shim.chmod(0o755)
        subprocess.run(["magick", "-size", "8x6", "xc:red", str(self.root / "fixture.png")], check=True)
        self.env = dict(os.environ, HOME=str(self.home), TMPDIR=str(self.tmp),
                        XDG_CONFIG_HOME=str(self.home / ".config"),
                        XDG_STATE_HOME=str(self.home / ".local/state"),
                        TEST_ROOT=str(self.root), SHOT_DIR=str(self.shots),
                        HELP_FLAGS=FLAGS, PATH=str(self.bin)+":"+os.environ["PATH"])
        for key in ("ST_IPC_FLAGS", "XDG_PICTURES_DIR", "EXPORTED_DIR", "LEGACY",
                    "CONFIRM_COPY", "NO_SAVE", "CAPTURE_DELAY"):
            self.env.pop(key, None)

    def capture(self, *args):
        result = subprocess.run([str(SCRIPT), *args], env=self.env, text=True,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = Path(result.stdout.strip())
        self.assertTrue(path.is_file(), result.stdout)
        return path

    def calls(self):
        return [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]

    def test_region_uses_core_filename_suffix_with_file_saving_disabled(self):
        self.env["NO_SAVE"] = "1"
        path = self.capture("region-tmp")
        self.assertEqual(path.name, "capture-region.png")
        self.assertEqual(path.parent.parent, self.tmp)
        self.assertFalse(self.shots.exists())

    def test_fullscreen_keeps_unsuffixed_filename(self):
        path = self.capture("fullscreen-tmp")
        self.assertEqual(path.name, "capture.png")
        self.assertFalse(self.shots.exists())

    def test_markup_hands_actual_region_path_to_editor(self):
        path = self.capture("annotate-region")
        self.assertIn(["msg", "annotate", str(path)], self.calls())
        self.assertFalse(self.shots.exists())

    def test_confirmation_cannot_override_temporary_capture_routing(self):
        self.env["CONFIRM_COPY"] = "1"
        self.capture("region-tmp")
        capture = next(call for call in self.calls() if "--filename-pattern" in call)
        for flag in ("--no-confirm-region", "--no-copy-to-clipboard", "--no-pipe-to-command", "--no-annotate"):
            self.assertIn(flag, capture)

    def test_keep_raw_uses_exported_config_directory(self):
        directory = self.home / "Directory from included config"
        self.env["EXPORTED_DIR"] = str(directory)
        path = self.capture("annotate-region", "keep")
        self.assertEqual(path.parent, directory)
        self.assertIn(["msg", "annotate", str(path)], self.calls())

    def test_keep_raw_empty_directory_uses_xdg_environment(self):
        directory = self.home / "My Pictures"
        self.env.update(EXPORTED_DIR="", XDG_PICTURES_DIR=str(directory))
        path = self.capture("annotate-fullscreen", "keep")
        self.assertEqual(path.parent, directory)

    def test_explicit_empty_state_directory_overrides_other_sources(self):
        self.settings.write_text('[shell.screenshot]\ndirectory = ""\n')
        directory = self.home / "Pictures from user dirs"
        config = self.home / ".config"
        config.mkdir()
        (config / "user-dirs.dirs").write_text('XDG_PICTURES_DIR="$HOME/Pictures from user dirs"\n')
        other = config / "noctalia/settings.toml"
        other.parent.mkdir()
        other.write_text('[shell.screenshot]\ndirectory = "/incorrect"\n')
        path = self.capture("annotate-region", "keep")
        self.assertEqual(path.parent, directory)

    def test_empty_directory_defaults_to_home_pictures(self):
        self.env["EXPORTED_DIR"] = ""
        path = self.capture("annotate-region", "keep")
        self.assertEqual(path.parent, self.home / "Pictures")

    def test_legacy_capture_is_moved_out_of_screenshots_directory(self):
        self.env["LEGACY"] = "1"
        path = self.capture("region-tmp")
        self.assertEqual(path.name, "capture.png")
        self.assertFalse(list(self.shots.glob("*.png")))
        self.assertIn(["msg", "screenshot-region"], self.calls())

    def test_probe_requires_every_flag_used_by_capture(self):
        self.env["HELP_FLAGS"] = FLAGS.replace("--no-confirm-region", "")
        self.capture("region-tmp")
        self.assertIn(["msg", "screenshot-region"], self.calls())

    def test_asynchronous_ack_does_not_skip_file_flush(self):
        self.env["CAPTURE_DELAY"] = "0.5"
        self.capture("region-tmp")


if __name__ == "__main__":
    unittest.main()
