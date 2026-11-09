# Official Containerlab runtime

`containerlab.lock.json` pins the official **v0.77.0** Linux amd64 and arm64
release archives and the SHA-256 of each extracted executable. Archive hashes
come from the upstream release assets. `CONTAINERLAB-LICENSE` retains its license.

Prepare with `python -m netopsbench.platform.runtime.build_tools`. Use `--archive`
for an offline release package and `--output` for a private destination. Runtime
selection uses `NETOPSBENCH_RUNTIME_TOOLS` or the package's ignored `_bin/`
directory. The installer publishes its manifest last. The resolver verifies
that manifest, the actual executable hash and the reported version before use.
There is no system binary fallback, Go fork, or patch application.

Upstream deployment, Docker bridge/DNS, and node stop/start own networking.
NetOpsBench retains its existing SONiC configuration/bootstrap wrapper with
sequential service starts, optional missing-service handling, readable config
files and full readiness checks. It does not install a management daemon or
modify Docker configuration. Runtime and campaign identity records the actual
binary version/hash and startup script hash; historical custom-backend campaigns
and capacity samples must remain archived rather than being resumed.

The baseline version includes the upstream parking-node implementation without
the older veth peer-index ownership heuristic. Verify stop/start and complete
routing/Pingmesh recovery on an independent lab when updating this release.
New upstream blockers require a reproduction and minimal correction evidence;
performance forks are not an automatic fallback.

The official v0.77.0 Docker runtime itself selects `nat-unprotected` on newer
Docker engines (see [upstream implementation](https://github.com/srl-labs/containerlab/blob/v0.77.0/runtime/docker/docker.go#L294-L305)).
Using upstream networking does not imply Docker’s default `nat` port filtering.
NetOpsBench does not override this upstream network option.
