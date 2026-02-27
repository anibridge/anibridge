"""Provider Webhook endpoint."""

from fastapi.routing import APIRouter
from starlette.requests import Request

from anibridge.app import log
from anibridge.app.exceptions import SchedulerNotInitializedError
from anibridge.app.utils.async_tasks import schedule_task
from anibridge.app.web.state import get_app_state

__all__ = ["router"]

router = APIRouter()


@router.post("/{provider_namespace}")
async def provider_webhook(
    provider_namespace: str,
    request: Request,
) -> None:
    """Receive Provider webhook and trigger a targeted sync.

    Args:
        provider_namespace (str): The provider namespace from the URL path.
        request (Request): The incoming HTTP request.
    """
    log.info(
        "Webhook: Received webhook for provider '%s'",
        provider_namespace,
    )
    scheduler = get_app_state().scheduler
    if not scheduler:
        log.warning("Webhook - Scheduler not available")
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
                "Webhook: Triggering sync for profile '%s' and library keys: %s",
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
            log.error(
                "Webhook: No bridge client found for profile '%s'",
                profile_name,
            )
            continue
