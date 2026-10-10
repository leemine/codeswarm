"""Bundled application. Gateway holds no task store or business service."""

from jiuwenswarm.extensions.sdk import ApplicationPluginExtension, FrontendContribution


def taskboard_enabled():
    from jiuwenswarm.common.config import get_config_raw

    return (get_config_raw().get("taskboard") or {}).get("enabled", True) is not False


class TaskboardApplicationPlugin(ApplicationPluginExtension):
    plugin_id = "taskboard"

    def is_enabled(self):
        return taskboard_enabled()

    async def initialize(self, config):
        pass

    async def shutdown(self):
        pass

    def frontend_contributions(self):
        return (
            FrontendContribution(
                id=self.plugin_id,
                nav_key="app:taskboard",
                title="Taskboard",
                title_i18n_key="taskboard.title",
                render_mode="bundled",
                component=self.plugin_id,
                position=70,
                nav_group="tasks",
            ),
        )

    def bind_web_channel(self, channel, services):
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request
        from functools import partial

        async def forward(method, ws, req_id, params, session_id, user_id=None):
            if not self.is_enabled():
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error="Taskboard disabled",
                    code="FEATURE_DISABLED",
                )
                return
            await proxy_unary_request(
                channel=channel,
                agent_client=services.require_agent_client(),
                ws=ws,
                req_id=req_id,
                params=params,
                session_id=None,
                user_id=user_id,
                req_method=ReqMethod(method),
                label=method,
                preserve_error_payload=True,
            )

        for method in (
            "taskboard.create",
            "taskboard.list",
            "taskboard.get",
            "taskboard.update",
        ):
            channel.register_method(method, partial(forward, method))


async def register_extensions(registry):
    plugin = TaskboardApplicationPlugin()
    registry.register_application_plugin(plugin)
    return [plugin]
