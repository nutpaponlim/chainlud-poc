# import chainlit as cl
# from azure.identity import DefaultAzureCredential
# from azure.ai.projects import AIProjectClient
# from settings import settings
# from foundry_agents import list_agent_names
# import os
# import asyncio
# import chainlit.data as cl_data
# from typing import Optional
# from datetime import datetime, timezone
# from dotenv import load_dotenv

# from chainlit.types import ThreadDict
# from cosmos_data_layer import CosmosDBDataLayer

# import logging
# logger = logging.getLogger(__name__)
# # from chainlit.data.sql_alchemy import SQLAlchemyDataLayer

# data_layer = None

# def setup_data_layer():
#     """Initializes and returns the CosmosDBDataLayer instance."""
#     cosmos_endpoint = settings.Azure_Cosmos_Endpoint
#     cosmos_key = settings.Azure_Cosmos_KEY
#     database_name = settings.Azuredb
    
#     if not cosmos_endpoint or not cosmos_key:
#         raise ValueError("Missing Cosmos DB credentials in config.")
    
#     return CosmosDBDataLayer(
#         endpoint=cosmos_endpoint,
#         key=cosmos_key,
#         database_name=database_name
#     )

# try:
#     data_layer = setup_data_layer()
#     cl_data._data_layer = data_layer
# except Exception as e:
#     logger.error(f"Failed to initialize Cosmos DB data layer: {e}")
#     import traceback
#     traceback.print_exc()

# @cl.set_chat_profiles
# async def chat_profiles():
#     agent_list = list_agent_names(limit=10)
#     logger.info(f"Retrieved {len(agent_list)} agents.")
#     profiles = []
#     for agent in agent_list:
#         profiles.append(
#             cl.ChatProfile(
#                 name=agent.name,
#                 markdown_description=f"Agent: {agent.name} \n \
#                 Model: {agent.versions.latest.definition.model} \n \
#                 Description: {agent.versions.latest.description}",
#             )
#         )
#     return profiles


# @cl.password_auth_callback
# def auth_callback(username: str, password: str):
#     username = username.strip().lower()
#     password = password.strip()

#     if username == "admin" and password == "admin":
#         logger.info(f"User '{username}' authenticated successfully.")
#         display_name = username.split('@')[0].title() if '@' in username else username.title()
#         return cl.User(identifier=username, display_name=display_name)
#     logger.warning(f"Authentication failed for user '{username}'. Invalid credentials.")
#     return None

# @cl.on_chat_resume
# async def on_chat_resume(thread):
#     pass



# @cl.on_chat_start
# async def on_chat_start():
#     global data_layer
#     if not data_layer:
#         logger.error("Data layer is not initialized. Cannot proceed with chat start.")
#         return

#     app_user = cl.user_session.get("user")
#     await cl.Message(f"Hello {app_user.identifier}").send()

#     agent_name = cl.user_session.get("chat_profile")
#     if not agent_name:
#         await cl.Message(content="No agent selected.").send()
#         return

#     cl.user_session.set("agent_name", agent_name)
#     cl.user_session.set("conversation_id", None)

#     await cl.Message(content=f"Starting chat using **{agent_name}**").send()


# @cl.on_message
# async def on_message(message: cl.Message):
#     text = (message.content or "").strip()

#     agent_name = cl.user_session.get("agent_name")
#     conversation_id = cl.user_session.get("conversation_id")
    
#     # Create an empty assistant message we will stream into
#     out = await cl.Message(content="").send()

#     with (
#         DefaultAzureCredential() as credential,
#         AIProjectClient(endpoint=settings.PROJECT_ENDPOINT, credential=credential) as project_client,
#         project_client.get_openai_client() as openai_client,
#     ):
#         files = message.elements
#         if files:
#             await out.stream_token(f"Received {len(files)} file(s). Currently, file handling is not implemented.\n")

#         # Create or continue conversation
#         if not conversation_id:
#             conversation = openai_client.conversations.create(
#                 items=[{"type": "message", "role": "user", "content": text}],
#             )
#             conversation_id = conversation.id
#             cl.user_session.set("conversation_id", conversation_id)
#         else:
#             openai_client.conversations.items.create(
#                 conversation_id=conversation_id,
#                 items=[{"type": "message", "role": "user", "content": text}],
#             )

#         # ✅ TRUE streaming
#         final_text_parts = []

#         with openai_client.responses.create(
#             conversation=conversation_id,
#             extra_body={"agent": {"name": agent_name, "type": "agent_reference"}},
#             input="",
#             stream=True,
#         ) as response_stream_events:

#             for event in response_stream_events:
#                 # The important event for token streaming
#                 if event.type == "response.output_text.delta":
#                     delta = event.delta or ""
#                     final_text_parts.append(delta)
#                     await out.stream_token(delta)
                    
#     # Ensure message is finalized in UI
#     out.content = "".join(final_text_parts)
#     await out.update()


import logging
from typing import Optional

import chainlit as cl
import chainlit.data as cl_data
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from settings import settings
from foundry_agents import list_agent_names
from cosmos_data_layer import CosmosDBDataLayer

logger = logging.getLogger(__name__)
# Silence Cosmos DB SDK logs
logging.getLogger("azure.cosmos").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline").setLevel(logging.WARNING)
logging.getLogger("azure.identity").setLevel(logging.WARNING)
# -----------------------------
# Data layer init (Cosmos)
# -----------------------------

