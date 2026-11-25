"""Imports every model module so Base.metadata is complete (for Alembic and schema tests)."""

from voice_platform.agents import models as _agents  # noqa: F401
from voice_platform.audit import models as _audit  # noqa: F401
from voice_platform.events import models as _events  # noqa: F401
from voice_platform.identity import models as _identity  # noqa: F401
from voice_platform.infrastructure.db_base import Base
from voice_platform.tenancy import models as _tenancy  # noqa: F401

metadata = Base.metadata
