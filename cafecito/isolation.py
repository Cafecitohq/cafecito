"""Gate isolation backends. The gate runs candidate code — regenerated
regions included — so by default it executes with nothing but a restricted
environment between that code and the operator's machine. Isolation modes
put a real boundary there:

  none       today's behavior: restricted env only.
  sandbox    macOS sandbox-exec: no network (unix sockets excepted — test
             runners fork), file writes confined to the gate's scratch
             directory (worktree + HOME + TMPDIR live inside it) and the
             system temp roots. Reads stay open — tests import from
             anywhere on the machine they always could.
  container  docker/podman: tests run inside a container with
             --network=none and only the gate worktree mounted. Needs
             `container_image` in config; runtime auto-detected unless
             `container_runtime` pins one.

Fail closed: an unavailable backend must redden the gate, never silently
fall back to unisolated execution — the engine enforces this via
`unavailable()` before each real run. Landings whose verification facts are
all inherited execute nothing, so they need no boundary.
"""

from __future__ import annotations

import pathlib
import shutil
import sys

MODES = ("none", "sandbox", "container")

# restricted-env vars the container run re-creates inside (PATH/HOME/TMPDIR
# are the image's own — the whole point is that the host's aren't there)
_CONTAINER_ENV = ("PYTHONDONTWRITEBYTECODE=1", "MPLBACKEND=Agg",
                  "LC_ALL=C.UTF-8")


def _sb_quote(path: str) -> str:
    """A path as a sandbox-profile string literal (Scheme-ish syntax)."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(write_roots: list[str]) -> str:
    """sandbox-exec profile: default-allow (reads work), network denied
    except unix sockets (pytest/node runners fork over them), writes denied
    outside `write_roots` + the system temp roots + /dev (tty, null)."""
    roots = [str(pathlib.Path(r).resolve()) for r in write_roots]
    roots += ["/private/tmp", "/private/var/folders", "/dev"]
    allows = "\n".join(f"  (subpath {_sb_quote(r)})" for r in roots)
    return ("(version 1)\n"
            "(allow default)\n"
            "(deny network*)\n"
            "(allow network* (local unix) (remote unix))\n"
            "(deny file-write*)\n"
            f"(allow file-write*\n{allows})\n")


def container_runtime(pinned: str = "") -> str | None:
    """The container runtime to use: the pinned one if set, else the first
    of docker/podman on PATH. None when nothing is runnable."""
    for cand in ([pinned] if pinned else ["docker", "podman"]):
        if shutil.which(cand):
            return cand
    return None


def unavailable(mode: str, image: str = "", runtime: str = "") -> str | None:
    """Why `mode` cannot run on this machine, or None when it can."""
    if mode == "none":
        return None
    if mode == "sandbox":
        if sys.platform != "darwin":
            return "sandbox mode is macOS-only (sandbox-exec)"
        if not shutil.which("sandbox-exec"):
            return "sandbox-exec not on PATH"
        return None
    if mode == "container":
        if not image:
            return "container mode needs container_image in config"
        if container_runtime(runtime) is None:
            return (f"pinned runtime {runtime!r} not on PATH" if runtime
                    else "no container runtime (docker/podman) on PATH")
        return None
    return f"unknown isolation mode {mode!r}"


def wrap(cmd: list[str], mode: str, *, worktree: str = "",
         write_roots: list[str] | None = None, image: str = "",
         runtime: str = "") -> list[str]:
    """`cmd` wrapped for `mode`. Caller must have cleared unavailable()."""
    if mode == "none":
        return list(cmd)
    if mode == "sandbox":
        return ["sandbox-exec", "-p", sandbox_profile(write_roots or []),
                *cmd]
    if mode == "container":
        rt = container_runtime(runtime)
        env = [x for pair in _CONTAINER_ENV for x in ("-e", pair)]
        return [rt, "run", "--rm", "--network=none",
                "-v", f"{pathlib.Path(worktree).resolve()}:/work",
                "-w", "/work", *env, image, *cmd]
    raise ValueError(f"unknown isolation mode {mode!r}")
