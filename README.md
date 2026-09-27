# nfs-model-library

One shared model library on a NAS, one writer, many readers, every file with a checksum and a source.

| File | What |
|---|---|
| `mlib.py` | `ingest`, `add` (hf download + ingest), `pull` (ollama pull + record), `verify`, `ls`, `retire`, `gc`. Standard library only. Runs on the librarian node. |
| `mounts.md` | NFS export rules on the NAS and mount options for the librarian and the readers. |
| `tests/` | Offline tests on a fake library: `python3 -m unittest discover -s tests`. |

```sh
export MLIB_ROOT=/srv/models
mlib.py add Qwen/Qwen2.5-Coder-32B-Instruct --include '*.safetensors' --include '*.json' --license Apache-2.0
mlib.py ingest gguf/unsloth/gpt-oss-120b-GGUF/mxfp4 --source https://huggingface.co/unsloth/gpt-oss-120b-GGUF --license Apache-2.0
mlib.py pull gpt-oss:20b           # the librarian's Ollama must run with OLLAMA_MODELS=$MLIB_ROOT/ollama
mlib.py verify --ollama            # exit 1 on any mismatch, missing or unlisted file, or rewritten SHA256SUMS
mlib.py ls
mlib.py gc --dry-run               # trash older than 14 days, more than 2 generations per model
```

`add` resolves the revision (default `main`) to a commit before downloading, names the entry directory by the first 12 characters of that commit, downloads every file from that commit, and records `main@<commit>` in the manifest. Two commits of the same model are two entries, which is what lets `gc` keep two generations.

Entries are frozen after ingest (files 0444, directories 0555). To replace one, `retire` it and `add` again; the manifest keeps both rows. `MANIFEST.tsv` is append-only and lives in the library, so readers can see provenance without this tool, and `sha256sum -c SHA256SUMS` inside an entry needs no tool either.

`ollama/` is Ollama's own store. `mlib.py` never writes checksums into it or freezes it; `pull` records the Ollama manifest digest, and `verify --ollama` checks every blob against its `sha256-<digest>` file name and flags `-partial` leftovers. `gc` only touches `llm/` and `gguf/`; an entry whose note contains the word `keep` is never retired and does not count toward the two generations.

Notes:

- `hf download` 0.36 keeps only the last of repeated `--include` flags, so `add` runs one download per pattern against the same commit.
- On a NAS share with per-user ACLs (QuTS hero on ZFS, `aclmode=passthrough`), `chmod 0444` does not stop anyone the share ACL lets write. When an entry carries an NFSv4 ACL (`system.nfs4_acl`), freezing replaces it with read-only `OWNER@`/`GROUP@`/`EVERYONE@` entries; the owner keeps the right to chmod, so `retire` can thaw it.
- Tested on a real NFSv4.1 mount (`hard,nconnect=4`) from two nodes at once: 2 x 1500 lock/replace/append rounds, no lost update, no torn manifest row. Run the suite there with `TMPDIR=<mount>/tmp python3 -m unittest discover -s tests` as a non-root user.

Apache-2.0.
