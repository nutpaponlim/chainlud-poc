# # helper_plot_csv.py
# import os
# import uuid
# from typing import Optional, Tuple
# import chainlit as cl
# import logging
# from azure.ai.projects.models import PromptAgentDefinition, CodeInterpreterTool, CodeInterpreterContainerAuto

# logger = logging.getLogger(__name__)
# def _extract_latest_container_file_citation(response) -> Tuple[str, str, str]:
#     """
#     Returns (file_id, filename, container_id) from the latest container_file_citation found in response output.
#     """
#     for item in reversed(getattr(response, "output", []) or []):
#         if getattr(item, "type", None) != "message":
#             continue
        
#         for content_item in reversed(getattr(item, "content", []) or []):
#             if getattr(content_item, "type", None) != "output_text":
#                 continue

#             annotations = getattr(content_item, "annotations", None) or []
#             for ann in reversed(annotations):
#                 if getattr(ann, "type", None) == "container_file_citation":
#                     file_id = getattr(ann, "file_id", "") or ""
#                     filename = getattr(ann, "filename", "") or ""
#                     container_id = getattr(ann, "container_id", "") or ""
#                     if file_id and filename and container_id:
#                         return file_id, filename, container_id

#     return "", "", ""


# def _collect_response_text(response) -> str:
#     out = []
#     for item in getattr(response, "output", []) or []:
#         if getattr(item, "type", None) == "message":
#             for c in getattr(item, "content", []) or []:
#                 if getattr(c, "type", None) == "output_text":
#                     out.append(getattr(c, "text", "") or "")
#     return "".join(out).strip()


# def run_graph_drawer(
#     *,
#     openai_client,
#     agent_name: str,
#     conversation_id: Optional[str],
#     user_text: str,
#     csv_path: Optional[str] = None,
#     csv_filename: Optional[str] = None,
#     download_dir: str = "./generated",
# ) -> Tuple[str, str, str, str]:
#     """
#     Works with text-only OR text+csv.

#     Returns:
#       (conversation_id, local_file_path, generated_filename, response_text)

#     - If csv_path is provided: uploads CSV and (optionally) passes file id to code interpreter.
#     - If csv_path is None: just asks the agent to generate a chart (it may create synthetic data or ask follow-ups).
#     """
#     os.makedirs(download_dir, exist_ok=True)

#     # 1) Ensure we have a conversation
#     if not conversation_id:
#         conversation = openai_client.conversations.create()
#         conversation_id = conversation.id

#     # 2) Upload CSV if provided
#     uploaded_file_id = None
#     if csv_path:
#         with open(csv_path, "rb") as f:
#             uploaded = openai_client.files.create(purpose="assistants", file=f)
#         uploaded_file_id = uploaded.id
#         tool = CodeInterpreterTool(container=CodeInterpreterContainerAuto(file_ids=[uploaded_file_id]))
#         logger.info(f"File uploaded (id: {uploaded.id})")


#     agent = openai_client.agents.create_version(
#         agent_name=agent_name,
#         definition=PromptAgentDefinition(
#             model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
#             instructions="You are a helpful assistant.",
#             tools=[tool],
#         ),
#         description="Code interpreter agent for data analysis and visualization.",
#     )

#     # 3) Compose prompt (text-only default still tries to create a graph)
#     prompt = (user_text or "").strip()
#     if not prompt:
#         if uploaded_file_id:
#             prompt = (
#                 "Please analyze the uploaded CSV and create a clear, useful chart. "
#                 "Generate the chart and provide it as a downloadable file."
#             )
#         else:
#             prompt = (
#                 "Please create a clear chart based on the information you have. "
#                 "If you need data, you may generate a small synthetic dataset to demonstrate the chart, "
#                 "and provide the chart as a downloadable file."
#             )

#     # 4) Call agent
#     # NOTE: depending on your SDK/agent configuration, you may need to pass file_ids explicitly.
#     extra_body = {"agent": {"name": agent_name, "type": "agent_reference"}}

#     # If your environment requires passing file_ids to code interpreter, uncomment:
#     # if uploaded_file_id:
#     #     extra_body["tools"] = [{"type": "code_interpreter", "file_ids": [uploaded_file_id]}]
#     logger.info(f"Prompt: {prompt}")

    
    
#     response = openai_client.responses.create(
#         conversation=conversation_id,
#         input=prompt,
#         extra_body=extra_body,
#     )

#     response_text = _collect_response_text(response)

#     # 5) Download the latest generated file (if any)
#     file_id, filename, container_id = _extract_latest_container_file_citation(response)
#     local_path = ""
#     safe_name = ""

#     if file_id and filename and container_id:
#         safe_name = os.path.basename(filename) or f"chart_{uuid.uuid4().hex}.bin"
#         local_path = os.path.join(download_dir, safe_name)

#         file_content = openai_client.containers.files.content.retrieve(
#             file_id=file_id,
#             container_id=container_id,
#         )
#         with open(local_path, "wb") as f:
#             f.write(file_content.read())

#     return conversation_id, local_path, safe_name, response_text


# def chainlit_file_from_local(path: str) -> cl.File:
#     abs_path = os.path.abspath(path)
#     _, ext = os.path.splitext(abs_path)
#     name_prefix='output'

#     if not os.path.exists(abs_path):
#         raise FileNotFoundError(f"File not found: {abs_path}")

#     with open(abs_path, "rb") as f:
#         data = f.read()

#     return cl.File(
#         # name=os.path.basename(abs_path),
#         name=f"{name_prefix}{ext}",
#         content=data,      # 🔑 IMPORTANT: use content, not path
#         display="inline",  # inline preview for images
#     )