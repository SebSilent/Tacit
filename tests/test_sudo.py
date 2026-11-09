"""The sudo switch: the store, the parser, the elevation, and what the routes admit.

`backend/sudo.py` hands the agent root on the machine it runs on, so most of this file is
about the three ways that could go quietly wrong: a secret that is not actually secret, a
command that was not actually elevated, and a switch that reports ready when it has nothing
to feed. Each has a test that would fail if the code were written the obvious way.

Every test runs against a throwaway home, with `config.OS` set to Linux where the platform
matters, so the elevation logic is the same on the Windows box this suite runs on.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import agent, config, sandbox, secrets, sudo   # noqa: E402
from tests.helpers import state_paths                       # noqa: E402

SECRET = "hunter2 € ünicode"


class Isolated(unittest.TestCase):
    """A temporary home for every state path, and a chosen OS for the ones that matter.

    The path list is derived (`tests/helpers.py:6-27`) so a new file under the home cannot
    be forgotten and end up written into the real ~/.tacit."""

    OS = "Linux"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self._keys = state_paths(config)
        self._orig = {key: getattr(config, key) for key in self._keys}
        self._home, self._os = config.HOME, config.OS
        self.addCleanup(self._restore)
        config.HOME = home
        config.OS = self.OS
        for key in self._keys:
            target = home / Path(self._orig[key]).name
            setattr(config, key, target)
            if key.endswith("_DIR"):
                target.mkdir(parents=True, exist_ok=True)

    def _restore(self):
        config.HOME = self._home
        config.OS = self._os
        for key, value in self._orig.items():
            setattr(config, key, value)

    def arm(self, password=SECRET):
        config.save_prefs({"sudoEnabled": True})
        sudo.set_password(password)


class TestPanelOnlyWhereTheCapabilityExists(Isolated):
    """`sudo` does not exist on the platform this project is developed on, and the person
    running that install should not have to read a row about it. An explanatory row is
    still a row: it spends attention on a capability that can never arrive there, and it
    invites the question of how to make it appear."""

    OS = "Windows"   # the install that must not see the row

    @property
    def src(self):
        return (Path(__file__).resolve().parents[1] / "static" / "platform.js").read_text(
            encoding="utf-8")

    def test_the_row_is_gated_on_the_server_having_sudo(self):
        start = self.src.index("const sudoRow =")
        head = self.src[start:start + 90]
        self.assertIn("!sd.available", head,
                      "the Sudo row renders on a host with no sudo to switch")

    def test_no_row_explains_its_own_absence(self):
        self.assertNotIn("Not available here", self.src,
                         "the panel renders a row about sudo on a platform that cannot have it")

    def test_the_server_still_answers_for_a_panel_that_asks(self):
        """Hiding the row is not the enforcement. The route is what stops a hand-made
        request turning elevation on for a machine that has nothing to feed."""
        self.OS = "Windows"
        config.save_prefs({"sudoEnabled": True})
        self.assertFalse(sudo.enabled())
        self.assertFalse(sudo.status()["available"])


class TestSecretStore(Isolated):
    def test_a_value_comes_back_as_it_went_in(self):
        secrets.put("sudo", SECRET)
        self.assertEqual(secrets.get("sudo"), SECRET)

    def test_the_plaintext_is_never_on_disk(self):
        secrets.put("sudo", SECRET)
        for path in (config.SECRETS_FILE, config.SECRET_KEY_FILE):
            blob = path.read_bytes()
            self.assertNotIn(SECRET.encode("utf-8"), blob, f"{path} holds it in the clear")
            self.assertNotIn("hunter2".encode("utf-8"), blob)

    def test_two_saves_of_the_same_password_are_not_the_same_bytes(self):
        """A fresh nonce per write. Two rows that match is a comparison an attacker can
        make without the key, and it tells them two accounts share a password."""
        secrets.put("a", SECRET)
        first = config.read_json(config.SECRETS_FILE, {})["a"]["value"]
        secrets.put("b", SECRET)
        rows = config.read_json(config.SECRETS_FILE, {})
        self.assertNotEqual(first, rows["b"]["value"])
        self.assertEqual(secrets.get("a"), secrets.get("b"))

    def test_an_edited_value_does_not_open(self):
        secrets.put("sudo", SECRET)
        rows = config.read_json(config.SECRETS_FILE, {})
        tok = rows["sudo"]["value"]
        rows["sudo"]["value"] = tok[:-6] + "AAAAAA"
        config.write_json(config.SECRETS_FILE, rows)
        with self.assertRaises(secrets.SecretError):
            secrets.get("sudo")

    def test_a_different_key_file_does_not_open(self):
        """The failure that matters most: a restored home with the old store and a fresh
        key. Decrypting that would produce a wrong password, which sudo reports as a bad
        password, and the person then re-types the thing that was never wrong."""
        secrets.put("sudo", SECRET)
        config.SECRET_KEY_FILE.write_text(os.urandom(32).hex() + "\n", encoding="ascii")
        with self.assertRaises(secrets.SecretError):
            secrets.get("sudo")

    def test_the_key_is_stored_where_a_strip_cannot_shorten_it(self):
        """The data-loss bug this file found by luck. Raw random bytes were read back
        with `.strip()`, and a key ending in 0x0a or 0x20 came back a byte short, so the
        store refused to open on roughly one install in twenty-five. Hex text cannot be
        shortened that way, and this checks the form rather than the odds."""
        secrets.put("sudo", SECRET)
        raw = config.SECRET_KEY_FILE.read_text(encoding="ascii")
        self.assertRegex(raw.strip(), r"^[0-9a-f]{64}$")
        self.assertEqual(len(bytes.fromhex(raw.strip())), 32)
        self.assertEqual(secrets.get("sudo"), SECRET)

    def test_a_short_key_file_is_refused_not_replaced(self):
        config.SECRET_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.SECRET_KEY_FILE.write_bytes(b"tiny")
        with self.assertRaises(ValueError):
            secrets.encrypt("x")

    def test_an_empty_value_removes_the_secret(self):
        secrets.put("sudo", SECRET)
        secrets.put("sudo", "")
        self.assertFalse(secrets.has("sudo"))
        self.assertEqual(secrets.get("sudo"), "")

    def test_names_never_carries_a_value(self):
        secrets.put("sudo", SECRET)
        self.assertEqual(secrets.names(), ["sudo"])

    @unittest.skipIf(os.name == "nt", "POSIX file modes do not exist here")
    def test_the_files_are_owner_only(self):
        secrets.put("sudo", SECRET)
        for path in (config.SECRETS_FILE, config.SECRET_KEY_FILE):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, f"{path} is group-readable")


class TestSudoSwitch(Isolated):
    def test_off_until_a_person_turns_it_on(self):
        self.assertFalse(sudo.enabled())
        self.assertFalse(sudo.armed())
        self.assertFalse(config.prefs().get("sudoEnabled"),
                         "absent means off, and nothing had to be written to say so")

    def test_on_with_nothing_saved_is_not_armed(self):
        """The state the panel has to be able to tell apart from ready, because a switch
        that is up with no password feeds nothing and the command hangs all the same."""
        config.save_prefs({"sudoEnabled": True})
        self.assertTrue(sudo.enabled())
        self.assertFalse(sudo.armed())

    def test_a_non_posix_platform_cannot_enable_it(self):
        config.save_prefs({"sudoEnabled": True})
        config.OS = "Windows"
        self.assertFalse(sudo.available())
        self.assertFalse(sudo.enabled(), "a pref left over from a Linux install must not "
                                         "offer elevation on Windows")
        self.assertFalse(sudo.armed())

    def test_status_never_carries_the_password(self):
        self.arm()
        blob = json.dumps(sudo.status())
        self.assertNotIn("hunter2", blob)
        self.assertTrue(sudo.status()["armed"])


class TestCommandPositions(Isolated):
    SITES = [
        ("sudo apt-get install -y curl", 1),
        ("echo done && sudo systemctl restart nginx", 1),
        ("ls -la; sudo rm -rf /tmp/x", 1),
        ("sudo make install | tee log", 1),
        ("/usr/bin/sudo -u postgres psql", 1),
        ("SUDO_ASKPASS=/x sudo whoami", 1),
        ("sudo", 1),
        ("echo sudo", 0),
        ("grep -r sudo backend/", 0),
        ("cat sudo.txt", 0),
        ("", 0),
    ]

    def test_what_counts_as_a_sudo_command(self):
        """Every one of these was wrong in the first draft. `echo done && sudo reboot`
        read as no sudo because the segment was sliced and the offset still counted from
        the whole line, and `cat sudo.txt` read as sudo because a word match has no
        notion of a command position."""
        for cmd, want in self.SITES:
            self.assertEqual(len(sudo.sites(cmd)), want, cmd)

    # (command, sites, of those: at the end of a pipe, inside quotes)
    SHAPES = [
        ("for f in a b; do sudo chmod 0 $f; done", 1, 0, 0),
        ("if test -f /tmp/x; then sudo cp /tmp/x /etc/x; fi", 1, 0, 0),
        ("while read -r l; do sudo tee -a /etc/hosts; done", 1, 0, 0),
        ("case $1 in start) sudo systemctl start nginx ;; esac", 1, 0, 0),
        ("until sudo -n true; do sleep 1; done", 1, 0, 0),
        ("!sudo whoami", 1, 0, 0),
        ("{ sudo whoami; }", 1, 0, 0),
        ("id=$(sudo id -u)", 1, 0, 0),
        ("echo `sudo whoami`", 1, 0, 0),
        ("echo x | sudo tee /etc/hosts", 1, 1, 0),
        ("echo x |& sudo tee /etc/hosts", 1, 1, 0),
        ("false || sudo whoami", 1, 0, 0),
        ("sh -c 'sudo apt update'", 0, 0, 0),
        ('bash -c "sudo x && sudo y"', 1, 0, 1),
    ]

    def test_a_sudo_inside_shell_syntax_is_still_found(self):
        """The reported failure, one line per shape.

        `for …; do sudo …; done` and `if …; then sudo …; fi` put a sudo at a real command
        position that the first draft did not see, because `do` and `then` were not stepped
        over. Nothing was added to it and nothing was fed, and no note was written either,
        since the switch was on: the model got a bare "a terminal is required to read the
        password" and reported back that root had been withdrawn from it."""
        for cmd, want, piped, quoted in self.SHAPES:
            got = sudo.sites(cmd)
            self.assertEqual(len(got), want, cmd)
            self.assertEqual(sum(1 for s in got if s["piped"]), piped, cmd)
            self.assertEqual(sum(1 for s in got if s["quoted"]), quoted, cmd)

    def test_what_is_found_after_a_keyword_is_actually_fed(self):
        self.arm()
        for cmd in ("for f in a b; do sudo chmod 0 $f; done",
                    "if test -f /tmp/x; then sudo cp /tmp/x /etc/x; fi",
                    "case $1 in start) sudo systemctl start nginx ;; esac",
                    "id=$(sudo id -u)"):
            got = sudo.prepare(cmd)
            self.assertEqual(got["fed"], 1, cmd)
            self.assertIn("-S -p ''", got["command"], cmd)

    def test_a_quoted_sudo_is_left_out_of_the_command(self):
        """Splicing `-p ''` into a single-quoted string changes what the string says, so a
        sudo inside quotes is never rewritten. The note says so instead."""
        self.arm()
        got = sudo.prepare('bash -c "sudo x && sudo y"')
        self.assertEqual(got["fed"], 0)
        self.assertEqual(got["command"], 'bash -c "sudo x && sudo y"')

    def test_an_unarmed_switch_changes_nothing(self):
        got = sudo.prepare("sudo apt-get install -y curl")
        self.assertEqual(got["command"], "sudo apt-get install -y curl")
        self.assertIsNone(got["stdin"])
        self.assertEqual(got["sites"], 1)
        self.assertFalse(got["elevated"])


