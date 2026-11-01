from functools import partial

from agent_framework_foundry_hosting import ResponsesHostServer
from azure.ai.agentserver.core import configure_observability

from agent.framework_agent import create_agent

app = ResponsesHostServer(
    create_agent(),
    configure_observability=partial(
        configure_observability,
        instrumentation_options={"openai_agents": {"enabled": False}},
    ),
)

if __name__ == "__main__":
    app.run()
