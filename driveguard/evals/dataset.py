"""Labeled evaluation sets built from the simulator.

Each eval case is one *detected* incident (that is what the agents see), with
ground truth derived from the simulator's injected labels:

  overlaps an injected incident label    -> expected = that label's kind
  overlaps a hard-negative label         -> expected = "no_incident"
  overlaps nothing                       -> expected = "no_incident" (detector noise)

Detector misses (labels with no detection) cannot reach the agents, so they
are reported as dataset stats rather than eval cases.

Two splits from different seeds: `dev` is for error analysis and iteration,
`test` is held out for the numbers we report. Changing code to fix `dev`
failures and then quoting `dev` numbers would be teaching to the test.

The dataset JSON is small and committed (evals/datasets/). The database it
refers to is rebuilt deterministically from `sim_config` on demand, and its
identity is checked against `db_fingerprint` (a hash of the incident ids plus
expected labels, which is stable across SQLite page layouts).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from driveguard.clock import FakeClock
from driveguard.pipeline.broker import SqliteBroker
from driveguard.pipeline.consumer import Consumer, ConsumerConfig
from driveguard.pipeline.runner import ingest_batches
from driveguard.sim.generator import INCIDENT_KINDS, Label, SimConfig, simulate
from driveguard.store.repository import Store

CLASSES = ("hard_braking", "crash", "near_miss", "phone_distraction", "no_incident")
SPLITS = {
    "dev": SimConfig(seed=101, n_trips=90, n_drivers=12, trip_seconds=(240, 480), incident_rate=0.8,
                     negative_rate=0.8, note_rate=0.3),
    "test": SimConfig(seed=202, n_trips=90, n_drivers=12, trip_seconds=(240, 480), incident_rate=0.8,
                      negative_rate=0.8, note_rate=0.3),
    "mini": SimConfig(seed=303, n_trips=18, n_drivers=4, trip_seconds=(200, 320), incident_rate=0.9,
                      negative_rate=0.9, note_rate=0.3),
}
OVERLAP_SLACK_MS = 2000
REPO_DATASETS = Path(__file__).resolve().parents[2] / "evals" / "datasets"


@dataclass
class EvalCase:
    case_key: str  # stable, human-readable key: <split>-<nn>
    incident_id: str
    trip_id: str
    detected_kind: str
    expected: str
    truth_source: str  # incident | hard_negative | unlabeled
    label_kind: str | None
    label_window: list[int] | None
    label_evidence_event_ids: list[str] = field(default_factory=list)


@dataclass
class Dataset:
    name: str
    split: str
    sim_config: dict[str, Any]
    per_class: int
    cases: list[EvalCase]
    stats: dict[str, Any]
    db_fingerprint: str

    @property
    def dataset_hash(self) -> str:
        body = json.dumps([asdict(c) for c in self.cases], sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    def to_json(self) -> str:
        return json.dumps({**asdict(self), "dataset_hash": self.dataset_hash}, indent=1)

    @classmethod
    def load(cls, path: str | Path) -> "Dataset":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        raw.pop("dataset_hash", None)
        raw["cases"] = [EvalCase(**c) for c in raw["cases"]]
        return cls(**raw)


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int | None:
    """Overlap in ms of [a0,a1] and [b0,b1] (may be slightly negative: gaps up to
    OVERLAP_SLACK_MS still count as touching). None when they don't touch."""
    if a0 > b1 + OVERLAP_SLACK_MS or b0 > a1 + OVERLAP_SLACK_MS:
        return None
    return min(a1, b1) - max(a0, b0)


def label_incident(inc: dict[str, Any], labels: list[Label]) -> tuple[str, str, Label | None]:
    """(expected, truth_source, label) for one detected incident. Incident labels win
    over hard negatives; among several, the largest overlap wins."""
    cands = [(l, _overlap(inc["t_start_ms"], inc["t_end_ms"], l.t_start_ms, l.t_end_ms))
             for l in labels if l.trip_id == inc["trip_id"]]
    cands = [(l, o) for l, o in cands if o is not None]
    incidents = sorted((c for c in cands if c[0].is_incident), key=lambda c: -c[1])
    if incidents:
        return incidents[0][0].kind, "incident", incidents[0][0]
    negatives = sorted((c for c in cands if not c[0].is_incident), key=lambda c: -c[1])
    if negatives:
        return "no_incident", "hard_negative", negatives[0][0]
    return "no_incident", "unlabeled", None


