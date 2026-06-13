"""Provider Webhook endpoint."""

from typing import Annotated

from litestar.connection.request import Request
from litestar.handlers.http_handlers.decorators import post
from litestar.params import PathParameter
from litestar.router import Router

from anibridge.app.exceptions import SchedulerNotInitializedError
from anibridge.app.logging import get_logger
from anibridge.app.utils.async_tasks import schedule_task
from anibridge.app.web.state import get_app_state

__all__ = ["router"]

log = get_logger(__name__)


@post(path="/{provider_namespace:str}", status_code=200)
async def provider_webhook(
    provider_namespace: Annotated[str, PathParameter()],
    request: Request,
) -> None:
    """Receive Provider webhook and trigger a targeted sync."""
    log.info("Received webhook for provider '%s'", provider_namespace)
    scheduler = get_app_state().scheduler
    if not scheduler:
        log.warning("Scheduler not available")
        raise SchedulerNotInitializedError("Scheduler not available")

    candidates = scheduler.get_profiles_for_library_provider(provider_namespace)
    for profile_name in candidates:
        try:
            is_valid, library_keys = await scheduler.bridge_clients[
                profile_name
            ].parse_webhook(request)
            if not is_valid:
                continue

            log.info(
                "Triggering sync for profile '%s' and library keys: %s",
                profile_name,
                library_keys,
            )
            schedule_task(
                scheduler.trigger_profile_sync(
                    profile_name,
                    poll=False,
                    library_keys=library_keys,
                    source="webhook:provider",
                ),
                name=f"webhook_sync:{profile_name}",
            )
        except KeyError:
            log.error("No bridge client found for profile '%s'", profile_name)
            continue


router = Router(path="", route_handlers=[provider_webhook])