class TestElevation(Isolated):
    def test_the_flags_that_make_a_password_readable_are_added(self):
        self.arm()
        got = sudo.prepare("sudo apt-get install -y curl")
        self.assertEqual(got["command"], "sudo -S -p '' apt-get install -y curl")
        self.assertEqual(got["stdin"], SECRET + "\n")

    def test_two_elevations_in_one_line_get_two_lines(self):
        """sudo reads exactly one line and execs with the rest of stdin intact. One line
        would leave the second sudo sitting on an EOF it cannot answer."""
        self.arm()
        got = sudo.prepare("sudo apt-get update && sudo apt-get install -y curl")
        self.assertEqual(got["command"].count("sudo -S -p ''"), 2)
        self.assertEqual(got["stdin"].splitlines(), [SECRET, SECRET])

    def test_a_line_that_already_reads_stdin_is_not_told_to_twice(self):
        self.arm()
        got = sudo.prepare("sudo -S sh")
        self.assertEqual(got["command"], "sudo -S sh")
        self.assertEqual(got["fed"], 1)

    def test_a_pipe_into_sudo_is_never_fed(self):
        """The shape behind "1 incorrect password attempt" in a report.

        `echo x | sudo -S tee /etc/hosts` hands sudo a stdin full of `x`. Adding `-S` there
        makes sudo read the piped data as the password, fail once, retry, and then pass the
        real password to tee as its input. The expectation this replaces was `fed == 1` for
        exactly this line: the test was pinning the bug."""
        self.arm()
        cmd = "echo x | sudo -S tee /etc/hosts"
        got = sudo.prepare(cmd)
        self.assertEqual(got["command"], cmd, "nothing is added to a piped sudo")
        self.assertEqual(got["fed"], 0)
        self.assertEqual(got["piped"], 1)
        self.assertFalse(got["elevated"])
        self.assertIsNone(got["stdin"], "the secret must not be written to a pipe at all")

    def test_a_pipe_into_sudo_without_the_flag_is_still_not_fed(self):
        """Whether the sudo already carries `-S` changes nothing about who owns its stdin."""
        self.arm()
        got = sudo.prepare("cat list | sudo tee /etc/hosts")
        self.assertEqual(got["fed"], 0)
        self.assertEqual(got["piped"], 1)

    def test_double_bar_is_not_a_pipe(self):
        """`false || sudo whoami` inherits the shell's stdin and can be fed. Reading `||` as
        a pipe would leave a plain conditional unexplained."""
        self.arm()
        got = sudo.prepare("false || sudo whoami")
        self.assertEqual(got["fed"], 1)
        self.assertEqual(got["piped"], 0)

    def test_and_and_are_not_pipes(self):
        self.arm()
        for cmd in ("true && sudo whoami", "true ; sudo whoami", "true\nsudo whoami"):
            self.assertEqual(sudo.prepare(cmd)["fed"], 1, cmd)

    def test_lowercase_s_is_a_shell_not_a_stdin_flag(self):
        """`sudo -s` asks for a shell. Reading it as `-S` would feed the password to that
        shell's stdin instead of to sudo, which is the secret, in the clear, in a
        transcript."""
        self.arm()
        got = sudo.prepare("sudo -s whoami")
        self.assertIn("-S -p ''", got["command"])

    def test_non_interactive_is_left_alone_and_not_fed(self):
        self.arm()
        for cmd in ("sudo -n true", "sudo --non-interactive ls"):
            got = sudo.prepare(cmd)
            self.assertEqual(got["command"], cmd)
            self.assertEqual(got["fed"], 0)
            self.assertFalse(got["elevated"])

    def test_a_stored_password_is_fed_exactly_as_saved(self):
        """One line, no trailing whitespace added, nothing stripped: sudo compares the
        line it reads against the credential, and a silent strip here would turn a
        correct password into a wrong one."""
        self.arm("  spaced  ")
        self.assertEqual(sudo.prepare("sudo whoami")["stdin"], "  spaced  \n")


