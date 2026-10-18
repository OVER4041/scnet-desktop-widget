"""Read-only SSH support extracted from OVER4041/mcp-scnet-hpc."""
from pathlib import PurePosixPath
import re
import shlex
from .config import BridgeError, Cluster, Settings
from .transport import SSH, Result

def quote(value: str) -> str:
    return shlex.quote(value)

def remote_path(value: str) -> str:
    if not isinstance(value, str) or not value or any((ord(c) < 32 for c in value)):
        raise BridgeError('Remote path must be nonempty and contain no control characters')
    if value.startswith(('/', '~')) or '\\' in value:
        raise BridgeError('Use a POSIX path relative to remote_root (for example cu_hpc/run.slurm)')
    if '..' in PurePosixPath(value).parts:
        raise BridgeError('Parent directory traversal is not allowed')
    return value

def job_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch('[1-9][0-9]*(?:_[0-9]+)?', value):
        raise BridgeError('job_id must be a numeric Slurm job ID or array task ID')
    return value

class Bridge:

    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.ssh = transport if transport is not None else SSH(settings)

    def cluster(self, name: str) -> Cluster:
        try:
            return self.settings.clusters[name]
        except KeyError as exc:
            raise BridgeError(f'Unknown cluster {name!r}; call list_clusters') from exc

    def preamble(self, cluster: Cluster) -> str:
        root = '"$HOME"' if cluster.remote_root == '~' else quote(cluster.remote_root)
        return f'set -eu\nexport LC_ALL=C\nroot=$(realpath -e -- {root})\ntest "$root" != / || {{ echo "remote_root cannot be /" >&2; exit 64; }}\ncd -- "$root"\n'

    def guard(self, value: str, variable: str='target', kind: str='') -> str:
        value = remote_path(value)
        result = f'{variable}=$(realpath -m -- {quote(value)})\ncase "${variable}" in "$root"|"$root"/*) ;; *) echo "Path escapes remote_root (possibly through a symlink)" >&2; exit 64;; esac\n'
        if kind:
            flag = {'file': '-f', 'directory': '-d'}[kind]
            result += f'test {flag} "${variable}" || {{ echo "Missing {kind}" >&2; exit 66; }}\n'
        return result

    async def run(self, name: str, body: str, **kwargs) -> Result:
        chosen = self.cluster(name)
        return (await self.ssh.run(chosen, self.preamble(chosen) + body, **kwargs)).checked()
