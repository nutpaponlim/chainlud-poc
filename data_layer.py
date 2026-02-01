import chainlit as cl
import chainlit.data as cl_data

async def upsert_thread_metadata(patch: dict):
    dl = getattr(cl_data, "_data_layer", None)
    if not dl:
        return  # no persistence configured

    thread_id = getattr(cl.context.session, "thread_id", None)
    if not thread_id:
        return  # thread not created yet

    # Read existing metadata so we don't overwrite it
    thread = await dl.get_thread(thread_id)
    existing = (thread.get("metadata") or {}) if thread else {}
    merged = {**existing, **patch}

    await dl.update_thread(thread_id=thread_id, metadata=merged)
