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
# from cosmos_data_layer import CosmosDBDataLayer
import chainlit as cl
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.data.storage_clients.azure import AzureStorageClient
from data_layer import upsert_thread_metadata

from file_handler import csv_to_agent_payload, MAX_ROWS_TO_SEND
# from healper_plot_csv import run_graph_drawer, chainlit_file_from_local

import csv
import json
import io
import asyncio

logger = logging.getLogger(__name__)
# Silence Cosmos DB SDK logs
# logging.getLogger("azure.cosmos").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline").setLevel(logging.WARNING)
logging.getLogger("azure.identity").setLevel(logging.WARNING)

@cl.data_layer
def get_data_layer():
    DB_CONN_INFO = settings.DATABASE_URL

    if not DB_CONN_INFO:
        logger.info(f"Missing DATABASE_URL environment variable for database connection.")
        return None
    try:
        # storage_client = AzureStorageClient(account_url="<your_account_url>", container="<your_container>")
        # return SQLAlchemyDataLayer(conninfo="<your conninfo>", storage_provider=storage_client)
        data_layer = SQLAlchemyDataLayer(conninfo=DB_CONN_INFO)
        return data_layer
    except Exception as e:      
        logger.exception("Failed to initialize SQLAlchemy data layer.")
        return None

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
_profiles_lock = asyncio.Lock()

@cl.set_chat_profiles
async def chat_profiles():
    global AGENT_PROFILES

    # Fast path: already cached (per process)
    if AGENT_PROFILES is not None:
        return AGENT_PROFILES

    # Prevent duplicate builds when multiple requests hit at once
    async with _profiles_lock:
        if AGENT_PROFILES is not None:
            return AGENT_PROFILES

        agent_list = await cl.make_async(list_agent_names)(limit=30)

        AGENT_PROFILES = [
            cl.ChatProfile(
                name=a.name,
                markdown_description=(
                    f"**Agent:** {a.name}\n\n"
                    f"**Model:** {getattr(a.versions.latest.definition, 'model', 'N/A')}\n\n"
                    f"**Description:** {a.versions.latest.description}"
                ),
            )
            for a in agent_list
        ]

        logger.info("Retrieved %d agents.", len(AGENT_PROFILES))
        return AGENT_PROFILES
    

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
            cl.user_session.set("role", 'admin' if user.identifier == 'admin' else 'user')

        agent_name = cl.user_session.get("chat_profile")
        if not agent_name:
            agent_name = settings.DEFAULT_AGENT_NAME

        cl.user_session.set("agent_name", agent_name)
        # cl.user_session.set("conversation_id", None)
        if cl.user_session.get("conversation_id") is None:
            cl.user_session.set("conversation_id", None)

        await cl.Message(content=f"Starting chat using **{agent_name}**").send()


# -----------------------------
# Messaging
# -----------------------------

@cl.on_message
async def on_message(message: cl.Message):
    text = (message.content or "").strip()
    files = message.elements or []

    # If nothing at all, do nothing
    if not text and not files:
        return

    agent_name = cl.user_session.get("agent_name")
    if not agent_name:
        await cl.Message("No agent selected.").send()
        return
    
    user_id = cl.user_session.get("user_id")
    user_role = cl.user_session.get("role")
    logger.warning(f"Chatting by user '{user_id}','{user_role}'.")
    app_metadata={
        "app_user_role": user_role,
        "app_user_id": user_id 
    }

    conversation_id = cl.user_session.get("conversation_id")
    out = await cl.Message(content="").send()

    try:
        ctx = get_azure_ctx()
        openai_client = ctx.openai_client

        # --- special path: agent-drawer + csv => plot ---
        if files:
            file = files[0]
            file_ext = os.path.splitext(file.name)[1].lower()
            if file_ext == ".csv":
                await cl.Message(content=f"File type '{file_ext}' is not supported.").send()
                return
        # --- SPECIAL: graph-drawer always tries to draw ---
        # if agent_name == "graph-drawer":
        #     csv_path = None
        #     csv_name = None

        #     if files:
        #         file = files[0]
        #         file_ext = os.path.splitext(file.name)[1].lower()
        #         if file_ext == ".csv":
        #             csv_path = file.path
        #             csv_name = file.name
        #         else:
        #             # Option A: reject non-csv
        #             await cl.Message(content=f"File type '{file_ext}' is not supported for graph drawing. Upload a .csv or send text only.").send()
        #             return
        #             # Option B: ignore non-csv and proceed with text-only:
        #             # pass

        #     await out.stream_token("📊 Creating chart...\n")

        #     new_conv_id, local_path, gen_name, response_text = run_graph_drawer(
        #         openai_client=openai_client,
        #         agent_name=agent_name,
        #         conversation_id=conversation_id,
        #         user_text=text,
        #         csv_path=csv_path,
        #         csv_filename=csv_name,
        #     )
        #     cl.user_session.set("conversation_id", new_conv_id)

        #     if response_text:
        #         out.content = response_text
        #         await out.update()

        #     if local_path and os.path.exists(local_path):
        #         await cl.Message(
        #             content="",
        #             elements=[chainlit_file_from_local(local_path)],
        #         ).send()
        #     # else:
        #         # await cl.Message(
        #         #     content="No downloadable chart file was generated. Try asking for a specific chart type and fields, "
        #         #             "or upload a CSV."
        #         # ).send()

        #     return  # IMPORTANT: do not continue normal chat logic
        
        # --- otherwise: continue your normal logic below ---
        # --- Create or continue conversation ---
        if not conversation_id:
            conversation = openai_client.conversations.create(
                items=[{"type": "message", "role": "user", "content": text}],
                metadata=app_metadata
            )
            conversation_id = conversation.id
            input_message = ""
            cl.user_session.set("conversation_id", conversation_id)

            # ✅ persist for resume
            await upsert_thread_metadata({"azure_conversation_id": conversation_id})
        if conversation_id:
            input_message = text

        final_parts: list[str] = []

        # --- Stream response ---
        with openai_client.responses.create(
            conversation=conversation_id,
            extra_body={"agent": {"name": agent_name, "type": "agent_reference"}},
            input=input_message,
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
        if 'APIError' in str(type(e)):
            if 'fabric' in agent_name.lower():
                logger.error("Fabric agents are not supported in this demo. Only user-authenticated are supported.")
                await out.stream_token("\n\n⚠️ Error: Fabric agents are not supported in this demo.")
                out.content = (out.content or "") + "\n\n⚠️ Error: Fabric agents are not supported in this demo."
                await out.update()
                return
        logger.exception("Error handling message.")
        await out.stream_token(f"\n\n⚠️ Error: {type(e).__name__}: {e}")
        out.content = (out.content or "") + f"\n\n⚠️ Error: {type(e).__name__}: {e}"
        await out.update()


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    metadata = thread.get("metadata", {})
    conversation_id = metadata.get("azure_conversation_id")

    if conversation_id:
        cl.user_session.set("conversation_id", conversation_id)
        logger.info(f"Resumed Azure conversation: {conversation_id}")
    else:
        logger.info("No previous Azure conversation found.")

    # Chat Stop
@cl.on_stop
def on_stop():
    logger.info(f"The user wants to stop the task!")

# Chat End
@cl.on_chat_end
def on_chat_end():
    logger.info(f"The user disconnected!")