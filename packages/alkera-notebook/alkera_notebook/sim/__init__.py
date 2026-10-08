"""The agent simulator: drives every notebook tool against an engine, checks
invariants after each step, and evaluates a real model on scripted tasks.

Targets: the in-process reference engine (:mod:`alkera_notebook.sim.reference`)
and the real engine (:mod:`alkera_notebook.sim.targets`). Drivers: scripted
scenarios (:mod:`alkera_notebook.sim.scenarios`), Hypothesis state machines
(:mod:`alkera_notebook.sim.machines`) and the model loop
(:mod:`alkera_notebook.sim.model`).
"""
