# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Registry configuration schema + factories.

Schema shape
------------

``planner.plugin_registration.*``
  - ``endpoints`` (UDS socket + optional gRPC listen addr)
  - ``auth`` (trusted_sources + per-source config)
  - ``transport`` (TransportConfig; see ``transport/config.py``)
  - ``protocol_version_min`` / ``_max``
  - ``heartbeat_timeout_seconds`` / ``heartbeat_missed_threshold``
  - ``in_process_plugins`` — lives next to other "how plugins register"
    settings
  - ``admin`` (simplified — ``AllowAllAdminAuth`` default)

``planner.scheduling.*``
  - ``clock`` (see ``transport/config.py``)
  - ``request_timeout_seconds`` / ``tick_max_duration_seconds``
  - ``builtins`` — per-builtin-plugin toggles (actual default for
    ``enabled`` is the ``enable_*_scaling`` toggle of the parent planner
    config; overriden here)

Auth scope: PR #1 wires ``static_secret`` + ``allow_unauthenticated``.
``k8s_sa`` and ``spiffe_jwt`` land in a follow-up PR alongside their
cluster-side configuration and end-to-end smoke tests.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from dynamo.planner.plugins.clock import Clock
from dynamo.planner.plugins.registry.auth import (
    AllowUnauthenticatedAuth,
    AuthValidator,
    MultiSourceAuth,
    StaticSecretAuth,
)
from dynamo.planner.plugins.registry.circuit_breaker import CircuitBreaker
from dynamo.planner.plugins.registry.server import PluginRegistryServer
from dynamo.planner.plugins.transport.base import PluginTransport
from dynamo.planner.plugins.transport.config import (
    TransportConfig,
    make_transport_for_endpoint,
)

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------------


class EndpointsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uds_socket_path: str = "/var/run/dynamo/planner/registry.sock"
    grpc_listen_addr: Optional[str] = None
    """``None`` by default — gRPC listener is OPT-IN to prevent accidental
    network exposure. Set to e.g. ``":50051"`` to enable."""


# ----------------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------------


AuthSource = Literal["static_secret", "allow_unauthenticated"]


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trusted_sources: list[AuthSource] = Field(default_factory=list)
    """Empty default = fail-closed; ``build_auth_validator`` raises."""

    static_secrets: dict[str, str] = Field(default_factory=dict)
    """``secret_value -> subject_label`` map."""


# ----------------------------------------------------------------------------
# Scheduling + in-process + builtins
# ----------------------------------------------------------------------------


class BuiltinPluginToggle(BaseModel):
    """Override for a builtin plugin's scheduling.

    ``enabled`` default ``True`` is overridden by the parent planner's
    ``enable_*_scaling`` toggle at bootstrap time; runtime config edits
    here take effect on next ``config.reload()``."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    priority: Optional[int] = None
    execution_interval_seconds: Optional[float] = None


class InProcessPluginSpec(BaseModel):
    """Spec for an in-process plugin entry — lives under
    PluginRegistrationConfig so all "how plugins come to exist"
    settings live together.

    ``extra="forbid"`` rejects unknown fields (including
    ``protocol_version``, which is nonsensical for in-process plugins).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    module: str
    class_: str = Field(..., alias="class")
    """Python class name in ``module``; aliased since ``class`` is a keyword."""

    plugin_id: str
    plugin_type: Literal["predict", "propose", "reconcile", "constrain"]
    priority: int
    execution_interval_seconds: float = 0.0
    hold_policy: Literal["ACCEPT_WHEN_IDLE", "HOLD_LAST"] = "ACCEPT_WHEN_IDLE"
    kwargs: dict[str, Any] = Field(default_factory=dict)


class SchedulingConfig(BaseModel):
    """``planner.scheduling.*`` config tree. Note: ``in_process_plugins``
    lives under ``PluginRegistrationConfig``, not here."""

    model_config = ConfigDict(extra="forbid")

    request_timeout_seconds: float = 5.0
    tick_max_duration_seconds: float = 30.0
    builtins: dict[str, BuiltinPluginToggle] = Field(default_factory=dict)


