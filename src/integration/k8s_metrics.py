"""Per-pod resource sampling via `kubectl` — the deployed/cloud performance dimension.

For a production-grade answer to "how much can ONE pod take, and what does it cost?", we ramp load
(k6) against the deployed ingress URL while sampling the target pod(s) CPU & memory from the cluster,
then report utilization against each pod's limits, plus restarts/OOM. Works on EKS and GKE alike
(needs metrics-server for `kubectl top`). Degrades gracefully: if kubectl/cluster is unreachable it
reports "not connected" — it never crashes a run.

Inputs come from the run config: k8s_namespace, k8s_selector (label selector, e.g. "app=idurar-api"),
optional k8s_kubeconfig (path) and k8s_context. Nothing here mutates the cluster (read-only).
"""
import shutil
import subprocess
import threading


def _kubectl(args, cfg, timeout=30):
    if not shutil.which("kubectl"):
        return None
    cmd = ["kubectl"]
    if cfg.get("kubeconfig"):
        cmd += ["--kubeconfig", cfg["kubeconfig"]]
    if cfg.get("context"):
        cmd += ["--context", cfg["context"]]
    if cfg.get("namespace"):
        cmd += ["-n", cfg["namespace"]]
    cmd += args
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def _cpu_m(v: str) -> float:
    """Kubernetes CPU quantity → millicores. '500m'→500, '1'→1000, '1.5'→1500, '250000000n'→250."""
    if not v:
        return 0.0
    v = str(v).strip()
    if v.endswith("m"):
        return float(v[:-1] or 0)
    if v.endswith("n"):
        return float(v[:-1] or 0) / 1e6
    if v.endswith("u"):
        return float(v[:-1] or 0) / 1e3
    try:
        return float(v) * 1000
    except ValueError:
        return 0.0


def _mem_mi(v: str) -> float:
    """Kubernetes memory quantity → MiB. '512Mi'→512, '1Gi'→1024, '500M'→~476, bytes→/2^20."""
    if not v:
        return 0.0
    v = str(v).strip()
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024,
             "K": 1000 / 1048576, "M": 1e6 / 1048576, "G": 1e9 / 1048576}
    for u, factor in units.items():
        if v.endswith(u):
            try:
                return float(v[:-len(u)]) * factor
            except ValueError:
                return 0.0
    try:
        return float(v) / 1048576  # raw bytes
    except ValueError:
        return 0.0


def available(cfg: dict) -> bool:
    if not shutil.which("kubectl"):
        return False
    r = _kubectl(["get", "pods", "--no-headers"], cfg, timeout=15)
    return bool(r and r.returncode == 0)


def pod_specs(cfg: dict) -> dict:
    """Per-pod resource limits/requests + restart counts (kubectl get pods -o json)."""
    import json
    sel = ["-l", cfg["selector"]] if cfg.get("selector") else []
    r = _kubectl(["get", "pods", *sel, "-o", "json"], cfg, timeout=25)
    out = {}
    if not (r and r.returncode == 0):
        return out
    try:
        data = json.loads(r.stdout or "{}")
    except Exception:
        return out
    for item in data.get("items", []):
        name = item.get("metadata", {}).get("name", "?")
        cpu_lim = mem_lim = cpu_req = mem_req = 0.0
        for c in item.get("spec", {}).get("containers", []):
            res = c.get("resources", {}) or {}
            cpu_lim += _cpu_m((res.get("limits") or {}).get("cpu", "0"))
            mem_lim += _mem_mi((res.get("limits") or {}).get("memory", "0"))
            cpu_req += _cpu_m((res.get("requests") or {}).get("cpu", "0"))
            mem_req += _mem_mi((res.get("requests") or {}).get("memory", "0"))
        restarts = sum(cs.get("restartCount", 0)
                       for cs in item.get("status", {}).get("containerStatuses", []) or [])
        out[name] = {"cpu_limit_m": cpu_lim, "mem_limit_mi": mem_lim,
                     "cpu_req_m": cpu_req, "mem_req_mi": mem_req, "restarts0": restarts,
                     "node": item.get("spec", {}).get("nodeName", "")}
    return out


def top(cfg: dict) -> dict:
    """One CPU/mem snapshot per pod (kubectl top pod — needs metrics-server)."""
    sel = ["-l", cfg["selector"]] if cfg.get("selector") else []
    r = _kubectl(["top", "pod", *sel, "--no-headers"], cfg, timeout=20)
    snap = {}
    if not (r and r.returncode == 0):
        return snap
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            snap[parts[0]] = {"cpu_m": _cpu_m(parts[1]), "mem_mi": _mem_mi(parts[2])}
    return snap


class PodSampler(threading.Thread):
    """Background sampler: polls `kubectl top` every `interval` sec until stopped, then builds a
    per-pod utilization report against each pod's limits."""
    def __init__(self, cfg: dict, interval: int = 5):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.interval = max(2, interval)
        self._stop = threading.Event()
        self.specs = pod_specs(cfg)
        self.samples = []   # [{pod, cpu_m, mem_mi}]

    def run(self):
        while not self._stop.is_set():
            for pod, v in top(self.cfg).items():
                self.samples.append({"pod": pod, **v})
            self._stop.wait(self.interval)

    def stop_and_report(self) -> dict:
        self._stop.set()
        self.join(timeout=self.interval + 5)
        end = pod_specs(self.cfg)   # re-read for restart deltas
        pods = []
        names = set(self.specs) | {s["pod"] for s in self.samples}
        for name in sorted(names):
            spec = self.specs.get(name, {})
            sm = [s for s in self.samples if s["pod"] == name]
            cpu = [s["cpu_m"] for s in sm] or [0]
            mem = [s["mem_mi"] for s in sm] or [0]
            cl, ml = spec.get("cpu_limit_m", 0), spec.get("mem_limit_mi", 0)
            restarts = end.get(name, {}).get("restarts0", spec.get("restarts0", 0)) - spec.get("restarts0", 0)
            pods.append({
                "pod": name, "node": spec.get("node", ""),
                "cpu_limit_m": round(cl), "mem_limit_mi": round(ml),
                "cpu_peak_m": round(max(cpu)), "cpu_avg_m": round(sum(cpu) / len(cpu)),
                "cpu_peak_pct": round(100 * max(cpu) / cl) if cl else None,
                "cpu_avg_pct": round(100 * (sum(cpu) / len(cpu)) / cl) if cl else None,
                "mem_peak_mi": round(max(mem)), "mem_avg_mi": round(sum(mem) / len(mem)),
                "mem_peak_pct": round(100 * max(mem) / ml) if ml else None,
                "restarts": max(0, restarts), "samples": len(sm),
            })
        return {"namespace": self.cfg.get("namespace"), "selector": self.cfg.get("selector"),
                "interval_s": self.interval, "pods": pods, "pod_count": len(pods),
                "connected": bool(self.specs or self.samples)}