class TestANotFedSudoIsNeverSilent(Isolated):
    """`explain` exists because of one sentence in a report: "root is no longer available
    to me", written by a model that had run `sudo -v` after a loop failed. The switch was
    on the whole time and the loop was the problem.

    The rule the tests hold: a sudo that failed and was not fed always gets a sentence
    saying which of the two it was, and no sentence ever implies the switch went off.
    """

    def test_it_stays_quiet_when_the_password_was_supplied(self):
        self.arm()
        got = sudo.prepare("sudo whoami")
        self.assertEqual(got["fed"], 1)
        self.assertEqual(sudo.explain(got, 1, "sudo: no password was provided"), "")

    def test_it_stays_quiet_when_the_switch_is_off(self):
        got = sudo.prepare("sudo whoami")
        self.assertFalse(got["armed"])
        self.assertEqual(sudo.explain(got, 1, "sudo: a terminal is required"), "",
                         "agent.py states the off case on every backend already")

    def test_it_stays_quiet_about_a_command_that_ran_fine(self):
        """`grep sudo backend/` mentions the word and needs nothing."""
        self.arm()
        got = sudo.prepare("grep -r sudo backend/")
        self.assertEqual(sudo.explain(got, 0, "backend/sudo.py: def armed()"), "")

    def test_a_pipe_is_explained_as_a_pipe(self):
        self.arm()
        note = sudo.explain(sudo.prepare("echo x | sudo tee /etc/hosts"), 1,
                            "sudo: 1 incorrect password attempt")
        self.assertIn("end of a pipe", note)
        self.assertIn("sudo cp", note)

    def test_a_quoted_sudo_is_explained_as_quoted(self):
        self.arm()
        note = sudo.explain(sudo.prepare("sh -c 'sudo apt update'"), 1,
                            "sudo: a terminal is required to read the password")
        self.assertIn("quoted string", note)
        self.assertIn("sudo sh -c", note)

    def test_a_dash_n_is_explained_as_dash_n(self):
        self.arm()
        note = sudo.explain(sudo.prepare("sudo -n whoami"), 1,
                            "sudo: a password is required")
        self.assertIn("`-n`", note)

    def test_an_empty_store_says_the_password_is_missing(self):
        """Ready, and nothing inside it. `put` refuses to store an empty value and the
        panel refuses to save one, so this arrives from a store edited by hand or half
        written, and the branch stays because the note is the only thing that says which
        of the two states the model is looking at."""
        self.arm()

        class Empty:
            def has(self, name):
                return True

            def get(self, name):
                return ""

        real = sudo._store
        sudo._store = Empty()
        self.addCleanup(setattr, sudo, "_store", real)
        got = sudo.prepare("sudo whoami")
        self.assertTrue(got["armed"], "the store has the name, so the switch reads ready")
        self.assertEqual(got["fed"], 0)
        note = sudo.explain(got, 1, "sudo: no password was provided")
        self.assertIn("saved password is empty", note)

    def test_no_note_ever_lets_the_model_infer_the_switch(self):
        """Every sentence ends by naming the state of the switch, because the report was a
        model inferring it from silence and telling a person root had gone."""
        self.arm()
        for cmd in ("echo x | sudo tee /etc/hosts", "sh -c 'sudo apt update'",
                    "sudo -n whoami"):
            note = sudo.explain(sudo.prepare(cmd), 1, "sudo: a password is required")
            self.assertTrue(note, cmd)
            self.assertIn("the switch is ON", note, cmd)
            self.assertNotIn("switch is off", note, cmd)
            self.assertNotIn("Settings > Tools, Sudo.", note, cmd)


