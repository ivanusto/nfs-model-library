"""Offline tests for mlib.py: a fake library in a temp directory, the CLI run
as a subprocess. `add` and `pull` need the network and are not covered here."""
import hashlib, json, os, shutil, subprocess, sys, tempfile, unittest

MLIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mlib.py")


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Library(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for dirpath, _, _ in os.walk(self.root):
            os.chmod(dirpath, 0o755)
        shutil.rmtree(self.root)

    def mlib(self, *args, rc=0):
        p = subprocess.run([sys.executable, MLIB, *args], capture_output=True, text=True,
                           env={**os.environ, "MLIB_ROOT": self.root}, timeout=60)
        if rc is not None:
            self.assertEqual(p.returncode, rc, p.stdout + p.stderr)
        return p.stdout + p.stderr

    def make(self, entry, files=None):
        d = os.path.join(self.root, entry)
        os.makedirs(d)
        for name, data in (files or {"config.json": b"{}", "model.safetensors": entry.encode()}).items():
            os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)
        return d

    def ingest(self, entry, note=""):
        self.make(entry)
        return self.mlib("ingest", entry, "--source", "test", *(["--note", note] if note else []))

    def manifest(self):
        with open(os.path.join(self.root, "MANIFEST.tsv")) as f:
            return [l.rstrip("\n").split("\t") for l in f if not l.startswith("#")]

    # ------------------------------------------------------------ ingest

    def test_ingest_freezes_and_records(self):
        d = self.make("llm/org/m/aaa", {"config.json": b"{}", "sub/w.bin": b"w"})
        self.mlib("ingest", "llm/org/m/aaa/", "--source", "s", "--note", "two\tlines\nhere")
        self.assertEqual(os.stat(os.path.join(d, "sub/w.bin")).st_mode & 0o777, 0o444)
        self.assertEqual(os.stat(os.path.join(d, "sub")).st_mode & 0o777, 0o555)
        rows = self.manifest()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 10)
        self.assertEqual(rows[0][1], "llm/org/m/aaa")
        self.assertEqual(rows[0][9], "two lines here")
        with open(os.path.join(d, "SHA256SUMS"), "rb") as f:
            self.assertEqual(rows[0][6], sha(f.read()))
        # readers need no tool
        subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=d, check=True)

    def test_ingest_refuses_twice(self):
        self.ingest("llm/org/m/aaa")
        self.assertIn("retire it", self.mlib("ingest", "llm/org/m/aaa", "--source", "s", rc=1))

    def test_ingest_refuses_tool_owned_or_wide_paths(self):
        os.makedirs(os.path.join(self.root, "ollama/blobs"))
        before = os.stat(os.path.join(self.root, "ollama/blobs")).st_mode
        for entry in ["ollama", "llm", "llm/org/m", ".trash/x", "../x", "misc/a"]:
            self.mlib("ingest", entry, "--source", "s", rc=1)
        self.assertEqual(os.stat(os.path.join(self.root, "ollama/blobs")).st_mode, before)

    # ------------------------------------------------------------ verify

    def test_verify_ok_mismatch_missing_unlisted(self):
        d = self.make("llm/org/m/aaa", {"a": b"1", "b": b"2", "c": b"3"})
        self.mlib("ingest", "llm/org/m/aaa", "--source", "s")
        self.assertIn("ok", self.mlib("verify"))
        os.chmod(d, 0o755)
        for n in ("a", "b"):
            os.chmod(os.path.join(d, n), 0o644)
        with open(os.path.join(d, "a"), "wb") as f:
            f.write(b"changed")
        os.remove(os.path.join(d, "b"))
        with open(os.path.join(d, "extra"), "wb") as f:
            f.write(b"x")
        out = self.mlib("verify", rc=1)
        for want in ("mismatch a", "missing b", "unlisted extra"):
            self.assertIn(want, out)

    def test_verify_catches_rewritten_sums(self):
        d = self.make("llm/org/m/aaa", {"a": b"1"})
        self.mlib("ingest", "llm/org/m/aaa", "--source", "s")
        os.chmod(d, 0o755)
        os.chmod(os.path.join(d, "a"), 0o644)
        with open(os.path.join(d, "a"), "wb") as f:
            f.write(b"2")
        os.remove(os.path.join(d, "SHA256SUMS"))
        with open(os.path.join(d, "SHA256SUMS"), "w") as f:
            f.write(f"{sha(b'2')}  a\n")
        out = self.mlib("verify", rc=1)
        self.assertIn("sums-changed", out)
        self.assertNotIn("mismatch", out)

    def test_verify_ollama_by_blob_name(self):
        blobs = os.path.join(self.root, "ollama/blobs")
        os.makedirs(blobs)
        good, cfg = b"weights", b"config"
        for data in (good, cfg):
            with open(os.path.join(blobs, "sha256-" + sha(data)), "wb") as f:
                f.write(data)
        mdir = os.path.join(self.root, "ollama/manifests/registry.ollama.ai/library/tiny")
        os.makedirs(mdir)
        m = {"config": {"digest": "sha256:" + sha(cfg), "size": 6},
             "layers": [{"digest": "sha256:" + sha(good), "size": 7}]}
        with open(os.path.join(mdir, "1b"), "w") as f:
            json.dump(m, f)
        with open(os.path.join(self.root, "MANIFEST.tsv"), "w") as f:
            f.write("\t".join(["t", "ollama/tiny:1b", "s", "", "13", "2",
                               sha256_path(os.path.join(mdir, "1b")), "", "u", ""]) + "\n")
        self.assertIn("ok       ollama/", self.mlib("verify", "--ollama"))
        with open(os.path.join(blobs, "sha256-" + sha(good)), "wb") as f:
            f.write(b"bitrot!")
        open(os.path.join(blobs, "sha256-" + "0" * 64 + "-partial-0"), "w").close()
        out = self.mlib("verify", "--ollama", rc=1)
        self.assertIn("mismatch sha256-" + sha(good), out)
        self.assertIn("partial", out)

    # ------------------------------------------------------------ retire and gc

    def test_retire_moves_to_trash_and_ls_marks_it(self):
        self.ingest("llm/org/m/aaa")
        self.mlib("retire", "llm/org/m/aaa")
        self.assertFalse(os.path.exists(os.path.join(self.root, "llm/org/m/aaa")))
        trash = os.listdir(os.path.join(self.root, ".trash"))
        self.assertEqual(len(trash), 1)
        self.assertEqual(self.manifest()[-1][2], "retired")
        self.assertTrue(self.mlib("ls").startswith("R"))

    def test_gc_keeps_two_generations(self):
        for rev in ("r1", "r2", "r3"):
            self.ingest(f"llm/org/m/{rev}")
        out = self.mlib("gc", "--dry-run")
        self.assertIn("would retire llm/org/m/r1", out)
        self.assertNotIn("r2", out)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "llm/org/m/r1")))
        self.mlib("gc")   # takes the lock once; retiring inside must not deadlock
        self.assertFalse(os.path.exists(os.path.join(self.root, "llm/org/m/r1")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "llm/org/m/r3")))
        self.assertEqual(self.mlib("gc", "--dry-run"), "")

    def test_gc_keep_note_is_exempt_and_uncounted(self):
        self.ingest("llm/org/m/r1", note="keep: Day 10 baseline")
        self.ingest("llm/org/m/r2")
        self.ingest("llm/org/m/r3")
        self.ingest("llm/org/m/r4")
        out = self.mlib("gc", "--dry-run")
        self.assertIn("would retire llm/org/m/r2", out)
        self.assertNotIn("r1", out)
        self.assertIn("K ", self.mlib("ls"))

    def test_gc_ignores_comfy_and_ollama(self):
        for rev in ("r1", "r2", "r3"):
            self.ingest(f"comfy/checkpoints/x/{rev}")
        self.assertEqual(self.mlib("gc", "--dry-run"), "")

    def test_gc_counts_a_reingested_path_once(self):
        self.ingest("llm/org/m/r1")
        self.mlib("retire", "llm/org/m/r1")
        self.ingest("llm/org/m/r1")
        self.ingest("llm/org/m/r2")
        self.assertEqual(self.mlib("gc", "--dry-run"), "")

    def test_gc_deletes_old_trash_only(self):
        for day in ("20000101", "29990101"):
            d = os.path.join(self.root, ".trash", day, "llm/org/m/r")
            os.makedirs(d)
            open(os.path.join(d, "f"), "w").close()
            os.chmod(d, 0o555)
        self.assertIn("deleted .trash/20000101", self.mlib("gc"))
        self.assertEqual(os.listdir(os.path.join(self.root, ".trash")), ["29990101"])

    def test_retire_twice_same_day(self):
        self.ingest("llm/org/m/r1")
        self.mlib("retire", "llm/org/m/r1")
        self.ingest("llm/org/m/r1")
        self.mlib("retire", "llm/org/m/r1")
        day = os.listdir(os.path.join(self.root, ".trash"))[0]
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, ".trash", day, "llm/org/m"))),
                         ["r1", "r1.1"])


def sha256_path(p):
    with open(p, "rb") as f:
        return sha(f.read())


if __name__ == "__main__":
    unittest.main()
