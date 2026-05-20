# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Chain-augment merge for PREDICT stage.

Sequential layered prediction: each plugin sees the running
``prediction`` on its ``PipelineContext`` and may set fields the
higher-precedence plugins ahead of it left unset, or it may stay
silent. Partial-merge is field-level on the ``Optional[float]``
prediction fields (``None`` = "no opinion, leave previous value
alone"; a concrete float — including ``0.0`` — = "I assert this
value").

Ordering and semantics
----------------------
- Chain is sorted by ``priority`` **ascending** (smallest priority
  number first). Smallest priority = highest precedence = runs
  **first** and writes the fields the lower-precedence plugins
  cannot overwrite.
- Partial-merge rule: **first writer wins**. A later plugin's value
  for a field is adopted only if every higher-precedence plugin left
  that field as ``None``. This matches the way ``priority`` works in
  the merge stages: smaller priority is more authoritative, larger
  priority fills in defaults / refinements.
- ``predictions=None`` in a response ≈ "no opinion"; the chain
  continues with the running prediction unchanged.
- ``final=True`` short-circuits the chain at the plugin that set it.
  Plugins later in the chain (lower precedence, larger priority
  number) are **not** called. This is the unified meaning of
  ``final=true`` across stages — "my answer is enough, no need to
  fall through to defaults".
- **Strong contract**: ``final=true`` MUST come from the
  lowest-priority-number (highest-precedence) plugin. Setting it
  from a larger-priority plugin would short-circuit BEFORE the
  authoritative plugin had a chance to weigh in, which is almost
  always a configuration bug. ``chain_augment`` detects this at
  runtime, logs a WARNING, and records a message in
  ``ChainAugmentOutcome.misuse_warnings``. The orchestrator emits a
  Prometheus ``predict_chain_final_at_non_lowest_priority_total{plugin_id}``
  counter.

This unifies the semantics of ``priority`` and ``final=true`` across
all four stages:

- **All stages**: smallest priority = most authoritative.
- **All stages**: ``final=true`` from the highest-precedence plugin
  means "my answer is the final one, skip everyone else".

The only difference is the underlying problem each stage solves:

- PROPOSE / RECONCILE / CONSTRAIN reconcile **conflicting** SET
  proposals via type-aware merge (winner-takes-all per key).
- PREDICT layers **complementary** prediction fields via first-writer
  partial-merge (no conflicts; everyone fills in different gaps).

The function is async only because it awaits plugin RPCs; the
algorithmic logic is synchronous and deterministic given plugin
responses.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from dynamo.planner.plugins.types import (
    PipelineContext,
    PredictionData,
)

from dynamo.planner.plugins.merge.types import (
    ChainAugmentOutcome,
    PredictPluginCallable,
)

log = logging.getLogger(__name__)

_PREDICTION_FIELDS = ("predicted_num_req", "predicted_isl", "predicted_osl")


def _partial_merge(
    prev: Optional[PredictionData], new: PredictionData
) -> PredictionData:
    """Field-level merge: ``prev`` (the higher-precedence plugin that
    ran earlier) wins on every field it already set; ``new``
    contributes only to fields ``prev`` left as ``None``.

    Concretely: for each prediction field, take ``prev.<field>`` if it
    is not ``None``, else ``new.<field>``. ``source`` takes
    ``prev.source`` if non-empty, else ``new.source``.

    If ``prev is None`` (first plugin in the chain), ``new`` is
    returned verbatim.
    """
    if prev is None:
        return new
    merged: dict[str, object] = {}
    for name in _PREDICTION_FIELDS:
        pv = getattr(prev, name)
        merged[name] = pv if pv is not None else getattr(new, name)
    merged["source"] = prev.source or new.source
    return PredictionData(**merged)


async def chain_augment(
    plugin_chain: Sequence[PredictPluginCallable],
    initial_context: PipelineContext,
) -> ChainAugmentOutcome:
    """Run a PREDICT chain, returning the first-writer-wins merged prediction.

    Args:
        plugin_chain: PREDICT plugins to run. Sorted by priority
            ascending internally (smallest priority first); caller may
            pass any order. Empty input → empty outcome.
        initial_context: Base PipelineContext shared across plugins.
            The ``predictions`` field is replaced per-iteration with
            the running merged prediction; other fields are preserved.

    Returns:
        ``ChainAugmentOutcome`` — ``prediction`` is the merged
        ``PredictionData`` (``None`` if no plugin produced content);
        ``final_from`` is the plugin that broke the chain (empty on
        full traversal); ``misuse_warnings`` is non-empty when a
        plugin other than the lowest-priority-number (highest
        precedence) returned ``final=true``.
    """
    chain = sorted(plugin_chain, key=lambda p: p.priority)
    lowest_priority = min((p.priority for p in chain), default=None)
    prediction: Optional[PredictionData] = None
    final_from = ""
    misuse_warnings: list[str] = []

    for p in chain:
        ctx = initial_context.model_copy(update={"predictions": prediction})
        resp = await p.call("Predict", ctx)
        if resp.predictions is not None:
            prediction = _partial_merge(prediction, resp.predictions)
        if resp.final:
            final_from = p.plugin_id
            if lowest_priority is not None and p.priority != lowest_priority:
                msg = (
                    f"chain_augment_final_misuse: plugin_id={p.plugin_id} "
                    f"priority={p.priority} returned final=true but is NOT "
                    f"the lowest priority in the chain "
                    f"(lowest_priority={lowest_priority}). "
                    "Chain short-circuited BEFORE the higher-precedence "
                    "plugin could weigh in. See ``chain_augment`` module "
                    "docstring for the correct usage contract."
                )
                log.warning(msg)
                misuse_warnings.append(msg)
            break

    return ChainAugmentOutcome(
        prediction=prediction,
        final_from=final_from,
        degraded=[],
        misuse_warnings=misuse_warnings,
    )


__all__ = ["chain_augment"]
