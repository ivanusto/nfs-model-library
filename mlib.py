#!/usr/bin/env python3
"""mlib: manifest, checksums and housekeeping for a shared NFS model library.

Single writer, many readers. This tool runs on the one node that mounts the
library read-write (the librarian). Every other node mounts it read-only and
never needs this tool; they only need the layout.

Layout under the library root (default $MLIB_ROOT or ./library):

    <root>/
      MANIFEST.tsv                one row per event, append-only, tab separated
      .lock/                      mkdir-based lock, works on NFS without lockd
      .trash/<YYYYMMDD>/<entry>   retired entries wait here until gc removes them
      llm/<org>/<name>/<commit12>/   one directory per entry, as downloaded
      gguf/<org>/<name>/<commit12>/
      comfy/...                   ComfyUI's own layout, see Day 7
      ollama/                     Ollama's store (OLLAMA_MODELS), written only by `pull`

An entry is one directory. Its identity is the relative path under <root>;
its content is fixed by SHA256SUMS inside it, written at ingest and checked by
verify, and SHA256SUMS itself is fixed by the sha256_of_sums column of its
manifest row. MANIFEST.tsv columns:

    added_at  entry  source  revision  bytes  files  sha256_of_sums  license  added_by  note

Commands:
    ingest  <entry> --source URL [--revision R] [--license L] [--note N]
            write SHA256SUMS in the entry, freeze it and append a manifest row
    add     <hf_repo> [--revision R] [--include GLOB ...] [--to llm|gguf]
            resolve R to a commit, hf download into <root>/<to>/<repo>/<commit12>/,
            then ingest
    pull    <model[:tag]>         ollama pull through the librarian's server, then
                                  record the Ollama manifest digest
    verify  [<entry> ...] [--ollama]
            recompute checksums and compare SHA256SUMS with the manifest;
            --ollama also checks every blob against its sha256 file name
    ls      [--json]              list entries in their last recorded state
    retire  <entry>               move to .trash/<today>/, append a retired row
    gc      [--keep-days N] [--keep-gens N] [--dry-run]
            delete trash older than N days; retire all but the newest N entries
            of every llm|gguf/<org>/<name>; entries whose note says "keep" are
            never retired and do not count (default keep-days 14, keep-gens 2)

Only the standard library is used. `add` needs the `hf` CLI (huggingface_hub),
`pull` needs `ollama`.
"""
import argparse, datetime as dt, hashlib, json, os, re, shutil, struct, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request

ROOT = os.environ.get("MLIB_ROOT", "./library")
MANIFEST = "MANIFEST.tsv"
SUMS = "SHA256SUMS"
COLS = ["added_at", "entry", "source", "revision", "bytes", "files",
        "sha256_of_sums", "license", "added_by", "note"]
GC_KINDS = ("llm", "gguf")
KEEP = re.compile(r"\bkeep\b", re.I)


# ---------------------------------------------------------------- helpers

def root():
    r = os.path.abspath(ROOT)
    if not os.path.isdir(r):
        sys.exit(f"library root does not exist: {r}")
    return r


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def who():
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or "?"


def sha256_file(path, bufsize=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(bufsize), b""):
            h.update(chunk)
    return h.hexdigest()


class Lock:
    """mkdir is atomic on NFS; flock is not reliable there. Stale after 6 h."""
    def __init__(self, r):
        self.path = os.path.join(r, ".lock")

    def __enter__(self):
        for _ in range(60):
            try:
                os.mkdir(self.path)
                with open(os.path.join(self.path, "owner"), "w") as f:
                    f.write(f"{os.uname().nodename} {os.getpid()} {now()}\n")
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.stat(self.path).st_mtime
                except FileNotFoundError:
                    continue
                if age > 6 * 3600:
                    shutil.rmtree(self.path, ignore_errors=True)
                    continue
                time.sleep(5)
        try:
            with open(os.path.join(self.path, "owner")) as f:
                owner = f.read().strip()
        except OSError:
            owner = "unknown"
        sys.exit(f"could not take {self.path}, held by {owner}; if that process is gone, "
                 f"check the manifest tail and remove {self.path}")

    def __exit__(self, *a):
        shutil.rmtree(self.path, ignore_errors=True)


