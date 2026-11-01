"""Private notification, reconciliation and queue-ingestion triggers."""

import json
import logging
import os
from contextlib import ExitStack, contextmanager
from datetime import datetime
from uuid import UUID

import azure.functions as func

from adapters import BlobStore, GraphClient, IngestQueue, emit, managed_identity
from core import (
    Config,
    GraphFailure,
    Processor,
    Reconciler,
    UTC,
    timestamp,
    transcript_resource,
)

app = func.FunctionApp()

# SDK logging can expose HTTP headers/remote error details; emit only our bounded events.
for name in ("azure", "urllib3", "requests", "msal"):
    logging.getLogger(name).setLevel(logging.CRITICAL)


def enrollment():
    value = json.loads(os.environ["TEAMS_ORGANIZERS"])
    tenant = str(UUID(value["tenantId"]))
    organizers = tuple(str(UUID(item)) for item in value["organizerIds"])
    if not organizers or len(set(organizers)) != len(organizers):
        raise ValueError("invalid_organizer_configuration")
    return tenant, organizers


@contextmanager
def components(organizer_id=None):
    tenant, organizers = enrollment()
    organizer_id = organizer_id or organizers[0]
    if organizer_id not in organizers:
        raise ValueError("organizer_not_enrolled")
    config = Config(
        tenant_id=tenant,
        organizer_id=organizer_id,
        app_id=os.environ["TEAMS_APP_CLIENT_ID"],
        client_state=os.environ["TEAMS_CLIENT_STATE"],
        notification_url=os.environ["TEAMS_NOTIFICATION_URL"],
    )
    with ExitStack() as clients:
        identity = managed_identity(os.environ["AZURE_CLIENT_ID"])
        clients.callback(identity.close)
        store = BlobStore(
            os.environ["TEAMS_STORAGE_BLOB_ENDPOINT"], identity, organizer_id
        )
        clients.callback(store.service.close)
        graph = GraphClient(config, identity)
        clients.callback(graph.close)

        def organizer_emit(name, **dimensions):
            emit(name, organizerId=organizer_id, **dimensions)

        reconciler = Reconciler(config, graph, store, organizer_emit)
        enqueue = None
        if os.environ.get("TEAMS_INGESTION_ENABLED", "false") == "true":
            queue = IngestQueue(os.environ["TEAMS_STORAGE_QUEUE_ENDPOINT"], identity)
            clients.callback(queue.client.close)
            enqueue = queue.enqueue
        yield (
            reconciler,
            Processor(config, store, reconciler, organizer_emit, enqueue=enqueue),
        )


@app.timer_trigger(
    arg_name="timer", schedule="0 */15 * * * *", run_on_startup=False, use_monitor=True
)
def subscription_timer(timer: func.TimerRequest):
    mode = os.environ.get("TEAMS_RECONCILIATION_MODE", "disabled")
    if mode == "disabled":
        return
    failed = False
    for organizer_id in enrollment()[1]:
        try:
            with components(organizer_id) as (reconciler, processor):
                reconciler.reconcile(datetime.now(UTC), rollback=mode == "rollback")
                if mode == "enabled" and processor.enqueue:
                    reconciler.store.handover_archived_notifications(
                        processor.enqueue, datetime.now(UTC)
                    )
        except Exception as exc:
            reason = (
                str(exc)
                if isinstance(exc, GraphFailure)
                else "subscription_reconciliation_unavailable"
            )
            emit("reconciliation_failure", organizerId=organizer_id, reason=reason)
            failed = True
    if failed:
        raise RuntimeError("subscription_reconciliation_unavailable") from None


@app.event_hub_message_trigger(
    arg_name="event",
    event_hub_name="%TEAMS_EVENT_HUB_NAME%",
    connection="TeamsEventHub",
    consumer_group="$Default",
    cardinality="one",
)
@app.retry(strategy="fixed_delay", max_retry_count="-1", delay_interval="00:00:10")
def transcript_notifications(event: func.EventHubEvent):
    try:
        metadata = event.metadata or {}
        # Only broker-owned identifiers, never application properties or raw trigger metadata.
        partition = metadata.get("PartitionContext", {})
        if isinstance(partition, str):
            import json

            partition = json.loads(partition)
        broker = {
            "partitionId": partition.get("PartitionId")
            if isinstance(partition, dict)
            else None,
            "sequenceNumber": event.sequence_number,
            "offset": str(event.offset),
            "enqueuedAt": timestamp(event.enqueued_time),
        }
        process_notifications(event.get_body(), broker, datetime.now(UTC))
    except Exception:
        emit("processing_failure")
        # The runtime retries indefinitely and does not advance the partition checkpoint.
        raise RuntimeError("notification_processing_unavailable") from None


