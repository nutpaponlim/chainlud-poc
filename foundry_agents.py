# foundry_agents.py
from __future__ import annotations

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential
from settings import settings


def get_project_and_openai_clients():
    """
    Returns (project_client, openai_client).
    NOTE: caller should close them (context manager).
    """
    credential = DefaultAzureCredential()
    project_client = AIProjectClient(endpoint=settings.PROJECT_ENDPOINT, credential=credential)
    openai_client = project_client.get_openai_client()
    return credential, project_client, openai_client


def list_agent_names(limit: int = 30) -> list[str]:
    # try:
    credential = DefaultAzureCredential()
        # Check if the credential works
    credential.get_token("https://management.azure.com/.default")
    # except Exception as ex:
        # Fall back to interactive mode if the default chain fails
        # credential = InteractiveBrowserCredential()
    with AIProjectClient(endpoint=settings.PROJECT_ENDPOINT, credential=credential) as project:
        names = []
        for a in project.agents.list():
            if a.name != "graph-drawer":
                names.append(a)
                # print(a)
                if len(names) >= limit:
                    break
        # return names
        return names


def get_agent_by_name(project_client: AIProjectClient, agent_name: str):
    # Your code proves this works:
    return project_client.agents.get(agent_name=agent_name)


def create_conversation(openai_client, first_user_message: str) -> str:
    convo = openai_client.conversations.create(
        items=[{"type": "message", "role": "user", "content": first_user_message}],
    )
    return convo.id


def add_user_message(openai_client, conversation_id: str, text: str) -> None:
    openai_client.conversations.items.create(
        conversation_id=conversation_id,
        items=[{"type": "message", "role": "user", "content": text}],
    )


def run_agent_response(openai_client, agent_name: str, conversation_id: str) -> str:
    # IMPORTANT: agent routing happens via extra_body
    resp = openai_client.responses.create(
        conversation=conversation_id,
        extra_body={"agent": {"name": agent_name, "type": "agent_reference"}},
        input="",  # we already added user msg as conversation item
    )
    return getattr(resp, "output_text", "") or str(resp)