def norm_entry(entry):
    parts = [p for p in entry.strip().strip("/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        sys.exit(f"entry must be a relative path inside the library: {entry}")
    return "/".join(parts)


def check_entry(entry):
    """Only directories the tool owns can be entries. Ingesting ollama/ or a
    whole llm/ tree would freeze it read-only and break the next pull."""
    entry = norm_entry(entry)
    parts = entry.split("/")
    if parts[0] in GC_KINDS:
        if len(parts) != 4:
            sys.exit(f"{parts[0]} entries are {parts[0]}/<org>/<name>/<rev>: {entry}")
    elif parts[0] == "comfy":
        if len(parts) < 2:
            sys.exit(f"name a directory under comfy/, not comfy/ itself: {entry}")
    elif parts[0] == "ollama":
        sys.exit("ollama/ belongs to Ollama; record pulls with `mlib.py pull`")
    else:
        sys.exit(f"entries live under llm/, gguf/ or comfy/: {entry}")
    return entry


def entry_dir(r, entry):
    p = os.path.normpath(os.path.join(r, entry))
    if not p.startswith(r + os.sep):
        sys.exit(f"entry must be a relative path inside the library: {entry}")
    return p


def walk_files(d):
    for dirpath, dirnames, filenames in os.walk(d):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if dirpath == d and fn in (SUMS, SUMS + ".tmp"):
                continue
            if os.path.islink(full):
                continue
            yield os.path.relpath(full, d), full


def hash_entry(d):
    """The slow part, done without the lock: hashing 155 GB takes minutes and
    other librarians give up after five."""
    lines, total, n = [], 0, 0
    for rel, full in walk_files(d):
        if "\n" in rel or "\\" in rel:
            sys.exit(f"file name sha256sum cannot list plainly: {rel!r}")
        lines.append(f"{sha256_file(full)}  {rel}\n")
        total += os.path.getsize(full)
        n += 1
    if n == 0:
        sys.exit(f"no files under {d}")
    return lines, total, n


def write_sums(d, lines):
    tmp = os.path.join(d, SUMS + ".tmp")
    with open(tmp, "w") as f:
        f.writelines(lines)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, os.path.join(d, SUMS))
    os.chmod(os.path.join(d, SUMS), 0o444)
    return sha256_file(os.path.join(d, SUMS))


def read_manifest(r):
    p = os.path.join(r, MANIFEST)
    if not os.path.exists(p):
        return []
    rows = []
    with open(p) as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            rows.append(dict(zip(COLS, parts + [""] * (len(COLS) - len(parts)))))
    return rows


def latest_rows(r):
    """Last row per entry, ordered by that row. A retired row ends an entry;
    a later ingest of the same path starts it again."""
    latest = {}
    for row in read_manifest(r):
        latest.pop(row["entry"], None)
        latest[row["entry"]] = row
    return latest


def append_manifest(r, row):
    p = os.path.join(r, MANIFEST)
    new = not os.path.exists(p)
    with open(p, "a") as f:
        if new:
            f.write("# " + "\t".join(COLS) + "\n")
        f.write("\t".join(re.sub(r"[\t\r\n]", " ", str(row.get(c, ""))) for c in COLS) + "\n")
        f.flush(); os.fsync(f.fileno())


NFS4_ACL = "system.nfs4_acl"
ACE_READ = 0x1 | 0x8 | 0x80 | 0x20000 | 0x100000   # data, named attrs, attrs, acl, synchronize
ACE_OWNER = ACE_READ | 0x100 | 0x40000             # owner keeps chmod/setacl, so thaw works
ACE_EXEC = 0x20


def nfs4_acl_readonly(path, isdir):
    """A NAS share with per-user ACLs (QuTS hero on ZFS, aclmode=passthrough)
    grants write through named ACEs that chmod does not touch, so 0444 alone is
    only what ls shows. Replace the ACL with OWNER@/GROUP@/EVERYONE@ read-only.
    Returns False where there is no NFSv4 ACL (local disk, NFSv3)."""
    try:
        os.getxattr(path, NFS4_ACL)
    except OSError:
        return False
    x = ACE_EXEC if isdir else 0
    aces = [(ACE_OWNER | x, "OWNER@"), (ACE_READ | x, "GROUP@"), (ACE_READ | x, "EVERYONE@")]
    b = struct.pack(">I", len(aces))
    for mask, who in aces:   # XDR: type ALLOW=0, flags 0, mask, who as padded string
        w = who.encode()
        b += struct.pack(">IIII", 0, 0, mask, len(w)) + w + b"\0" * (-len(w) % 4)
    os.setxattr(path, NFS4_ACL, b)
    return True


def freeze(d):
    """Files 0444, directories 0555 under the entry: readers cannot alter, and
    neither can a careless librarian without chmod first."""
    for dirpath, dirnames, filenames in os.walk(d):
        for fn in filenames:
            os.chmod(os.path.join(dirpath, fn), 0o444)
            nfs4_acl_readonly(os.path.join(dirpath, fn), False)
    for dirpath, dirnames, filenames in os.walk(d, topdown=False):
        os.chmod(dirpath, 0o555)
        nfs4_acl_readonly(dirpath, True)


def thaw(d):
    for dirpath, dirnames, filenames in os.walk(d):
        os.chmod(dirpath, 0o755)


# ---------------------------------------------------------------- Hugging Face

def hf_token():
    t = os.environ.get("HF_TOKEN")
    if t:
        return t
    home = os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
    try:
        with open(os.path.join(home, "token")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def hf_resolve(repo, rev):
    """Branch or tag to commit, before downloading anything, so the directory
    can be named by content and every file comes from the same commit."""
    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    url = f"{endpoint}/api/models/{repo}/revision/{urllib.parse.quote(rev, safe='')}"
    req = urllib.request.Request(url)
    tok = hf_token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            sha = json.load(resp).get("sha", "")
    except urllib.error.HTTPError as e:
        sys.exit(f"cannot resolve {repo}@{rev}: HTTP {e.code} (gated repo needs HF_TOKEN)")
    except (urllib.error.URLError, OSError, ValueError) as e:
        sys.exit(f"cannot resolve {repo}@{rev}: {e}")
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        sys.exit(f"cannot resolve {repo}@{rev}: no commit in the API answer")
    return sha


def hf_metadata_commits(d):
    """Commits hf wrote into <local-dir>/.cache/huggingface/download/*.metadata
    (first line of each file in huggingface_hub 0.36)."""
    found = set()
    for dirpath, _, files in os.walk(os.path.join(d, ".cache", "huggingface", "download")):
        for fn in files:
            if fn.endswith(".metadata"):
                try:
                    with open(os.path.join(dirpath, fn)) as f:
                        found.add(f.readline().strip())
                except OSError:
                    pass
    return found


# ---------------------------------------------------------------- Ollama

def ollama_manifest(r, name):
    """model[:tag] to <root>/ollama/manifests/<host>/<namespace>/<model>/<tag>."""
    base, _, tag = name.partition(":")
    parts = base.split("/")
    if len(parts) == 1:
        parts = ["registry.ollama.ai", "library"] + parts
    elif len(parts) == 2:
        parts = ["registry.ollama.ai"] + parts
    return os.path.join(r, "ollama", "manifests", *parts, tag or "latest")


def ollama_entry(name):
    return "ollama/" + (name if ":" in name else name + ":latest")


def blob_path(r, digest):
    return os.path.join(r, "ollama", "blobs", digest.replace(":", "-"))


# ---------------------------------------------------------------- commands

def ingest(r, entry, source, revision="", license="", note=""):
    d = entry_dir(r, entry)
    if not os.path.isdir(d):
        sys.exit(f"not a directory: {d}")
    done = f"{entry} already has {SUMS}; retire it and add or ingest again"
    if os.path.exists(os.path.join(d, SUMS)):
        sys.exit(done)
    lines, total, n = hash_entry(d)
    with Lock(r):
        if os.path.exists(os.path.join(d, SUMS)):   # another ingest finished first
            sys.exit(done)
        sums_hash = write_sums(d, lines)
        freeze(d)
        append_manifest(r, {
            "added_at": now(), "entry": entry, "source": source,
            "revision": revision or "", "bytes": total, "files": n,
            "sha256_of_sums": sums_hash, "license": license or "",
            "added_by": who(), "note": note or ""})
    print(f"ingested {entry}: {n} files, {total / 2**30:.1f} GiB, SHA256SUMS {sums_hash[:12]}")


def cmd_ingest(a):
    ingest(root(), check_entry(a.entry), a.source, a.revision, a.license, a.note)


def cmd_add(a):
    r = root()
    rev = a.revision or "main"
    sha = hf_resolve(a.hf_repo, rev)
    entry = check_entry(f"{a.to}/{a.hf_repo}/{sha[:12]}")
    d = entry_dir(r, entry)
    os.makedirs(os.path.dirname(d), exist_ok=True)
    try:
        os.mkdir(d)   # the claim: a second add of the same commit stops here
    except FileExistsError:
        sys.exit(f"{entry} exists: already in the library, being downloaded, or a "
                 f"failed download left for inspection")
    # One call per pattern: hf 0.36 keeps only the last of repeated --include,
    # and hf 1.x does not take several values after one --include.
    for pats in ([[g] for g in a.include] if a.include else [[]]):
        cmd = ["hf", "download", a.hf_repo, "--revision", sha, "--local-dir", d]
        for g in pats:
            cmd += ["--include", g]
        print("+", " ".join(cmd))
        rc = subprocess.call(cmd)
        if rc != 0:
            sys.exit(f"hf download failed ({rc}); {d} left for inspection, not ingested")
    seen = hf_metadata_commits(d)
    if seen - {sha}:
        sys.exit(f"hf wrote commits {sorted(seen)} but {rev} resolved to {sha}; not ingested")
    shutil.rmtree(os.path.join(d, ".cache"), ignore_errors=True)
    ingest(r, entry, f"https://huggingface.co/{a.hf_repo}", f"{rev}@{sha}",
           a.license, a.note)


def cmd_pull(a):
    r = root()
    cmd = ["ollama", "pull", a.model]
    print("+", " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        sys.exit(f"ollama pull failed ({rc}); check ollama/blobs for -partial leftovers")
    p = ollama_manifest(r, a.model)
    if not os.path.exists(p):
        sys.exit(f"pulled, but {os.path.relpath(p, r)} does not exist: the Ollama server "
                 f"is not using {os.path.join(r, 'ollama')} as OLLAMA_MODELS")
    with open(p) as f:
        m = json.load(f)
    blobs = {l["digest"]: l.get("size", 0) for l in [m["config"]] + m.get("layers", [])}
    with Lock(r):
        append_manifest(r, {
            "added_at": now(), "entry": ollama_entry(a.model),
            "source": a.model if "/" in a.model
                      else "https://ollama.com/library/" + a.model.split(":")[0],
            "revision": "", "bytes": sum(blobs.values()),
            "files": len(blobs), "sha256_of_sums": sha256_file(p),
            "license": a.license or "", "added_by": who(), "note": a.note or ""})
    print(f"recorded {ollama_entry(a.model)}: manifest {sha256_file(p)[:12]}")


def verify_entry(d, recorded=""):
    p = os.path.join(d, SUMS)
    if not os.path.exists(p):
        return "no-sums", []
    listed, bad = {}, []
    with open(p) as f:
        for line in f:
            digest, _, rel = line.rstrip("\n").partition("  ")
            listed[rel] = digest
    if recorded and sha256_file(p) != recorded:
        bad.append(f"sums-changed {SUMS} differs from {MANIFEST}")
    for rel, digest in listed.items():
        full = os.path.join(d, rel)
        if not os.path.exists(full):
            bad.append(f"missing {rel}")
        elif sha256_file(full) != digest:
            bad.append(f"mismatch {rel}")
    bad += [f"unlisted {rel}" for rel, _ in walk_files(d) if rel not in listed]
    return ("ok" if not bad else "bad"), bad


def verify_ollama(r, rows):
    """Blob file names are sha256 digests, so no SHA256SUMS is needed. Ollama
    checks a digest when it pulls, not when it loads."""
    bad = []
    for row in rows:
        p = ollama_manifest(r, row["entry"][len("ollama/"):])
        if not os.path.exists(p):
            bad.append(f"missing {row['entry']} manifest")
            continue
        if sha256_file(p) != row["sha256_of_sums"]:
            bad.append(f"manifest-changed {row['entry']} (pulled again without mlib?)")
        with open(p) as f:
            m = json.load(f)
        for l in [m["config"]] + m.get("layers", []):
            if not os.path.exists(blob_path(r, l["digest"])):
                bad.append(f"missing blob {l['digest']} of {row['entry']}")
    blobs = os.path.join(r, "ollama", "blobs")
    for fn in sorted(os.listdir(blobs)) if os.path.isdir(blobs) else []:
        if "-partial" in fn:
            bad.append(f"partial {fn} (a failed pull; poisons the next one)")
        elif fn.startswith("sha256-") and sha256_file(os.path.join(blobs, fn)) != fn[7:]:
            bad.append(f"mismatch {fn}")
    return bad


def cmd_verify(a):
    r = root()
    latest = latest_rows(r)
    active = {e: row for e, row in latest.items() if row["source"] != "retired"}
    if a.entry:
        entries = [norm_entry(e) for e in a.entry]
    else:
        entries = [e for e in active
                   if not e.startswith("ollama/") and os.path.isdir(os.path.join(r, e))]
    rc = 0
    for e in entries:
        if e.startswith("ollama/"):
            continue
        state, bad = verify_entry(entry_dir(r, e), active.get(e, {}).get("sha256_of_sums", ""))
        print(f"{state:8s} {e}")
        for b in bad:
            print(f"         {b}")
        if state != "ok":
            rc = 1
    if a.ollama:
        bad = verify_ollama(r, [row for e, row in active.items() if e.startswith("ollama/")])
        print(f"{'ok' if not bad else 'bad':8s} ollama/")
        for b in bad:
            print(f"         {b}")
        if bad:
            rc = 1
    sys.exit(rc)


def present(r, e):
    if e.startswith("ollama/"):
        return os.path.exists(ollama_manifest(r, e[len("ollama/"):]))
    return os.path.isdir(os.path.join(r, e))


def cmd_ls(a):
    """One line per entry, in the state the manifest last recorded it."""
    r = root()
    out = []
    for e, row in latest_rows(r).items():
        out.append({**row, "retired": row["source"] == "retired",
                    "keep": bool(KEEP.search(row["note"])), "present": present(r, e)})
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    for o in out:
        gib = int(o["bytes"] or 0) / 2**30
        flag = "R" if o["retired"] or not o["present"] else "K" if o["keep"] else " "
        print(f"{flag} {gib:7.1f} GiB  {o['added_at'][:10]}  {o['entry']}  {o['revision']}")


def retire_entry(r, entry, keep_days):
    """Caller holds the lock."""
    d = entry_dir(r, entry)
    if not os.path.isdir(d):
        sys.exit(f"not present: {entry}")
    dest = base = os.path.join(r, ".trash", dt.date.today().strftime("%Y%m%d"), entry)
    n = 1
    while os.path.exists(dest):
        dest, n = f"{base}.{n}", n + 1
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    thaw(d)
    shutil.move(d, dest)
    append_manifest(r, {"added_at": now(), "entry": entry, "source": "retired",
                        "note": f"moved to {os.path.relpath(dest, r)}", "added_by": who()})
    print(f"retired {entry} -> {os.path.relpath(dest, r)} (gc deletes it after {keep_days} days)")


def cmd_retire(a):
    r = root()
    entry = check_entry(a.entry)
    with Lock(r):
        retire_entry(r, entry, a.keep_days)


def rmtree_frozen(p):
    """Directories under .trash may still be 0555 if moved by hand."""
    for dirpath, _, _ in os.walk(p):
        os.chmod(dirpath, 0o755)
    shutil.rmtree(p)


def cmd_gc(a):
    r = root()
    with Lock(r):
        # 1. empty old trash
        trash = os.path.join(r, ".trash")
        cutoff = dt.date.today() - dt.timedelta(days=a.keep_days)
        if os.path.isdir(trash):
            for day in sorted(os.listdir(trash)):
                try:
                    when = dt.datetime.strptime(day, "%Y%m%d").date()
                except ValueError:
                    continue
                if when < cutoff:
                    if a.dry_run:
                        print(f"would delete .trash/{day}")
                    else:
                        rmtree_frozen(os.path.join(trash, day))
                        print(f"deleted .trash/{day}")
        # 2. keep only the newest N generations per llm|gguf/<org>/<name>
        gens = {}
        # manifest order, not added_at: rows are appended under the lock, clocks can skew
        for i, (e, row) in enumerate(latest_rows(r).items()):
            parts = e.split("/")
            if row["source"] == "retired" or parts[0] not in GC_KINDS or len(parts) != 4:
                continue
            if KEEP.search(row["note"]) or not os.path.isdir(os.path.join(r, e)):
                continue
            gens.setdefault("/".join(parts[:3]), []).append((i, e))
        for key, lst in sorted(gens.items()):
            lst.sort(reverse=True)
            for _, old in lst[a.keep_gens:]:
                if a.dry_run:
                    print(f"would retire {old} (newer generations exist under {key})")
                else:
                    retire_entry(r, old, a.keep_days)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest"); p.add_argument("entry"); p.add_argument("--source", required=True)
    p.add_argument("--revision"); p.add_argument("--license"); p.add_argument("--note")
    p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("add"); p.add_argument("hf_repo"); p.add_argument("--revision")
    p.add_argument("--include", action="append"); p.add_argument("--to", default="llm", choices=GC_KINDS)
    p.add_argument("--license"); p.add_argument("--note"); p.set_defaults(fn=cmd_add)
    p = sub.add_parser("pull"); p.add_argument("model"); p.add_argument("--license")
    p.add_argument("--note"); p.set_defaults(fn=cmd_pull)
    p = sub.add_parser("verify"); p.add_argument("entry", nargs="*")
    p.add_argument("--ollama", action="store_true"); p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("ls"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_ls)
    p = sub.add_parser("retire"); p.add_argument("entry"); p.add_argument("--keep-days", type=int, default=14)
    p.set_defaults(fn=cmd_retire)
    p = sub.add_parser("gc"); p.add_argument("--keep-days", type=int, default=14)
    p.add_argument("--keep-gens", type=int, default=2); p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_gc)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