def setup_data_layer() -> CosmosDBDataLayer:
    endpoint = settings.Azure_Cosmos_Endpoint
    key = settings.Azure_Cosmos_KEY
    db_name = settings.Azuredb

    if not endpoint or not key or not db_name:
        raise ValueError("Missing Cosmos DB config (endpoint/key/database_name).")

    return CosmosDBDataLayer(endpoint=endpoint, key=key, database_name=db_name)

try:
    cl_data._data_layer = setup_data_layer()
    logger.info("Cosmos DB data layer initialized.")
except Exception:
    logger.exception("Failed to initialize Cosmos DB data layer.")
    # Leave Chainlit without a data layer rather than crashing import time.


# -----------------------------
# Azure client lifecycle
# -----------------------------

class AzureOpenAIContext:
    """
    Holds Azure credential/project client/openai client for reuse across messages.
    Stored in Chainlit user_session (per user/session).
    """
    def __init__(self):
        self.credential = DefaultAzureCredential()
        self.project_client = AIProjectClient(
            endpoint=settings.PROJECT_ENDPOINT,
            credential=self.credential,
        )
        self.openai_client = self.project_client.get_openai_client()

    def close(self):
        # Close in reverse order (best-effort)
        try:
            self.openai_client.close()
        except Exception:
            pass
        try:
            self.project_client.close()
        except Exception:
            pass
        try:
            self.credential.close()
        except Exception:
            pass


def get_azure_ctx() -> AzureOpenAIContext:
    ctx: Optional[AzureOpenAIContext] = cl.user_session.get("azure_ctx")
    if ctx is None:
        ctx = AzureOpenAIContext()
        cl.user_session.set("azure_ctx", ctx)
    return ctx


@cl.on_chat_end
async def on_chat_end():
    # Ensure clients get closed when chat session ends
    ctx: Optional[AzureOpenAIContext] = cl.user_session.get("azure_ctx")
    if ctx:
        ctx.close()
        cl.user_session.set("azure_ctx", None)


# -----------------------------
# Profiles + auth
# -----------------------------
# AGENT_PROFILES = None

@cl.set_chat_profiles
async def chat_profiles():
    # global AGENT_PROFILES

    # if AGENT_PROFILES is not None:
        # return AGENT_PROFILES
    
    # list_agent_names looks synchronous; run it off the event loop.
    agent_list = await cl.make_async(list_agent_names)(limit=10)

    AGENT_PROFILES = [
        cl.ChatProfile(
            name=a.name,
            markdown_description=(
                f"**Agent:** {a.name}\n\n"
                f"**Model:** {a.versions.latest.definition.model}\n\n"
                f"**Description:** {a.versions.latest.description}"
            ),
        )
        for a in agent_list
    ]

    logger.info("Retrieved %d agents.", len(AGENT_PROFILES))
    return AGENT_PROFILES


@cl.password_auth_callback
def auth_callback(username: str, password: str):
    username = (username or "").strip().lower()
    password = (password or "").strip()

    # ⚠️ Replace with real auth (env vars, SSO, etc.)
    if username == "admin" and password == "admin":
        display_name = username.split("@")[0].title() if "@" in username else username.title()
        logger.info("User '%s' authenticated.", username)
        return cl.User(identifier=username, display_name=display_name)

    logger.warning("Authentication failed for user '%s'.", username)
    return None


@cl.on_chat_start
async def on_chat_start():
    # Data layer is optional; if you *require* it, check cl_data._data_layer
    user = cl.user_session.get("user")
    await cl.Message(f"Hello {user.identifier}").send()

    agent_name = cl.user_session.get("chat_profile")
    if not agent_name:
        await cl.Message(content="No agent selected.").send()
        return

    cl.user_session.set("agent_name", agent_name)
    cl.user_session.set("conversation_id", None)

    await cl.Message(content=f"Starting chat using **{agent_name}**").send()


# -----------------------------
# Messaging
# -----------------------------

@cl.on_message
async def on_message(message: cl.Message):
    text = (message.content or "").strip()
    if not text and not message.elements:
        return

    agent_name = cl.user_session.get("agent_name")
    if not agent_name:
        await cl.Message("No agent selected.").send()
        return

    conversation_id = cl.user_session.get("conversation_id")
    out = await cl.Message(content="").send()

    try:
        ctx = get_azure_ctx()
        openai_client = ctx.openai_client

        # Notify about files (still not implemented)
        files = message.elements or []
        if files:
            await out.stream_token(
                f"Received {len(files)} file(s). File handling is not implemented yet.\n\n"
            )

        # Create or continue conversation
        if not conversation_id:
            conversation = openai_client.conversations.create(
                items=[{"type": "message", "role": "user", "content": text}],
            )
            conversation_id = conversation.id
            cl.user_session.set("conversation_id", conversation_id)
        else:
            openai_client.conversations.items.create(
                conversation_id=conversation_id,
                items=[{"type": "message", "role": "user", "content": text}],
            )

        final_parts: list[str] = []

        # Stream response tokens
        with openai_client.responses.create(
            conversation=conversation_id,
            extra_body={"agent": {"name": agent_name, "type": "agent_reference"}},
            input="",
            stream=True,
        ) as events:
            for event in events:
                if event.type == "response.output_text.delta":
                    delta = event.delta or ""
                    if delta:
                        final_parts.append(delta)
                        await out.stream_token(delta)

        out.content = "".join(final_parts) or out.content
        await out.update()

    except Exception as e:
        logger.exception("Error handling message.")
        await out.stream_token(f"\n\n⚠️ Error: {type(e).__name__}: {e}")
        out.content = (out.content or "") + f"\n\n⚠️ Error: {type(e).__name__}: {e}"
        await out.update()
