"""FastAPI app factory.

`uvicorn app.main:app --reload` to run in development.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.db import engine, warmup
from app.licensing.enforcement import require_module
from app.licensing.plant_modules import ROUTER_PLANT_MODULE, cams_fire_only_guard, require_plant_module
from app.licensing.router_map import ROUTER_MODULE
from app.licensing.state import refresh_state
from app.routers import (
    agents,
    agents_config,
    alerts,
    anomalies,
    assurance,
    attachments,
    audit_compliance,
    audit_log,
    auth,
    brsr,
    business_excellence,
    business_excellence_p2,
    cams,
    cams_completion,
    capa,
    capture,
    supplier_portal,
    competency,
    training_engine,
    dashboard,
    devices,
    eai,
    erm,
    erm_attachments,
    erm_p2,
    erm_p3,
    erm_t3,
    epc_contractors,
    epc_dashboard,
    epc_gate,
    epc_induction,
    epc_mobilization,
    epc_sites,
    epc_workers,
    factory,
    factory_ext,
    fire_audits,
    fire_checklists,
    fire_safety,
    flow_analytics,
    scorecard,
    form_engine,
    display_labels,
    plants,
    programme,
    flra,
    hira,
    incidents,
    insights,
    inspection_findings,
    inspections,
    jobs,
    whatsapp,
    kaizen,
    licensing,
    loto,
    manhours,
    moc,
    near_miss,
    notifications,
    observation_deroster,
    observation_severity,
    observation_sla,
    observation_taxonomy,
    observations,
    workforce,
    ppe,
    ptw,
    ptw_active,
    ptw_annexures,
    ptw_lifecycle,
    ptw_reports,
    rca,
    rca_field,
    risk_dashboard,
    risk_register,
    safety_culture,
    sci,
    scr,
    signals,
    training,
    users,
    workflow,
    workflow_definitions,
)

settings = get_settings()
logging.basicConfig(level=settings.log_level)
log = logging.getLogger("safeops360")

# Every router this app mounts, keyed by its import name (matches ROUTER_MODULE).
# Order only affects OpenAPI grouping.
_ROUTERS = {
    "auth": auth, "users": users,
    # MUST be mounted BEFORE `observations`: this dict's order is the mount
    # order, and observations.py owns `GET /api/observations/{observation_id}`,
    # which would otherwise swallow `/api/observations/sla-config` and resolve
    # it as an observation id.
    "observation_sla": observation_sla,
    # Same ordering constraint: owns `/api/observations/severity-suggestion`,
    # which `observations` would otherwise match as `/{observation_id}`.
    "observation_severity": observation_severity,
    "observation_deroster": observation_deroster,
    "observations": observations, "near_miss": near_miss,
    # Unified Worker Involved picker over User + ContractorWorker (the two
    # disjoint people tables). Ungated like the taxonomy lookup — per-endpoint
    # auth plus plant scoping is the real gate.
    "workforce": workforce,
    # DuPont STOP category/sub-category master for the observation forms.
    # Ungated (read-only dropdown data, auth-gated per endpoint) so the same
    # lookup serves web, /capture PWA and mobile without a licence carve-out.
    "observation_taxonomy": observation_taxonomy,
    # LOTO (Lockout/Tagout). Mounted BEFORE ptw only for OpenAPI grouping — the
    # prefixes do not overlap. Mounted UNGATED for the same reason as
    # fire_safety / capture / alerts: the LOTO module IS registered in the
    # licensing model, but the signed dev licence predates this code, so gating
    # it via ROUTER_MODULE would 403 the whole module in dev. Add
    # "loto": "LOTO" to ROUTER_MODULE once a LOTO-inclusive licence is issued.
    #
    # Note the router carries ONE unauthenticated endpoint (GET /api/loto/qr/
    # {token}) — see the docstring there for why, and for the three properties
    # that make it safe.
    "loto": loto,
    "ptw": ptw, "ptw_active": ptw_active, "ptw_lifecycle": ptw_lifecycle,
    "ptw_reports": ptw_reports,
    # Hazard annexures + precaution checklists. Same PTW licence module as the
    # rest of the permit routers.
    "ptw_annexures": ptw_annexures,
    "flra": flra, "incidents": incidents,
    "training": training, "inspections": inspections, "inspection_findings": inspection_findings, "manhours": manhours,
    "workflow": workflow, "workflow_definitions": workflow_definitions, "anomalies": anomalies,
    "agents": agents, "agents_config": agents_config, "hira": hira, "capa": capa, "eai": eai,
    "erm": erm, "erm_attachments": erm_attachments, "erm_p2": erm_p2, "erm_p3": erm_p3, "erm_t3": erm_t3, "competency": competency,
    # Training & Competency Engine (Trigger + Assignment + Content Adapter +
    # Correlation). Mounted ungated (insights/capture precedent) — per-endpoint
    # SKILL_MATRIX RBAC is the real gate; the signed dev licence predates the code.
    "training_engine": training_engine,
    "moc": moc, "risk_register": risk_register, "risk_dashboard": risk_dashboard,
    "rca": rca, "rca_field": rca_field, "notifications": notifications,
    "scr": scr, "sci": sci, "kaizen": kaizen, "safety_culture": safety_culture, "ppe": ppe,
    # Business Excellence — shop-floor Kaizen, OPL and Poka Yoke. NOT the same
    # thing as "kaizen" two entries above: THAT is the Safety Culture Index's
    # anonymous posting board (KaizenPost, committee quorum, points ledger);
    # this is a shop-floor improvement register with a cost, a savings figure
    # and an implementation owner. Different tables, different licence module,
    # deliberately not merged.
    #
    # Mounted UNGATED for the same reason as loto / fire_safety / capture: the
    # module IS registered in the licensing model, but FULL_PLATFORM expands
    # ALL_PRODUCT_CODES into the signed payload AT ISSUE TIME, so the current
    # dev licence — issued before this code existed — has no BUSINESS_EXCELLENCE
    # claim. Adding "business_excellence": "BUSINESS_EXCELLENCE" to ROUTER_MODULE
    # would 403 the whole module in dev. Add it once a BE-inclusive licence is
    # issued; per-endpoint RBAC is the real gate meanwhile.
    "business_excellence": business_excellence,
    # Phase 2 (Suggestion Scheme, QCC, SIP, benefit realisation). Same prefix,
    # same ungated reasoning as above — a separate router file only because
    # Phase 1's is already 1,500 lines, not because it is a separate module.
    "business_excellence_p2": business_excellence_p2,
    "epc_sites": epc_sites, "epc_contractors": epc_contractors, "epc_workers": epc_workers,
    "epc_mobilization": epc_mobilization, "epc_gate": epc_gate, "epc_induction": epc_induction,
    "epc_dashboard": epc_dashboard, "audit_compliance": audit_compliance, "cams": cams,
    # Assurance integrity (docs/cams/09 Part 2) — auditor independence,
    # competence linkage, meeting records, report integrity. Mounted alongside
    # CAMS and gated by the SAME CAMS.* permission codes, so no RBAC migration
    # is needed for a tenant that already has CAMS.
    "assurance": assurance,
    # Annual Audit Programme (docs/cams/08) — the artefact a certification body
    # asks for BEFORE it looks at a single audit. Sits above BOTH engines via a
    # polymorphic slot→engagement pointer. Same CAMS.* permission codes.
    "programme": programme,
    # Waves 3-5 completion (docs/cams/09 §3.3/3.5/3.6, §2.6): suppliers, field
    # i18n, evidence packs, notification preferences. Same CAMS.* codes.
    "cams_completion": cams_completion,
    # Supplier portal (WP-45 stage 2) — the ONLY unauthenticated router. A
    # vendor factory manager holds no seat, so access is a signed, expiring,
    # single-audit token instead of a session. Mounted ungated for the same
    # reason the WhatsApp webhook is: an external caller cannot present a
    # licence context, and the token itself is the authorisation.
    "supplier_portal": supplier_portal,
    "factory": factory, "factory_ext": factory_ext, "devices": devices, "plants": plants,
    "dashboard": dashboard, "licensing": licensing, "audit_log": audit_log, "jobs": jobs,
    # AI Insights engine (Stream A) — deterministic, airgap-safe insight layer
    # over the list screens. Mounted ungated (read-only, computed from records
    # the caller can already see; auth-gated via get_current_user).
    # Per-flow analytics (trend / distribution / ageing / SLA / ownership) for
    # each operational flow. Mounted ungated like `insights`: read-only, derived
    # from records the caller can already see, and plant-scoped fail-closed per
    # endpoint off the flow's own READ permission.
    "flow_analytics": flow_analytics,
    "scorecard": scorecard,
    "insights": insights,
    # Signal Engine ("SafeOps Signal") — the cross-module correlation layer.
    # Distinct from `insights` above: that engine computes WITHIN one module and
    # renders on that module's screen; this one correlates across modules and
    # owns its own entity. Mounted ungated for the same reason as insights —
    # read-only, computed from records the caller can already see, and gated per
    # endpoint (System-Admin for rule/run administration, and the DATA_QUALITY
    # category is admin-scoped inside the feed itself).
    "signals": signals,
    # Shared Evidence Attachment layer (Stream B) — generic /api/evidence upload
    # for any registered entity. Mounted ungated; each endpoint re-checks the
    # entity's own read/write permission via the evidence registry.
    "attachments": attachments,
    # Fire Safety (FIRE module). Mounted always-on in dev: the unsigned dev licence
    # predates the FIRE code, so gating it via ROUTER_MODULE would 403 it. The FIRE
    # module IS registered in the licensing model (registry/editions) — add
    # "fire_safety": "FIRE" to ROUTER_MODULE once a FIRE-inclusive licence is issued.
    # Fire Safety AUDIT scheduling (independence-checked) and asset scope —
    # "Include in audit". Mounted before fire_safety so /api/fire/audits is not
    # shadowed by any /api/fire/{...} route.
    "fire_audits": fire_audits,
    "fire_safety": fire_safety,
    # Fire & Life Safety checklists, branded registers, QR and compliance read
    # model (ported from the Page Industries build). Same /api/fire prefix and
    # the same ungated-in-dev reasoning as fire_safety: the dev licence carries
    # no FIRE code. Every endpoint checks FIRE.* (fallback INCIDENT.*) itself.
    "fire_checklists": fire_checklists,
    # Guided Field Capture (CAPTURE module) — same dev-licence situation as
    # fire_safety: registered in the licensing model, mounted ungated until a
    # CAPTURE-inclusive licence is issued ("capture": "CAPTURE" in ROUTER_MODULE).
    "capture": capture,
    # Daily Alert Brief (ALERTS module) — same dev-licence situation; add
    # "alerts": "ALERTS" to ROUTER_MODULE once a licence including it is issued.
    "alerts": alerts,
    # BRSR Reporting (BRSR module) — SEBI Business Responsibility & Sustainability
    # disclosure. Standalone: aggregates FROM ERM / Manhours / Incident / Training /
    # EPC / Facilities but owns its own model and report generator, the Daily Brief
    # pattern. Mounted ungated for the same reason as fire_safety and capture: BRSR
    # IS registered in the licensing model, but the signed dev licence predates the
    # code, so gating it via ROUTER_MODULE would 403 the whole module. Add
    # "brsr": "BRSR" to ROUTER_MODULE once a BRSR-inclusive licence is issued.
    # Per-endpoint BRSR.* RBAC is the real gate meanwhile.
    "brsr": brsr,
    # WhatsApp-native capture (Incident Intelligence Slice 2, Feature 6) — a new
    # input adapter into the existing incident workflow. Mounted ungated; the
    # webhook is public (Meta/BSP calls it) and self-guards via sender identity.
    "whatsapp": whatsapp,
    # Form & Workflow Engine (Part A) — the no-code form primitive. Mounted
    # UNGATED for the same reason as fire_safety / capture / brsr: the signed dev
    # licence predates the code, so gating it via ROUTER_MODULE would 403 the
    # whole engine in dev. Every endpoint checks the DEFINITION's own permission
    # prefix (SUSTAINABILITY.*, BEX.*, FORMS.*), which is the real gate — and it
    # is per-form rather than per-router, so a licence code here would be the
    # wrong granularity anyway.
    "form_engine": form_engine,
    # Per-plant display-label overrides (UI vocabulary only). Core, ungated:
    # every page needs its labels regardless of which modules are licensed.
    "display_labels": display_labels,
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Pre-warm the connection pool so the first user request doesn't pay
    # the cold TCP + TLS + auth handshake to Supabase.
    try:
        await warmup()
        log.info("DB connection warmed up")
    except Exception as e:  # noqa: BLE001
        log.warning(f"DB warmup failed (non-fatal): {e}")

    # Validate the licence on boot (offline; no network). A failure here MUST
    # NOT crash the app — it fails closed to a locked state instead.
    try:
        state = await refresh_state()
        log.info("Licence boot validation: status=%s", state.status)
    except Exception as e:  # noqa: BLE001
        log.warning("Licence boot validation failed (fails closed): %s", e)

    # Load the per-factory module-allocation cache (within the licence ceiling).
    try:
        from app.licensing import factory_entitlements
        await factory_entitlements.refresh()
    except Exception as e:  # noqa: BLE001
        log.warning("Factory-entitlement cache load failed: %s", e)

    recheck = asyncio.create_task(_licence_recheck_loop())

    # P2-1 background scheduler (opt-in). Single asyncio supervisor; jobs are
    # idempotent and record JobRun rows. Off by default on a shared dev DB.
    sched_stop = asyncio.Event()
    sched_task = None
    if settings.scheduler_enabled:
        from app.services.scheduler import supervisor_loop
        sched_task = asyncio.create_task(supervisor_loop(sched_stop))
        log.info("Background scheduler ENABLED")

    try:
        yield
    finally:
        recheck.cancel()
        if sched_task is not None:
            sched_stop.set()
            sched_task.cancel()
        await engine.dispose()


async def _licence_recheck_loop() -> None:
    """Periodic re-validation — catches expiry roll-over, grace transitions, and
    clock tamper between boots without needing a restart (build prompt §5.1)."""
    interval = max(60, settings.licence_recheck_seconds)
    while True:
        try:
            await asyncio.sleep(interval)
            await refresh_state()
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Periodic licence re-check failed (fails closed): %s", e)


def create_app() -> FastAPI:
    # Install the platform-wide soft-delete guard (P1-3): registers the governed
    # entities and arms the before_flush hard-delete blocker (import side-effect).
    from app.core.soft_delete import register_default_governed

    register_default_governed()

    # Arm the unified audit trail (P1-1): import registers the ORM capture
    # listeners; register the audited entities.
    from app.models.alerts import Alert
    from app.models.attachment import Attachment
    from app.models.audit_compliance import ComplianceAudit
    from app.models.capa import Capa
    from app.models.capture import CaptureSubmission, RcaFieldRequest
    from app.models.erm import EnterpriseRisk, RiskAssessment
    from app.models.erm_p2 import LossEvent
    from app.models.erm_t3 import Control
    from app.models.incident import Incident
    from app.models.incident_intel import GoldenThreadLink, StatutoryFormInstance, WhatsappSender
    from app.models.permit import (
        Permit,
        PermitActionEvidence,
        PermitAttachment,
        PermitCrewMember,
        PermitExtension,
        PermitGasTestReading,
        PermitIsolation,
        PermitSuspension,
    )
    from app.models.fire_safety import (
        FireAmcContract,
        FireAssetCertificate,
        FireDrill,
        FireEmergencyPlan,
        FireEquipment,
        FireZone,
        InspectionFrequencyMaster,
    )
    from app.models.loto import (
        LotoExecution,
        LotoExecutionParticipant,
        LotoProcedure,
        LotoProcedureVersion,
        LotoVerificationRecord,
    )
    from app.models.form_engine import FormDefinition, FormRecord
    from app.models.rca import RcaIdentifiedCause, RcaRiskLink, RootCauseAnalysis
    from app.models.safety_culture import (
        CultureMaturityProfile,
        CultureObserverIntegrity,
        LeadershipWalk,
        PerceptionSurveyTemplate,
        RecognitionEntry,
    )
    from app.services.audit_log import register_audited

    register_audited(
        Incident, Capa, ComplianceAudit, Permit, EnterpriseRisk, RiskAssessment, LossEvent,
        Control,
        # PTW closed-loop: every safety-critical permit child table joins the
        # hash-chain — evidence rows, attachments, isolations, gas readings,
        # suspensions, extensions, and crew changes are all tamper-evident.
        PermitActionEvidence, PermitAttachment, PermitIsolation, PermitGasTestReading,
        PermitSuspension, PermitExtension, PermitCrewMember,
        FireEquipment, FireEmergencyPlan, FireDrill,
        # Fire & Life Safety: the frequency master is the reason a due date is
        # what it is, and the AMC/certificate rows are what a regulator asks to
        # see. FireZone joins because moving an asset between zones changes which
        # hot-work permits it guards.
        FireZone, InspectionFrequencyMaster, FireAmcContract, FireAssetCertificate,
        RootCauseAnalysis, RcaIdentifiedCause, RcaRiskLink,
        CaptureSubmission, RcaFieldRequest, Alert,
        # Incident Intelligence Slice 2 — golden-thread links, generated statutory
        # forms, and WhatsApp sender identity are all audit-worthy.
        GoldenThreadLink, StatutoryFormInstance, WhatsappSender,
        # Safety Culture — score recalcs, walk logging, survey admin & recognition
        # awards write to the tamper-evident hash-chain (§Cross-cutting). The
        # integrity-review outcome is auditable too (who cleared/upheld a flag).
        CultureMaturityProfile, LeadershipWalk, PerceptionSurveyTemplate, RecognitionEntry,
        CultureObserverIntegrity,
        # Shared Evidence Attachment layer — compliance evidence is tamper-evident
        # (upload / supersede / soft-delete all write to the hash-chain).
        Attachment,
        # LOTO — energy isolation is the highest-consequence record in the
        # product, so the whole chain joins the hash-chain: the procedure, every
        # published version, the lockout event, each person's individual lock
        # confirmation, and each zero-energy verification. "Who confirmed their
        # lock was off, and when" is precisely the fact an incident
        # investigation turns on, so it must be tamper-evident.
        LotoProcedure, LotoProcedureVersion, LotoExecution,
        LotoExecutionParticipant, LotoVerificationRecord,
        # Form & Workflow Engine. BOTH sides join the hash-chain, not just the
        # records: "which version of the form was live when this was filed, and
        # who published it" is exactly the question an assurance reviewer asks
        # about a disclosure, and a definition edit is the one change that can
        # alter what every future record means.
        FormDefinition, FormRecord,
    )

    app = FastAPI(
        title="SafeOps360 — Backend",
        version="1.0.0",
        description="Python backend for the SafeOps360 EHS platform.",
        # Keep debug OFF in every environment. When Starlette runs with debug=True
        # its ServerErrorMiddleware renders the raw traceback for an unhandled 500
        # AND bypasses the custom Exception handler below — leaking a stack trace to
        # the browser. The handler already logs the full traceback server-side and
        # returns a clean JSON 500, so the debug page is both redundant and unsafe.
        debug=False,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Never leak a raw Python traceback to an API client (even in dev/debug).
    # Unhandled errors are logged server-side and returned as a clean JSON 500.
    # HTTPException keeps its own handler, so 4xx business errors are unaffected.
    from fastapi.responses import JSONResponse
    from starlette.requests import Request

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception):  # noqa: ANN001
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. The team has been notified."},
        )

    # Mount every router, attaching the module-entitlement guard to gated ones.
    # The guard is the API security boundary — a disabled module's endpoints
    # 403 regardless of the UI (build prompt §5.2, TL-01).
    for name, module in _ROUTERS.items():
        module_code = ROUTER_MODULE.get(name)
        deps = [Depends(require_module(module_code))] if module_code else []
        # Ungated routers still honour an explicit per-plant OFF row (no row → on).
        if name in ROUTER_PLANT_MODULE:
            deps.append(Depends(require_plant_module(ROUTER_PLANT_MODULE[name])))
        # A CAMS fire-only plant may use /api/cams for FIRE engagements only.
        if name == "cams":
            deps.append(Depends(cams_fire_only_guard))
        app.include_router(module.router, dependencies=deps)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env}

    @app.get("/api/meta/min-version", tags=["meta"])
    async def min_supported_version() -> dict[str, str]:
        """Force-update gate consumed by the mobile Bootstrapper. Returns the
        oldest app version we still allow to talk to this backend. Bump these
        to push users off a known-broken build."""
        return {"ios": "1.0.0", "android": "1.0.0"}

    return app


app = create_app()