# ----------------------------------------------------------------------------
# Admin
# ----------------------------------------------------------------------------


class AdminAuthConfig(BaseModel):
    """Admin (ListPlugins) RBAC config. Default = allow_all (dev);
    K8s RBAC admin is a follow-up."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["allow_all", "k8s_rbac"] = "allow_all"


# ----------------------------------------------------------------------------
# Top-level aggregate
# ----------------------------------------------------------------------------


class PluginRegistrationConfig(BaseModel):
    """``planner.plugin_registration.*`` root config tree (v11)."""

    model_config = ConfigDict(extra="forbid")

    endpoints: EndpointsConfig = Field(default_factory=EndpointsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    transport: TransportConfig = Field(default_factory=TransportConfig)
    protocol_version_min: str = "1.0"
    protocol_version_max: str = "1.0"
    heartbeat_timeout_seconds: float = 15.0
    heartbeat_missed_threshold: int = 2
    in_process_plugins: list[InProcessPluginSpec] = Field(default_factory=list)
    admin: AdminAuthConfig = Field(default_factory=AdminAuthConfig)


# ----------------------------------------------------------------------------
# Factories
# ----------------------------------------------------------------------------


def build_auth_validator(config: AuthConfig) -> AuthValidator:
    """Construct the composed auth validator from ``AuthConfig``.

    Raises ``ValueError`` on empty ``trusted_sources`` (fail-closed) —
    PR #1 supports ``static_secret`` and ``allow_unauthenticated``.
    """
    if not config.trusted_sources:
        raise ValueError(
            "AuthConfig.trusted_sources is empty; registry would reject "
            "every token. Configure at least one source (e.g. "
            "['static_secret']) or ['allow_unauthenticated'] for dev."
        )
    sources: list[AuthValidator] = []
    for source_name in config.trusted_sources:
        if source_name == "static_secret":
            if not config.static_secrets:
                log.warning(
                    "AuthConfig.static_secrets is empty but 'static_secret' "
                    "listed in trusted_sources — StaticSecretAuth will reject "
                    "every token."
                )
            sources.append(StaticSecretAuth(config.static_secrets))
        elif source_name == "allow_unauthenticated":
            sources.append(AllowUnauthenticatedAuth())
        else:  # pragma: no cover — schema Literal prevents reaching here
            raise ValueError(f"unknown auth source: {source_name!r}")
    return MultiSourceAuth(sources)


def build_registry_from_config(
    config: PluginRegistrationConfig,
    clock: Clock,
) -> tuple[PluginRegistryServer, CircuitBreaker]:
    """Construct and wire the registry + circuit breaker.

    Returns the pair so the caller (orchestrator) can hand the circuit
    breaker to other subsystems (scheduler, heartbeat monitor).
    """
    auth = build_auth_validator(config.auth)
    cb = CircuitBreaker(clock)

    transport_factory = functools.partial(
        _transport_factory_shim, transport_config=config.transport
    )

    server = PluginRegistryServer(
        clock=clock,
        auth=auth,
        circuit_breaker=cb,
        transport_factory=transport_factory,
        protocol_versions=(config.protocol_version_min, config.protocol_version_max),
    )
    return server, cb


def _transport_factory_shim(
    plugin_id: str,
    endpoint: str,
    *,
    in_process_instance: Any = None,
    transport_config: TransportConfig,
) -> PluginTransport:
    """Adapter: ``make_transport_for_endpoint`` takes ``config`` as the
    third positional argument; the registry's factory protocol is
    ``(plugin_id, endpoint, *, in_process_instance=None)``."""
    return make_transport_for_endpoint(
        plugin_id,
        endpoint,
        transport_config,
        in_process_instance=in_process_instance,
    )


__all__ = [
    "EndpointsConfig",
    "AuthSource",
    "AuthConfig",
    "BuiltinPluginToggle",
    "InProcessPluginSpec",
    "SchedulingConfig",
    "AdminAuthConfig",
    "PluginRegistrationConfig",
    "build_auth_validator",
    "build_registry_from_config",
]
