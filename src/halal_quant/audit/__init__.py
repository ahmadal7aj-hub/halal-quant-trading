"""Append-only audit log (PRD §30, BRD §15)."""

from halal_quant.audit.events import AuditEvent, audit_event_table, record_event

__all__ = ["AuditEvent", "audit_event_table", "record_event"]
