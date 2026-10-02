"""Checks that feed the dashboard."""

import logging
import time

from naruto.services import Services

logger = logging.getLogger(__name__)


async def check_model(services: Services) -> None:
    """Ask the inference server which models it serves."""
    status = services.status
    try:
        models = await services.llm.list_models()
    except Exception as exc:
        if status.llm_reachable is not False:
            logger.warning("Model server unreachable: %s", type(exc).__name__)
        status.llm_reachable = False
        status.llm_error = f"{type(exc).__name__}: {exc}"[:300]
    else:
        if status.llm_reachable is False:
            logger.info("Model server reachable again")
        status.llm_reachable = True
        status.llm_models = models
        status.llm_error = None
    status.llm_checked_at = time.time()