def db_fingerprint(db_path: str | Path) -> str:
    with sqlite3.connect(db_path) as c:
        rows = c.execute("SELECT incident_id, kind, t_start_ms FROM detected_incidents "
                         "WHERE superseded_at_ms IS NULL ORDER BY incident_id").fetchall()
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]


async def _ingest(cfg: SimConfig, db_path: Path) -> list[Label]:
    sim = simulate(cfg)
    clock = FakeClock()
    store = await Store(db_path).connect()
    broker = await SqliteBroker(db_path.with_suffix(".queue.db"), clock).connect()
    try:
        await store.seed_reference(sim.drivers, sim.vehicles)
        consumer = Consumer(broker, store, clock, ConsumerConfig(jitter_seed=0))
        await ingest_batches([[e.to_json() for e in sim.events]], broker=broker, consumer=consumer, store=store,
                             clock=clock)
    finally:
        await broker.close()
        await store.close()
    with sqlite3.connect(db_path) as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db_path.with_suffix(".queue.db").unlink(missing_ok=True)
    return sim.labels


def build_db(split: str, db_path: str | Path) -> list[Label]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    coro = _ingest(SPLITS[split], db_path)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Called from inside an event loop (e.g. `eval run` on a fresh clone): asyncio.run would
    # raise, so run the ingest on its own loop in a worker thread (M-16).
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, coro).result()


def build_dataset(split: str, db_path: str | Path, per_class: int = 8, reuse_db: bool = True) -> Dataset:
    """Ingest is the slow part (~75 s per split); with reuse_db an existing database is
    kept and only the (fast, deterministic) simulation is re-run to recover labels."""
    if reuse_db and Path(db_path).exists():
        labels = simulate(SPLITS[split]).labels
    else:
        labels = build_db(split, db_path)
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        incs = [dict(r) for r in c.execute(
            "SELECT incident_id, trip_id, kind, t_start_ms, t_end_ms FROM detected_incidents "
            "WHERE superseded_at_ms IS NULL ORDER BY incident_id")]
    by_class: dict[str, list[EvalCase]] = {k: [] for k in CLASSES}
    for inc in incs:
        expected, source, lab = label_incident(inc, labels)
        by_class[expected].append(EvalCase(
            case_key="", incident_id=inc["incident_id"], trip_id=inc["trip_id"], detected_kind=inc["kind"],
            expected=expected, truth_source=source, label_kind=lab.kind if lab else None,
            label_window=[lab.t_start_ms, lab.t_end_ms] if lab else None,
            label_evidence_event_ids=list(lab.evidence_event_ids) if lab else []))
    rng = random.Random(f"{split}-{per_class}")
    chosen: list[EvalCase] = []
    for k in CLASSES:
        pool = by_class[k]
        chosen += rng.sample(pool, min(per_class, len(pool)))
    chosen.sort(key=lambda c: (CLASSES.index(c.expected), c.incident_id))
    for i, c in enumerate(chosen):
        c.case_key = f"{split}-{i:02d}"

    detected_labels = {id(label_incident(i, labels)[2]) for i in incs}
    missed = [l for l in labels if l.is_incident and id(l) not in detected_labels]
    stats = {
        "detected_incidents": len(incs),
        "available_by_expected": {k: len(v) for k, v in by_class.items()},
        "selected_by_expected": {k: sum(c.expected == k for c in chosen) for k in CLASSES},
        "selected_by_truth_source": {s: sum(c.truth_source == s for c in chosen)
                                     for s in ("incident", "hard_negative", "unlabeled")},
        "detector_kind_mismatch_selected": sum(c.detected_kind != c.expected for c in chosen),
        "injected_incidents": sum(l.is_incident for l in labels),
        "detector_missed_incidents": {k: sum(l.kind == k for l in missed) for k in INCIDENT_KINDS},
    }
    return Dataset(name=f"evalset-v2-{split}", split=split, sim_config=asdict(SPLITS[split]), per_class=per_class,
                   cases=chosen, stats=stats, db_fingerprint=db_fingerprint(db_path))


def ensure_db(ds: Dataset, db_path: str | Path) -> Path:
    """Rebuild the dataset's database if missing, and refuse to run on a mismatched one."""
    db_path = Path(db_path)
    if not db_path.exists():
        build_db(ds.split, db_path)
    fp = db_fingerprint(db_path)
    if fp != ds.db_fingerprint:
        raise RuntimeError(f"{db_path} does not match dataset {ds.name} (fingerprint {fp} != {ds.db_fingerprint}); "
                           "delete it to rebuild")
    return db_path
