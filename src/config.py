"""Central configuration."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# ATM_GENERATED_DIR relocates agent artifacts (deployments mount a volume; tests use a tmpdir so
# a test run never writes into the working tree).
GENERATED_DIR = Path(os.environ.get("ATM_GENERATED_DIR") or (PROJECT_ROOT / "generated"))

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))

# Quality gate policy (design doc §4.4) — tune as you learn the app's behavior
GATE_POLICY = {
    "UNIT": {"min_pass_rate": 1.0},
    "QG1": {"min_pass_rate": 0.95, "max_p95_ms": 800, "max_p99_ms": 1500,
            "max_error_rate": 0.01, "min_throughput_rps": 0},
    "QG2": {"min_pass_rate": 0.98},
    "COVERAGE": {"min_coverage": 0.70},   # statement coverage floor (advisory unless coverage_blocking)
}
