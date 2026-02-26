from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DosePair:
    amplification: float
    cyclin_throttle: float

    def key(self, precision: int = 6) -> tuple[float, float]:
        return (round(self.amplification, precision), round(self.cyclin_throttle, precision))

    def to_dict(self) -> dict[str, float]:
        return {
            "amplification": float(self.amplification),
            "cyclin_throttle": float(self.cyclin_throttle),
        }


@dataclass(frozen=True)
class Condition:
    tissue: str
    donor_age: int
    dose: DosePair


@dataclass(frozen=True)
class SweepProfile:
    name: str
    replicates_per_condition: int
    n_divisions: int
    fucci_cells: int
    bootstrap_n: int
    workers: int | str


@dataclass
class TaskOutput:
    tissue: str
    donor_age: int
    amplification: float
    cyclin_throttle: float
    replicate_count: int
    mutation_reduction: list[float]
    viability: list[float]
    sphase_fold: list[float]
    leakage: list[float]
    on_half_life: list[float]
    off_half_life: list[float]
    on_r2: list[float]
    off_r2: list[float]

    def to_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for i in range(self.replicate_count):
            records.append(
                {
                    "tissue": self.tissue,
                    "donor_age": int(self.donor_age),
                    "amplification": float(self.amplification),
                    "cyclin_throttle": float(self.cyclin_throttle),
                    "replicate": i,
                    "mutation_reduction": float(self.mutation_reduction[i]),
                    "viability": float(self.viability[i]),
                    "sphase_fold": float(self.sphase_fold[i]),
                    "leakage": float(self.leakage[i]),
                    "on_half_life": float(self.on_half_life[i]),
                    "off_half_life": float(self.off_half_life[i]),
                    "on_r2": float(self.on_r2[i]),
                    "off_r2": float(self.off_r2[i]),
                }
            )
        return records
