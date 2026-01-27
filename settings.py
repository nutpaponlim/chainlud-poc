# settings.py
from dotenv import load_dotenv
from pydantic import BaseModel
import os

# Load .env into environment variables
load_dotenv()


class Settings(BaseModel):
    # App
    APP_NAME: str = os.getenv("APP_NAME", "chainlit-app")
    ENV: str = os.getenv("ENV", "local")

    # Azure OpenAI (placeholder for now)
    AOAI_ENDPOINT: str = os.getenv("AOAI_ENDPOINT", "")
    AOAI_KEY: str = os.getenv("AOAI_KEY", "")
    AOAI_API_VERSION: str = os.getenv("AOAI_API_VERSION", "2024-10-21")


    PROJECT_ENDPOINT: str = os.getenv("PROJECT_ENDPOINT", "")
    DEFAULT_AGENT_NAME: str = os.getenv("DEFAULT_AGENT_NAME", "agent-km")

    BUCKET_NAME: str = os.getenv("BUCKET_NAME", "")
    APP_AZURE_STORAGE_ACCOUNT: str = os.getenv("APP_AZURE_STORAGE_ACCOUNT", "")
    APP_AZURE_STORAGE_ACCESS_KEY: str = os.getenv("APP_AZURE_STORAGE_ACCESS_KEY", "")

    # Azure Cosmos DB
    Azure_Cosmos_Endpoint: str = os.getenv("Azure_Cosmos_Endpoint", "")
    Azure_Cosmos_KEY: str = os.getenv("Azure_Cosmos_KEY", "")
    Azuredb: str = os.getenv("Azuredb", "")

# Single shared settings object
settings = Settings()
