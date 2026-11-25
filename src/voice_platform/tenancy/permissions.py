"""The permission catalog and the six system roles.

Single source of truth for role -> permission mapping. The initial migration seeds the
database from a frozen copy of these values; later changes ship as new migrations.
Permissions never imply each other: reading is not writing, writing is not deleting.
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    ORG_READ = "org.read"
    ORG_UPDATE = "org.update"
    ORG_DELETE = "org.delete"
    BILLING_MANAGE = "billing.manage"
    MEMBERS_READ = "members.read"
    MEMBERS_INVITE = "members.invite"
    MEMBERS_UPDATE_ROLE = "members.update_role"
    MEMBERS_REMOVE = "members.remove"
    AGENTS_READ = "agents.read"
    AGENTS_WRITE = "agents.write"
    AGENTS_PUBLISH = "agents.publish"
    KNOWLEDGE_READ = "knowledge.read"
    KNOWLEDGE_WRITE = "knowledge.write"
    PHONE_NUMBERS_READ = "phone_numbers.read"
    PHONE_NUMBERS_MANAGE = "phone_numbers.manage"
    CALLS_READ = "calls.read"
    CALLS_TRANSCRIPT_READ = "calls.transcript.read"
    RECORDINGS_READ = "recordings.read"
    RECORDINGS_DOWNLOAD = "recordings.download"
    ANALYTICS_READ = "analytics.read"
    INTEGRATIONS_MANAGE = "integrations.manage"
    API_KEYS_MANAGE = "api_keys.manage"
    AUDIT_READ = "audit.read"


class Role(StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MANAGER = "MANAGER"
    AGENT_MANAGER = "AGENT_MANAGER"
    ANALYST = "ANALYST"
    VIEWER = "VIEWER"


P = Permission

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(Permission),
    Role.ADMIN: frozenset(Permission) - {P.ORG_DELETE, P.BILLING_MANAGE},
    Role.MANAGER: frozenset(
        {
            P.ORG_READ,
            P.MEMBERS_READ,
            P.MEMBERS_INVITE,
            P.MEMBERS_UPDATE_ROLE,
            P.MEMBERS_REMOVE,
            P.AGENTS_READ,
            P.AGENTS_WRITE,
            P.AGENTS_PUBLISH,
            P.KNOWLEDGE_READ,
            P.KNOWLEDGE_WRITE,
            P.PHONE_NUMBERS_READ,
            P.PHONE_NUMBERS_MANAGE,
            P.CALLS_READ,
            P.CALLS_TRANSCRIPT_READ,
            P.RECORDINGS_READ,
            P.ANALYTICS_READ,
            P.INTEGRATIONS_MANAGE,
        }
    ),
    Role.AGENT_MANAGER: frozenset(
        {
            P.ORG_READ,
            P.MEMBERS_READ,
            P.AGENTS_READ,
            P.AGENTS_WRITE,
            P.AGENTS_PUBLISH,
            P.KNOWLEDGE_READ,
            P.KNOWLEDGE_WRITE,
            P.PHONE_NUMBERS_READ,
            P.CALLS_READ,
            P.CALLS_TRANSCRIPT_READ,
            P.ANALYTICS_READ,
        }
    ),
    Role.ANALYST: frozenset(
        {
            P.ORG_READ,
            P.AGENTS_READ,
            P.KNOWLEDGE_READ,
            P.CALLS_READ,
            P.CALLS_TRANSCRIPT_READ,
            P.ANALYTICS_READ,
        }
    ),
    Role.VIEWER: frozenset({P.ORG_READ, P.AGENTS_READ, P.CALLS_READ, P.ANALYTICS_READ}),
}

# Higher rank = more authority. A member may only grant roles at or below their own rank.
ROLE_RANK: dict[Role, int] = {
    Role.OWNER: 100,
    Role.ADMIN: 80,
    Role.MANAGER: 60,
    Role.AGENT_MANAGER: 40,
    Role.ANALYST: 20,
    Role.VIEWER: 10,
}

PERMISSION_DESCRIPTIONS: dict[Permission, str] = {
    P.ORG_READ: "View organization details",
    P.ORG_UPDATE: "Change organization settings",
    P.ORG_DELETE: "Delete the organization",
    P.BILLING_MANAGE: "Manage plan, payment and invoices",
    P.MEMBERS_READ: "View members",
    P.MEMBERS_INVITE: "Invite members",
    P.MEMBERS_UPDATE_ROLE: "Change a member's role",
    P.MEMBERS_REMOVE: "Remove members",
    P.AGENTS_READ: "View agents",
    P.AGENTS_WRITE: "Create and edit agents",
    P.AGENTS_PUBLISH: "Publish and roll back agent versions",
    P.KNOWLEDGE_READ: "View knowledge bases",
    P.KNOWLEDGE_WRITE: "Upload and edit knowledge",
    P.PHONE_NUMBERS_READ: "View phone numbers",
    P.PHONE_NUMBERS_MANAGE: "Add, assign and release phone numbers and carriers",
    P.CALLS_READ: "View call history and metrics",
    P.CALLS_TRANSCRIPT_READ: "Read call transcripts",
    P.RECORDINGS_READ: "Listen to call recordings",
    P.RECORDINGS_DOWNLOAD: "Download call recordings",
    P.ANALYTICS_READ: "View analytics",
    P.INTEGRATIONS_MANAGE: "Connect and configure integrations",
    P.API_KEYS_MANAGE: "Create and revoke API keys",
    P.AUDIT_READ: "View the audit log",
}


def permissions_for(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS[role]