class TestRunCapturedStdin(Isolated):
    """The plumbing, with a real child. A stubbed Popen would prove the argument was
    passed and not that the password arrived, that the pipe closed, and that the command
    did not hang on it."""

    READER = "import sys; sys.stdout.write('GOT[' + sys.stdin.readline().strip() + ']')"

    def setUp(self):
        super().setUp()
        self.script = Path(self._tmp.name) / "reader.py"
        self.script.write_text(self.READER, encoding="utf-8")

    def test_stdin_data_reaches_the_child_and_the_pipe_closes(self):
        code, out, err, timed = sandbox._run_captured(
            f'"{sys.executable}" "{self.script}"', cwd=str(Path(self._tmp.name)),
            timeout=30, shell=True, stdin_data=SECRET + "\n")
        self.assertFalse(timed, "the write did not close the pipe: " + err[:200])
        self.assertEqual(code, 0, err[:300])
        self.assertIn(f"GOT[{SECRET}]", out)

    def test_no_stdin_data_still_means_an_immediate_eof(self):
        code, out, err, timed = sandbox._run_captured(
            f'"{sys.executable}" "{self.script}"', cwd=str(Path(self._tmp.name)),
            timeout=30, shell=True)
        self.assertEqual((code, out.strip(), timed), (0, "GOT[]", False))

    def test_a_viewed_command_gets_its_stdin_too(self):
        """The streaming path is the one a person is watching, and it is a different
        branch. A password written on the caller's thread would deadlock it."""
        seen = []
        code, out, err, timed = sandbox._run_captured(
            f'"{sys.executable}" "{self.script}"', cwd=str(Path(self._tmp.name)),
            timeout=30, shell=True, stdin_data=SECRET + "\n",
            on_output=lambda kind, text: seen.append((kind, text)))
        self.assertEqual(code, 0, err[:300])
        self.assertIn(f"GOT[{SECRET}]", "".join(t for _k, t in seen))


