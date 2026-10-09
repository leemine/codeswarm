"""Keep SDK-created context models on their owning Native model authority."""
from __future__ import annotations

from openjiuwen.core.context_engine.processor.forked.compressor.base import (
    PrefixCompactProcessor, PrefixCompactProcessorConfig,
)
from openjiuwen.core.context_engine.processor.forked.compressor.support.compression_executor import CompressionExecutor
from openjiuwen.core.foundation.llm import Model

from .model_consumer import model_request_authority
from .resources import ResourceAccessDenied


class _PendingContextAuthority:
    """Permit history construction before request binding, never model IO."""

    def __init__(self, agent, client, request):
        self._agent, self._client, self._request = agent, client, request

    def bind_for_call(self):
        source = getattr(getattr(self._agent, 'deep_config', None), 'model', None)
        authority = getattr(getattr(source, '_client', None), '_request_authority', None)
        if authority is None:
            raise ResourceAccessDenied('context model authority unavailable')
        if (self._client != source.model_client_config
                or self._request.model_name != source.model_config.model_name):
            raise ResourceAccessDenied('context model differs from its owning model')
        # Capture this exact source's logical-call authority now. No callback
        # may be borrowed from a later Turn after this method returns.
        return authority.bind_for_call()


def bind_context_model_factory(agent) -> None:
    """Adapt the locked SDK's per-engine factory; leave its registry/keys intact.

    ef159d7c rebuilds compressor Models from credential-free client configs but
    does not carry the original instance authority. There is no public factory
    hook or authority getter in that SDK. Keep this compatibility seam confined
    to the owning engine, including hot reload, without patching global Models.
    """
    react = getattr(agent, 'react_agent', None)
    engine = getattr(react, 'context_engine', None)
    if engine is None:
        return
    create = engine._create_processor  # pylint: disable=protected-access
    original = getattr(create, '_native_original_factory', create)

    def create_processor(kind, config):
        client = getattr(config, 'model_client', None)
        request = getattr(config, 'model', None)
        if not isinstance(config, PrefixCompactProcessorConfig) or client is None or request is None:
            return original(kind, config)
        source = getattr(getattr(agent, 'deep_config', None), 'model', None)
        # The public Model API has no authority getter; this exact instance's
        # SDK client owns the factory, including Team member and entry checks.
        authority = getattr(getattr(source, '_client', None), '_request_authority', None)
        if authority is None:
            if client.api_key == 'MODEL_REQUEST_AUTHORITY':
                # Cold history warmup precedes the host's request-model binding.
                # Construct the original processor now; deny actual use until
                # that same engine has an exact, fully bound owning model.
                authority = _PendingContextAuthority(agent, client, request)
            elif model_request_authority({**client.model_dump(),
                                          'model_name': request.model_name}) is not None:
                raise ResourceAccessDenied('context model authority unavailable')
            else:
                return original(kind, config)
        elif (client != source.model_client_config
                or request.model_name != source.model_config.model_name):
            # Never borrow the primary credential for an independent override.
            raise ResourceAccessDenied('context model differs from its owning model')
        # Avoid constructing an unauthorised, unused SDK client first. Preserve
        # all original processor settings, algorithms, names and event handling.
        processor = original(kind, config.model_copy(update={'model_client': None}))
        if not isinstance(processor, PrefixCompactProcessor):
            raise ResourceAccessDenied('context processor implementation mismatch')
        model = Model(client, request, request_authority=authority)
        processor._config = config  # pylint: disable=protected-access
        processor._model = model  # pylint: disable=protected-access
        processor._compression_executor = CompressionExecutor(model)  # pylint: disable=protected-access
        return processor

    create_processor._native_original_factory = original
    engine._create_processor = create_processor  # pylint: disable=protected-access
