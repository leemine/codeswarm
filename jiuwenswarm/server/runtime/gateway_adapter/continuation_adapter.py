"""Thin trusted continuation RPCs; Runtime retains creation and publication."""
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.governance.continuation import ContinuationInput
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_boundary import parse_continuation_options
from jiuwenswarm.governance.session_sharing import SessionSharingConflict, SessionSharingDenied
from .base import GatewayAdapter, build_error_response


class ContinuationAdapter(GatewayAdapter):
    methods = frozenset({'session.share.continuation.options', 'session.share.continue'})

    def __init__(self, *, runtime_resolver, identity_resolver):
        self._runtime = runtime_resolver
        self._identity = identity_resolver

    async def handle(self, request):
        try:
            method = getattr(request.req_method, 'value', request.req_method)
            if method not in self.methods:
                raise ValueError('unsupported continuation method')
            identity = self._identity(request)
            if not isinstance(identity, TrustedIdentity):
                raise SessionSharingDenied('trusted continuation identity required')
            if method == 'session.share.continue':
                params = ContinuationInput.from_wire(request.params)
                result = await self._runtime().continue_session(params)
            else:
                params = parse_continuation_options(request.params)
                result = await self._runtime().continuation_options(params)

            def guard():
                if self._identity(request) != identity:
                    raise SessionSharingDenied('continuation identity changed')
                result.revalidate()
                if self._identity(request) != identity:
                    raise SessionSharingDenied('continuation identity changed')
            guard()
            payload = result.to_payload()
            guard()
            response = AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                                     ok=True, payload=payload, metadata=request.metadata)
            # This only guards the AgentServer socket. The Gateway separately
            # re-reads committed authority at its existing final writer sink.
            response._delivery_guard = guard
            return response
        except SessionSharingConflict:
            return build_error_response(request, 'Continuation input or revision changed.', code='CONFLICT')
        except (SessionSharingDenied, PermissionError):
            return build_error_response(request, 'Continuation authorization denied.', code='FORBIDDEN')
        except (ValueError, TypeError):
            return build_error_response(request, 'Invalid continuation request.', code='BAD_REQUEST')
        except Exception:
            return build_error_response(request, 'Continuation unavailable.', code='FORBIDDEN')
