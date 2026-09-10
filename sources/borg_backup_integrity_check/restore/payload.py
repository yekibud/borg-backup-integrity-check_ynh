"""PayloadRetriever: bring sampled (or full) large data onto the host and describe it generically."""

from __future__ import annotations

from dataclasses import dataclass

from ..evidence.models import Evidence
from ..logging_setup import get_logger
from ..report.models import SampleResult, VerificationLevel
from ..sampling.sampler import SampledObject
from .agent import HostAgent
from .planner import ComponentPlan

log = get_logger("payload")


@dataclass
class PayloadOutcome:
    samples: list[SampleResult]
    full_roots_ok: int = 0
    full_roots_failed: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


class PayloadRetriever:
    def __init__(self, agent: HostAgent, include_sender: bool = True) -> None:
        self.agent = agent
        self.include_sender = include_sender

    def retrieve(self, cp: ComponentPlan) -> PayloadOutcome:
        comp = cp.component
        outcome = PayloadOutcome(samples=[])
        roots_spec = []
        objects: list[SampledObject] = []
        for sample in cp.samples:
            root = sample.root
            if not root.live_path:
                outcome.errors.append(
                    f"{root.archive_path}: live path unknown, cannot place objects"
                )
                continue
            entry = {
                "archive_path": root.archive_path,
                "live_path": root.live_path,
                "full": cp.payload_mode == "full",
                "objects": [],
            }
            for obj in sample.objects:
                if obj.live_path:
                    entry["objects"].append(
                        {
                            "archive_path": obj.archive_path,
                            "live_path": obj.live_path,
                            "kind": obj.kind,
                        }
                    )
                    objects.append(obj)
            roots_spec.append(entry)
        if not roots_spec:
            return outcome
        result = self.agent.call(
            "extract-payload",
            spec={"archive": comp.archive.name, "roots": roots_spec},
            timeout=12 * 3600,
        )
        extracted: dict[str, bool] = {}
        for root_result in result.get("roots", []):
            if root_result.get("error"):
                outcome.errors.append(
                    f"{root_result['archive_path']}: {root_result['error'][:300]}"
                )
                if cp.payload_mode == "full":
                    outcome.full_roots_failed += 1
            elif cp.payload_mode == "full":
                outcome.full_roots_ok += 1
            for obj in root_result.get("objects", []):
                extracted[obj["archive_path"]] = bool(obj.get("extracted"))
        # Describe every sampled object generically on the host.
        describe_spec = {
            "include_sender": self.include_sender,
            "objects": [
                {
                    "archive_path": o.archive_path,
                    "live_path": o.live_path,
                    "kind": o.kind,
                    "mtime": o.mtime.isoformat() if o.mtime else None,
                    "size": o.size,
                    "relative_path": o.relative_path,
                }
                for o in objects
            ],
        }
        described = (
            self.agent.call("describe", spec=describe_spec, timeout=3600)
            if objects
            else {"objects": []}
        )
        evidence_by_path = {
            d["archive_path"]: Evidence.from_dict(d["evidence"])
            for d in described.get("objects", [])
        }
        for obj in objects:
            ev = evidence_by_path.get(obj.archive_path)
            was_extracted = extracted.get(obj.archive_path, False)
            if ev is None:
                ev = Evidence(
                    kind="unknown",
                    title=obj.relative_path.rsplit("/", 1)[-1],
                    when=obj.mtime,
                    size=obj.size,
                    readable=False,
                    error="not described",
                )
            ev.details.setdefault("relative_path", obj.relative_path)
            if not was_extracted:
                outcome.samples.append(
                    SampleResult(
                        ev, VerificationLevel.OBJECT_LISTED, obj.archive_path, error="not extracted"
                    )
                )
            elif not ev.readable:
                outcome.samples.append(
                    SampleResult(
                        ev,
                        VerificationLevel.OBJECT_EXTRACTED,
                        obj.archive_path,
                        error=ev.error or "unreadable",
                    )
                )
            else:
                outcome.samples.append(
                    SampleResult(ev, VerificationLevel.OBJECT_READABLE, obj.archive_path)
                )
        return outcome
