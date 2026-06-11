"""Local pre-deployment testing: Puddles (Source snapshots) + a single local Pond Run.

No engine, no FastAPI, no Ducks — ``hydrate`` materialises ``@puddle`` definitions into
``puddles/ponds/{source}/data/`` and ``run`` executes the Pond's Ripples in topo order against
them, writing output to ``puddles/out/``.
"""

from .hydrate import hydrate
from .project import Project, load_project
from .runner import run_pond

__all__ = ["Project", "load_project", "hydrate", "run_pond"]
