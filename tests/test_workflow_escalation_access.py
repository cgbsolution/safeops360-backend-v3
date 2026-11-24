"""Escalation tasks must be actionable by the person they were escalated to.

House style: pure functions, no DB. Regression for the 2026-11-24 audit: the
overdue sweep raises '[Escalation] <step>' tasks on the ORIGINAL step but
assigns them to the step's escalationRole holder (e.g. HSE Manager for a
Supervisor step); the action gate demanded the original approverRole, so 41
open escalation tasks could never be completed by their assignee.
"""

from types import SimpleNamespace

from app.services.workflow_engine import escalation_task_roles, is_escalation_task


def step(approver=None, escalation=None):
    return SimpleNamespace(approverRole=approver, escalationRole=escalation)


def task(name):
    return SimpleNamespace(stepName=name)


def test_normal_task_needs_the_step_role_only():
    assert escalation_task_roles(task("Section Head Review"), step("SUPERVISOR", "HSE_MANAGER")) == ["SUPERVISOR"]


def test_escalation_task_also_accepts_the_escalation_role():
    roles = escalation_task_roles(task("[Escalation] Section Head Review"), step("SUPERVISOR", "HSE_MANAGER"))
    assert roles == ["SUPERVISOR", "HSE_MANAGER"]


def test_field_resolved_step_without_role_stays_unrestricted():
    assert escalation_task_roles(task("Action Owner Executes"), step(None, "HSE_MANAGER")) == []


def test_escalation_on_field_resolved_step_is_for_the_escalation_role():
    assert escalation_task_roles(task("[Escalation] Action Owner Executes"), step(None, "HSE_MANAGER")) == ["HSE_MANAGER"]


def test_escalation_prefix_detection():
    assert is_escalation_task(task("[Escalation] Joint Review"))
    assert not is_escalation_task(task("Joint Review"))
    assert not is_escalation_task(task(None))
