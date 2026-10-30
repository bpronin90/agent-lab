"""Personal AI Agent Lab: a minimal PydanticAI agent runtime."""

__version__ = "0.1.0"

import os as _os

# Keep CLI and container logs clean; the startup banner is purely cosmetic.
_os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
