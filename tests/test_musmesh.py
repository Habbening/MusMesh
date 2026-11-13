"""Off-device tests. Run on Linux from the repo root: python3 -m unittest discover tests

Set MUSMESH_BUILD to an extracted test build (.musmesh directory, x86-64 wheels) to run the
bundled clients as well.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.environ.get("MUSMESH_BUILD")
UP, DOWN, LEFT, RIGHT, ENTER = b"\x1bOA", b"\x1bOB", b"\x1bOD", b"\x1bOC", b"\r"

# Runs musmesh.main() with the port list and client commands pointed at temporary stand-ins.
HARNESS = """
import os, sys
sys.path.insert(0, {root!r})
import musmesh
musmesh.HERE = {here!r}
musmesh.DATA = os.path.join({here!r}, "data")
musmesh.PORT_GLOBS = ({port!r} + "*",)
stub = lambda port: [{record!r}, port]
code = "import sys; open(sys.argv[1], 'w').write(sys.argv[2]); sys.exit({rc})"
musmesh.CLIENTS = tuple((c[0], {client!r} or code, stub, c[3]) for c in musmesh.CLIENTS)
musmesh.main()
"""


class Session:
    def __init__(self, args, env=None, rows=24, cols=64):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvpe(args[0], args, dict(os.environ, TERM="xterm-256color", **(env or {})))
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.out = b""

    def wait_for(self, text, timeout=15):
        end = time.monotonic() + timeout
        while text not in self.out:
            if time.monotonic() > end:
                raise AssertionError("no %r in %r" % (text, self.out[-400:]))
            if select.select([self.fd], [], [], 0.1)[0]:
                try:
                    self.out += os.read(self.fd, 65536)
                except OSError:
                    break
        if text not in self.out:
            raise AssertionError("exited without %r: %r" % (text, self.out[-400:]))

    def drain(self):
        while select.select([self.fd], [], [], 0.1)[0]:
            self.out += os.read(self.fd, 65536)
        return self.out

    def send(self, data):
        self.out = b""
        os.write(self.fd, data)

    def wait_exit(self, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if select.select([self.fd], [], [], 0.1)[0]:
                try:
                    self.out += os.read(self.fd, 65536)
                except OSError:
                    pass
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.pid = None
                return os.waitstatus_to_exitcode(status)
        raise AssertionError("still running: %r" % self.out[-400:])

    def close(self):
        if self.pid:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        os.close(self.fd)


class MenuTest(unittest.TestCase):
    def start(self, rc=0, port_exists=True, client="", credits=None, accepted=True, fresh=True):
        if fresh:
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
        here = self.tmp.name
        self.port = os.path.join(here, "ttyACM0")
        self.record = os.path.join(here, "record")
        self.accepted = os.path.join(here, "data", "storage-accepted")
        if accepted:
            os.makedirs(os.path.dirname(self.accepted), exist_ok=True)
            open(self.accepted, "w").close()
        if port_exists:
            open(self.port, "w").close()
        with open(os.path.join(here, "CREDITS.txt"), "w") as fh:
            fh.write(credits or "contact 1.7.4: GPL-3.0-only\n")
        code = HARNESS.format(root=ROOT, here=here, port=self.port, record=self.record, rc=rc, client=client)
        session = Session([sys.executable, "-c", code])
        session.home = os.path.join(here, "data")
        self.addCleanup(session.close)
        return session

    def test_no_node_blocks_client_and_quit_is_clean(self):
        s = self.start(port_exists=False)
        s.wait_for(b"none, plug one in")
        s.send(ENTER)
        s.wait_for(b"No node found")
        self.assertFalse(os.path.exists(self.record))
        s.send(UP)
        s.wait_for(b"> Quit")
        s.send(ENTER)
        self.assertEqual(s.wait_exit(), 0)

    def test_storage_warning_accept_is_remembered(self):
        s = self.start(accepted=False)
        s.wait_for(b"not encrypted")
        s.send(DOWN)
        s.drain()
        self.assertNotIn(b"> Meshtastic", s.out)
        s.send(ENTER)
        s.wait_for(b"> Meshtastic")
        self.assertTrue(os.path.exists(self.accepted))
        s.send(UP)
        s.wait_for(b"> Quit")
        s.send(ENTER)
        self.assertEqual(s.wait_exit(), 0)
        s = self.start(accepted=False, fresh=False)
        s.wait_for(b"> Meshtastic")
        self.assertNotIn(b"not encrypted", s.out)

    def test_storage_warning_b_quits(self):
        s = self.start(accepted=False)
        s.wait_for(b"not encrypted")
        s.send(b"\x7f")
        self.assertEqual(s.wait_exit(), 0)
        self.assertFalse(os.path.exists(self.accepted))
        self.assertFalse(os.path.exists(self.record))

    def test_storage_warning_ctrl_h_and_q_quit(self):
        for key in (b"\x08", b"q"):
            s = self.start(accepted=False)
            s.wait_for(b"not encrypted")
            s.send(key)
            self.assertEqual(s.wait_exit(), 0, key)
            self.assertFalse(os.path.exists(self.accepted), key)

    def test_storage_warning_says_what_is_accepted(self):
        s = self.start(accepted=False)
        s.wait_for(b"Start: accept")
        for phrase in (b"not encrypted", b"channel keys", b"repeater passwords", b".musmesh/data",
                       b"/tmp/musmesh-contact.log", b"root", b"radio"):
            self.assertIn(phrase, s.out)

    def test_storage_warning_double_start_does_not_launch_client(self):
        s = self.start(accepted=False)
        s.wait_for(b"not encrypted")
        # Two real presses: separate writes, so separate reads.
        os.write(s.fd, ENTER)
        time.sleep(0.15)
        os.write(s.fd, ENTER)
        s.wait_for(b"> Meshtastic")
        time.sleep(1)
        s.drain()
        self.assertFalse(os.path.exists(self.record))
        self.assertNotIn(b"Connecting", s.out)
        self.assertNotIn(b"No node found", s.out)

    def test_storage_warning_returns_after_data_is_deleted(self):
        s = self.start(accepted=False)
        s.wait_for(b"not encrypted")
        s.send(ENTER)
        s.wait_for(b"> Meshtastic")
        s.send(UP)
        s.wait_for(b"> Quit")
        s.send(ENTER)
        self.assertEqual(s.wait_exit(), 0)
        shutil.rmtree(os.path.join(self.tmp.name, "data"))
        s = self.start(accepted=False, fresh=False)
        s.wait_for(b"not encrypted")

    def test_node_found_when_plugged_in_later(self):
        s = self.start(port_exists=False)
        s.wait_for(b"none, plug one in")
        open(self.port, "w").close()
        s.wait_for(self.port.encode(), timeout=4)

    def test_client_gets_port_and_menu_returns(self):
        s = self.start()
        s.wait_for(b"> Meshtastic")
        s.send(ENTER)
        s.wait_for(b"Client closed")
        s.wait_for(b"Press Start")
        with open(self.record) as fh:
            self.assertEqual(fh.read(), self.port)
        s.send(ENTER)
        s.wait_for(b"> Meshtastic")

    def test_quiet_exit_output_stays_until_start(self):
        # meshcore-cli prints the connect error and exits 0.
        s = self.start(client="import sys; print('cannot connect'); sys.exit(0)")
        s.wait_for(b"> Meshtastic")
        s.send(ENTER)
        s.wait_for(b"cannot connect")
        s.wait_for(b"Press Start")
        s.send(DOWN + UP)
        time.sleep(0.3)
        self.assertNotIn(b"> Meshtastic", s.drain())

    def test_terminal_restored_after_client_dies_raw(self):
        s = self.start(client="import os, signal, sys, tty; tty.setraw(0); sys.stdout.write('\\x1b[?25l'); "
                              "sys.stdout.flush(); os.kill(os.getpid(), signal.SIGKILL)")
        s.wait_for(b"> Meshtastic")
        s.send(ENTER)
        s.wait_for(b"Client exited with code -9")
        self.assertIn(b"\x1b[?25h", s.out)
        self.assertIn(b"\x1b[?1049l", s.out)
        s.wait_for(b"Press Start")
        s.send(ENTER)
        s.wait_for(b"> Meshtastic")

    def test_ctrl_c_goes_to_client_and_menu_survives(self):
        s = self.start(client="import sys, time\ntry:\n    time.sleep(30)\nexcept KeyboardInterrupt:\n"
                              "    print('client got it'); sys.exit(0)")
        s.wait_for(b"> Meshtastic")
        s.send(ENTER)
        s.wait_for(b"Connecting")
        time.sleep(0.5)
        s.send(b"\x03")
        s.wait_for(b"client got it")
        s.wait_for(b"Press Start")
        s.send(ENTER)
        s.wait_for(b"> Meshtastic")
        s.send(b"\x03")
        s.send(DOWN)
        s.wait_for(b"> MeshCore")

    def test_unknown_escape_sequence_is_not_read_as_keys(self):
        s = self.start()
        s.wait_for(b"> Meshtastic")
        s.send(b"\x1b[1;2q" + DOWN)
        s.wait_for(b"> MeshCore")

    def test_keys_during_client_do_not_dismiss_pause(self):
        s = self.start(client="import time; print('cannot connect', flush=True); time.sleep(1)")
        s.wait_for(b"> Meshtastic")
        s.send(ENTER)
        s.wait_for(b"cannot connect")
        s.send(ENTER)
        s.wait_for(b"Press Start")
        time.sleep(0.3)
        self.assertNotIn(b"> Meshtastic", s.drain())

    def test_key_burst_cannot_dismiss_pause(self):
        s = self.start(client="print('cannot connect')")
        s.wait_for(b"> Meshtastic")
        s.send(ENTER + ENTER)
        s.wait_for(b"Press Start")
        time.sleep(0.3)
        self.assertNotIn(b"> Meshtastic", s.drain())

    def test_alt_screen_left_only_after_signal(self):
        s = self.start(rc=3)
        s.wait_for(b"> Meshtastic")
        s.send(ENTER)
        s.wait_for(b"Client exited with code 3")
        self.assertNotIn(b"\x1b[?1049l", s.out)

    def test_client_runs_in_data_with_home_and_meshcore_dir(self):
        code = ("import os; home = os.environ['HOME']; assert os.getcwd() == os.path.realpath(home); "
                "assert os.path.isdir(home + '/.config/meshcore'); print('layout ok')")
        s = self.start(client=code)
        s.wait_for(b"> Meshtastic")
        s.send(ENTER)
        s.wait_for(b"layout ok")
        self.assertEqual(os.path.basename(s.home), "data")

    def test_unwritable_last_client_does_not_stop_client(self):
        s = self.start()
        os.makedirs(os.path.join(self.tmp.name, "data", "last-client"))
        s.wait_for(b"> Meshtastic")
        s.send(ENTER)
        s.wait_for(b"Client closed")

    def test_empty_credits_do_not_crash(self):
        s = self.start(credits="\n")
        with open(os.path.join(self.tmp.name, "CREDITS.txt"), "w"):
            pass
        s.wait_for(b"> Meshtastic")
        s.send(DOWN + DOWN)
        s.wait_for(b"> Credits")
        s.send(ENTER)
        s.wait_for(b"Page 1/1")

    def test_key_burst_is_not_lost(self):
        s = self.start()
        s.wait_for(b"> Meshtastic")
        s.send(DOWN + DOWN)
        s.wait_for(b"> Credits")

    def test_last_choice_is_remembered(self):
        s = self.start()
        s.wait_for(b"> Meshtastic")
        s.send(DOWN + ENTER)
        s.wait_for(b"Press Start")
        s.send(ENTER)
        s.wait_for(b"> MeshCore")
        s.send(b"q")
        self.assertEqual(s.wait_exit(), 0)
        code = HARNESS.format(root=ROOT, here=self.tmp.name, port=self.port, record=self.record, rc=0, client="")
        again = Session([sys.executable, "-c", code])
        self.addCleanup(again.close)
        again.wait_for(b"> MeshCore")

    def test_left_right_changes_node(self):
        s = self.start()
        other = self.port + "1"
        open(other, "w").close()
        s.wait_for(b"Left/Right: change node", timeout=4)
        s.send(RIGHT)
        s.wait_for(("Node: %s\r\n" % other).encode())

    def test_long_credits_page(self):
        s = self.start(credits="".join("line %d\n" % i for i in range(60)))
        s.wait_for(b"> Meshtastic")
        s.send(DOWN + DOWN)
        s.wait_for(b"> Credits")
        s.send(ENTER)
        s.wait_for(b"Page 1/")
        self.assertIn(b"line 0\r\n", s.out)
        self.assertNotIn(b"line 59", s.out)
        s.send(DOWN + DOWN + DOWN)
        s.wait_for(b"line 59")
        s.send(ENTER)
        s.wait_for(b"> Credits")

    def test_failed_client_is_reported(self):
        s = self.start(rc=3)
        s.wait_for(b"> Meshtastic")
        s.send(ENTER)
        s.wait_for(b"Client exited with code 3")
        s.wait_for(b"Press Start")
        s.send(ENTER)
        s.wait_for(b"> Meshtastic")

    def test_navigation_wraps_and_accepts_both_arrow_encodings(self):
        s = self.start()
        s.wait_for(b"> Meshtastic")
        s.send(b"\x1b[B")
        s.wait_for(b"> MeshCore")
        s.send(DOWN)
        s.wait_for(b"> Credits")
        s.send(ENTER)
        s.wait_for(b"GPL-3.0-only")
        s.wait_for(b"Start: back")
        s.send(ENTER)
        s.wait_for(b"> Credits")
        s.send(DOWN)
        s.wait_for(b"> Quit")
        s.send(DOWN)
        s.wait_for(b"> Meshtastic")


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CommandTest(unittest.TestCase):
    def test_storage_warning_fits_smallest_terminal(self):
        musmesh = load_module(os.path.join(ROOT, "musmesh.py"), "musmesh")
        lines = musmesh.STORAGE_WARNING.splitlines()
        self.assertLessEqual(len(lines), 23)
        self.assertLessEqual(max(map(len, lines)), 64)

    def test_env_and_contact_log(self):
        musmesh = load_module(os.path.join(ROOT, "musmesh.py"), "musmesh")
        with tempfile.TemporaryDirectory() as data:
            musmesh.DATA = data
            os.makedirs(os.path.join(data, "contact"))
            stale = os.path.join(data, "contact", "client.log")
            open(stale, "w").close()
            with mock.patch.dict(os.environ, COLUMNS="39", LINES="20"):
                env = musmesh.client_env()
            self.assertNotIn("COLUMNS", env)
            self.assertNotIn("LINES", env)
            self.assertFalse(os.path.exists(stale))
            with open(os.path.join(data, "contact", "config.json")) as fh:
                self.assertEqual(json.load(fh)["log_file_path"], "/tmp/musmesh-contact.log")

    def test_real_client_commands(self):
        musmesh = load_module(os.path.join(ROOT, "musmesh.py"), "musmesh")
        self.assertEqual(musmesh.client_command(0, "/dev/ttyACM0")[1:3], ["-c", musmesh.CLIENTS[0][1]])
        self.assertEqual(musmesh.client_command(0, "/dev/ttyACM0")[3:], ["--port", "/dev/ttyACM0"])
        self.assertEqual(musmesh.client_command(1, "/dev/ttyACM0")[3:], ["-s", "/dev/ttyACM0", "-b", "115200", "chat"])


class FilesTest(unittest.TestCase):
    def test_launcher_is_posix_sh(self):
        path = os.path.join(ROOT, "app", "mux_launch.sh")
        subprocess.run(["sh", "-n", path], check=True)
        with open(path, "rb") as fh:
            self.assertNotIn(b"\r", fh.read())

    def test_meshcore_is_advertised_as_companion_only(self):
        with open(os.path.join(ROOT, "app", "mux_launch.sh"), encoding="utf-8") as fh:
            help_line = next(line for line in fh if line.startswith("# HELP:"))
        self.assertIn("MeshCore companion nodes", help_line)
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        self.assertIn("MeshCore companion nodes", readme.splitlines()[2])
        self.assertIn("companion firmware only", readme)

    def test_keyboard_layer_fits_muxterm_limits(self):
        # muxterm allows four rows of 12 keys, each "label | bytes | width".
        with open(os.path.join(ROOT, "app", "osk-mesh.conf")) as fh:
            body = fh.read().split("\n", 1)[1]
        rows = body.strip().split("\n\n")
        self.assertLessEqual(len(rows), 4)
        for row in rows:
            keys = row.split("\n")
            self.assertLessEqual(len(keys), 12)
            for key in keys:
                self.assertEqual(len(key.split(" | ")), 3, key)

    def test_keyboard_layer_sends_contact_keys(self):
        with open(os.path.join(ROOT, "app", "osk-mesh.conf")) as fh:
            keys = dict(line.split(" | ")[:2] for line in fh if " | " in line)
        self.assertEqual(keys, {
            "Esc": "\\x1b", "Help": "\\x0b", "Reply": "\\x12", "Search": "/", "Setup": "`",
            "Fav": "\\x06", "Ignore": "\\x07", "Trace": "\\x14", "Packets": "\\x10",
        })


STUB_FUNC = """
MUOS_SHARE_DIR="{root}/share"; MUOS_STORE_DIR="{root}/store"
GET_VAR() {{ echo "$WIDTH_FOR_TEST"; }}
SETUP_STAGE_OVERLAY() {{ :; }}
SETUP_APP() {{ [ -n "${{PRELOAD_FOR_TEST:-}}" ] && export LD_PRELOAD="$PRELOAD_FOR_TEST"; :; }}
"""
STUB_MUXTERM = """#!{sh}
for a in "$@"; do echo "$a"; done >"{root}/args"
pwd >"{root}/cwd"
"""


class LauncherTest(unittest.TestCase):
    def run_launcher(self, width="640", no_python=False, gui_exit=3, env_extra=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        sh = shutil.which("sh")
        musmesh_dir = os.path.join(root, "share/application/MusMesh/.musmesh")
        os.makedirs(os.path.join(musmesh_dir, "gui"))
        # Stand-in for the GUI: records what the launcher gave it, exits as told.
        with open(os.path.join(musmesh_dir, "gui", "app.py"), "w") as fh:
            fh.write("import os, sys\n"
                     f"open({os.path.join(root, 'gui-env')!r}, 'w').write('\\n'.join(\n"
                     "    k + '=' + os.environ.get(k, '') for k in ('LD_PRELOAD', 'PYTHONPATH', 'HOME')))\n"
                     f"sys.exit({gui_exit})\n")
        with open(os.path.join(root, "func.sh"), "w") as fh:
            fh.write(STUB_FUNC.format(root=root))
        muxterm = os.path.join(root, "muxterm")
        with open(muxterm, "w") as fh:
            fh.write(STUB_MUXTERM.format(root=root, sh=sh))
        os.chmod(muxterm, 0o755)
        with open(os.path.join(ROOT, "app", "mux_launch.sh")) as fh:
            script = fh.read()
        script = (script.replace("/opt/muos/script/var/func.sh", root + "/func.sh")
                  .replace("/opt/muos/frontend/muxterm", muxterm)
                  .replace("/opt/muos/share/info/config/Terminal", root + "/cfg")
                  .replace("/tmp/musmesh-launch.log", root + "/launch.log"))
        path = os.path.join(root, "launch.sh")
        with open(path, "w") as fh:
            fh.write(script)
        env = dict(os.environ, WIDTH_FOR_TEST=width, **(env_extra or {}))
        if no_python:
            # A PATH without python3, so command -v fails.
            bin_dir = os.path.join(root, "bin")
            os.mkdir(bin_dir)
            for tool in ("mkdir",):
                os.symlink(shutil.which(tool), os.path.join(bin_dir, tool))
            env["PATH"] = bin_dir
        subprocess.run([sh, path], check=not no_python, env=env)
        return root

    def args(self, root):
        with open(os.path.join(root, "args")) as fh:
            return fh.read().split("\n")

    def font(self, root):
        with open(os.path.join(root, "cfg", "MusMesh.conf")) as fh:
            return fh.read().strip()

    def test_runs_menu_in_muxterm(self):
        root = self.run_launcher()
        args = self.args(root)
        musmesh_dir = os.path.join(root, "share/application/MusMesh/.musmesh")
        self.assertEqual(args[args.index("--") + 1:args.index("--") + 3], ["python3", "musmesh.py"])
        self.assertEqual(args[args.index("--osk-layout") + 1], musmesh_dir + "/osk-mesh.conf")
        self.assertIn("--no-sb-persist", args)
        self.assertEqual(args[args.index("-c") + 1], os.path.join(root, "cfg", "MusMesh.conf"))
        with open(os.path.join(root, "cwd")) as fh:
            self.assertEqual(fh.read().strip(), musmesh_dir)

    def gui_env(self, root):
        with open(os.path.join(root, "gui-env")) as fh:
            return dict(line.split("=", 1) for line in fh.read().split("\n"))

    def test_gui_quit_does_not_open_terminal(self):
        root = self.run_launcher(gui_exit=0)
        self.assertFalse(os.path.exists(os.path.join(root, "args")))
        env = self.gui_env(root)
        self.assertEqual(env["PYTHONPATH"], "vendor:vendor.zip")
        self.assertTrue(env["HOME"].endswith("/.musmesh/data"))

    def test_gui_keeps_the_muos_environment(self):
        # The screens use the muOS SDL, so the libraries muOS preloads for it stay.
        preload = "/opt/muos/frontend/lib/libmustage.so /opt/muos/frontend/lib/libmucursor.so"
        root = self.run_launcher(gui_exit=0, env_extra={"PRELOAD_FOR_TEST": preload})
        self.assertEqual(self.gui_env(root)["LD_PRELOAD"], preload)

    def test_gui_killed_by_muos_leaves(self):
        for code in (137, 143):
            root = self.run_launcher(gui_exit=code)
            self.assertFalse(os.path.exists(os.path.join(root, "args")), code)

    def test_gui_failure_falls_back_to_terminal_clients(self):
        root = self.run_launcher(gui_exit=1)
        args = self.args(root)
        self.assertEqual(args[args.index("--") + 1:args.index("--") + 3], ["python3", "musmesh.py"])
        with open(os.path.join(root, "launch.log")) as fh:
            self.assertIn("gui exit: 1", fh.read())

    def test_font_follows_screen_width(self):
        self.assertEqual(self.font(self.run_launcher("640")), "term_font_size = 13")
        self.assertEqual(self.font(self.run_launcher("1024")), "term_font_size = 21")
        self.assertEqual(self.font(self.run_launcher("320")), "term_font_size = 10")
        self.assertEqual(self.font(self.run_launcher("")), "term_font_size = 13")

    def test_missing_python_is_reported(self):
        root = self.run_launcher(no_python=True)
        self.assertIn("needs Python 3", " ".join(self.args(root)))
        with open(os.path.join(root, "launch.log")) as fh:
            self.assertIn("python3 not found", fh.read())


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.build = load_module(os.path.join(ROOT, "build_muxapp.py"), "build_muxapp")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def wheel(self, name, version, data=b"x"):
        path = os.path.join(self.tmp.name, f"{name}-{version}-py3-none-any.whl")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_lock_accepts_match_and_rejects_changes(self):
        path = self.wheel("a", "1")
        digest = hashlib.sha256(b"x").hexdigest()
        self.build.check_lock([path], {"a": ("1", digest)})
        for lock in ({"a": ("1", "0" * 64)}, {"a": ("2", digest)}, {}, {"a": ("1", digest), "b": ("1", digest)}):
            with self.assertRaises(SystemExit):
                self.build.check_lock([path], lock)

    def test_package_without_licence_stops_build(self):
        with self.assertRaises(SystemExit):
            self.build.licences_text({"a": {}}, {"a": "Name: a"})

    def test_extra_licences_are_added(self):
        text = self.build.licences_text({"pyserial": {}, "certifi": {"LICENSE": b"notice"}},
                                        {"pyserial": "", "certifi": ""})
        self.assertIn("Chris Liechti", text)
        self.assertIn("Mozilla Public License Version 2.0", text)
        self.assertIn("notice", text)

    def test_same_named_licences_both_kept(self):
        wheel = os.path.join(self.tmp.name, "b-1-py3-none-any.whl")
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr("b-1.dist-info/licenses/one/LICENSE", "first")
            zf.writestr("b-1.dist-info/licenses/two/LICENSE", "second")
            zf.writestr("b-1.dist-info/METADATA", "Name: b\nVersion: 1\n")
        _, parts = self.build.split_wheel(wheel)
        self.assertEqual(len(parts["licence"]), 2)

    def test_credits_clean_up_metadata(self):
        meta = 'Version: 1\r\nLicense: MIT\r\nAuthor-email: bare@example.org\r\nProject-URL: Changelog, https://e.org/c\r\n'
        text = self.build.credits_text({"meshtastic": meta})
        self.assertNotIn("\r", text)
        self.assertNotIn("bare@example.org", text)
        self.assertNotIn("https://e.org/c", text)
        self.assertIn("https://github.com/meshtastic/python", text)

    def test_credits_name_author_and_url_without_address(self):
        meta = ('Name: x\nVersion: 1\nLicense: MIT\nAuthor-email: "A Person" <a@example.org>\n'
                "Project-URL: Homepage, https://example.org/x\n")
        text = self.build.credits_text({"x": meta})
        self.assertIn("A Person", text)
        self.assertIn("https://example.org/x", text)
        self.assertNotIn("a@example.org", text)
        self.assertIn("Habbening", text)

    def test_credits_show_copyright_lines_and_modified_note(self):
        licence = (b"Copyright (c) 2001 Real Holder <r@example.org>\n"
                   b"Copyright (C) <year>  <name of author>\n"
                   b"Copyright [yyyy] [name of copyright owner]\n"
                   b"(c) You must retain all copyright notices\n"
                   b"Copyright (C) 2007 Free Software Foundation, Inc.\n"
                   b"Copyright 2019 Ingy d\xf6t Net\n")
        text = self.build.credits_text({"x": "Version: 1\nLicense: MIT\n"}, {"x": {"LICENSE": licence}})
        self.assertIn("  Copyright (c) 2001 Real Holder\n", text)
        self.assertIn("Ingy döt Net", text)
        for template in ("<year>", "[yyyy]", "You must retain", "Free Software Foundation", "r@example.org"):
            self.assertNotIn(template, text)
        self.assertIn("modified only by removing files", text)
        self.assertRegex(text, r"Packaged for MusMesh on \d{4}-\d{2}-\d{2}\.")
        self.assertIn("GPL-3.0-only", text)

    def test_credits_join_wrapped_holders_and_cut_sentence_tails(self):
        licence = (b"Copyright 2001-2015\n  Francesco Zappa Nardelli\n"
                   b"                    Copyright 1996-2002, 2006 by\n"
                   b"          David Turner, Robert Wilhelm, and Werner Lemberg\n"
                   b"Copyright (c) 1998-2000 Glenn Randers-Pehrson, are derived from libpng-0.96,\n"
                   b"Copyright \xc2\xa9 2010-2022  Google, Inc.\n")
        text = self.build.credits_text({"x": "Version: 1\n"}, {"x": {"LICENSE": licence}})
        self.assertIn("  Copyright 1996-2002, 2006 by David Turner, Robert Wilhelm, and Werner Lemberg\n", text)
        self.assertIn("  Copyright 2001-2015 Francesco Zappa Nardelli\n", text)
        self.assertIn("  Copyright (c) 1998-2000 Glenn Randers-Pehrson\n", text)
        self.assertIn("  Copyright © 2010-2022 Google, Inc.\n", text)

    def test_credits_take_star_lines_and_bare_c_holders(self):
        licence = (b" * Copyright (c) 1995-2019 The PNG Reference Library Authors.\n"
                   b"(C) A. Schiffler, aschiffler [at] ferzkopp.net 2012-2018, licensed under the zlib license\n"
                   b"(C) 2004 Joe Wreschnig\n"
                   b"     (c) You must retain, in the Source form\n")
        text = self.build.credits_text({"x": "Version: 1\n"}, {"x": {"LICENSE": licence}})
        self.assertIn("  Copyright (c) 1995-2019 The PNG Reference Library Authors.\n", text)
        self.assertIn("  (C) A. Schiffler, 2012-2018\n", text)
        self.assertIn("  (C) 2004 Joe Wreschnig\n", text)
        self.assertNotIn("You must", text)
        self.assertNotIn("[at]", text)

    def test_credits_normalise_spdx_and_keep_url_holders(self):
        licence = (b"SPDX-FileCopyrightText: 2026 Some One <s@example.org>\n"
                   b"SPDX-FileCopyrightText: Copyright 2020 Other Body\n"
                   b"Copyright (c) 2018 Holder Inc <https://example.org>\n")
        text = self.build.credits_text({"x": "Version: 1\n"}, {"x": {"LICENSE": licence}})
        self.assertIn("  Copyright 2026 Some One\n", text)
        self.assertIn("  Copyright 2020 Other Body\n", text)
        self.assertIn("  Copyright (c) 2018 Holder Inc\n", text)
        self.assertNotIn("SPDX", text)

    def test_extra_copyright_holders_are_credited(self):
        credits = self.build.credits_text({"dbus_fast": "Version: 1\n", "packaging": "Version: 1\n"}, {})
        self.assertIn("Tony Crisci", credits)
        self.assertIn("Ofek Lev", credits)
        text = self.build.licences_text({"dbus_fast": {"LICENSE": b"mit"}, "protobuf": {"LICENSE": b"bsd"}},
                                        {"dbus_fast": "", "protobuf": ""})
        self.assertIn("Tony Crisci", text)
        self.assertIn("Copyright 2023 Google LLC", text)

    def test_compiled_in_code_licences_are_added(self):
        text = self.build.licences_text({"protobuf": {"LICENSE": b"bsd"}, "pycryptodome": {"LICENSE.rst": b"bsd"}},
                                        {"protobuf": "", "pycryptodome": ""})
        self.assertIn("Copyright (c) 2019 Yibo Cai", text)
        self.assertIn("Helder Eijs", text)
        self.assertIn("modified only by removing files", text)
        credits = self.build.credits_text({"protobuf": "Version: 1\n"}, {"protobuf": {}})
        self.assertIn("Yibo Cai", credits)

    def test_latin1_licence_is_not_mangled(self):
        text = self.build.licences_text({"x": {"LICENSE": b"d\xf6t"}}, {"x": ""})
        self.assertIn("döt", text)
        self.assertNotIn("�", text)


@unittest.skipUnless(BUILD, "MUSMESH_BUILD not set")
class BundleTest(unittest.TestCase):
    """Runs the clients as musmesh.py does, with -S so site-packages cannot hide a gap."""

    def setUp(self):
        self.musmesh = load_module(os.path.join(BUILD, "musmesh.py"), "bundled_musmesh")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.musmesh.DATA = tmp.name
        self.env = self.musmesh.client_env()
        self.env["TERM"] = "xterm-256color"

    def command(self, index, args):
        return [sys.executable, "-S", "-c", self.musmesh.CLIENTS[index][1]] + args

    def test_zip_imports_use_bundled_pyc_and_crypto_works(self):
        code = (
            "from Crypto.Cipher import AES; from Crypto.Hash import HMAC, SHA256\n"
            "import meshcore, meshtastic, meshtastic.serial_interface, meshtastic.ble_interface, yaml, serial, wcwidth\n"
            "import google.protobuf.internal.api_implementation as a\n"
            "c = AES.new(b'k' * 16, AES.MODE_ECB).encrypt(b'b' * 16); h = HMAC.new(b'k', b'm', SHA256).hexdigest()\n"
            # The calls meshcore makes, with only the kept Crypto native modules in the bundle.
            "AES.new(b'k' * 16, AES.MODE_ECB).decrypt(c); m = HMAC.new(b'k' * 16, digestmod=SHA256)\n"
            "m.update(b'x'); m.digest(); SHA256.new(b'a').digest(); SHA256.new(b'a').hexdigest()\n"
            "import meshcore.meshcore_parser, meshcore.reader, meshcore_cli.meshcore_cli\n"
            "assert 'vendor.zip' in meshtastic.__file__ and meshtastic.__file__.endswith('.pyc'), meshtastic.__file__\n"
            "import contact.version; print(a.Type(), contact.version.__name__, len(c), h[:8])\n"
        )
        run = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True, env=self.env)
        self.assertEqual(run.returncode, 0, run.stderr[-600:])
        self.assertIn("upb", run.stdout)

    def test_contact_demo_draws_and_logs_off_card(self):
        env = {k: self.env[k] for k in ("PYTHONPATH", "HOME", "CONTACT_CONFIG_ROOT", "PYTHONDONTWRITEBYTECODE", "ESCDELAY")}
        s = Session(self.command(0, ["--demo-screenshot"]), env=env, rows=26, cols=80)
        self.addCleanup(s.close)
        s.wait_for(b"Message:", timeout=30)
        contact_dir = os.path.join(self.musmesh.DATA, "contact")
        self.assertFalse(os.path.exists(os.path.join(contact_dir, "client.log")))
        with open(os.path.join(contact_dir, "config.json")) as fh:
            config = json.load(fh)
        self.assertEqual(config["log_file_path"], "/tmp/musmesh-contact.log")
        self.assertGreater(len(config), 1)

    def test_meshcore_cli_starts_from_bundle(self):
        run = subprocess.run(self.command(1, ["-v"]), capture_output=True, text=True, env=self.env, timeout=60)
        self.assertIn("1.6.9", run.stdout + run.stderr, run.stderr[-600:])

    def test_licences_cover_every_package(self):
        with open(os.path.join(BUILD, "THIRD_PARTY_LICENSES.txt"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(BUILD, "CREDITS.txt"), encoding="utf-8") as fh:
            built_on = fh.read().split("Built on:")[1]
        names = re.findall(r"^(\S+) \S+: ", built_on, re.M)
        self.assertEqual(len(names), 22, names)
        for name in names:
            self.assertIn(f"\n{name}\n", text)
        self.assertIn("Mozilla Public License Version 2.0", text)
        self.assertIn("Chris Liechti", text)
        # Notices for code compiled into the shipped .so files, and the Apache NOTICE of requests.
        self.assertIn("Copyright (c) 2019 Yibo Cai", text)
        self.assertIn("SPDX-FileCopyrightText: 2026 Legrandin", text)
        self.assertIn("Requests\nCopyright 2019 Kenneth Reitz", text)
        self.assertIn("Habbening", text.splitlines()[0])
        self.assertIn("PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2", text)
        # Holders whose notice is only in a shipped source file or a README.
        for holder in ("2019 Google LLC", "Denny Vrandečić", "Python Software Foundation", "Tony Crisci",
                       "Ofek Lev", "Markus Kuhn", "Copyright 2023 Google LLC"):
            self.assertIn(holder, built_on)
            self.assertIn(holder, text)

    def test_warning_details_file_ships(self):
        self.assertTrue(os.path.exists(os.path.join(BUILD, "README.md")))


PACKAGE = os.path.join(ROOT, "dist", "MusMesh_v%s.muxapp" % os.environ.get("MUSMESH_VERSION", "0.7"))


@unittest.skipUnless(os.path.exists(PACKAGE), "no aarch64 package")
class PackageTest(unittest.TestCase):
    def test_every_native_module_is_aarch64(self):
        with zipfile.ZipFile(PACKAGE) as zf:
            native = [n for n in zf.namelist() if n.endswith(".so")]
            self.assertTrue(native)
            for name in native:
                head = zf.read(name)[:20]
                self.assertEqual(head[:4], b"\x7fELF", name)
                self.assertEqual(struct.unpack("<H", head[18:20])[0], 183, name)
            self.assertEqual(zf.getinfo("MusMesh/mux_launch.sh").external_attr >> 16 & 0o111, 0o111)
            self.assertIn("MusMesh/.musmesh/README.md", zf.namelist())

    def test_only_used_crypto_native_modules_ship(self):
        with zipfile.ZipFile(PACKAGE) as zf:
            crypto = sorted(n.split("/vendor/Crypto/")[1].split(".")[0] for n in zf.namelist()
                            if "/vendor/Crypto/" in n and n.endswith(".so"))
        self.assertEqual(crypto, ["Cipher/_raw_aes", "Cipher/_raw_aes_armv8", "Cipher/_raw_ecb",
                                  "Hash/_SHA256", "Hash/_SHA256_armv8", "Util/_cpuid_c", "Util/_strxor"])

    def test_screens_ship_without_their_own_sdl(self):
        # The screens load the device's SDL2 and SDL2_ttf, so no copy of them is in the package.
        with zipfile.ZipFile(PACKAGE) as zf:
            names = zf.namelist()
        self.assertFalse([n for n in names if "pygame" in n or "libSDL" in n])
        for gui in ("app.py", "radios.py", "sdl.py", "store.py"):
            self.assertIn("MusMesh/.musmesh/gui/" + gui, names)


if __name__ == "__main__":
    unittest.main()
