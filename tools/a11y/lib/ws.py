"""The workspace every Python stage reads and writes (KTD2, KTD3). Mirrors lib/cli.mjs."""
import datetime
import os

from lib.canon import ROOT, read_json

DEFAULTS = {
    "workspace": os.path.join(ROOT, "a11y"),
    "data": os.path.join(ROOT, "data"),
    "map": os.path.join(ROOT, "build", "map.json"),
    "base_url": "https://demo.mercury.com",
}
COMMITTED_POLICY = os.path.join(ROOT, "a11y", "policy.json")


class Workspace:
    def __init__(self, workspace=None, data=None, map=None, base_url=None, run=None):
        self.path = os.path.abspath(workspace or DEFAULTS["workspace"])
        self.data_dir = os.path.abspath(data or DEFAULTS["data"])
        self.map_path = os.path.abspath(map or DEFAULTS["map"])
        self.base_url = (base_url or DEFAULTS["base_url"]).rstrip("/")
        self.run = run

    @classmethod
    def from_args(cls, args):
        return cls(args.workspace, args.data, args.map, args.base_url, getattr(args, "run", None))

    # Paths -----------------------------------------------------------------

    @property
    def runs_dir(self):
        return os.path.join(self.path, "runs")

    @property
    def recipes_dir(self):
        return os.path.join(self.path, "recipes")

    @property
    def published_dir(self):
        return os.path.join(self.path, "published")

    @property
    def policy_path(self):
        own = os.path.join(self.path, "policy.json")
        return own if os.path.exists(own) else COMMITTED_POLICY

    def policy(self):
        return read_json(self.policy_path)

    def runs(self):
        """Run ids that have a states.json, oldest first."""
        if not os.path.isdir(self.runs_dir):
            return []
        return sorted(r for r in os.listdir(self.runs_dir)
                      if os.path.exists(os.path.join(self.runs_dir, r, "states.json")))

    def run_id(self, required=True):
        if self.run:
            return self.run
        runs = self.runs()
        if runs:
            return runs[-1]
        if required:
            raise SystemExit("no run found: run `audit.py plan` first, or pass --run")
        return None

    def run_dir(self, run_id=None):
        return os.path.join(self.runs_dir, run_id or self.run_id())

    def raw_dir(self, run_id=None):
        return os.path.join(self.run_dir(run_id), "raw")

    def file(self, name, run_id=None):
        return os.path.join(self.run_dir(run_id), name)

    @staticmethod
    def new_run_id(now=None):
        return (now or datetime.datetime.now()).strftime("%Y-%m-%d-%H%M")

    # Data ------------------------------------------------------------------

    def states(self, run_id=None):
        doc = read_json(self.file("states.json", run_id))
        return doc["states"] if doc else {}

    def load_map(self):
        m = read_json(self.map_path)
        if m is None:
            raise SystemExit(f"{self.map_path} not found: run `python3 build.py` first")
        return m

    def published_meta(self):
        return read_json(os.path.join(self.published_dir, "meta.json"))