def process_notifications(body, broker, now):
    """Route each event once; one organizer's failure cannot starve the other."""
    organizers = enrollment()[1]
    with ExitStack() as stack:
        processors = {}

        def processor_for(oid):
            if oid not in processors:
                processors[oid] = stack.enter_context(components(oid))[1]
            return processors[oid]

        try:
            envelope = json.loads(body)
        except (ValueError, UnicodeError):
            return processor_for(organizers[0]).process(body, broker, now)
        if (
            not isinstance(envelope, dict)
            or not isinstance(envelope.get("value"), list)
            or not envelope["value"]
        ):
            return processor_for(organizers[0]).process(body, broker, now)
        failed = False
        for notification in envelope["value"]:
            try:
                organizer = None
                if isinstance(notification, dict):
                    resource = notification.get("resource")
                    parsed = (
                        transcript_resource(resource)
                        if isinstance(resource, str) and len(resource) <= 4096
                        else None
                    )
                    if parsed:
                        organizer = parsed[0].lower()
                    elif isinstance(resource, str):
                        organizer = next(
                            (
                                oid
                                for oid in organizers
                                if resource.lower()
                                == f"users/{oid}/onlineMeetings/getAllTranscripts".lower()
                            ),
                            None,
                        )
                    if organizer is None and notification.get("subscriptionId") != "NA":
                        lookup_failed = False
                        for oid in organizers:
                            try:
                                state = processor_for(oid).store.read_state()
                                if any(
                                    s["id"] == notification.get("subscriptionId")
                                    for s in state.get("acceptedSubscriptions", [])
                                ):
                                    organizer = oid
                                    break
                            except Exception:
                                lookup_failed = True
                        if organizer is None and lookup_failed:
                            raise RuntimeError("notification_routing_unavailable")
                processor_for(
                    organizer if organizer in organizers else organizers[0]
                ).process_one(notification, broker, now)
            except Exception:
                emit("processing_failure", organizerId=organizer or "unknown")
                failed = True
        if failed:
            raise RuntimeError("notification_processing_unavailable")


@app.queue_trigger(
    arg_name="message", queue_name="transcript-ingest", connection="AzureWebJobsStorage"
)
def transcript_ingest(message: func.QueueMessage):
    import json
    from corpus import CorpusError
    from ingestion import CosmosWriter, Embedder, Ingester

    if os.environ.get("TEAMS_INGESTION_ENABLED", "false") != "true":
        raise RuntimeError("ingestion_disabled")
    organizer_id = None
    try:
        work = json.loads(message.get_body())
        organizer_id = work.get("organizerId")
        if organizer_id not in enrollment()[1]:
            raise ValueError("organizer_not_enrolled")
        with components(organizer_id) as (reconciler, _), ExitStack() as clients:
            graph = reconciler.graph
            writer = CosmosWriter(
                os.environ["TEAMS_COSMOS_ENDPOINT"],
                os.environ["TEAMS_DATABASE"],
                os.environ["TEAMS_CONTAINER"],
                graph.identity,
            )
            # Cosmos 4.9.0 exposes context-manager cleanup, not close().
            clients.enter_context(writer.client)
            embedder = Embedder(
                os.environ["TEAMS_EMBEDDING_ENDPOINT"],
                os.environ["TEAMS_EMBEDDING_KEY"],
            )
            clients.callback(embedder.session.close)
            status = Ingester(
                writer,
                graph,
                embedder,
                reconciler.config.tenant_id,
                reconciler.config.organizer_id,
            ).ingest(work)
        emit("ingestion_complete", organizerId=organizer_id, status=status)
    except CorpusError as exc:
        if str(exc) == "transcript_expired":
            emit("ingestion_expired")
            return
        emit("ingestion_failure", organizerId=organizer_id, reason=str(exc))
        raise RuntimeError(str(exc)) from None
    except GraphFailure as exc:
        emit(
            "ingestion_failure",
            organizerId=organizer_id,
            reason=str(exc),
            httpStatus=exc.status,
        )
        raise RuntimeError(str(exc)) from None
    except Exception:
        emit(
            "ingestion_failure",
            organizerId=organizer_id,
            reason="ingestion_unavailable",
        )
        raise RuntimeError("ingestion_unavailable") from None
