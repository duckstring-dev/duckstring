from __future__ import annotations

import json
import re
from pathlib import Path
from textwrap import dedent
from typing import Any, Optional
from urllib.parse import urlparse

import typer
from click.shell_completion import CompletionItem

from duckstring import Catchment

app = typer.Typer(help="Work with Catchments.", add_completion=False, no_args_is_help=True)

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_GIT_SCP_RE = re.compile(r"^[^@\s]+@[^:\s]+:[^\s]+$")

# TODO


