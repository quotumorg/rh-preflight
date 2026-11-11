"""Only supported registrations; no replacement of host code."""

from . import cli, commands, tools
from .hooks import Service
from .settings import Settings


def register(ctx):
    service = Service(settings=Settings.from_context(ctx), context=ctx)
    ctx.register_tool(name="builder_card", toolset="rh-preflight", schema=tools.SCHEMA,
                      handler=lambda args, **kw: tools.handler(service, args, **kw))
    ctx.register_hook("pre_tool_call", service.pre_tool_call)
    ctx.register_hook("transform_tool_result", service.transform_tool_result)
    ctx.register_hook("post_approval_response", service.post_approval_response)
    ctx.register_command("rh-preflight", handler=lambda raw, **kw: commands.card_command(service, raw, **kw),
                         description="rh-preflight by quotum with HEY Research Lab; read a network card of source records under your approval rules",
                         args_hint="<address|repository-url|project>")
    ctx.register_cli_command(name="rh-preflight", help="rh-preflight by quotum with HEY Research Lab; read builder records plus ten Robinhood Chain projects and local checks",
                             setup_fn=cli.setup, handler_fn=lambda args: cli.run(service, args))