class TestRunNoneSuppliesIt(Isolated):
    def capture(self, cmd, code=0, out="ok", err=""):
        """Run `_run_none` with the spawn recorded instead of executed."""
        seen = {}

        def fake(argv, **kw):
            seen["argv"] = argv
            seen.update(kw)
            return code, out, err, False

        real = sandbox._run_captured
        sandbox._run_captured = fake
        try:
            res = sandbox._run_none(cmd, Path(self._tmp.name), 30, "s1")
        finally:
            sandbox._run_captured = real
        return res, seen

    def test_the_rewritten_command_and_the_password_go_together(self):
        self.arm()
        res, seen = self.capture("sudo whoami")
        self.assertEqual(seen["argv"], "sudo -S -p '' whoami")
        self.assertEqual(seen["stdin_data"], SECRET + "\n")
        self.assertTrue(any("sudo" in n for n in res["notes"]),
                        "the model must be told the command ran as root")

    def test_a_sudo_it_could_not_feed_says_so_in_the_result(self):
        """The bug report, as a result object.

        Switch on, a sudo Tacit cannot feed, command failing on a password prompt. Before
        this the notes carried nothing about sudo at all, and the model concluded from
        sudo's own line that root had been taken away and told the person so."""
        self.arm()
        res, seen = self.capture("sh -c 'sudo apt update'", code=1,
                                 err="sudo: a terminal is required to read the password")
        self.assertIsNone(seen["stdin_data"], "the secret must not be written at all here")
        notes = " ".join(res["notes"])
        self.assertIn("the switch is ON", notes)
        self.assertIn("quoted string", notes)

    def test_a_loop_that_runs_sudo_gets_the_password(self):
        self.arm()
        res, seen = self.capture("for f in a b; do sudo chmod 0 $f; done")
        self.assertIn("sudo -S -p '' chmod 0 $f", seen["argv"])
        self.assertEqual(seen["stdin_data"], SECRET + "\n")
        self.assertIn("supplied to 1 command", " ".join(res["notes"]))

    def test_a_command_it_fed_does_not_also_get_the_miss_note(self):
        self.arm()
        res, _ = self.capture("sudo whoami")
        notes = " ".join(res["notes"])
        self.assertIn("supplied to 1 command", notes)
        self.assertNotIn("did not feed", notes)

    def test_the_fed_note_does_not_claim_more_than_it_knows(self):
        """A fed command can still be refused by sudoers or by a wrong saved password. The
        note states what Tacit did and leaves the rest to the output, because the model
        reads this line as the state of the switch."""
        self.arm()
        res, _ = self.capture("sudo whoami")
        notes = " ".join(res["notes"])
        self.assertIn("not the switch being off", notes)

    def test_the_password_is_not_in_what_anyone_can_see(self):
        self.arm()
        res, _seen = self.capture("sudo whoami")
        blob = json.dumps(res)
        self.assertNotIn("hunter2", blob)

    def test_an_unarmed_run_passes_the_command_through_untouched(self):
        res, seen = self.capture("sudo whoami")
        self.assertEqual(seen["argv"], "sudo whoami")
        self.assertIsNone(seen["stdin_data"])

    def _audit_rows(self):
        return [json.loads(line) for line in
                config.AUDIT_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_the_audit_line_records_that_it_ran_as_root(self):
        """The ledger is where a person finds out afterwards that something in this
        session had root. The field counts the commands the password was fed to, so a
        line that ran two of them reads as two rather than blurring into a yes."""
        self.arm()
        sandbox._run_none("sudo true", Path(self._tmp.name), 30, "s1")
        rec = [r for r in self._audit_rows() if r.get("tool") == "run_shell"][-1]
        self.assertEqual(rec["sudo"], 1)

    def test_an_ordinary_command_records_no_elevation(self):
        self.arm()
        sandbox._run_none("true", Path(self._tmp.name), 30, "s1")
        rec = [r for r in self._audit_rows() if r.get("tool") == "run_shell"][-1]
        self.assertEqual(rec["sudo"], 0)

    def test_the_sandboxed_backends_say_they_do_not_supply_it(self):
        """Neither one feeds a password: micro leaves stdin at DEVNULL and the container
        drops the setuid bit. Both have to say so where the model will read it."""
        src = Path(sandbox.__file__).read_text(encoding="utf-8")
        for name in ("_run_micro", "_run_container"):
            start = src.index(f"def {name}(")
            nxt = src.find("\ndef ", start + 1)
            body = src[start:nxt if nxt != -1 else len(src)]
            self.assertIn("sudo.uses_sudo(cmd)", body,
                          f"{name} runs a sudo command without reporting that the "
                          "password was not supplied")


class TestRoutes(Isolated):
    class Req:
        def __init__(self, body):
            self._b = body

        async def json(self):
            return self._b

    def post(self, body):
        from backend.routers import api as api_router
        return asyncio.run(api_router.set_sudo(self.Req(body)))

    def get(self):
        from backend.routers import api as api_router
        return asyncio.run(api_router.get_sudo())

    def _body(self, res):
        return res if isinstance(res, dict) else json.loads(res.body)

    def test_get_reports_the_state_and_not_the_secret(self):
        self.arm()
        body = self._body(self.get())
        self.assertTrue(body["armed"])
        self.assertNotIn("password", body)
        self.assertNotIn("hunter2", json.dumps(body))

    def test_the_switch_round_trips(self):
        self.assertFalse(self._body(self.get())["enabled"])
        self.assertTrue(self._body(self.post({"enabled": True}))["enabled"])
        self.assertFalse(self._body(self.post({"enabled": False}))["enabled"])

    def test_saving_a_password_reports_armed(self):
        self.post({"enabled": True})
        body = self._body(self.post({"password": SECRET}))
        self.assertTrue(body["armed"])
        self.assertNotIn("hunter2", json.dumps(body))

    def test_an_empty_password_is_refused_rather_than_clearing(self):
        self.arm()
        res = self._body(self.post({"password": ""}))
        self.assertFalse(res["ok"], "an empty box must not delete a stored secret")
        self.assertTrue(sudo.has_password())

    def test_a_newline_password_is_refused(self):
        self.arm()
        res = self._body(self.post({"password": "two\nlines"}))
        self.assertFalse(res["ok"])
        self.assertEqual(sudo.status()["has_password"], True)
        self.assertEqual(secrets.get("sudo"), SECRET, "the refused write must not replace")

    def test_forget_removes_it_and_leaves_the_switch(self):
        self.arm()
        body = self._body(self.post({"clear_password": True}))
        self.assertFalse(body["has_password"])
        self.assertTrue(body["enabled"], "forgetting the password is not turning sudo off")

    def test_a_windows_server_refuses_the_switch(self):
        config.OS = "Windows"
        res = self._body(self.post({"enabled": True}))
        self.assertFalse(res["ok"])
        self.assertIn("Windows", res["error"])

    def test_the_routes_are_wired(self):
        from fastapi.testclient import TestClient

        from backend.main import app
        with TestClient(app) as c:
            got = c.get("/api/harness/sudo").json()
            saved = c.post("/api/harness/sudo", json={"enabled": True}).json()
        self.assertTrue(got["ok"] and saved["ok"], (got, saved))
        self.assertTrue(saved["enabled"])


class TestWhatTheAgentSees(Isolated):
    def test_the_prompt_block_is_absent_until_it_is_true(self):
        """A capability that is off must cost nothing: no block, no tokens. And one that
        is off but described would have the model reaching for root it does not have."""
        from backend.ai import prompts
        plain = prompts.system_prompt(project=None, session="")
        self.assertNotIn("Root elevation", plain)
        self.arm()
        armed = prompts.system_prompt(project=None, session="")
        self.assertIn("Root elevation is armed", armed)

    def test_chat_mode_never_gets_it(self):
        from backend.ai import prompts
        self.arm()
        self.assertNotIn("Root elevation", prompts.system_prompt(chat=True, session=""))

    def test_the_store_and_the_key_are_protected_from_the_file_tools(self):
        for name in ("secrets.json", "secret.key"):
            with self.assertRaises(agent.ToolError):
                agent._safe(str(config.HOME / name), project=None)

    def test_run_shell_says_why_a_sudo_command_had_no_password(self):
        out = agent.t_run_shell("sudo definitely-not-a-real-command", session="s1")
        self.assertIn("[sudo]", out)
        self.assertIn("Settings > Tools", out)

    def test_and_does_not_when_it_supplied_one(self):
        self.arm()
        out = agent.t_run_shell("sudo definitely-not-a-real-command", session="s1")
        self.assertNotIn("Tacit supplied no password", out)

    def test_a_command_that_only_carries_the_word_sudo_is_left_alone(self):
        """Found by running the product on itself: a one-liner whose text mentioned `sudo`
        inside a string literal, failing for a reason nothing to do with root, was told
        "Tacit supplied no password". That sentence is only true of a command that was going
        to be asked for one, so it now needs sudo at a command position or sudo's own line
        in the output, and a non-zero exit besides."""
        out = agent.t_run_shell("grep sudo definitely-not-a-real-file", session="s1")
        self.assertNotIn("[sudo]", out)

    def test_sudo_talking_is_recognised_and_anything_else_is_not(self):
        """The narrower half of the complaint pattern, on its own: the wide one matches
        `permission denied`, which a hundred unrelated failures say."""
        self.assertTrue(sudo.complained("sudo: a terminal is required to read the password"))
        self.assertTrue(sudo.complained("Sorry, try again.\nsudo: 1 incorrect password attempt"))
        self.assertTrue(sudo.complained("user is not in the sudoers file. This incident will be reported."))
        self.assertFalse(sudo.complained("'grep' is not recognized as an internal or external command"))
        self.assertFalse(sudo.complained("chmod: Operation not permitted"))
        self.assertFalse(sudo.complained(""))


if __name__ == "__main__":
    unittest.main()
