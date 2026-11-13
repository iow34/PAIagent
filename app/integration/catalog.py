"""Discoverable, versioned contract. Availability never implies authorization."""

from dataclasses import asdict
from typing import Any

from app.integration.validation import (
    MAX_DEPTH,
    MAX_ITEMS,
    MAX_PAYLOAD_BYTES,
    MAX_PROPERTIES,
    MAX_SCHEMA_BYTES,
    MAX_STRING,
)
from app.jobs.runtime import LIMITS
from app.security.context import AccessContext

REQUEST_BYTES = 98304
REQUESTS_PER_MINUTE = 120


def catalog(context: AccessContext) -> dict[str, Any]:
    return {
        "api_version": "1",
        "contract_version": "0.7.0",
        "completed_step": 11,
        "registry": {
            "sources": True,
            "collections": True,
            "schema_versions": True,
            "envelope_validation": True,
            "record_persistence": True,
            "normalized_context": True,
            "normalized_hybrid_search": True,
            "event_history": True,
            "imports_and_reconciliation": True,
            "privacy_erasure": True,
            "transactional_outbox": True,
            "background_execution": True,
            "analysis_profiles": True,
            "successful_analysis_results": True,
            "manual_change_interval_triggers": True,
            "durable_run_polling": True,
            "managed_memory": True,
            "result_feedback": True,
            "opt_in_personalization": True,
        },
        "capabilities": [
            {
                "name": "legacy_search",
                "available": True,
                "path": "/v1/search",
                "input": "indexed_documents",
                "result": "SearchResponse",
                "scope": "legacy:*",
            },
            {
                "name": "answer",
                "available": True,
                "path": "/v1/query",
                "input": "indexed_documents_and_conversation",
                "result": "QueryResponse",
                "scope": "legacy:*",
            },
            {
                "name": "document_knowledge",
                "available": True,
                "path": "/v1/documents/{document_id}/knowledge",
                "input": "single_indexed_document",
                "result": "DocumentKnowledge",
                "scope": "legacy:*",
            },
            *[
                {
                    "name": name,
                    "available": True,
                    "path": "/v1/profiles/{profile_id}/runs",
                    "input": "RunInput + immutable ProfileInput version",
                    "result": "CapabilityResult",
                    "scope": "ai:run; data:read for collections",
                    "execution": "bounded synchronous or durable PostgreSQL jobs; collected results persisted; inline sync only",
                }
                for name in [
                    "search",
                    "query",
                    "summarize",
                    "extract",
                    "analyze",
                    "recommend",
                ]
            ],
        ],
        "formats": {
            "schema_validation": ["application/json"],
            "document_upload": ["text/plain", "text/markdown"],
            "normalized_file_upload": [
                "text/plain",
                "text/markdown",
                "application/pdf",
                "application/epub+zip",
            ],
            "csv_explicit_mapping": True,
            "planned_not_available": ["image/ocr", "encrypted_documents"],
        },
        "configuration": {
            "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
            "schema_subset": "paiagent-1",
            "field_roles": [
                "search_text",
                "display_name",
                "event_time",
                "quantity",
                "category",
                "external_reference",
            ],
            "profiles_available": True,
            "memory_settings": "/v1/memory/settings",
            "memory_items": "/v1/memory/items",
            "feedback": "/v1/results/{result_id}/feedback",
            "triggers_path": "/v1/profiles/{profile_id}/triggers",
            "run_creation_path": "/v1/runs",
            "run_status_path": "/v1/runs/{run_id}",
        },
        "limits": {
            "tenant_execution": asdict(LIMITS),
            "registry_request_bytes": REQUEST_BYTES,
            "data_request_bytes": 1048576,
            "normalization_request_bytes": 6291456,
            "file_bytes": 5242880,
            "extracted_characters": 500000,
            "file_parts": 500,
            "fragments_per_input": 1000,
            "parser_seconds": 10,
            "batch_items": 100,
            "import_parts_max": 10000,
            "references_per_item": 20,
            "schema_bytes": MAX_SCHEMA_BYTES,
            "payload_bytes": MAX_PAYLOAD_BYTES,
            "schema_depth": MAX_DEPTH,
            "schema_properties": MAX_PROPERTIES,
            "array_max_items": MAX_ITEMS,
            "string_max_length": MAX_STRING,
            "page_size_max": 100,
            "document_upload_bytes": 1048576,
            "registry_requests_per_minute": REQUESTS_PER_MINUTE,
            "data_requests_per_minute": REQUESTS_PER_MINUTE,
            "data_request_timeout_seconds": 60,
            "rate_limit_scope": "peer IP per application process; shared across integration routes; no forwarded headers",
        },
        "access": {
            "scopes": list(context.scopes),
            "source_ids": None
            if context.source_ids is None
            else [str(value) for value in context.source_ids],
            "registry_visibility": "tenant-shared metadata; grants required",
            "data_owner": "tenant_id + X-User-ID",
        },
        "links": {
            "openapi": "/schema/openapi.json",
            "documentation": "/schema",
            "contract": "/v1/integration-contract",
        },
    }
