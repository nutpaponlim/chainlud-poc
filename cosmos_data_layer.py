import asyncio
import uuid
from datetime import datetime, timezone
from dataclasses import asdict
from typing import TYPE_CHECKING, Dict, List, Optional, Any

from azure.cosmos import CosmosClient, PartitionKey
from azure.cosmos.exceptions import CosmosResourceNotFoundError, CosmosHttpResponseError

import chainlit as cl
from chainlit.data.base import BaseDataLayer
from chainlit.types import (
    Feedback,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadDict,
    ThreadFilter,
)
from chainlit.data.utils import queue_until_user_message
import logging
logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from chainlit.element import Element, ElementDict
    from chainlit.step import StepDict
    from chainlit.user import PersistedUser, User

class CosmosDBDataLayer(BaseDataLayer):
    """
    A direct implementation of the BaseDataLayer for Azure Cosmos DB,
    mirroring the logic of the official Chainlit data layers.
    """

    def __init__(
        self,
        endpoint: str,
        key: str,
        database_name: str = "RA",
    ):
        self.client = CosmosClient(endpoint, key)
        self.database_name = database_name

        self.container_configs = {
            'users': {'name': 'users', 'partition_key': '/id'},
            'threads': {'name': 'threads', 'partition_key': '/id'},
            'steps': {'name': 'steps', 'partition_key': '/threadId'},
            'elements': {'name': 'elements', 'partition_key': '/threadId'}
        }

        self._initialize_database()
        logger.info("Cosmos DB data layer initialized.")

    def _initialize_database(self):
        logger.debug(f"Attempting to initialize Cosmos DB database: '{self.database_name}' and containers.")
        try:
            database = self.client.create_database_if_not_exists(id=self.database_name)
            logger.debug(f"Database '{self.database_name}' ensured to exist.")
            for config in self.container_configs.values():
                container_name = config['name']
                partition_key_path = config['partition_key']
                database.create_container_if_not_exists(
                    id=container_name,
                    partition_key=PartitionKey(path=partition_key_path)
                )
                logger.debug(f"Container '{container_name}' with partition key '{partition_key_path}' ensured to exist.")
        except CosmosHttpResponseError as e:
            logger.error(f"Error initializing Cosmos DB: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"An unexpected error occurred during Cosmos DB initialization: {e}", exc_info=True)
            raise

    def _get_container(self, container_type: str):
        config = self.container_configs[container_type]
        database = self.client.get_database_client(self.database_name)
        return database.get_container_client(config['name'])

    def _serialize(self, doc: Dict):
        for k, v in doc.items():
            if isinstance(v, datetime):
                doc[k] = v.isoformat() + "Z"
            elif isinstance(v, dict):
                self._serialize(v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        self._serialize(item)
        return doc

    def _clean_item(self, item: Dict) -> Dict:
        """Removes Cosmos DB internal fields starting with '_'."""
        if not item:
            return {}
        return {k: v for k, v in item.items() if not k.startswith('_')}

    async def get_user(self, identifier: str) -> Optional["PersistedUser"]:
        logger.debug(f"Attempting to retrieve user with identifier: '{identifier}'.")
        try:
            container = self._get_container('users')
            user_doc = container.read_item(item=identifier, partition_key=identifier)
            logger.debug(f"User '{identifier}' found in DB.")
            cleaned_user_doc = self._clean_item(user_doc)
            # Ensure 'identifier' is a string, fallback to 'id' if it's None
            if cleaned_user_doc.get('identifier') is None:
                cleaned_user_doc['identifier'] = cleaned_user_doc.get('id', identifier)
            return cl.PersistedUser(**cleaned_user_doc)
        except CosmosResourceNotFoundError:
            logger.info(f"User with identifier '{identifier}' not found in DB.")
            return None
        except Exception as e:
            logger.error(f"Error retrieving user '{identifier}': {e}", exc_info=True)
            return None

    async def create_user(self, user: "User") -> Optional["PersistedUser"]:
        container = self._get_container('users')
        user_doc = {
            "id": user.identifier,
            "identifier": user.display_name or user.identifier, # Ensure identifier is never None
            "createdAt": datetime.utcnow().isoformat() + "Z",
            "metadata": user.metadata or {},
        }
        try:
            container.create_item(body=user_doc)
            logger.info(f"User '{user.identifier}' created successfully.")
        except CosmosHttpResponseError as e:
            if e.status_code == 409:
                logger.info(f"User '{user.identifier}' already exists, retrieving existing user.")
                return await self.get_user(user.identifier)
            logger.error(f"Failed to create user '{user.identifier}': {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"An unexpected error occurred while creating user '{user.identifier}': {e}", exc_info=True)
            return None
        return cl.PersistedUser(**user_doc)
    
    async def get_or_create_user(self, user: "User") -> Optional["PersistedUser"]:
        existing_user = await self.get_user(user.identifier)
        if existing_user:
            return existing_user
        return await self.create_user(user)

    async def upsert_feedback(self, feedback: Feedback) -> str:
        if not feedback.threadId or not feedback.forId:
            raise ValueError("Feedback must have a threadId and forId")

        steps_container = self._get_container('steps')

        # Find the child step (the assistant message) whose parent is the message the feedback is for.
        query = "SELECT * FROM c WHERE c.threadId = @thread_Id AND c.parentId = @parent_Id"
        params = [
            {"name": "@thread_Id", "value": feedback.threadId},
            {"name": "@parent_Id", "value": feedback.forId}
        ]
        
        # This query is efficient because it uses the partition key (threadId)
        items = list(steps_container.query_items(query=query, parameters=params, partition_key=feedback.threadId))

        if not items:
            step_to_update = await self._get_step(feedback.threadId, feedback.forId)
        else:
            step_to_update = items[0]

        if not step_to_update:
            raise ValueError(f"Step with id or parentId {feedback.forId} not found.")

        feedback.id = f"{feedback.threadId}::{feedback.forId}"
        step_to_update['feedback'] = self._serialize(asdict(feedback))
        steps_container.upsert_item(body=step_to_update)
        
        logger.info(f"Feedback upserted for step {step_to_update['id']}.")
        return feedback.id

    async def delete_feedback(self, feedback_id: str) -> bool:
        try:
            thread_id, for_id = feedback_id.split("::")
        except ValueError:
            logger.error(f"Invalid feedback_id format: {feedback_id}")
            return False

        steps_container = self._get_container('steps')
        
        query = "SELECT * FROM c WHERE c.threadId = @thread_id AND c.parentId = @parent_id"
        params = [
            {"name": "@thread_id", "value": thread_id},
            {"name": "@parent_id", "value": for_id}
        ]
        items = list(steps_container.query_items(query=query, parameters=params, partition_key=thread_id))

        if not items:
            step_to_update = await self._get_step(thread_id, for_id)
        else:
            step_to_update = items[0]

        if step_to_update and 'feedback' in step_to_update:
            del step_to_update['feedback']
            steps_container.upsert_item(body=step_to_update)
            logger.info(f"Feedback deleted for step {step_to_update['id']}.")
            return True
        
        return False

    @queue_until_user_message()
    async def create_step(self, step_dict: "StepDict"):
        container = self._get_container('steps')
        try:
            container.create_item(body=self._serialize(step_dict))
            logger.debug(f"Step '{step_dict.get('id', 'N/A')}' created for thread '{step_dict.get('threadId', 'N/A')}'.")
        except Exception as e:
            logger.error(f"Error creating step '{step_dict.get('id', 'N/A')}' for thread '{step_dict.get('threadId', 'N/A')}': {e}", exc_info=True)

    @queue_until_user_message()
    async def update_step(self, step_dict: "StepDict"):
        container = self._get_container('steps')
        try:
            container.upsert_item(body=self._serialize(step_dict))
            logger.debug(f"Step '{step_dict.get('id', 'N/A')}' updated for thread '{step_dict.get('threadId', 'N/A')}'.")
        except Exception as e:
            logger.error(f"Error updating step '{step_dict.get('id', 'N/A')}' for thread '{step_dict.get('threadId', 'N/A')}': {e}", exc_info=True)

    async def get_thread(self, thread_id: str) -> "Optional[ThreadDict]":
        threads_container = self._get_container('threads')
        logger.debug(f"Attempting to retrieve thread with ID: '{thread_id}'.")
        
        query = "SELECT * FROM c WHERE c.id = @thread_id"
        params = [{"name": "@thread_id", "value": thread_id}]
        
        try:
            items = list(threads_container.query_items(query=query, parameters=params, partition_key=thread_id))
        except Exception as e:
            logger.error(f"Error querying thread '{thread_id}': {e}", exc_info=True)
            return None
        
        if not items:
            logger.warning(f"Thread with id '{thread_id}' not found in DB.")
            return None
            
        thread_doc_raw = self._clean_item(items[0])
        logger.debug(f"Thread '{thread_id}' found. Fetching steps and elements.")
        
        steps = await self.get_steps(thread_id)
        
        elements_container = self._get_container('elements')
        elements_query = "SELECT * FROM c WHERE c.threadId = @thread_id"
        element_params = [{"name": "@thread_id", "value": thread_id}]
        try:
            elements = list(elements_container.query_items(query=elements_query, parameters=element_params, partition_key=thread_id))
        except Exception as e:
            logger.error(f"Error querying elements for thread '{thread_id}': {e}", exc_info=True)
            elements = []

        # Manually construct the ThreadDict to ensure it matches the required shape
        thread_doc: ThreadDict = {
            "id": thread_doc_raw.get("id"),
            "createdAt": thread_doc_raw.get("createdAt"),
            "name": thread_doc_raw.get("name"),
            "userId": thread_doc_raw.get("userId"),
            "userIdentifier": thread_doc_raw.get("userIdentifier"),
            "tags": thread_doc_raw.get("tags"),
            "metadata": thread_doc_raw.get("metadata"),
            "steps": [self._clean_item(s) for s in steps],
            "elements": [self._clean_item(e) for e in elements],
        }
        logger.debug(f"Thread '{thread_id}' successfully constructed.")
        return thread_doc

    async def list_threads(self, pagination: Pagination, filters: ThreadFilter) -> PaginatedResponse[ThreadDict]:
        container = self._get_container('threads')
        logger.debug(f"Listing threads for user '{filters.userId}' with pagination cursor '{pagination.cursor}'.")
        
        query = "SELECT * FROM c WHERE c.userId = @user_id"
        params = [{"name": "@user_id", "value": filters.userId}]

        if filters.search:
            query += " AND CONTAINS(c.name, @search, true)"
            params.append({"name": "@search", "value": filters.search})
            
        query += " ORDER BY c.createdAt DESC"
        
        try:
            items = list(container.query_items(
                query=query,
                parameters=params,
                enable_cross_partition_query=True
            ))
        except Exception as e:
            logger.error(f"Error listing threads for user '{filters.userId}': {e}", exc_info=True)
            return PaginatedResponse(data=[], pageInfo=PageInfo(hasNextPage=False, startCursor=None, endCursor=None))
        
        start = int(pagination.cursor) if pagination.cursor else 0
        end = start + pagination.first
        paginated_items = items[start:end]

        has_next_page = len(items) > end
        next_cursor = str(end) if has_next_page else None

        data = [
            ThreadDict(
                id=item['id'],
                name=item.get('name'),
                createdAt=item.get('createdAt')
            )
            for item in paginated_items
        ]
        logger.debug(f"Successfully listed {len(data)} threads for user '{filters.userId}'. Has next page: {has_next_page}.")
        return PaginatedResponse(
            data=data,
            pageInfo=PageInfo(
                hasNextPage=has_next_page,
                startCursor=pagination.cursor,
                endCursor=next_cursor
            ),
        )

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ):
        container = self._get_container('threads')
        logger.debug(f"Attempting to update thread '{thread_id}'.")
        try:
            item = container.read_item(item=thread_id, partition_key=thread_id)
        except CosmosResourceNotFoundError:
            item = {"id": thread_id, "createdAt": datetime.utcnow().isoformat() + "Z"}
            logger.info(f"Thread '{thread_id}' not found for update, creating new thread.")
        except Exception as e:
            logger.error(f"Error reading thread '{thread_id}' for update: {e}", exc_info=True)
            return

        if name is not None:
            item["name"] = name
        if user_id is not None:
            item["userId"] = user_id
            # As per the ThreadDict spec and dynamodb.py, set both
            item["userIdentifier"] = user_id
        if metadata is not None:
            if "metadata" not in item or item["metadata"] is None:
                item["metadata"] = {}
            item["metadata"].update(metadata)
        if tags is not None:
            item["tags"] = tags

        try:
            container.upsert_item(body=self._serialize(item))
            logger.info(f"Thread '{thread_id}' updated successfully.")
        except Exception as e:
            logger.error(f"Error upserting thread '{thread_id}': {e}", exc_info=True)
        
    async def get_steps(self, thread_id: str) -> List[Dict[str, Any]]:
        container = self._get_container('steps')
        logger.debug(f"Attempting to retrieve steps for thread '{thread_id}'.")
        query = "SELECT * FROM c WHERE c.threadId = @thread_id ORDER BY c.createdAt"
        params = [{"name": "@thread_id", "value": thread_id}]

        try:
            items = list(container.query_items(
                query=query,
                parameters=params,
                partition_key=thread_id
            ))
            logger.debug(f"Retrieved {len(items)} steps for thread '{thread_id}'.")
            return items
        except Exception as e:
            logger.error(f"Error retrieving steps for thread '{thread_id}': {e}", exc_info=True)
            return []

    @queue_until_user_message()
    async def create_element(self, element: "Element"):
        container = self._get_container('elements')
        try:
            container.upsert_item(body=self._serialize(asdict(element)))
            logger.debug(f"Element '{element.id}' created/upserted.")
        except Exception as e:
            logger.error(f"Error creating/upserting element '{element.id}': {e}", exc_info=True)

    async def get_element(self, thread_id: str, element_id: str) -> Optional["ElementDict"]:
        container = self._get_container('elements')
        logger.debug(f"Attempting to retrieve element '{element_id}' for thread '{thread_id}'.")
        try:
            item = container.read_item(item=element_id, partition_key=thread_id)
            logger.debug(f"Element '{element_id}' found for thread '{thread_id}'.")
            return self._clean_item(item)
        except CosmosResourceNotFoundError:
            logger.info(f"Element '{element_id}' not found for thread '{thread_id}'.")
            return None
        except Exception as e:
            logger.error(f"Error retrieving element '{element_id}' for thread '{thread_id}': {e}", exc_info=True)
            return None
    
    @queue_until_user_message()
    async def delete_element(self, element_id: str, thread_id: Optional[str] = None):
        if not thread_id:
            logger.error("delete_element requires a thread_id for Cosmos DB.")
            return
        container = self._get_container('elements')
        logger.debug(f"Attempting to delete element '{element_id}' from thread '{thread_id}'.")
        try:
            container.delete_item(item=element_id, partition_key=thread_id)
            logger.info(f"Element '{element_id}' deleted from thread '{thread_id}'.")
        except CosmosResourceNotFoundError:
            logger.info(f"Element '{element_id}' not found for deletion in thread '{thread_id}'.")
            pass
        except Exception as e:
            logger.error(f"Error deleting element '{element_id}' from thread '{thread_id}': {e}", exc_info=True)
            
    @queue_until_user_message()
    async def delete_step(self, step_id: str):
        container = self._get_container('steps')
        logger.debug(f"Attempting to delete step '{step_id}'.")
        query = "SELECT * FROM c WHERE c.id = @step_id"
        params = [{"name": "@step_id", "value": step_id}]
        try:
            items = list(container.query_items(query, params, enable_cross_partition_query=True))
            if items:
                thread_id = items[0]['threadId']
                container.delete_item(item=step_id, partition_key=thread_id)
                logger.info(f"Step '{step_id}' deleted from thread '{thread_id}'.")
            else:
                logger.info(f"Step '{step_id}' not found for deletion.")
        except CosmosResourceNotFoundError:
            logger.info(f"Step '{step_id}' not found for deletion.")
            pass
        except Exception as e:
            logger.error(f"Error deleting step '{step_id}': {e}", exc_info=True)

    async def get_thread_author(self, thread_id: str) -> str:
        threads_container = self._get_container('threads')
        logger.debug(f"Attempting to get author for thread '{thread_id}'.")
        try:
            item = threads_container.read_item(item=thread_id, partition_key=thread_id)
            author = item.get("userId", "")
            logger.debug(f"Author '{author}' retrieved for thread '{thread_id}'.")
            return author
        except CosmosResourceNotFoundError:
            logger.info(f"Thread '{thread_id}' not found when getting author.")
            return ""
        except Exception as e:
            logger.error(f"Error getting thread author for '{thread_id}': {e}", exc_info=True)
            return ""

    async def delete_thread(self, thread_id: str):
        container = self._get_container('threads')
        logger.debug(f"Attempting to delete thread '{thread_id}'.")
        try:
            container.delete_item(item=thread_id, partition_key=thread_id)
            logger.info(f"Thread '{thread_id}' deleted successfully.")
        except CosmosResourceNotFoundError:
            logger.info(f"Thread '{thread_id}' not found for deletion.")
            pass
        except Exception as e:
            logger.error(f"Error deleting thread '{thread_id}': {e}", exc_info=True)

    async def build_debug_url(self) -> str:
        return f"CosmosDB - DB: {self.database_name}"
    
    async def close(self):
        # Implementation to close connection
        pass

    async def get_favorite_steps(self, *args, **kwargs):
        # Implementation to fetch favorite steps
        return []
