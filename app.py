import logging
import os
from typing import Optional

import chainlit as cl
import chainlit.data as cl_data
from chainlit.types import ThreadDict

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from settings import settings
from foundry_agents import list_agent_names
from cosmos_data_layer import CosmosDBDataLayer
from file_handler import csv_to_agent_payload, MAX_ROWS_TO_SEND

import csv
import json
import io

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
AGENT_PROFILES = None

@cl.set_chat_profiles
async def chat_profiles():
    global AGENT_PROFILES

    # if AGENT_PROFILES is not None:
        # return AGENT_PROFILES
    
    # list_agent_names looks synchronous; run it off the event loop.
    agent_list = await cl.make_async(list_agent_names)(limit=30)

    AGENT_PROFILES = [
        cl.ChatProfile(
            name=a.name,
            markdown_description=(
                f"**Agent:** {a.name}\n\n"
                f"**Model:** {getattr(a.versions.latest.definition, 'model', 'N/A')}\n\n"
                f"**Description:** {a.versions.latest.description}"
                # default=True
            ),
        )
        for a in agent_list
    ]

    logger.info("Retrieved %d agents.", len(AGENT_PROFILES))
    return AGENT_PROFILES


# @cl.password_auth_callback
# def auth_callback(username: str, password: str):
#     # Fetch the user matching username from your database
#     # and compare the hashed password with the value stored in the database
#     if (username, password) == ("admin", "admin"):
#         # ADD cl.user_session.get("user")
#         return cl.User(
#             identifier="admin", metadata={"role": "admin", "provider": "credentials"}
#         )
#     else:
#         return None

@cl.password_auth_callback
def auth_callback(username: str, password: str) -> Optional[cl.User]:
    """
    Authenticates the user.
    """
    username = username.strip().lower()
    password = password.strip()

    if username == "admin" and password == "admin":
        logger.info(f"User '{username}' authenticated successfully.")
        display_name = username.split('@')[0].title() if '@' in username else username.title()
        return cl.User(identifier=username, display_name=display_name)
    logger.warning(f"Authentication failed for user '{username}'. Invalid credentials.")
    return None

@cl.on_chat_start
async def on_chat_start():
    if not cl.user_session.get("initialized"):
        # Run initialization code here
        cl.user_session.set("initialized", True)
        # Data layer is optional; if you *require* it, check cl_data._data_layer
        # Chainlit injects the authenticated user here (after auth succeeds)
        user = cl.user_session.get("user")

        # Optionally store convenience fields in the session
        if user:
            cl.user_session.set("user_id", user.identifier)
            cl.user_session.set("role", (user.metadata or {}).get("role"))

        agent_name = cl.user_session.get("chat_profile")
        if not agent_name:
            agent_name = settings.DEFAULT_AGENT_NAME

        cl.user_session.set("agent_name", agent_name)
        cl.user_session.set("conversation_id", None)

        await cl.Message(content=f"Starting chat using **{agent_name}**").send()


# -----------------------------
# Messaging
# -----------------------------

@cl.on_message
async def on_message(message: cl.Message):
    text = (message.content or "").strip()

    # If nothing at all, do nothing
    if not text and not files:
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

        # Build the user payload that will be written into the conversation
        user_payload_parts: list[str] = []
        if text:
            user_payload_parts.append(text)

        # --- File handling (first file only) ---
        files = message.elements or []
        if files:
            file = files[0]
            file_ext = os.path.splitext(file.name)[1].lower()

            if file_ext != ".csv":
                await cl.Message(content=f"File type '{file_ext}' is not supported.").send()
                return

            payload = csv_to_agent_payload(file.path, file.name)
            user_payload_parts.append(payload)

            await cl.Message(
                content=f"Received CSV: **{file.name}**. Sent summary + up to {MAX_ROWS_TO_SEND} sample rows to agent."
            ).send()


        user_payload = "\n\n".join(user_payload_parts).strip()
        if not user_payload:
            # This should be rare, but keep it safe
            await cl.Message(content="Nothing to send.").send()
            return

        # --- Create or continue conversation ---
        if not conversation_id:
            conversation = openai_client.conversations.create(
                items=[{"type": "message", "role": "user", "content": user_payload}],
            )
            conversation_id = conversation.id
            cl.user_session.set("conversation_id", conversation_id)
        else:
            openai_client.conversations.items.create(
                conversation_id=conversation_id,
                items=[{"type": "message", "role": "user", "content": user_payload}],
            )

        final_parts: list[str] = []

        logger.info("[DEV-LOG] Conversation_id=%s", conversation_id)
        logger.info("[DEV-LOG] Session_id=%s", cl.user_session.get("id"))
        logger.info("[DEV-LOG] Thread_id=%s", cl.context.session.thread_id)

        # --- Stream response ---
        with openai_client.responses.create(
            conversation=conversation_id,
            extra_body={"agent": {"name": agent_name, "type": "agent_reference"}},
            input="",   # ok because we already wrote the user message into the conversation
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

# @cl.on_chat_resume
# async def on_chat_resume(thread: ThreadDict):
#     logger.info("Resuming chat thread %s", thread.id)
#     pass
#     # thread is a ThreadDict loaded from your persistence layer
#     meta = (thread.get("metadata") or {})
#     conv_id = meta.get("conversation_id")
#     agent_name = meta.get("agent_name")

#     if conv_id:
#         cl.user_session.set("conversation_id", conv_id)
#     if agent_name:
#         cl.user_session.set("agent_name", agent_name)

@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    logger.info("Resuming chat thread %s", thread.id)