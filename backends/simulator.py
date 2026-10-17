"""SimulatorBackend: thin wrapper over sim.Simulator (no second observation schema)."""
from __future__ import annotations

from sim import Simulator

from .base import ActionResult


class SimulatorBackend:
    def __init__(self, campaign, seed: int = 0):
        self.sim = Simulator(campaign, seed=seed)

    @classmethod
    def wrap(cls, sim: Simulator) -> "SimulatorBackend":
        self = cls.__new__(cls)
        self.sim = sim
        return self

    def apply_plan(self, plan: dict) -> ActionResult:
        return self.sim.apply_plan(plan)

    def observe(self) -> dict:
        return self.sim.observe()

    def act(self, action: dict) -> ActionResult:
        return self.sim.act(action)

    def advance(self) -> dict:
        self.sim.step()
        return {"due_at": None}

    @property
    def done(self) -> bool:
        return self.sim.done

    @property
    def status(self) -> str:
        return self.sim.status
