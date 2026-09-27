# Mounts and exports

## On the NAS (QNAP TS-464, QuTS hero 6.0.2)

Shared folder `models`. NFS service: Control Panel > Network & File Services >
Win/Mac/NFS/WebDAV > NFS Service, with NFSv4.1 enabled. This firmware also
offers "NFSv4.2 (Beta)"; the library does not depend on anything 4.2 adds, so
it stays on 4.1.

Host access: Control Panel > Privilege > Shared Folders > Edit Shared Folder
Permission, permission type "NFS host access" (繁中介面：NFS 主機存取). One
row per node:

| Host / IP / Network | Permission | Squash Option | Why |
|---|---|---|---|
| librarian (DGX Spark A) | Read/Write (讀取/寫入) | Squash no users (不作 Squash), or Squash all users to the `mlib` account | the only writer |
| DGX Spark B | Read only (唯讀) | Squash all users (Squash 所有使用者) | reader |
| Intel workstation (ComfyUI) | Read only (唯讀) | Squash all users | reader |
| NAS itself (Ollama container, Day 8) | bind mount, `:ro` | n/a | reader; pulls happen on the librarian |

Write each node as a single address (`/32`). A network entry such as
`192.0.2.0/24` makes every node on it a writer or none of them; and
`192.0.2.0/32` matches only the address `.0`, that is, nobody.

## On the librarian (read-write)

```
nas:/models  /srv/models  nfs4  rw,vers=4.1,hard,noatime,nconnect=4,rsize=1048576,wsize=1048576,_netdev  0 0
```

`MLIB_ROOT=/srv/models`. Ollama on this node runs with
`OLLAMA_MODELS=/srv/models/ollama`; it is the only Ollama allowed to pull, and
pulls go through `mlib.py pull` so the manifest gets a row.

## On every reader

```
nas:/models  /srv/models  nfs4  ro,vers=4.1,hard,noatime,nconnect=4,rsize=1048576,_netdev  0 0
```

Ollama on a reader runs with the same `OLLAMA_MODELS=/srv/models/ollama`;
on a read-only mount it logs one warning about its cache and serves normally
(Day 8 measurement). vLLM and llama.cpp point straight at
`/srv/models/llm/<org>/<name>/<commit12>`.

## Why these options

`hard` on purpose: a soft mount returns I/O errors into a model load half way
through, and the loader may not notice.

`nconnect=4` because a single NFS TCP connection does not fill 10GbE (Day 9);
Day 14 measures whether 4 is the right number. It only takes effect on the
first mount to a given NAS address: a second mount of the same server shares
the first one's connections, whatever its own `nconnect` says. Check with
`ss -tn 'dst <nas>:2049' | tail -n +2 | wc -l`.

No `autofs`: Day 11's snapshot found one node with an automount and one
without, and that is drift. Every node gets the same fstab line, differing
only in `rw` and `ro`.
