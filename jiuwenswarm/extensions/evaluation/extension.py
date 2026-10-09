"""Trusted bundled application; business services are instantiated by AgentServer."""

from jiuwenswarm.extensions.sdk import ApplicationPluginExtension, FrontendContribution


class EvaluationApplicationPlugin(ApplicationPluginExtension):
    plugin_id = "evaluation-experiments"

    def is_enabled(self) -> bool:
        from jiuwenswarm.common.config import get_config_raw

        return (get_config_raw().get("evaluation") or {}).get("enabled", True) is not False

    async def initialize(self, config):
        del config

    async def shutdown(self):
        return None

    def frontend_contributions(self):
        return (
            FrontendContribution(
                id=self.plugin_id,
                nav_key="app:" + self.plugin_id,
                title="Evaluation experiments",
                title_i18n_key="evaluation.title",
                render_mode="bundled",
                component=self.plugin_id,
                position=80,
                nav_group="experiments",
            ),
        )

    def compose(self, *, runtime, data_root, send_push=None):
        from jiuwenswarm.extensions.evaluation.backend.rpc import EvaluationService

        return EvaluationService(runtime=runtime, data_root=data_root, send_push=send_push)


async def register_extensions(registry):
    plugin = EvaluationApplicationPlugin()
    registry.register_application_plugin(plugin)
    registry.register_capability("evaluation.application", plugin, version="1.0")
    return [plugin]
