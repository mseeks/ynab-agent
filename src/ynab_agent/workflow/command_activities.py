"""The I/O ports of the command-confirm subsystem, as Temporal activities.

A standing command ("always categorize X as Y") grants standing autonomy, so it
is not blessed inline — it goes through a read-back + one-word confirm (SPEC
§5c, §0.6). These wire that flow: the confirm workflow opens its read-back
thread (:func:`open_command_thread`); the reply is read by the *shared* offer
interpreter (``offer_activities.interpret_offer_reply``); a yes blesses the rule
via the registry and confirms (:func:`accept_command`); a no sends a brief note
(:func:`decline_command`). Heavy clients are imported lazily so they never enter
a workflow sandbox.
"""

from __future__ import annotations

from temporalio import activity

from ynab_agent.learn.events import ExplicitCommand


def _category_id(command: ExplicitCommand) -> str | None:
    """The single category a command names, or ``None`` for a split."""
    from ynab_agent.domain.allocations import ProposedCategory

    allocation = command.action.allocation
    if isinstance(allocation, ProposedCategory):
        return str(allocation.category)
    return None


async def _category_names() -> dict[str, str]:
    """A category-id→name map read from YNAB (the source of truth)."""
    import asyncio

    from ynab_agent.ynab.client import YnabClient

    client = YnabClient.from_env()
    spends = await asyncio.to_thread(client.category_spends)
    return {str(spend.category): spend.name for spend in spends}


def _category_display(command: ExplicitCommand, names: dict[str, str]) -> str:
    """The display name of the category a command names (best effort)."""
    cid = _category_id(command)
    if cid is None:
        return "a split"
    return names.get(cid, cid)


@activity.defn
async def open_command_thread(command: ExplicitCommand) -> str:
    """Open the read-back thread echoing the command and return its id (§5c).

    A new standalone thread (its own ``yacmd-`` idempotency label, so a retry
    re-finds it rather than sending twice), addressed to the owners. The payee
    comes from the command; the category name from a YNAB read at send time.
    """
    import asyncio

    from ynab_agent.agentic.compose import render_command_confirm
    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings
    from ynab_agent.workflow.command_types import command_confirm_id

    settings = Settings()
    names = await _category_names()
    payee = command.match.payee_pattern
    category = _category_display(command, names)
    body = render_command_confirm(payee, category)
    mail = MailClient.from_env()
    return await asyncio.to_thread(
        mail.open_thread,
        inbox_id=settings.inbox,
        to=list(settings.owners),
        subject=f"Confirm: always {payee} as {category}?",
        body=body,
        txn_label=f"yacmd-{command_confirm_id(command)}",
    )


@activity.defn
async def accept_command(command: ExplicitCommand, thread_id: str) -> None:
    """Bless the command and confirm — the owner confirmed it (SPEC §5c, §14).

    Signals the durable registry's ``bless`` (signal-with-start, reusing the
    running singleton) with the explicit command, which trusts the rule for
    auto-apply (``source=human_explicit``), then sends a confirmation on the
    read-back thread (idempotent on its accept label, so a retry never
    double-sends).
    """
    import asyncio

    from temporalio.common import WorkflowIDConflictPolicy

    from ynab_agent.agentic.compose import render_offer_accepted
    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings
    from ynab_agent.workflow.command_types import command_confirm_id
    from ynab_agent.workflow.registry_types import (
        REGISTRY_WORKFLOW_ID,
        RegistryParams,
    )
    from ynab_agent.workflow.temporal_client import client, task_queue

    temporal = await client()
    await temporal.start_workflow(
        "RuleRegistryWorkflow",
        RegistryParams(),
        id=REGISTRY_WORKFLOW_ID,
        task_queue=task_queue(),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="bless",
        start_signal_args=[command],
    )

    settings = Settings()
    names = await _category_names()
    body = render_offer_accepted(
        command.match.payee_pattern, _category_display(command, names)
    )
    mail = MailClient.from_env()
    await asyncio.to_thread(
        mail.send_on_thread,
        inbox_id=settings.inbox,
        thread_id=thread_id,
        body=body,
        seq_label=f"yacmd-accept-{command_confirm_id(command)}",
    )


@activity.defn
async def decline_command(command: ExplicitCommand, thread_id: str) -> None:
    """Send the brief "I'll keep proposing" note — owner declined (SPEC §5c)."""
    import asyncio

    from ynab_agent.agentic.compose import render_offer_declined
    from ynab_agent.mail.client import MailClient
    from ynab_agent.settings import Settings
    from ynab_agent.workflow.command_types import command_confirm_id

    settings = Settings()
    body = render_offer_declined(command.match.payee_pattern)
    mail = MailClient.from_env()
    await asyncio.to_thread(
        mail.send_on_thread,
        inbox_id=settings.inbox,
        thread_id=thread_id,
        body=body,
        seq_label=f"yacmd-decline-{command_confirm_id(command)}",
    )
