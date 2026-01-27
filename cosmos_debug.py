import asyncio
import json
import os
from azure.cosmos.aio import CosmosClient

# Get your Cosmos DB credentials from environment variables


# Your database and container names



async def fetch_thread_and_steps(thread_id: str):
    """
    Connects to Cosmos DB and fetches a specific thread and its related steps.
    """
    if not ENDPOINT or not KEY:
        print("🔴 ERROR: Please set Azure_Cosmos_Endpoint and Azure_Cosmos_KEY in your environment or .env file.")
        return

    print(f"\n🔍 Attempting to connect to Cosmos DB at endpoint: {ENDPOINT[:30]}...")

    async with CosmosClient(url=ENDPOINT, credential=KEY) as client:
        try:
            database = client.get_database_client(DATABASE_NAME)
            threads_container = database.get_container_client(THREADS_CONTAINER_NAME)
            steps_container = database.get_container_client(STEPS_CONTAINER_NAME)
            print("✅ Connection successful. Database and containers are ready.")
        except Exception as e:
            print(f"🔴 ERROR: Could not connect to the database or containers. Details: {e}")
            return

        # --- 1. Fetch the Thread Document ---
        print(f"\n--- Fetching Thread Document for ID: {thread_id} ---")
        thread_query = "SELECT * FROM c WHERE c.id = @thread_id"
        thread_params = [{"name": "@thread_id", "value": thread_id}]

        try:
            # The partition key for the 'threads' container is the thread's own ID
            thread_items = [item async for item in threads_container.query_items(
                query=thread_query,
                parameters=thread_params,
                partition_key=thread_id
            )]

            if thread_items:
                print("✅ Thread document FOUND.")
                print(json.dumps(thread_items[0], indent=4))
                thread_found = True
            else:
                print("❌ Thread document NOT FOUND.")
                thread_found = False

        except Exception as e:
            print(f"🔴 An error occurred while fetching the thread: {e}")
            thread_found = False


        # --- 2. Fetch the Related Step Documents ---
        if thread_found:
            print(f"\n--- Fetching Steps for Thread ID: {thread_id} ---")
            steps_query = "SELECT * FROM c WHERE c.threadId = @thread_id ORDER BY c.createdAt"
            steps_params = [{"name": "@thread_id", "value": thread_id}]

            try:
                # The partition key for the 'steps' container is the threadId
                step_items = [item async for item in steps_container.query_items(
                    query=steps_query,
                    parameters=steps_params,
                    partition_key=thread_id
                )]

                if step_items:
                    print(f"✅ Found {len(step_items)} step(s).")
                    for i, step in enumerate(step_items):
                        print(f"\n--- Step {i+1} ---")
                        print(json.dumps(step, indent=4))
                else:
                    print("🟡 No steps found for this thread.")

            except Exception as e:
                print(f"🔴 An error occurred while fetching steps: {e}")


async def main():
    thread_id_to_debug = input("Enter the thread_id to debug: ")
    if thread_id_to_debug:
        await fetch_thread_and_steps(thread_id_to_debug)
    else:
        print("No thread_id entered. Exiting.")

if __name__ == "__main__":
    asyncio.run(main())